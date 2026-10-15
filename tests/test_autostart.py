import os
import plistlib
import tomllib

import pytest


def make_installation(tmp_path, runner=None):
    from sessiongauge.autostart import Installation

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = tmp_path / "config with spaces.toml"
    config.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Installation(
        data_dir=data,
        socket_path=tmp_path / "herdr.sock",
        config_path=config,
        python_executable="/usr/bin/python3",
        herdr_bin="/opt/herdr/bin/herdr",
        home=home,
        uid=501,
        runner=runner or (lambda *_args, **_kwargs: result(113, stderr="Could not find service")),
    )


def result(code, stdout="", stderr=""):
    return type("Result", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()


def test_install_writes_launch_agent_and_plugin_bundle(tmp_path):
    from sessiongauge.autostart import Installation

    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config with spaces.toml"
    config.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        missing = args[1] == "print"
        return type(
            "Result",
            (),
            {
                "returncode": 113 if missing else 0,
                "stdout": "",
                "stderr": "Could not find service" if missing else "",
            },
        )()

    installation = Installation(
        data_dir=data,
        socket_path=tmp_path / "herdr.sock",
        config_path=config,
        python_executable="/usr/bin/python3",
        herdr_bin="/opt/herdr/bin/herdr",
        home=home,
        uid=501,
        runner=runner,
    )
    result = installation.install()

    with result.plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    manifest = tomllib.loads(result.plugin_path.read_text(encoding="utf-8"))
    assert plist["ProgramArguments"] == [
        "/usr/bin/python3",
        "-m",
        "sessiongauge",
        "--config",
        str(config),
        "--data-dir",
        str(data),
        "autostart",
        "run",
        "--socket-path",
        str(tmp_path / "herdr.sock"),
        "--herdr-bin",
        "/opt/herdr/bin/herdr",
    ]
    assert manifest["id"] == result.plugin_id
    assert [action["id"] for action in manifest["actions"]] == ["start", "stop", "status"]
    assert all(
        isinstance(action["id"], str)
        and isinstance(action["title"], str)
        and isinstance(action["command"], list)
        and all(isinstance(argument, str) for argument in action["command"])
        for action in manifest["actions"]
    )
    assert calls[-1][0][:3] == ["/bin/launchctl", "bootstrap", "gui/501"]


def test_install_rejects_other_existing_file_without_overwrite(tmp_path):
    from sessiongauge.autostart import AutostartError

    installation = make_installation(tmp_path)
    installation.plist_path.parent.mkdir(parents=True)
    installation.plist_path.write_bytes(b"unrelated")
    with pytest.raises(AutostartError, match="overwrite"):
        installation.install()
    assert installation.plist_path.read_bytes() == b"unrelated"


def test_install_is_idempotent_for_identical_generated_files(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return result(113, stderr="Could not find service") if args[1] == "print" else result(0)

    installation = make_installation(tmp_path, runner)
    installation.install()
    plist_before = installation.plist_path.read_bytes()
    plugin_before = installation.plugin_path.read_bytes()
    installation.install()
    assert installation.plist_path.read_bytes() == plist_before
    assert installation.plugin_path.read_bytes() == plugin_before
    assert sum(call[0][1] == "bootstrap" for call in calls) == 2


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_generated_paths_reject_non_regular_existing_targets(tmp_path, kind):
    from sessiongauge.autostart import AutostartError

    installation = make_installation(tmp_path)
    installation.plugin_path.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text("preserve", encoding="utf-8")
    if kind == "symlink":
        installation.plugin_path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, installation.plugin_path)
    else:
        installation.plugin_path.mkdir()
    with pytest.raises(AutostartError):
        installation.install()
    assert target.read_text(encoding="utf-8") == "preserve"


def test_launchctl_failure_is_not_treated_as_missing(tmp_path):
    from sessiongauge.autostart import AutostartError

    installation = make_installation(tmp_path, lambda *_args, **_kwargs: result(1, stderr="secret"))
    with pytest.raises(AutostartError, match="status failed") as error:
        installation.install()
    assert "secret" not in str(error.value)


def test_status_only_returns_allowlisted_fields(tmp_path):
    installation = make_installation(tmp_path)
    installation = type(installation)(
        **{
            **installation.__dict__,
            "runner": lambda *_args, **_kwargs: result(
                0,
                f"{installation.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {installation.scope_hash}\nstate = running\npid = 42\nlast exit code = 0\napi_key = hidden\n",
            ),
        }
    )
    assert installation.status() == {
        "loaded": True,
        "state": "running",
        "pid": "42",
        "last_exit_code": "0",
    }


def test_start_bootstraps_missing_owned_job_then_kickstarts_without_k(tmp_path):
    from sessiongauge.autostart import Installation

    calls = []
    loaded = False
    instance = make_installation(tmp_path, lambda *_args, **_kwargs: result(0))
    instance._ensure_files()

    def runner(args, **kwargs):
        nonlocal loaded
        calls.append(args)
        if args[1] == "print":
            return (
                result(
                    0, f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\n"
                )
                if loaded
                else result(113, stderr="Could not find service")
            )
        if args[1] == "bootstrap":
            loaded = True
        return result(0)

    instance = Installation(**{**instance.__dict__, "runner": runner})
    instance.start()
    assert [call[1] for call in calls] == ["print", "bootstrap", "print", "kickstart"]
    assert "-k" not in calls[-1]


def test_start_kickstarts_loaded_owned_inactive_job_and_preserves_active_pid(tmp_path):
    instance = make_installation(tmp_path)
    instance._ensure_files()
    calls = []
    active = True

    def runner(args, **_kwargs):
        calls.append(args[1])
        if args[1] == "print":
            state = "running\npid = 4242" if active else "not running\npid = 4242"
            return result(
                0,
                f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\nstate = {state}\n",
            )
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner})
    instance.start()
    assert calls == ["print"]

    active = False
    calls.clear()
    instance.start()
    assert calls == ["print", "kickstart"]


