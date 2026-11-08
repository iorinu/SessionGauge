from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_collector_acceptance import Collector, Config, MonitorDB, Sources, pane


@pytest.mark.parametrize("method", ["record_panes", "record_poll"])
def test_interleaved_observations_keep_latest_title_and_event(tmp_path, method):
    with MonitorDB(tmp_path / "monitor.db") as first, MonitorDB(tmp_path / "monitor.db") as second:
        profile = first.register_profile("test", tmp_path / "source.db")
        original = replace(pane(), terminal_id=None, title="old")
        connection = first.connection
        injected = False

        class InterleavedConnection:
            def __enter__(self):
                connection.__enter__()
                return self

            def __exit__(self, *args):
                return connection.__exit__(*args)

            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, parameters=()):
                nonlocal injected
                # ロック導入後は取得直前、導入前は読み取り直後に先行観測を確定する。
                if sql == "BEGIN IMMEDIATE" and not injected:
                    injected = True
                    second.record_panes([original], 10)
                cursor = connection.execute(sql, parameters)
                if sql.startswith("SELECT title FROM panes") and not injected:
                    prior = cursor.fetchone()
                    assert not connection.in_transaction
                    injected = True
                    second.record_panes([original], 10)
                    return SimpleNamespace(fetchone=lambda: prior)
                return cursor

        first.connection = InterleavedConnection()
        try:
            latest = replace(original, title="new")
            if method == "record_panes":
                first.record_panes([latest], 20)
            else:
                first.record_poll(
                    profile_id=profile, sessions=[], panes=[latest], matches={}, collected_at=20
                )
        finally:
            first.connection = connection
        assert injected
        assert connection.execute("SELECT title,first_seen,last_seen FROM panes").fetchall() == [
            ("new", 10, 20)
        ]
        at, detail = connection.execute(
            "SELECT at,detail FROM events WHERE kind='pane_title_changed'"
        ).fetchone()
        assert at == 20
        assert json.loads(detail) == {
            "endpoint": original.endpoint,
            "pane_id": original.pane_id,
            "terminal_id": None,
            "old_title": "old",
            "new_title": "new",
        }
        first.record_panes([replace(original, terminal_id="", title="empty")], 30)
        assert connection.execute(
            "SELECT terminal_id,title FROM panes ORDER BY last_seen"
        ).fetchall() == [(None, "new"), ("", "empty")]


def test_null_terminal_insert_is_atomic_between_collectors(tmp_path):
    path = tmp_path / "monitor.db"
    null_pane = replace(pane(), terminal_id=None)
    with MonitorDB(path) as first, MonitorDB(path) as second:
        connection = first.connection

        class InterleavedConnection:
            def execute(self, sql, parameters=()):
                cursor = connection.execute(sql, parameters)
                if sql.startswith("SELECT title FROM panes"):
                    prior = cursor.fetchone()
                    assert prior is None
                    # 最初の読み取り後、もう一方のcollectorが同じ世代を確定する。
                    second.record_panes([null_pane], 10)
                    return SimpleNamespace(fetchone=lambda: prior)
                return cursor

        first.connection = InterleavedConnection()
        try:
            with connection:
                first._record_panes([null_pane], 10)
        finally:
            first.connection = connection
        assert connection.execute(
            "SELECT terminal_id,first_seen,last_seen FROM panes"
        ).fetchall() == [(None, 10, 10)]


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_pane_read_holds_writer_lock(tmp_path, outer_transaction):
    with MonitorDB(tmp_path / "monitor.db") as first, MonitorDB(tmp_path / "monitor.db") as second:
        second.connection.execute("PRAGMA busy_timeout=0")
        connection = first.connection
        if outer_transaction:
            connection.execute("BEGIN")
        checked = []

        def check_lock(sql):
            if sql.startswith("SELECT title FROM panes"):
                assert connection.in_transaction
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    second.record_panes([replace(pane(), title="blocked")], 10)
                checked.append(True)

        connection.set_trace_callback(check_lock)
        try:
            first.record_panes([pane()], 20)
        finally:
            connection.set_trace_callback(None)
        assert checked == [True]
        second.record_panes([replace(pane(), title="next")], 30)
        assert connection.execute("SELECT title,last_seen FROM panes").fetchone() == ("next", 30)


def setup(tmp_path, initial_pane):
    source = tmp_path / "state.db"
    source.touch()
    config = Config(3, (source,), tmp_path / "data", "local")
    monitor = MonitorDB(config.data_dir / "monitor.db", source_paths=(source,))
    monitor.__enter__()
    inputs = Sources([initial_pane])
    return Collector(config, monitor, inputs), monitor, inputs


def test_null_terminal_polls_reuse_pane_and_active_link(tmp_path):
    collector, db, inputs = setup(tmp_path, replace(pane(), terminal_id=None))
    first = collector.collect(now=100)
    task_id = first["panes"][0]["task_id"]
    inputs.panes = [replace(inputs.panes[0], title="renamed")]
    second = collector.collect(now=110)
    third = collector.collect(now=120)

    assert [second["panes"][0]["title"], third["panes"][0]["title"]] == [
        "renamed",
        "renamed",
    ]
    assert all(result["panes"][0]["task_id"] == task_id for result in (second, third))
    assert db.connection.execute("SELECT count(*) FROM panes").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM pane_session_links").fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT task_id,valid_from,valid_to FROM pane_session_links"
    ).fetchone() == (task_id, 100, None)
    assert db.connection.execute("SELECT title,last_seen FROM panes").fetchone() == (
        "renamed",
        120,
    )
    db.__exit__()


def test_null_terminal_manual_link_survives_reopen_and_poll(tmp_path):
    null_pane = replace(pane(sid=None), terminal_id=None)
    collector, db, inputs = setup(tmp_path, null_pane)
    profile = db.register_profile("default", collector.config.hermes_db_paths[0])
    task_id = db.link_manual(null_pane, profile, "s", 90)
    assert db.connection.execute("SELECT count(*) FROM panes").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM pane_session_links").fetchone()[0] == 1

    collector.collect(now=100)
    db.__exit__()

    db = MonitorDB(
        collector.config.data_dir / "monitor.db", source_paths=collector.config.hermes_db_paths
    )
    db.__enter__()
    reopened = Collector(collector.config, db, inputs)
    result = reopened.collect(now=110)

    assert result["panes"][0]["task_id"] == task_id
    assert db.connection.execute("SELECT count(*) FROM panes").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM pane_session_links").fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT task_id,valid_from,valid_to,source FROM pane_session_links"
    ).fetchone() == (task_id, 90, None, "manual")
    db.__exit__()


def test_null_terminal_is_distinct_from_empty_terminal(tmp_path):
    collector, db, inputs = setup(tmp_path, replace(pane(), terminal_id=None))
    collector.collect(now=100)
    inputs.panes = [replace(pane(), terminal_id="")]
    collector.collect(now=110)

    assert db.connection.execute("SELECT count(*) FROM panes").fetchone()[0] == 2
    assert (
        db.connection.execute(
            "SELECT count(*) FROM pane_session_links WHERE terminal_id IS NULL"
        ).fetchone()[0]
        == 1
    )
    assert (
        db.connection.execute(
            "SELECT count(*) FROM pane_session_links WHERE terminal_id=''"
        ).fetchone()[0]
        == 1
    )
    db.__exit__()
