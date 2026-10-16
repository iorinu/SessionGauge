# 検証記録

ここではfixture test、ビルド確認、実環境での読み取り・metadata確認を区別します。以下のテスト結果は最終実装チェックの記録です。文書変更後の独立reviewを意味しません。

## 旧revisionの実装チェック（歴史記録）

| コマンド | 結果 |
|---|---|
| `uv run --no-sync pytest -q` | 207 passed（0.40s） |
| `uv run --no-sync ruff check .` | 成功 |
| `uv run --no-sync ruff format --check .` | 45 files already formatted |
| `uv build` | sdist / wheel build成功 |
| Python 3.11 fresh wheelを隔離環境でsite-packagesからimportしてpytest | 207 passed（0.49s） |

pytestはfixture・unit testで、実API呼び出しを行いません。受け入れケース18件のテスト名と証拠レベルは[要件トレーサビリティ](requirements-traceability.md#18受け入れケース)から[テスト根拠表](verification.md#受け入れケースのテスト根拠)を参照してください。

## window範囲対応revisionの確認

| コマンド | 結果 |
|---|---|
| `PYTHONPATH=$PWD/src uv run --no-sync pytest -q`（前回チェックポイント） | 388 passed（0.74s） |
| `PYTHONPATH=$PWD/src uv run --no-sync pytest -q`（現チェックポイント） | 396 passed（0.65s） |

これらの実行はfixture/unit testで、実API照会ではありません。作業treeの `src` を使います。前回チェックポイントからの8ケース追加は既存挙動のcharacterizationで、新しい不具合修正やTDD REDではありません。
追加した `test_reference_before_window_calibrates_current_window_sidebar_estimate` は[window範囲方式のテスト根拠](#window範囲方式のテスト根拠)に記載しています。期待値には合成rateを用います。合成rateは期待値計算のみのもので、実際のprovider重みは不明であり、productionではrateを使用しません。

このrevisionの追加確認は次のとおりです。歴史的な207件の結果とは別の実行・証拠です。

| 確認 | 結果 |
|---|---|
| `uv build --offline` | sdistとwheelを生成 |
| Python 3.11.15の隔離venvへwheelとpytestをofflineで導入し、site-packagesからimportしてpytest（`PYTHONPATH`なし、作業treeからテスト実行） | 前回388 passed（0.59s）、現396 passed（0.69s） |
| wheelからのCLI `--help` | 成功 |

## 遅延review指摘の扱いと回帰characterization

親は新規テストの意味を確認しました。新規テストへの独立reviewを行ったものではありません。親SHA256比較では初回独立review対象のproduction 7ファイルに差分はありません（変更0件）。

| 論点 | 扱い・確認範囲 |
|---|---|
| sampleがwindow開始前であること | 初回reviewの指摘です。採用済みの条件付きモデルでは `P_ref` をcurrent planのnormalized lifetime budget相当と置き、as-of時点のreference counter `F` とcurrent windowの差分 `G` を分けるため、window前sampleを拒否する条件は採用しません。親はこれを仕様・provider cutoff仮定として分類しました。後続の独立reviewはこの条件付き契約を明示的に受け入れ、確認できる誤りなしとしました。`test_reference_before_window_calibrates_current_window_sidebar_estimate` は仮定下の算術・配線を示すだけで、native APIの意味を証明しません。 |
| profile間の同一SID | `test_duplicate_sid_in_distinct_profiles_blocks_both_sidebar_targets` は衝突前に一方のpaneが対象となれることと、衝突後は両対象を照会せず更新しないことを確認します。以前のowned metadataはTTLで失効するのみです。複合profile選択機能ではありません。 |
| 疎な履歴とreset | `test_sqlite_wide_gap_keeps_bracket_and_conditional_usage_ranges` は広がったbracketと条件付き範囲、`test_sqlite_observed_reset_inside_gap_refuses_window_usage` は記録されたresetの拒否を確認します。長いpoll gap内で未観測のresetと回復が起きた場合、端点から検出できません。 |
| close中の処理・account scope変更 | `test_close_does_not_cancel_running_worker_or_publish_its_result` は実 `concurrent.futures.Future` とfake executorで実行中処理のclose後結果を無視することを確認します。`test_account_scope_change_refresh_does_not_mix_old_account_reference` はscope変更後に新scopeの参照へ置き換えてrefreshすることを確認します。 |
| pending中のTTL・reference TTL | `test_pending_refresh_does_not_extend_cached_context_ttl` はpending refreshでcontext TTLを延長しないこと、`test_reference_ttl_is_independent_and_rpc_cannot_rejuvenate_reference` は同じas-ofの2回目のrequest完了後もreferenceを若返らせないことを確認します。 |

上記は `tests/test_window_review_regressions.py` の7関数・8ケース（parametrize 2件を含む）です。すべて既存挙動のcharacterizationです。

## window範囲方式の実環境probeと合成確認

- read-onlyのNEW DRIVER確認では、通常bridge経由の `WindowQuotaEstimator` async futureを使い、2 targetと利用可能な過去reference 1件から、両targetに有限の推定範囲が返ることを確認しました。context開始直後はpendingで推定なし、完了後は2件の推定がありました。close後にfresh engineを構築して再確認し、同じ形式の範囲が2件返りました。main threadの観測は約12ms（単回測定12.04/11.76ms）です。benchmark保証ではありません。
- この確認ではmetadata書き込み、install、service restart、pixel表示確認はしていません。fresh engineによる再構築であり、実monitorの再起動確認ではありません。実際の範囲値・ID・利用者pathは記録しません。
- wheelを使った追加ad-hoc確認では、既知のrateを挟む決定的な合成ケース10,000件すべてで合成期待rateを包含しました。また、model変更、mixed model、reference失効、counter reset、refresh失敗、新accountでreferenceなし、close後に完了した遅延futureの7 lifecycle caseが通りました。これらは追加pytestではなく、providerでの経験的精度確認でもありません。
- 実際の誤差・providerに対する精度は依然未知です。

## その後のruntime適用

- 条件付きestimatorの適用について明示的な承認を受けた後、SessionGaugeのみを停止し、wheelを更新して既存のowned LaunchAgentで再起動しました。Hermes/herdr本体は再起動していません。
- installed production Python modules 23件はwheelのbytesと一致し、rollback wheelは更新前のinstalled modules 20件と一致しました。設定、LaunchAgent、plugin filesは適用前後でbyte-identicalでした。
- producerは1つだけ稼働し、両collector sourceにfailure・stale stateはありませんでした。SIDを正確に確認できた2 paneでは6秒間に3回のreadbackすべてでestimated metadataを確認し、SID未確認の1 paneは対象外でした。
- pixel-level UI表示と推定精度の実測検証は未実施です。

## 受け入れケースのテスト根拠

以下のテスト関数は現行checkoutに存在する名前です。関数名の列挙自体は個々のcaseのpass証明ではなく、実行結果は上の207件の記録と区別してください。207件は旧revisionの歴史的記録です。

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

SDK/APIのfixture testはmock/fixtureであり、実環境でのAPI照会と同一ではありません。設定確認やherdr readbackなどのad-hoc確認もpytest件数に含めません。window方式のread-only NEW DRIVER確認の範囲と限界は上記に記録しています。

## Issue #2の回帰試験と精度上の限界

`tests/test_quota_accuracy.py::test_delayed_historical_percent_is_not_scaled_by_request_time_tokens` は遅延した履歴値と時刻差のあるローカルcounterでも `N/A` となることを検証します。関連するfixture/unit testsはsample校正拒否、model/cacheやreset/restart等の条件が精度を証明しないこと、SID単位のAPI raw reference表示、警告のopt-in/一度限りを検証します。これらはデータ処理のfail-closed/reference handlingを示すもので、実APIの正確さを示しません。

旧token比例式 `P0*T/T0` は停止済みです。新しいwindow範囲方式はADR 0002に記載した条件付き推定で、精度契約や独立検証済みの誤差上限を得たものではありません。fixtureの合成rateで計算が一致することは数学処理の確認であり、上流意味の検証ではありません。実APIの参照は1件のread-only probeに限られ、独立したempirical validationではありません。

## window範囲方式のテスト根拠

| 対象 | 現行テスト関数 | 根拠 |
|---|---|---|
| 4 bucket計算・合成rate | `test_estimate_bounds_uses_independent_bucket_weighting_for_window_usage`; `test_bounds_cover_mixed_rates_and_open_unobserved_bucket`; `test_bracket_widening` — `tests/test_window_quota.py` | unit、合成値のみ |
| 不正値・不定上限 | `test_bounds_fail_closed_for_bad_ranges_and_keep_overage`; `test_invalid_percentages_fail_closed`; `test_invalid_bucket_lengths_and_values_fail_closed` — `tests/test_window_quota.py` | unit |
| 履歴read-only・profile・bracket・reset | `test_history_is_readonly_scoped_and_brackets_uncertainty`; `test_history_rejects_wrong_profile_missing_bracket_and_counter_reset`; `test_ended_session_can_reuse_last_stable_observation`; `test_reference_candidates_excludes_before_limit`; `test_window_rejects_future_start_and_fresh_reset`; `test_active_session_without_after_observation_is_unknown`; `test_same_sid_in_another_profile_is_not_a_reference`; `test_mixed_model_refusal` — `tests/test_quota_history.py` | fixture integration |
| async lifecycle・参照選択・鮮度 | `test_pending_job_uses_real_buckets_window_and_one_reference_job`; `test_reference_candidates_may_be_empty_for_current_sid_api_sample`; `test_context_age_starts_when_job_completes_not_when_consumed`; `test_expired_window_and_reference_do_not_retain_prior_estimate`; `test_close_is_idempotent_and_prevents_resubmission` — `tests/test_window_estimator.py` | unit/fake |
| 過去referenceによる現windowのsidebar推定 | `test_reference_before_window_calibrates_current_window_sidebar_estimate` — `tests/test_window_quota_flow.py` | fixture E2E、synthetic ratesでの期待値のみ |
| 表示・factory | `test_window_formatter_fails_closed_and_preserves_unbounded_ranges`; `test_cli_factory_uses_collector_data_dir_and_source_profile`; `test_estimate_mode_displays_empty_and_updated_engine_results`; `test_disabled_or_stale_sidebar_does_not_call_estimate_engine`; `test_close_closes_injected_estimate_engine_once`; `test_api_mode_displays_api_quota_with_injected_estimate_engine` — `tests/test_window_sidebar.py` | unit/fake |

## 未確認事項

- 実画面のpixel描画とlayout、live metadata clear。
- Linux、30 Pane stress、全version/profile構成。
- 新window driverの実環境metadata readbackは確認済み。実pixel描画と実運用での精度検証は未確認。
- 実環境でのあらゆるprofile衝突、Session移動・再開・compression分岐。
- model変更途中の正確なmodel別配分、actual costの取得。

## 記録の範囲

旧revisionでは、実装・設定・テスト10ファイルを固定した差分で独立レビューし、指摘はありませんでした。この歴史的記録は旧revisionの範囲です。

別途、独立read-only OpenCode gpt-6-luna reviewを、production 7ファイル、test 5ファイル、ADR 0002の計13ファイルに対して実施しました。SHA256の前後比較で対象が不変であることを確認し、`passed=true`、`security_concerns=[]`、`logic_errors=[]`、low指摘2件でした。reviewではテストやSDKを実行していません。このreview後に追加したE2E testは親が確認済みで、独立reviewの対象外です。

CI workflowの実行結果として記載したものはありません。
