import pytest

from sessiongauge.window_quota import estimate_bounds


def test_estimate_bounds_uses_independent_bucket_weighting_for_window_usage():
    from sessiongauge.window_quota import estimate_bounds

    rates = (1, 0.1, 1.25, 6)
    denominator = 1000

    # 独立した合成課金モデルで、過去の基準利用率を計算する。
    reference_buckets = (100, 900, 0, 10)
    reference_cost = sum(tokens * rate for tokens, rate in zip(reference_buckets, rates))
    reference_percent = reference_cost / denominator * 100

    # 基準時点と現在windowの値を分け、要求時点の累積token数は使わない。
    window_usage = (50, 450, 0, 5)
    estimate = estimate_bounds(
        reference_percent,
        reference_buckets,
        reference_buckets,
        window_usage,
        window_usage,
    )

    expected_window_percent = (
        sum(tokens * rate for tokens, rate in zip(window_usage, rates)) / denominator * 100
    )
    assert expected_window_percent == pytest.approx(12.5)
    assert (estimate.lower, estimate.upper) == pytest.approx((12.5, 12.5))


def test_bounds_cover_mixed_rates_and_open_unobserved_bucket():
    from sessiongauge.window_quota import estimate_bounds

    rates = (0.5, 0.1, 2.0, 5.0)
    reference = (100, 100, 0, 10)
    window = (20, 0, 0, 30)
    percent = sum(a * b for a, b in zip(reference, rates))
    estimate = estimate_bounds(percent, reference, reference, window, window)
    actual = sum(a * b for a, b in zip(window, rates))
    assert estimate.lower <= actual <= estimate.upper
    assert estimate_bounds(10, (1, 0, 0, 0), (1, 0, 0, 0), (0, 0, 1, 0), (0, 0, 1, 0)).upper is None


def test_bounds_fail_closed_for_bad_ranges_and_keep_overage():
    from sessiongauge.window_quota import estimate_bounds

    zero = estimate_bounds(10, (1, 0, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0))
    assert (zero.lower, zero.upper) == (0, 0)
    result = estimate_bounds(10, (1, 0, 0, 0), (1, 0, 0, 0), (11, 0, 0, 0), (11, 0, 0, 0))
    assert result.lower == 110
    assert result.upper == 110
    assert estimate_bounds(10, (True, 0, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0)) is None
    assert estimate_bounds(10, (2, 0, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0)) is None


@pytest.mark.parametrize(
    "percent",
    [None, True, 0, -1, float("inf"), 10**10000],
    ids=["none", "bool", "zero", "negative", "infinite", "huge"],
)
def test_invalid_percentages_fail_closed(percent):
    assert estimate_bounds(percent, (1, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)) is None


@pytest.mark.parametrize(
    "buckets",
    [None, (1, 2, 3), (True, 0, 0, 0), (-1, 0, 0, 0), (10**10000, 0, 0, 0)],
    ids=["none", "short", "bool", "negative", "huge"],
)
def test_invalid_bucket_lengths_and_values_fail_closed(buckets):
    assert estimate_bounds(10, buckets, buckets, (1, 0, 0, 0), (1, 0, 0, 0)) is None


def test_bracket_widening():
    result = estimate_bounds(10, (1, 0, 0, 0), (2, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0))
    assert (result.lower, result.upper) == (5, 10)
