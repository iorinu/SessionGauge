"""herdr sidebar metadata reporter; metadataは明示的に有効化された場合だけ送る。"""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sessiongauge.adapters.herdr import HerdrAdapter
from sessiongauge.quota import QuotaReader


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
    ):
        self.enabled = enabled
        self.ttl_ms = ttl_ms
        self.runner = runner or subprocess.run
        self.adapter = adapter or HerdrAdapter()
        self.source = source or f"sessiongauge:{uuid.uuid4()}"
        self.warning = warning or (lambda message: print(f"Warning: {message}", file=sys.stderr))
        self._capability = None
        self._unsupported_warned = False
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
        if not self.enabled or data.get("stale") or not self._supports():
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
        quota = self.quota_reader.read() if self.quota_enabled and quota_targets else None
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
            token = str(tokens)
            if key in quota_targets:
                token += f" · 5h {quota:g}%" if quota is not None else " · 5h N/A"
            if self._send(key[1], token=token):
                self._owned[key] = sid

    def _clear_all(self):
        for key in list(self._owned):
            self._send(key[1], clear=True)
            self._owned.pop(key, None)

    def close(self):
        if self.enabled:
            self._clear_all()
