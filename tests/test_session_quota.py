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


def metadata_payload(stamp, rows=None):
    return {
        "data_as_of": stamp,
        "threads": rows
        if rows is not None
        else [{"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 12.5}],
    }


def test_metadata_subprocess_boundary_returns_only_allowlisted_values(tmp_path):
    instance, calls = reader_for(tmp_path, metadata_payload("2025-01-02T03:04:05+00:00"))
    result = instance.read_session_metadata(request("sid"))
    assert result == {"sid": {"percent": 12.5, "data_as_of": 1735787045.0}}
    args, kwargs = calls[0]
    assert "--include-metadata" in args
    assert kwargs["input"] == json.dumps({"threads": request("sid")})
    assert "tokens" not in kwargs["input"] and "model" not in kwargs["input"]
    assert "tokens" not in calls[0][1]["input"] and "model" not in calls[0][1]["input"]


@pytest.mark.parametrize(
    "stamp",
    [None, "", "not-a-date", "2025-01-02T03:04:05", "9999-01-02T03:04:05Z"],
)
def test_metadata_rejects_missing_or_invalid_timestamp(tmp_path, stamp):
    instance, _ = reader_for(tmp_path, metadata_payload(stamp))
    assert instance.read_session_metadata(request("sid")) == {}


def test_metadata_rejects_future_timestamp(tmp_path):
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    instance, _ = reader_for(tmp_path, metadata_payload(future))
    assert instance.read_session_metadata(request("sid")) == {}


@pytest.mark.parametrize(
    "status,value",
    [("partial", 12.5), ("unavailable", None), ("unknown", 12.5)],
)
def test_metadata_rejects_nonavailable_and_unknown_status(tmp_path, status, value):
    row = {"thread_id": "sid", "data_status": status, "five_hour_limit_percent": value}
    instance, _ = reader_for(tmp_path, metadata_payload("2025-01-02T03:04:05Z", [row]))
    assert instance.read_session_metadata(request("sid")) == {}


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 1},
            {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 2},
        ],
        [{"thread_id": "other", "data_status": "available", "five_hour_limit_percent": 1}],
    ],
)
def test_metadata_rejects_duplicate_or_unexpected_ids(tmp_path, rows):
    instance, _ = reader_for(tmp_path, metadata_payload("2025-01-02T03:04:05Z", rows))
    assert instance.read_session_metadata(request("sid")) == {}


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "[]",
        '{"threads": []}',
        '{"data_as_of":"2025-01-02T03:04:05Z","threads":[],"tokens":"private"}',
    ],
)
def test_metadata_rejects_malformed_subprocess_output_without_leaking(
    tmp_path, caplog, capsys, stdout
):
    helper = tmp_path / "helper.py"
    helper.touch()
    calls = []

    def run(args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="private-secret")

    instance = QuotaReader(runner=run, auth_helper=helper)
    assert instance.read_session_metadata(request("sid")) == {}
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "private" not in captured.out and "private-secret" not in captured.out
    assert "private" not in captured.err and "private-secret" not in captured.err
    assert "private" not in caplog.text and "private-secret" not in caplog.text


def test_metadata_timeout_does_not_leak_output(tmp_path, caplog):
    helper = tmp_path / "helper.py"
    helper.touch()

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(
            args, kwargs["timeout"], output="private-output", stderr="private-log"
        )

    instance = QuotaReader(runner=timeout, auth_helper=helper)
    assert instance.read_session_metadata(request("sid")) == {}
    assert "private-output" not in caplog.text and "private-log" not in caplog.text


@pytest.mark.parametrize(
    "threads",
    [
        request(" "),
        [{"thread_id": 1, "created_at": None, "descendant_thread_ids": []}],
        [{"thread_id": [], "created_at": None, "descendant_thread_ids": []}],
        request("x" * 513),
        request("sid", "sid"),
        [{"thread_id": "sid", "created_at": None, "descendant_thread_ids": [], "extra": 1}],
        [{"thread_id": "sid", "created_at": 1, "descendant_thread_ids": []}],
        [{"thread_id": "sid", "created_at": None, "descendant_thread_ids": ["child"]}],
        request(*(f"sid-{n}" for n in range(101))),
    ],
)
def test_metadata_rejects_invalid_requests_before_runner(tmp_path, threads):
    instance, calls = reader_for(tmp_path, metadata_payload("2025-01-02T03:04:05Z"))
    assert instance.read_session_metadata(threads) == {}
    assert calls == []


@pytest.mark.parametrize("percent", [0, 101.2])
def test_metadata_accepts_finite_nonnegative_percent(tmp_path, percent):
    row = {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": percent}
    instance, _ = reader_for(tmp_path, metadata_payload("2025-01-02T03:04:05Z", [row]))
    assert instance.read_session_metadata(request("sid"))["sid"]["percent"] == percent


def test_metadata_poll_is_independent_from_legacy_session_poll(tmp_path):
    helper = tmp_path / "helper.py"
    helper.touch()
    clock = [0]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "--include-metadata" in args:
            output = metadata_payload("2025-01-02T03:04:05Z")
        else:
            output = {"threads": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")

    instance = QuotaReader(runner=run, clock=lambda: clock[0], auth_helper=helper)
    instance.read_sessions(request("sid"))
    instance.read_session_metadata(request("sid"))
    assert len(calls) == 2
    clock[0] = 1
    instance.read_sessions(request("sid"))
    instance.read_session_metadata(request("sid"))
    assert len(calls) == 2
