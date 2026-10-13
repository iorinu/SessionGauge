# SessionGauge — 要件定義・Codex実装引き継ぎ書

- Version: 1.0
- Date: 2026-10-13
- Status: Implementation-ready specification (integration details subject to local verification)
- Target: macOS / terminal-first, Hermes Agent running in multiple herdr panes
- Product name: **SessionGauge**
- GitHub repository: `session-gauge`
- CLI command: `sessiongauge`
- Python package: `sessiongauge`
- Initial integrations: Hermes Agent + herdr (future expansion to other agents)

## 0. Codexへの最重要指示

**命名ルール**: ユーザー向け正式名称は `SessionGauge`、GitHubリポジトリは `session-gauge`、CLIとPythonパッケージは `sessiongauge` に統一する。Hermes/herdrは初期アダプターとして扱い、プロダクト名や公開APIを特定のエージェントに依存させない。

本書を起点として、このリポジトリに**動作する実装、テスト、README、設定例**を作成すること。単なる設計案や擬似コードで終わらせない。ただし実機環境でしか確認できない情報は推測で固定せず、環境調査と安全なフォールバックを実装すること。現行のユーザー環境を壊す操作はしないこと。

**最初に確認すること**: `herdr --version`, `herdr pane --help`, `herdr pane list --help`, `herdr pane report-metadata --help`, `herdr integration status`, `hermes --help`, `hermes sessions --help`, Hermesホームディレクトリ、SQLiteスキーマ。コマンドや列が存在することをチェックしてから依存すること。バージョンによってAPI/出力が異なる場合はアダプター化すること。

実装の進行順は **調査 → 最小限の取得・照合 → 永続化 → live TUI → history / export → herdr sidebar → テスト・文書化**。既存のHermes DBは**絶対に書き換えない**。未知の対応関係を推測して誤帰属させない。

## 1. 背景と目的

ユーザーはmacOSでHermes Agentをターミナルから使い、herdrで複数のHermesタスクを並列実行している。各タスクがどの程度のトークンを消費し、どのモデル・コストで実行されたかを把握したい。

### ゴール

1. herdrの各Pane / タスクとHermesのセッションを対応付ける。
2. 複数タスクの **Input / Output / Cache Read / Cache Write / Reasoning / Total / Estimated Cost / Actual Cost（存在する場合）**をリアルタイムに近い頻度で表示する。
3. 終了済みのタスクも保存し、日付・タスク・モデル・プロジェクトで履歴を検索・集計できるようにする。
4. タスク名変更、Pane閉鎖、セッション再開、Hermesの圧縮によるセッション分岐を扱う。
5. CSV/JSONでエクスポートできるようにする。
6. 可能であればherdrサイドバーに累計トークンを表示する。
7. 完全にローカル動作し、利用者の会話テキストやAPIキーを外部送信しない。

### 非ゴール（MVP）

- Hermes/herdr本体へのパッチ適用
- プロバイダー請求額を完全再現すること（プロバイダーごとに料金体系が違う）
- モデル生成中のトークンを文字単位で計測すること
- 過去に保存されていないPane⇔Sessionの紐付けを完全復元すること
- 他のCoding Agent（Codex/Claude Codeなど）への対応（後続拡張）
- クラウドダッシュボード / チーム共有

## 2. 確認済みの公開仕様と未確認事項

### 確認済み

- Hermesの永続化先はデフォルト `~/.hermes/state.db`（SQLite, WAL）。`HERMES_HOME` または名前付きプロファイルで別DBになる。
- `sessions` テーブルには `id`, `source`, `model`, `title`, `parent_session_id`, `started_at`, `ended_at`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `estimated_cost_usd`, `actual_cost_usd` 等がある（最新仕様の例。実機PRAGMAで検証）。
- herdrにはHermes Agent用の公式インテグレーションがあり、`herdr integration install hermes` で導入できる。Hermesの再起動が必要。セッションIDをherdrへ報告する。
- herdrには `pane list` / `pane get` と `pane report-metadata` がある。正確な引数、JSONスキーマ、サイドバー側表示条件は導入バージョンで検証する。

