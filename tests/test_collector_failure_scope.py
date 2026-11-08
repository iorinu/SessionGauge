from __future__ import annotations

from sessiongauge.collector import Collector
from sessiongauge.config import Config
from sessiongauge.domain import Counters, Pane, Session
from sessiongauge.persistence import MonitorDB


def session(sid="same", input_tokens=100):
    return Session(
        sid,
        "cli",
        "model",
        None,
        Counters(input_tokens, 20, 0, 0, None),
        0.5,
        None,
        1,
        None,
        title=sid,
    )


def pane(pid, sid=None):
    return Pane("socket://test", pid, f"terminal-{pid}", "hermes", "working", "/work", pid, sid)


class FakeSources:
    def __init__(self, panes, rows):
        self.panes = panes
        self.rows = rows
        self.fail_herdr = False
        self.fail_sources = set()

    def list_panes(self):
        if self.fail_herdr:
            raise OSError("herdr unavailable")
        return self.panes

    def sessions(self, path):
        if path in self.fail_sources:
            raise OSError("source unavailable")
        return self.rows[path]


def test_recovered_herdr_and_failed_source_returns_unlinked_stale_session(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    config = Config(3, (source,), tmp_path / "data", "local")
    inputs = FakeSources([], {source: [session()]})
    with MonitorDB(config.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(config, monitor, inputs)
        collector.collect(now=100)

        inputs.fail_herdr = True
        collector.collect(now=110)
        inputs.fail_herdr = False
        inputs.fail_sources.add(source)
        inputs.panes = [pane("p", "same")]
        links_before = monitor.connection.execute(
            "SELECT count(*) FROM pane_session_links"
        ).fetchone()[0]
        attributions_before = monitor.connection.execute(
            "SELECT count(*) FROM usage_attributions"
        ).fetchone()[0]

        result = collector.collect(now=113)

        assert result["stale"] is True
        assert result["panes"][0]["task_id"] is None
        assert (
            monitor.connection.execute("SELECT count(*) FROM pane_session_links").fetchone()[0]
            == links_before
        )
        assert (
            monitor.connection.execute("SELECT count(*) FROM usage_attributions").fetchone()[0]
            == attributions_before
        )


def test_herdr_failure_records_fresh_source_usage_unattributed(tmp_path):
    source = tmp_path / "state.db"
    source.touch()
    config = Config(3, (source,), tmp_path / "data", "local")
    inputs = FakeSources([], {source: [session()]})
    with MonitorDB(config.data_dir / "monitor.db", source_paths=(source,)) as monitor:
        collector = Collector(config, monitor, inputs)
        collector.collect(now=100)

        inputs.fail_herdr = True
        inputs.rows[source] = [session(input_tokens=140)]
        collector.collect(now=110)

        snapshot = monitor.connection.execute(
            "SELECT input_tokens,source_ok FROM snapshots ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
        attribution = monitor.connection.execute(
            "SELECT input_tokens,task_id,provenance FROM usage_attributions"
        ).fetchone()

        assert snapshot == (140, 0)
        assert attribution == (40, None, "source_failure")


def test_stale_first_profile_does_not_take_second_profiles_same_sid_fallback(tmp_path):
    source_a, source_b = tmp_path / "a.db", tmp_path / "b.db"
    source_a.touch()
    source_b.touch()
    config = Config(3, (source_a, source_b), tmp_path / "data", "local")
    inputs = FakeSources(
        [pane("a"), pane("b")],
        {source_a: [session()], source_b: [session()]},
    )
    with MonitorDB(config.data_dir / "monitor.db", source_paths=config.hermes_db_paths) as monitor:
        collector = Collector(config, monitor, inputs)
        profile_a = monitor.register_profile("a", source_a)
        profile_b = monitor.register_profile("b", source_b)
        task_a = monitor.link_manual(inputs.panes[0], profile_a, "same", 90)
        task_b = monitor.link_manual(inputs.panes[1], profile_b, "same", 90)

        # fallback の根拠だけを残し、active link をなくして stale 行への誤帰属を検出する。
        monitor.connection.execute(
            "UPDATE pane_session_links SET valid_to=95 WHERE task_id IN (?, ?)",
            (task_a, task_b),
        )
        inputs.fail_herdr = True
        collector.collect(now=100)
        inputs.fail_herdr = False
        inputs.fail_sources.add(source_a)
        inputs.panes = [pane("a"), pane("b")]

        result = collector.collect(now=103)

        stale_pane = next(item for item in result["panes"] if item["pane_id"] == "a")
        assert stale_pane["task_id"] is None
        assert stale_pane["task_id"] != task_b
