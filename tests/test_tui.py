from sessiongauge.tui import (
    TUIState,
    deduplicated_totals,
    handle_key,
    render_frame,
    run_tui,
    visible_rows,
)


def sample():
    return {
        "status": "healthy",
        "stale": False,
        "panes": [
            {
                "pane_id": "%1",
                "status": "matched",
                "task_id": "t1",
                "profile": "p1",
                "title": "日本語タスク",
                "state": "working",
                "session": {
                    "id": "s1",
                    "model": "m",
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "cache_read_tokens": 4,
                    "cache_write_tokens": 1,
                    "reasoning_tokens": 2,
                    "total_tokens": 5,
                    "estimated_cost_usd": 0.1,
                    "actual_cost_usd": None,
                },
            },
            {
                "pane_id": "%2",
                "status": "matched",
                "task_id": "t1",
                "profile": "p1",
                "session": {
                    "id": "s1",
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                    "estimated_cost_usd": 0.1,
                },
            },
            {
                "pane_id": "%3",
                "status": "unlinked",
                "task_id": None,
                "profile": None,
                "session": None,
            },
        ],
        "sessions": [],
    }


def test_render_is_clipped_at_80_columns_and_sanitizes_control_characters():
    frame = render_frame(sample(), TUIState(), width=80, now=1_700_000_000)
    assert all(len(line) <= 80 for line in frame)
    assert "日本語タスク" in "\n".join(frame)
    hostile = sample()
    hostile["panes"][0]["title"] = "bad\x1b[31m\nname"
    assert "\x1b" not in "\n".join(render_frame(hostile, TUIState(), width=80))


def test_selection_sort_filter_detail_help_refresh_and_quit_keys():
    data = sample()
    state = TUIState()
    assert visible_rows(data, state)[0]["pane_id"] == "%1"
    state = handle_key(state, "j", data)
    assert state.selected == 0
    state = handle_key(state, "t", data)
    assert state.sort_by == "total"
    state = handle_key(state, "f", data)
    assert state.filter_by == "all"
    state = handle_key(state, "Enter", data)
    assert state.details
    assert "SessionID" in "\n".join(render_frame(data, state, width=120))
    assert "Keys" in "\n".join(render_frame(data, handle_key(state, "?", data), width=120))
    assert handle_key(state, "r", data).refresh_requested
    assert handle_key(state, "q", data).quit_requested


def test_totals_deduplicate_profile_session_and_preserve_unknown_cost():
    totals = deduplicated_totals(sample())
    assert totals["input_tokens"] == 2
    assert totals["total_tokens"] == 5
    assert totals["estimated_cost_usd"] == 0.1
    assert totals["actual_cost_usd"] is None


def test_stale_and_missing_values_are_not_rendered_as_zero():
    data = {"panes": [], "sessions": [], "status": "stale", "stale": True}
    text = "\n".join(render_frame(data, TUIState(), width=80))
    assert "STALE" in text
    assert "N/A" in text


def test_render_uses_collection_time_instead_of_session_activity_time():
    from types import SimpleNamespace

    data = {
        "collected_at": 1_700_000_000,
        "panes": [{"session": {"last_activity_at": 1_600_000_000}}],
    }

    text = "\n".join(
        render_frame(data, TUIState(), width=100, config=SimpleNamespace(time_zone="UTC"))
    )

    assert "2023-11-14" in text
    assert "2020-09-13" not in text


