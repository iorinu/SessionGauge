import hashlib
import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sessiongauge import hermes_usage_bridge as bridge
from sessiongauge.quota import QuotaReader


def request(*ids):
    return [{"thread_id": sid, "created_at": None, "descendant_thread_ids": []} for sid in ids]


def test_bridge_estimate_mode_uses_same_url_headers_and_returns_opaque_scope(tmp_path, monkeypatch):
    now = datetime.now(UTC).timestamp()
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def credentials(): return ('token','https://chatgpt.com','account-secret')\n"
    )
    monkeypatch.setitem(__import__("sys").modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        __import__("sys").modules,
        "agent.account_usage",
        SimpleNamespace(_resolve_codex_usage_url=lambda base: base + "/usage"),
    )
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return Response(
            {
                "threads": [
                    {
                        "thread_id": "sid",
                        "data_status": "available",
                        "five_hour_limit_percent": 12.5,
                    }
                ],
                "data_as_of": "2026-10-15T12:00:00Z",
                "used_percent": 91,
            }
        )

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response(
            {
                "account_id": "account-secret",
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"reset_at": now + 1800, "limit_window_seconds": 18000}
                },
                "email": "private@example.test",
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "httpx", SimpleNamespace(post=post, get=get))
    result = bridge._request(tmp_path, helper, request("sid"), include_window=True)
    assert [call[0] for call in calls] == ["post", "get"]
    assert calls[0][1] == "https://chatgpt.com/usage/thread_usage/query_v2"
    assert calls[1][1] == "https://chatgpt.com/usage"
    assert calls[0][2]["headers"] == calls[1][2]["headers"]
    assert calls[1][2]["follow_redirects"] is False and calls[1][2]["timeout"] == 10
    assert result["account_scope"] == hashlib.sha256(b"account-secret\0plus").hexdigest()
    assert set(result) == {"threads", "data_as_of", "window", "account_scope"}
    assert result["window"] == {
        "start": pytest.approx(now - 16200, rel=0, abs=1e-6),
        "end": pytest.approx(now + 1800, rel=0, abs=1e-6),
    }
    assert all(
        secret not in json.dumps(result)
        for secret in ("account-secret", "private@example.test", "used_percent")
    )


def test_legacy_bridge_mode_does_not_issue_get(tmp_path, monkeypatch):
    helper = tmp_path / "helper.py"
    helper.write_text("def credentials(): return ('t','https://chatgpt.com','a')\n")
    monkeypatch.setitem(__import__("sys").modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        __import__("sys").modules,
        "agent.account_usage",
        SimpleNamespace(_resolve_codex_usage_url=lambda base: base),
    )
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"threads": []}

    monkeypatch.setitem(
        __import__("sys").modules,
        "httpx",
        SimpleNamespace(
            post=lambda *a, **k: calls.append("post") or Response(),
            get=lambda *a, **k: calls.append("get"),
        ),
    )
    assert bridge._request(tmp_path, helper, request("sid")) == {"threads": []}
    assert calls == ["post"]


@pytest.mark.parametrize(
    "account_data",
    [
        {"account_id": "account-secret", "plan_type": "plus"},
        {"account_id": "account-secret", "plan_type": "plus", "rate_limit": []},
        {
            "account_id": "account-secret",
            "plan_type": "plus",
            "rate_limit": {},
        },
        {
            "account_id": "account-secret",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"reset_at": "not-a-timestamp", "limit_window_seconds": 18000}
            },
        },
        {
            "account_id": "account-secret",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"reset_at": 9999999999, "limit_window_seconds": 3600}
            },
        },
        {
            "account_id": "other-account",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"reset_at": 9999999999, "limit_window_seconds": 18000}
            },
        },
        {
            "account_id": " ",
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"reset_at": 9999999999, "limit_window_seconds": 18000}
            },
        },
        {
            "account_id": "account-secret",
            "plan_type": " ",
            "rate_limit": {
                "primary_window": {"reset_at": 9999999999, "limit_window_seconds": 18000}
            },
        },
    ],
    ids=[
        "missing-rate-limit",
        "non-dict-rate-limit",
        "missing-primary-window",
        "bad-reset-timestamp",
        "bad-window-duration",
        "account-mismatch",
        "blank-account",
        "blank-plan",
    ],
)
def test_bridge_rejects_invalid_account_usage_window(tmp_path, monkeypatch, account_data):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def credentials(): return ('token','https://chatgpt.com','account-secret')\n"
    )
    monkeypatch.setitem(__import__("sys").modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        __import__("sys").modules,
        "agent.account_usage",
        SimpleNamespace(_resolve_codex_usage_url=lambda base: base + "/usage"),
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    monkeypatch.setitem(
        __import__("sys").modules,
        "httpx",
        SimpleNamespace(
            post=lambda *a, **k: Response({"threads": []}),
            get=lambda *a, **k: Response(account_data),
        ),
    )
    with pytest.raises(ValueError):
        bridge._request(tmp_path, helper, request("sid"), include_window=True)


def test_estimate_reader_validates_envelope_and_filters_samples(tmp_path):
    now = datetime.now(UTC).timestamp()
    helper = tmp_path / "helper.py"
    helper.touch()
    payload = {
        "threads": [
            {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 12.5},
        ],
        "data_as_of": datetime.fromtimestamp(now - 1, UTC).isoformat(),
        "window": {"start": now - 10000, "end": now + 8000},
        "account_scope": "a" * 64,
    }
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="secret")

    reader = QuotaReader(runner=run, auth_helper=helper)
    assert reader.read_estimate_context(request("sid")) == {
        "samples": {"sid": {"percent": 12.5, "data_as_of": pytest.approx(now - 1)}},
        "window_start": pytest.approx(now - 10000, rel=0, abs=1e-6),
        "window_end": pytest.approx(now + 8000, rel=0, abs=1e-6),
        "account_scope": "a" * 64,
    }
    assert "--include-window" in calls[0][0] and calls[0][1]["timeout"] == 35


