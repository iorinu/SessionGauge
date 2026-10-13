import json

from sessiongauge.cli import _doctor


def test_doctor_reports_product_and_does_not_dump_identifiers(tmp_path, capsys):
    from sessiongauge.config import Config

    assert _doctor(Config(data_dir=tmp_path, hermes_db_paths=())) == 0
    output = capsys.readouterr().out
    assert '"product": "SessionGauge"' in output
    assert "conversation" not in output


def test_doctor_reports_read_only_integration_status_and_versions(tmp_path, capsys, monkeypatch):
    import subprocess

    from sessiongauge import cli
    from sessiongauge.config import Config

    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[-2:] == ["integration", "status"]:
            return subprocess.CompletedProcess(
                command, 0, "hermes: current(v5)\nother: /private/title\n", ""
            )
        if command[-2:] == ["status", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "client": {"version": "0.9.2", "protocol": 22},
                        "server": {
                            "version": "0.9.0",
                            "protocol": 22,
                            "compatible": True,
                            "endpoint_compatible": True,
                        },
                        "update": {"restart_needed": False},
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "herdr 2.3", "")

    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", runner)
    assert _doctor(Config(data_dir=tmp_path, hermes_db_paths=())) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["herdr"]["integration"] == {"hermes": "current(v5)"}
    assert report["herdr"]["client_version"] == "0.9.2"
    assert report["herdr"]["client_protocol"] == 22
    assert report["herdr"]["server_version"] == "0.9.0"
    assert report["herdr"]["server_protocol"] == 22
    assert report["herdr"]["compatible"] is True
    assert report["herdr"]["endpoint_compatible"] is True
    assert report["herdr"]["restart_needed"] is False
    assert "/private/title" not in json.dumps(report)
    assert "herdr integration install hermes" not in " ".join(report["guidance"])
    assert all("install" not in " ".join(command) for command, _ in calls)
    assert all(kwargs.get("timeout") for _, kwargs in calls)
