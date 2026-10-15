import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessiongauge.domain import Pane
from sessiongauge.sidebar import SidebarReporter


class Runner:
    def __init__(self, responses=None):
        self.commands = []
        self.responses = responses or []

    def __call__(self, args, **kwargs):
        self.commands.append(args)
        if args[-1:] == ["--help"]:
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "report-metadata --source --token --seq --ttl-ms",
                    "stderr": "",
                },
            )()
        if args[-2:] == ["schema", "--json"]:
            schema = json.loads(Path("tests/fixtures/herdr_metadata_schema.json").read_text())
            return type(
                "Result", (), {"returncode": 0, "stdout": json.dumps(schema), "stderr": ""}
            )()
        response = self.responses.pop(0) if self.responses else {"result": {"type": "ok"}}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(response), "stderr": ""})()


class Adapter:
    def list_panes(self):
        return [Pane("endpoint", "p1", "t1", "hermes", "running", None, None, "s")]


class BothPanesAdapter:
    def list_panes(self):
        return [
            Pane("endpoint", "p1", "t1", "hermes", "running", None, None, "s"),
            Pane("endpoint", "p2", "t2", "hermes", "running", None, None, "s2"),
        ]


class SilentResultRunner(Runner):
    def __init__(self, *, returncode=0, stdout=""):
        super().__init__()
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, args, **kwargs):
        if args[-1:] == ["--help"] or args[-2:] == ["schema", "--json"]:
            return super().__call__(args, **kwargs)
        self.commands.append(args)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def test_disabled_makes_no_commands():
    runner = Runner()
    reporter = SidebarReporter(enabled=False, runner=runner)
    reporter.publish({})
    reporter.close()
    assert runner.commands == []


def test_quota_accuracy_warning_is_once_and_only_when_estimate_quota_is_used():
    from test_estimate_lifecycle import ManualExecutor

    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))
    warnings = []

    class Quota:
        calls = 0

        def read_session_metadata(self, threads):
            self.calls += 1
            return {}

    quota = Quota()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_mode="estimate",
        quota_reader=quota,
        runner=Runner(),
        adapter=Adapter(),
        warning=warnings.append,
    )
    reporter._quota_executor.shutdown()
    reporter._quota_executor = ManualExecutor()
    row = {
        "pane_id": "p1",
        "terminal_id": "t1",
        "status": "matched",
        "profile": profile,
        "session": {"id": "s", "total_tokens": 100, "model": "m"},
    }
    try:
        reporter.publish({"panes": [row]})
        reporter.publish({"panes": [row]})
        assert len(reporter._quota_executor.jobs) == 1
        reporter._quota_executor.jobs[0].complete()
        reporter.publish({"panes": [row]})
        assert len(warnings) == 1
        assert "accounting window, denominator, and token weighting are unknown" in warnings[0]
        assert "percent_tokens" in warnings[0] and "raw task budget" in warnings[0]
        assert quota.calls == 1
    finally:
        reporter.close()


@pytest.mark.parametrize("enabled,quota_enabled", [(False, True), (True, False)])
def test_disabled_quota_or_reporting_has_no_accuracy_diagnostic_or_request(enabled, quota_enabled):
    warnings = []

    class Quota:
        def read_session_metadata(self, threads):
            raise AssertionError("disabled quota must not make a request")

    reporter = SidebarReporter(
        enabled=enabled,
        quota_enabled=quota_enabled,
        quota_mode="estimate",
        quota_reader=Quota(),
        runner=Runner(),
        adapter=Adapter(),
        warning=warnings.append,
    )
    try:
        home = Path("~/.hermes").expanduser()
        profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))
        reporter.publish(
            {
                "panes": [
                    {
                        "pane_id": "p1",
                        "terminal_id": "t1",
                        "status": "matched",
                        "profile": profile,
                        "session": {"id": "s", "total_tokens": 100, "model": "m"},
                    }
                ]
            }
        )
        assert warnings == []
    finally:
        reporter.close()


