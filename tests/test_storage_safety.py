from __future__ import annotations

import os
import sqlite3

import pytest

from sessiongauge.persistence import MonitorDB


def test_unknown_database_v1_refused(tmp_path):
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value)")
        conn.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError), MonitorDB(path):
        pass


def test_source_symlink_protection(tmp_path):
    source = tmp_path / "source.db"
    source.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(source)
    with pytest.raises(ValueError), MonitorDB(tmp_path / "monitor.db", source_paths=(alias,)):
        pass


def test_source_hardlink_protection(tmp_path):
    source = tmp_path / "source.db"
    source.touch()
    alias = tmp_path / "alias.db"
    os.link(source, alias)
    with pytest.raises(ValueError), MonitorDB(alias, source_paths=(source,)):
        pass
