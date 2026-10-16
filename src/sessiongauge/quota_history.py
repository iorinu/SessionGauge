"""quota推定に必要な履歴だけを読み取り専用で取得する。"""

import math
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

_BUCKETS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _counts(row):
    values = tuple(row[name] for name in _BUCKETS)
    if any(type(value) is not int or value < 0 or value.bit_length() > 1023 for value in values):
        return None
    return values


class QuotaHistory:
    def __init__(self, monitor_path, source_path, profile_id):
        self.monitor_path = str(Path(monitor_path).expanduser().resolve())
        self.source_path = str(Path(source_path).expanduser().resolve())
        self.profile_id = profile_id
        if str(uuid.uuid5(uuid.NAMESPACE_URL, self.source_path)) != profile_id:
            raise ValueError("profile mismatch")

    @staticmethod
    def _open(path):
        db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        return db

    def reference_candidates(self, exclude_ids, now, limit=3):
        if not _number(now) or type(limit) is not int or limit <= 0:
            return []
        try:
            excluded = tuple(exclude_ids)
            exclusion_sql = (
                " AND session_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                if excluded
                else ""
            )
            with closing(self._open(self.monitor_path)) as db:
                rows = db.execute(
                    "SELECT session_id,model,started_at,ended_at,last_activity_at FROM session_catalog "
                    "WHERE profile_id=? AND parent_session_id IS NULL AND started_at < ? "
                    + exclusion_sql
                    + " ORDER BY COALESCE(last_activity_at,started_at) DESC LIMIT ?",
                    (self.profile_id, now - 21600, *excluded, min(limit, 3)),
                ).fetchall()
            return [
                {"sid": sid, "model": model, "started_at": started, "ended_at": ended}
                for sid, model, started, ended, _activity in rows
                if sid not in exclude_ids
                and isinstance(model, str)
                and model
                and _number(started)
                and (ended is None or _number(ended))
            ]
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return []

    def bucket_bracket(self, sid, at):
        if not _number(at):
            return None
        try:
            query = (
                "SELECT collected_at,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,source_ok "
                "FROM snapshots WHERE profile_id=? AND session_id=? AND collected_at {op} ? "
                "ORDER BY collected_at {order} LIMIT 1"
            )
            with closing(self._open(self.monitor_path)) as db:
                lo = db.execute(
                    query.format(op="<=", order="DESC"), (self.profile_id, sid, at)
                ).fetchone()
                hi = db.execute(
                    query.format(op=">=", order="ASC"), (self.profile_id, sid, at)
                ).fetchone()
                end = (
                    None
                    if hi is not None
                    else db.execute(
                        "SELECT ended_at FROM session_catalog WHERE profile_id=? AND session_id=?",
                        (self.profile_id, sid),
                    ).fetchone()
                )
            if lo is None:
                return None
            lower, upper = (
                _counts(
                    dict(
                        zip(
                            (
                                "input_tokens",
                                "cache_read_tokens",
                                "cache_write_tokens",
                                "output_tokens",
                            ),
                            lo[1:5],
                        )
                    )
                ),
                None,
            )
            if lo[5] != 1 or lower is None:
                return None
            if hi is None:
                if not end or not _number(end[0]) or end[0] > lo[0]:
                    return None
                upper = lower
            else:
                upper = _counts(
                    dict(
                        zip(
                            (
                                "input_tokens",
                                "cache_read_tokens",
                                "cache_write_tokens",
                                "output_tokens",
                            ),
                            hi[1:5],
                        )
                    )
                )
                if hi[5] != 1 or upper is None or any(a > b for a, b in zip(lower, upper)):
                    return None
            return lower, upper
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return None

    def window_usage(self, sid, started_at, current_buckets, window_start, now):
        current = (
            _counts(dict(zip(_BUCKETS, current_buckets)))
            if isinstance(current_buckets, (tuple, list)) and len(current_buckets) == 4
            else None
        )
        if (
            current is None
            or not all(_number(v) for v in (started_at, window_start, now))
            or started_at > now
            or window_start > now
        ):
            return None
        if started_at >= window_start and started_at <= now:
            base_lower = base_upper = (0, 0, 0, 0)
        else:
            bracket = self.bucket_bracket(sid, window_start)
            if bracket is None:
                return None
            base_lower, base_upper = bracket
        try:
            with closing(self._open(self.monitor_path)) as db:
                samples = db.execute(
                    "SELECT collected_at,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,source_ok "
                    "FROM snapshots WHERE profile_id=? AND session_id=? AND collected_at>=? AND collected_at<=? "
                    "ORDER BY collected_at",
                    (self.profile_id, sid, window_start, now),
                ).fetchall()
            prior = base_upper
            for row in samples:
                counts = _counts(dict(zip(_BUCKETS, row[1:5])))
                if row[5] != 1 or counts is None or any(a > b for a, b in zip(prior, counts)):
                    return None
                prior = counts
            if any(observed > current_value for observed, current_value in zip(prior, current)):
                return None
            low = tuple(c - b for c, b in zip(current, base_upper))
            high = tuple(c - b for c, b in zip(current, base_lower))
            if any(v < 0 for v in low + high):
                return None
            return low, high
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return None

    def single_model(self, sid, model):
        try:
            with closing(self._open(self.source_path)) as db:
                rows = db.execute(
                    "SELECT DISTINCT model FROM session_model_usage WHERE session_id=? "
                    "AND (input_tokens>0 OR cache_read_tokens>0 OR cache_write_tokens>0 OR output_tokens>0)",
                    (sid,),
                ).fetchall()
            return len(rows) == 1 and rows[0][0] == model
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return False
