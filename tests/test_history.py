import json
import time

import pytest

from sessiongauge.cli import main
from sessiongauge.domain import Counters
from sessiongauge.persistence import MonitorDB


def test_history_reports_observed_interval_delta_and_baseline_separately(tmp_path, capsys):
    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("work", tmp_path / "state.db")
        now = time.time()
        db.record_snapshot(
            profile, "s1", Counters(100, 20), collected_at=now - 100, estimated_cost_usd=1.0
        )
        db.record_snapshot(
            profile, "s1", Counters(150, 35), collected_at=now - 50, estimated_cost_usd=1.5
        )
        db.connection.execute(
            "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,input_tokens,output_tokens,estimated_cost_usd,confidence,provenance) VALUES(?,?,?,?,?,?,?,?,?)",
            (profile, "s1", now - 100, now - 50, 50, 15, 0.5, "unattributed", "test"),
        )
        db.connection.commit()
    assert main(["--data-dir", str(tmp_path), "history", "--days", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result
    assert any(
        row.get("input_tokens") == 50 and row.get("output_tokens") == 15 for row in result["groups"]
    )
    assert not any(row.get("input_tokens") == 100 for row in result["groups"])


def test_days_must_be_positive():
    from sessiongauge.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["history", "--days", "0"])
