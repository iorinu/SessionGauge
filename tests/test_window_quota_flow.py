"""公開sidebar経由で実SQLite履歴を使ったwindow quota校正を検証する。"""

import hashlib
import sqlite3
import uuid
from contextlib import closing

from test_estimate_lifecycle import (
    FakeHerdrAdapter,
    FakeHerdrRunner,
    displayed,
    make_data,
    make_pane,
)
from test_window_estimator import FakeClock, FakeExecutor

from sessiongauge.quota_history import QuotaHistory
from sessiongauge.sidebar import SidebarReporter
from sessiongauge.window_estimator import WindowQuotaEstimator


class ContextReader:
    poll_interval_seconds = 60

    def __init__(self, hermes_home):
        self.hermes_home = hermes_home
        self.requests = []

    def read_estimate_context(self, threads):
        self.requests.append(threads)
        return {
            "samples": {"oldSID": {"percent": 25, "data_as_of": 48000}},
            "window_start": 49000,
            "window_end": 67000,
            "account_scope": "a" * 64,
        }

    def read(self):
        raise AssertionError("legacy aggregate quota reader must not be called")

    def read_session_metadata(self, threads):
        raise AssertionError("legacy session metadata reader must not be called")


def _create_databases(source_path, monitor_path, profile):
    buckets = (100, 900, 0, 10)
    with closing(sqlite3.connect(source_path)) as db:
        db.execute(
            "CREATE TABLE session_model_usage("
            "session_id,model,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens)"
        )
        db.executemany(
            "INSERT INTO session_model_usage VALUES(?,?,?,?,?,?)",
            [(sid, "m", *buckets) for sid in ("oldSID", "session-a")],
        )
        db.commit()

    with closing(sqlite3.connect(monitor_path)) as db:
        db.executescript(
            "CREATE TABLE session_catalog("
            "profile_id,session_id,parent_session_id,model,started_at,ended_at,last_activity_at);"
            "CREATE TABLE snapshots("
            "profile_id,session_id,collected_at,input_tokens,cache_read_tokens,"
            "cache_write_tokens,output_tokens,reasoning_tokens,source_ok);"
        )
        db.executemany(
            "INSERT INTO session_catalog VALUES(?,?,?,?,?,?,?)",
            [
                (profile, "oldSID", None, "m", 1000, 48100, 48100),
                (profile, "session-a", None, "m", 47000, None, 50000),
            ],
        )
        db.executemany(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (profile, sid, stamp, *values, 0, 1)
                for sid, stamp, values in (
                    ("oldSID", 47900, buckets),
                    ("oldSID", 48100, buckets),
                    ("session-a", 48900, buckets),
                    ("session-a", 49100, buckets),
                    ("session-a", 50000, (150, 1350, 0, 15)),
                )
            ],
        )
        db.commit()


def test_reference_before_window_calibrates_current_window_sidebar_estimate(tmp_path):
    source_path = tmp_path / "hermes" / "state.db"
    source_path.parent.mkdir(exist_ok=True)
    monitor_path = tmp_path / "monitor.db"
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str(source_path.resolve())))
    _create_databases(source_path, monitor_path, profile)
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()
    monitor_hash = hashlib.sha256(monitor_path.read_bytes()).digest()

    wall = FakeClock(50000)
    monotonic = FakeClock(0)
    executor = FakeExecutor()
    reader = ContextReader(source_path.parent)
    history = QuotaHistory(monitor_path, source_path, profile)
    estimator = WindowQuotaEstimator(
        history,
        reader,
        clock=monotonic,
        wall_clock=wall,
        executor=executor,
    )
    runner = FakeHerdrRunner()
    adapter = FakeHerdrAdapter()
    adapter.panes = [make_pane("m")]
    warnings = []
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_mode="estimate",
        runner=runner,
        adapter=adapter,
        quota_reader=reader,
        hermes_home=str(source_path.parent),
        monotonic_clock=monotonic,
        wall_clock=wall,
        warning=warnings.append,
        window_estimator=estimator,
    )

    data = make_data(profile, "m", tokens=165)
    session = data["panes"][0]["session"]
    session.update(
        {
            "started_at": 47000,
            "input_tokens": 150,
            "cache_read_tokens": 1350,
            "cache_write_tokens": 0,
            "output_tokens": 15,
        }
    )
    try:
        reporter.publish(data)
        assert displayed(runner) == "N/A"
        assert len(executor.jobs) == 1
        executor.jobs[0].complete()
        reporter.publish(data)

        # 合成rateは期待値計算専用で、実際のprovider重みは不明。productionではrateを使わない。
        synthetic_rates = (1, 0.1, 1.25, 6)
        denominator = 1000
        window_delta = (150 - 100, 1350 - 900, 0, 15 - 10)
        expected = (
            sum(tokens * rate for tokens, rate in zip(window_delta, synthetic_rates))
            / denominator
            * 100
        )
        assert expected == 12.5
        assert displayed(runner) == f"~{expected:.1f}%"
        assert reader.requests == [
            [
                {
                    "thread_id": "session-a",
                    "created_at": "1970-01-01T13:03:20Z",
                    "descendant_thread_ids": [],
                },
                {
                    "thread_id": "oldSID",
                    "created_at": "1970-01-01T00:16:40Z",
                    "descendant_thread_ids": [],
                },
            ]
        ]

        payload = runner.commands[-1]
        assert payload[3] == "pane-a"
        thread_payload = executor.jobs[0].args[0]
        assert all(
            set(thread) == {"thread_id", "created_at", "descendant_thread_ids"}
            for thread in thread_payload
        )
        assert {thread["thread_id"] for thread in thread_payload} == {"oldSID", "session-a"}
        assert any("stable same-model bucket rates" in warning for warning in warnings)
        assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash
        assert hashlib.sha256(monitor_path.read_bytes()).digest() == monitor_hash
    finally:
        reporter.close()
        reporter.close()
