import queue
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sessiongauge.persistence import MonitorDB
from sessiongauge.tui import TUIState, _worker, handle_key, render_frame


@pytest.mark.parametrize("owned_child", [False, True])
def test_tui_quit_cancels_blocked_synchronous_quota(tmp_path, monkeypatch, owned_child):
    from test_sidebar import Adapter, Runner

    from sessiongauge import cli, quota, sidebar, tui
    from sessiongauge.config import Config

    entered, release, cancelled, closed = (threading.Event() for _ in range(4))
    errors, owners = [], []
    runner = Runner()
    home = tmp_path / "quota-home"
    helper = tmp_path / "helper"
    helper.touch()
    config = Config(
        sidebar_enabled=True,
        sidebar_quota_enabled=True,
        sidebar_quota_mode="api",
        sidebar_quota_hermes_home=home,
        sidebar_quota_auth_helper=helper,
    )
    children = []

    class Child:
        returncode = None

        def __init__(self, *args, **kwargs):
            children.append(self)

        def communicate(self, **kwargs):
            entered.set()
            assert release.wait(3)
            return '{"threads":[]}', ""

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15
            cancelled.set()
            release.set()

        def wait(self, timeout):
            assert 0 < timeout <= 1
            assert self.returncode is not None
            return self.returncode

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def read_sessions(self, threads):
            entered.set()
            assert release.wait(3), "テストの読取待機が解除されなかった"
            return {"s": 12}

        def close(self):
            cancelled.set()
            release.set()

    real_reporter = sidebar.SidebarReporter

    def reporter_factory(**kwargs):
        reporter = real_reporter(**kwargs, runner=runner, adapter=Adapter())
        owners.append(reporter)
        return reporter

    class Collector:
        def __init__(self, config):
            self.owner = threading.get_ident()
            self.monitor = MonitorDB(tmp_path / "monitor.db").__enter__()
            self.herdr_state = SimpleNamespace(last_success=Adapter().list_panes())

        def collect(self):
            return {
                "panes": [
                    {
                        "pane_id": "p1",
                        "terminal_id": "t1",
                        "status": "matched",
                        "profile": owners[0].quota_profile_id,
                        "session": {"id": "s", "total_tokens": 100},
                    }
                ]
            }

        def close(self):
            assert threading.get_ident() == self.owner
            self.monitor.connection.execute("SELECT 1")
            self.monitor.__exit__()
            closed.set()

    class Screen:
        def nodelay(self, value):
            pass

        def getmaxyx(self):
            return 24, 100

        def erase(self):
            pass

        def addnstr(self, *args):
            pass

        def refresh(self):
            pass

        def getch(self):
            assert entered.wait(2)
            return ord("q")

    monkeypatch.setattr(cli, "Collector", Collector)
    if owned_child:
        monkeypatch.setattr(quota.subprocess, "Popen", Child)
    else:
        monkeypatch.setattr(quota, "QuotaReader", Reader)
    monkeypatch.setattr(sidebar, "SidebarReporter", reporter_factory)

    def run():
        try:
            tui.run_tui(config, cli._sidebar_factory(config), screen_runner=lambda ui: ui(Screen()))
        except (AssertionError, RuntimeError, OSError, sqlite3.Error) as exc:
            errors.append(exc)

    ui_thread = threading.Thread(target=run)
    ui_thread.start()
    try:
        assert entered.wait(2)
        assert cancelled.wait(1), "quit did not cancel the blocked quota reader"
        assert closed.wait(1), "worker did not close its own SQLite connection"
    finally:
        release.set()
        ui_thread.join(12)
    assert not ui_thread.is_alive()
    assert not errors
    assert all(child.poll() is not None for child in children)
    if owned_child:
        assert len(children) == 1
        assert not owners[0].quota_reader._children
    assert not [cmd for cmd in runner.commands if "--token" in cmd or "--clear-token" in cmd]