def test_estimate_reader_bad_window_or_spoofed_id_fails_closed(tmp_path):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    payload = {
        "threads": [
            {"thread_id": "spoof", "data_status": "available", "five_hour_limit_percent": 1}
        ],
        "data_as_of": datetime.now(UTC).isoformat(),
        "window": {"start": now - 18000, "end": now},
        "account_scope": "b" * 64,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    assert reader.read_estimate_context(request("sid")) == {}


@pytest.mark.parametrize(
    "row",
    [
        {"thread_id": "spoof", "data_status": "available", "five_hour_limit_percent": 1},
        {"thread_id": "sid", "data_status": "unknown", "five_hour_limit_percent": 1},
    ],
    ids=["spoofed-id", "unknown-status"],
)
def test_estimate_reader_rejects_individually_invalid_rows(tmp_path, row):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    payload = {
        "threads": [row],
        "data_as_of": datetime.fromtimestamp(now - 1, UTC).isoformat(),
        "window": {"start": now - 10000, "end": now + 8000},
        "account_scope": "a" * 64,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    assert reader.read_estimate_context(request("sid")) == {}


def test_estimate_reader_rejects_duplicate_rows(tmp_path):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    row = {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 1}
    payload = {
        "threads": [row, row],
        "data_as_of": datetime.fromtimestamp(now - 1, UTC).isoformat(),
        "window": {"start": now - 10000, "end": now + 8000},
        "account_scope": "a" * 64,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    assert reader.read_estimate_context(request("sid")) == {}


@pytest.mark.parametrize("scope", ["", "x" * 64, None])
def test_estimate_reader_rejects_malformed_scope(tmp_path, scope):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    payload = {
        "threads": [{"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 1}],
        "data_as_of": datetime.fromtimestamp(now - 1, UTC).isoformat(),
        "window": {"start": now - 10000, "end": now + 8000},
        "account_scope": scope,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    assert reader.read_estimate_context(request("sid")) == {}


@pytest.mark.parametrize(
    "window",
    [
        {"start": 1, "end": 2},
        {"start": "bad", "end": 18000},
        {"start": 100, "end": 18100},
        {"start": 1, "end": 18001},
    ],
)
def test_estimate_reader_rejects_invalid_window_separately(tmp_path, window):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    payload = {
        "threads": [{"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 1}],
        "data_as_of": datetime.fromtimestamp(now - 1, UTC).isoformat(),
        "window": window,
        "account_scope": "a" * 64,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    assert reader.read_estimate_context(request("sid")) == {}


@pytest.mark.parametrize("stamp", [None, "9999-01-01T00:00:00Z"])
def test_estimate_reader_keeps_window_but_drops_samples_for_missing_or_future_as_of(
    tmp_path, stamp
):
    helper = tmp_path / "helper.py"
    helper.touch()
    now = datetime.now(UTC).timestamp()
    payload = {
        "threads": [{"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 1}],
        "data_as_of": stamp,
        "window": {"start": now - 10000, "end": now + 8000},
        "account_scope": "a" * 64,
    }
    reader = QuotaReader(
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
        auth_helper=helper,
    )
    result = reader.read_estimate_context(request("sid"))
    assert result["samples"] == {}
    assert result["window_start"] == pytest.approx(now - 10000, rel=0, abs=1e-6)
    assert result["window_end"] == pytest.approx(now + 8000, rel=0, abs=1e-6)


def test_estimate_reader_timeout_is_silent(tmp_path, caplog):
    helper = tmp_path / "helper.py"
    helper.touch()

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="private", stderr="secret")

    reader = QuotaReader(runner=timeout, auth_helper=helper)
    assert reader.read_estimate_context(request("sid")) == {}
    assert "private" not in caplog.text and "secret" not in caplog.text
