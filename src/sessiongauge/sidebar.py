"""herdr sidebar metadata reporter; metadataは明示的に有効化された場合だけ送る。"""

import json
import math
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from sessiongauge.adapters.herdr import HerdrAdapter
from sessiongauge.quota import QuotaReader, TokenQuotaEstimator


def _format_tokens(tokens):
    """大きなtoken数だけ、整数演算で概算表示する。"""
    if tokens < 1000:
        return str(tokens)
    unit = 1000
    suffix = "K"
    for next_suffix in ("M", "B", "T"):
        if tokens < unit * 1000:
            break
        unit *= 1000
        suffix = next_suffix
    hundredths = (tokens * 100 + unit // 2) // unit
    if hundredths >= 100000 and suffix != "T":
        unit *= 1000
        suffix = {"K": "M", "M": "B", "B": "T"}[suffix]
        hundredths = (tokens * 100 + unit // 2) // unit
    whole, fraction = divmod(hundredths, 100)
    decimal = f".{fraction:02d}".rstrip("0") if fraction else ""
    return f"{whole}{decimal}{suffix}"


class SidebarReporter:
    def __init__(
        self,
        *,
        enabled=False,
        ttl_ms=15000,
        runner=None,
        adapter=None,
        source=None,
        warning=None,
        quota_enabled=False,
        quota_reader=None,
        hermes_home="~/.hermes",
        display="percent",
        quota_mode="api",
        estimate_max_age_seconds=86400,
        monotonic_clock=None,
        wall_clock=None,
        window_estimator=None,
    ):
        if display not in ("percent", "percent_tokens"):
            raise ValueError("display must be 'percent' or 'percent_tokens'")
        self.display = display
        if quota_mode not in ("estimate", "api"):
            raise ValueError("quota_mode must be 'estimate' or 'api'")
        self.quota_mode = quota_mode
        self.window_estimator = window_estimator
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self.estimator = TokenQuotaEstimator(
            estimate_max_age_seconds, self._monotonic_clock, self._wall_clock
        )
        self._estimators = {}
        self._estimate_max_age_seconds = estimate_max_age_seconds
        self._quota_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="quota-metadata"
        )
        self._quota_future = None
        self._quota_future_context = None
        self._estimate_scopes = {}
        self._estimate_generations = {}
        self._closed = False
        self._last_quota_submit = None
        self.enabled = enabled
        self.ttl_ms = ttl_ms
        self.runner = runner or subprocess.run
        self.adapter = adapter or HerdrAdapter()
        self.source = source or f"sessiongauge:{uuid.uuid4()}"
        self.warning = warning or (lambda message: print(f"Warning: {message}", file=sys.stderr))
        self._capability = None
        self._unsupported_warned = False
        self._quota_accuracy_warned = False
        self._owned = {}
        self._last_seq = 0
        self._counter = 0
        self.quota_enabled = quota_enabled
        self.quota_reader = quota_reader or QuotaReader(
            hermes_home=str(Path(hermes_home).expanduser())
        )
        self.quota_profile_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                str((Path(hermes_home).expanduser() / "state.db").resolve(strict=False)),
            )
        )

    def _run(self, args):
        return self.runner(args, capture_output=True, text=True, timeout=5, check=False)

    def _supports(self):
        if self._capability is not None:
            return self._capability
        try:
            help_result = self._run(["herdr", "pane", "report-metadata", "--help"])
            schema_result = self._run(["herdr", "api", "schema", "--json"])
            schema = json.loads(schema_result.stdout)
            if "schemas" in schema:
                params = schema["schemas"]["request"]["$defs"]["PaneReportMetadataParams"]
                tokens = params["properties"]["tokens"]
                seq = params["properties"]["seq"]
                ttl = params["properties"]["ttl_ms"]
                success = schema["schemas"]["success_response"]["$defs"]["ResponseResult"]
            else:
                params = schema["request_params"]
                tokens = params["properties"]["tokens"]
                seq = params["properties"]["seq"]
                ttl = params["properties"]["ttl_ms"]
                success = schema["allowed_success_shape"]
            variants = success if isinstance(success, list) else success.get("oneOf", [])
            has_ok = any(
                item.get("properties", {}).get("type", {}).get("const") == "ok"
                for item in variants
                if isinstance(item, dict)
            )
            help_text = (help_result.stdout or "") + (help_result.stderr or "")
            self._capability = bool(
                help_result.returncode == 0
                and schema_result.returncode == 0
                and "--source" in help_text
                and "--token" in help_text
                and "--seq" in help_text
                and "--ttl-ms" in help_text
                and tokens.get("type") == "object"
                and tokens.get("maxProperties", 16) >= 1
                and seq.get("format") == "uint64"
                and ttl.get("minimum") == 1
                and ttl.get("maximum") == 86400000
                and has_ok
            )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            self._capability = False
        if not self._capability and not self._unsupported_warned:
            self.warning("herdr metadata reporting is unsupported; sidebar reporting disabled")
            self._unsupported_warned = True
        return self._capability

    def _sequence(self):
        self._counter += 1
        value = max(time.time_ns(), self._last_seq + 1, self._counter)
        if value > 18446744073709551615:
            value = self._last_seq + 1
        self._last_seq = value
        return value

    def _send(self, pane_id, *, token=None, clear=False):
        if not self._supports():
            return False
        args = [
            "herdr",
            "pane",
            "report-metadata",
            pane_id,
            "--source",
            self.source,
            "--seq",
            str(self._sequence()),
        ]
        if clear:
            args.extend(["--clear-token", "sessiongauge_usage"])
        else:
            args.extend(["--token", f"sessiongauge_usage={token}", "--ttl-ms", str(self.ttl_ms)])
        try:
            result = self._run(args)
            if result.returncode != 0:
                ok = False
            elif not result.stdout.strip():
                ok = True
            else:
                payload = json.loads(result.stdout)
                ok = (
                    isinstance(payload, dict)
                    and isinstance(payload.get("result"), dict)
                    and payload["result"].get("type") == "ok"
                )
            if not ok:
                self.warning("herdr metadata request failed; sidebar value will expire")
            return ok
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            self.warning("herdr metadata request failed; sidebar value will expire")
            return False

    def publish(self, data, observed_panes=None):
        if self._closed:
            return
        if not self.enabled:
            return
        if data.get("stale"):
            self._sync_estimate_scopes({})
            return
        if not self._supports():
            self._sync_estimate_scopes({})
            return
        panes = data.get("panes", [])
        counts = {}
        blocked = {row.get("pane_id") for row in panes if row.get("status") == "conflict"}
        for row in panes:
            session = row.get("session") or {}
            if row.get("status") == "matched" and session.get("id"):
                counts[session["id"]] = counts.get(session["id"], 0) + 1
        blocked.update(
            row.get("pane_id")
            for row in panes
            if row.get("status") == "matched"
            and counts.get((row.get("session") or {}).get("id")) != 1
        )
        live = {}
        quota_targets = set()
        try:
            current = self.adapter.list_panes()
        except (OSError, ValueError, subprocess.SubprocessError, TypeError):
            self._sync_estimate_scopes({})
            return
        for row in panes:
            session = row.get("session") or {}
            sid, pane_id, terminal_id = (
                session.get("id"),
                row.get("pane_id"),
                row.get("terminal_id"),
            )
            if row.get("status") != "matched" or not sid or counts.get(sid) != 1:
                continue
            if type(session.get("total_tokens")) is not int or session["total_tokens"] < 0:
                continue
            expected = [
                p
                for p in (observed_panes or current)
                if p.pane_id == pane_id
                and p.terminal_id == terminal_id
                and p.agent == "hermes"
                and p.session_id == sid
            ]
            pane = [
                p
                for p in current
                if p.pane_id == pane_id
                and p.terminal_id == terminal_id
                and p.agent == "hermes"
                and p.session_id == sid
                and (not observed_panes or any(p.endpoint == old.endpoint for old in expected))
            ]
            if len(pane) == 1:
                key = (pane[0].endpoint, pane_id, terminal_id)
                live[key] = (sid, session["total_tokens"])
                if self.quota_enabled and row.get("profile") == self.quota_profile_id:
                    quota_targets.add(key)
        quota = {}
        sessions_by_key = {}
        for row in panes:
            session = row.get("session") or {}
            sid = session.get("id")
            if (
                row.get("status") == "matched"
                and sid
                and row.get("profile") == self.quota_profile_id
            ):
                key = next((k for k, value in live.items() if value[0] == sid), None)
                if key in quota_targets:
                    sessions_by_key[key] = session
        desired_scopes = {}
        for key in quota_targets:
            sid, tokens = live[key]
            session = sessions_by_key.get(key, {})
            model = session.get("model")
            if isinstance(model, str) and model.strip():
                desired_scopes[key] = (
                    (self.quota_profile_id, sid, model),
                    tokens,
                )
        if self.quota_mode == "estimate":
            self._sync_estimate_scopes(desired_scopes)
            if self.quota_enabled and quota_targets and not self._quota_accuracy_warned:
                self.warning(
                    (
                        "Ranges assume stable same-model bucket rates and provider cutoff; they are not empirical accuracy guarantees. "
                        if self.window_estimator is not None
                        else f"{self.estimator.unavailable_reason}; estimate mode remains N/A. "
                    )
                    + "percent_tokens shows local counts; api mode shows the provider's raw task budget, not current-window accuracy."
                )
                self._quota_accuracy_warned = True
        if self.quota_enabled and quota_targets:
            threads = []
            for key in sorted(quota_targets, key=lambda item: str(item)):
                session = sessions_by_key.get(key, {})
                started = session.get("started_at")
                created_at = None
                if isinstance(started, str):
                    try:
                        parsed = datetime.fromisoformat(started)
                        if parsed.tzinfo is not None:
                            created_at = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
                    except ValueError:
                        pass
                if isinstance(started, (int, float)) and not isinstance(started, bool):
                    try:
                        created_at = (
                            datetime.fromtimestamp(started, UTC).isoformat().replace("+00:00", "Z")
                        )
                    except (OverflowError, OSError, ValueError):
                        pass
                threads.append(
                    {
                        "thread_id": live[key][0],
                        "created_at": created_at,
                        "descendant_thread_ids": [],
                    }
                )
            if self.quota_mode == "api":
                try:
                    if len(threads) <= 100:
                        quota = self.quota_reader.read_sessions(threads)
                    if not isinstance(quota, dict):
                        quota = {}
                except (OSError, subprocess.SubprocessError, ValueError, TypeError, OverflowError):
                    quota = {}
            elif len(threads) <= 100 and not self._closed:
                if self.window_estimator is not None:
                    try:
                        targets = {
                            live[k][0]: sessions_by_key[k]
                            for k in quota_targets
                            if k in sessions_by_key
                        }
                        quota = self.window_estimator.estimates(targets)
                    except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError):
                        quota = {}
                else:
                    self._collect_estimates(threads, sessions_by_key, live, quota_targets)
                    quota = {
                        sid: self._estimator_for(
                            (
                                self.quota_profile_id,
                                sid,
                                (sessions_by_key.get(key) or {}).get("model"),
                            )
                        ).estimate(
                            profile_id=self.quota_profile_id,
                            session_id=sid,
                            model=(sessions_by_key.get(key) or {}).get("model"),
                            total_tokens=tokens,
                        )
                        for key, (sid, tokens) in live.items()
                        if key in quota_targets
                    }
        for key in self._owned.keys() - live.keys():
            if key[1] in blocked:
                continue
            target_exists = any(
                p.endpoint == key[0] and p.pane_id == key[1] and p.agent == "hermes"
                for p in current
            )
            if target_exists:
                self._send(key[1], clear=True)
            self._owned.pop(key, None)
        for key, (sid, tokens) in live.items():
            if key in self._owned and self._owned[key] != sid:
                self._send(key[1], clear=True)
            token = _format_tokens(tokens)
            if self.display == "percent":
                value = quota.get(sid) if key in quota_targets else None
                token = (
                    (self._format_window(value) or "N/A")
                    if self.window_estimator is not None and self.quota_mode == "estimate"
                    else (
                        "API " + f"{value:.2f}".rstrip("0").rstrip(".") + "%"
                        if self._valid_display_percent(value)
                        else "N/A"
                    )
                )
            elif key in quota_targets:
                value = quota.get(sid)
                if self.quota_mode == "api" and self._valid_display_percent(value):
                    percent = f"{value:.2f}".rstrip("0").rstrip(".")
                    token = f"API {percent}% · {token}"
                elif self.window_estimator is not None and self.quota_mode == "estimate":
                    formatted = self._format_window(value)
                    token = f"{formatted if formatted else 'N/A'} · {token}"
                else:
                    token = f"{'N/A' if self.quota_mode == 'estimate' else 'API N/A'} · {token}"
            if self._send(key[1], token=token):
                self._owned[key] = sid

    def _clear_all(self):
        for key in list(self._owned):
            self._send(key[1], clear=True)
            self._owned.pop(key, None)

    @staticmethod
    def _valid_display_percent(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False

    def _sync_estimate_scopes(self, desired):
        """各paneの有効scopeを更新し、変化した世代の校正を破棄する。"""
        previous = self._estimate_scopes
        for key in previous.keys() | desired.keys():
            old = previous.get(key)
            new = desired.get(key)
            if old is not None and new is not None and old[0] == new[0] and new[1] >= old[1]:
                previous[key] = new
                continue
            self._estimate_generations[key] = self._estimate_generations.get(key, 0) + 1
            if old is not None:
                profile, sid, model = old[0]
                self._estimator_for((profile, sid, model)).invalidate(
                    profile_id=profile, session_id=sid, model=model
                )
        self._estimate_scopes = dict(desired)

    def _estimator_for(self, scope):
        if scope not in self._estimators:
            self._estimators[scope] = TokenQuotaEstimator(
                self._estimate_max_age_seconds, self._monotonic_clock, self._wall_clock
            )
        return self._estimators[scope]

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.window_estimator is not None:
            self.window_estimator.close()
        self._quota_executor.shutdown(wait=False, cancel_futures=True)
        if self.enabled:
            self._clear_all()

    @staticmethod
    def _format_window(value):
        from sessiongauge.window_quota import QuotaEstimate

        if not isinstance(value, QuotaEstimate) or not SidebarReporter._valid_display_percent(
            value.lower
        ):
            return None
        if value.lower == 0 and value.upper is None:
            return None
        try:
            lower = math.floor(value.lower * 10) / 10
        except (OverflowError, ValueError):
            return None
        if value.upper is None:
            return f"~≥{lower:.1f}%"
        if not SidebarReporter._valid_display_percent(value.upper) or value.upper < value.lower:
            return None
        if value.lower == value.upper:
            return f"~{value.lower:.1f}%"
        try:
            upper = math.ceil(value.upper * 10) / 10
        except (OverflowError, ValueError):
            return None
        return f"~{lower:.1f}–{upper:.1f}%"

    def _collect_estimates(self, threads, sessions_by_key, live, quota_targets):
        if self._quota_future is not None and self._quota_future.done():
            try:
                rows = self._quota_future.result()
                for key, ctx in self._quota_future_context.items():
                    current = live.get(key)
                    session_now = sessions_by_key.get(key, {})
                    if (
                        current is None
                        or current[0] != ctx["sid"]
                        or session_now.get("model") != ctx["model"]
                        or key not in quota_targets
                        or (
                            "generation" in ctx
                            and self._estimate_generations.get(key, 0) != ctx["generation"]
                        )
                    ):
                        continue
                    row = rows.get(ctx["sid"], {}) if isinstance(rows, dict) else {}
                    estimator = self._estimator_for((ctx["profile"], ctx["sid"], ctx["model"]))
                    estimator.calibrate(
                        profile_id=ctx["profile"],
                        session_id=ctx["sid"],
                        model=ctx["model"],
                        total_tokens=ctx["tokens"],
                        percent=row.get("percent"),
                        data_as_of=row.get("data_as_of"),
                    )
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
            self._quota_future = None
            self._quota_future_context = None
        now = self._monotonic_clock()
        interval = getattr(self.quota_reader, "poll_interval_seconds", 60)
        if (
            self._quota_future is None
            and not self._closed
            and (self._last_quota_submit is None or now - self._last_quota_submit >= interval)
        ):
            contexts = {}
            for key in quota_targets:
                sid, tokens = live[key]
                session = sessions_by_key.get(key, {})
                contexts[key] = {
                    "sid": sid,
                    "tokens": tokens,
                    "model": session.get("model"),
                    "profile": self.quota_profile_id,
                    "generation": self._estimate_generations.get(key, 0),
                }
            if contexts and hasattr(self.quota_reader, "read_session_metadata"):
                self._quota_future_context = contexts
                self._last_quota_submit = now
                self._quota_future = self._quota_executor.submit(
                    self.quota_reader.read_session_metadata, threads
                )
