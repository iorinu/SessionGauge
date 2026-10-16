"""同一profileの履歴と5時間windowから条件付きquota範囲を求める。"""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sessiongauge.window_quota import QuotaEstimate, estimate_bounds


class WindowQuotaEstimator:
    def __init__(
        self, history, reader, max_age_seconds=86400, clock=None, wall_clock=None, executor=None
    ):
        self.history = history
        self.reader = reader
        self.max_age_seconds = max_age_seconds
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="window-quota"
        )
        self._owns_executor = executor is None
        self._future = None
        self._last_submit = None
        self._context = None
        self._context_fetched = None
        self._closed = False
        self.diagnostics = {}

    @staticmethod
    def _started(value):
        try:
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value)
                return parsed.astimezone(UTC).timestamp() if parsed.tzinfo else None
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                return float(value)
        except (OverflowError, OSError, ValueError):
            pass
        return None

    @staticmethod
    def _buckets(session):
        keys = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
        counters = session.get("counters")
        if counters is not None and not isinstance(counters, dict):
            return None
        values = tuple(session.get(k, counters.get(k) if counters else None) for k in keys)
        if any(type(v) is not int or v < 0 or v.bit_length() > 1023 for v in values):
            return None
        return values

    def _refresh(self, threads, ids, now, current_refs):
        try:
            candidates = self.history.reference_candidates(ids, now, limit=min(3, 100 - len(ids)))
            refs = list(current_refs)
            for item in candidates:
                sid, model, started = item.get("sid"), item.get("model"), item.get("started_at")
                if not isinstance(sid, str) or sid in ids or sid in {r[0] for r in refs}:
                    continue
                stamp = self._started(started)
                refs.append((sid, model, stamp))
                threads.append(
                    {
                        "thread_id": sid,
                        "created_at": datetime.fromtimestamp(stamp, UTC)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if stamp is not None
                        else None,
                        "descendant_thread_ids": [],
                    }
                )
            context = self.reader.read_estimate_context(threads)
            return context, refs, self.wall_clock()
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError):
            return {}, [], self.wall_clock()

    def estimates(self, sessions):
        if self._closed:
            return {}
        self.diagnostics.clear()
        now, mono = self.wall_clock(), self.clock()
        if self._future is not None and self._future.done():
            try:
                context, refs, self._context_fetched = self._future.result()
                self._context = (context, refs, self._context_fetched)
            except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError):
                self._context = None
                self._context_fetched = None
            self._future = None
        result = {}
        context = self._context
        if context:
            try:
                payload, refs, _ = context
                start, end = payload["window_start"], payload["window_end"]
                fetched = max(35, 2 * getattr(self.reader, "poll_interval_seconds", 60))
                valid_window = (
                    start <= now < end
                    and self._context_fetched is not None
                    and 0 <= now - self._context_fetched <= fetched
                )
                for sid, session in sessions.items():
                    diag = {
                        "status": "unavailable",
                        "reason": "no_reference",
                        "reference_count": 0,
                        "reference_age_seconds": None,
                        "empirical_error_percent": None,
                    }
                    model, started, buckets = (
                        session.get("model"),
                        self._started(session.get("started_at")),
                        self._buckets(session),
                    )
                    current = []
                    if (
                        valid_window
                        and started is not None
                        and started <= now
                        and buckets is not None
                        and self.history.single_model(sid, model)
                    ):
                        usage = self.history.window_usage(sid, started, buckets, start, now)
                        if usage:
                            for ref_id, ref_model, ref_start in refs:
                                sample = payload.get("samples", {}).get(ref_id)
                                if ref_model != model or not sample:
                                    continue
                                age = now - sample.get("data_as_of", now + 1)
                                if ref_start is None or sample.get("data_as_of", 0) < ref_start:
                                    continue
                                bracket = self.history.bucket_bracket(
                                    ref_id, sample.get("data_as_of")
                                )
                                if (
                                    age < 0
                                    or age > self.max_age_seconds
                                    or bracket is None
                                    or not self.history.single_model(ref_id, model)
                                ):
                                    continue
                                estimate = estimate_bounds(sample.get("percent"), *bracket, *usage)
                                if estimate is not None and not (
                                    estimate.lower == 0 and estimate.upper is None
                                ):
                                    current.append((estimate, age))
                    if current:
                        low = max(v.lower for v, _ in current)
                        highs = [v.upper for v, _ in current]
                        upper = min((v for v in highs if v is not None), default=None)
                        if upper is None or low <= upper:
                            result[sid] = QuotaEstimate(low, upper)
                            diag.update(
                                status="available",
                                reason="ok",
                                reference_count=len(current),
                                reference_age_seconds=min(a for _, a in current),
                            )
                        else:
                            diag["reason"] = "inconsistent_references"
                    self.diagnostics[sid] = diag
            except (KeyError, TypeError, ValueError, OverflowError):
                self._context = None
        else:
            self.diagnostics.clear()
            for sid in sessions:
                self.diagnostics[sid] = {
                    "status": "unavailable",
                    "reason": "refresh_pending",
                    "reference_count": 0,
                    "reference_age_seconds": None,
                    "empirical_error_percent": None,
                }
        if (
            self._future is None
            and not self._closed
            and sessions
            and len(sessions) <= 100
            and (
                self._last_submit is None
                or mono - self._last_submit >= getattr(self.reader, "poll_interval_seconds", 60)
            )
        ):
            threads = []
            current_refs = []
            for sid, session in sessions.items():
                stamp = self._started(session.get("started_at"))
                current_refs.append((sid, session.get("model"), stamp))
                threads.append(
                    {
                        "thread_id": sid,
                        "created_at": (
                            datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")
                            if stamp is not None
                            else None
                        ),
                        "descendant_thread_ids": [],
                    }
                )
            ids = [t["thread_id"] for t in threads]
            if threads and len(ids) <= 100:
                self._last_submit = mono
                self._future = self.executor.submit(
                    self._refresh, threads, ids, now, tuple(current_refs)
                )
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._context = None
        self._context_fetched = None
        if self._future is not None:
            self._future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
