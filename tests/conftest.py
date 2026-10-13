import pytest


@pytest.fixture(autouse=True)
def isolate_sessiongauge_environment(tmp_path, monkeypatch):
    """CLI tests must never discover a real Hermes database or user config."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    data_dir = tmp_path / "sessiongauge"
    data_dir.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("SESSIONGAUGE_CONFIG", str(config_path))
    monkeypatch.setenv("SESSIONGAUGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SESSIONGAUGE_HERMES_DB_PATHS", str(hermes_home / "state.db"))