def test_reports_only_matched_unique_session_and_clears_owned_token():
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        ttl_ms=15000,
        runner=runner,
        adapter=Adapter(),
        source="sessiongauge:test",
        display="percent_tokens",
    )
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "session": {"id": "s", "total_tokens": 42},
            }
        ]
    }
    reporter.publish(data)
    reporter.publish(data)
    writes = [
        command
        for command in runner.commands
        if "report-metadata" in command and "--help" not in command
    ]
    assert writes[0][writes[0].index("--token") + 1] == "sessiongauge_usage=42"
    assert int(writes[1][writes[1].index("--seq") + 1]) > int(
        writes[0][writes[0].index("--seq") + 1]
    )
    assert "--ttl-ms" in writes[0]
    reporter.close()
    clears = [command for command in runner.commands if "--clear-token" in command]
    assert len(clears) == 1
    assert clears[0][clears[0].index("--clear-token") + 1] == "sessiongauge_usage"


@pytest.mark.parametrize("stdout", ["", "  \n\t"])
def test_silent_success_owns_token_and_clears_it_on_close(stdout):
    runner = SilentResultRunner(stdout=stdout)
    reporter = SidebarReporter(enabled=True, runner=runner, adapter=Adapter())
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "session": {"id": "s", "total_tokens": 42},
                }
            ]
        }
    )

    assert reporter._owned == {("endpoint", "p1", "t1"): "s"}
    reporter.close()
    clears = [command for command in runner.commands if "--clear-token" in command]
    assert len(clears) == 1
    write = next(command for command in runner.commands if "--token" in command)
    assert int(clears[0][clears[0].index("--seq") + 1]) > int(write[write.index("--seq") + 1])


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "not json"), (0, '{"result":{"type":"unexpected"}}')],
)
def test_non_success_silent_or_invalid_response_does_not_own_token(returncode, stdout):
    warnings = []
    runner = SilentResultRunner(returncode=returncode, stdout=stdout)
    reporter = SidebarReporter(
        enabled=True, runner=runner, adapter=Adapter(), warning=warnings.append
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "session": {"id": "s", "total_tokens": 42},
                }
            ]
        }
    )

    assert not reporter._owned
    assert warnings == ["herdr metadata request failed; sidebar value will expire"]


def test_unsupported_and_invalid_success_degrade_without_throwing():
    warnings = []
    runner = Runner(responses=[{"result": {"type": "unexpected"}}])
    reporter = SidebarReporter(
        enabled=True, runner=runner, adapter=Adapter(), warning=warnings.append
    )
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "session": {"id": "s", "total_tokens": 1},
            }
        ]
    }
    reporter.publish(data)
    assert warnings == ["herdr metadata request failed; sidebar value will expire"]
    assert not reporter._owned

    class Unsupported:
        def __init__(self):
            self.commands = []

        def __call__(self, args, **kwargs):
            self.commands.append(args)
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    unsupported = Unsupported()
    reporter = SidebarReporter(enabled=True, runner=unsupported, warning=warnings.append)
    reporter.publish({"panes": []})
    assert len(unsupported.commands) == 2
    assert warnings[-1] == "herdr metadata reporting is unsupported; sidebar reporting disabled"


def test_conflict_and_duplicate_sessions_are_not_reported():
    runner = Runner()
    reporter = SidebarReporter(enabled=True, runner=runner, adapter=Adapter())
    rows = [
        {
            "pane_id": pane,
            "terminal_id": "t1",
            "status": "matched",
            "session": {"id": "s", "total_tokens": 9},
        }
        for pane in ("p1", "p2")
    ]
    reporter.publish({"panes": rows})
    assert not [command for command in runner.commands if "--token" in command]


def test_quota_is_only_added_to_unique_matching_target_profile():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        calls = 0

        def read_sessions(self, threads):
            self.calls += 1
            return {thread["thread_id"]: 57 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=BothPanesAdapter(),
        display="percent_tokens",
    )
    rows = [
        {
            "pane_id": "p1",
            "terminal_id": "t1",
            "status": "matched",
            "profile": profile,
            "session": {"id": "s", "total_tokens": 42},
        },
        {
            "pane_id": "p2",
            "terminal_id": "t2",
            "status": "matched",
            "profile": "other",
            "session": {"id": "s2", "total_tokens": 8},
        },
    ]
    reporter.publish({"panes": rows})
    writes = {
        command[3]: command[command.index("--token") + 1]
        for command in runner.commands
        if "--token" in command
    }
    assert writes == {
        "p1": "sessiongauge_usage=API 57% · 42",
        "p2": "sessiongauge_usage=8",
    }
    assert quota.calls == 1


