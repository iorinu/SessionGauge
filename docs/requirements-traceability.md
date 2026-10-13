# 要件トレーサビリティ

基準文書は変更していない `SESSION_GAUGE_REQUIREMENTS.md` と `docs/discovery.md` です。テスト名は現行テストコードから転記し、最終実行結果は別途の最終統合チェックに従います。

## 機能要件 FR-01〜FR-10

| 要件 | 実装根拠 | テスト根拠 | 証拠レベル |
|---|---|---|---|
| FR-01 診断 | `src/sessiongauge/cli.py::_doctor`、Hermes/herdr version・integration status・pane照合レポート | `test_doctor_reports_product_and_does_not_dump_identifiers`; `test_doctor_reports_read_only_integration_status_and_versions` — `tests/test_doctor.py` | unit / fake CLI integration。実環境情報はread-only調査 |
| FR-02 Hermes読取 | `src/sessiongauge/adapters/hermes_db.py::HermesDB` | `test_sqlite_readonly_whitelisted_and_nullable`; `test_missing_columns_and_invalid_counters_are_unavailable`; `test_wal_reader_does_not_block_writer` — `tests/test_phase1.py` | fixture integration |
| FR-03 herdr Pane取得 | `src/sessiongauge/adapters/herdr.py::HerdrAdapter` | `test_herdr_strict_envelope_and_agent_session` — `tests/test_phase1.py` | fixture integration、実環境21 panes |
| FR-04 identity/Task対応 | `src/sessiongauge/identity.py::match_panes`、`src/sessiongauge/collector.py::Collector.collect`、`src/sessiongauge/cli.py::_link` | `test_exact_matching_conflict_and_db_collision`; `test_manual_fallback_same_sid_is_scoped_to_profile_and_task`; `test_manual_link_is_used_for_hermes_pane_without_official_sid` | unit / fixture integration |
| FR-05 poll・snapshot・stale | `src/sessiongauge/collector.py::PollState` / `Collector.collect`、`src/sessiongauge/persistence.py::record_poll` | `test_backoff_skips_calls_until_retry_due`; `test_collector_failed_source_is_stale_and_never_attributes_gap`; `test_unchanged_ended_sessions_are_not_snapshotted_repeatedly` | unit / fixture integration |
| FR-06 live TUI/JSON | `src/sessiongauge/tui.py`、`src/sessiongauge/cli.py::_live` / `_render` | `test_fake_screen_event_loop_handles_refresh_and_quit_and_closes_worker`; `test_selected_row_is_scrolled_into_view_with_details_on_small_screen`; `test_idle_matched_pane_is_active_and_render_uses_configured_zone` | fake screen / unit。PTYは実環境で80x30、視覚確認なし |
| FR-07 history/show | `src/sessiongauge/history.py::history`、`src/sessiongauge/cli.py::_show` | `test_history_reports_observed_interval_delta_and_baseline_separately`; `test_show_includes_task_session_lineage_and_name_history` | fixture integration |
| FR-08 CSV/JSON export | `src/sessiongauge/export.py::export_rows`、`src/sessiongauge/cli.py::_export` | `test_csv_formula_guard_handles_leading_whitespace_and_controls`; `test_standalone_export_has_stable_columns_guard_and_protects_hardlinks`; `test_export_cli_keeps_interval_delta_separate_from_baseline` | unit / fixture integration |
| FR-09 sidebar opt-in | `src/sessiongauge/sidebar.py::SidebarReporter`、`src/sessiongauge/config.py` | `test_disabled_makes_no_commands`; `test_reports_only_matched_unique_session_and_clears_owned_token`; `test_conflict_and_duplicate_sessions_are_not_reported` | fake CLI unit。実機送信・clear・描画は未実施 |
| FR-10 設定 | `src/sessiongauge/config.py::load_config`、`src/sessiongauge/cli.py::_parser` | `test_config_precedence_cli_env_toml_and_defaults`; `test_sidebar_precedence_and_environment`; `test_common_options_are_available_before_and_after_subcommand` | unit |

## 18受け入れケース

具体的な18ケースと実在するテスト関数名、証拠レベル、個別ケースの説明は[検証記録](verification.md#受け入れケースのテスト根拠)を参照してください。テスト関数が存在することと、現在のcheckoutでそのテストがpassしたことを混同しません。最終結果は下記コマンド実行記録に基づきます。

## Safety / performance

| 要件 | 実装根拠 | テスト根拠 | 証拠レベル |
|---|---|---|---|
| Hermes DBはread-only、安全な列のみ | `src/sessiongauge/adapters/hermes_db.py` のreadonly URI・列allowlist | `test_sqlite_readonly_whitelisted_and_nullable`; `test_source_values_are_sanitized_and_source_file_remains_unchanged` | fixture integration |
| monitor DBの分離・path保護・権限 | `src/sessiongauge/persistence.py::MonitorDB._guard` / `__enter__` | `test_monitor_rejects_hermes_database_alias_before_creation`; `test_monitor_does_not_chmod_existing_parent`; `test_monitor_sidecar_hardlink_rejected_without_source_change` | fixture integration |
| 外部送信をsidebar opt-inに限定 | `src/sessiongauge/sidebar.py::SidebarReporter.publish` | `test_disabled_makes_no_commands` | fake CLI unit。実機sidebarは未検証 |
| 安全性優先のunattributedと重複回避 | `src/sessiongauge/persistence.py::record_poll`、history aggregation | `test_move_interval_unattributed`; `test_conflict_unattributed`; `test_repeated_and_decreasing_observations_do_not_add_intervals_or_rewind_catalog` | fixture integration |
| 大きな負荷を避けるpoll頻度/30 pane想定 | 設定範囲2〜60秒、標準3秒。1 pollごとにDB状態を読む | `test_config_precedence_cli_env_toml_and_defaults` — `tests/test_phase1.py` | unitのみ。30 active pane性能stressは未検証、ベンチマーク値なし |

## 実行・実機証拠レベル

- 自動テストは `uv run pytest -q` の最終実行結果を参照。test名列挙のみでpassを主張しない。
- 実環境のバージョン・21 panes/3 exact matches・一時monitor DB・PTYの詳細は[検証記録](verification.md)に記載。
- PTYの80x30動作は確認したが、人による視覚確認ではない。サイドバーは実機検証中無効だった。
- Linux、全バージョン、30 active pane stress、production Hermes DBのファイルhash不変は証明していない。稼働中のHermes自身がDBを更新するため、hash不変は安全性の根拠にしない。
