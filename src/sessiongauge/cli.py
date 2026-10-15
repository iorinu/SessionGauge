import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sessiongauge import __version__
from sessiongauge.adapters.herdr import HerdrAdapter
from sessiongauge.adapters.hermes_db import SAFE_COLUMNS, HermesDB
from sessiongauge.autostart import AutostartError
from sessiongauge.collector import Collector
from sessiongauge.config import load_config, profile_name_for_path
from sessiongauge.monitor_lock import (
    MonitorAlreadyRunning,
    MonitorEndpointError,
    MonitorLease,
    resolve_endpoint,
)
from sessiongauge.persistence import MonitorDB

MONITOR_ALREADY_RUNNING_MESSAGE = (
    "SessionGauge monitor is already running for this data directory and herdr endpoint."
)


def _parser():
    parser = argparse.ArgumentParser(prog="sessiongauge")
    parser.add_argument("--config")
    parser.add_argument("--hermes-db", action="append")
    parser.add_argument("--data-dir")
    parser.add_argument(
        "--poll-interval-seconds", "--interval", dest="poll_interval_seconds", type=int
    )
    parser.add_argument("--time-zone")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command):
        command.add_argument("--config", default=argparse.SUPPRESS)
        command.add_argument("--data-dir", default=argparse.SUPPRESS)
        command.add_argument("--hermes-db", action="append", default=argparse.SUPPRESS)
        command.add_argument(
            "--poll-interval-seconds",
            "--interval",
            dest="poll_interval_seconds",
            type=int,
            default=argparse.SUPPRESS,
        )
        command.add_argument("--time-zone", default=argparse.SUPPRESS)
        command.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)

    doctor = sub.add_parser("doctor")
    common(doctor)
    live = sub.add_parser("live")
    common(live)
    live.add_argument("--once", action="store_true")
    live.add_argument("--json", action="count", default=0)
    live.add_argument("--profile")
    live.add_argument("--sidebar", dest="sidebar_enabled", action="store_true", default=None)
    live.add_argument("--no-sidebar", dest="sidebar_enabled", action="store_false")
    history = sub.add_parser("history")
    common(history)
    history.add_argument("--show")
    history.add_argument("--days", type=_positive, default=30)
    history.add_argument(
        "--group-by", choices=("task", "session", "model", "day", "project"), default="task"
    )
    for option in ("task", "model", "project", "profile"):
        history.add_argument("--" + option)
    export = sub.add_parser("export")
    common(export)
    export.add_argument("--format", choices=("csv", "json"), default="json")
    export.add_argument("--days", type=_positive, default=30)
    export.add_argument("--output", required=True)
    export.add_argument("--bom", action="store_true")
    export.add_argument("--force", action="store_true")
    link = sub.add_parser("link")
    common(link)
    link.add_argument("pane_id")
    link.add_argument("session_id")
    link.add_argument("--profile")
    show = sub.add_parser("show")
    common(show)
    show.add_argument("id")
    autostart = sub.add_parser("autostart")
    common(autostart)
    actions = autostart.add_subparsers(dest="autostart_action", required=True)
    for name in ("install", "start", "stop", "status", "uninstall", "run"):
        action = actions.add_parser(name)
        common(action)
        action.add_argument("--socket-path")
        action.add_argument("--herdr-bin")
    return parser


def _positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return result


