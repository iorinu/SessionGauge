"""遅延した利用率応答の精度を検証する。"""

from test_estimate_lifecycle import displayed, lifecycle_harness, make_data, make_pane


def test_delayed_historical_percent_is_not_scaled_by_request_time_tokens():
    reporter, profile, monotonic, wall, _reader, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]

    # 時刻100でのローカル履歴（1000トークン）があっても、率の期間・分母との対応を証明しない。
    try:
        reporter.publish(make_data(profile, "model-a", tokens=1000))
        reporter._quota_future.complete({})
        monotonic.advance(60)
        wall.advance(60)

        # 要求時点の累積値は10000。時点100の10%応答を時刻160に受け取る。
        reporter.publish(make_data(profile, "model-a", tokens=10000))
        reporter._quota_future.complete({"session-a": {"percent": 10, "data_as_of": 100}})
        reporter.publish(make_data(profile, "model-a", tokens=11000))

        # 10%*11000/10000も、1000から10000までの履歴補間も独立した検証ではない。
        assert displayed(runner) == "N/A"
    finally:
        reporter.close()


def test_same_or_mixed_model_and_cache_counters_never_authorize_a_percentage():
    for model, cache_read, cache_write in (
        ("model-a", 0, 0),
        ("model-a", 500, 250),
        ("model-b", 500, 250),
    ):
        reporter, profile, _monotonic, _wall, _reader, adapter, runner = lifecycle_harness()
        adapter.panes = [make_pane(model)]
        try:
            row = make_data(profile, model, tokens=1000)
            row["panes"][0]["session"].update(
                {"cache_read_tokens": cache_read, "cache_write_tokens": cache_write}
            )
            reporter.publish(row)
            reporter._quota_future.complete({"session-a": {"percent": 10, "data_as_of": 100}})
            reporter.publish(make_data(profile, model, tokens=2000))
            assert displayed(runner) == "N/A"
        finally:
            reporter.close()


def test_window_boundary_is_unavailable_and_counter_reset_never_authorizes_percentage():
    reporter, profile, _monotonic, _wall, _reader, adapter, runner = lifecycle_harness()
    adapter.panes = [make_pane("model-a")]
    try:
        reporter.publish(make_data(profile, "model-a", tokens=1000))
        # data_as_ofにはwindow境界がなく、履歴sampleとのcounter対応も証明できない。
        reporter._quota_future.complete({"session-a": {"percent": 80, "data_as_of": 100}})
        reporter.publish(make_data(profile, "model-a", tokens=0))
        reporter.publish(make_data(profile, "model-a", tokens=2000))
        assert displayed(runner) == "N/A"
    finally:
        reporter.close()
