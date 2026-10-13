from __future__ import annotations

import sqlite3

import pytest

from sessiongauge.collector import Collector
from sessiongauge.config import Config
from sessiongauge.domain import Counters, Pane, Session
from sessiongauge.persistence import MonitorDB


def session(sid="s", input_tokens=100, output_tokens=20, *, parent=None, end_reason=None):
    return Session(
        sid,
        "cli",
        "model",
        parent,
        Counters(input_tokens, output_tokens, 0, 0, None),
        0.5,
        None,
        1,
        None,
        title="session",
        end_reason=end_reason,
    )


class Sources:
    def __init__(self, panes=None, sessions=None):
        self.panes = panes or []
        self.rows = sessions or [session()]
        self.fail = False

    def list_panes(self):
        if self.fail:
            raise OSError("offline")
        return self.panes

    def sessions(self):
        return self.rows


def pane(pid="p", sid="s", terminal="t"):
    return Pane("socket://test", pid, terminal, "hermes", "working", "/work", "task", sid)


def test_collector_baseline_then_exact_attribution_and_persistent_task(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    inputs = Sources([pane()])
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(cfg, monitor, inputs)
        first = collector.collect(now=100)
        assert first["collected_at"] == 100
        assert first["panes"][0]["session"] is not None
        inputs.rows = [session(input_tokens=150, output_tokens=35)]
        second = collector.collect(now=110)
        assert second["collected_at"] == 110
        assert second["panes"][0]["observed"]["input_tokens"] == 50
        assert second["panes"][0]["observed"]["output_tokens"] == 15
        records = monitor.connection.execute(
            "SELECT task_id,input_tokens,output_tokens FROM usage_attributions"
        ).fetchall()
        assert len(records) == 1
        assert records[0][0] is not None
        assert records[0][1:] == (50, 15)


def test_generic_hermes_pane_uses_confirmed_session_title(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    adapter = Sources(
        [Pane("socket://test", "p", "t", "hermes", "working", "/work", "Hermes", "s")]
    )
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        result = Collector(cfg, monitor, adapter).collect(now=100)

        assert result["panes"][0]["title"] == "session"
        assert monitor.connection.execute("SELECT title FROM panes").fetchone() == ("session",)
        assert monitor.connection.execute("SELECT display_name FROM tasks").fetchone() == (
            "session",
        )


def test_collector_failed_source_is_stale_and_never_attributes_gap(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    adapter = Sources([pane()])
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(cfg, monitor, adapter)
        collector.collect(now=100)
        adapter.fail = True
        failed = collector.collect(now=110)
        assert failed["stale"] is True
        adapter.fail = False
        recovered = collector.collect(now=120)
        assert recovered["stale"] is False
        rows = monitor.connection.execute("SELECT task_id FROM usage_attributions").fetchall()
        assert rows and all(row[0] is None for row in rows)


def test_monitor_refuses_unknown_database_without_creating_tables(tmp_path):
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value)")
    with pytest.raises(ValueError), MonitorDB(path):
        pass
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("unrelated",)
        ]


def test_monitor_does_not_chmod_existing_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    with MonitorDB(parent / "monitor.db"):
        pass
    assert parent.stat().st_mode & 0o777 == 0o755
