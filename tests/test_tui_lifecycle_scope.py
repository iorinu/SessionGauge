import queue
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sessiongauge.persistence import MonitorDB
from sessiongauge.tui import TUIState, _worker, handle_key, render_frame


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
