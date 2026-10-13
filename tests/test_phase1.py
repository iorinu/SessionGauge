from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sessiongauge.accounting import counter_deltas
from sessiongauge.adapters.herdr import HerdrAdapter
from sessiongauge.adapters.hermes_db import HermesDB
from sessiongauge.config import load_config
from sessiongauge.domain import Counters
from sessiongauge.identity import lineage, match_panes
from sessiongauge.persistence import MonitorDB


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (25)")
        conn.execute(
            "CREATE TABLE sessions(id TEXT, source TEXT, model TEXT, parent_session_id TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, "
            "actual_cost_usd REAL, started_at REAL, ended_at REAL, cwd TEXT, title TEXT, "
            "model_config TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, 'cli', 'model-x', NULL, ?, ?, NULL, 0, NULL, "
            "NULL, NULL, 1, NULL, '/private/project', 'private task', 'SECRET')",
            [(f"s{i}", i * 10, i * 2) for i in range(1, 4)],
        )
    return path


def test_sqlite_readonly_whitelisted_and_nullable(db):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with HermesDB(db) as adapter:
        rows = adapter.sessions()
        assert adapter.schema_version == 25
        assert rows[0].total_tokens == 12
        assert rows[0].cache_read_tokens is None
        assert rows[0].reasoning_tokens is None
        assert "model_config" not in adapter.last_query
        with pytest.raises(sqlite3.OperationalError):
            adapter.connection.execute("UPDATE sessions SET title='x'")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_herdr_strict_envelope_and_agent_session():
    fixture = json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures/herdr_panes.json").read_text()
    )
    panes = HerdrAdapter.parse(fixture, endpoint="local")
    assert len(panes) == 21
    linked = [pane for pane in panes if pane.session_id]
    assert [pane.session_id for pane in linked] == [
        "fixture-session-5",
        "fixture-session-8",
        "fixture-session-21",
    ]
    fixture["result"]["type"] = "other"
    with pytest.raises(ValueError):
        HerdrAdapter.parse(fixture, endpoint="local")
    fixture = json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures/herdr_panes.json").read_text()
    )
    pane = fixture["result"]["panes"][4]
    pane["agent_session"]["kind"] = "title"
    assert HerdrAdapter.parse(fixture, endpoint="local")[4].session_id is None
    pane["agent_session"]["kind"] = "id"
    pane["agent"] = "other-agent"
    assert HerdrAdapter.parse(fixture, endpoint="local")[4].session_id is None


def test_exact_matching_conflict_and_db_collision():
    panes = [
        {
            "endpoint": "e",
            "pane_id": "p1",
            "terminal_id": "t1",
            "session_id": "x",
            "agent": "hermes",
        },
        {
            "endpoint": "e",
            "pane_id": "p2",
            "terminal_id": "t2",
            "session_id": "x",
            "agent": "hermes",
        },
    ]
    result = match_panes(panes, {"db1": {"x"}, "db2": {"x"}})
    assert all(item.status == "conflict" and item.profile is None for item in result)
    panes[1]["session_id"] = "missing"
    result = match_panes(panes, {"db1": {"x"}})
    assert [item.status for item in result] == ["matched", "unlinked"]


def test_lineage_is_cycle_safe():
    assert lineage("c", {"c": "b", "b": "a", "a": None}) == ("c", "b", "a")
    assert lineage("a", {"a": "b", "b": "a"}) == ("a", "b")


def test_missing_columns_and_invalid_counters_are_unavailable(tmp_path):
    path = tmp_path / "partial.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT, input_tokens INTEGER)")
        conn.execute("INSERT INTO sessions VALUES ('one', -4)")
        conn.execute("INSERT INTO sessions VALUES ('two', NULL)")
    with HermesDB(path) as adapter:
        rows = adapter.sessions()
        assert rows[0].input_tokens is None
        assert rows[0].output_tokens is None
        assert rows[0].total_tokens is None


def test_wal_reader_does_not_block_writer(db):
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        with HermesDB(db) as reader:
            writer.execute("UPDATE sessions SET input_tokens=99 WHERE id='s1'")
            writer.commit()
            assert reader.sessions()[0].input_tokens in (10, 99)


def test_counter_deltas_unknown_on_initial_null_or_decrease():
    assert counter_deltas(None, Counters(input_tokens=100)).input_tokens is None
    delta = counter_deltas(
        Counters(input_tokens=100, output_tokens=20), Counters(input_tokens=150, output_tokens=15)
    )
    assert delta.input_tokens == 50
    assert delta.output_tokens is None
    assert delta.total_tokens is None


def test_config_precedence_cli_env_toml_and_defaults(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('poll_interval_seconds = 8\ndata_dir = "/toml"\n')
    env = {"SESSIONGAUGE_POLL_INTERVAL_SECONDS": "5", "SESSIONGAUGE_DATA_DIR": "/env"}
    config = load_config(
        cli={"poll_interval_seconds": 2, "data_dir": "/cli"}, environ=env, config_path=config_file
    )
    assert config.poll_interval_seconds == 2
    assert config.data_dir == Path("/cli")
    assert load_config(environ={}, config_path=tmp_path / "missing.toml").poll_interval_seconds == 3


def test_cli_doctor_redacts_and_live_once_json(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sessiongauge",
            "live",
            "--once",
            "--json",
            "--hermes-db",
            str(tmp_path / "absent.db"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["sessions"] == []


def test_monitor_persists_baseline_and_observed_delta(tmp_path):
    monitor_path = tmp_path / "data" / "monitor.db"
    first = Counters(input_tokens=100, output_tokens=20)
    second = Counters(input_tokens=150, output_tokens=35)
    with MonitorDB(monitor_path) as monitor:
        profile = monitor.register_profile("named", tmp_path / "hermes.db")
        assert monitor.record_snapshot(profile, "s", first, collected_at=100) is None
        delta = monitor.record_snapshot(profile, "s", second, collected_at=110)
        assert delta.input_tokens == 50
        assert delta.output_tokens == 15
    with MonitorDB(monitor_path) as monitor:
        assert monitor.snapshots(profile, "s") == 2


def test_monitor_rejects_hermes_database_alias_before_creation(tmp_path):
    source = tmp_path / "hermes.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT)")
    with pytest.raises(ValueError):
        MonitorDB(source, source_paths=[source]).__enter__()
    assert source.exists()


def test_live_once_and_profile_selector_are_parsed(tmp_path):
    from sessiongauge.cli import _parser

    args = _parser().parse_args(["live", "--once", "--json", "--profile", "work"])
    assert args.once is True
    assert args.profile == "work"
