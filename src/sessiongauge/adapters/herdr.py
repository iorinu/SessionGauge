import json
import subprocess
from urllib.parse import urlparse

from sessiongauge.domain import Pane


class HerdrAdapter:
    @staticmethod
    def parse(payload, *, endpoint: str) -> list[Pane]:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise TypeError("invalid herdr envelope")
        result = payload["result"]
        if result.get("type") != "pane_list" or not isinstance(result.get("panes"), list):
            raise ValueError("unexpected herdr result type")
        panes = []
        for raw in result["panes"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("pane_id"), str):
                raise TypeError("invalid pane")
            ref = raw.get("agent_session")
            sid = None
            if (
                raw.get("agent") == "hermes"
                and isinstance(ref, dict)
                and ref.get("agent") == "hermes"
                and ref.get("kind") == "id"
                and ref.get("source") == "herdr:hermes"
                and isinstance(ref.get("value"), str)
                and ref["value"]
            ):
                sid = ref["value"]
            panes.append(
                Pane(
                    endpoint,
                    raw["pane_id"],
                    raw.get("terminal_id"),
                    raw.get("agent"),
                    raw.get("agent_status"),
                    raw.get("cwd"),
                    raw.get("title") or raw.get("label") or raw.get("terminal_title_stripped"),
                    sid,
                )
            )
        return panes

    def list_panes(self) -> list[Pane]:
        proc = subprocess.run(
            ["herdr", "pane", "list"], check=True, capture_output=True, text=True, timeout=10
        )
        import os

        endpoint = os.environ.get("HERDR_SOCKET_PATH")
        if not endpoint:
            # Herdrのstatusは読み取り専用のため、ローカルendpoint identityを推測しない。
            status = subprocess.run(
                ["herdr", "status", "--json"], check=True, capture_output=True, text=True, timeout=5
            )
            try:
                endpoint = json.loads(status.stdout).get("socket_path")
            except (ValueError, AttributeError):
                endpoint = None
        endpoint = str(endpoint or "herdr:unknown")
        return self.parse(proc.stdout, endpoint=endpoint)


def endpoint_identity(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
