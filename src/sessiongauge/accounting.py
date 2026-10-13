from sessiongauge.domain import Counters


def counter_deltas(previous: Counters | None, current: Counters) -> Counters:
    """初回・欠損・減少を差分不明として保持し、負の差分を作らない。"""
    if previous is None:
        return Counters()
    values = {}
    for field in Counters.__dataclass_fields__:
        before, now = getattr(previous, field), getattr(current, field)
        values[field] = (
            now - before if before is not None and now is not None and now >= before else None
        )
    return Counters(**values)