@pytest.mark.parametrize("quota_enabled", [False])
def test_disabled_quota_compacts_target_profile_without_read(quota_enabled):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        calls = 0

        def read_sessions(self, threads):
            self.calls += 1
            return {thread["thread_id"]: 57 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=quota_enabled,
        quota_reader=quota,
        runner=runner,
        adapter=Adapter(),
        display="percent_tokens",
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {"id": "s", "total_tokens": 1450298},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=1.45M"
    assert quota.calls == 0


def test_quota_failure_keeps_numeric_metadata_and_adds_na():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read_sessions(self, threads):
            return {}

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=Quota(),
        runner=runner,
        adapter=Adapter(),
        wall_clock=lambda: 1,
        display="percent_tokens",
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {"id": "s", "total_tokens": 1450298},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=API N/A · 1.45M"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1K"),
        (1234, "1.23K"),
        (35394, "35.39K"),
        (1450298, "1.45M"),
        (999999, "1M"),
        (1000000, "1M"),
        (999999999, "1B"),
        (1000000000, "1B"),
        (1000000000000, "1T"),
        (9223372036854775807, "9223372.04T"),
    ],
)
def test_sidebar_compacts_token_metadata_only_for_target_quota_profile(count, expected):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        calls = 0

        def read_sessions(self, threads):
            self.calls += 1
            return {thread["thread_id"]: 0 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=Adapter(),
        display="percent_tokens",
    )
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "profile": profile,
                "session": {"id": "s", "total_tokens": count},
            }
        ]
    }

    reporter.publish(data)

    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == f"sessiongauge_usage=API 0% · {expected}"
    assert data["panes"][0]["session"]["total_tokens"] == count
    assert quota.calls == 1


def test_sidebar_compacts_large_count_with_quota_and_preserves_herdr_prefix():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        calls = 0

        def read_sessions(self, threads):
            self.calls += 1
            return {thread["thread_id"]: 0 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=Adapter(),
        display="percent_tokens",
    )
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "profile": profile,
                "session": {"id": "s", "total_tokens": 1450298},
            }
        ]
    }

    reporter.publish(data)

    write = next(command for command in runner.commands if "--token" in command)
    metadata = write[write.index("--token") + 1]
    assert metadata == "sessiongauge_usage=API 0% · 1.45M"
    assert quota.calls == 1
    assert data["panes"][0]["session"]["total_tokens"] == 1450298
    display = "hermes · " + metadata.removeprefix("sessiongauge_usage=")
    assert display.startswith("hermes · API")
    assert len(display) <= 24


def test_small_count_metadata_remains_exact():
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True, runner=runner, adapter=Adapter(), display="percent_tokens"
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "session": {"id": "s", "total_tokens": 42},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=42"


def test_sidebar_large_count_for_other_profile_is_compact_without_quota_read():
    class Quota:
        calls = 0

        def read_sessions(self, threads):
            self.calls += 1
            return {thread["thread_id"]: 57 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=BothPanesAdapter(),
        display="percent_tokens",
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": "other",
                    "session": {"id": "s", "total_tokens": 1450298},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=1.45M"
    assert quota.calls == 0


def test_cli_factory_wires_configured_quota_reader(monkeypatch):
    from sessiongauge import cli

    created = {}

    class CollectorStub:
        def __init__(self, config):
            self.monitor = None
            self.herdr_state = SimpleNamespace(last_success=[])

    class ReporterStub:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def publish(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "Collector", CollectorStub)
    monkeypatch.setattr("sessiongauge.sidebar.SidebarReporter", ReporterStub)
    config = SimpleNamespace(
        sidebar_enabled=True,
        sidebar_ttl_ms=15000,
        sidebar_quota_enabled=True,
        sidebar_quota_agent_resume_bin="/usr/local/bin/agent-resume",
        sidebar_quota_hermes_home=Path("~/custom-hermes").expanduser(),
        sidebar_quota_poll_interval_seconds=90,
        sidebar_display="percent_tokens",
    )
    cli._sidebar_factory(config)(config)
    assert created["quota_enabled"] is True
    assert created["quota_reader"].agent_resume_bin == "/usr/local/bin/agent-resume"
    assert created["quota_reader"].hermes_home == str(config.sidebar_quota_hermes_home)
    assert created["quota_reader"].poll_interval_seconds == 90
    assert created["display"] == "percent_tokens"


def test_unlink_clears_just_owned_token_and_report_does_not_set_labels():
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True, runner=runner, adapter=Adapter(), source="sessiongauge:test"
    )
    row = {
        "pane_id": "p1",
        "terminal_id": "t1",
        "status": "matched",
        "session": {"id": "s", "total_tokens": 9},
    }
    reporter.publish({"panes": [row]})
    reporter.publish({"panes": [{**row, "status": "unlinked"}]})
    writes = [command for command in runner.commands if "--token" in command]
    assert len(writes) == 1
    assert (
        "--title" not in writes[0]
        and "--display-agent" not in writes[0]
        and "--state-label" not in writes[0]
    )
    clears = [command for command in runner.commands if "--clear-token" in command]
    assert len(clears) == 1
    assert clears[0][clears[0].index("--source") + 1] == "sessiongauge:test"