def test_start_bootstrap_race_kickstarts_only_after_owned_confirmation(tmp_path):
    from sessiongauge.autostart import AutostartError

    instance = make_installation(tmp_path)
    instance._ensure_files()
    calls = []
    prints = 0

    def runner(args, **_kwargs):
        nonlocal prints
        calls.append(args[1])
        if args[1] == "print":
            prints += 1
            if prints == 1:
                return result(113, stderr="Could not find service")
            return result(0, f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => wrong\n")
        if args[1] == "bootstrap":
            return result(1)
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner})
    with pytest.raises(AutostartError, match="bootstrap failed"):
        instance.start()
    assert calls == ["print", "bootstrap", "print"]


def test_start_bootstrap_race_does_not_kickstart_confirmed_active_job(tmp_path):
    instance = make_installation(tmp_path)
    instance._ensure_files()
    calls = []

    def runner(args, **_kwargs):
        calls.append(args[1])
        if args[1] == "print" and calls.count("print") == 1:
            return result(113, stderr="Could not find service")
        if args[1] == "print":
            return result(
                0,
                f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\n"
                "state = running\npid = 4242\n",
            )
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner})
    instance.start()
    assert calls == ["print", "bootstrap", "print"]


def test_start_refuses_foreign_loaded_service(tmp_path):
    from sessiongauge.autostart import AutostartError

    instance = make_installation(tmp_path, lambda *_args, **_kwargs: result(0, "state = running\n"))
    instance._ensure_files()
    with pytest.raises(AutostartError, match="ownership"):
        instance.start()


def test_stop_signals_only_owned_service_target(tmp_path):
    calls = []
    instance = make_installation(tmp_path)
    instance._ensure_files()

    def runner(args, **kwargs):
        calls.append(args)
        if args[1] == "print":
            return (
                result(113, stderr="Could not find service")
                if len([call for call in calls if call[1] == "print"]) > 1
                else result(
                    0,
                    f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\npid = 42\nstate = running\n",
                )
            )
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner, "stop_timeout": 0})
    instance.stop()
    assert [call[1] for call in calls] == ["print", "kill", "print"]
    assert calls[1] == ["/bin/launchctl", "kill", "SIGINT", instance.service_target]


