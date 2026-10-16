"""Hermes Agent内の認証経路を使う、最小限のthread usageブリッジ。"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _safe_threads(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"threads"}
        or not isinstance(value["threads"], list)
    ):
        raise ValueError
    threads = value["threads"]
    ids = []
    for thread in threads:
        if not isinstance(thread, dict) or set(thread) != {
            "thread_id",
            "created_at",
            "descendant_thread_ids",
        }:
            raise ValueError
        sid = thread["thread_id"]
        created = thread["created_at"]
        descendants = thread["descendant_thread_ids"]
        if (
            not isinstance(sid, str)
            or not sid.strip()
            or len(sid) > 512
            or (created is not None and not isinstance(created, str))
            or descendants != []
        ):
            raise ValueError
        ids.append(sid)
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError
    return threads, set(ids)


def _valid_percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0


def _audit_guard(event, args):
    if event != "open" or not args:
        return
    if isinstance(args[0], int):
        return
    try:
        path = Path(os.path.expanduser(os.fsdecode(args[0])))
        if not path.is_absolute():
            raise PermissionError("access to protected path denied")
        parts = path.parts
        protected = lambda values: (
            ".ssh" in values or any(part == ".env" or part.startswith(".env.") for part in values)
        )
        if protected(parts):
            raise PermissionError("access to protected path denied")
        if protected(path.resolve(strict=False).parts):
            raise PermissionError("access to protected path denied")
    except PermissionError:
        raise
    except Exception:  # noqa: BLE001 - 解決失敗時はfail-closed
        raise PermissionError("access to protected path denied")


def _request(root, helper, threads, include_metadata=False, include_window=False):
    root = str(Path(root).resolve())
    helper = Path(helper).resolve()
    sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("sessiongauge_auth_bridge", helper)
    if spec is None or spec.loader is None:
        raise ValueError
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 認証情報は既存のHermes連携モジュールだけから取得し、値は絶対に出力しない。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        token, base_url, account_id = module.credentials()
    usage_module = __import__("agent.account_usage", fromlist=["_resolve_codex_usage_url"])
    usage_url = usage_module._resolve_codex_usage_url(base_url).rstrip("/")
    url = usage_url + "/thread_usage/query_v2"
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"chatgpt.com", "api.openai.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
        **({"ChatGPT-Account-Id": account_id} if account_id else {}),
    }
    response = httpx.post(
        url,
        json={"threads": threads},
        headers=headers,
        timeout=10 if include_window else 20,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("threads") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise TypeError
    output, seen = [], set()
    allowed_ids = {t["thread_id"] for t in threads}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("thread_id"), str)
            or row["thread_id"] not in allowed_ids
            or row["thread_id"] in seen
        ):
            raise ValueError
        seen.add(row["thread_id"])
        status = row.get("data_status")
        percent = row.get("five_hour_limit_percent") if status == "available" else None
        output.append(
            {
                "thread_id": row["thread_id"],
                "data_status": status,
                "five_hour_limit_percent": percent if _valid_percent(percent) else None,
            }
        )
    stamp = payload.get("data_as_of")
    safe_stamp = stamp if isinstance(stamp, str) and len(stamp) <= 128 else None
    if include_window:
        account_response = httpx.get(usage_url, headers=headers, timeout=10, follow_redirects=False)
        account_response.raise_for_status()
        account_data = account_response.json()
        if not isinstance(account_data, dict):
            raise ValueError
        response_account = account_data.get("account_id")
        plan = account_data.get("plan_type")
        if (
            not isinstance(response_account, str)
            or not response_account.strip()
            or not isinstance(plan, str)
            or not plan.strip()
            or (account_id and account_id != response_account)
        ):
            raise ValueError
        rate_limit = account_data.get("rate_limit")
        if not isinstance(rate_limit, dict):
            raise ValueError
        primary = rate_limit.get("primary_window")
        if not isinstance(primary, dict):
            raise ValueError
        reset = primary.get("reset_at")
        duration = primary.get("limit_window_seconds")
        if (
            isinstance(reset, bool)
            or not isinstance(reset, (int, float))
            or not math.isfinite(float(reset))
            or isinstance(duration, bool)
            or duration != 18000
        ):
            raise ValueError
        end = float(reset)
        start = end - 18000
        import time

        now = time.time()
        if not (start <= now < end):
            raise ValueError
        return {
            "threads": output,
            "data_as_of": safe_stamp,
            "window": {"start": start, "end": end},
            "account_scope": hashlib.sha256((response_account + "\0" + plan).encode()).hexdigest(),
        }
    result = {"threads": output}
    if include_metadata and safe_stamp is not None:
        result["data_as_of"] = safe_stamp
    return result


def main(argv=None):
    sys.addaudithook(_audit_guard)
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", required=True)
    parser.add_argument("--auth-helper", required=True)
    parser.add_argument("--include-metadata", action="store_true")
    parser.add_argument("--include-window", action="store_true")
    args = parser.parse_args(argv)
    try:
        input_value = json.load(sys.stdin)
        threads, _ = _safe_threads(input_value)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if args.include_window:
                result = _request(
                    args.hermes_root, args.auth_helper, threads, args.include_metadata, True
                )
            else:
                result = _request(
                    args.hermes_root, args.auth_helper, threads, args.include_metadata
                )
        sys.stdout.write(json.dumps(result, allow_nan=False))
        return 0
    except Exception:  # noqa: BLE001 - CLI境界では詳細を漏らさず失敗を隠す
        sys.stdout.write('{"error":"unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
