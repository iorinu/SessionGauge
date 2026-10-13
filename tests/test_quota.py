import json
import subprocess
from types import SimpleNamespace

import pytest

from sessiongauge.quota import QuotaReader


def response(value=57):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "provider": "openai-codex",
                "account_key": "private",
                "windows": [{"label": "Session", "used_percent": value}],
            }
        ),
        stderr="secret",
    )


@pytest.mark.parametrize("value", [0, 100, 57.25])
def test_reads_valid_session_quota(value):
    reader = QuotaReader(runner=lambda *args, **kwargs: response(value))
    assert reader.read() == value


def test_caches_and_refreshes_by_fake_clock():
    now = [0]
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return response(len(calls))

    reader = QuotaReader(runner=run, clock=lambda: now[0], poll_interval_seconds=30)
    assert reader.read() == 1
    now[0] = 29
    assert reader.read() == 1
    now[0] = 30
    assert reader.read() == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "value", [True, "57", float("nan"), float("inf"), -1, 101, 10**400, -(10**400)]
)
def test_rejects_invalid_percent(value):
    assert QuotaReader(runner=lambda *args, **kwargs: response(value)).read() is None


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "other", "windows": []},
        {"provider": "openai-codex", "windows": []},
        {
            "provider": "openai-codex",
            "windows": [
                {"label": "Session", "used_percent": 1},
                {"label": "Session", "used_percent": 2},
            ],
        },
    ],
)
def test_rejects_wrong_provider_or_window(payload):
    result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    assert QuotaReader(runner=lambda *args, **kwargs: result).read() is None


@pytest.mark.parametrize(
    "stdout",
    [
        "{",
        "[]",
        "null",
        '{"provider":"openai-codex","windows":null}',
        '{"provider":"openai-codex","windows":[{"label":"Session","used_percent":57}]} trailing',
    ],
)
def test_rejects_malformed_or_invalid_json_payloads(stdout):
    result = SimpleNamespace(returncode=0, stdout=stdout, stderr="private output")
    assert QuotaReader(runner=lambda *args, **kwargs: result).read() is None


def test_invokes_helper_with_argv_timeout_and_captured_output_without_shell():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return response()

    assert QuotaReader("/custom/agent-resume", "/home/hermes", runner=run).read() == 57
    args, kwargs = calls[0]
    assert args == [
        "/custom/agent-resume",
        "usage",
        "--agent",
        "hermes",
        "--hermes-home",
        "/home/hermes",
    ]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 35, "check": False}
    assert "shell" not in kwargs


def test_timeout_is_a_failure_and_output_is_not_logged(caplog):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output="secret stdout", stderr="secret stderr"
        )

    assert QuotaReader(runner=timeout).read() is None
    assert "secret stdout" not in caplog.text
    assert "secret stderr" not in caplog.text


def test_failure_discards_cache_and_rate_limits_retry():
    now = [0]
    results = [
        response(50),
        SimpleNamespace(returncode=1, stdout="private", stderr="secret"),
        response(70),
    ]
    reader = QuotaReader(
        runner=lambda *args, **kwargs: results.pop(0),
        clock=lambda: now[0],
        poll_interval_seconds=30,
    )
    assert reader.read() == 50
    now[0] = 30
    assert reader.read() is None
    now[0] = 31
    assert reader.read() is None
    now[0] = 60
    assert reader.read() == 70
