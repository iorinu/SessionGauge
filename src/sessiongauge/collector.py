import os
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sessiongauge.adapters.herdr import HerdrAdapter
from sessiongauge.adapters.hermes_db import HermesDB
from sessiongauge.config import profile_name_for_path
from sessiongauge.identity import match_panes
from sessiongauge.persistence import MonitorDB


@dataclass
class PollState:
    last_success: object = None
    stale: bool = False
    failures: int = 0
    next_retry_at: float = 0

    def poll(self, operation, now=None):
        now = time.time() if now is None else now
        if now < self.next_retry_at:
            return self.last_success, {
                "error": "backoff",
                "retry_after_seconds": self.next_retry_at - now,
                "stale": self.stale,
            }
        try:
            value = operation()
        except (OSError, sqlite3.Error, ValueError, TypeError, subprocess.SubprocessError) as exc:
            self.failures += 1
            self.stale = self.last_success is not None
            delay = min(60, 2 ** min(self.failures, 6))
            self.next_retry_at = now + delay
            return self.last_success, {
                "error": type(exc).__name__,
                "retry_after_seconds": delay,
                "stale": self.stale,
            }
        self.last_success, self.stale, self.failures = value, False, 0
        self.next_retry_at = 0
        return value, None


def poll_forever(operation, interval: int):
    state = PollState()
    while True:
        result = state.poll(operation)
        yield result
        delay = result[1]["retry_after_seconds"] if result[1] else interval
        time.sleep(delay)


