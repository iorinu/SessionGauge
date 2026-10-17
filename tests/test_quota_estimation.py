import pytest


def test_estimates_percentage_from_token_increase_within_same_scope():
    from sessiongauge import quota

    monotonic_time = [0]
    estimator = quota.TokenQuotaEstimator(
        max_age_seconds=86400,
        clock=lambda: monotonic_time[0],
        wall_clock=lambda: 100,
    )

    assert estimator.calibrate(
        profile_id="profile-a",
        session_id="session-a",
        model="model-a",
        total_tokens=1000,
        percent=10,
        data_as_of=100,
    )

    estimated_percent = estimator.estimate(
        profile_id="profile-a",
        session_id="session-a",
        model="model-a",
        total_tokens=1500,
    )

    assert estimated_percent == pytest.approx(15)


def test_estimator_recalibrates_only_on_newer_sample_and_expires():
    from sessiongauge.quota import TokenQuotaEstimator

    now = [0]
    estimator = TokenQuotaEstimator(max_age_seconds=10, clock=lambda: now[0], wall_clock=lambda: 20)
    scope = {"profile_id": "p", "session_id": "s", "model": "m"}
    assert estimator.calibrate(**scope, total_tokens=100, percent=10, data_as_of=10)
    assert not estimator.calibrate(**scope, total_tokens=200, percent=90, data_as_of=10)
    assert estimator.estimate(**scope, total_tokens=200) == pytest.approx(20)
    assert estimator.calibrate(**scope, total_tokens=200, percent=15, data_as_of=11)
    assert estimator.estimate(**scope, total_tokens=300) == pytest.approx(22.5)
    assert not estimator.calibrate(**scope, total_tokens=300, percent=5, data_as_of=9)
    now[0] = 10
    assert estimator.estimate(**scope, total_tokens=300) is None


@pytest.mark.parametrize("tokens", [0, -1, True, 10**400])
def test_estimator_rejects_invalid_token_counts(tokens):
    from sessiongauge.quota import TokenQuotaEstimator

    estimator = TokenQuotaEstimator(wall_clock=lambda: 1)
    assert not estimator.calibrate(
        profile_id="p",
        session_id="s",
        model="m",
        total_tokens=tokens,
        percent=10,
        data_as_of=1,
    )


@pytest.mark.parametrize("percent", [0, -1, True, float("nan"), float("inf"), 10**400])
def test_estimator_rejects_invalid_calibration_percentages(percent):
    from sessiongauge.quota import TokenQuotaEstimator

    estimator = TokenQuotaEstimator(wall_clock=lambda: 1)
    assert not estimator.calibrate(
        profile_id="p",
        session_id="s",
        model="m",
        total_tokens=100,
        percent=percent,
        data_as_of=1,
    )


def test_estimator_does_not_clamp_above_100_and_invalidates_counter_reset():
    from sessiongauge.quota import TokenQuotaEstimator

    estimator = TokenQuotaEstimator(wall_clock=lambda: 1)
    scope = {"profile_id": "p", "session_id": "s", "model": "m"}
    assert estimator.calibrate(**scope, total_tokens=100, percent=90, data_as_of=1)
    assert estimator.estimate(**scope, total_tokens=120) == pytest.approx(108)
    assert estimator.estimate(**scope, total_tokens=99) is None


def test_estimator_scope_changes_do_not_reuse_calibration():
    from sessiongauge.quota import TokenQuotaEstimator

    estimator = TokenQuotaEstimator(wall_clock=lambda: 1)
    assert estimator.calibrate(
        profile_id="p",
        session_id="s",
        model="m",
        total_tokens=100,
        percent=10,
        data_as_of=1,
    )
    assert (
        estimator.estimate(profile_id="p", session_id="s", model="other", total_tokens=200) is None
    )
    assert (
        estimator.estimate(profile_id="other", session_id="s", model="m", total_tokens=200) is None
    )
