"""Quota読取のclose時に子プロセスと推定ジョブを停止できることを検証する。"""

import os
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Thread

import pytest

from sessiongauge.quota import QuotaReader
from sessiongauge.sidebar import SidebarReporter

THREADS = [{"thread_id": "s", "created_at": None, "descendant_thread_ids": []}]


@pytest.mark.parametrize("failure", [None, "timeout", "error"])
def test_default_runner_success_and_cleanup(monkeypatch, failure):
    events = []

    class Child:
        returncode = None

        def __init__(self, args, **kwargs):
            assert kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
            self.args = args

        def communicate(self, input=None, timeout=None):
            assert timeout == 35
            if failure == "timeout":
                raise subprocess.TimeoutExpired(self.args, timeout)
            if failure == "error":
                raise OSError("isolated failure")
            self.returncode = 0
            return (
                '{"provider":"openai-codex","windows":[{"label":"Session","used_percent":12}]}',
                "",
            )

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            assert timeout is not None and 0 < timeout <= 1
            events.append("wait")
            if self.returncode is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", Child)
    reader = QuotaReader()
    assert reader.read() == (12 if failure is None else None)
    reader.close()
    assert events == ([] if failure is None else ["terminate", "wait", "kill", "wait"])
    assert reader.read() is None


def test_close_before_request_rejects_all_reads_and_cached_values(tmp_path):
    calls = []
    helper = tmp_path / "helper"
    helper.touch()
    reader = QuotaReader(runner=lambda *a, **k: calls.append(a), auth_helper=str(helper))
    reader._cached = 12
    reader._sessions_cached = {"s": 12}
    reader._last_poll = reader._last_sessions_poll = reader.clock()
    reader.close()
    reader.close()
    assert reader.read() is None
    assert reader.read_sessions(THREADS) == {}
    assert reader.read_session_metadata(THREADS) == {}
    assert not calls


@pytest.mark.parametrize("submit_fails", [False, True])
def test_close_during_submit_signals_reader_before_shutdown(submit_fails):
    entered, release, closed = Event(), Event(), Event()
    errors, events = [], []
    future = Future()

    class Reader:
        def read_session_metadata(self, threads):
            return {}

        def close(self):
            events.append("reader")
            closed.set()

    class Executor:
        def submit(self, *args):
            entered.set()
            assert release.wait(2)
            if submit_fails:
                raise RuntimeError("executor already shut down")
            return future

        def shutdown(self, **kwargs):
            events.append("executor")

    reporter = SidebarReporter(quota_reader=Reader())
    reporter._quota_executor.shutdown()
    reporter._quota_executor = Executor()

    def collect():
        try:
            reporter._collect_estimates(THREADS, {"k": {"model": "m"}}, {"k": ("s", 100)}, {"k"})
        except (RuntimeError, AssertionError) as exc:
            errors.append(exc)

    worker = Thread(target=collect)
    closer = Thread(target=reporter.close)
    worker.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert closed.wait(1), "submit must not hold the lifecycle lock"
    finally:
        release.set()
        worker.join(2)
        if closer.ident is not None:
            closer.join(2)
        reporter.close()
    assert not worker.is_alive() and not closer.is_alive()
    assert not errors
    assert events == ["reader", "executor"]
    assert reporter._quota_future is None
    assert submit_fails or future.cancelled()


@pytest.mark.parametrize("method", ["read", "read_sessions", "read_session_metadata"])
def test_injected_runner_late_result_is_discarded(tmp_path, method):
    entered, release = Event(), Event()
    helper = tmp_path / "helper"
    helper.touch()
    results = []

    def runner(args, **kwargs):
        entered.set()
        assert release.wait(2)
        if method == "read":
            payload = (
                '{"provider":"openai-codex","windows":[{"label":"Session","used_percent":12}]}'
            )
        else:
            payload = '{"threads":[{"thread_id":"s","data_status":"available","five_hour_limit_percent":12}]'
            if method == "read_session_metadata":
                payload += ',"data_as_of":"2026-01-01T00:00:00Z"'
            payload += "}"
        return subprocess.CompletedProcess(args, 0, payload, "")

    reader = QuotaReader(runner=runner, auth_helper=str(helper))

    def read():
        results.append(getattr(reader, method)(*(() if method == "read" else (THREADS,))))

    worker = Thread(target=read)
    worker.start()
    try:
        assert entered.wait(2)
        reader.close()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results == [None if method == "read" else {}]
    assert reader._cached is None
    assert reader._sessions_cached == {}


def test_close_stops_owned_child_and_prevents_late_estimate_publication(tmp_path):
    """外側の監視プロセスも含め、失敗時にテスト自身の子だけを回収する。"""
    root = Path(__file__).resolve().parents[1]
    started = tmp_path / "child-started"
    pid_file = tmp_path / "child.pid"
    child_script = tmp_path / "hermes_usage_bridge.py"
    child_script.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"pathlib.Path({str(started)!r}).write_text('started')\n"
        "time.sleep(35)\n"
        'print(\'{"threads": [], "data_as_of": "2026-01-01T00:00:00+00:00"}\')\n',
        encoding="utf-8",
    )
    worker = textwrap.dedent(
        f"""
        import json, os, pathlib, subprocess, sys, time
        from sessiongauge import quota
        from sessiongauge.quota import QuotaReader
        from sessiongauge.sidebar import SidebarReporter

        quota.__file__ = {str(child_script)!r}
        helper = pathlib.Path({str(tmp_path / "auth-helper")!r})
        helper.write_text('isolated test placeholder')
        reader = QuotaReader(hermes_python=sys.executable, auth_helper=str(helper))
        reporter = SidebarReporter(quota_enabled=True, quota_mode='estimate', quota_reader=reader)
        threads = [{{'thread_id': 'test-session', 'created_at': None, 'descendant_thread_ids': []}}]
        result = []
        reporter._quota_future = reporter._quota_executor.submit(reader.read_session_metadata, threads)
        deadline = time.monotonic() + 3
        while not pathlib.Path({str(started)!r}).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pathlib.Path({str(started)!r}).exists(), 'bridge child did not start'
        child_pid = int(pathlib.Path({str(pid_file)!r}).read_text())
        reporter.close()
        reporter.close()
        assert reporter._quota_future is not None
        deadline = time.monotonic() + 2
        while not reporter._quota_future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reporter._quota_future.cancelled() or reporter._quota_future.done()
        if reporter._quota_future.done() and not reporter._quota_future.cancelled():
            result.append(reporter._quota_future.result())
        assert not result or result == [{{}}], 'late metadata result was published'
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('owned bridge child is still alive after close')
        # close後に推定収集を呼んでも新しいrequestをsubmitしない。
        calls = []
        reporter._quota_executor.submit = lambda *args, **kwargs: calls.append(args)
        reporter._collect_estimates(threads, {{}}, {{}}, set())
        assert not calls, 'request launched after close'
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        raise AssertionError("close did not make the isolated worker exit promptly")
    finally:
        # timeout以外の失敗でも、テスト専用の子孫をグループごと回収する。
        if process.returncode != 0:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)
    assert process.returncode == 0, f"worker failed:\n{stdout}\n{stderr}"