def test_large_values_are_compact_and_missing_cost_stays_distinct_from_zero():
    data = sample()
    data["panes"] = [
        dict(
            data["panes"][0],
            session={
                "id": "large",
                "input_tokens": 123456789,
                "output_tokens": 0,
                "total_tokens": 123456789,
                "estimated_cost_usd": 123456.75,
                "actual_cost_usd": None,
            },
        )
    ]
    text = "\n".join(render_frame(data, TUIState(), width=80))
    assert "123.46M~" in text
    assert "$123.46k~" in text
    assert "actual=N/A" in text
    assert "out=0" in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_body_row_compacts_each_numeric_cell_to_its_column_width():
    data = {
        "panes": [
            {
                "title": "fixture",
                "pane_id": "p1",
                "status": "matched",
                "state": "working",
                "profile": "p1",
                "session": {
                    "id": "s1",
                    "input_tokens": 123456789,
                    "output_tokens": 42,
                    "cache_read_tokens": 987654321,
                    "total_tokens": 123456831,
                    "estimated_cost_usd": 123456.75,
                },
            }
        ]
    }
    frame = render_frame(data, TUIState(), width=80)
    row = next(line for line in frame if line.startswith(">fixture"))
    assert len(row) <= 80
    assert not any(value in row for value in ("1234567", "98765432", "1234568", "$123.46"))
    assert "123.5M~" in row
    assert "987.65M~" in row
    assert row.count("123.5M~") == 2
    assert "$123k~" in row


def test_width_compaction_rounding_and_non_finite_values():
    from sessiongauge.tui import _compact_number

    assert _compact_number(999.999, 7, currency=True) == "$1.00k~"
    assert _compact_number(123456789012345678901234567890, 7) == "N/A"
    assert _compact_number(1e308, 7, currency=True) == "N/A"
    for value in (float("nan"), float("inf"), "not-a-number"):
        assert _compact_number(value, 7) == "N/A"
        assert _compact_number(value, 7, currency=True) == "N/A"
    assert _compact_number(0, 7) == "0"
    assert _compact_number(None, 7) == "N/A"


def test_fake_screen_event_loop_handles_refresh_and_quit_and_closes_worker():
    class Collector:
        def __init__(self, _config):
            self.closed = False

        def collect(self):
            return sample()

        def close(self):
            self.closed = True

    instance = None

    def factory(config):
        nonlocal instance
        instance = Collector(config)
        return instance

    class Screen:
        keys = iter((ord("r"), ord("q")))

        def nodelay(self, _value):
            pass

        def getmaxyx(self):
            return 24, 80

        def erase(self):
            pass

        def addnstr(self, *_args):
            pass

        def refresh(self):
            pass

        def getch(self):
            return next(self.keys, ord("q"))

    assert (
        run_tui(object(), factory, screen_runner=lambda callback: callback(Screen()), interval=0.01)
        == 0
    )
    assert instance.closed


def test_selected_row_is_scrolled_into_view_with_details_on_small_screen():
    data = {
        "panes": [
            {
                "pane_id": str(i),
                "status": "unlinked",
                "title": f"row-{i}",
                "session": {"id": f"s{i}"},
            }
            for i in range(120)
        ]
    }
    state = TUIState(selected=100, filter_by="all", details=True)
    frame = render_frame(data, state, width=80, height=30)
    text = "\n".join(frame)
    assert "row-100" in text
    assert "Details:" in text
    assert len(frame) <= 30


def test_worker_interval_attribution_sums_recent_intervals_and_task_sessions(tmp_path):
    import queue
    import threading

    from sessiongauge.persistence import MonitorDB
    from sessiongauge.tui import _worker

    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("default", tmp_path / "state.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('task',1)")
        for sid in ("s1", "s2"):
            db.connection.execute(
                "INSERT INTO session_catalog(profile_id,session_id,title) VALUES(?,?,?)",
                (profile, sid, "old title"),
            )
        db.connection.executemany(
            "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,actual_cost_usd) VALUES(?,?,?,?,?,?,?,?)",
            [
                (profile, "s1", 0, 20, "task", 10, 1, 0.1),
                (profile, "s1", 20, 40, "task", 20, 2, 0.2),
                (profile, "s2", 20, 40, "task", 30, 3, 0.3),
            ],
        )
        db.connection.commit()

        class Collector:
            monitor = db

            def collect(self):
                stop.set()
                return {
                    "panes": [
                        {
                            "pane_id": "p",
                            "task_id": "task",
                            "profile": profile,
                            "status": "matched",
                            "session": {"id": "s1", "last_activity_at": 40},
                        },
                        {
                            "pane_id": "unsupported",
                            "status": "unsupported",
                            "session": None,
                        },
                    ],
                }

            def close(self):
                pass

        messages, stop, refresh = queue.Queue(), threading.Event(), threading.Event()
        _worker(None, lambda _: Collector(), messages, stop, refresh, 60, None)
        payload = messages.get(timeout=1)[0]
    pane = payload["panes"][0]
    assert payload["panes"][1]["session"] is None
    assert pane["recent_60s"]["input_tokens"] == 60
    assert pane["task_lifetime"]["input_tokens"] == 60


