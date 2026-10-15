from pathlib import Path

import pytest

from sessiongauge.config import load_config


def test_sidebar_precedence_and_environment(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[sidebar]\nenabled = true\nttl_ms = 20000\ntime_zone = "UTC"\n')
    config = load_config(
        config_path=path,
        environ={"SESSIONGAUGE_SIDEBAR_ENABLED": "false", "SESSIONGAUGE_SIDEBAR_TTL_MS": "12345"},
    )
    assert config.sidebar_enabled is False
    assert config.sidebar_ttl_ms == 12345
    assert config.sidebar_time_zone == "UTC"
    config = load_config(config_path=path, environ={}, cli={"sidebar_enabled": False})
    assert config.sidebar_enabled is False


def test_sidebar_display_precedence_and_validation(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[sidebar]\ndisplay = "percent_tokens"\n')
    assert load_config(config_path=path, environ={}).sidebar_display == "percent_tokens"
    assert (
        load_config(
            config_path=path,
            environ={"SESSIONGAUGE_SIDEBAR_DISPLAY": "percent"},
        ).sidebar_display
        == "percent"
    )
    assert (
        load_config(
            config_path=path,
            environ={"SESSIONGAUGE_SIDEBAR_DISPLAY": "percent_tokens"},
            cli={"sidebar_display": "percent"},
        ).sidebar_display
        == "percent"
    )
    path.write_text("")
    assert load_config(config_path=path, environ={}).sidebar_display == "percent"


@pytest.mark.parametrize("value", ["unknown", 42, True])
def test_sidebar_display_rejects_unknown_or_nonstring_modes(tmp_path, value):
    path = tmp_path / "config.toml"
    if isinstance(value, str):
        path.write_text(f'[sidebar]\ndisplay = "{value}"\n')
    elif value is True:
        path.write_text("[sidebar]\ndisplay = true\n")
    else:
        path.write_text("[sidebar]\ndisplay = 42\n")
    with pytest.raises((TypeError, ValueError)):
        load_config(config_path=path, environ={})


@pytest.mark.parametrize("value", [0, 86400001, "nope"])
def test_sidebar_ttl_validation(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[sidebar]\nttl_ms = {value!r}\n")
    with pytest.raises(ValueError):
        load_config(config_path=path, environ={})


def test_sidebar_quota_config_and_validation(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[sidebar.quota]\nenabled = true\nhermes_home = "~/custom"\npoll_interval_seconds = 45\n'
    )
    config = load_config(config_path=path, environ={})
    assert config.sidebar_quota_enabled is True
    assert config.sidebar_quota_hermes_home == Path.home() / "custom"
    assert config.sidebar_quota_poll_interval_seconds == 45
    assert config.sidebar_quota_mode == "estimate"
    assert config.sidebar_quota_estimate_max_age_seconds == 86400
    path.write_text(
        '[sidebar.quota]\nhermes_root = "~/agent"\nhermes_python = "~/py"\nauth_helper = "~/bridge.py"\n'
    )
    configured = load_config(config_path=path, environ={})
    assert configured.sidebar_quota_hermes_root == Path.home() / "agent"
    assert configured.sidebar_quota_hermes_python == Path.home() / "py"
    assert configured.sidebar_quota_auth_helper == Path.home() / "bridge.py"
    path.write_text("")
    defaults = load_config(config_path=path, environ={})
    assert defaults.sidebar_quota_enabled is False
    assert defaults.sidebar_quota_agent_resume_bin == "agent-resume"
    assert defaults.sidebar_quota_hermes_home == Path.home() / ".hermes"
    assert defaults.sidebar_quota_poll_interval_seconds == 60
    assert defaults.sidebar_quota_hermes_root == Path.home() / ".hermes/hermes-agent"
    assert defaults.sidebar_quota_hermes_python is None
    assert defaults.sidebar_quota_auth_helper is None
    path.write_text('[sidebar.quota]\nmode = "api"\nestimate_max_age_seconds = 42\n')
    configured = load_config(config_path=path, environ={})
    assert configured.sidebar_quota_mode == "api"
    assert configured.sidebar_quota_estimate_max_age_seconds == 42
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-hermes"))
    assert (
        load_config(
            config_path=path, environ={"HERMES_HOME": "~/env-hermes"}
        ).sidebar_quota_hermes_home
        == Path.home() / "env-hermes"
    )


@pytest.mark.parametrize(
    "text",
    [
        '[sidebar.quota]\nenabled = "yes"\n',
        '[sidebar.quota]\nagent_resume_bin = "  "\n',
        "[sidebar.quota]\npoll_interval_seconds = 29\n",
        "[sidebar.quota]\npoll_interval_seconds = 3601\n",
        "[sidebar.quota]\npoll_interval_seconds = true\n",
    ],
)
def test_invalid_quota_config_is_rejected(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises((TypeError, ValueError)):
        load_config(config_path=path, environ={})


@pytest.mark.parametrize("interval", [30, 3600])
def test_quota_poll_interval_boundaries(tmp_path, interval):
    path = tmp_path / "config.toml"
    path.write_text(f"[sidebar.quota]\npoll_interval_seconds = {interval}\n")
    assert load_config(config_path=path, environ={}).sidebar_quota_poll_interval_seconds == interval


def test_agent_resume_bin_trims_and_expands_home_but_keeps_bare_command(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[sidebar.quota]\nagent_resume_bin = " ~/bin/agent-resume "\n')
    assert load_config(config_path=path, environ={}).sidebar_quota_agent_resume_bin == str(
        Path.home() / "bin/agent-resume"
    )
    path.write_text('[sidebar.quota]\nagent_resume_bin = " agent-resume "\n')
    assert (
        load_config(config_path=path, environ={}).sidebar_quota_agent_resume_bin == "agent-resume"
    )
