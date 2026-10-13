# 検証記録

確認範囲と未確認事項を分けて記録します。以下の最終結果は、実装担当の報告だけでなく、統合後にHermesが再実行して確認した結果です。

## 実環境（読み取り中心）

- macOS / Hermes Agent v0.20.0（2026.8.3）。
- herdr CLI 0.9.2、server 0.9.0、protocol 22、schema 25。integration current v5。
- Hermes DBはschema_version 25、WAL。安全な列を `mode=ro` で確認。
- herdrは21 panesを返し、うち3件にSession IDがあり、3件ともDB内IDと完全一致。
- 別の一時monitor DBで3 tasks、279 catalog entriesを扱い、初回baselineをusage 0として記録。SQLite integrity check正常、directory 0700 / file 0600。
- history / export / showを操作確認。CSV BOMと既存出力の上書き拒否も確認。
- 一時PTYの80x30で `HEALTHY` を確認し、`?`、`r`、Enter、`f`、`q` 操作後に終了コード0。端末描画・キー操作の確認であり、人による視覚確認ではない。
- 実機検証中はサイドバー無効。metadata送信/clear、herdr再起動、integration更新、設定変更は未実施。
- Hermes自身が稼働中にDBを書き込むため、実機DBのhash不変は保証しない。fixture DBの不変性・書き込み拒否を自動テストで確認する。

## 受け入れケースのテスト根拠

各項目は実在するテスト名を記載しています。個別テストの存在は、該当ケースの最終実行が成功したことと区別します。証拠レベルは `fixture integration` / `unit` です。

| # | ケース | 実在するテスト | 証拠レベル |
|---:|---|---|---|
| 1 | 単一Paneの正確なID照合・使用量表示 | `test_collector_baseline_then_exact_attribution_and_persistent_task` — `tests/test_collector_acceptance.py` | fixture integration |
| 2 | 複数Paneのfixture取得とID照合 | `test_herdr_strict_envelope_and_agent_session` — `tests/test_phase1.py`（21-pane fixture、3 exact IDs） | fixture integration |
| 3 | 不明IDや衝突を推測リンクしない | `test_exact_matching_conflict_and_db_collision` — `tests/test_phase1.py` | unit |
| 4 | 欠損DB列を利用不可として扱う | `test_missing_columns_and_invalid_counters_are_unavailable` — `tests/test_phase1.py` | fixture integration |
| 5 | WAL readerとwriterの共存 | `test_wal_reader_does_not_block_writer` — `tests/test_phase1.py` | fixture integration |
| 6 | 累計差分100→150 / 20→35 | `test_collector_baseline_then_exact_attribution_and_persistent_task` — `tests/test_collector_acceptance.py` | fixture integration |
| 7 | 初回観測を期間差分にしない | `test_history_reports_observed_interval_delta_and_baseline_separately` — `tests/test_history.py` | fixture integration |
| 8 | compression継続は同一taskにまとめ、子Sessionのbaselineを重複計上しない。subagentは独立task | `test_compression_continuation_reuses_task_but_keeps_child_baseline`; `test_compression_continuation_does_not_join_subagent` — `tests/test_mapping_lifecycle.py` | fixture integration |
| 9 | Session移動でbaselineを重複生成しない | `test_moving_session_baseline_is_emitted_once` — `tests/test_final_contracts.py` | fixture integration |
| 10 | 複数Pane競合をunattributedにする | `test_conflict_unattributed` — `tests/test_mapping_lifecycle.py` | fixture integration |
| 11 | Pane消失・終了Sessionの履歴扱い | `test_panes_are_persisted_without_any_hermes_sessions` — `tests/test_durability.py`; `test_unchanged_ended_sessions_are_not_snapshotted_repeatedly` — `tests/test_final_contracts.py` | fixture integration |
| 12 | herdr失敗時にstaleを保持し復旧する | `test_first_herdr_failure_and_empty_success` — `tests/test_mapping_lifecycle.py`; `test_failed_herdr_does_not_refresh_pane_or_task_metadata` — `tests/test_durability.py` | fixture integration |
| 13 | CSV式対策と既存出力保護 | `test_csv_formula_guard_handles_leading_whitespace_and_controls`; `test_export_refuses_existing_output_without_force` — `tests/test_export.py` | unit / fixture integration |
| 14 | profile間の同一Session ID分離 | `test_manual_fallback_same_sid_is_scoped_to_profile_and_task` — `tests/test_review_regressions.py` | fixture integration |
| 15 | 欠損costをゼロ表示せず、大きな数値は桁を落とさず概数表示 | `test_stale_and_missing_values_are_not_rendered_as_zero`; `test_large_values_are_compact_and_missing_cost_stays_distinct_from_zero` — `tests/test_tui.py` | unit |
| 16 | sidebar無効時に外部コマンドを出さない | `test_disabled_makes_no_commands` — `tests/test_sidebar.py` | unit |
| 17 | source DBデータを変更しない | `test_source_values_are_sanitized_and_source_file_remains_unchanged` — `tests/test_durability.py` | fixture integration |
| 18 | 再起動後のtask継続と区間gapの扱い | `test_restart_persistent_task_but_gap_unattributed` — `tests/test_mapping_lifecycle.py` | fixture integration |
| 19 | 80列の本文行で数値セルを幅内の単位付き概数にし、丸め境界・非有限値を処理 | `test_body_row_compacts_each_numeric_cell_to_its_column_width`; `test_width_compaction_rounding_and_non_finite_values` — `tests/test_tui.py` | unit |

## 初回統合チェック（履歴）