### 未確認（Codexが実機で調査する）

- `herdr pane list` のJSON出力オプションと全フィールド名（Pane ID、タイトル、cwd、Agent Session ID、状態など）
- `herdr pane report-metadata` のローカルでのサポートと具体的な表示設定
- 実機 `state.db` のスキーマバージョン・列の有無
- Hermesのトークン数がどのタイミングでDBへ反映されるか
- 複数プロファイル利用状況
- 同一Paneでの複数セッション / 同一セッションを別Paneで再開した場合の実データ形状

※ 以前の会話で示した単純な `SELECT title, model, ... FROM sessions` は**実機未検証**。列が存在しない場合の処理を実装すること。

## 3. 想定アーキテクチャ

```text
           herdr CLI / local API                   Hermes Session DB(s)
           pane IDs, title, cwd,                   sessions table (tokens, cost,
           agent session ref, state                model, timestamps, lineage)
                      |                                      |
                      v                                      v
                  HerdrAdapter                         HermesDBAdapter
                      \                                      /
                       \                                    /
                        v                                  v
                          Matcher / Identity Resolver
                          (confirmed links only; warnings)
                                      |
                                      v
                              Collector (every 2–5s)
                                      |
                       +--------------+--------------+
                       |                             |
                       v                             v
            independent local SQLite              live snapshot
            ~/.local/share/sessiongauge/                  in-process
            monitor.db (read/write)                  |
                       |                      +-------+-------+
                       v                      |               |
                History / Export             TUI         MetadataReporter
                                                       (optional herdr)
```

**重要**: Hermesの `state.db` は読み取り専用。独自の `monitor.db` のみ書き込み可。herdrの `report-metadata` は明示フラグが有効な場合に限定。

## 4. 機能要件

### FR-01: セットアップ・診断（必須）

`sessiongauge doctor` を提供し、以下をチェックする。

- OS、Pythonバージョン、Hermes/herdrのコマンド有無とバージョン
- herdrサーバーへの接続可否、Pane一覧の取得可否
- herdr–Hermes integrationの検出（導入コマンドは案内するが勝手にinstallしない）
- Hermes DBのパス（環境変数 `HERMES_HOME` を優先し、プロファイルは明示設定・探索で扱う）
- SQLite schema, 読み取り権限, WALモード
- サンプルPaneとSessionのIDが照合可能か（秘密や会話内容は出力しない）
- 使えない機能、推奨対処、実装アダプタの互換性を表示

機密情報のある環境ログは全文コピーさせず、サニタイズした診断情報を出力する。

### FR-02: Hermesセッション読取（必須）

- `sqlite3` の URI `mode=ro` で読み取る。未存在DBは作らない。
- `PRAGMA table_info(sessions)` で列を検出する。欠損列は `NULL`/Unavailableとして扱う。
- 各DB / プロファイルのセッションIDを **(db identity, session_id)** として扱い、他DBとのID衝突を防ぐ。
- `started_at` / `ended_at` はUnix秒などを実測して正規化し、TZは表示時だけユーザーTZで変換する。
- タイトル、モデル、ソース、各トークン、推定/実費、親子セッション、ワークスペース(cwd / repo)があれば取得する。
- システムプロンプト/メッセージ本文/ツール引数/APIキーは読まない・保存しない。
- `input_tokens + output_tokens` を `total_tokens` と表示する。Cache/Reasoningは定義がプロバイダーで異なり、無条件加算しない。
- 欠損・未計測は0と区別して `N/A` と表示。DBの値が負、NULL、単調増加しない場合に備える。

### FR-03: herdr Pane取得（必須）

