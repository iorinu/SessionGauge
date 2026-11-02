from __future__ import annotations

import sqlite3

import pytest

from sessiongauge.persistence import MonitorDB


@pytest.mark.parametrize(
    ("component", "truncated_name"),
    [
        ("data#fragment", "data"),
        ("data?query", "data"),
        ("data%2Fescaped", "data/escaped"),
    ],
)
def test_unknown_database_with_uri_special_path_is_untouched(tmp_path, component, truncated_name):
    directory = tmp_path / component
    directory.mkdir()
    path = directory / "monitor.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value)")
        connection.execute("PRAGMA user_version=2")

    original_bytes = path.read_bytes()
    original_mode = path.stat().st_mode
    truncated_sibling = tmp_path / truncated_name
    assert not truncated_sibling.exists()

    with pytest.raises(ValueError, match="unknown SQLite database"), MonitorDB(path):
        pass

    assert path.read_bytes() == original_bytes
    assert path.stat().st_mode == original_mode
    assert not truncated_sibling.exists()


@pytest.mark.parametrize("component", ["data#fragment", "data?query", "data%2Fescaped"])
def test_valid_monitor_database_reopens_with_uri_special_path(tmp_path, component):
    path = tmp_path / component / "monitor.db"

    with MonitorDB(path):
        pass

    with MonitorDB(path) as monitor:
        version = monitor.connection.execute("PRAGMA user_version").fetchone()[0]

    assert version == 2