以下は初回統合時点の検証記録であり、現在のテスト件数・実機状態を示すものではありません。

| コマンド | 結果 |
|---|---|
| `uv run pytest -q` | 92 passed（Hermes再実行: 0.35s） |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv build` | sdist / wheel ともにbuild成功 |
| `uv run --isolated --no-project --python 3.11 --with ./dist/sessiongauge-0.1.0-py3-none-any.whl --with pytest pytest -q` | 92 passed（0.34s） |

初回統合時、TUIワーカーで `session: null` のPaneが混在するケースと、収集時刻を最終活動時刻より優先するケースを追加して確認しました。修正後の実PTY検証も成功しました。これは初回フェーズの記録です。

80列の合成データ本文行（`>fixture`）: `>fixture       p1    working N/A       123.5M~ 42      987.65M~ 123.5M~ $123k~  `。Input / CacheRead / Total / EstUSDすべてに単位・概数記号が出ており、本文行全体は80列以内。

初回統合時、Python 3.11の隔離環境でビルドしたwheelをインストールして92件を再実行しました。pytestの設定にsrcへのpythonpath追加はなく、テスト用conftestもsrcをsys.pathへ挿入しません。独立したwheelのimport確認でもsite-packagesからロードされることを確認しました。

## 未検証・制限

- sidebar quota metadataの実際の描画、およびmetadata clear（clearはunit testのみ）。
- 実データでのprofile間ID衝突、全ての移動・再開・compression分岐。
- 30 active panesの実機stress、Linuxおよび未確認version。
- モデル切替途中の正確なmodel別分割、実費取得（調査時 `actual_cost_usd` はNULL）。
- 観測開始前の履歴と中断/再開時の所有関係。期間境界をまたぐ区間は除外されlower boundとなる。

## 初回統合後の追加実機確認（履歴）

以下はsidebar quota metadataの実機送信を行う前の初回フェーズの記録です。現在の状態は次節を参照してください。

- 独立した一時データディレクトリで `live --json --no-sidebar --poll-interval-seconds 2` を継続実行し、2つのJSONフレームを受信。両方ともhealthy、stale=false、15 Pane中3件がmatchedでした。初回調査の21 Paneとは確認時点が異なります。
- Ctrl-C相当のSIGINTで終了コード130。これはCLIの中断時の仕様です。TUIのq終了コード0とは区別します。
- 同じ監視データでhistory、CSV（UTF-8 BOM付き）/JSON export、showを確認。出力は0600で、既存出力の上書きは終了コード1で拒否され、ファイルのハッシュも変わりませんでした。
- 監視DBは3 tasks、280 session_catalog entries、316 snapshots、36 usage_attributions。integrity_check=ok、ディレクトリ0700 / DBファイル0600でした。これらの件数は継続監視テスト時点の値です。
- 最終ソースを実PTYの80列×30行で起動し、pyteで制御シーケンスを復元した画面にHEALTHYと3セッションが表示されることを確認。`?`、`r`、Enter、`f`、`q`を操作し、終了コード0、stderr 0 bytesでした。pyteは検証環境だけの補助ツールで、アプリの依存には追加していません。
- 形態素解析を使う補助的な日本語文書lintは、Sudachi未導入のため完走していません。これをアプリのRuff検査成功と混同しません。

人によるPTY画面の視覚確認はしていません。初回フェーズではSessionGaugeはHermes DBにmode=roで接続し、herdr server設定・利用者設定を変更せず、実機metadata送信も実施していません。

## 最新フェーズ（2026-10-14）

親セッションで実行した実コマンドと実環境での確認結果です。

| コマンド / 確認 | 結果 |
|---|---|
| `uv run --no-sync pytest -q` | 133 passed（0.35s） |
| `uv run --no-sync ruff check .` | All checks passed |
| `uv run --no-sync ruff format --check .` | 42 files already formatted |
| `uv build` | sdist / wheel ともにbuild成功 |
| Python 3.11隔離環境でfresh wheelをsite-packagesからimportしてpytest | 133 passed（0.36s）。テストはビルド成果物をimport |
| agent-resume認証済みhelperによるprimary/secondary window設定確認 | primary `limit_window_seconds=18000`、secondary `604800` |
| 新しいローカル非secret SessionGauge quota設定でbackground monitorの `live --json --sidebar` を実行 | 既存の `sessiongauge_usage` に数値のaccount quotaを追加して送信。herdr live snapshotで3 matched panesへの反映を確認し、次回updateでquota値が実際に増加 |
| 一時monitor DBを使ったno-sidebar CLI確認 | healthy、3 matched、期待するtarget profile。stderr空、一時ファイルを削除 |
| OS一時ディレクトリ内の `hermes-verify-*.py` によるherdr設定確認 | symlinkがdotfiles sourceを指すこと、既存display rowと `sessiongauge_usage` reference、keybindings不変を確認。native `herdr config check` 成功後、verifierを削除 |

herdr設定確認は一時verifierを使ったad-hoc確認であり、pytest suiteの一部ではありません。このフェーズではherdr serverの再起動もherdr設定layoutの追加編集も行わず、既存display referenceを再利用しました。SessionGaugeは既存のread-only adapter経由でHermes DBを読み、独立したmonitor DBを所有します。認証情報は既存のagent-resumeにのみ委譲しました。

実際の送信と次回updateでのquota反映は確認済みですが、新しいquotaの実画面描画とlive metadata clearは未確認です（clearはunit testのみ）。live accountの割合・token数・個人パス・IDはここに記録しません。Linux、30 panes、load時の検証も未実施です。