- `herdr pane list` または対応する公式ローカルAPIでPane一覧を取る。
- JSONがサポートされていればJSON優先。安定した機械可読形式が無ければ、CLIソース/APIを調査し、テキストパースに依存しない方法を優先する。
- Pane ID、タイトル、cwd、ステータス、Agent種別、Agent Session IDを取得（取得不能ならN/A）。
- herdrの応答遅延やサーバーダウンで監視全体を停止しない。
- Pane数が0でもHistory/Exportは使える。

### FR-04: タスクとセッションの対応付け（最重要、必須）

優先順位：

1. herdrの公式Hermes integrationが報告した**正確なSession ID**。
2. 自システムが過去に確定保存したPane–Session対応関係。
3. ユーザーによる明示的な `sessiongauge link <pane-id> <session-id> [--profile ...]`。

**禁止**: 同じcwd / タイトル / タイムスタンプ / 直近使用セッションという理由**だけ**で自動的に確定リンクすること。そのような情報は候補提示にしか使わない。

- identity: `task_id`（本ツールの永続UUID）, `pane_id`, `hermes_profile`, `session_id`, `valid_from`, `valid_to`, `link_source`, `confidence` を分ける。
- Paneが閉じられてもTask履歴を残す。
- Pane内で新しいSession IDが報告されたら、タスクの継続か新規かを自動断定せず、同Pane内のセッション交代として記録し、可能ならユーザー設定によるポリシーで扱う。
- セッションの圧縮/分割では `parent_session_id` の系譜を取得し、同一Taskへ結合できる。複数Taskへの二重計上は禁止。
- セッション再開（別Paneで同一セッション）では、その時点までの総量を複数Taskへ丸ごと足さない。セッション累計の**観測差分**を時間に応じてタスクに帰属する設計とし、正確な帰属ができない時間帯は `unattributed` に分離して注記。
- 同じSession IDが複数Paneで同時に見つかった場合は競合を表示し、自動集計の重複を避ける。
- 過去データで対応付けが不明なら `unlinked` と表示し、数字を捏造しない。

### FR-05: 監視・収集（必須）

- デフォルト3秒間隔（設定可能：2〜60秒）。ユーザー指定で更新。
- 少なくとも1回のHermes DB書き込みが完了した次のポーリングで表示更新されることを目指す（DB更新タイミングには依存）。
- 各セッションの累計カウンタを定期的にスナップショットとして永続保存する。
- 新規セッション発見、Pane変更、タイトル変更、コスト増分、停止・復帰を記録する。
- エラー時は指数バックオフ／次回再試行。最後の正常スナップショットを「stale」と明示。
- 履歴時系列は将来的な時間別トークン消費グラフを生成できる形式で保存。
- ポーリング中のDBロック/スキーマ変化/再接続時に壊れない。

### FR-06: TUIライブ画面（必須）

最低限、次のカラム：

| Task | Pane | State | Model | Input | Output | Cache Read | Total | Est. USD |
|---|---|---|---|---:|---:|---:|---:|---:|
| auth API | 3 | working | model-x | 420k | 38k | 190k | 458k | $1.23 |
| tests | 5 | idle | model-y | 180k | 24k | 60k | 204k | $0.48 |

上記数値はサンプル。

- タスク行の選択・詳細（Session ID、全トークン項目、実費/推定費、開始時刻、リンク状態、子セッション）。
- ソート（total / cost / last activity）、フィルター（active / all / unlinked / profile）。
- 全体合計、観測日時、未対応Pane数、stale表示。
- `q`で終了、`r`で手動更新、`?`でキー表示。
- 80桁の狭いターミナルでも崩れず、省略カラム表示。
- 技術選択例：Python + Textual。依存最小化を優先する場合はRich Liveでも可。選んだ理由をREADMEに記載。
- コストとトークンの「直近1分の増加量」はスナップショットの差分から計算。観測間隔外は推定と表示。

### FR-07: 履歴・集計（必須）

`sessiongauge history --days 30`, `sessiongauge history --group-by task|session|model|day|project`, `sessiongauge show <task-id>`。

