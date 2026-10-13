"""agent-resumeを介したHermes account quotaの読み取り。"""

import json
import math
import subprocess
import time


class QuotaReader:
    def __init__(
        self,
        agent_resume_bin="agent-resume",
        hermes_home="~/.hermes",
        poll_interval_seconds=60,
        *,
        runner=None,
        clock=None,
    ):
        self.agent_resume_bin = agent_resume_bin
        self.hermes_home = hermes_home
        self.poll_interval_seconds = poll_interval_seconds
        self.runner = runner or subprocess.run
        self.clock = clock or time.monotonic
        self._last_poll = None
        self._cached = None

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
