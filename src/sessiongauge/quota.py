"""Hermes会話ごとのCodex利用率を読み取る。"""

import json
import math
import os
import subprocess
import time
from pathlib import Path


class QuotaReader:
    def __init__(
        self,
        agent_resume_bin="agent-resume",
        hermes_home="~/.hermes",
        poll_interval_seconds=60,
        *,
        runner=None,
        clock=None,
        hermes_root="~/.hermes/hermes-agent",
        hermes_python=None,
        auth_helper=None,
    ):
        self.agent_resume_bin = agent_resume_bin
        self.hermes_home = hermes_home
        self.poll_interval_seconds = poll_interval_seconds
        self.runner = runner or subprocess.run
        self.clock = clock or time.monotonic
        self.hermes_root = str(Path(hermes_root).expanduser())
        self.hermes_python = (
            str(Path(hermes_python).expanduser())
            if hermes_python
            else str(Path(self.hermes_root) / "venv/bin/python")
        )
        self.auth_helper = (
            str(Path(auth_helper).expanduser())
            if auth_helper
            else self._detect_helper(agent_resume_bin)
        )
        self._last_poll = None
        self._last_sessions_poll = None
        self._cached = None
        self._sessions_cached = {}

    @staticmethod
    def _detect_helper(binary):
        from shutil import which

        executable = which(binary) if not Path(binary).is_absolute() else binary
        if not executable:
            return None
        return str(Path(executable).resolve().parent.parent.parent / "adapters/hermes_bridge.py")

    def read_sessions(self, threads):
        """返却値は要求したthread_idに厳密に対応する5時間枠率だけ。"""
        now = self.clock()
        if not isinstance(threads, list) or any(not isinstance(t, dict) for t in threads):
            return {}
        ids = [t.get("thread_id") for t in threads]
        if (
            not 1 <= len(ids) <= 100
            or any(not isinstance(i, str) or not i.strip() or len(i) > 512 for i in ids)
            or len(set(ids)) != len(ids)
        ):
            return {}
        if any(
            set(t) != {"thread_id", "created_at", "descendant_thread_ids"}
            or (t["created_at"] is not None and not isinstance(t["created_at"], str))
            or t["descendant_thread_ids"] != []
            for t in threads
        ):
            return {}
        if (
            self._last_sessions_poll is not None
            and now - self._last_sessions_poll < self.poll_interval_seconds
        ):
            return {sid: value for sid, value in self._sessions_cached.items() if sid in ids}
        self._last_sessions_poll = now
        self._sessions_cached = {}
        if not self.auth_helper or not Path(self.auth_helper).is_file():
            return {}
        try:
            result = self.runner(
                [
                    self.hermes_python,
                    str(Path(__file__).with_name("hermes_usage_bridge.py")),
                    "--hermes-root",
                    self.hermes_root,
                    "--auth-helper",
                    self.auth_helper,
                ],
                input=json.dumps({"threads": threads}),
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
                env={**os.environ, "HERMES_HOME": self.hermes_home, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if result.returncode != 0:
                return {}
            payload = json.loads(result.stdout)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"threads"}
                or not isinstance(payload["threads"], list)
            ):
                return {}
            output = {}
            seen = set()
            for row in payload["threads"]:
                if not isinstance(row, dict) or not isinstance(row.get("thread_id"), str):
                    return {}
                sid = row["thread_id"]
                if (
                    sid not in ids
                    or sid in seen
                    or row.get("data_status") not in {"available", "partial", "unavailable"}
                ):
                    return {}
                seen.add(sid)
                if row["data_status"] != "available":
                    continue
                value = row.get("five_hour_limit_percent")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    continue
                try:
                    numeric = float(value)
                except (OverflowError, ValueError):
                    continue
                if math.isfinite(numeric):
                    output[sid] = numeric
            self._sessions_cached = output
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, OverflowError):
            self._sessions_cached = {}
        return dict(self._sessions_cached)

    def read(self):
        now = self.clock()
        if self._last_poll is not None and now - self._last_poll < self.poll_interval_seconds:
            return self._cached
        self._last_poll = now
        self._cached = None
        try:
            result = self.runner(
                [
                    self.agent_resume_bin,
                    "usage",
                    "--agent",
                    "hermes",
                    "--hermes-home",
                    self.hermes_home,
                ],
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            if result.returncode != 0:
                return None
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict) or payload.get("provider") != "openai-codex":
                return None
            windows = payload.get("windows")
            if not isinstance(windows, list):
                return None
            matches = [
                item
                for item in windows
                if isinstance(item, dict) and item.get("label") == "Session"
            ]
            if len(matches) != 1:
                return None
            value = matches[0].get("used_percent")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if not 0 <= value <= 100:
                return None
            if not math.isfinite(value) or not 0 <= value <= 100:
                return None
            self._cached = value
            return value
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, OverflowError):
            return None