- 過去に本ツールが記録したタスク名、Pane履歴、セッション対応、トークン差分を復元。
- 本ツール導入以前のHermesセッションも読み取り、Session単位のバックフィルを実施する。ただし昔のPane/Taskは復元不可なら `unlinked` とする。
- 期間集計は期間内のスナップショット**差分**に基づく。本ツール導入前からの累計を「その日の消費量」として誤集計しない。
- 対象期間前の基準値がない場合は「minimum observed」または「unknown historical usage」として明示。
- 1つのHermesセッションのトークンを複数回カウントしない。
- コストはestimatedとactualを区別し、actualが未提供なら「0ドル」と表示しない。

### FR-08: エクスポート（必須）

- `sessiongauge export --format csv|json --days 30 --output <path>`。
- `task_id`, task title, pane IDs, project, profile, session IDs, timestamps, token counts, cost, mapping status, data sourceを出す。
- Excel互換のUTF-8 BOMはオプション。CSV式インジェクション（`=`, `+`, `-`, `@`等）を防止。
- 出力先ファイルを上書きするときは `--force` または確認を要求。

### FR-09: herdrサイドバー連携（MVPではオプション）

- `sessiongauge live --sidebar` や設定の明示ON時だけ `herdr pane report-metadata` を使う。
- reporter用の固有 `--source` ID、適切な `--ttl-ms`、単調な `--seq` を使い、他プラグインの状態ラベルを上書きしない。
- トークンメタデータは例: `usage=458k`。表示テンプレート構文は実機herdrのドキュメントに従う。
- 終了時・リンク解除時は自分が所有するトークンをclear、またはTTL失効。公式API仕様に従う。
- 非対応バージョンでは警告してTUIのみ継続する。
- 可能なら `--sidebar-only` のバックグラウンドプロセス/launchd常駐を後続タスクとして検討。MVPではデーモン化不要。

### FR-10: 設定（必須）

- `~/.config/sessiongauge/config.toml` とCLIオプション。優先順位 CLI > ENV > TOML > defaults。
- `poll_interval_seconds`, `hermes_db_paths`, `data_dir`, `sidebar.enabled`, `sidebar.ttl_ms`, `time_zone`。
- 設定例を同梱。`HERMES_HOME` と名前付きプロファイル対応を明記。
- 適切なエラー表示と `--verbose` / `--debug` を提供。ただし秘匿ログは出さない。

## 5. 推奨データモデル（monitor.db）

具体的なDDLはCodexが整備してよいが、以下のエンティティ・制約を満たすこと。

- `profiles`: id, name, hermes_db_path (重複防止)
- `tasks`: task_id(UUID PK), display_name, project_path, created_at, closed_at
- `panes`: pane_key（herdr server instance + pane ID）, title, cwd, first_seen, last_seen, status
- `task_pane_links`: task_id, pane_key, valid_from, valid_to, source
- `session_catalog`: (profile_id, session_id)複合キー, parent_session_id, model, source, started_at, ended_at
- `task_session_links`: task_id, profile_id, session_id, valid_from, valid_to, link_source, mapping_status
- `snapshots`: (profile_id, session_id, collected_at), input/output/cache/reasoning counters, cost counters, schema_version
- `usage_attributions`: profile_id, session_id, interval_start/end, task_id nullable, deltas, confidence, provenance
- `sync_state`: last_successful_poll, discovered_schema_version, warnings

必要なindex、FK、UNIQUE制約、スキーママイグレーション（SQLite `user_version` 等）、クラッシュ時の復旧を実装する。スナップショットから差分を再計算可能にし、集計結果の二重保存で不整合を招かないこと。導入直後の初回読み取りでは「現時点累計」と「導入後増分」を明確に分ける。

## 6. 非機能要件

