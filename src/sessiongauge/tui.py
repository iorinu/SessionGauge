"""標準ライブラリだけで動くライブTUI。描画・状態更新は端末非依存。"""

import curses
import math
import queue
import sqlite3
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TUIState:
    selected: int = 0
    sort_by: str = "activity"
    filter_by: str = "active"
    details: bool = False
    help: bool = False
    profile_index: int = -1
    refresh_requested: bool = False
    quit_requested: bool = False


def _clean(value):
    text = "" if value is None else str(value)
    return "".join(
        ch if ch.isprintable() and unicodedata.category(ch) != "Cf" else " " for ch in text
    )


def _width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "FWA" else 1 for ch in text)


def _clip(text, width):
    text, out, used = _clean(text), "", 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in "FWA" else 1
        if used + size > width:
            break
        out += char
        used += size
    return out + " " * max(0, width - used)


def _compact_number(value, width=None, *, currency=False):
    """桁を隠さず、幅内に単位付き概数を表示する。"""
    if value is None:
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "N/A"
    if not math.isfinite(number):
        return "N/A"
    limit = 32 if width is None else max(0, width)
    prefix = "$" if currency else ""
    if abs(number) < 1000:
        plain = f"{prefix}{number:.3f}" if currency else str(value)
        if len(plain) <= limit:
            return plain

    suffixes = ("k", "M", "G", "T", "P", "E")
    scale = 0
    scaled = number
    while abs(scaled) >= 1000 and scale < len(suffixes):
        scaled /= 1000
        scale += 1
    while True:
        suffix = suffixes[scale - 1] if scale else ""
        marker = "~" if scale else ""
        room = limit - len(prefix + suffix + marker)
        if room < 1:
            return "N/A" if limit >= 3 else ""
        sign = 1 if scaled < 0 else 0
        integer_width = len(str(int(abs(scaled)))) if abs(scaled) >= 1 else 1
        precision = max(0, min(2, int(room) - sign - integer_width - 1))
        rendered = f"{prefix}{scaled:.{precision}f}{suffix}{marker}"
        rounded = abs(float(f"{scaled:.{precision}f}"))
        if rounded >= 1000 and scale < len(suffixes):
            scaled /= 1000
            scale += 1
            continue
        if len(rendered) <= limit:
            return rendered
        if scale == 0 and abs(number) >= 999.5:
            scaled = number / 1000
            scale = 1
            continue
        return "N/A" if limit >= 3 else ""


def _identity(row):
    session = row.get("session") or {}
    return row.get("profile"), session.get("id")


def deduplicated_totals(data):
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "actual_cost_usd",
    )
    result = {field: 0 for field in fields}
    seen = set()
    known = {field: False for field in fields}
    for row in data.get("panes", []):
        session = row.get("session") or {}
        ident = _identity(row)
        if not ident[1] or ident in seen:
            continue
        seen.add(ident)
        for field in fields:
            value = session.get(field)
            if value is not None:
                result[field] += value
                known[field] = True
    for field in fields:
        if not known[field]:
            result[field] = None
    return result


