from sessiongauge.cli import main
from sessiongauge.monitor_lock import MonitorAlreadyRunning, MonitorLease, resolve_endpoint


def test_continuous_json_refuses_existing_scope_before_collector_start(
    tmp_path, monkeypatch, capsys
):
    endpoint = str(tmp_path / "herdr.sock")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)
    started = []

    class Collector:
        def collect(self):
            raise KeyboardInterrupt

        def close(self):
            pass

    def factory(_config):
        started.append(True)
        return Collector()

    monkeypatch.setattr("sessiongauge.cli._sidebar_factory", lambda _config: factory)

    with MonitorLease(data_dir, endpoint):
        assert main(["--data-dir", str(data_dir), "live", "--json"]) == 0

    assert started == []
    assert "already running" in capsys.readouterr().err


def test_continuous_json_interruption_releases_lease(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    endpoint = str(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)

    class Collector:
        def collect(self):
            raise KeyboardInterrupt

        def close(self):
            pass

    monkeypatch.setattr("sessiongauge.cli._sidebar_factory", lambda _config: lambda _c: Collector())
    assert main(["--data-dir", str(data_dir), "live", "--json"]) == 130
    with MonitorLease(data_dir, endpoint):
        pass


def test_live_close_error_still_releases_lease(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    endpoint = str(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)

    class Collector:
        def collect(self):
            raise KeyboardInterrupt

        def close(self):
            raise OSError("close failed")

    monkeypatch.setattr("sessiongauge.cli._sidebar_factory", lambda _config: lambda _c: Collector())
    assert main(["--data-dir", str(data_dir), "live", "--json"]) == 1
    with MonitorLease(data_dir, endpoint):
        pass


def test_continuous_data_directory_is_private(tmp_path, monkeypatch):
    import os
    import stat

    data_dir = tmp_path / "new-data"
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "herdr.sock"))
    monkeypatch.setattr(
        "sessiongauge.cli._sidebar_factory",
        lambda _config: (
            lambda _c: type(
                "C",
                (),
                {
                    "collect": lambda self: (_ for _ in ()).throw(KeyboardInterrupt()),
                    "close": lambda self: None,
                },
            )()
        ),
    )
    main(["--data-dir", str(data_dir), "live", "--json"])
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_tui_entry_refuses_duplicate_and_releases_on_exception(tmp_path, monkeypatch):
    from sessiongauge.cli import _continuous_tui

    endpoint = str(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = type("Config", (), {"data_dir": data_dir})()
    starts = []

    with MonitorLease(data_dir, endpoint):
        monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "other.sock"))
        assert _continuous_tui(config, lambda *_args, **_kwargs: 23) == 23
        monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)
        try:
            _continuous_tui(config, lambda *_args, **_kwargs: starts.append(True))
        except MonitorAlreadyRunning:
            pass
        else:
            raise AssertionError("duplicate TUI monitor was allowed")
    assert starts == []

    def broken(*_args, lease, **_kwargs):
        lease.__exit__(None, None, None)
        raise RuntimeError("TUI failed")

    try:
        _continuous_tui(config, broken)
    except RuntimeError:
        pass
    else:
        raise AssertionError("TUI exception was swallowed")
    with MonitorLease(data_dir, endpoint):
        pass


def test_tui_lease_lives_until_worker_collector_closes(tmp_path, monkeypatch):
    import threading

    from sessiongauge.cli import _continuous_tui
    from sessiongauge.tui import run_tui

    endpoint = str(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", endpoint)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = type("Config", (), {"data_dir": data_dir, "poll_interval_seconds": 3})()
    collecting = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    released = threading.Event()
    real_join = threading.Thread.join
    monkeypatch.setattr(threading.Thread, "join", lambda self, timeout=None: real_join(self, 0.01))
    lease = MonitorLease(data_dir, endpoint)
    original_exit = lease.__exit__

    def release_lease(*args):
        try:
            return original_exit(*args)
        finally:
            released.set()

    lease.__exit__ = release_lease
    monkeypatch.setattr("sessiongauge.cli._continuous_lease", lambda _config: lease)

    class Collector:
        def collect(self):
            collecting.set()
            release.wait()
            return {"panes": [], "sessions": []}

        def close(self):
            closed.set()

    class Screen:
        def nodelay(self, _value):
            pass

        def keypad(self, _value):
            pass

        def getmaxyx(self):
            return (12, 80)

        def erase(self):
            pass

        def addnstr(self, *_args):
            pass

        def refresh(self):
            pass

        def getch(self):
            return ord("q")

    def run_with_lease(_config, factory, *, lease):
        return run_tui(
            config,
            factory,
            lease=lease,
            screen_runner=lambda ui: (collecting.wait(2), ui(Screen())),
        )

    monkeypatch.setattr("sessiongauge.cli._sidebar_factory", lambda _config: lambda _c: Collector())
    assert _continuous_tui(config, run_with_lease) == 0
    assert collecting.is_set()
    assert not closed.is_set()
    try:
        with MonitorLease(data_dir, endpoint):
            raise AssertionError("lease was released while the worker was active")
    except MonitorAlreadyRunning:
        pass

    release.set()
    assert closed.wait(2)
    assert released.wait(2)
    with MonitorLease(data_dir, endpoint):
        pass


def test_tui_thread_start_failure_releases_lease(tmp_path, monkeypatch):
    import threading

    import pytest

    from sessiongauge.tui import run_tui

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    endpoint = str(tmp_path / "herdr.sock")
    lease = MonitorLease(data_dir, endpoint)
    lease.__enter__()
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )
    config = type("Config", (), {"poll_interval_seconds": 3})()
    with pytest.raises(RuntimeError, match="thread start failed"):
        run_tui(config, lambda _config: None, lease=lease)
    with MonitorLease(data_dir, endpoint):
        pass


def test_endpoint_resolver_uses_status_contract_when_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HERDR_BIN_PATH", "/fixture/herdr")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return type(
            "Result", (), {"returncode": 0, "stdout": '{"server":{"socket":"/tmp/server.sock"}}'}
        )()

    assert resolve_endpoint(runner=runner, which=lambda _name: None) == "/tmp/server.sock"
    assert calls[0][0] == ["/fixture/herdr", "status", "--json"]
    assert calls[0][1]["timeout"] == 5