def test_native_session_percentages_are_bound_to_session_ids():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def __init__(self):
            self.requests = []

        def read_sessions(self, threads):
            self.requests.append(threads)
            return {"s": 113.58442524, "s2": 1.08005576}

        def read(self):
            raise AssertionError("account quota must never be used")

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=BothPanesAdapter(),
        display="percent_tokens",
    )
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "profile": profile,
                "session": {
                    "id": "s",
                    "total_tokens": 1450298,
                    "created_at": None,
                    "descendant_thread_ids": [],
                },
            },
            {
                "pane_id": "p2",
                "terminal_id": "t2",
                "status": "matched",
                "profile": profile,
                "session": {
                    "id": "s2",
                    "total_tokens": 42,
                    "created_at": None,
                    "descendant_thread_ids": [],
                },
            },
        ]
    }

    reporter.publish(data)

    writes = {
        command[3]: command[command.index("--token") + 1]
        for command in runner.commands
        if "--token" in command
    }
    assert writes == {
        "p1": "sessiongauge_usage=API 113.58% · 1.45M",
        "p2": "sessiongauge_usage=API 1.08% · 42",
    }
    assert quota.requests == [
        [
            {"thread_id": "s", "created_at": None, "descendant_thread_ids": []},
            {"thread_id": "s2", "created_at": None, "descendant_thread_ids": []},
        ]
    ]
    assert [row["session"]["total_tokens"] for row in data["panes"]] == [1450298, 42]


def test_native_session_percentage_missing_for_one_session_is_na_not_shared():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read_sessions(self, threads):
            return {"s": 113.58}

        def read(self):
            raise AssertionError("account quota must never be used")

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=Quota(),
        runner=runner,
        adapter=BothPanesAdapter(),
        display="percent_tokens",
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {
                        "id": "s",
                        "total_tokens": 1450298,
                        "created_at": None,
                        "descendant_thread_ids": [],
                    },
                },
                {
                    "pane_id": "p2",
                    "terminal_id": "t2",
                    "status": "matched",
                    "profile": profile,
                    "session": {
                        "id": "s2",
                        "total_tokens": 42,
                        "created_at": None,
                        "descendant_thread_ids": [],
                    },
                },
            ]
        }
    )

    writes = {
        command[3]: command[command.index("--token") + 1]
        for command in runner.commands
        if "--token" in command
    }
    assert writes == {
        "p1": "sessiongauge_usage=API 113.58% · 1.45M",
        "p2": "sessiongauge_usage=API N/A · 42",
    }


@pytest.mark.parametrize("count,expected_calls", [(100, 1), (101, 0)])
def test_quota_never_splits_cached_request_into_unpollable_batches(count, expected_calls):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class ManyPanesAdapter:
        def list_panes(self):
            return [
                Pane("endpoint", f"p{i}", f"t{i}", "hermes", "running", None, None, f"s{i}")
                for i in range(count)
            ]

    class Quota:
        def __init__(self):
            self.sizes = []

        def read_sessions(self, threads):
            self.sizes.append(len(threads))
            return {thread["thread_id"]: 10 for thread in threads}

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=ManyPanesAdapter(),
        display="percent_tokens",
    )
    rows = [
        {
            "pane_id": f"p{i}",
            "terminal_id": f"t{i}",
            "status": "matched",
            "profile": profile,
            "session": {"id": f"s{i}", "total_tokens": 1},
        }
        for i in range(count)
    ]
    reporter.publish({"panes": rows})
    assert len(quota.sizes) == expected_calls
    assert all(size <= 100 for size in quota.sizes)
    if count == 101:
        writes = [command for command in runner.commands if "--token" in command]
        assert all("API N/A" in command[command.index("--token") + 1] for command in writes)