def test_worker_waits_after_success_and_manual_refresh_wakes_it(tmp_path):
    import queue
    import threading

    from sessiongauge.tui import _worker

    class RefreshEvent:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout):
            assert timeout == 3
            self.calls += 1
            if self.calls == 1:
                # 手動更新のイベントを、待機中に決定的に発生させる。
                self.set = True
            else:
                stop.set()

        def clear(self):
            self.set = False

        set = False

    class Collector:
        closed = False

        def __init__(self, _config):
            pass

        def collect(self):
            return {"panes": []}

        def close(self):
            self.closed = True

    stop, refresh, messages = threading.Event(), RefreshEvent(), queue.Queue()
    instance = None

    def factory(config):
        nonlocal instance
        instance = Collector(config)
        return instance

    _worker(None, factory, messages, stop, refresh, 3, None)
    assert refresh.calls == 2
    assert messages.qsize() == 2
    assert instance.closed


def test_worker_preserves_unknown_interval_values_and_skips_unattributed_tasks(tmp_path):
    import queue
    import threading

    from sessiongauge.persistence import MonitorDB
    from sessiongauge.tui import _worker

    with MonitorDB(tmp_path / "monitor.db") as db:
        profile = db.register_profile("default", tmp_path / "state.db")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('task',1)")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('unknown-task',1)")
        db.connection.execute("INSERT INTO tasks(task_id,created_at) VALUES('zero-task',1)")
        db.connection.executemany(
            "INSERT INTO session_catalog(profile_id,session_id) VALUES(?,?)",
            [(profile, sid) for sid in ("known", "zero", "unattributed-a", "unattributed-b")],
        )
        db.connection.executemany(
            "INSERT INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,actual_cost_usd) VALUES(?,?,?,?,?,?,?,?)",
            [
                (profile, "known", 0, 40, "unknown-task", None, None, None),
                (profile, "zero", 0, 40, "zero-task", 0, 0, 0),
                (profile, "unattributed-a", 0, 40, None, 10, 2, 0.2),
                (profile, "unattributed-b", 0, 40, None, 30, 3, 0.3),
            ],
        )
        db.connection.commit()
        stop, refresh, messages = threading.Event(), threading.Event(), queue.Queue()

        class Collector:
            monitor = db

            def collect(self):
                stop.set()
                return {
                    "panes": [
                        {
                            "pane_id": "known",
                            "profile": profile,
                            "task_id": "unknown-task",
                            "session": {"id": "known"},
                        },
                        {
                            "pane_id": "zero",
                            "profile": profile,
                            "task_id": "zero-task",
                            "session": {"id": "zero"},
                        },
                        {
                            "pane_id": "unlinked-a",
                            "profile": profile,
                            "task_id": None,
                            "session": {"id": "unattributed-a"},
                        },
                        {
                            "pane_id": "unlinked-b",
                            "profile": profile,
                            "task_id": None,
                            "session": {"id": "unattributed-b"},
                        },
                    ],
                    "collected_at": 40,
                }

            def close(self):
                pass

        _worker(None, lambda _: Collector(), messages, stop, refresh, 60, None)
        panes = messages.get(timeout=1)[0]["panes"]

    assert panes[0]["task_lifetime"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "actual_cost_usd": None,
    }
    assert panes[0]["recent_60s"] == panes[0]["task_lifetime"]
    assert panes[1]["task_lifetime"]["input_tokens"] == 0
    assert panes[1]["task_lifetime"]["total_tokens"] == 0
    assert all("task_lifetime" not in pane and "recent_60s" not in pane for pane in panes[2:])