def test_quit_during_collector_creation_cancels_before_collect():
    from sessiongauge.tui import _WorkerLifecycle

    entered, release = threading.Event(), threading.Event()
    events = []
    lifecycle = _WorkerLifecycle()

    class Collector:
        def cancel(self):
            events.append("cancel")

        def collect(self):
            events.append("collect")

        def close(self):
            events.append("close")

    def factory(config):
        entered.set()
        assert release.wait(2)
        return Collector()

    stop = threading.Event()
    worker = threading.Thread(
        target=_worker,
        args=(None, factory, queue.Queue(), stop, threading.Event(), 1, None, lifecycle),
    )
    worker.start()
    try:
        assert entered.wait(1)
        stop.set()
        lifecycle.cancel()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert events == ["cancel", "close"]


def test_worker_limits_historical_sessions_to_configured_profiles(tmp_path):
    selected_path = tmp_path / "selected" / "state.db"
    excluded_path = tmp_path / "excluded" / "state.db"
    monitor_path = tmp_path / "monitor.db"
    config = SimpleNamespace(hermes_db_paths=(selected_path,))

    with MonitorDB(monitor_path, source_paths=(selected_path, excluded_path)) as monitor:
        selected_profile = monitor.register_profile("selected", selected_path)
        excluded_profile = monitor.register_profile("excluded", excluded_path)
        monitor.connection.executemany(
            "INSERT INTO session_catalog(profile_id,session_id,title) VALUES(?,?,?)",
            [
                (selected_profile, "selected-session", "selected history"),
                (excluded_profile, "excluded-session", "excluded history"),
            ],
        )
        monitor.connection.commit()
        stop, refresh, messages = threading.Event(), threading.Event(), queue.Queue()

        class Collector:
            def __init__(self, _config):
                self.monitor = monitor

            def collect(self):
                stop.set()
                return {"panes": []}

            def close(self):
                pass

        _worker(config, Collector, messages, stop, refresh, 1, None)
        payload, error = messages.get_nowait()

    assert error is None
    sessions = {row["session"]["id"] for row in payload["panes"]}
    assert sessions == {"selected-session"}
    assert {row["profile"] for row in payload["panes"]} == {selected_profile}

    all_view = render_frame(
        payload,
        TUIState(filter_by="all"),
        width=120,
        height=30,
        config=SimpleNamespace(time_zone="UTC"),
    )
    assert "Totals sessions=1" in "\n".join(all_view)
    assert "excluded" not in "\n".join(all_view)
    cycled = handle_key(TUIState(), "p", payload)
    assert cycled.profile_index == 0
    assert "excluded" not in "\n".join(
        render_frame(payload, cycled, width=120, config=SimpleNamespace(time_zone="UTC"))
    )


@pytest.mark.parametrize(
    "error",
    [OSError("private path /secret/state.db"), sqlite3.OperationalError("private database detail")],
)
def test_worker_recovers_when_collector_factory_fails_once(error):
    stop, refresh, messages = threading.Event(), threading.Event(), queue.Queue()
    factory_calls = 0

    class RetryRefresh:
        """待機を同期化し、sleepなしで手動更新を模擬する。"""

        calls = 0

        def wait(self, timeout):
            assert timeout == 7
            self.calls += 1
            if self.calls == 1:
                # 初回エラー後、待機中の手動更新で再試行を起こす。
                self.requested = True
            else:
                stop.set()

        def clear(self):
            self.requested = False

        requested = False

        def set(self):
            self.requested = True

    refresh = RetryRefresh()

    class Collector:
        closed = False

        def collect(self):
            return {"panes": [{"profile": "selected", "session": {"id": "recovered"}}]}

        def close(self):
            self.closed = True

    recovered = Collector()

    def factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise error
        return recovered

    _worker(None, factory, messages, stop, refresh, 7, None)

    first_payload, first_error = messages.get_nowait()
    second_payload, second_error = messages.get_nowait()
    assert first_payload["status"] == "error"
    assert first_error == type(error).__name__
    assert "private" not in first_error
    assert second_error is None
    assert second_payload["panes"][0]["session"]["id"] == "recovered"
    assert factory_calls == 2
    assert refresh.calls == 2
    assert recovered.closed