def test_cli_factory_uses_hermes_root_for_default_python(monkeypatch, tmp_path):
    from sessiongauge import cli

    captured = {}

    class CollectorStub:
        def __init__(self, config):
            self.monitor = None
            self.herdr_state = SimpleNamespace(last_success=[])

    class ReporterStub:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def publish(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "Collector", CollectorStub)
    monkeypatch.setattr("sessiongauge.sidebar.SidebarReporter", ReporterStub)
    root = tmp_path / "hermes-root"
    config = SimpleNamespace(
        sidebar_enabled=True,
        sidebar_ttl_ms=15000,
        sidebar_quota_enabled=True,
        sidebar_quota_agent_resume_bin="agent-resume",
        sidebar_quota_hermes_home=tmp_path,
        sidebar_quota_poll_interval_seconds=60,
        sidebar_quota_hermes_root=root,
    )
    cli._sidebar_factory(config)(config)
    assert captured["quota_reader"].hermes_python == str(root / "venv/bin/python")


@pytest.mark.parametrize(
    ("quota_value", "expected"),
    [
        (113.58442524, "API 113.58%"),
        (0, "API 0%"),
        (1.08, "API 1.08%"),
        (None, "N/A"),
    ],
)
def test_default_percent_only(quota_value, expected):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read_sessions(self, threads):
            return {} if quota_value is None else {"s": quota_value}

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=Quota(),
        runner=runner,
        adapter=Adapter(),
        wall_clock=lambda: 1,
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {
                        "id": "s",
                        "total_tokens": 42,
                        "created_at": None,
                        "descendant_thread_ids": [],
                    },
                }
            ]
        }
    )

    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == f"sessiongauge_usage={expected}"


@pytest.mark.parametrize("quota_enabled,profile", [(False, None), (True, "other")])
def test_default_percent_only_is_na_without_eligible_quota(quota_enabled, profile):
    class Quota:
        def read_sessions(self, threads):
            raise AssertionError("ineligible sidebar values must not be queried")

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=quota_enabled,
        quota_reader=Quota(),
        runner=runner,
        adapter=Adapter(),
    )
    row = {
        "pane_id": "p1",
        "terminal_id": "t1",
        "status": "matched",
        "session": {"id": "s", "total_tokens": 1450298},
    }
    if profile is not None:
        row["profile"] = profile
    reporter.publish({"panes": [row]})
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=N/A"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 10**400])
def test_api_raw_reference_rejects_boolean_negative_and_nonfinite_values(value):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read_sessions(self, threads):
            return {"s": value}

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=Quota(),
        runner=runner,
        adapter=Adapter(),
        display="percent_tokens",
    )
    try:
        reporter.publish(
            {
                "panes": [
                    {
                        "pane_id": "p1",
                        "terminal_id": "t1",
                        "status": "matched",
                        "profile": profile,
                        "session": {"id": "s", "total_tokens": 1000},
                    }
                ]
            }
        )
        write = next(command for command in runner.commands if "--token" in command)
        assert write[write.index("--token") + 1] == "sessiongauge_usage=API N/A · 1K"
    finally:
        reporter.close()


def test_estimate_mode_remains_na_even_with_fresh_calibration_sample():
    from concurrent.futures import Future

    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read_session_metadata(self, threads):
            return {}

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_mode="estimate",
        quota_reader=Quota(),
        runner=runner,
        adapter=Adapter(),
        wall_clock=lambda: 1,
    )
    context = {"sid": "s", "tokens": 1000, "model": "gpt-x", "profile": reporter.quota_profile_id}
    future = Future()
    future.set_result({"s": {"percent": 10, "data_as_of": 1}})
    reporter._quota_future = future
    reporter._quota_future_context = {("endpoint", "p1", "t1"): context}
    data = {
        "panes": [
            {
                "pane_id": "p1",
                "terminal_id": "t1",
                "status": "matched",
                "profile": profile,
                "session": {"id": "s", "total_tokens": 1500, "model": "gpt-x"},
            }
        ]
    }
    reporter.publish(data)
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=N/A"
    reporter.close()
