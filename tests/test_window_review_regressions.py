import sqlite3
from concurrent.futures import Future
from contextlib import closing

import pytest
from test_estimate_lifecycle import displayed, lifecycle_harness, make_data, make_pane
from test_quota_history import history_files as history_files  # noqa: PLC0414
from test_window_estimator import (
    CURRENT,
    F,
    FakeClock,
    History,
    Reader,
    make_estimator,
    session,
)
from test_window_sidebar import EngineStub

from sessiongauge.quota_history import QuotaHistory
from sessiongauge.window_estimator import WindowQuotaEstimator
from sessiongauge.window_quota import QuotaEstimate


class ControlledExecutor:
    def __init__(self):
        self.jobs = []
        self.shutdown_calls = 0

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls += 1


def test_duplicate_sid_in_distinct_profiles_blocks_both_sidebar_targets():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    try:
        adapter.panes = [
            make_pane("model-a", pane_id="pane-a", terminal_id="term-a"),
            make_pane("model-a", pane_id="pane-b", terminal_id="term-b"),
        ]
        engine = EngineStub({"session-a": QuotaEstimate(12.5, 12.5)})
        reporter.window_estimator = engine
        data = make_data(profile, "model-a")
        first = data["panes"][0]
        first["pane_id"], first["terminal_id"] = "pane-a", "term-a"
        second = {
            **first,
            "pane_id": "pane-b",
            "terminal_id": "term-b",
            "profile": "another-profile",
            "session": {**first["session"]},
        }
        reporter.publish({"panes": [first]})
        assert len(engine.calls) == 1
        assert displayed(runner, "pane-a") == "~12.5%"
        command_index = len(runner.commands)
        engine.calls.clear()
        reporter.publish({"panes": [first, second]})
        assert engine.calls == []
        assert not any("--token" in command for command in runner.commands[command_index:])
    finally:
        reporter.close()


def test_close_does_not_cancel_running_worker_or_publish_its_result():
    executor = ControlledExecutor()
    estimator = WindowQuotaEstimator(
        History(), Reader(), clock=FakeClock(0), wall_clock=FakeClock(20000), executor=executor
    )
    sessions = {"current": session(buckets=CURRENT)}
    try:
        assert estimator.estimates(sessions) == {}
        future, fn, args = executor.jobs[0]
        assert future.set_running_or_notify_cancel()
        estimator.close()
        assert future.cancel() is False
        future.set_result(fn(*args))
        assert estimator.estimates(sessions) == {}
        assert len(executor.jobs) == 1
        assert executor.shutdown_calls == 1
    finally:
        estimator.close()


@pytest.mark.parametrize(
    "samples,expected",
    [
        ({"old": {"percent": 50, "data_as_of": 18500}}, QuotaEstimate(25, 25)),
        ({}, None),
    ],
)
def test_account_scope_change_refresh_does_not_mix_old_account_reference(samples, expected):
    wall, clock = FakeClock(20000), FakeClock(0)
    reader = Reader()
    estimator = make_estimator(reader=reader, clock=clock, wall=wall)
    sessions = {"current": session()}
    try:
        estimator.estimates(sessions)
        estimator.executor.jobs[0].complete()
        assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
        clock.advance(60)
        wall.advance(60)
        reader.context = {
            **reader.context,
            "account_scope": "b" * 64,
            "samples": samples,
        }
        assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
        assert len(estimator.executor.jobs) == 2
        estimator.executor.jobs[1].complete()
        actual = estimator.estimates(sessions)
        assert actual == ({"current": expected} if expected is not None else {})
        if expected is None:
            assert estimator.diagnostics["current"]["empirical_error_percent"] is None
    finally:
        estimator.close()


def test_pending_refresh_does_not_extend_cached_context_ttl():
    wall, clock = FakeClock(20000), FakeClock(0)
    estimator = make_estimator(wall=wall, clock=clock)
    sessions = {"current": session()}
    try:
        estimator.estimates(sessions)
        estimator.executor.jobs[0].complete()
        assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
        wall.advance(60)
        clock.advance(60)
        assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
        assert len(estimator.executor.jobs) == 2
        wall.advance(61)
        assert estimator.estimates(sessions) == {}
        assert len(estimator.executor.jobs) == 2
    finally:
        estimator.close()


def test_reference_ttl_is_independent_and_rpc_cannot_rejuvenate_reference():
    wall, clock = FakeClock(20000), FakeClock(0)
    estimator = make_estimator(wall=wall, clock=clock)
    estimator.max_age_seconds = 1500
    sessions = {"current": session()}
    try:
        estimator.estimates(sessions)
        estimator.executor.jobs[0].complete()
        assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
        assert len(estimator.reader.requests) == 1
        wall.advance(1)
        clock.advance(1)
        assert estimator.estimates(sessions) == {}
        assert len(estimator.executor.jobs) == 1
        wall.advance(59)
        clock.advance(59)
        assert estimator.estimates(sessions) == {}
        assert len(estimator.executor.jobs) == 2
        assert len(estimator.reader.requests) == 1
        estimator.executor.jobs[1].complete()
        assert estimator.estimates(sessions) == {}
        assert len(estimator.reader.requests) == 2
    finally:
        estimator.close()


def _insert_gap(history_files, *, reset=False):
    monitor, _source, profile = history_files
    with closing(sqlite3.connect(monitor)) as db:
        db.execute(
            "INSERT INTO session_catalog VALUES(?,?,?,?,?,?,?)",
            (profile, "gap", None, "m", 0, None, 11000),
        )
        rows = [(profile, "gap", 100, *F, 0, 1), (profile, "gap", 10000, 200, 1800, 0, 20, 0, 1)]
        if reset:
            rows.insert(1, (profile, "gap", 7000, 0, 0, 0, 0, 0, 1))
        db.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
    return QuotaHistory(monitor, _source, profile)


def test_sqlite_wide_gap_keeps_bracket_and_conditional_usage_ranges(history_files):
    history = _insert_gap(history_files)
    two_f = (200, 1800, 0, 20)
    # 観測されないギャップ内のreset+recoveryは観測値だけから検出できない。これは条件付き範囲であり、隠れたreset検出の証明ではない。
    assert history.bucket_bracket("gap", 5000) == (F, two_f)
    usage = history.window_usage("gap", 0, (250, 2250, 0, 25), 5000, 11000)
    assert usage == ((50, 450, 0, 5), (150, 1350, 0, 15))
    from sessiongauge.window_quota import estimate_bounds

    assert estimate_bounds(25, F, F, *usage) == QuotaEstimate(12.5, 37.5)


def test_sqlite_observed_reset_inside_gap_refuses_window_usage(history_files):
    history = _insert_gap(history_files, reset=True)
    assert history.window_usage("gap", 0, (250, 2250, 0, 25), 5000, 11000) is None
