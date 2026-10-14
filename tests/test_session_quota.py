import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessiongauge.quota import QuotaReader


def request(*ids):
    return [{"thread_id": sid, "created_at": None, "descendant_thread_ids": []} for sid in ids]


def reader_for(tmp_path, payload, *, clock=lambda: 0, poll=60):
    helper = tmp_path / "hermes_bridge.py"
    helper.touch()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="private")

    instance = QuotaReader(runner=run, clock=clock, poll_interval_seconds=poll, auth_helper=helper)
    return instance, calls


def test_reads_percentages_by_exact_id_when_response_order_differs(tmp_path):
    instance, calls = reader_for(
        tmp_path,
        {
            "threads": [
                {
                    "thread_id": "b",
                    "data_status": "available",
                    "five_hour_limit_percent": 113.58442524,
                },
                {"thread_id": "a", "data_status": "available", "five_hour_limit_percent": 1.08},
            ]
        },
    )
    assert instance.read_sessions(request("a", "b")) == {"b": 113.58442524, "a": 1.08}
    args, kwargs = calls[0]
    assert args[-4:] == [
        "--hermes-root",
        str(instance.hermes_root),
        "--auth-helper",
        str(instance.auth_helper),
    ]
    assert kwargs["input"] == json.dumps({"threads": request("a", "b")})
    assert kwargs["timeout"] == 35 and kwargs["capture_output"] and "shell" not in kwargs
    assert kwargs["env"]["HERMES_HOME"] == instance.hermes_home
    assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_default_python_is_from_hermes_root_and_helper_wiring(tmp_path):
    root = tmp_path / "custom-hermes"
    instance = QuotaReader(hermes_root=root, auth_helper=tmp_path / "helper.py")
    assert Path(instance.hermes_python) == Path(instance.hermes_root) / "venv/bin/python"


@pytest.mark.parametrize(
    "status,value", [("partial", 42), ("unavailable", None), ("available", None)]
)
def test_missing_or_nonavailable_is_unknown(tmp_path, status, value):
    instance, _ = reader_for(
        tmp_path,
        {"threads": [{"thread_id": "a", "data_status": status, "five_hour_limit_percent": value}]},
    )
    assert instance.read_sessions(request("a", "b")) == {}


@pytest.mark.parametrize("value", [0, 101.2, True, "2", -1, float("nan"), float("inf"), 10**400])
def test_session_values_fail_closed_or_keep_valid_out_of_range_above_100(tmp_path, value):
    instance, _ = reader_for(
        tmp_path,
        {
            "threads": [
                {"thread_id": "a", "data_status": "available", "five_hour_limit_percent": value}
            ]
        },
    )
    result = instance.read_sessions(request("a"))
    if value == 0:
        assert result == {"a": 0}
    elif value == 101.2:
        assert result == {"a": 101.2}
    else:
        assert result == {}


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"thread_id": "a", "data_status": "available", "five_hour_limit_percent": 1},
            {"thread_id": "a", "data_status": "available", "five_hour_limit_percent": 2},
        ],
        [{"thread_id": "unexpected", "data_status": "available", "five_hour_limit_percent": 1}],
    ],
)
def test_duplicate_and_unexpected_ids_reject_response(tmp_path, rows):
    instance, _ = reader_for(tmp_path, {"threads": rows})
    assert instance.read_sessions(request("a")) == {}


def test_duplicate_unavailable_row_then_available_row_rejects_response(tmp_path):
    instance, _ = reader_for(
        tmp_path,
        {
            "threads": [
                {"thread_id": "a", "data_status": "unavailable", "five_hour_limit_percent": None},
                {"thread_id": "a", "data_status": "available", "five_hour_limit_percent": 12},
            ]
        },
    )
    assert instance.read_sessions(request("a")) == {}


def test_legacy_read_does_not_delay_session_poll(tmp_path):
    helper = tmp_path / "helper.py"
    helper.touch()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "agent-resume":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout='{"threads":[]}', stderr="")

    instance = QuotaReader(runner=run, auth_helper=helper)
    instance.read()
    instance.read_sessions(request("a"))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload", [{"windows": []}, {"threads": None}, [], {"threads": [], "plan": "private"}]
)
def test_malformed_or_global_response_is_rejected(tmp_path, payload):
    instance, _ = reader_for(tmp_path, payload)
    assert instance.read_sessions(request("a")) == {}


def test_cache_is_per_id_and_failure_clears_previous_values(tmp_path):
    helper = tmp_path / "helper.py"
    helper.touch()
    clock = [0]
    responses = [
        SimpleNamespace(
            returncode=0,
            stdout='{"threads":[{"thread_id":"a","data_status":"available","five_hour_limit_percent":73}]}',
            stderr="",
        ),
        SimpleNamespace(returncode=1, stdout="secret", stderr="secret"),
    ]
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    instance = QuotaReader(
        runner=run, clock=lambda: clock[0], auth_helper=helper, poll_interval_seconds=30
    )
    assert instance.read_sessions(request("a")) == {"a": 73}
    assert instance.read_sessions(request("b")) == {}
    clock[0] = 30
    assert instance.read_sessions(request("b")) == {}
    assert instance.read_sessions(request("b")) == {}
    assert len(calls) == 2


def test_session_timeout_is_silent_and_clears_values(tmp_path, caplog):
    helper = tmp_path / "helper.py"
    helper.touch()

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="private", stderr="secret")

    instance = QuotaReader(runner=timeout, auth_helper=helper)
    assert instance.read_sessions(request("sid")) == {}
    assert "private" not in caplog.text and "secret" not in caplog.text
