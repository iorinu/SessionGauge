from datetime import datetime
from zoneinfo import ZoneInfo

from sessiongauge.config import Config
from sessiongauge.domain import Counters, Session
from sessiongauge.history import history
from sessiongauge.persistence import MonitorDB
from sessiongauge.tui import TUIState, render_frame, visible_rows


def test_session_groups_have_distinct_labels_and_partial_aggregate_notes(tmp_path, monkeypatch):
    import sessiongauge.collector

    class NoopCollector:
        def __init__(self, config):
            pass

        def collect(self, now=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sessiongauge.collector, "Collector", NoopCollector)
    now = datetime(2025, 1, 4, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    with MonitorDB(tmp_path / "data/monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        for sid in ("s1", "s2"):
            db.connection.execute(
                "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,input_tokens,output_tokens) VALUES(?,?,?,?,?,?)",
                (profile, sid, now - 60, now - 30, 2, None),
            )
        db.connection.commit()
    report = history(
        Config(data_dir=tmp_path / "data", time_zone="UTC"), now=now, group_by="session"
    )
    assert len({group["group"] for group in report["groups"]}) == 2
    assert all(group["total_tokens"] is None for group in report["groups"])
    assert any("counter" in note.lower() for note in report["notes"])


def test_moving_session_baseline_is_emitted_once(tmp_path, monkeypatch):
    import sessiongauge.collector

    class NoopCollector:
        def __init__(self, config):
            pass

        def collect(self, now=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sessiongauge.collector, "Collector", NoopCollector)
    with MonitorDB(tmp_path / "data/monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute(
            "INSERT INTO session_catalog(profile_id,session_id,title) VALUES(?,?,?)",
            (profile, "s", "session"),
        )
        db.connection.execute(
            "INSERT INTO snapshots(profile_id,session_id,collected_at,input_tokens,output_tokens) VALUES(?,?,?,?,?)",
            (profile, "s", 100, 10, 2),
        )
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('t1',1)")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('t2',1)")
        for pane, task, start, end in (("p1", "t1", 1, 50), ("p2", "t2", 50, None)):
            db.connection.execute(
                "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,valid_to,source,confidence) VALUES('e',?,?,?,?,?,?,?,'exact')",
                (pane, task, profile, "s", start, end, "manual"),
            )
        db.connection.commit()
    report = history(Config(data_dir=tmp_path / "data", time_zone="UTC"), now=200)
    assert len(report["baselines"]) == 1
    assert len(report["baselines"][0]["pane_ids"]) == 1


def test_idle_matched_pane_is_active_and_render_uses_configured_zone():
    data = {
        "collected_at": 0,
        "panes": [{"pane_id": "p", "status": "matched", "state": "idle", "session": {"id": "s"}}],
    }
    assert len(visible_rows(data, TUIState())) == 1
    frame = render_frame(data, TUIState(), config=Config(time_zone="Asia/Tokyo"))
    assert "09:00:00 JST" in frame[0]


def test_unchanged_ended_sessions_are_not_snapshotted_repeatedly(tmp_path):
    session = Session("ended", "cli", "model", None, Counters(10, 2), 0.1, None, 1, 2, cwd="/work")
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        for at in (10, 20, 30):
            db.record_poll(
                profile_id=profile, sessions=[session], panes=[], matches={}, collected_at=at
            )
        assert (
            db.connection.execute(
                "SELECT count(*) FROM snapshots WHERE session_id='ended'"
            ).fetchone()[0]
            == 1
        )
        active = Session("active", "cli", "model", None, Counters(10, 2), 0.1, None, 1, None)
        db.record_poll(profile_id=profile, sessions=[active], panes=[], matches={}, collected_at=40)
        db.record_poll(profile_id=profile, sessions=[active], panes=[], matches={}, collected_at=50)
        assert (
            db.connection.execute(
                "SELECT count(*) FROM snapshots WHERE session_id='active'"
            ).fetchone()[0]
            == 2
        )


def test_config_deduplicates_existing_database_hardlinks(tmp_path):
    from sessiongauge.config import load_config

    first, second = tmp_path / "first.db", tmp_path / "second.db"
    first.touch()
    second.hardlink_to(first)
    config = load_config(environ={"SESSIONGAUGE_HERMES_DB_PATHS": f"{first}:{second}"})
    assert len(config.hermes_db_paths) == 1


def test_show_includes_task_session_lineage_and_name_history(tmp_path, capsys):
    import json

    from sessiongauge.cli import _show

    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('task',1)")
        db.connection.execute(
            "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,model,title) VALUES(?,?,?,?,?)",
            (profile, "parent", None, "m", "parent"),
        )
        db.connection.execute(
            "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,model,title) VALUES(?,?,?,?,?)",
            (profile, "child", "parent", "m", "child"),
        )
        db.connection.execute(
            "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,source,confidence) VALUES('e','pane','task',?,'child',1,'manual','confirmed')",
            (profile,),
        )
        db.connection.execute(
            "INSERT INTO events(at,kind,detail) VALUES(1,'pane_title_changed',?)",
            ('{"pane_id":"pane","old_title":"old","new_title":"new"}',),
        )
        db.connection.commit()
    _show(Config(data_dir=tmp_path), "task")
    output = json.loads(capsys.readouterr().out)
    assert {row["session_id"] for row in output["lineage"]} == {"parent", "child"}
    assert output["name_changes"][0]["old_title"] == "old"
