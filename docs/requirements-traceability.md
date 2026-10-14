# 要件トレーサビリティ

機能要件FR-01〜FR-10の基準は、変更していない [`SESSION_GAUGE_REQUIREMENTS.md`](../SESSION_GAUGE_REQUIREMENTS.md) です。`docs/discovery.md` はPhase 0で得た初期観測であり、要件文書ではありません。テスト名は現行テストコード上の名称です。実行結果と証拠レベルは[検証記録](verification.md)を参照してください。

## 機能要件 FR-01〜FR-10

| 要件 | 実装根拠 | テスト根拠 | 証拠レベル |
|---|---|---|---|
| FR-01 診断 | `src/sessiongauge/cli.py::_doctor` | `test_doctor_reports_product_and_does_not_dump_identifiers`; `test_doctor_reports_read_only_integration_status_and_versions` — `tests/test_doctor.py` | unit / fake CLI。環境情報は別途read-only調査 |
| FR-02 Hermes読取 | `src/sessiongauge/adapters/hermes_db.py::HermesDB` | `test_sqlite_readonly_whitelisted_and_nullable`; `test_missing_columns_and_invalid_counters_are_unavailable`; `test_wal_reader_does_not_block_writer` — `tests/test_phase1.py` | fixture integration |
| FR-03 herdr Pane取得 | `src/sessiongauge/adapters/herdr.py::HerdrAdapter` | `test_herdr_strict_envelope_and_agent_session` — `tests/test_phase1.py` | fixture integration。実環境21 panesの初期調査あり |
| FR-04 identity / Task対応 | `src/sessiongauge/identity.py::match_panes`; `src/sessiongauge/collector.py::Collector.collect`; `src/sessiongauge/cli.py::_link` | `test_exact_matching_conflict_and_db_collision`; `test_manual_fallback_same_sid_is_scoped_to_profile_and_task`; `test_manual_link_is_used_for_hermes_pane_without_official_sid` | unit / fixture integration |
| FR-05 poll・snapshot・stale | `src/sessiongauge/collector.py::PollState`; `src/sessiongauge/persistence.py::record_poll` | `test_backoff_skips_calls_until_retry_due`; `test_collector_failed_source_is_stale_and_never_attributes_gap`; `test_unchanged_ended_sessions_are_not_snapshotted_repeatedly` | unit / fixture integration |
| FR-06 live TUI / JSON | `src/sessiongauge/tui.py`; `src/sessiongauge/cli.py::_live` / `_render` | `test_fake_screen_event_loop_handles_refresh_and_quit_and_closes_worker`; `test_selected_row_is_scrolled_into_view_with_details_on_small_screen`; `test_idle_matched_pane_is_active_and_render_uses_configured_zone` | fake screen / unit。PTY確認は視覚確認ではない |
| FR-07 history / show | `src/sessiongauge/history.py::history`; `src/sessiongauge/cli.py::_show` | `test_history_reports_observed_interval_delta_and_baseline_separately`; `test_show_includes_task_session_lineage_and_name_history` | fixture integration |
| FR-08 CSV / JSON export | `src/sessiongauge/export.py::export_rows`; `src/sessiongauge/cli.py::_export` | `test_csv_formula_guard_handles_leading_whitespace_and_controls`; `test_standalone_export_has_stable_columns_guard_and_protects_hardlinks`; `test_export_cli_keeps_interval_delta_separate_from_baseline` | unit / fixture integration |
| FR-09 sidebar opt-in | `src/sessiongauge/sidebar.py::SidebarReporter` | `test_disabled_makes_no_commands`; `test_reports_only_matched_unique_session_and_clears_owned_token`; `test_conflict_and_duplicate_sessions_are_not_reported` — `tests/test_sidebar.py` | fake CLI unit。実環境では3 matched panesへのmetadata送信・readbackを確認。画面描画は未確認 |
| FR-10 設定 | `src/sessiongauge/config.py::load_config`; `src/sessiongauge/cli.py::_parser` | `test_config_precedence_cli_env_toml_and_defaults`; `test_sidebar_precedence_and_environment`; `test_common_options_are_available_before_and_after_subcommand` | unit。sidebar display設定は実環境metadataでも確認 |

## 18受け入れケース

18ケースの説明、現行テスト関数名、証拠レベルは[検証記録のテスト根拠表](verification.md#受け入れケースのテスト根拠)を参照してください。テスト名の存在と最終実行結果は別の証拠です。

## 追加されたquota・表示設定

以下は原要件FRとは区別して追加した機能です。

| 機能 | 実装根拠 | テスト根拠 | 証拠レベル |
|---|---|---|---|
| SID別quota取得と表示 | `src/sessiongauge/quota.py::QuotaReader.read_sessions`; `src/sessiongauge/hermes_usage_bridge.py::_request`; `src/sessiongauge/sidebar.py::SidebarReporter.publish` | `test_reads_percentages_by_exact_id_when_response_order_differs`; `test_missing_or_nonavailable_is_unknown`; `test_session_values_fail_closed_or_keep_valid_out_of_range_above_100`; `test_cache_is_per_id_and_failure_clears_previous_values` — `tests/test_session_quota.py` | unit/fixture。実API・metadataの実機確認は[検証記録](verification.md)参照 |
| percent / percent_tokens設定 | `src/sessiongauge/config.py::load_config`; `src/sessiongauge/sidebar.py::SidebarReporter` | `test_sidebar_display_precedence_and_validation`; `test_sidebar_quota_config_and_validation` — `tests/test_sidebar_config.py` | unit。実機metadata確認あり、pixel描画未確認 |

quotaはSIDとの厳密な対応、API通信、privacy境界を持つopt-in拡張です。詳細は[アーキテクチャ](architecture.md)と[既知の制限](known-limitations.md)を参照してください。

## 安全性・性能

| 項目 | 実装・テスト根拠 | 証拠レベル |
|---|---|---|
| Hermes DB read-onlyと列allowlist | `HermesDB`; `test_sqlite_readonly_whitelisted_and_nullable`; `test_source_values_are_sanitized_and_source_file_remains_unchanged` | fixture integration |
| monitor DB分離・path保護・権限 | `src/sessiongauge/persistence.py::MonitorDB`; `test_monitor_rejects_hermes_database_alias_before_creation`; `test_monitor_does_not_chmod_existing_parent`; `test_monitor_sidecar_hardlink_rejected_without_source_change` | fixture integration |
| sidebar opt-in | `SidebarReporter.publish`; `test_disabled_makes_no_commands` | fake CLI unit |
| 安全性優先のunattributed | `src/sessiongauge/persistence.py::record_poll`; `test_move_interval_unattributed`; `test_conflict_unattributed`; `test_repeated_and_decreasing_observations_do_not_add_intervals_or_rewind_catalog` | fixture integration |

テストは `uv run pytest -q` で実行します。実環境の読み取り調査やmetadata確認、PTYでの確認はfixture testとは別の証拠です。Linux、全バージョン、30 Pane stress、production DB全体のhash不変は証明していません。
