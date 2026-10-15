import json
import os
import plistlib
import signal
import tomllib
from pathlib import Path
from types import SimpleNamespace

from sessiongauge import cli


def test_cli_install_generates_files_and_bootstraps(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    data_dir = tmp_path / "data"
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "print":
            return type(
                "Result", (), {"returncode": 113, "stdout": "", "stderr": "Could not find service"}
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        cli, "platform", type("Platform", (), {"system": staticmethod(lambda: "Darwin")})()
    )
    monkeypatch.setattr(
        cli,
        "shutil",
        type(
            "Shutil",
            (),
            {
                "which": staticmethod(
                    lambda name: "/opt/herdr/bin/herdr" if name == "herdr" else None
                )
            },
        )(),
    )
    monkeypatch.setattr("sessiongauge.autostart.subprocess.run", runner)

    assert (
        cli.main(
            [
                "autostart",
                "install",
                "--config",
                str(config_path),
                "--data-dir",
                str(data_dir),
                "--socket-path",
                str(tmp_path / "herdr.sock"),
                "--herdr-bin",
                "/opt/herdr/bin/herdr",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    plugin_path = data_dir / "autostart" / output["scope_hash"] / "herdr-plugin.toml"
    manifest = tomllib.loads(plugin_path.read_text(encoding="utf-8"))
    assert output["plugin_id"] == manifest["id"]
    assert output["plugin_dir"] == str(plugin_path.parent)
    assert calls[-1][:3] == ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}"]
    assert "link" not in repr(manifest)


def test_cli_install_persists_explicit_config_overrides_into_run_arguments(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    data_dir = tmp_path / "data"

    def fake_runner(argv, **_kwargs):
        if argv[1] == "print":
            return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/fixture/herdr" if name == "herdr" else None
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_runner)
    install_args = [
        "--config",
        str(config_path),
        "--data-dir",
        str(data_dir),
        "autostart",
        "install",
        "--socket-path",
        str(tmp_path / "herdr.sock"),
        "--herdr-bin",
        "/fixture/herdr",
        "--hermes-db",
        "relative.db",
        "--poll-interval-seconds",
        "7",
        "--time-zone",
        "Asia/Tokyo",
    ]
    assert cli.main(install_args) == 0
    output = json.loads(capsys.readouterr().out)
    with (
        home
        / "Library/LaunchAgents"
        / f"io.github.iorinu.sessiongauge.{output['scope_hash']}.plist"
    ).open("rb") as stream:
        arguments = plistlib.load(stream)["ProgramArguments"]

    observed = []
    monkeypatch.setattr(
        cli, "_autostart_run", lambda _instance, config: observed.append(config) or 0
    )
    assert cli.main(arguments[3:]) == 0
    config = observed[0]
    assert config.hermes_db_paths == ((Path.cwd() / "relative.db").resolve(),)
    assert config.poll_interval_seconds == 7
    assert config.time_zone == "Asia/Tokyo"


def test_unsupported_os_rejects_before_creating_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    assert (
        cli.main(
            [
                "autostart",
                "install",
                "--data-dir",
                str(tmp_path / "data"),
                "--socket-path",
                str(tmp_path / "sock"),
                "--herdr-bin",
                "/usr/bin/herdr",
            ]
        )
        == 1
    )
    assert not (tmp_path / "data").exists()
    assert "macOS" in capsys.readouterr().err


def test_managed_run_defers_when_server_is_unready(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (
            calls.append(a)
            or SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"server": {"running": False, "socket": str(tmp_path / "sock")}}),
            )
        ),
    )
    monkeypatch.setattr(
        cli, "_live", lambda *a, **k: (_ for _ in ()).throw(AssertionError("collector started"))
    )
    instance = SimpleNamespace(
        socket_path=tmp_path / "sock", herdr_bin="/usr/bin/herdr", config_path=tmp_path / "config"
    )
    assert cli._autostart_run(instance) == 0
    assert calls and calls[0][0] == ["/usr/bin/herdr", "status", "--json"]


