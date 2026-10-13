from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest

from sessiongauge.adapters.hermes_db import HermesDB
from sessiongauge.collector import Collector
from sessiongauge.config import Config
from sessiongauge.domain import Counters, Pane, Session
from sessiongauge.persistence import MonitorDB


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("version", [0, 1])
def test_migration_failure_rolls_back_and_can_retry(tmp_path, monkeypatch, version):
    from sessiongauge import persistence

    path = tmp_path / "monitor.db"
    if version == 1:
        with MonitorDB(path):
            pass
    if version == 1:
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE snapshots DROP COLUMN owner_task_id")
            conn.execute("ALTER TABLE snapshots DROP COLUMN source_ok")
            conn.execute("ALTER TABLE snapshots DROP COLUMN run_id")
            conn.execute("PRAGMA user_version=1")
    original = persistence._migration_step
    calls = 0

    def fail_once(connection, statement):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected migration failure")
        return original(connection, statement)

    monkeypatch.setattr(persistence, "_migration_step", fail_once)
    with pytest.raises(sqlite3.OperationalError), MonitorDB(path):
        pass
    monkeypatch.setattr(persistence, "_migration_step", original)
    with MonitorDB(path) as db:
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_monitor_sidecar_hardlink_rejected_without_source_change(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "monitor.db-wal"
    source.write_bytes(b"protected source bytes")
    os.link(source, sidecar)
    before = _hash(source)
    with pytest.raises(ValueError), MonitorDB(tmp_path / "monitor.db", source_paths=(source,)):
        pass
    assert _hash(source) == before
    assert sidecar.read_bytes() == source.read_bytes()


def test_existing_group_writable_parent_is_refused_without_chmod(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o770)
    with pytest.raises(ValueError), MonitorDB(parent / "monitor.db"):
        pass
    assert parent.stat().st_mode & 0o777 == 0o770


class _Adapter:
    def __init__(self, panes, sessions=()):
        self.panes = panes
        self.rows = list(sessions)
        self.fail = False

    def list_panes(self):
        if self.fail:
            raise OSError("offline")
        return self.panes

    def sessions(self, *_):
        return self.rows


def _pane(title="task"):
    return Pane("local", "pane-1", "term-1", "hermes", "working", "/work", title, "s1")


def _session(sid="s1", tokens=10, git_repo_root="/repo"):
    return Session(
        sid,
        "cli",
        "model",
        None,
        Counters(tokens, 2),
        0.2,
        None,
        1,
        None,
        title="session",
        git_repo_root=git_repo_root,
    )


def test_failed_herdr_does_not_refresh_pane_or_task_metadata(tmp_path):
    source = tmp_path / "hermes.db"
    source.touch()
    config = Config(3, (source,), tmp_path / "data", "local")
    adapter = _Adapter([_pane()], [_session()])
    with MonitorDB(config.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(config, monitor, adapter)
        collector.collect(now=100)
        adapter.panes = [_pane("renamed")]
        adapter.fail = True
        collector.collect(now=110)
        row = monitor.connection.execute("SELECT title,last_seen FROM panes").fetchone()
        task = monitor.connection.execute("SELECT display_name FROM tasks").fetchone()
        assert tuple(row) == ("task", 100)
        assert task[0] == "task"


def test_panes_are_persisted_without_any_hermes_sessions(tmp_path):
    source = tmp_path / "hermes.db"
    source.touch()
    config = Config(3, (source,), tmp_path / "data", "local")
    adapter = _Adapter([_pane()], [])
    with MonitorDB(config.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        Collector(config, monitor, adapter).collect(now=100)
        assert monitor.connection.execute("SELECT title,last_seen FROM panes").fetchone() == (
            "task",
            100,
        )


def test_repeated_and_decreasing_observations_do_not_add_intervals_or_rewind_catalog(tmp_path):
    path = tmp_path / "monitor.db"
    with MonitorDB(path) as db:
        profile = db.register_profile("test", tmp_path / "hermes.db")
        db.record_poll(
            profile_id=profile,
            sessions=[_session(tokens=10)],
            panes=[],
            matches={},
            collected_at=100,
        )
        db.record_poll(
            profile_id=profile,
            sessions=[_session(tokens=20)],
            panes=[],
            matches={},
            collected_at=100,
        )
        db.record_poll(
            profile_id=profile,
            sessions=[_session(tokens=5, git_repo_root="/old")],
            panes=[],
            matches={},
            collected_at=90,
        )
        assert db.connection.execute("SELECT count(*) FROM usage_attributions").fetchone()[0] == 0
        catalog = db.connection.execute("SELECT git_repo_root FROM session_catalog").fetchone()[0]
        assert catalog == "/repo"
        assert db.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_snapshot_api_rejects_repeated_or_decreasing_time(tmp_path):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("test", tmp_path / "hermes.db")
        db.record_snapshot(profile, "s", Counters(10, 2), collected_at=100)
        db.record_snapshot(profile, "s", Counters(20, 2), collected_at=100)
        db.record_snapshot(profile, "s", Counters(5, 2), collected_at=90)
        assert db.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_pane_title_event_includes_old_and_new_json_values(tmp_path):
    with MonitorDB(tmp_path / "monitor.db") as db:
        db.record_poll(
            profile_id="p", sessions=[], panes=[_pane("old")], matches={}, collected_at=1
        )
        db.record_poll(
            profile_id="p", sessions=[], panes=[_pane("new")], matches={}, collected_at=2
        )
        kind, detail = db.connection.execute("SELECT kind,detail FROM events").fetchone()
        assert kind == "pane_title_changed"
        assert json.loads(detail) == {"pane_id": "pane-1", "old_title": "old", "new_title": "new"}


def test_source_values_are_sanitized_and_source_file_remains_unchanged(tmp_path):
    path = tmp_path / "hermes.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE sessions(id, input_tokens, output_tokens, estimated_cost_usd, started_at)"
        )
        conn.execute("INSERT INTO sessions VALUES('s', '1.5', 'NaN', 'inf', 'not-a-time')")
    before = _hash(path)
    with HermesDB(path) as db:
        row = db.sessions()[0]
    assert row.counters.input_tokens is None
    assert row.counters.output_tokens is None
    assert row.estimated_cost_usd is None
    assert row.started_at is None
    assert _hash(path) == before
