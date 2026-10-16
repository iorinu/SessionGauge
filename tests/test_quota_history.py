import hashlib
import sqlite3
import uuid
from contextlib import closing

import pytest

from sessiongauge.quota_history import QuotaHistory


@pytest.fixture
def history_files(tmp_path):
    monitor = tmp_path / "monitor.db"
    source = tmp_path / "source.db"
    profile = str(uuid.uuid5(uuid.NAMESPACE_URL, str(source.resolve())))
    with sqlite3.connect(monitor) as db:
        db.executescript(
            "CREATE TABLE session_catalog(profile_id,session_id,parent_session_id,model,started_at,ended_at,last_activity_at);"
            "CREATE TABLE snapshots(profile_id,session_id,collected_at,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,reasoning_tokens,source_ok);"
        )
        db.execute(
            "INSERT INTO session_catalog VALUES(?,?,?,?,?,?,?)",
            (profile, "same", None, "m", 1, 15, 10),
        )
        db.executemany(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            [(profile, "same", 10, 10, 90, 0, 2, 1, 1), (profile, "same", 20, 20, 100, 0, 5, 3, 1)],
        )
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE session_model_usage(session_id,model,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens)"
        )
        db.execute("INSERT INTO session_model_usage VALUES('same','m',1,2,0,0)")
    return monitor, source, profile


def test_history_is_readonly_scoped_and_brackets_uncertainty(history_files):
    monitor, source, profile = history_files
    before = hashlib.sha256(source.read_bytes()).digest()
    history = QuotaHistory(monitor, source, profile)
    assert history.bucket_bracket("same", 15) == ((10, 90, 0, 2), (20, 100, 0, 5))
    assert history.single_model("same", "m")
    assert history.reference_candidates({"same"}, 30000) == []
    assert hashlib.sha256(source.read_bytes()).digest() == before
    db = history._open(monitor)
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO session_catalog VALUES('x','x',NULL,'m',0,NULL,NULL)")
    finally:
        db.close()


def test_history_rejects_wrong_profile_missing_bracket_and_counter_reset(history_files):
    monitor, source, profile = history_files
    with pytest.raises(ValueError):
        QuotaHistory(monitor, source, "wrong")
    history = QuotaHistory(monitor, source, profile)
    assert history.bucket_bracket("same", 5) is None
    assert history.window_usage("same", 0, (30, 20, 0, 6), 15, 25) is None


def test_ended_session_can_reuse_last_stable_observation(history_files):
    monitor, source, profile = history_files
    history = QuotaHistory(monitor, source, profile)
    assert history.bucket_bracket("same", 30) == ((20, 100, 0, 5), (20, 100, 0, 5))


def test_reference_candidates_excludes_before_limit(history_files):
    monitor, source, profile = history_files
    with closing(sqlite3.connect(monitor)) as db:
        db.executemany(
            "INSERT INTO session_catalog VALUES(?,?,?,?,?,?,?)",
            [(profile, f"live{i}", None, "m", 1, None, 29999 - i) for i in range(4)],
        )
        db.commit()
    history = QuotaHistory(monitor, source, profile)
    assert [
        row["sid"]
        for row in history.reference_candidates({f"live{i}" for i in range(3)}, 30000, limit=1)
    ] == ["live3"]


def test_window_rejects_future_start_and_fresh_reset(history_files):
    monitor, source, profile = history_files
    history = QuotaHistory(monitor, source, profile)
    assert history.window_usage("same", 30, (100, 100, 0, 100), 15, 25) is None
    with closing(sqlite3.connect(monitor)) as db:
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            (profile, "same", 22, 30, 120, 0, 6, 0, 1),
        )
        db.commit()
    assert history.window_usage("same", 0, (31, 110, 0, 7), 15, 25) is None


def test_active_session_without_after_observation_is_unknown(history_files):
    monitor, source, profile = history_files
    with closing(sqlite3.connect(monitor)) as db:
        db.execute("UPDATE session_catalog SET ended_at=NULL WHERE session_id='same'")
        db.commit()
    assert QuotaHistory(monitor, source, profile).bucket_bracket("same", 30) is None


def test_same_sid_in_another_profile_is_not_a_reference(history_files):
    monitor, source, profile = history_files
    with closing(sqlite3.connect(monitor)) as db:
        db.execute("INSERT INTO session_catalog VALUES('other','same',NULL,'m',1,15,10)")
        db.commit()
    history = QuotaHistory(monitor, source, profile)
    assert history.bucket_bracket("same", 15) == ((10, 90, 0, 2), (20, 100, 0, 5))


def test_mixed_model_refusal(history_files):
    monitor, source, profile = history_files
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO session_model_usage VALUES('same','other',1,0,0,0)")
        db.commit()
    assert not QuotaHistory(monitor, source, profile).single_model("same", "m")