def test_managed_run_rejects_wrong_endpoint_and_invalid_status(tmp_path, monkeypatch):
    import pytest

    from sessiongauge.autostart import AutostartError

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    instance = SimpleNamespace(
        socket_path=tmp_path / "requested.sock",
        herdr_bin="/usr/bin/herdr",
        config_path=tmp_path / "config",
    )
    for payload in (
        {"server": {"running": True, "socket": str(tmp_path / "other.sock")}},
        {"server": {"running": "yes", "socket": str(instance.socket_path)}},
    ):
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, _payload=payload, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps(_payload)
            ),
        )
        with pytest.raises(AutostartError):
            cli._autostart_run(instance)


def test_managed_run_ready_endpoint_calls_live_and_restores_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HERDR_ENV", "parent")
    original_socket = os.environ.get("HERDR_SOCKET_PATH")
    endpoint = tmp_path / "sock"
    payload = {"server": {"running": True, "socket": str(endpoint), "compatible": True}}
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    observed = []

    def live(config, as_json, once):
        observed.append((as_json, once, os.environ["HERDR_SOCKET_PATH"], os.environ["HERDR_ENV"]))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_live", live)
    instance = SimpleNamespace(
        socket_path=endpoint, herdr_bin="/usr/bin/herdr", config_path=tmp_path / "config"
    )
    assert cli._autostart_run(instance) == 0
    assert observed == [(True, False, str(endpoint), "1")]
    assert os.environ["HERDR_ENV"] == "parent"
    assert os.environ.get("HERDR_SOCKET_PATH") == original_socket


def test_managed_run_signal_handler_stops_cleanly_and_is_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    payload = {"server": {"running": True, "socket": str(tmp_path / "sock")}}
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    installed = {}
    real_signal = signal.signal

    def capture(sig, handler):
        if handler is not None:
            installed[sig] = handler
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", capture)
    calls = []

    def live(*_args, **_kwargs):
        calls.append(True)
        installed[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(cli, "_live", live)
    instance = SimpleNamespace(
        socket_path=tmp_path / "sock", herdr_bin="/usr/bin/herdr", config_path=tmp_path / "config"
    )
    assert cli._autostart_run(instance) == 0
    assert calls == [True]
    assert signal.getsignal(signal.SIGINT) == handlers[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == handlers[signal.SIGTERM]


def test_plugin_context_for_other_endpoint_is_not_applicable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "other.sock"))
    assert (
        cli.main(
            [
                "autostart",
                "status",
                "--data-dir",
                str(tmp_path / "data"),
                "--socket-path",
                str(tmp_path / "requested.sock"),
                "--herdr-bin",
                "/usr/bin/herdr",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"not_applicable": True}
    assert not (tmp_path / "data").exists()


def test_control_actions_dispatch_to_installation(tmp_path, monkeypatch, capsys):
    from sessiongauge import autostart

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    called = []

    class FakeInstallation:
        def __init__(self, **_kwargs):
            self.label = "label"
            self.plist_path = tmp_path / "job.plist"
            self.plugin_path = tmp_path / "plugin.toml"
            self.plugin_id = "plugin"
            self.scope_hash = "scope"

        def start(self):
            called.append("start")

        def stop(self):
            called.append("stop")

        def status(self):
            called.append("status")
            return {"loaded": True}

        def uninstall(self):
            called.append("uninstall")

    monkeypatch.setattr(autostart, "Installation", FakeInstallation)
    for action in ("start", "stop", "status", "uninstall"):
        assert (
            cli.main(
                [
                    "autostart",
                    action,
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--socket-path",
                    str(tmp_path / "sock"),
                    "--herdr-bin",
                    "/usr/bin/herdr",
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert called == ["start", "stop", "status", "uninstall"]
