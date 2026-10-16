import math
from types import SimpleNamespace

from test_estimate_lifecycle import displayed, lifecycle_harness, make_data, make_pane

from sessiongauge.sidebar import SidebarReporter
from sessiongauge.window_quota import QuotaEstimate


class EngineStub:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []
        self.closecount = 0

    def estimates(self, sessions):
        self.calls.append(sessions)
        return self.values

    def close(self):
        self.closecount += 1


def test_window_formatter_fails_closed_and_preserves_unbounded_ranges():
    assert SidebarReporter._format_window(QuotaEstimate(2.61, 9.01)) == "~2.6–9.1%"
    assert SidebarReporter._format_window(QuotaEstimate(2.61, None)) == "~≥2.6%"
    assert SidebarReporter._format_window(QuotaEstimate(12.5, 12.5)) == "~12.5%"
    assert SidebarReporter._format_window(QuotaEstimate(0, 0)) == "~0.0%"
    assert SidebarReporter._format_window(QuotaEstimate(125, 125)) == "~125.0%"
    assert SidebarReporter._format_window(QuotaEstimate(0, None)) is None
    assert SidebarReporter._format_window(QuotaEstimate(1e308, 1e308)) is None
    assert SidebarReporter._format_window(QuotaEstimate(10, 9)) is None
    assert SidebarReporter._format_window(math.nan) is None


def test_cli_factory_uses_collector_data_dir_and_source_profile(monkeypatch, tmp_path):
    from sessiongauge import cli

    captured = {}

    class Collector:
        def __init__(self, config):
            self.monitor = None
            self.herdr_state = SimpleNamespace(last_success=[])

    class History:
        def __init__(self, monitor_path, source_path, profile_id):
            captured["history"] = (monitor_path, source_path, profile_id)

    class Estimator:
        def __init__(self, history, reader, max_age):
            captured["engine"] = (history, reader, max_age)

        def close(self):
            pass

    class Reporter:
        def __init__(self, **kwargs):
            captured["reporter"] = kwargs

        def publish(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "Collector", Collector)
    monkeypatch.setattr("sessiongauge.quota_history.QuotaHistory", History)
    monkeypatch.setattr("sessiongauge.window_estimator.WindowQuotaEstimator", Estimator)
    monkeypatch.setattr("sessiongauge.sidebar.SidebarReporter", Reporter)
    home = tmp_path / "custom-home"
    config = SimpleNamespace(
        sidebar_enabled=True,
        sidebar_quota_enabled=True,
        sidebar_quota_mode="estimate",
        sidebar_quota_hermes_home=home,
        sidebar_quota_agent_resume_bin="fake",
        sidebar_quota_poll_interval_seconds=60,
        sidebar_ttl_ms=1000,
        sidebar_quota_estimate_max_age_seconds=123,
    )
    inner = SimpleNamespace(data_dir=tmp_path / "actual-data")
    cli._sidebar_factory(config)(inner)
    monitor, source, profile = captured["history"]
    assert monitor == inner.data_dir / "monitor.db"
    assert source == home / "state.db"
    assert (
        profile
        == cli.uuid.uuid5(cli.uuid.NAMESPACE_URL, str(source.resolve(strict=False))).__str__()
    )
    assert captured["engine"][2] == 123


def test_estimate_mode_displays_empty_and_updated_engine_results():
    reporter, profile, _, _, _, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    engine = EngineStub()
    reporter.window_estimator = engine
    data = make_data(profile, "model-a")

    reporter.publish(data)
    assert displayed(runner) == "N/A"
    engine.values = {"session-a": QuotaEstimate(2.61, 9.01)}
    reporter.publish(data)
    assert displayed(runner) == "~2.6–9.1%"
    engine.values = {}
    reporter.publish(data)
    assert displayed(runner) == "N/A"
    reporter.close()


def test_disabled_or_stale_sidebar_does_not_call_estimate_engine():
    for enabled, quota_enabled, status in (
        (False, True, "matched"),
        (True, False, "matched"),
        (True, True, "stale"),
    ):
        reporter, profile, _, _, _, adapter, _ = lifecycle_harness()
        reporter.enabled = enabled
        reporter.quota_enabled = quota_enabled
        engine = EngineStub()
        reporter.window_estimator = engine
        adapter.panes = [make_pane("model-a")]
        reporter.publish(make_data(profile, "model-a", status=status))
        assert engine.calls == []
        reporter.close()


def test_close_closes_injected_estimate_engine_once():
    reporter, _, _, _, _, _, _ = lifecycle_harness()
    engine = EngineStub()
    reporter.window_estimator = engine
    reporter.close()
    reporter.close()
    assert engine.closecount == 1


def test_api_mode_displays_api_quota_with_injected_estimate_engine():
    reporter, profile, _, _, reader, adapter, runner = lifecycle_harness()
    reporter.quota_mode = "api"
    reader.read_sessions = lambda threads: {"session-a": 12.5}
    engine = EngineStub()
    reporter.window_estimator = engine
    adapter.panes = [make_pane("model-a")]
    reporter.publish(make_data(profile, "model-a"))
    assert displayed(runner) == "API 12.5%"
    reporter.close()