- **安全性**: Hermes DBへのDML/DDL禁止。接続はread-only。SQLite URIやファイル権限で保護。インストールや設定更新はユーザーへの案内のみ。
- **正確性**: 誤帰属より `unknown`/`unattributed` を優先。重複排除、欠損とゼロを区別。
- **性能**: 10〜30 Pane程度を想定し、3秒周期で大きな負荷をかけない。増分読取や索引を使う。
- **ローカル完結**: ネットワーク送信・外部テレメトリなし。
- **互換性**: macOS優先、Linuxでの動作も可能な設計。herdr/Hermesバージョン差に対処。
- **堅牢性**: エラーで収集処理全体が落ちない。部分故障を明示。
- **プライバシー**: プロジェクト名・パス・タスク名を含むローカルDBは所有者のみ読み書き可能な権限とする。
- **配布**: `pyproject.toml`、インストール手順、`sessiongauge` エントリポイント、テストを提供。

## 7. 実装フェーズ・順序

### Phase 0 — Discovery（最優先、ブロッカー解除）

1. 必要なCLIの存在とバージョンを調査。
2. `herdr pane list/get` の正確なJSON/API仕様を検証。
3. ローカルHermes DBに `PRAGMA table_info(sessions)` を実行し、実カラム確認。
4. インテグレーションの状態とSession ID連携を実際に確認。
5. 仕様・発見・バージョン・制限を `docs/discovery.md` に書き、アダプターの入力サンプルを個人情報を伏せてfixture化。

### Phase 1 — Core/MVP

- CLI `doctor`, `live`, `history`, `export`
- Hermes read-only DB adapter / Herdr adapter / mapping engine
- ローカル永続DBとバックフィル
- 最低限のライブ画面、履歴ランキング
- 自動/手動のマッピング
- pytestでユニット/統合テスト

### Phase 2 — Usability

- TUIのフィルタ・ソート・詳細画面
- 複数DB / プロファイル
- セッション分岐/再開時の正確な差分帰属
- CSV/JSON整備とREADME

### Phase 3 — herdr sidebar

- `report-metadata`サポートの機能検出
- TTL更新、競合回避、終了時clear
- サイドバー表示の具体的な設定例

### Phase 4 — Optional

- 時系列グラフ、日別/モデル別比較、料金/予算アラート
- daemon/launchdによるバックグラウンド収集
- 他のCoding Agentにも対応可能な抽象インターフェース

## 8. テスト要件と受け入れ基準

最低限、次のテストを用意し全件通す。

1. **単一Pane**: Hermes Session IDが一致し、Total = Input + Outputになる。
2. **3つの同時Pane**: 異なるセッションを誤混合せず表示。
3. **不明ID**: cwdとタイトルが同じでも自動リンクしない。
4. **DB欠損カラム**: 落ちずにN/Aへフォールバック。
5. **DBロック/WAL**: リトライ可能、Hermesの書込みを妨げない。
6. **差分**: input 100→150/output 20→35で増加は50/15、表示累計は150/35。
7. **初回観測**: 既存累計を観測期間内の新規使用として数えない。
8. **セッション分割**: `parent_session_id` をたどって系譜を計算し、二重計上しない。
9. **セッション再開**: 同じIDが別Paneに移っても全累計を再課金/再計上しない。
10. **同時重複Pane**: 一意に帰属できない増分をunattributedにする。
11. **Paneクローズ**: 履歴には残り、ライブではInactive。
12. **herdr切断**: 最後の取得値をstale表示し復旧後再同期。
13. **CSV**: カラム順/UTF-8/CSV injection対策を検証。
14. **複数プロファイル**: 同一Session IDでもprofileが違えば別セッション。
15. **コスト欠損**: `actual_cost_usd=NULL` を `$0.00` と表示しない。
16. **Sidebar disabled**: herdrに書き込みコマンドを発行しない。
17. **Read-only検証**: Hermes DBのデータ/スキーマを変更しない。
18. **リスタート**: monitor再起動後もTask/Panes/履歴を復元。

**完成の定義 (DoD)**:

