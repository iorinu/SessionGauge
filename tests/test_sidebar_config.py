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


@pytest.mark.parametrize("value", [0, 86400001, "nope"])
def test_sidebar_ttl_validation(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[sidebar]\nttl_ms = {value!r}\n")
    with pytest.raises(ValueError):
        load_config(config_path=path, environ={})