def test_uninstall_removes_only_owned_plist_after_bootout(tmp_path):
    calls = []
    instance = make_installation(tmp_path)
    instance._ensure_files()

    def runner(args, **kwargs):
        calls.append(args)
        if args[1] == "print":
            return result(
                0, f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\n"
            )
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner})
    plugin_bytes = instance.plugin_path.read_bytes()
    instance.uninstall()
    assert calls[-1] == ["/bin/launchctl", "bootout", instance.service_target]
    assert not instance.plist_path.exists()
    assert instance.plugin_path.read_bytes() == plugin_bytes


def test_scope_and_generated_environment_keep_only_nonsecret_config(tmp_path, monkeypatch):
    instance = make_installation(tmp_path)
    monkeypatch.setenv("SECRET_API_KEY", "must-not-be-written")
    monkeypatch.setenv("HERMES_HOME", "/fixture/hermes")
    monkeypatch.setenv("SESSIONGAUGE_TIME_ZONE", "Asia/Tokyo")
    monkeypatch.setenv("SESSIONGAUGE_SIDEBAR_ENABLED", "true")
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", "context-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "token-secret")
    plist = plistlib.loads(instance._plist_bytes())
    environment = plist["EnvironmentVariables"]
    assert instance.label == f"io.github.iorinu.sessiongauge.{instance.scope_hash}"
    assert instance.plugin_id == f"iorinu.sessiongauge.{instance.scope_hash}"
    assert {
        key: environment[key]
        for key in (
            "HERDR_ENV",
            "HERDR_SOCKET_PATH",
            "HERDR_BIN_PATH",
            "PATH",
            "SESSIONGAUGE_AUTOSTART_SCOPE",
            "HOME",
            "HERMES_HOME",
            "SESSIONGAUGE_TIME_ZONE",
            "SESSIONGAUGE_SIDEBAR_ENABLED",
        )
    } == {
        "HERDR_ENV": "1",
        "HERDR_SOCKET_PATH": str(instance.socket_path),
        "HERDR_BIN_PATH": instance.herdr_bin,
        "PATH": "/opt/herdr/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "SESSIONGAUGE_AUTOSTART_SCOPE": instance.scope_hash,
        "HOME": str(instance.home),
        "HERMES_HOME": "/fixture/hermes",
        "SESSIONGAUGE_TIME_ZONE": "Asia/Tokyo",
        "SESSIONGAUGE_SIDEBAR_ENABLED": "true",
    }
    assert set(environment) <= {
        "HERDR_ENV",
        "HERDR_SOCKET_PATH",
        "HERDR_BIN_PATH",
        "PATH",
        "SESSIONGAUGE_AUTOSTART_SCOPE",
        "HOME",
        "HERMES_HOME",
        "SESSIONGAUGE_CONFIG",
        "SESSIONGAUGE_HERMES_DB_PATHS",
        "SESSIONGAUGE_DATA_DIR",
        "SESSIONGAUGE_POLL_INTERVAL_SECONDS",
        "SESSIONGAUGE_TIME_ZONE",
        "SESSIONGAUGE_SIDEBAR_ENABLED",
        "SESSIONGAUGE_SIDEBAR_TTL_MS",
        "SESSIONGAUGE_SIDEBAR_DISPLAY",
        "SESSIONGAUGE_SIDEBAR_TIME_ZONE",
    }
    for secret in ("must-not-be-written", "context-secret", "token-secret"):
        assert secret not in repr(plist)


def test_invalid_paths_are_rejected_before_writing(tmp_path):
    from sessiongauge.autostart import Installation

    for value in ("relative/socket", "/tmp/with\0nul"):
        with pytest.raises(ValueError):
            Installation(
                data_dir=tmp_path,
                socket_path=value,
                config_path=tmp_path / "config",
                python_executable="/usr/bin/python3",
                herdr_bin="/usr/bin/herdr",
                home=tmp_path / "home",
                uid=501,
                runner=lambda *_args, **_kwargs: result(113, stderr="Could not find service"),
            )


