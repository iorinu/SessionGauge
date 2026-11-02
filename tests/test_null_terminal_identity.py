from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from test_collector_acceptance import Collector, Config, MonitorDB, Sources, pane


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
