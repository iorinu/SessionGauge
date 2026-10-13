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


def test_reports_only_matched_unique_session_and_clears_owned_token():
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True, ttl_ms=15000, runner=runner, adapter=Adapter(), source="sessiongauge:test"
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

        def read(self):
            self.calls += 1
            return 57

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=True,
        quota_reader=quota,
        runner=runner,
        adapter=BothPanesAdapter(),
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
        "p1": "sessiongauge_usage=42 · 5h 57%",
        "p2": "sessiongauge_usage=8",
    }
    assert quota.calls == 1


@pytest.mark.parametrize("quota_enabled", [False])
def test_disabled_quota_keeps_exact_target_profile_plain_without_read(quota_enabled):
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        calls = 0

        def read(self):
            self.calls += 1
            return 57

    quota = Quota()
    runner = Runner()
    reporter = SidebarReporter(
        enabled=True,
        quota_enabled=quota_enabled,
        quota_reader=quota,
        runner=runner,
        adapter=Adapter(),
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {"id": "s", "total_tokens": 42},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=42"
    assert quota.calls == 0


def test_quota_failure_keeps_numeric_metadata_and_adds_na():
    home = Path("~/.hermes").expanduser()
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str((home / "state.db").resolve())))

    class Quota:
        def read(self):
            return None

    runner = Runner()
    reporter = SidebarReporter(
        enabled=True, quota_enabled=True, quota_reader=Quota(), runner=runner, adapter=Adapter()
    )
    reporter.publish(
        {
            "panes": [
                {
                    "pane_id": "p1",
                    "terminal_id": "t1",
                    "status": "matched",
                    "profile": profile,
                    "session": {"id": "s", "total_tokens": 42},
                }
            ]
        }
    )
    write = next(command for command in runner.commands if "--token" in command)
    assert write[write.index("--token") + 1] == "sessiongauge_usage=42 · 5h N/A"


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
    )
    cli._sidebar_factory(config)(config)
    assert created["quota_enabled"] is True
    assert created["quota_reader"].agent_resume_bin == "/usr/local/bin/agent-resume"
    assert created["quota_reader"].hermes_home == str(config.sidebar_quota_hermes_home)
    assert created["quota_reader"].poll_interval_seconds == 90


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