def test_wrong_scope_marker_is_foreign_and_status_is_not_running(tmp_path):
    from sessiongauge.autostart import AutostartError

    instance = make_installation(tmp_path, lambda *_args, **_kwargs: result(0, "state = running\n"))
    instance._ensure_files()
    instance = type(instance)(
        **{
            **instance.__dict__,
            "runner": lambda *_args, **_kwargs: result(
                0,
                f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => wrong\nstate = running\npid = 4\n",
            ),
        }
    )
    assert instance.status()["loaded"] is False
    with pytest.raises(AutostartError, match="ownership"):
        instance.start()
    with pytest.raises(AutostartError, match="ownership"):
        instance.install()


@pytest.mark.parametrize(
    "payload", [b"not plist", plistlib.dumps({"Label": "x", "EnvironmentVariables": []})]
)
def test_malformed_owned_plist_fails_closed_before_native_calls(tmp_path, payload):
    from sessiongauge.autostart import AutostartError

    calls = []
    instance = make_installation(tmp_path, lambda *args, **kwargs: calls.append(args))
    instance.plist_path.parent.mkdir(parents=True)
    instance.plist_path.write_bytes(payload)
    with pytest.raises(AutostartError):
        instance.start()
    assert calls == []


def test_arbitrary_bad_request_error_is_not_missing(tmp_path):
    from sessiongauge.autostart import AutostartError

    instance = make_installation(
        tmp_path, lambda *_args, **_kwargs: result(1, stderr="Bad request: permission denied")
    )
    with pytest.raises(AutostartError):
        instance.install()


def test_native_missing_service_contract(tmp_path):
    instance = make_installation(
        tmp_path, lambda *_args, **_kwargs: result(113, stderr="Could not find service")
    )
    assert instance._is_missing(result(113, stderr="Could not find service"))
    assert not instance._is_missing(result(113, stderr="permission denied"))


def test_status_parser_keeps_multiword_state_and_hides_raw_environment(tmp_path):
    instance = make_installation(tmp_path)
    instance = type(instance)(
        **{
            **instance.__dict__,
            "runner": lambda *_args, **_kwargs: result(
                0,
                f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\nstate = not running\n",
            ),
        }
    )
    assert instance.status()["state"] == "not running"
    assert "secret" not in repr(instance.status())


def test_stop_times_out_if_owned_pid_remains_active(tmp_path):
    from sessiongauge.autostart import AutostartError

    instance = make_installation(tmp_path)
    instance._ensure_files()
    calls = []

    def runner(args, **_kwargs):
        calls.append(args[1])
        if args[1] == "print":
            return result(
                0,
                f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\npid = 42\nstate = running\n",
            )
        return result(0)

    instance = type(instance)(**{**instance.__dict__, "runner": runner, "stop_timeout": 0})
    with pytest.raises(AutostartError, match="did not stop"):
        instance.stop()
    assert calls == ["print", "kill", "print"]


def test_stop_does_not_kill_owned_inactive_job(tmp_path):
    instance = make_installation(tmp_path)
    instance._ensure_files()
    calls = []

    def runner(args, **_kwargs):
        calls.append(args[1])
        return result(
            0,
            f"{instance.label}\nSESSIONGAUGE_AUTOSTART_SCOPE => {instance.scope_hash}\nstate = not running\n",
        )

    instance = type(instance)(**{**instance.__dict__, "runner": runner, "stop_timeout": 0})
    instance.stop()
    assert calls == ["print"]


def test_owned_file_write_rejects_zero_byte_write(tmp_path, monkeypatch):
    from sessiongauge.autostart import Installation

    destination = tmp_path / "new-owned-file"
    monkeypatch.setattr("sessiongauge.autostart.os.write", lambda _fd, _view: 0)
    with pytest.raises(OSError, match="failed to write"):
        Installation._write_owned(destination, b"content")