def main(argv=None):
    args = _parser().parse_args(argv)
    cli = {
        "data_dir": args.data_dir,
        "poll_interval_seconds": args.poll_interval_seconds,
        "time_zone": getattr(args, "time_zone", None),
    }
    if args.command == "live":
        cli["sidebar_enabled"] = args.sidebar_enabled
    if args.hermes_db:
        cli["hermes_db_paths"] = args.hermes_db
    try:
        config = load_config(
            cli=cli, config_path=Path(args.config).expanduser() if args.config else None
        )
        if args.command == "autostart":
            return _autostart(config, args)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "live":
            if not args.once and not args.json:
                if sys.stdin.isatty() and sys.stdout.isatty():
                    if args.profile:
                        from dataclasses import replace

                        config = replace(
                            config,
                            hermes_db_paths=_select_profiles(config.hermes_db_paths, args.profile),
                        )
                    from sessiongauge.tui import run_tui

                    return _continuous_tui(config, run_tui)
                print(
                    "live requires a TTY; use --once for a text snapshot or --json for JSON output",
                    file=sys.stderr,
                )
                return 2
            return _live(config, args.json, once=args.once, profile=getattr(args, "profile", None))
        if args.command == "history":
            return _history(config, args.days, args.group_by, args)
        if args.command == "export":
            return _export(config, args)
        if args.command == "link":
            return _link(config, args)
        if args.command == "show":
            return _show(config, args.id)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except AutostartError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except MonitorAlreadyRunning:
        print(MONITOR_ALREADY_RUNNING_MESSAGE, file=sys.stderr)
        return 0
    except MonitorEndpointError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
        TypeError,
        EOFError,
    ) as exc:
        if getattr(args, "debug", False):
            print(f"SessionGauge debug: error_type={type(exc).__name__}", file=sys.stderr)
        print(
            json.dumps({"error": type(exc).__name__})
            if getattr(args, "json", False)
            else f"Error: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


def _doctor(config):
    reports = {
        "product": "SessionGauge",
        "version": __version__,
        "platform": platform.system(),
        "python": f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
        "commands": {},
        "databases": [],
    }
    for name in ("hermes", "herdr"):
        executable = shutil.which(name)
        info = {"installed": executable is not None}
        if executable:
            try:
                result = subprocess.run(
                    [executable, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                version = (result.stdout or result.stderr).strip().splitlines()
                info["version"] = version[0][:120] if version else "unknown"
            except (OSError, subprocess.SubprocessError):
                info["version"] = "unavailable"
        reports["commands"][name] = info
    for path in config.hermes_db_paths:
        item = {
            "path": str(path.expanduser()).replace(str(Path.home()), "~", 1),
            "available": path.is_file(),
        }
        if path.is_file():
            try:
                with HermesDB(path) as db:
                    item.update(
                        {
                            "readable": True,
                            "schema_version": db.schema_version,
                            "user_version": db.user_version,
                            "journal_mode": db.journal_mode,
                            "sessions_table": bool(db.columns),
                            "session_count": len(db.sessions()),
                            "schema_columns": sorted(db.columns),
                        }
                    )
                    required = {"id"}
                    missing = sorted(required - set(db.columns))
                    optional_missing = sorted((SAFE_COLUMNS - required) - set(db.columns))
                    item["compatible"] = not missing
                    if missing or optional_missing:
                        item["warning"] = "Missing session columns: " + ", ".join(
                            missing + optional_missing
                        )
            except (sqlite3.Error, OSError):
                item["readable"] = False
                item["warning"] = "Database could not be read; verify its path and permissions."
        reports["databases"].append(item)
    herdr_executable = shutil.which("herdr")
    integration = {}
    status_details = {}
    if herdr_executable:
        try:
            result = subprocess.run(
                [herdr_executable, "integration", "status"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            for line in result.stdout.splitlines():
                name, separator, status = line.partition(":")
                if separator and name.strip() == "hermes":
                    # 他の integration の情報や出力中の識別子を診断結果へ含めない。
                    import re

                    parsed = re.match(
                        r"\s*((?:current\(v[0-9]+\))|[A-Za-z0-9_. -]{1,40})", status, re.IGNORECASE
                    )
                    if parsed:
                        integration["hermes"] = parsed.group(1).strip()
                    break
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            result = subprocess.run(
                [herdr_executable, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            status = json.loads(result.stdout)
            if isinstance(status, dict):
                client, server, update = (status.get(key) for key in ("client", "server", "update"))
                if isinstance(client, dict) and isinstance(server, dict):
                    details = {}
                    for section_name, section in (("client", client), ("server", server)):
                        for key in ("version", "protocol"):
                            value = section.get(key)
                            if isinstance(value, (str, int)) and not isinstance(value, bool):
                                details[f"{section_name}_{key}"] = value
                    for key in ("compatible", "endpoint_compatible"):
                        if isinstance(server.get(key), bool):
                            details[key] = server[key]
                    if isinstance(update, dict) and isinstance(update.get("restart_needed"), bool):
                        details["restart_needed"] = update["restart_needed"]
                    status_details = details
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass
    panes = None
    try:
        panes = HerdrAdapter().list_panes()
        reports["herdr"] = {
            "reachable": True,
            "pane_count": len(panes),
            "integration": integration,
            **status_details,
        }
        from sessiongauge.identity import match_panes

        databases = {}
        for db_path in config.hermes_db_paths:
            profile = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:12]
            try:
                with HermesDB(db_path) as source:
                    databases[profile] = {row.id for row in source.sessions()}
            except (OSError, sqlite3.Error, ValueError):
                databases[profile] = set()
        matched = match_panes(panes, databases)
        reports["herdr"]["exact_match_count"] = sum(item.status == "matched" for item in matched)
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        reports["herdr"] = {
            "reachable": False,
            "error": type(exc).__name__,
            "integration": integration,
            **status_details,
        }
    reports["guidance"] = [
        "Install Hermes and herdr using their official instructions if they are missing."
    ]
    if integration.get("hermes", "").lower() not in ("current", "installed", "current(v5)"):
        reports["guidance"].append(
            "If needed, run `herdr integration install hermes` to install the Hermes integration."
        )
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


def _autostart(config, args):
    from sessiongauge.autostart import Installation

    if platform.system() != "Darwin":
        raise AutostartError("autostart is only supported on macOS")
    executable = (
        getattr(args, "herdr_bin", None)
        or os.environ.get("HERDR_BIN_PATH")
        or shutil.which("herdr")
    )
    if not executable or not Path(executable).is_absolute():
        raise AutostartError("herdr executable must be an absolute path")
    endpoint = getattr(args, "socket_path", None)
    if endpoint is None:
        endpoint = resolve_endpoint()
    endpoint = Path(endpoint).expanduser()
    if not endpoint.is_absolute():
        raise AutostartError("herdr socket path must be absolute")
    config_path = (
        Path(args.config).expanduser()
        if args.config
        else Path(
            os.environ.get("SESSIONGAUGE_CONFIG", Path.home() / ".config/sessiongauge/config.toml")
        ).expanduser()
    )
    instance = Installation(
        data_dir=config.data_dir,
        socket_path=endpoint,
        config_path=config_path,
        python_executable=sys.executable,
        herdr_bin=executable,
        runner=subprocess.run,
        hermes_db_paths=(
            tuple(Path(path).expanduser().resolve(strict=False) for path in args.hermes_db)
            if args.hermes_db
            else None
        ),
        poll_interval_seconds=args.poll_interval_seconds,
        time_zone=args.time_zone,
    )
    action = args.autostart_action
    if action == "run":
        return _autostart_run(instance, config)
    # plugin actionはmanifestが注入するsocketと対象を一致させてからnative処理する。
    if os.environ.get("HERDR_ENV"):
        injected = os.environ.get("HERDR_SOCKET_PATH")
        if not injected or Path(injected).expanduser().resolve(strict=False) != endpoint.resolve(
            strict=False
        ):
            print(json.dumps({"not_applicable": True}))
            return 0
    if action == "install":
        instance.install()
        print(
            json.dumps(
                {
                    "label": instance.label,
                    "plist_path": str(instance.plist_path),
                    "plugin_dir": str(instance.plugin_path.parent),
                    "plugin_path": str(instance.plugin_path),
                    "plugin_id": instance.plugin_id,
                    "scope_hash": instance.scope_hash,
                }
            )
        )
    elif action == "start":
        instance.start()
        print(json.dumps({"started": True}))
    elif action == "stop":
        instance.stop()
        print(json.dumps({"stopped": True}))
    elif action == "status":
        print(json.dumps(instance.status()))
    elif action == "uninstall":
        instance.uninstall()
        print(json.dumps({"uninstalled": True}))
    return 0


def _autostart_run(instance, config=None):
    previous = {
        key: os.environ.get(key) for key in ("HERDR_SOCKET_PATH", "HERDR_BIN_PATH", "HERDR_ENV")
    }
    old_handlers = {}
    try:
        os.environ.update(
            {
                "HERDR_SOCKET_PATH": str(instance.socket_path),
                "HERDR_BIN_PATH": instance.herdr_bin,
                "HERDR_ENV": "1",
            }
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            result = subprocess.run(
                [instance.herdr_bin, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        if result.returncode != 0:
            return 0
        try:
            status = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise AutostartError("herdr status response is invalid") from exc
        if not isinstance(status, dict) or not isinstance(status.get("server"), dict):
            raise AutostartError("herdr status response is invalid")
        server = status["server"]
        if type(server.get("running")) is not bool:
            raise AutostartError("herdr status response is invalid")
        socket = server.get("socket", server.get("socket_path"))
        if server["running"] is False:
            return 0
        if not isinstance(socket, str) or not Path(socket).is_absolute():
            raise AutostartError("herdr status endpoint is invalid")
        if Path(socket).resolve(strict=False) != instance.socket_path.resolve(strict=False):
            raise AutostartError("herdr status endpoint does not match configured socket")
        return _live(
            config or load_config(config_path=instance.config_path), as_json=True, once=False
        )
    except KeyboardInterrupt:
        return 0
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _live(config, as_json, *, once=True, profile=None):
    if profile:
        from dataclasses import replace

        selected = _select_profiles(config.hermes_db_paths, profile)
        config = replace(config, hermes_db_paths=selected)
    collector = None
    lease = _continuous_lease(config) if not once else None
    try:
        if lease is not None:
            lease.__enter__()
        while True:
            try:
                if collector is None:
                    collector = _sidebar_factory(config)(config)
                result = collector.collect()
            except (
                OSError,
                ValueError,
                sqlite3.Error,
                subprocess.SubprocessError,
                TypeError,
            ) as exc:
                result = {"panes": [], "sessions": [], "stale": True, "error": type(exc).__name__}
            _render(result, as_json)
            if once:
                return 0
            time.sleep(config.poll_interval_seconds)
    finally:
        try:
            if collector:
                collector.close()
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)


def _continuous_lease(config):
    if os.name != "posix":
        return None
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return MonitorLease(config.data_dir, resolve_endpoint())


def _continuous_tui(config, run_tui):
    factory = _sidebar_factory(config)
    lease = _continuous_lease(config)
    if lease is None:
        return run_tui(config, factory)
    lease.__enter__()
    return run_tui(config, factory, lease=lease)


def _sidebar_factory(config):
    def factory(inner_config):
        collector = Collector(inner_config)
        from sessiongauge.quota import QuotaReader
        from sessiongauge.sidebar import SidebarReporter

        quota_reader = QuotaReader(
            str(config.sidebar_quota_agent_resume_bin),
            str(config.sidebar_quota_hermes_home),
            config.sidebar_quota_poll_interval_seconds,
            hermes_root=str(
                getattr(config, "sidebar_quota_hermes_root", Path.home() / ".hermes/hermes-agent")
            ),
            hermes_python=(
                str(config.sidebar_quota_hermes_python)
                if getattr(config, "sidebar_quota_hermes_python", None)
                else None
            ),
            auth_helper=str(getattr(config, "sidebar_quota_auth_helper", None))
            if getattr(config, "sidebar_quota_auth_helper", None)
            else None,
        )
        reporter = SidebarReporter(
            enabled=config.sidebar_enabled,
            ttl_ms=config.sidebar_ttl_ms,
            quota_enabled=config.sidebar_quota_enabled,
            quota_reader=quota_reader,
            hermes_home=config.sidebar_quota_hermes_home,
            display=getattr(config, "sidebar_display", "percent"),
            quota_mode=getattr(config, "sidebar_quota_mode", "estimate"),
            estimate_max_age_seconds=getattr(
                config, "sidebar_quota_estimate_max_age_seconds", 86400
            ),
        )

        class ReportingCollector:
            @property
            def monitor(self):
                return collector.monitor

            def collect(self):
                result = collector.collect()
                reporter.publish(result, observed_panes=collector.herdr_state.last_success)
                return result

            def close(self):
                try:
                    reporter.close()
                finally:
                    collector.close()

        return ReportingCollector()

    return factory


def _collect(config, selected_profile=None):
    if selected_profile:
        from dataclasses import replace

        config = replace(
            config, hermes_db_paths=_select_profiles(config.hermes_db_paths, selected_profile)
        )
    return Collector(config).collect()


def _select_profiles(paths, selected):
    matches = []
    for path in paths:
        canonical = str(path.expanduser().resolve(strict=False))
        profile_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical))
        human = profile_name_for_path(path, os.environ.get("HERMES_HOME"))
        if selected in (human, profile_id, canonical):
            matches.append(path)
    if not matches:
        raise ValueError("profile not found")
    return tuple(matches)


def _render(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        for row in result["panes"]:
            usage = row["session"]
            print(
                f"{row['pane_id']}  {row['status']}  "
                + (str(usage["total_tokens"]) if usage else "N/A")
            )
    return 0


def _history(config, days, group_by, args=None):
    from sessiongauge.history import history

    args = args or argparse.Namespace()
    report = history(
        config,
        days=days,
        group_by=group_by,
        task=getattr(args, "task", None),
        model=getattr(args, "model", None),
        project=getattr(args, "project", None),
        profile=getattr(args, "profile", None),
    )
    if getattr(args, "show", None):
        report["groups"] = [
            item
            for item in report["groups"]
            if any(
                row.get("task_id") == args.show or row.get("session_id") == args.show
                for row in item.get("records", [])
            )
        ]
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _export(config, args):
    path = Path(args.output).expanduser()
    from sessiongauge.export import export_rows

    protected = [*config.hermes_db_paths, config.data_dir / "monitor.db"]
    # alias検査を履歴収集より先に行い、拒否時にDBを書き換えない。
    from sessiongauge.export import _protected

    if _protected(
        path,
        [
            Path(p)
            for source in protected
            for p in (
                source,
                Path(str(source) + "-wal"),
                Path(str(source) + "-shm"),
                Path(str(source) + "-journal"),
            )
        ],
    ):
        raise ValueError("output aliases a protected database or is a symlink")
    if path.exists() and not args.force:
        raise ValueError("output exists; use --force")
    from sessiongauge.history import history

    report = history(config, days=args.days)
    data = []
    for group in report["groups"]:
        for item in group.get("records", []):
            data.append(
                {
                    "task_id": item.get("task_id"),
                    "task_title": item.get("task_title"),
                    "pane_ids": item.get("pane_ids", []),
                    "project": item.get("project_path")
                    or item.get("git_repo_root")
                    or item.get("cwd"),
                    "profile": item.get("profile"),
                    "profile_id": item.get("profile_id"),
                    "session_ids": [item.get("session_id")],
                    "interval_start": item.get("period_start"),
                    "interval_end": item.get("period_end"),
                    "baseline_timestamp": None,
                    **{
                        name: item.get(name)
                        for name in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "cache_write_tokens",
                            "reasoning_tokens",
                            "total_tokens",
                            "estimated_cost_usd",
                            "actual_cost_usd",
                        )
                    },
                    "model": item.get("model"),
                    "model_provenance": item.get("model_provenance"),
                    "mapping_status": item.get("mapping_status"),
                    "data_source": item.get("data_source"),
                }
            )
    for item in report.get("baselines", []):
        total = (
            item.get("input_tokens") + item.get("output_tokens")
            if item.get("input_tokens") is not None and item.get("output_tokens") is not None
            else None
        )
        data.append(
            {
                "task_id": None,
                "task_title": item.get("title"),
                "pane_ids": item.get("pane_ids", []),
                "project": item.get("project_path") or item.get("git_repo_root") or item.get("cwd"),
                "profile": item.get("profile"),
                "profile_id": item.get("profile_id"),
                "session_ids": [item.get("session_id")],
                "interval_start": None,
                "interval_end": None,
                "baseline_timestamp": item.get("collected_at"),
                **{
                    name: item.get(name)
                    for name in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "reasoning_tokens",
                        "estimated_cost_usd",
                        "actual_cost_usd",
                    )
                },
                "total_tokens": total,
                "mapping_status": "unknown",
                "data_source": "baseline",
            }
        )
    export_rows(
        data,
        path,
        format=args.format,
        bom=bool(args.bom and args.format == "csv"),
        force=args.force,
        protected_paths=protected,
    )
    return 0


def _safe_csv(value):
    if isinstance(value, str):
        index = next((i for i, ch in enumerate(value) if ch not in " \t\r\n\x00"), len(value))
        if index < len(value) and value[index] in "=+-@":
            return "'" + "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    return value


def _show(config, identifier):
    with MonitorDB(config.data_dir / "monitor.db", source_paths=config.hermes_db_paths) as db:

        def named(cursor):
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

        task = named(
            db.connection.execute(
                "SELECT task_id,display_name,project_path,created_at,closed_at FROM tasks WHERE task_id=?",
                (identifier,),
            )
        )
        session = named(
            db.connection.execute(
                "SELECT profile_id,session_id,parent_session_id,model,source,title,started_at,ended_at,last_activity_at,end_reason FROM session_catalog WHERE session_id=?",
                (identifier,),
            )
        )
        if len({item["profile_id"] for item in session}) > 1:
            raise ValueError("session id is ambiguous; use its task id")
        links = named(
            db.connection.execute(
                "SELECT endpoint,pane_id,terminal_id,profile_id,session_id,valid_from,valid_to,source,confidence FROM pane_session_links WHERE task_id=? ORDER BY valid_from",
                (identifier,),
            )
        )
        if session:
            links = named(
                db.connection.execute(
                    "SELECT endpoint,pane_id,terminal_id,profile_id,session_id,valid_from,valid_to,source,confidence FROM pane_session_links WHERE session_id=? ORDER BY valid_from",
                    (identifier,),
                )
            )
        events = named(
            db.connection.execute(
                "SELECT at,kind,detail FROM events WHERE detail=? ORDER BY at", (identifier,)
            )
        )
        usage = named(
            db.connection.execute(
                "SELECT interval_start,interval_end,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd FROM usage_attributions WHERE task_id=? ORDER BY interval_start",
                (identifier,),
            )
        )
        if session:
            usage = named(
                db.connection.execute(
                    "SELECT interval_start,interval_end,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd FROM usage_attributions WHERE session_id=? ORDER BY interval_start",
                    (identifier,),
                )
            )
        task_sessions = named(
            db.connection.execute(
                "SELECT DISTINCT c.profile_id,c.session_id,c.parent_session_id,c.model,c.title FROM session_catalog c JOIN pane_session_links l ON l.profile_id=c.profile_id AND l.session_id=c.session_id WHERE l.task_id=?",
                (identifier,),
            )
        )
        lineage = {}
        for item in task_sessions:
            lineage[item["session_id"]] = item
        for item in list(task_sessions):
            if item["parent_session_id"]:
                parent = named(
                    db.connection.execute(
                        "SELECT profile_id,session_id,parent_session_id,model,title FROM session_catalog WHERE profile_id=? AND session_id=?",
                        (item["profile_id"], item["parent_session_id"]),
                    )
                )
                if parent:
                    lineage[parent[0]["session_id"]] = parent[0]
        for parent_id in tuple(lineage):
            for child in named(
                db.connection.execute(
                    "SELECT profile_id,session_id,parent_session_id,model,title FROM session_catalog WHERE parent_session_id=?",
                    (parent_id,),
                )
            ):
                lineage[child["session_id"]] = child
        name_events = []
        for row in db.connection.execute(
            "SELECT at,kind,detail FROM events WHERE kind='pane_title_changed' ORDER BY at"
        ):
            try:
                detail = json.loads(row[2])
            except (ValueError, TypeError):
                continue
            if isinstance(detail, dict) and "old_title" in detail and "new_title" in detail:
                name_events.append(
                    {
                        "at": row[0],
                        "pane_id": detail.get("pane_id"),
                        "old_title": detail["old_title"],
                        "new_title": detail["new_title"],
                    }
                )
    if not task and not links and not session:
        raise ValueError("task or session not found")
    print(
        json.dumps(
            {
                "task": task,
                "session": session,
                "task_sessions": task_sessions,
                "lineage": list(lineage.values()),
                "name_changes": name_events,
                "links": links,
                "events": events,
                "period_counters": usage,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _link(config, args):
    paths = []
    for candidate in config.hermes_db_paths:
        canonical = str(candidate.expanduser().resolve(strict=False))
        profile_id = uuid.uuid5(uuid.NAMESPACE_URL, canonical)
        profile_name = (
            candidate.parent.name if candidate.parent.parent.name == "profiles" else "default"
        )
        if args.profile is None or args.profile in (profile_name, str(profile_id), canonical):
            paths.append(candidate)
    if args.profile and not paths:
        raise ValueError("profile not found")
    containing = []
    for candidate in paths:
        try:
            with HermesDB(candidate) as source:
                if args.session_id in {s.id for s in source.sessions()}:
                    containing.append(candidate)
        except (OSError, sqlite3.Error):
            continue
    paths = containing
    if len(paths) != 1:
        raise ValueError("session profile not found or ambiguous")
    with HermesDB(paths[0]) as source:
        sessions = source.sessions()
    if args.session_id not in {s.id for s in sessions}:
        raise ValueError("session not found")
    pane = next((p for p in HerdrAdapter().list_panes() if p.pane_id == args.pane_id), None)
    if pane is None:
        raise ValueError("pane not found")
    if pane.session_id is not None and pane.session_id != args.session_id:
        raise ValueError("session does not match pane's official session")
    with MonitorDB(config.data_dir / "monitor.db", source_paths=config.hermes_db_paths) as db:
        profile_name = profile_name_for_path(paths[0], os.environ.get("HERMES_HOME"))
        profile_id = db.register_profile(profile_name, paths[0])
        active = db.connection.execute(
            "SELECT source,profile_id,session_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
            pane.generation_key,
        ).fetchone()
        if active and active[0] == "official" and active[1:] != (profile_id, args.session_id):
            raise ValueError("official link cannot be overridden")
        task_id = db.link_manual(pane, profile_id, args.session_id, time.time())
    print(task_id)
    return 0
