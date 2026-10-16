"""履歴バケットから現在windowのquota範囲を計算する。"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaEstimate:
    lower: float
    upper: float | None


def _buckets(value):
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    if any(type(item) is not int or item < 0 or item.bit_length() > 1023 for item in value):
        return None
    return tuple(value)


def estimate_bounds(reference_percent, reference_lower, reference_upper, usage_lower, usage_upper):
    """一定かつ非負のbucket単価と一致したprovider cutoffを仮定した条件付き範囲を返す。

    実APIの利用率・quota精度を保証するものではない。
    """
    try:
        if isinstance(reference_percent, bool) or not isinstance(reference_percent, (int, float)):
            return None
        percent = float(reference_percent)
        if not math.isfinite(percent) or percent <= 0:
            return None
        fl, fu, gl, gu = map(_buckets, (reference_lower, reference_upper, usage_lower, usage_upper))
        if any(item is None for item in (fl, fu, gl, gu)):
            return None
        if any(a > b for a, b in zip(fl, fu)) or any(a > b for a, b in zip(gl, gu)):
            return None
        if not any(fu):
            return None
        if not any(gu):
            return QuotaEstimate(0.0, 0.0)
        ratios_low = [g / f for g, f in zip(gl, fu) if f > 0]
        lower = percent * min(ratios_low, default=0.0)
        if any(f == 0 and g > 0 for f, g in zip(fl, gu)):
            upper = None
        else:
            ratios_high = [g / f for g, f in zip(gu, fl) if f > 0]
            upper = percent * max(ratios_high, default=0.0)
        if not math.isfinite(lower) or (upper is not None and not math.isfinite(upper)):
            return None
        return QuotaEstimate(lower, upper)
    except (OverflowError, ValueError, TypeError, ZeroDivisionError):
        return None
