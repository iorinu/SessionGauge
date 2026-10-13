from __future__ import annotations

from sessiongauge.collector import Collector
from sessiongauge.config import Config
from sessiongauge.domain import Counters, Pane, Session
from sessiongauge.persistence import MonitorDB


def session(sid="same", input_tokens=100, output_tokens=20):
    return Session(
        sid,
        "cli",
        "model",
        None,
        Counters(input_tokens, output_tokens, 0, 0, None),
        0.5,
        None,
        1,
        None,
        title=sid,
    )


def pane(pid, sid=None):
    return Pane("socket://test", pid, "terminal-" + pid, "hermes", "working", "/work", pid, sid)


class TwoSources:
    def __init__(self, panes, rows):
        self.panes = panes
        self.rows = rows

    def list_panes(self):
        return self.panes

    def sessions(self, path):
        return self.rows[path]


def test_manual_fallback_same_sid_is_scoped_to_profile_and_task(tmp_path):
    source_a, source_b = tmp_path / "a.db", tmp_path / "b.db"
    source_a.touch()
    source_b.touch()
    cfg = Config(3, (source_a, source_b), tmp_path / "data", "local")
    sources = TwoSources([pane("a"), pane("b")], {source_a: [session()], source_b: [session()]})
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=cfg.hermes_db_paths) as monitor:
        collector = Collector(cfg, monitor, sources)
        profile_a = monitor.register_profile("a", source_a)
        profile_b = monitor.register_profile("b", source_b)
        task_a = monitor.link_manual(sources.panes[0], profile_a, "same", 90)
        task_b = monitor.link_manual(sources.panes[1], profile_b, "same", 90)
        collector.collect(now=100)
        sources.rows[source_a] = [session(input_tokens=150, output_tokens=35)]
        collector.collect(now=110)
        rows = monitor.connection.execute(
            "SELECT profile_id,task_id,input_tokens,output_tokens FROM usage_attributions"
        ).fetchall()
        delta_a = next(row for row in rows if row[0] == profile_a)
        assert delta_a[1:] == (task_a, 50, 15)
        assert all(row[1] != task_a for row in rows if row[0] == profile_b)
        assert task_a != task_b


def test_official_and_manual_fallback_duplicate_become_conflict(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    sources = TwoSources([pane("official", "same"), pane("manual")], {source: [session()]})
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(cfg, monitor, sources)
        collector.collect(now=100)
        profile = monitor.register_profile("default", source)
        monitor.link_manual(sources.panes[1], profile, "same", 101)
        sources.rows[source] = [session(input_tokens=150, output_tokens=35)]
        result = collector.collect(now=110)
        assert [item["status"] for item in result["panes"]] == ["conflict", "conflict"]
        assert (
            monitor.connection.execute("SELECT task_id FROM usage_attributions").fetchone()[0]
            is None
        )


def test_manual_fallback_requires_session_present_in_source(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    cfg = Config(3, (source,), tmp_path / "data", "local")
    sources = TwoSources([pane("manual")], {source: [session()]})
    with MonitorDB(cfg.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(cfg, monitor, sources)
        profile = monitor.register_profile("default", source)
        monitor.link_manual(sources.panes[0], profile, "missing", 90)
        result = collector.collect(now=100)
        assert result["panes"][0]["status"] == "unlinked"
        assert result["panes"][0]["task_id"] is None
