import io
import sys
from types import SimpleNamespace

import pytest

from sessiongauge import hermes_usage_bridge as bridge


@pytest.fixture(autouse=True)
def isolate_audit_hook_installation(monkeypatch):
    monkeypatch.setattr(bridge.sys, "addaudithook", lambda hook: None)


def safe_threads():
    return [{"thread_id": "sid", "created_at": None, "descendant_thread_ids": []}]


def test_bridge_posts_read_only_thread_query_and_whitelists_values(tmp_path, monkeypatch):
    helper = tmp_path / "hermes_bridge.py"
    helper.write_text(
        "def protect_files(event, args):\n    assert event == 'runtime'\n"
        "def credentials():\n    return ('secret-token', 'https://chatgpt.com', 'private-account')\n"
    )
    account_usage = SimpleNamespace(
        _resolve_codex_usage_url=lambda base: base + "/backend-api/wham/usage"
    )
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage)
    recorded = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "threads": [
                    {
                        "thread_id": "sid",
                        "data_status": "available",
                        "five_hour_limit_percent": 113.58,
                        "weekly_limit_percent": 77,
                        "groups": ["PII"],
                    }
                ]
            }

    def post(url, **kwargs):
        recorded.update(url=url, **kwargs)
        return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=post))
    result = bridge._request(tmp_path, helper, safe_threads())
    assert result == {
        "threads": [
            {"thread_id": "sid", "data_status": "available", "five_hour_limit_percent": 113.58}
        ]
    }
    assert recorded["url"] == "https://chatgpt.com/backend-api/wham/usage/thread_usage/query_v2"
    assert recorded["json"] == {"threads": safe_threads()}
    assert recorded["headers"]["Authorization"] == "Bearer secret-token"
    assert recorded["headers"]["ChatGPT-Account-Id"] == "private-account"
    assert recorded["headers"]["Accept"] == "application/json"
    assert recorded["headers"]["User-Agent"] == "codex-cli"
    assert recorded["follow_redirects"] is False and recorded["timeout"] == 20


@pytest.mark.parametrize(
    "url",
    [
        "http://chatgpt.com/usage",
        "https://evil.example/usage",
        "https://user@chatgpt.com/usage",
        "https://chatgpt.com/usage?token=x",
        "https://chatgpt.com/usage#fragment",
    ],
)
def test_bridge_rejects_untrusted_usage_urls(tmp_path, monkeypatch, url):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def protect_files(event, args): pass\ndef credentials(): return ('t','https://chatgpt.com','a')\n"
    )
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "agent.account_usage", SimpleNamespace(_resolve_codex_usage_url=lambda _: url)
    )
    with pytest.raises(ValueError):
        bridge._request(tmp_path, helper, safe_threads())


def test_input_rejects_invalid_ids_descendants_and_limits():
    with pytest.raises(ValueError):
        bridge._safe_threads(
            {"threads": [{"thread_id": " ", "created_at": None, "descendant_thread_ids": []}]}
        )
    with pytest.raises(ValueError):
        bridge._safe_threads(
            {
                "threads": [
                    {"thread_id": "a", "created_at": None, "descendant_thread_ids": ["child"]}
                ]
            }
        )
    with pytest.raises(ValueError):
        bridge._safe_threads(
            {
                "threads": [
                    {"thread_id": str(i), "created_at": None, "descendant_thread_ids": []}
                    for i in range(101)
                ]
            }
        )


def test_safe_threads_accepts_reordered_object_keys():
    item = {"descendant_thread_ids": [], "thread_id": "sid", "created_at": None}
    assert bridge._safe_threads({"threads": [item]}) == ([item], {"sid"})


@pytest.mark.parametrize("path", ["/tmp/.env", "/tmp/.env.local", "/Users/test/.ssh/id_rsa"])
def test_audit_guard_rejects_sensitive_paths(path):
    with pytest.raises(PermissionError):
        bridge._audit_guard("open", (path, "r"))


def test_audit_guard_rejects_relative_path_without_file_descriptor():
    with pytest.raises(PermissionError):
        bridge._audit_guard("open", ("ordinary-relative", 0, 0))


@pytest.mark.parametrize(
    "path",
    ["/tmp/alias.env", b"/tmp/alias.env", bridge.Path("/tmp/alias.env")],
)
@pytest.mark.parametrize("canonical", ["/private/.env", "/private/.ssh/id_rsa"])
def test_audit_guard_rejects_canonical_sensitive_path_aliases(monkeypatch, path, canonical):
    monkeypatch.setattr(bridge.Path, "resolve", lambda self, strict=False: bridge.Path(canonical))
    with pytest.raises(PermissionError):
        bridge._audit_guard("open", (path, "r"))


def test_main_installs_audit_hook_before_auth_or_sdk_imports(monkeypatch):
    order = []
    monkeypatch.setattr(bridge.sys, "addaudithook", lambda hook: order.append("hook"))
    monkeypatch.setattr(
        bridge.sys,
        "stdin",
        io.StringIO(
            '{"threads":[{"thread_id":"sid","created_at":null,"descendant_thread_ids":[]}]}'
        ),
    )
    monkeypatch.setattr(
        bridge, "_request", lambda *args: order.append("request") or {"threads": []}
    )
    assert bridge.main(["--hermes-root", "/tmp/hermes", "--auth-helper", "/tmp/helper.py"]) == 0
    assert order == ["hook", "request"]


def test_main_hides_arbitrary_request_exception(monkeypatch, capsys):
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO('{"threads":[]}'))

    class PrivateError(Exception):
        pass

    def fail(*args):
        raise PrivateError("private credential material")

    monkeypatch.setattr(bridge, "_request", fail)
    assert bridge.main(["--hermes-root", "/tmp/hermes", "--auth-helper", "/tmp/helper.py"]) == 1
    output = capsys.readouterr()
    assert output.out == '{"error":"unavailable"}'
    assert "private" not in output.out and "private" not in output.err


def test_percent_handles_huge_integer_without_overflow():
    assert bridge._valid_percent(10**400) is False
    assert bridge._valid_percent(113.58) is True


def test_metadata_mode_reads_top_level_as_of_and_only_whitelists_it(monkeypatch, tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("def credentials(): return ('t','https://chatgpt.com','a')\n")
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "agent.account_usage",
        SimpleNamespace(_resolve_codex_usage_url=lambda base: base),
    )

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data_as_of": "2026-10-15T12:00:00Z",
                "threads": [
                    {
                        "thread_id": "sid",
                        "data_status": "available",
                        "five_hour_limit_percent": 12.5,
                        "data_as_of": "wrong-row-time",
                        "groups": ["private"],
                        "raw": "private",
                    }
                ],
                "other": "private",
            }

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=lambda *a, **k: Response()))
    assert bridge._request(tmp_path, helper, safe_threads(), include_metadata=True) == {
        "data_as_of": "2026-10-15T12:00:00Z",
        "threads": [
            {
                "thread_id": "sid",
                "data_status": "available",
                "five_hour_limit_percent": 12.5,
            }
        ],
    }
