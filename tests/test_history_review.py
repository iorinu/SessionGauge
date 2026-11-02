from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from sessiongauge.config import Config
from sessiongauge.history import _zone, history
from sessiongauge.persistence import MonitorDB


@pytest.mark.parametrize("tz", ["not/a-zone", "UTC0"])
def test_local_zone_falls_back_when_tz_is_not_an_iana_zone(monkeypatch, tz):
    monkeypatch.setenv("TZ", tz)
    from importlib import import_module

    history_module = import_module("sessiongauge.history")
    monkeypatch.setattr("pathlib.Path.resolve", lambda self: type(self)("/mock/localtime"))

    monkeypatch.setattr(history_module, "datetime", _FallbackDateTime)
    assert _zone("local").utcoffset(None) == timedelta(0)


class _FallbackDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 1, 1, tzinfo=ZoneInfo("UTC"))

    def astimezone(self, tz=None):
        return self if tz is None else super().astimezone(tz)


def test_local_zone_falls_back_when_resolved_iana_zone_is_missing(monkeypatch):
    from importlib import import_module

    history_module = import_module("sessiongauge.history")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        "pathlib.Path.resolve", lambda self: type(self)("/mock/zoneinfo/Missing/Zone")
    )
    attempted = []

    def missing_zone(name):
        attempted.append(name)
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(history_module, "ZoneInfo", missing_zone)
    monkeypatch.setattr(history_module, "datetime", _FallbackDateTime)
    assert _zone("local").utcoffset(None) == timedelta(0)
    assert attempted == ["Missing/Zone"]


@pytest.mark.parametrize(
    "ordering", [("attributed", "unattributed"), ("unattributed", "attributed")]
)
def test_group_mapping_status_marks_mixed_records_as_mixed(tmp_path, monkeypatch, ordering):
    import sessiongauge.collector

    class NoopCollector:
        def __init__(self, config):
            pass

        def collect(self, now=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sessiongauge.collector, "Collector", NoopCollector)
    now = datetime(2025, 1, 4, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    data_dir = tmp_path / "data"
    with MonitorDB(data_dir / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('task',1)")
        rows = {"attributed": ("task", 5), "unattributed": (None, 7)}
        for ix, status in enumerate(ordering):
            task_id, value = rows[status]
            start = now - 500 + ix * 100
            db.connection.execute(
                "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,confidence,provenance) VALUES(?,?,?,?,?,?,?,?,?)",
                (profile, "same", start, start + 30, task_id, value, 2, "low", "snapshot"),
            )
        for session_id, task_id in (("only-attributed", "task"), ("only-unattributed", None)):
            db.connection.execute(
                "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,confidence,provenance) VALUES(?,?,?,?,?,1,1,'low','snapshot')",
                (profile, session_id, now - 300, now - 270, task_id),
            )
        db.connection.commit()

    report = history(Config(data_dir=data_dir, time_zone="UTC"), now=now, group_by="session")
    group = report["groups"][0]
    assert group["mapping_status"] == "mixed"
    assert [record["mapping_status"] for record in group["records"]] == list(ordering)
    assert group["input_tokens"] == 12
    assert group["output_tokens"] == 4
    homogeneous = {
        group["group_key"][1]: group["mapping_status"]
        for group in report["groups"]
        if group["group_key"][1] != "same"
    }
    assert homogeneous == {"only-attributed": "attributed", "only-unattributed": "unattributed"}
