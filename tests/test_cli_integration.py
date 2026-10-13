import json

from sessiongauge.cli import _parser, main
from sessiongauge.config import load_config


def test_common_options_are_available_before_and_after_subcommand():
    for args in (["--debug", "history"], ["history", "--debug"]):
        assert _parser().parse_args(args).debug is True


def test_time_zone_environment_override(tmp_path):
    config = load_config(
        config_path=tmp_path / "none.toml", environ={"SESSIONGAUGE_TIME_ZONE": "UTC"}
    )
    assert config.time_zone == "UTC"


def test_export_rejects_monitor_sidecar_before_creating_monitor(tmp_path):
    source = tmp_path / "hermes.db"
    source.touch()
    target_dir = tmp_path / "data"
    target_dir.mkdir()
    sidecar = target_dir / "monitor.db-wal"
    assert (
        main(
            [
                "--hermes-db",
                str(source),
                "--data-dir",
                str(target_dir),
                "export",
                "--output",
                str(sidecar),
            ]
        )
        == 1
    )
    assert not (target_dir / "monitor.db").exists()


def test_history_profile_and_task_filters_use_named_metadata(tmp_path, capsys):
    from sessiongauge.persistence import MonitorDB

    now = __import__("time").time()
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute(
            "INSERT INTO tasks(task_id,display_name,project_path,created_at) VALUES(?,?,?,?)",
            ("task-1", "Task One", "/work", now),
        )
        db.connection.execute(
            "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?)",
            (profile, "session-1", now - 60, now - 30, "task-1", 50, 15),
        )
        db.connection.commit()
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path),
                "history",
                "--days",
                "1",
                "--profile",
                "work",
                "--task",
                "task-1",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["groups"]
    row = report["groups"][0]["records"][0]
    assert row["profile"] == "work" and row["task_title"] == "Task One"


def test_export_cli_keeps_interval_delta_separate_from_baseline(tmp_path, monkeypatch):
    import csv

    from sessiongauge import history as history_module

    monkeypatch.setattr(
        history_module,
        "history",
        lambda *args, **kwargs: {
            "groups": [
                {
                    "records": [
                        {
                            "task_id": "task-1",
                            "task_title": "Task",
                            "pane_ids": ["pane-1"],
                            "profile": "work",
                            "profile_id": "profile-1",
                            "session_id": "s1",
                            "period_start": "start",
                            "period_end": "end",
                            "input_tokens": 50,
                            "output_tokens": 15,
                            "total_tokens": 65,
                            "data_source": "test",
                        }
                    ]
                }
            ],
            "baselines": [
                {
                    "title": "Task",
                    "profile": "work",
                    "profile_id": "profile-1",
                    "session_id": "s1",
                    "collected_at": 123,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "data_source": "baseline",
                }
            ],
        },
    )
    output = tmp_path / "usage.csv"
    source = tmp_path / "hermes.db"
    source.touch()
    assert (
        main(
            [
                "--hermes-db",
                str(source),
                "--data-dir",
                str(tmp_path),
                "export",
                "--format",
                "csv",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    interval = next(row for row in rows if row["data_source"] == "test")
    baseline = next(row for row in rows if row["data_source"] == "baseline")
    assert (interval["input_tokens"], interval["output_tokens"], interval["total_tokens"]) == (
        "50",
        "15",
        "65",
    )
    assert (baseline["input_tokens"], baseline["output_tokens"]) == ("100", "20")
    assert baseline["interval_start"] == "" and baseline["baseline_timestamp"]


def test_select_profiles_distinguishes_default_and_named_state_databases(tmp_path):
    from sessiongauge.cli import _select_profiles

    default = tmp_path / "state.db"
    named = tmp_path / "profiles" / "work" / "state.db"
    assert _select_profiles((default, named), "default") == (default,)
    assert _select_profiles((default, named), "work") == (named,)
    assert _select_profiles(
        (default, named),
        str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, str(named.resolve()))),
    ) == (named,)
