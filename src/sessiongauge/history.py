"""Observed usage history, derived only from persisted attribution intervals."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sessiongauge.persistence import MonitorDB

COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
)


def _zone(name):
    if name != "local":
        return ZoneInfo(name)
    # tzlocalは任意依存とし、一般的なUnix環境ではTZからIANAタイムゾーンを取得する。
    import os
    from pathlib import Path

    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        try:
            return ZoneInfo(tz)
        except (ValueError, TypeError):
            pass
    localtime = Path("/etc/localtime")
    try:
        resolved = str(localtime.resolve()).split("/zoneinfo/", 1)[1]
        return ZoneInfo(resolved)
    except (OSError, IndexError, ValueError):
        return datetime.now().astimezone().tzinfo


def history(
    config, *, days=30, group_by="task", task=None, model=None, project=None, profile=None, now=None
):
    if days <= 0:
        raise ValueError("days must be positive")
    if group_by not in {"task", "session", "model", "day", "project"}:
        raise ValueError("unsupported group_by")
    zone = _zone(config.time_zone)
    current = datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)
    end = current.timestamp()
    start = (current - timedelta(days=days)).timestamp()
    notes = []
    from sessiongauge.collector import Collector

    collector = Collector(config)
    try:
        collector.collect(now=end)
    finally:
        collector.close()
    with MonitorDB(config.data_dir / "monitor.db", source_paths=config.hermes_db_paths) as db:
        cursor = db.connection.execute("""SELECT u.profile_id,u.session_id,u.interval_start,u.interval_end,u.task_id,
          u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,u.reasoning_tokens,
          u.estimated_cost_usd,u.actual_cost_usd,u.confidence,u.provenance,p.name AS profile,
          c.model,c.source,c.title,c.cwd,c.git_repo_root,c.started_at,c.ended_at,
          t.display_name AS task_title,t.project_path
          FROM usage_attributions u JOIN profiles p ON p.id=u.profile_id
          LEFT JOIN session_catalog c ON c.profile_id=u.profile_id AND c.session_id=u.session_id
          LEFT JOIN tasks t ON t.task_id=u.task_id ORDER BY u.interval_start,u.interval_end""")
        columns = [x[0] for x in cursor.description]
        rows = cursor.fetchall()
        raw = [dict(zip(columns, row)) for row in rows]
        entries = []
        for e in raw:
            if (
                e["interval_start"] < start < e["interval_end"]
                or e["interval_start"] < end < e["interval_end"]
            ):
                notes.append(
                    "An interval crosses a requested period boundary and was skipped; totals are a lower bound."
                )
                continue
            if e["interval_start"] < start or e["interval_end"] > end:
                continue
            local_start = datetime.fromtimestamp(e["interval_start"], zone)
            local_end = datetime.fromtimestamp(e["interval_end"], zone)
            if local_start.date() != local_end.date():
                notes.append(
                    "An interval crosses local midnight and was skipped; totals are a lower bound."
                )
                continue
            if profile and profile not in (e["profile"], e["profile_id"]):
                continue
            if task and task not in (e["task_id"], e["task_title"]):
                continue
            if project and project not in (e["project_path"], e["git_repo_root"], e["cwd"]):
                continue
            if model and model != e["model"]:
                continue
            e["period_start"], e["period_end"] = local_start.isoformat(), local_end.isoformat()
            e["total_tokens"] = (
                e["input_tokens"] + e["output_tokens"]
                if e["input_tokens"] is not None and e["output_tokens"] is not None
                else None
            )
            e["mapping_status"] = "attributed" if e["task_id"] is not None else "unattributed"
            e["data_source"] = e["provenance"]
            e["model_provenance"] = "best-known-session-model" if e["model"] else "unknown"
            e["pane_ids"] = [
                r[0]
                for r in db.connection.execute(
                    "SELECT DISTINCT pane_id FROM pane_session_links WHERE profile_id=? AND session_id=? AND valid_from < ? AND (valid_to IS NULL OR valid_to > ?)",
                    (e["profile_id"], e["session_id"], e["interval_end"], e["interval_start"]),
                )
            ]
            entries.append(e)
        grouped = {}
        for e in entries:
            project_key = e["project_path"] or e["git_repo_root"] or e["cwd"]
            keys = {
                "task": (e["task_id"] or "unattributed",),
                "session": (e["profile_id"], e["session_id"]),
                "model": (e["model"] or "unknown",),
                "project": (project_key or "unknown",),
                "day": (datetime.fromtimestamp(e["interval_start"], zone).date().isoformat(),),
            }
            key = keys[group_by]
            target = grouped.setdefault(
                key,
                {
                    "group": f"{key[0]}:{key[1]}" if group_by == "session" else key[0],
                    "group_key": list(key),
                    "notes": [],
                    **{f: None for f in COUNTERS},
                    "total_tokens": None,
                    "records": [],
                    "mapping_status": e["mapping_status"],
                    "model": e["model"],
                    "model_provenance": e["model_provenance"],
                },
            )
            for field in COUNTERS:
                if e[field] is not None:
                    target[field] = (target[field] or 0) + e[field]
            if target["input_tokens"] is not None and target["output_tokens"] is not None:
                target["total_tokens"] = target["input_tokens"] + target["output_tokens"]
            if any(e[field] is None for field in COUNTERS):
                notes.append("Some counter values are unavailable; totals are partial/N/A.")
            if e["estimated_cost_usd"] is None or e["actual_cost_usd"] is None:
                notes.append("Some cost values are unavailable; reported costs are partial/N/A.")
            target["records"].append(e)
        for target in grouped.values():
            target["notes"] = list(dict.fromkeys(notes))
        catalogs = db.connection.execute("""SELECT c.profile_id,c.session_id,c.title,c.model,c.source,c.cwd,c.git_repo_root,
          c.started_at,c.ended_at,c.last_activity_at,p.name AS profile FROM session_catalog c JOIN profiles p ON p.id=c.profile_id
          WHERE NOT EXISTS (SELECT 1 FROM usage_attributions u WHERE u.profile_id=c.profile_id AND u.session_id=c.session_id)""").fetchall()
        catalog_cols = [
            x[0]
            for x in db.connection.execute(
                "SELECT c.profile_id,c.session_id,c.title,c.model,c.source,c.cwd,c.git_repo_root,c.started_at,c.ended_at,c.last_activity_at,p.name FROM session_catalog c JOIN profiles p ON p.id=c.profile_id LIMIT 0"
            ).description
        ]
        baselines = db.connection.execute(
            """SELECT s.profile_id,s.session_id,s.collected_at,s.input_tokens,s.output_tokens,s.cache_read_tokens,s.cache_write_tokens,s.reasoning_tokens,s.estimated_cost_usd,s.actual_cost_usd,p.name,c.title,c.model,c.cwd,c.git_repo_root,c.ended_at FROM snapshots s JOIN (SELECT profile_id,session_id,MIN(collected_at) at FROM snapshots GROUP BY profile_id,session_id) f ON f.profile_id=s.profile_id AND f.session_id=s.session_id AND f.at=s.collected_at JOIN profiles p ON p.id=s.profile_id LEFT JOIN session_catalog c ON c.profile_id=s.profile_id AND c.session_id=s.session_id"""
        ).fetchall()
        baseline_cols = (
            "profile_id",
            "session_id",
            "collected_at",
            *COUNTERS,
            "profile",
            "title",
            "model",
            "cwd",
            "git_repo_root",
            "ended_at",
        )
        baseline_list = []
        for row in baselines:
            item = dict(
                zip(baseline_cols, row),
                mapping_status="unlinked",
                data_source="baseline",
                unknown_historical_usage=True,
            )
            item["project_path"] = item["git_repo_root"] or item["cwd"]
            item["pane_ids"] = [
                link[0]
                for link in db.connection.execute(
                    "SELECT DISTINCT pane_id FROM pane_session_links WHERE profile_id=? AND session_id=? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)",
                    (
                        item["profile_id"],
                        item["session_id"],
                        item["collected_at"],
                        item["collected_at"],
                    ),
                )
            ]
            baseline_list.append(item)
        referenced = {(item["profile_id"], item["session_id"]) for item in entries}
        baseline_list = list(
            {
                (item["profile_id"], item["session_id"]): item
                for item in baseline_list
                if item.get("ended_at") is None
                or item.get("ended_at") >= start
                or (item["profile_id"], item["session_id"]) in referenced
            }.values()
        )

    def selected(item):
        return (
            (not profile or profile in (item.get("profile"), item.get("profile_id")))
            and (not model or model == item.get("model"))
            and (
                not project
                or project in (item.get("project_path"), item.get("git_repo_root"), item.get("cwd"))
            )
            and (not task or task in (item.get("task_id"), item.get("task_title")))
        )

    historical = [
        dict(
            zip(catalog_cols, r),
            mapping_status="unlinked",
            data_source="catalog",
            unknown_historical_usage=True,
        )
        for r in catalogs
        if r[8] is None or r[8] >= start
    ]
    return {
        "days": days,
        "group_by": group_by,
        "period": {"start": start, "end": end},
        "groups": list(grouped.values()),
        "baselines": [r for r in baseline_list if selected(r)],
        "historical_sessions": [r for r in historical if selected(r)],
        "notes": list(dict.fromkeys(notes)),
    }
