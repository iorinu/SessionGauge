from sessiongauge.window_estimator import WindowQuotaEstimator
from sessiongauge.window_quota import QuotaEstimate


class FakeFuture:
    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.finished = False
        self.cancelled = False
        self.value = None

    def done(self):
        return self.finished

    def result(self):
        if not self.finished:
            raise RuntimeError("pending")
        return self.value

    def complete(self, value=None):
        self.value = self.fn(*self.args) if value is None else value
        self.finished = True

    def cancel(self):
        self.cancelled = True
        return True


class FakeExecutor:
    def __init__(self):
        self.jobs = []
        self.shutdown_calls = 0

    def submit(self, fn, *args):
        job = FakeFuture(fn, args)
        self.jobs.append(job)
        return job

    def shutdown(self, **kwargs):
        self.shutdown_calls += 1


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


F = (100, 900, 0, 10)
CURRENT = (150, 1350, 0, 15)


class History:
    def __init__(self, candidates=None, reference=(F, F), single=True):
        self.candidates = (
            candidates
            if candidates is not None
            else [{"sid": "old", "model": "m", "started_at": 1000}]
        )
        self.reference, self.single = reference, single
        self.calls = []

    def reference_candidates(self, ids, now, limit):
        self.calls.append((ids, now, limit))
        return self.candidates

    def single_model(self, sid, model):
        return self.single and model == "m"

    def bucket_bracket(self, sid, at):
        self.calls.append((sid, at))
        return self.reference

    def window_usage(self, sid, started, buckets, start, now):
        self.calls.append((sid, started, buckets, start, now))
        current = tuple(buckets)
        baseline = F
        deltas = tuple(now - before for now, before in zip(current, baseline))
        if any(delta < 0 for delta in deltas):
            return None
        return (deltas, deltas)


class Reader:
    poll_interval_seconds = 60

    def __init__(self, context=None):
        self.context = context or {
            "samples": {"old": {"percent": 25, "data_as_of": 18500}},
            "window_start": 19000,
            "window_end": 37000,
            "account_scope": "a" * 64,
        }
        self.requests = []

    def read_estimate_context(self, threads):
        self.requests.append(threads)
        return deepcopy(self.context)


def session(sid="current", model="m", buckets=CURRENT):
    return dict(
        zip(("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"), buckets)
    ) | {
        "model": model,
        "started_at": 17000,
    }


def make_estimator(*, history=None, reader=None, clock=None, wall=None):
    return WindowQuotaEstimator(
        history or History(),
        reader or Reader(),
        clock=clock or FakeClock(0),
        wall_clock=wall or FakeClock(20000),
        executor=FakeExecutor(),
    )


def test_pending_job_uses_real_buckets_window_and_one_reference_job():
    history, reader = History(), Reader()
    estimator = make_estimator(history=history, reader=reader)
    sessions = {"current": session()}
    assert estimator.estimates(sessions) == {}
    assert len(estimator.executor.jobs) == 1
    estimator.executor.jobs[0].complete()
    assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
    assert ("current", 17000.0, CURRENT, 19000, 20000) in history.calls
    assert ("old", 18500) in history.calls
    assert estimator.diagnostics["current"]["empirical_error_percent"] is None
    estimator.close()


def test_reference_candidates_may_be_empty_for_current_sid_api_sample():
    context = Reader().context
    context["samples"] = {"current": {"percent": 25, "data_as_of": 18500}}
    history = History(candidates=[])
    estimator = make_estimator(history=history, reader=Reader(context))
    sessions = {"current": session()}
    estimator.estimates(sessions)
    estimator.executor.jobs[0].complete()
    assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
    estimator.close()


def test_context_age_starts_when_job_completes_not_when_consumed():
    wall = FakeClock(20000)
    estimator = make_estimator(wall=wall)
    sessions = {"current": session()}
    estimator.estimates(sessions)
    estimator.executor.jobs[0].complete()
    wall.advance(121)
    assert estimator.estimates(sessions) == {}
    estimator.close()


def test_expired_window_and_reference_do_not_retain_prior_estimate():
    clock = FakeClock(0)
    wall = FakeClock(20000)
    reader = Reader()
    estimator = make_estimator(reader=reader, clock=clock, wall=wall)
    sessions = {"current": session()}
    estimator.estimates(sessions)
    estimator.executor.jobs[0].complete()
    assert estimator.estimates(sessions) == {"current": QuotaEstimate(12.5, 12.5)}
    wall.advance(17001)
    clock.advance(60)
    assert estimator.estimates(sessions) == {}
    estimator.close()


def test_close_is_idempotent_and_prevents_resubmission():
    estimator = make_estimator()
    estimator.estimates({"current": session()})
    job = estimator.executor.jobs[0]
    estimator.close()
    estimator.close()
    assert job.cancelled
    assert estimator.executor.shutdown_calls == 1
    assert estimator.estimates({"current": session()}) == {}
    assert len(estimator.executor.jobs) == 1


from copy import deepcopy
