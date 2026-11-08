import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_collector_acceptance import pane

from sessiongauge import cli
from sessiongauge.cli import _show
from sessiongauge.config import Config
from sessiongauge.persistence import MonitorDB


def _title_event(db, at, pane_id, old_title, new_title):
    detail = json.dumps({"pane_id": pane_id, "old_title": old_title, "new_title": new_title})
    db.connection.execute(
        "INSERT INTO events(at,kind,detail) VALUES(?, 'pane_title_changed', ?)",
        (at, detail),
    )


@pytest.fixture
def old_duplicate_links_db(tmp_path):
    # 旧実装で生じたNULL terminalの重複を、実際のSQLiteファイルに保存する。
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "source.db")
        other_profile = db.register_profile("other", tmp_path / "other.db")
        target = replace(pane(), terminal_id=None)
        task = db.link_manual(target, profile, "session", 10)
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('other-task',10)")
        db.connection.execute(
            "INSERT INTO pane_session_links(endpoint,pane_id,terminal_id,task_id,profile_id,session_id,valid_from,source,confidence) "
            "SELECT endpoint,pane_id,terminal_id,task_id,profile_id,session_id,11,'official','exact' FROM pane_session_links"
        )
        db.connection.commit()
        db.record_panes([replace(target, title="new")], 20)
        _title_event(db, 21, target.pane_id, "new", "legacy-new")
        db.connection.commit()
    return tmp_path, task, other_profile


@pytest.mark.parametrize("difference", [None, "task_id", "profile_id", "session_id"])
def test_show_old_duplicate_ownership(old_duplicate_links_db, capsys, difference):
    path, task, other_profile = old_duplicate_links_db
    with MonitorDB(path / "monitor.db") as db:
        if difference:
            value = {
                "task_id": "other-task",
                "profile_id": other_profile,
                "session_id": "other-session",
            }[difference]
            db.connection.execute(
                f"UPDATE pane_session_links SET {difference}=? WHERE id=2", (value,)
            )
            db.connection.commit()
        before = db.connection.execute("SELECT * FROM pane_session_links ORDER BY id").fetchall()
    _show(Config(data_dir=path), task)
    changes = json.loads(capsys.readouterr().out)["name_changes"]
    assert [event["new_title"] for event in changes] == (
        [] if difference else ["new", "legacy-new"]
    )
    with MonitorDB(path / "monitor.db") as db:
        assert (
            db.connection.execute("SELECT * FROM pane_session_links ORDER BY id").fetchall()
            == before
        )


@pytest.mark.parametrize("renamed_target", [False, True])
def test_show_new_name_events_match_full_generation(tmp_path, capsys, renamed_target):
    target = replace(pane(), endpoint="endpoint-a", pane_id="shared", terminal_id="terminal-a")
    other = replace(target, endpoint="endpoint-b", terminal_id="terminal-b")
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        task = db.link_manual(target, profile, "target-session", 10)
        db.record_panes([target, other], 11)
        changed = target if renamed_target else other
        db.record_panes([replace(changed, title="renamed")], 20)

    _show(Config(data_dir=tmp_path), task)
    changes = json.loads(capsys.readouterr().out)["name_changes"]
    assert changes == (
        [{"at": 20.0, "pane_id": "shared", "old_title": target.title, "new_title": "renamed"}]
        if renamed_target
        else []
    )


@pytest.mark.parametrize("other_first_seen", [11, 21])
def test_show_legacy_event_checks_known_unlinked_generations(tmp_path, capsys, other_first_seen):
    target = replace(pane(), endpoint="endpoint-a", pane_id="shared", terminal_id="terminal-a")
    other = replace(target, endpoint="endpoint-b", terminal_id="terminal-b")
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        task = db.link_manual(target, profile, "target-session", 10)
        db.record_panes([other], other_first_seen)
        db.record_panes([target, other], 30)
        _title_event(db, 20, "shared", "old", "new")
        db.connection.commit()

    _show(Config(data_dir=tmp_path), task)
    changes = json.loads(capsys.readouterr().out)["name_changes"]
    assert len(changes) == (0 if other_first_seen <= 20 else 1)


