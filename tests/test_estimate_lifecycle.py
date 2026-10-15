"""推定ジョブの世代とpane lifecycleを公開publish経由で検証する。"""

import json
import uuid
from pathlib import Path

from sessiongauge.domain import Pane
from sessiongauge.sidebar import SidebarReporter


class ManualFuture:
    def __init__(self, fn, args):
        self.fn = fn
        self.args = args
        self._done = False
        self._result = None

    def done(self):
        return self._done

    def result(self):
        if not self._done:
            raise RuntimeError("job is still pending")
        return self._result

    def complete(self, result=None):
        self._result = self.fn(*self.args) if result is None else result
        self._done = True


class ManualExecutor:
    def __init__(self, max_workers=1, thread_name_prefix=None):
        self.jobs = []

    def submit(self, fn, *args):
        future = ManualFuture(fn, args)
        self.jobs.append(future)
        return future

    def shutdown(self, wait=False, cancel_futures=False):
        pass


class FakeClock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


class FakeQuotaReader:
    poll_interval_seconds = 60

    def __init__(self):
        self.requests = []
        self.response = {}

    def read_session_metadata(self, threads):
        self.requests.append(threads)
        return self.response


class FakeHerdrRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, args, **kwargs):
        self.commands.append(args)
        if args[-1:] == ["--help"]:
            stdout = "report-metadata --source --token --seq --ttl-ms"
        elif args[-2:] == ["schema", "--json"]:
            stdout = Path("tests/fixtures/herdr_metadata_schema.json").read_text()
        else:
            stdout = json.dumps({"result": {"type": "ok"}})
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


class FakeHerdrAdapter:
    def __init__(self):
        self.panes = []

    def list_panes(self):
        return list(self.panes)


def lifecycle_harness():
    profile = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            str((Path("~/.hermes").expanduser() / "state.db").resolve(strict=False)),
        )
    )
    monotonic = FakeClock()
    wall = FakeClock()
    reader = FakeQuotaReader()
    adapter = FakeHerdrAdapter()
    runner = FakeHerdrRunner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_mode="estimate",
        display="percent",
        runner=runner,
        adapter=adapter,
        quota_reader=reader,
        monotonic_clock=monotonic,
        wall_clock=wall,
    )
    reporter._quota_executor.shutdown()
    reporter._quota_executor = ManualExecutor()
    return reporter, profile, monotonic, wall, reader, adapter, runner


def make_pane(model, *, pane_id="pane-a", terminal_id="terminal-a", sid="session-a", tokens=1000):
    return Pane("endpoint-a", pane_id, terminal_id, "hermes", "running", None, None, sid)


def make_data(profile, model, *, sid="session-a", tokens=1000, status="matched"):
    return {
        "panes": [
            {
                "pane_id": "pane-a",
                "terminal_id": "terminal-a",
                "status": status,
                "profile": profile,
                "session": {"id": sid, "total_tokens": tokens, "model": model},
            }
        ]
    }


def displayed(runner, pane_id="pane-a"):
    command = [
        command
        for command in runner.commands
        if len(command) > 3 and command[3] == pane_id and "--token" in command
    ][-1]
    return command[command.index("--token") + 1].removeprefix("sessiongauge_usage=")


def test_old_model_generation_is_rejected_after_model_returns():
    reporter, profile, _, _, _reader, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]

    reporter.publish(make_data(profile, "model-a"))
    assert displayed(runner) == "N/A"
    job = reporter._quota_future

    reporter.publish(make_data(profile, "model-b"))
    reporter.publish(make_data(profile, "model-a"))
    job.complete({"session-a": {"percent": 10, "data_as_of": 100}})
    reporter.publish(make_data(profile, "model-a"))

    assert displayed(runner) == "N/A"
    assert len(reporter._quota_executor.jobs) == 1
    reporter.close()


def test_same_sample_batch_and_token_increases_remain_unavailable():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [
        make_pane("model-a", pane_id="pane-a", terminal_id="terminal-a", sid="session-a"),
        make_pane("model-b", pane_id="pane-b", terminal_id="terminal-b", sid="session-b"),
    ]

    def data(tokens_a, tokens_b):
        rows = []
        for pane_id, terminal_id, sid, model, tokens in (
            ("pane-a", "terminal-a", "session-a", "model-a", tokens_a),
            ("pane-b", "terminal-b", "session-b", "model-b", tokens_b),
        ):
            rows.append(
                {
                    "pane_id": pane_id,
                    "terminal_id": terminal_id,
                    "status": "matched",
                    "profile": profile,
                    "session": {"id": sid, "total_tokens": tokens, "model": model},
                }
            )
        return {"panes": rows}

    reporter.publish(data(1000, 1000))
    reporter._quota_future.complete(
        {
            "session-a": {"percent": 15, "data_as_of": 100},
            "session-b": {"percent": 30, "data_as_of": 100},
        }
    )
    reporter.publish(data(1000, 1000))
    assert displayed(runner, "pane-a") == "N/A"
    assert displayed(runner, "pane-b") == "N/A"

    reporter.publish(data(1200, 1100))
    assert displayed(runner, "pane-a") == "N/A"
    assert displayed(runner, "pane-b") == "N/A"
    reporter.close()


