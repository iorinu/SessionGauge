import json
from pathlib import Path

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
