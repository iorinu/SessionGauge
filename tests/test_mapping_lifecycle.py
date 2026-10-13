from __future__ import annotations

from test_collector_acceptance import Collector, Config, MonitorDB, Sources, pane, session


def setup(tmp_path, panes=None):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    monitor = MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,))
    monitor.__enter__()
    adapter = Sources(panes or [pane()])
    return Collector(cfg, monitor, adapter), monitor, adapter


def test_move_interval_unattributed(tmp_path):
    collector, db, src = setup(tmp_path, [pane("p1")])
    collector.collect(now=100)
    src.rows = [session(input_tokens=150)]
    src.panes = [pane("p2")]
    collector.collect(now=110)
    assert db.connection.execute("SELECT task_id FROM usage_attributions").fetchone()[0] is None
    db.__exit__()


def test_restart_persistent_task_but_gap_unattributed(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    task = db.connection.execute("SELECT task_id FROM tasks").fetchone()[0]
    collector.close()
    next_collector = Collector(collector.config, db, src)
    src.rows = [session(input_tokens=150)]
    next_collector.collect(now=110)
    task2 = db.connection.execute("SELECT task_id FROM tasks").fetchone()[0]
    assert task == task2
    assert db.connection.execute("SELECT task_id FROM usage_attributions").fetchone()[0] is None
    db.__exit__()


def test_repeated_poll_no_duplicate_links(tmp_path):
    collector, db, _ = setup(tmp_path)
    collector.collect(now=100)
    collector.collect(now=110)
    assert db.connection.execute("SELECT count(*) FROM task_pane_links").fetchone()[0] == 1
    db.__exit__()


def test_title_rename_history(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    src.panes = [pane()]
    from dataclasses import replace

    src.panes = [replace(src.panes[0], title="renamed")]
    collector.collect(now=110)
    assert db.connection.execute("SELECT display_name FROM tasks").fetchone()[0] == "renamed"
    db.__exit__()


def test_unrelated_session_new_task(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    old = db.connection.execute("SELECT task_id FROM tasks").fetchone()[0]
    src.rows = [session("other")]
    src.panes = [pane(sid="other")]
    collector.collect(now=110)
    assert db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    assert (
        old
        != db.connection.execute("SELECT task_id FROM tasks ORDER BY created_at DESC").fetchone()[0]
    )
    db.__exit__()


def test_conflict_unattributed(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    src.panes = [pane("p1"), pane("p2")]
    src.rows = [session(input_tokens=150)]
    collector.collect(now=110)
    assert db.connection.execute("SELECT task_id FROM usage_attributions").fetchone()[0] is None
    db.__exit__()


def test_first_herdr_failure_and_empty_success(tmp_path):
    collector, db, src = setup(tmp_path)
    src.fail = True
    assert collector.collect(now=100)["stale"] is False
    src.fail = False
    src.panes = []
    assert collector.collect(now=110)["stale"] is False
    db.__exit__()


def test_manual_link_is_used_for_hermes_pane_without_official_sid(tmp_path):
    collector, db, src = setup(tmp_path, [pane(sid=None)])
    src.panes = [pane(sid=None)]
    profile = db.register_profile("default", collector.config.hermes_db_paths[0])
    linked_task = db.link_manual(src.panes[0], profile, "s", 90)
    result = collector.collect(now=100)
    assert result["panes"][0]["status"] == "matched"
    assert result["panes"][0]["task_id"] == linked_task
    assert (
        db.connection.execute(
            "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
        ).fetchone()[0]
        == linked_task
    )
    db.__exit__()


def test_previous_link_matches_only_hermes_same_generation(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    previous_task = db.connection.execute(
        "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
    ).fetchone()[0]
    from dataclasses import replace

    src.panes = [replace(pane(sid=None), agent="other")]
    blocked = collector.collect(now=110)
    assert blocked["panes"][0]["status"] == "unlinked"
    src.panes = [replace(pane(sid=None), terminal_id="new-terminal")]
    blocked = collector.collect(now=120)
    assert blocked["panes"][0]["status"] == "unlinked"
    src.panes = [pane(sid=None)]
    accepted = collector.collect(now=130)
    assert accepted["panes"][0]["task_id"] == previous_task
    db.__exit__()


def test_compression_continuation_reuses_task_but_keeps_child_baseline(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    task = db.connection.execute(
        "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
    ).fetchone()[0]
    src.rows = [
        session("s", input_tokens=150, end_reason="compression"),
        session("child", input_tokens=500, parent="s"),
    ]
    src.panes = [pane(sid="child")]
    result = collector.collect(now=110)
    child_task = db.connection.execute(
        "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
    ).fetchone()[0]
    assert child_task == task
    assert (
        db.connection.execute(
            "SELECT count(*) FROM usage_attributions WHERE session_id='child'"
        ).fetchone()[0]
        == 0
    )
    assert result["panes"][0]["lineage"] == ["child", "s"]
    db.__exit__()


def test_backoff_skips_calls_until_retry_due(tmp_path):
    collector, db, src = setup(tmp_path)
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise OSError("offline")

    src.list_panes = fail
    collector.collect(now=100)
    collector.collect(now=101)
    assert calls == 1
    collector.collect(now=102)
    assert calls == 2
    db.__exit__()


def test_db_source_backoff_skips_actual_reads(tmp_path):
    collector, db, src = setup(tmp_path)
    calls = 0

    def fail(*_):
        nonlocal calls
        calls += 1
        raise OSError("database unavailable")

    src.sessions = fail
    collector.collect(now=100)
    collector.collect(now=101)
    assert calls == 1
    collector.collect(now=102)
    assert calls == 2
    db.__exit__()


def test_official_unknown_session_never_falls_back(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    src.panes = [pane(sid="not-in-the-db")]
    result = collector.collect(now=110)
    assert result["panes"][0]["status"] == "unlinked"
    assert result["panes"][0]["task_id"] is None
    db.__exit__()


def test_official_duplicate_session_id_is_conflict_not_fallback(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    second_source = tmp_path / "profile" / "state.db"
    second_source.parent.mkdir()
    second_source.touch()
    collector.config = Config(
        3, (collector.config.hermes_db_paths[0], second_source), collector.config.data_dir, "local"
    )
    collector.source_states[str(second_source)] = collector.source_states[
        str(collector.config.hermes_db_paths[0])
    ].__class__()
    collector.source_gap[str(second_source)] = False
    collector.source_first_poll.add(str(second_source))
    src.panes = [pane(sid="s")]
    result = collector.collect(now=110)
    assert result["panes"][0]["status"] == "conflict"
    db.__exit__()


def test_compression_continuation_does_not_join_subagent(tmp_path):
    collector, db, src = setup(tmp_path)
    collector.collect(now=100)
    parent_task = db.connection.execute(
        "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
    ).fetchone()[0]
    src.rows = [session("s", end_reason="compression"), session("child", parent="s")]
    # Subagent session sources are independent even when they identify a parent.
    from dataclasses import replace

    src.rows[1] = replace(src.rows[1], source="subagent")
    src.panes = [pane(sid="child")]
    collector.collect(now=110)
    child_task = db.connection.execute(
        "SELECT task_id FROM pane_session_links WHERE valid_to IS NULL"
    ).fetchone()[0]
    assert child_task != parent_task
    db.__exit__()


def test_active_named_hermes_home_keeps_profile_name_for_collection_and_history(
    tmp_path, monkeypatch, capsys
):
    import json
    import sqlite3
    from dataclasses import replace

    active_home = tmp_path / ".hermes" / "profiles" / "work"
    active_home.mkdir(parents=True)
    source = active_home / "state.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sessions(id TEXT)")
        connection.execute("INSERT INTO sessions VALUES('s')")

    collector, db, _ = setup(tmp_path)
    collector.config = replace(collector.config, hermes_db_paths=(source,))
    collector.source_states = {str(source): collector.source_states.pop(str(tmp_path / "state.db"))}
    collector.source_gap = {str(source): False}
    collector.source_first_poll = {str(source)}
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    collector.collect(now=100)
    profile = db.connection.execute("SELECT name FROM profiles").fetchone()[0]
    assert profile == "work"

    from sessiongauge.cli import main

    assert (
        main(
            [
                "--data-dir",
                str(collector.config.data_dir),
                "--hermes-db",
                str(source),
                "history",
                "--profile",
                "work",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output
    assert all(item["profile"] == "work" for item in json.loads(output)["historical_sessions"])
    assert db.connection.execute("SELECT name FROM profiles").fetchone()[0] == "work"
    db.__exit__()