def test_pending_estimate_is_not_resubmitted_and_poll_boundary_is_exact():
    reporter, profile, monotonic, _, _, adapter, _ = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    data = make_data(profile, "model-a")
    reporter.publish(data)
    assert len(reporter._quota_executor.jobs) == 1
    for _ in range(4):
        reporter.publish(data)
    assert len(reporter._quota_executor.jobs) == 1

    reporter._quota_future.complete({})
    reporter.publish(data)
    assert len(reporter._quota_executor.jobs) == 1
    monotonic.advance(59)
    reporter.publish(data)
    assert len(reporter._quota_executor.jobs) == 1
    monotonic.advance(1)
    reporter.publish(data)
    assert len(reporter._quota_executor.jobs) == 2
    reporter.close()


def test_counter_reset_never_enables_estimate():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    reporter.publish(make_data(profile, "model-a", tokens=1000))
    reporter._quota_future.complete({"session-a": {"percent": 10, "data_as_of": 100}})
    reporter.publish(make_data(profile, "model-a", tokens=1500))
    assert displayed(runner) == "N/A"

    reporter.publish(make_data(profile, "model-a", tokens=1400))
    assert displayed(runner) == "N/A"
    reporter.close()


def test_counter_zero_resets_generation_before_tokens_rise_again():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    reporter.publish(make_data(profile, "model-a", tokens=1000))
    reporter._quota_future.complete({"session-a": {"percent": 10, "data_as_of": 100}})
    reporter.publish(make_data(profile, "model-a", tokens=1500))
    reporter.publish(make_data(profile, "model-a", tokens=0))
    reporter.publish(make_data(profile, "model-a", tokens=1600))
    assert displayed(runner) == "N/A"
    reporter.close()


def test_disappearance_rejects_old_result_without_invalidating_other_pane():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    pane_a = make_pane("model-a", pane_id="pane-a", terminal_id="terminal-a", sid="session-a")
    pane_b = make_pane("model-b", pane_id="pane-b", terminal_id="terminal-b", sid="session-b")
    adapter.panes = [pane_a, pane_b]
    data = {
        "panes": [
            {
                "pane_id": "pane-a",
                "terminal_id": "terminal-a",
                "status": "matched",
                "profile": profile,
                "session": {"id": "session-a", "total_tokens": 1000, "model": "model-a"},
            },
            {
                "pane_id": "pane-b",
                "terminal_id": "terminal-b",
                "status": "matched",
                "profile": profile,
                "session": {"id": "session-b", "total_tokens": 1000, "model": "model-b"},
            },
        ]
    }
    reporter.publish(data)
    job = reporter._quota_future
    adapter.panes = [pane_b]
    reporter.publish({"panes": [data["panes"][1]]})
    adapter.panes = [pane_a, pane_b]
    reporter.publish(data)
    job.complete(
        {
            "session-a": {"percent": 10, "data_as_of": 100},
            "session-b": {"percent": 20, "data_as_of": 100},
        }
    )
    reporter.publish(data)
    assert displayed(runner, "pane-a") == "N/A"
    assert displayed(runner, "pane-b") == "N/A"
    reporter.close()


def test_close_clears_owned_value_once_and_completed_job_cannot_publish():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    reporter.publish(make_data(profile, "model-a"))
    job = reporter._quota_future
    writes_before_close = len([command for command in runner.commands if "--token" in command])
    reporter.close()
    clears_at_close = len([command for command in runner.commands if "--clear-token" in command])
    job.complete({"session-a": {"percent": 10, "data_as_of": 100}})
    reporter.publish(make_data(profile, "model-a"))
    assert (
        len([command for command in runner.commands if "--token" in command]) == writes_before_close
    )
    assert (
        len([command for command in runner.commands if "--clear-token" in command])
        == clears_at_close
    )


def test_invalid_token_and_new_sample_cannot_authorize_estimate():
    from sessiongauge.quota import TokenQuotaEstimator

    estimator = TokenQuotaEstimator(max_age_seconds=30, clock=lambda: 0, wall_clock=lambda: 100)
    scope = {"profile_id": "p", "session_id": "s", "model": "m"}
    assert not estimator.calibrate(**scope, total_tokens=1000, percent=10, data_as_of=100)
    assert estimator.estimate(**scope, total_tokens=0) is None
    assert estimator.estimate(**scope, total_tokens=1600) is None
    assert not estimator.calibrate(**scope, total_tokens=1600, percent=15, data_as_of=100)


def test_new_reporter_starts_and_remains_without_estimate():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    reporter.publish(make_data(profile, "model-a"))
    assert displayed(runner) == "N/A"
    reporter.close()

    restarted, restarted_profile, _, _, _, restarted_adapter, restarted_runner = lifecycle_harness()
    restarted_adapter.panes = [make_pane("model-a")]
    restarted.publish(make_data(restarted_profile, "model-a"))
    assert displayed(restarted_runner) == "N/A"
    restarted.close()
