from datetime import datetime
from zoneinfo import ZoneInfo

from sessiongauge.config import Config
from sessiongauge.history import history
from sessiongauge.persistence import MonitorDB


def test_daily_groups_sum_intervals_and_session_identity_includes_profile(tmp_path, monkeypatch):
    import sessiongauge.collector

    class NoopCollector:
        def __init__(self, config):
            pass

        def collect(self, now=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sessiongauge.collector, "Collector", NoopCollector)
    data_dir = tmp_path / "data"
    now = datetime(2025, 1, 4, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    with MonitorDB(data_dir / "monitor.db") as db:
        p1 = db.register_profile("one", tmp_path / "one.db")
        p2 = db.register_profile("two", tmp_path / "two.db")
        for profile, offset in ((p1, 0), (p2, 10)):
            for ix in range(2):
                start = now - 3600 - offset - ix * 100
                db.connection.execute(
                    "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,input_tokens,output_tokens,estimated_cost_usd,confidence,provenance) VALUES(?,?,?,?,?,?,?,?,?)",
                    (profile, "same", start, start + 30, 5, 2, None, "low", "snapshot"),
                )
        db.connection.commit()
    report = history(Config(data_dir=data_dir, time_zone="UTC"), now=now, group_by="day")
    assert len(report["groups"]) == 1
    assert report["groups"][0]["input_tokens"] == 20
    assert any("cost" in note for note in report["notes"])
    sessions = history(Config(data_dir=data_dir, time_zone="UTC"), now=now, group_by="session")
    assert len(sessions["groups"]) == 2


def test_midnight_crossing_interval_is_skipped_with_lower_bound_note(tmp_path, monkeypatch):
    import sessiongauge.collector

    class NoopCollector:
        def __init__(self, config):
            pass

        def collect(self, now=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sessiongauge.collector, "Collector", NoopCollector)
    zone = ZoneInfo("UTC")
    now = datetime(2025, 1, 3, 12, tzinfo=zone).timestamp()
    start = datetime(2025, 1, 2, 23, 59, tzinfo=zone).timestamp()
    data_dir = tmp_path / "data"
    with MonitorDB(data_dir / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute(
            "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,input_tokens,output_tokens) VALUES(?,?,?,?,?,?)",
            (profile, "s", start, start + 120, 99, 1),
        )
        db.connection.commit()
    report = history(Config(data_dir=data_dir, time_zone="UTC"), now=now)
    assert report["groups"] == []
    assert any("midnight" in n for n in report["notes"])