class Collector:
    """herdrとHermesを独立して取得し、成功した観測だけを永続化する。"""

    def __init__(self, config, monitor=None, adapter=None):
        self.config = config
        self.monitor = monitor
        self.adapter = adapter or HerdrAdapter()
        self.run_id = str(uuid.uuid4())
        self.herdr_state = PollState()
        self.source_states = {str(path): PollState() for path in config.hermes_db_paths}
        self.source_gap = {str(path): False for path in config.hermes_db_paths}
        self.source_first_poll = set(self.source_states)
        self._owned_monitor = monitor is None

    def collect(self, now=None):
        now = time.time() if now is None else now
        pane_value, pane_error = self.herdr_state.poll(self.adapter.list_panes, now)
        if pane_error:
            for key in self.source_gap:
                self.source_gap[key] = True
        panes = pane_value or []
        sessions, profiles, schema = [], {}, {}
        profile_paths = {}
        for path in self.config.hermes_db_paths:
            key = str(path)
            state = self.source_states[key]
            if now < state.next_retry_at:
                if state.last_success is not None:
                    sessions.extend((path, row) for row in state.last_success)
                continue

            def read(path=path):
                if hasattr(self.adapter, "sessions"):
                    try:
                        return self.adapter.sessions(path)
                    except TypeError:
                        return self.adapter.sessions()
                with HermesDB(path) as db:
                    return db.sessions(), db.schema_version

            try:
                value = read()
                rows, version = (
                    value
                    if isinstance(value, tuple)
                    and len(value) == 2
                    and isinstance(value[1], (int, type(None)))
                    else (value, None)
                )
                state.poll(lambda rows=rows: rows, now)
                state.last_success = rows
                sessions.extend((path, row) for row in rows)
                schema[key] = version
            except (OSError, sqlite3.Error, ValueError, TypeError, subprocess_error_types()):
                state.failures += 1
                state.stale = state.last_success is not None
                delay = min(60, 2 ** min(state.failures, 6))
                state.next_retry_at = now + delay
                self.source_gap[key] = True
                if state.last_success is not None:
                    sessions.extend((path, row) for row in state.last_success)
        monitor = self.monitor
        if monitor is None:
            monitor = MonitorDB(
                self.config.data_dir / "monitor.db", source_paths=self.config.hermes_db_paths
            )
            monitor.__enter__()
            self.monitor = monitor
        output, all_sessions = [], []
        monitor.record_sync_state(
            "herdr",
            now=now,
            success=not bool(pane_error),
            failures=self.herdr_state.failures,
            warning=(pane_error or {}).get("error"),
        )
        for path in self.config.hermes_db_paths:
            records = [(p, row) for p, row in sessions if p == path]
            source_state = self.source_states[str(path)]
            monitor.record_sync_state(
                str(path.resolve(strict=False)),
                now=now,
                success=not source_state.stale and source_state.last_success is not None,
                failures=source_state.failures,
                warning=(
                    f"schema_version={schema[str(path)]}"
                    if str(path) in schema and schema[str(path)] not in (None, 1, 2)
                    else None
                ),
            )
            if not records:
                continue
            hermes_root = (
                Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
                .expanduser()
                .resolve(strict=False)
            )
            profile_name = profile_name_for_path(path, hermes_root)
            profile_id = monitor.register_profile(profile_name, path)
            profile_paths[profile_id] = path
            profiles[profile_id] = {row.id for _, row in records}
        matched = match_panes(panes, profiles)
        fallback_tasks = {}
        # 補完照合は Hermes pane の同一 generation に限り、DB上の確認済みリンクを使う。
        from dataclasses import replace

        fallback_candidates = []
        for index, item in enumerate(matched):
            if item.status != "unlinked" or item.pane.agent != "hermes" or item.pane.session_id:
                continue
            prior = monitor.connection.execute(
                "SELECT profile_id,session_id,task_id,source,confidence FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? ORDER BY valid_from DESC LIMIT 1",
                item.pane.generation_key,
            ).fetchone()
            if prior and prior[0] in profiles and prior[1] in profiles[prior[0]]:
                fallback_candidates.append((index, item, prior))
        candidate_counts = {}
        for _, _, prior in fallback_candidates:
            identity = (prior[0], prior[1])
            candidate_counts[identity] = candidate_counts.get(identity, 0) + 1
        for index, item, prior in fallback_candidates:
            if candidate_counts[(prior[0], prior[1])] != 1:
                continue
            matched[index] = replace(item, profile=prior[0], session_id=prior[1], status="matched")
            fallback_tasks[(prior[0], prior[1])] = prior[2]
        # 公式照合と手動 fallback の両方を集めた後、同一複合IDの重複を解消する。
        composite_counts = {}
        for item in matched:
            if item.status == "matched":
                identity = (item.profile, item.session_id)
                composite_counts[identity] = composite_counts.get(identity, 0) + 1
        for index, item in enumerate(matched):
            if item.status == "matched" and composite_counts[(item.profile, item.session_id)] > 1:
                matched[index] = replace(item, profile=None, session_id=None, status="conflict")
        # 汎用的なHermes端末名だけ、確認済みSessionのタイトルで表示名を補う。
        sessions_by_identity = {
            (profile_id, row.id): row
            for profile_id, path in profile_paths.items()
            for source_path, row in sessions
            if source_path == path
        }
        for index, item in enumerate(matched):
            row = sessions_by_identity.get((item.profile, item.session_id))
            if (
                item.status == "matched"
                and item.pane.agent == "hermes"
                and (item.pane.title or "").strip().casefold()
                in {"hermes", "hermes agent", "hermes cli"}
                and row
                and row.title
            ):
                matched[index] = replace(item, pane=replace(item.pane, title=row.title))
        if not pane_error:
            effective_panes = {item.pane.generation_key: item.pane for item in matched}
            monitor.record_panes(
                [effective_panes.get(pane.generation_key, pane) for pane in panes], now
            )
        match_rows = []
        by_profile = profile_paths
        for item in matched:
            match_rows.append((item.pane, item.profile, item.session_id, item.status))
        for path in self.config.hermes_db_paths:
            records = [(p, row) for p, row in sessions if p == path]
            if not records:
                continue
            profile_id = next(pid for pid, p in by_profile.items() if p == path)
            if self.source_states[str(path)].stale:
                # 失敗時は表示用の最終値を返すが、古い行を新しい観測として保存しない。
                all_sessions.extend(row for _, row in records)
                continue
            row_owners = {}
            for item in matched:
                if item.status == "matched" and item.profile == profile_id:
                    active = monitor.connection.execute(
                        "SELECT task_id,profile_id,session_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                        item.pane.generation_key,
                    ).fetchone()
                    row_owners[item.session_id] = (
                        active[0]
                        if active and active[1:] == (item.profile, item.session_id)
                        else str(uuid.uuid4())
                    )
                    if not active or active[1:] != (item.profile, item.session_id):
                        child = next(
                            (r for p, r in sessions if p == path and r.id == item.session_id), None
                        )
                        if child and child.parent_session_id:
                            parent = next(
                                (
                                    r
                                    for p, r in sessions
                                    if p == path and r.id == child.parent_session_id
                                ),
                                None,
                            )
                            prior = monitor.connection.execute(
                                "SELECT end_reason,source FROM session_catalog WHERE profile_id=? AND session_id=?",
                                (item.profile, child.parent_session_id),
                            ).fetchone()
                            end_reason = (parent.end_reason if parent else None) or (
                                prior[0] if prior else None
                            )
                            source = parent.source if parent else child.source
                            if (
                                end_reason
                                and "compress" in end_reason.lower()
                                and prior
                                and prior[1] == source == child.source
                                and "subagent" not in (child.source or "").lower()
                            ):
                                continuation = monitor.connection.execute(
                                    "SELECT task_id FROM pane_session_links WHERE profile_id=? AND session_id=? ORDER BY valid_from DESC LIMIT 1",
                                    (item.profile, child.parent_session_id),
                                ).fetchone()
                                if continuation:
                                    row_owners[item.session_id] = continuation[0]
                    if (item.profile, item.session_id) in fallback_tasks:
                        row_owners[item.session_id] = fallback_tasks[
                            (item.profile, item.session_id)
                        ]
            monitor.record_poll(
                profile_id=profile_id,
                sessions=[r for _, r in records],
                panes=[],
                matches={**row_owners, "_pane_matches": match_rows},
                collected_at=now,
                schema_version=schema.get(str(path)),
                source_ok=not self.source_states[str(path)].stale
                and not pane_error
                and not self.source_gap[str(path)],
                run_id=self.run_id,
            )
            self.source_first_poll.discard(str(path))
            if not self.source_states[str(path)].stale and not pane_error:
                self.source_gap[str(path)] = False
            all_sessions.extend(row for _, row in records)
        for item in matched:
            row = next(
                (
                    r
                    for p, r in sessions
                    if item.profile
                    and profile_paths.get(item.profile) == p
                    and r.id == item.session_id
                ),
                None,
            )
            active = (
                monitor.connection.execute(
                    "SELECT task_id,profile_id,session_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                    item.pane.generation_key,
                ).fetchone()
                if item.status == "matched"
                else None
            )
            cumulative = (
                _cumulative(monitor, item.profile, item.session_id)
                if item.status == "matched"
                else None
            )
            observed = (
                _latest_delta(monitor, item.profile, item.session_id)
                if item.status == "matched"
                else None
            )
            recent = (
                _recent_delta(monitor, item.profile, item.session_id, now)
                if item.status == "matched"
                else None
            )
            active_task = (
                active[0]
                if active
                and item.status == "matched"
                and active[1:] == (item.profile, item.session_id)
                else None
            )
            output.append(
                {
                    "pane_id": item.pane.pane_id,
                    "terminal_id": item.pane.terminal_id,
                    "status": item.status,
                    "task_id": active_task
                    or (row_owners.get(item.session_id) if item.status == "matched" else None),
                    "session": row.as_dict() if row else None,
                    "title": item.pane.title,
                    "cwd": item.pane.cwd,
                    "profile": item.profile,
                    "state": item.pane.agent_status,
                    "lineage": _lineage(monitor, item.profile, item.session_id)
                    if item.status == "matched"
                    else [],
                    "observed": observed,
                    "cumulative": cumulative,
                    "recent_60s": recent,
                }
            )
        if not pane_error:
            monitor.close_missing_panes(panes, now)
        source_failed = any(s.failures for s in self.source_states.values())
        return {
            "panes": output,
            "sessions": [row.as_dict() for row in all_sessions],
            "collected_at": now,
            "stale": bool(
                (pane_error and self.herdr_state.last_success is not None)
                or any(s.stale for s in self.source_states.values())
            ),
            "status": "error"
            if (pane_error and self.herdr_state.last_success is None)
            or (
                source_failed
                and not any(s.last_success is not None for s in self.source_states.values())
            )
            else "stale"
            if pane_error or source_failed
            else "healthy",
            "errors": {
                "herdr": pane_error,
                "sources": {
                    k: {"stale": v.stale, "failures": v.failures}
                    for k, v in self.source_states.items()
                },
            },
        }

    def close(self):
        if self._owned_monitor and self.monitor is not None:
            self.monitor.__exit__(None, None, None)
            self.monitor = None