def visible_rows(data, state):
    rows = []
    seen = set()
    for row in data.get("panes", []):
        ident = _identity(row)
        key = ident if ident[1] else (row.get("profile"), row.get("pane_id"), row.get("task_id"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    profiles = sorted({row.get("profile") for row in rows if row.get("profile")})
    if profiles and state.profile_index >= 0:
        rows = [
            row
            for row in rows
            if row.get("profile") == profiles[min(state.profile_index, len(profiles) - 1)]
        ]
    if state.filter_by == "active":
        rows = [
            row
            for row in rows
            if row.get("status") == "matched" and row.get("state") not in ("ended", "inactive")
        ]
    elif state.filter_by == "unlinked":
        rows = [row for row in rows if row.get("status") == "unlinked"]
    if state.sort_by == "total":
        rows.sort(
            key=lambda r: (
                (r.get("session") or {}).get("total_tokens")
                if (r.get("session") or {}).get("total_tokens") is not None
                else -1
            ),
            reverse=True,
        )
    elif state.sort_by == "cost":
        rows.sort(
            key=lambda r: (
                (r.get("session") or {}).get("estimated_cost_usd")
                if (r.get("session") or {}).get("estimated_cost_usd") is not None
                else -1
            ),
            reverse=True,
        )
    elif state.sort_by == "activity":
        rows.sort(
            key=lambda r: (r.get("session") or {}).get("last_activity_at") or -1, reverse=True
        )
    return rows


def handle_key(state, key, data):
    if key in ("q", "Q", "\x03"):
        return replace(state, quit_requested=True)
    if key in ("r", "R"):
        return replace(state, refresh_requested=True)
    if key == "?":
        return replace(state, help=not state.help)
    if key in ("Enter", "\n", "\r"):
        return replace(state, details=not state.details)
    if key in ("t", "c", "l"):
        return replace(state, sort_by={"t": "total", "c": "cost", "l": "activity"}[key])
    if key == "f":
        return replace(
            state,
            filter_by={"active": "all", "all": "unlinked", "unlinked": "active"}[state.filter_by],
        )
    if key == "p":
        profiles = sorted(
            {row.get("profile") for row in data.get("panes", []) if row.get("profile")}
        )
        return replace(
            state,
            profile_index=-1
            if not profiles or state.profile_index >= len(profiles) - 1
            else state.profile_index + 1,
            selected=0,
        )
    rows = visible_rows(data, state)
    if key in ("j", "KEY_DOWN"):
        return replace(state, selected=min(state.selected + 1, max(0, len(rows) - 1)))
    if key in ("k", "KEY_UP"):
        return replace(state, selected=max(0, state.selected - 1))
    return state


def render_frame(data, state, width=80, now=None, height=None, config=None):
    observed = data.get("collected_at")
    if observed is None:
        observed = max(
            (
                s.get("collected_at", 0)
                for row in data.get("panes", [])
                for s in [row.get("session") or {}]
            ),
            default=None,
        )
    zone_name = getattr(config, "time_zone", "local") if config is not None else "local"
    zone = ZoneInfo(zone_name) if zone_name != "local" else None
    timestamp = (
        datetime.fromtimestamp(observed, zone).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if observed is not None and zone is None
        else (
            datetime.fromtimestamp(observed, zone).strftime("%Y-%m-%d %H:%M:%S %Z")
            if observed is not None
            else "observation time N/A"
        )
    )
    panes = data.get("panes", [])
    totals = deduplicated_totals({"panes": visible_rows(data, state)})
    unsupported = sum(row.get("status") == "unsupported" for row in panes)
    unlinked = sum(row.get("status") == "unlinked" for row in panes)
    lines = [f"SessionGauge  {timestamp}  {str(data.get('status', 'healthy')).upper()}"[:width]]
    unique_sessions = {_identity(row) for row in visible_rows(data, state) if _identity(row)[1]}
    lines.append(f"Totals sessions={len(unique_sessions)}")
    lines.append(
        "Usage: "
        + " ".join(
            f"{label}={_compact_number(totals[field])}"
            for label, field in (
                ("in", "input_tokens"),
                ("out", "output_tokens"),
                ("total", "total_tokens"),
            )
        )
    )
    lines.append(
        f"Cost: est={_compact_number(totals['estimated_cost_usd'], currency=True)} "
        f"actual={_compact_number(totals['actual_cost_usd'], currency=True)}"
    )
    if data.get("stale"):
        lines.append("STALE data; showing last successful observation"[:width])
    if unsupported:
        lines.append(f"Unsupported panes: {unsupported}"[:width])
    lines.append(f"Unlinked panes: {unlinked}"[:width])
    profiles = sorted({row.get("profile") for row in panes if row.get("profile")})
    profile_label = (
        "all"
        if state.profile_index < 0 or not profiles
        else profiles[state.profile_index % len(profiles)]
    )
    lines.append(
        f"Filter: {state.filter_by}  Profile: {profile_label}  Sort: {state.sort_by}"[:width]
    )
    headers = ["Task", "Pane", "State", "Model", "Input", "Output", "CacheRead", "Total", "EstUSD"]
    rows = visible_rows(data, state)
    if width >= 110:
        sizes = [20, 7, 9, 14, 9, 9, 10, 9, 9]
    else:
        sizes = [max(8, width - 67), 5, 7, 9, 7, 7, 8, 7, 7]
    lines.append(" ".join(_clip(h, n) for h, n in zip(headers, sizes))[:width])
    details_lines = []
    if state.details and rows:
        row = rows[min(state.selected, len(rows) - 1)]
        session = row.get("session") or {}
        details_lines.extend(
            f"{label}: {_clean(value if value is not None else 'N/A')}"
            for label, value in (
                ("Details: SessionID", session.get("id")),
                ("taskUUID", row.get("task_id")),
                ("profile", row.get("profile")),
                ("link", row.get("status")),
                ("confidence", row.get("confidence")),
            )
        )
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "actual_cost_usd",
            "estimated_cost_usd",
            "started_at",
            "last_activity_at",
            "end_reason",
        ):
            details_lines.append(
                f"{key}: {session.get(key) if session.get(key) is not None else 'N/A'}"[:width]
            )
        for key in ("task_lifetime", "recent_60s"):
            metric = row.get(key)
            if metric is not None:
                details_lines.append(
                    f"{key}: input={metric.get('input_tokens', 'N/A')} output={metric.get('output_tokens', 'N/A')} total={metric.get('total_tokens', 'N/A')} actual=${metric.get('actual_cost_usd', 'N/A')}"[
                        :width
                    ]
                )
        details_lines.extend(
            f"Lineage: {_clean(parent)}"[:width] for parent in row.get("lineage", [])
        )
    footer = "Observed period: recent 60s uses complete observed intervals; partial interval/baseline is unknown (lower bound)"
    if rows:
        selected = rows[min(state.selected, len(rows) - 1)]
        metric = selected.get("recent_60s")
        if metric is not None:
            footer = f"Selected task recent 60s: input={metric.get('input_tokens', 'N/A')} output={metric.get('output_tokens', 'N/A')} total={metric.get('total_tokens', 'N/A')} actual=${metric.get('actual_cost_usd', 'N/A')}"
    help_lines = (
        [
            "Keys: q quit | r refresh | ? help | j/k or arrows select | Enter details",
            "t total sort | c estimated cost sort | l last activity sort | f filter | p profile",
        ]
        if state.help
        else []
    )
    fixed = len(lines) + len(details_lines) + len(help_lines) + 1
    available = max(0, (height if height is not None else 10**9) - fixed)
    start = max(0, min(state.selected - available + 1, len(rows) - available)) if rows else 0
    for idx, row in enumerate(rows[start : start + available], start):
        session = row.get("session") or {}
        values = [
            _clean(row.get("title") or row.get("task_id") or "N/A"),
            row.get("pane_id", "N/A"),
            row.get("state") or row.get("status", "N/A"),
            session.get("model") or "N/A",
        ]
        values += [
            _compact_number(session.get(k), sizes[index])
            for index, k in enumerate(
                ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens"), 4
            )
        ]
        cost = session.get("estimated_cost_usd")
        values += [_compact_number(cost, sizes[8], currency=True)]
        line = " ".join(_clip(v, n) for v, n in zip(values, sizes))[:width]
        lines.append((">" if idx == state.selected else " ") + line[: max(0, width - 1)])
    lines.extend(details_lines)
    lines.extend(help_lines)
    lines.append(footer[:width])
    return [_clip(line, width) for line in lines]


def _worker(config, collector_factory, messages, stop, refresh, interval, profile):
    collector = None
    try:
        collector = collector_factory(config)
        while not stop.is_set():
            try:
                data = collector.collect()
                # monitor接続はCollectorを所有するこのワーカー内でのみ参照する。
                monitor = getattr(collector, "monitor", None)
                if monitor is not None:
                    known = monitor.connection.execute("""SELECT c.profile_id,c.session_id,c.model,c.title,c.cwd,c.started_at,c.ended_at,c.last_activity_at,c.end_reason,
                        t.task_id,t.display_name,t.project_path,l.source,l.confidence,
                        s.input_tokens,s.output_tokens,s.cache_read_tokens,s.cache_write_tokens,s.reasoning_tokens,s.estimated_cost_usd,s.actual_cost_usd
                        FROM session_catalog c LEFT JOIN tasks t ON t.task_id=(SELECT task_id FROM pane_session_links x WHERE x.profile_id=c.profile_id AND x.session_id=c.session_id ORDER BY x.valid_from DESC LIMIT 1)
                        LEFT JOIN pane_session_links l ON l.task_id=t.task_id AND l.profile_id=c.profile_id AND l.session_id=c.session_id
                        LEFT JOIN snapshots s ON s.profile_id=c.profile_id AND s.session_id=c.session_id AND s.collected_at=(SELECT MAX(z.collected_at) FROM snapshots z WHERE z.profile_id=c.profile_id AND z.session_id=c.session_id)
                        """).fetchall()
                    existing = {
                        (p.get("profile"), (p.get("session") or {}).get("id"))
                        for p in data.get("panes", [])
                    }
                    for r in known:
                        profile_id, session_id = r[0], r[1]
                        if (profile_id, session_id) in existing:
                            continue
                        data.setdefault("panes", []).append(
                            {
                                "pane_id": "—",
                                "status": "unlinked",
                                "state": "historical",
                                "profile": profile_id,
                                "task_id": r[9],
                                "title": r[10] or r[3],
                                "project": r[11],
                                "source": r[12],
                                "confidence": r[13],
                                "session": {
                                    "id": session_id,
                                    "model": r[2],
                                    "started_at": r[5],
                                    "ended_at": r[6],
                                    "last_activity_at": r[7],
                                    "end_reason": r[8],
                                    "input_tokens": r[14],
                                    "output_tokens": r[15],
                                    "cache_read_tokens": r[16],
                                    "cache_write_tokens": r[17],
                                    "reasoning_tokens": r[18],
                                    "total_tokens": r[14] + r[15]
                                    if r[14] is not None and r[15] is not None
                                    else None,
                                    "estimated_cost_usd": r[19],
                                    "actual_cost_usd": r[20],
                                },
                            }
                        )
                    intervals = monitor.connection.execute(
                        "SELECT profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,actual_cost_usd FROM usage_attributions"
                    ).fetchall()
                    observed = data.get("collected_at")
                    if observed is None:
                        observed = max(
                            (
                                (p.get("session") or {}).get("last_activity_at") or 0
                                for p in data.get("panes", [])
                            ),
                            default=0,
                        )
                    by_task = {}
                    for r in intervals:
                        if r[4] is None:
                            continue
                        ident = (r[0], r[4])
                        entry = by_task.setdefault(
                            ident,
                            {
                                "all": {
                                    "input": 0,
                                    "output": 0,
                                    "total": 0,
                                    "cost": 0,
                                    "known": set(),
                                },
                                "recent": {
                                    "input": 0,
                                    "output": 0,
                                    "total": 0,
                                    "cost": 0,
                                    "known": set(),
                                },
                            },
                        )
                        buckets = [entry["all"]]
                        if r[2] >= observed - 60 and r[3] <= observed:
                            buckets.append(entry["recent"])
                        for vals in buckets:
                            if r[5] is not None:
                                vals["input"] += r[5]
                                vals["known"].add("input")
                            if r[6] is not None:
                                vals["output"] += r[6]
                                vals["known"].add("output")
                            if r[5] is not None and r[6] is not None:
                                vals["total"] += r[5] + r[6]
                                vals["known"].add("total")
                            if r[7] is not None:
                                vals["cost"] += r[7]
                                vals["known"].add("cost")
                    for pane in data.get("panes", []):
                        task_id = pane.get("task_id")
                        if task_id is None:
                            continue
                        values = by_task.get((pane.get("profile"), task_id))
                        if values:
                            for name, bucket in (
                                ("task_lifetime", "all"),
                                ("recent_60s", "recent"),
                            ):
                                vals = values[bucket]
                                pane[name] = {
                                    "input_tokens": vals["input"]
                                    if "input" in vals["known"]
                                    else None,
                                    "output_tokens": vals["output"]
                                    if "output" in vals["known"]
                                    else None,
                                    "total_tokens": vals["total"]
                                    if "total" in vals["known"]
                                    else None,
                                    "actual_cost_usd": vals["cost"]
                                    if "cost" in vals["known"]
                                    else None,
                                }
                if profile:
                    data["panes"] = [
                        p for p in data.get("panes", []) if p.get("profile") == profile
                    ]
                messages.put((data, None))
            except (
                OSError,
                ValueError,
                sqlite3.Error,
                subprocess.SubprocessError,
                TypeError,
            ) as exc:
                messages.put(
                    (
                        {"panes": [], "sessions": [], "stale": True, "status": "error"},
                        type(exc).__name__,
                    )
                )
            if not stop.is_set():
                refresh.wait(interval)
                refresh.clear()
    finally:
        if collector is not None:
            collector.close()


def run_tui(config, collector_factory, *, screen_runner=None, interval=None, profile=None):
    messages, stop, refresh = queue.Queue(), threading.Event(), threading.Event()
    worker = threading.Thread(
        target=_worker,
        args=(
            config,
            collector_factory,
            messages,
            stop,
            refresh,
            interval or config.poll_interval_seconds,
            profile,
        ),
        daemon=True,
    )
    worker.start()
    state, data = TUIState(), {"panes": [], "sessions": [], "status": "starting"}

    def ui(screen):
        nonlocal state, data
        screen.nodelay(True)
        if hasattr(screen, "keypad"):
            screen.keypad(True)
        while not state.quit_requested:
            while not messages.empty():
                data, _ = messages.get_nowait()
            height, width = screen.getmaxyx()
            screen.erase()
            for index, line in enumerate(
                render_frame(data, state, width, height=height, config=config)
            ):
                if index >= height:
                    break
                try:
                    screen.addnstr(index, 0, line, max(0, width - 1))
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            if key == -1:
                time.sleep(0.05)
                continue
            mapped = {
                curses.KEY_UP: "KEY_UP",
                curses.KEY_DOWN: "KEY_DOWN",
                10: "Enter",
                13: "Enter",
                27: "Escape",
            }.get(key, chr(key) if 0 <= key < 256 else "")
            state = handle_key(state, mapped, data)
            if state.refresh_requested:
                state = replace(state, refresh_requested=False)
                refresh.set()

    try:
        (screen_runner or curses.wrapper)(ui)
    finally:
        stop.set()
        refresh.set()
        worker.join(timeout=10)
    return 0