- `sessiongauge doctor` が対応状況とブロッカーを説明できる。
- `sessiongauge live` が実環境で少なくとも1つのHermes Paneにセッショントークンを対応付けて表示できる（実機が使える場合）。
- `sessiongauge history --days 7` が時系列/履歴を出し、未計測期間を誤表示しない。
- `sessiongauge export --format csv` で解析可能なファイルを生成できる。
- すべての自動テスト合格、READMEにコマンド例と制限を書く。
- サイドバーが利用できない場合もCoreが動く。

## 9. 実装想定リポジトリ構成

```text
session-gauge/
├── pyproject.toml
├── README.md
├── config.example.toml
├── docs/
│   ├── discovery.md
│   ├── architecture.md
│   └── known-limitations.md
├── src/sessiongauge/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── adapters/
│   │   ├── hermes_db.py
│   │   └── herdr.py
│   ├── identity.py
│   ├── collector.py
│   ├── persistence.py
│   ├── accounting.py
│   ├── tui.py
│   ├── history.py
│   ├── export.py
│   └── sidebar.py
└── tests/
    ├── fixtures/
    ├── test_hermes_db.py
    ├── test_herdr_adapter.py
    ├── test_identity.py
    ├── test_accounting.py
    ├── test_persistence.py
    └── test_cli.py
```

## 10. Codexが最初に実行するコマンド（安全な読取のみ）

```bash
command -v hermes; hermes --version 2>/dev/null || true
command -v herdr; herdr --version 2>/dev/null || true
herdr integration status 2>/dev/null || true
herdr pane --help 2>/dev/null || true
herdr pane list --help 2>/dev/null || true
herdr pane report-metadata --help 2>/dev/null || true
hermes sessions --help 2>/dev/null || true
printf 'HERMES_HOME=%s\n' "${HERMES_HOME:-<unset>}"
ls -l "${HERMES_HOME:-$HOME/.hermes}/state.db" 2>/dev/null || true
# DBが存在する場合だけschema確認（SELECT/PRAGMAのみ、CREATEしない）
DB="${HERMES_HOME:-$HOME/.hermes}/state.db"
if [ -f "$DB" ]; then
  sqlite3 "file:$DB?mode=ro" 'PRAGMA table_info(sessions);'
fi
```

**注意**: このコマンド群はデフォルトprofileの例。名前付きprofileや別パスは`doctor`で追加検出すること。`herdr pane list` などの戻り値は個人のプロジェクト名を含む可能性があるため、公開ログへ転載しない。

## 11. Codexに投げる開始プロンプト（コピペ可）

```text
このリポジトリの SESSION_GAUGE_REQUIREMENTS.md をすべて読んで、そこに記載された sessiongauge を実装してください。

最優先は Phase 0 の実環境調査です。herdr/HermesのバージョンやAPIの形を推測せず、ローカルCLIとSQLiteスキーマを確認してください。確実なSession ID紐付けを守り、Hermes DBは必ず読み取り専用にしてください。未知の紐付けや期間不明な差分はunknown/unattributedとして扱い、勝手に既存の設定ファイルを書き換えないでください。

Phase 1のdoctor/live/history/exportと永続化を完成させ、可能ならPhase 2と3まで進めてください。実装にはpytestによる自動テストとREADMEを含めてください。変更したファイル、実行したテスト、実機で確認できたこと/未確認なこと、動かすコマンドを最後に報告してください。

必要なら仕様の細部は合理的に補完してよいですが、集計精度、読み取り専用、安全性の要件は緩めないでください。作業を設計段階で止めず、可能なところまで実装してください。
```

## 12. 参考公式資料

- Hermes — Session Storage: https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/
- Hermes — Sessions: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md
- herdr — Integrations: https://herdr.dev/docs/integrations/
- herdr — CLI Reference: https://herdr.dev/docs/cli-reference/
- herdr — GitHub: https://github.com/motionharvest/herdr

**資料は変更される可能性がある。実機の `--help` とインストール済みバージョンを優先すること。**