def test_show_task_name_changes_are_limited_to_linked_panes_and_intervals(tmp_path, capsys):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.executemany(
            "INSERT INTO tasks(task_id,created_at) VALUES(?,1)",
            (("target-task",), ("other-task",)),
        )
        db.connection.execute(
            "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,valid_to,source,confidence) "
            "VALUES('e','reused-pane','target-task',?,'target-session',10,30,'manual','confirmed')",
            (profile,),
        )
        db.connection.execute(
            "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,valid_to,source,confidence) "
            "VALUES('e','other-pane','other-task',?,'other-session',10,30,'manual','confirmed')",
            (profile,),
        )
        # 同じpane_idでも、対象リンクの有効区間外での名前変更は別の利用期間。
        _title_event(db, 20, "reused-pane", "linked-old", "linked-new")
        _title_event(db, 40, "reused-pane", "later-old", "later-new")
        _title_event(db, 20, "other-pane", "other-old", "other-new")
        db.connection.commit()

    _show(Config(data_dir=tmp_path), "target-task")
    output = json.loads(capsys.readouterr().out)

    assert output["name_changes"] == [
        {
            "at": 20.0,
            "pane_id": "reused-pane",
            "old_title": "linked-old",
            "new_title": "linked-new",
        }
    ]


def test_show_task_lineage_stays_with_linked_profile_and_session_identity(tmp_path, capsys):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile_a = db.register_profile("profile-a", tmp_path / "a.db")
        profile_b = db.register_profile("profile-b", tmp_path / "b.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('target-task',1)")
        for profile, title in ((profile_a, "parent-a"), (profile_b, "parent-b")):
            db.connection.execute(
                "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,title) VALUES(?,?,NULL,?)",
                (profile, "shared-parent", title),
            )
        db.connection.execute(
            "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,title) VALUES(?,?,?,?)",
            (profile_a, "shared-child", "shared-parent", "child-a"),
        )
        db.connection.execute(
            "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,title) VALUES(?,?,?,?)",
            (profile_b, "shared-child", "shared-parent", "child-b"),
        )
        db.connection.execute(
            "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,source,confidence) "
            "VALUES('e','pane','target-task',?,'shared-child',1,'manual','confirmed')",
            (profile_a,),
        )
        db.connection.commit()

    _show(Config(data_dir=tmp_path), "target-task")
    output = json.loads(capsys.readouterr().out)

    assert {(row["profile_id"], row["session_id"], row["title"]) for row in output["lineage"]} == {
        (profile_a, "shared-parent", "parent-a"),
        (profile_a, "shared-child", "child-a"),
    }


def test_show_omits_legacy_name_event_with_ambiguous_link_generation(tmp_path, capsys):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('target-task',1)")
        for session_id in ("session-a", "session-b"):
            db.connection.execute(
                "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,valid_to,source,confidence) "
                "VALUES('e','pane','target-task',?,?,10,30,'manual','confirmed')",
                (profile, session_id),
            )
        _title_event(db, 20, "pane", "old", "new")
        db.connection.commit()

    _show(Config(data_dir=tmp_path), "target-task")
    assert json.loads(capsys.readouterr().out)["name_changes"] == []


def test_show_omits_legacy_event_when_other_endpoint_has_same_pane_id(tmp_path, capsys):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.executemany(
            "INSERT INTO tasks(task_id,created_at) VALUES(?,1)",
            (("target-task",), ("other-task",)),
        )
        for endpoint, task_id, session_id in (
            ("endpoint-a", "target-task", "session-a"),
            ("endpoint-b", "other-task", "session-b"),
        ):
            db.connection.execute(
                "INSERT INTO pane_session_links(endpoint,pane_id,task_id,profile_id,session_id,valid_from,valid_to,source,confidence) "
                "VALUES(?, 'pane', ?, ?, ?, 10, 30, 'manual', 'confirmed')",
                (endpoint, task_id, profile, session_id),
            )
        _title_event(db, 20, "pane", "old", "new")
        db.connection.commit()

    _show(Config(data_dir=tmp_path), "target-task")
    assert json.loads(capsys.readouterr().out)["name_changes"] == []


def test_link_uses_custom_hermes_home_profile_name(tmp_path, monkeypatch, capsys):
    home = tmp_path / "custom"
    source = home / "state.db"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sessions(id TEXT)")
        connection.execute("INSERT INTO sessions VALUES('session-1')")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "HerdrAdapter",
        lambda: SimpleNamespace(
            list_panes=lambda: [
                SimpleNamespace(
                    endpoint="endpoint",
                    pane_id="pane-1",
                    terminal_id=None,
                    session_id="session-1",
                    title="test",
                    agent_status=None,
                    generation_key=("endpoint", "pane-1", None),
                    cwd=None,
                    git_branch=None,
                    started_at=None,
                )
            ]
        ),
    )

    assert (
        cli.main(
            [
                "--hermes-db",
                str(source),
                "--data-dir",
                str(tmp_path / "data"),
                "link",
                "pane-1",
                "session-1",
                "--profile",
                "custom",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip()
