# 検証記録

ここではfixture test、ビルド確認、実環境での読み取り・metadata確認を区別します。以下のテスト結果は最終実装チェックの記録です。文書変更後の独立reviewを意味しません。

## 最終実装チェック

| コマンド | 結果 |
|---|---|
| `uv run --no-sync pytest -q` | 207 passed（0.40s） |
| `uv run --no-sync ruff check .` | 成功 |
| `uv run --no-sync ruff format --check .` | 45 files already formatted |
| `uv build` | sdist / wheel build成功 |
| Python 3.11 fresh wheelを隔離環境でsite-packagesからimportしてpytest | 207 passed（0.49s） |

pytestはfixture・unit testで、実API呼び出しを行いません。受け入れケース18件のテスト名と証拠レベルは[要件トレーサビリティ](requirements-traceability.md#18受け入れケース)から[テスト根拠表](verification.md#受け入れケースのテスト根拠)を参照してください。

## 受け入れケースのテスト根拠

以下のテスト関数は現行checkoutに存在する名前です。関数名の列挙自体は個々のcaseのpass証明ではなく、実行結果は上の207件の記録と区別してください。

| # | ケース | テスト関数・ファイル | 根拠 |
|---:|---|---|---|
| 1 | 単一PaneのID照合・使用量表示 | `test_collector_baseline_then_exact_attribution_and_persistent_task` — `tests/test_collector_acceptance.py` | fixture integration |
| 2 | 複数Pane取得・ID照合 | `test_herdr_strict_envelope_and_agent_session` — `tests/test_phase1.py` | fixture integration |
| 3 | 不明IDや衝突を推測リンクしない | `test_exact_matching_conflict_and_db_collision` — `tests/test_phase1.py` | unit |
| 4 | 欠損DB列・不正counter | `test_missing_columns_and_invalid_counters_are_unavailable` — `tests/test_phase1.py` | fixture integration |
| 5 | WAL reader / writer共存 | `test_wal_reader_does_not_block_writer` — `tests/test_phase1.py` | fixture integration |
| 6 | 累計差分の記録 | `test_collector_baseline_then_exact_attribution_and_persistent_task` — `tests/test_collector_acceptance.py` | fixture integration |
| 7 | 初回baselineと期間差分の分離 | `test_history_reports_observed_interval_delta_and_baseline_separately` — `tests/test_history.py` | fixture integration |
| 8 | compression継続とsubagent分離 | `test_compression_continuation_reuses_task_but_keeps_child_baseline`; `test_compression_continuation_does_not_join_subagent` — `tests/test_mapping_lifecycle.py` | fixture integration |
| 9 | Session移動時のbaseline重複防止 | `test_moving_session_baseline_is_emitted_once` — `tests/test_final_contracts.py` | fixture integration |
| 10 | Pane競合のunattributed扱い | `test_conflict_unattributed` — `tests/test_mapping_lifecycle.py` | fixture integration |
| 11 | Pane消失・終了Sessionの履歴 | `test_panes_are_persisted_without_any_hermes_sessions` — `tests/test_durability.py`; `test_unchanged_ended_sessions_are_not_snapshotted_repeatedly` — `tests/test_final_contracts.py` | fixture integration |
| 12 | herdr失敗時のstale扱い | `test_first_herdr_failure_and_empty_success` — `tests/test_mapping_lifecycle.py`; `test_failed_herdr_does_not_refresh_pane_or_task_metadata` — `tests/test_durability.py` | fixture integration |
| 13 | CSV式対策・既存出力保護 | `test_csv_formula_guard_handles_leading_whitespace_and_controls`; `test_export_refuses_existing_output_without_force` — `tests/test_export.py` | unit / fixture integration |
| 14 | profile間の同一ID分離 | `test_manual_fallback_same_sid_is_scoped_to_profile_and_task` — `tests/test_review_regressions.py` | fixture integration |
| 15 | 欠損値と大きな数値の表示 | `test_stale_and_missing_values_are_not_rendered_as_zero`; `test_large_values_are_compact_and_missing_cost_stays_distinct_from_zero` — `tests/test_tui.py` | unit |
| 16 | sidebar無効時に外部commandを出さない | `test_disabled_makes_no_commands` — `tests/test_sidebar.py` | unit / fake CLI |
| 17 | source DB値を変更しない | `test_source_values_are_sanitized_and_source_file_remains_unchanged` — `tests/test_durability.py` | fixture integration |
| 18 | 再起動後のTask継続とgap | `test_restart_persistent_task_but_gap_unattributed` — `tests/test_mapping_lifecycle.py` | fixture integration |

## 実環境確認

- 初期のread-only調査ではHermes DBを `mode=ro` で確認し、herdrが報告した3件のSession IDがDBのIDと一致しました。初期調査時のPane数やversionは[Phase 0記録](discovery.md)にあります。実機DB全体のhash不変は主張しません。Hermes自身が動作中にDBを書き込むためです。
- sidebar quotaの実API照会とmetadata報告を個別に確認しました。3 matched panesへのmetadataでquota labelが届き、数値2件と `N/A` 1件を確認しました。実値や識別情報は記録しません。これはfixture testではありません。
- 現行既定の `percent` 設定で実環境metadataの読み戻しを確認しました。`percent_tokens` を含む両設定の読込・切替はad-hoc確認とunit/fake CLIで検証していますが、実環境でmetadata書き込みを確認したのは現行既定の割合表示です。以前の複合metadata確認は、この設定追加前の既定形式でした。
- monitorの再起動後に古いmonitorと新しいmonitorが同じsidebar keyへ書き込むと表示がちらつく事象を観測しました。混在はmonitorが2組あったことによるもので、停止後は新しいmonitor 1組で再観測しました。
- sidebar metadataはherdrから3 matched panesでreadback確認済みです。実際のpixel描画・行layoutは未確認です。metadata clearはunit testでのみ確認しました。
- PTYでの画面出力確認は合成環境の80x30で実施しました。実利用者によるpixel・視覚確認ではありません。

SDK/APIのfixture testはmock/fixtureであり、実環境でのAPI照会と同一ではありません。設定確認やherdr readbackなどのad-hoc確認もpytest件数に含めません。

## Issue #2の回帰試験と精度上の限界

`tests/test_quota_accuracy.py::test_delayed_historical_percent_is_not_scaled_by_request_time_tokens` は遅延した履歴値と時刻差のあるローカルcounterでも `N/A` となることを検証します。関連するfixture/unit testsはsample校正拒否、model/cacheやreset/restart等の条件が精度を証明しないこと、SID単位のAPI raw reference表示、警告のopt-in/一度限りを検証します。これらはデータ処理のfail-closed/reference handlingを示すもので、実APIの正確さを示しません。

accounting window、denominator、counter alignment、token weightingの精度契約と、同一account/SID/periodで独立検証可能な参照値は **BLOCKED** です。この回帰試験で実APIまたはlive monitorは実行していません。

## 未確認事項

- 実画面のpixel描画とlayout、live metadata clear。
- Linux、30 Pane stress、全version/profile構成。
- 実環境でのあらゆるprofile衝突、Session移動・再開・compression分岐。
- model変更途中の正確なmodel別配分、actual costの取得。

## 記録の範囲

コミット対象の実装・設定・テスト10ファイルを固定した差分で独立レビューし、指摘はありませんでした。文書整理はこのレビュー対象外です。CI workflowの実行結果として記載したものはありません。
