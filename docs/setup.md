# セットアップと運用

## 必要なもの

- Python 3.11以上
- [uv](https://docs.astral.sh/uv/)
- セッション情報を表示するにはHermes Agent。Paneとの自動照合にはherdrとHermes integration

SessionGauge自身の実行時依存ライブラリはありません。TUIはPython標準の `curses` を使います。

## 開発環境で実行

```sh
uv sync --dev
uv run sessiongauge doctor
uv run sessiongauge live
```

`doctor` はOS/Python、Hermes・herdr CLIの有無と取得可能なversion、herdr integration status、pane到達性、DBのread-only互換性とschema、照合可能数をJSONで報告します。installやintegration更新は実行しません。

ライブTUIを使わず、一度だけテキスト出力するには `uv run sessiongauge live --once`、JSONなら `uv run sessiongauge live --once --json` を使います。

最初は `doctor` と `live --once` で読み取り状態を確かめてから継続監視を始めてください。会話内容、Session ID、認証情報を共有する必要はありません。

HermesのDBは通常 `~/.hermes/state.db` です。`HERMES_HOME` が設定されている場合はその配下を使います。検出された名前付きprofileも対象です。明示的に指定する場合:

```sh
uv run sessiongauge --hermes-db ~/.hermes/state.db --hermes-db ~/.hermes/profiles/work/state.db doctor
```

個人情報を含むパスや出力を不用意に共有しないでください。トラブル調査ではまず `doctor` のエラー種別と互換性情報を確認し、会話内容・認証情報・実際のSession IDは共有しないでください。

## 設定

必要な場合だけ `config.example.toml` を参考に `~/.config/sessiongauge/config.toml` を作成します。SessionGaugeはこの設定を自動生成・変更しません。優先順位は次の通りです。

1. コマンドライン引数
2. 環境変数
3. TOML設定
4. 既定値

主な環境変数:

| 環境変数 | 用途 |
|---|---|
| `HERMES_HOME` | Hermesホームの指定 |
| `SESSIONGAUGE_CONFIG` | TOML設定ファイルの指定 |
| `SESSIONGAUGE_HERMES_DB_PATHS` | 読み込むDBパス（OSのpath separator区切り） |
| `SESSIONGAUGE_DATA_DIR` | 独自監視DBの保存先 |
| `SESSIONGAUGE_POLL_INTERVAL_SECONDS` | 更新間隔（2〜60秒） |
| `SESSIONGAUGE_TIME_ZONE` | 表示・集計タイムゾーン |
| `SESSIONGAUGE_SIDEBAR_ENABLED` | サイドバー報告の有効/無効 |
| `SESSIONGAUGE_SIDEBAR_TTL_MS` | サイドバーtokenのTTL |

サイドバーは既定で無効です。`[sidebar] enabled = true`、環境変数、または `live --sidebar` で明示的に有効化した場合だけ、namespaced sourceと `$sessiongauge_usage` tokenを使用します。表示行はherdr側の設定に利用者が追加します（`config.example.toml` 参照）。

監視データは既定で `~/.local/share/sessiongauge/monitor.db` に保存されます。独自監視DBは所有者だけがアクセスできる権限で作成されます。

## 手動リンクと履歴

herdr integrationでSession IDが取得できないPaneは、対象が正しいと確認したうえで明示的にリンクできます。

```sh
uv run sessiongauge link <pane-id> <session-id> [--profile <profile>]
uv run sessiongauge history --days 7 --group-by task
uv run sessiongauge history --days 7 --group-by session
uv run sessiongauge show <task-id>
```

曖昧なprofileや存在しないPane/Sessionはリンクされません。同一Sessionを複数DBから識別できないときはprofileを明示してください。履歴には監視開始時のbaselineも出ますが、これは導入前の利用量を示すものではありません。`unlinked` はPaneとSessionの対応が未確定、`unattributed` はSessionの観測差分を特定Taskに安全に割り当てられない状態です。どちらも推測で配賦しません。

liveには累計、観測差分、直近60秒に観測された差分を表示します。Hermes DBの更新間隔とポーリング周期に依存するため、生成中のリアルタイム計測や完全な時間按分ではありません。観測開始後の履歴はminimum observedです。

## CSV / JSONエクスポート

```sh
uv run sessiongauge export --days 7 --format csv --output usage.csv
uv run sessiongauge export --days 7 --format json --output usage.json
```

出力ファイルがすでにある場合、既定では上書きしません。確認のうえ上書きする場合だけ `--force` を付けます。出力にはタイトル、Pane ID、プロジェクト情報が含まれ得るため、共有前に内容を確認してください。

## オプトインのherdrサイドバー

既定ではサイドバーへ報告しません。設定の `[sidebar] enabled = true` または `live --sidebar` を明示した場合に限って、herdrの対応状況を検出して専用の `sessiongauge_usage` tokenを送ります。未対応の場合は警告を出し、通常の監視は継続します。終了時・リンク状態の変化時はSessionGaugeが所有するtokenだけをclearし、TTLでも失効します。

表示側はherdrの設定へ利用者が明示的に追加してください。ドキュメント例は `config.example.toml` にあります。SessionGaugeは設定を自動変更せず、実機でのサイドバー描画も保証しません。

herdrのmetadata送信は、成功時に標準出力が空でも終了コード `0` なら成功として扱います。headless環境で実行確認する場合は、既定の監視DBを使わず、別のデータディレクトリを指定できます（JSON出力は破棄します）。

### アカウントquota表示（任意）

`agent-resume` を別途インストールし、Hermesで利用中のCodexアカウントを設定したうえで、`[sidebar.quota]` の `enabled = true` を指定します。設定項目は `agent_resume_bin`（既定 `agent-resume`）、`hermes_home`（既定 `~/.hermes`、`~` 展開）、`poll_interval_seconds`（30〜3600秒、既定60）です。CLIは `agent-resume usage --agent hermes --hermes-home <home>` を引数配列で起動し、timeoutは35秒です。shell起動はせず、stdout/stderrやエラー本文を表示しません。

quotaはアカウント全体の値です。Hermes homeの `state.db` に対応するprofile UUIDに正確に照合できたpaneだけに ` · 5h 57%` のように追加します。他profileには表示しません。CLIが未導入・失敗・応答不正の場合は対象paneで ` · 5h N/A` とし、通常のトークン表示を継続します。ポーリング値はメモリ内に限り、エクスポートや監視DBへ保存しません。

`5h` はCodexのアカウントprimary Session利用枠であり、Hermes会話のセッショントークン数ではありません。helperが公開する `primary` limit windowは `limit_window_seconds = 18000`（5時間）です。契約は [Hermes slash commands](https://hermes-agent.nousresearch.com/docs/reference/slash-commands) と [`agent-resume` の usage出力仕様](https://github.com/iorinu/agent-resume#usage) を参照してください。

```sh
uv run --no-sync python -m sessiongauge --data-dir ~/.local/share/sessiongauge/sidebar live --json --sidebar >/dev/null
```

## 更新・アンインストール

uv toolとしてインストールした場合:

```sh
uv tool upgrade sessiongauge
uv tool uninstall sessiongauge
```

上記はSessionGaugeの実行ファイルだけを対象にします。監視データやHermes DBは消しません。データをどう扱うかは利用者自身が決めてください。削除コマンドは提供していません。