def subprocess_error_types():
    import subprocess

    return subprocess.SubprocessError


def _latest_delta(monitor, profile, session_id):
    if not profile or not session_id:
        return None
    row = monitor.connection.execute(
        "SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd FROM usage_attributions WHERE profile_id=? AND session_id=? ORDER BY interval_end DESC LIMIT 1",
        (profile, session_id),
    ).fetchone()
    if row is None:
        return None
    return dict(
        zip(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "estimated_cost_usd",
                "actual_cost_usd",
            ),
            row,
        )
    )


def _lineage(monitor, profile, session_id):
    if not profile or not session_id:
        return []
    parents = {
        sid: parent
        for sid, parent in monitor.connection.execute(
            "SELECT session_id,parent_session_id FROM session_catalog WHERE profile_id=?",
            (profile,),
        )
    }
    from sessiongauge.identity import lineage

    return list(lineage(session_id, parents))


def _cumulative(monitor, profile, session_id):
    row = monitor.connection.execute(
        "SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd FROM snapshots WHERE profile_id=? AND session_id=? ORDER BY collected_at DESC LIMIT 1",
        (profile, session_id),
    ).fetchone()
    return (
        dict(
            zip(
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "estimated_cost_usd",
                    "actual_cost_usd",
                ),
                row,
            )
        )
        if row
        else None
    )


def _recent_delta(monitor, profile, session_id, now):
    row = monitor.connection.execute(
        "SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd FROM usage_attributions WHERE profile_id=? AND session_id=? AND interval_end>=? ORDER BY interval_end DESC LIMIT 1",
        (profile, session_id, now - 60),
    ).fetchone()
    return (
        dict(
            zip(
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "estimated_cost_usd",
                    "actual_cost_usd",
                ),
                row,
            )
        )
        if row
        else None
    )
