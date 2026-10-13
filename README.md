# SessionGauge

Hermes Agent のセッション使用量をローカルで確認する、読み取り専用の監視・履歴ツールです。herdr が正確な Session ID を報告した場合、または利用者が明示的にリンクした場合だけタスクへ結び付けます。対応付けできないデータは推測で帰属させず、unlinked / unattributed として扱います。

## セットアップ

Python 3.11 以上と [uv](https://docs.astral.sh/uv/) が必要です。

```sh
uv sync --dev
uv run sessiongauge doctor
```

Hermes と herdr は別途インストールしてください。`doctor` は利用可能なCLI、DB、Paneの状態を診断します。初回起動時の監視DBは `~/.local/share/sessiongauge/monitor.db` に作られます。

最初は `doctor` と `live --once` で読み取り状態を確かめてから、必要に応じて継続監視や手動リンクを使ってください。会話内容、Session ID、認証情報を共有する必要はありません。

開発環境を作らずにインストールする場合は、リポジトリのルートで `uv tool install .` を実行できます。このcheckoutから更新する場合は、同じ場所で `uv tool install --force .` を実行してください。`uv tool upgrade` は公開済みパッケージの更新向けです。インストールだけを解除する場合は `uv tool uninstall sessiongauge` を使います。

## コマンド

```sh
# 対話型ライブ画面（TTYで実行）
uv run sessiongauge live

# 1回だけ取得（テキスト/JSON）
uv run sessiongauge live --once
uv run sessiongauge live --once --json

# 直近7日をタスク別に集計
uv run sessiongauge history --days 7 --group-by task

# task / session / model / day / project 別の集計
uv run sessiongauge history --days 7 --group-by model

# CSV / JSONへ出力。既存ファイルの上書きには --force が必要
uv run sessiongauge export --days 7 --format csv --output usage.csv
uv run sessiongauge export --days 7 --format json --output usage.json

# PaneとSessionを明示的にリンクし、タスクの詳細を表示
uv run sessiongauge link <pane-id> <session-id>
uv run sessiongauge show <task-id>
```

集計期間に含まれる観測差分と、ツール導入時点の累計baselineは別物です。baselineは過去の期間使用量ではなく、実費が提供されていない場合は `N/A` です。詳細は[セットアップ・運用](docs/setup.md)、[既知の制限](docs/known-limitations.md)を参照してください。

ライブ画面では幅に収めるため大きな値を `123.46M~` / `$123.46k~` のような概数で表示します（`~` は丸めた値）。詳細画面では元の値を確認できます。`N/A` は値が取得できていない状態で、`0` とは異なります。

## 設定

設定ファイルは `~/.config/sessiongauge/config.toml` です。サンプルは [`config.example.toml`](config.example.toml) を参照してください。優先順位は CLI > 環境変数 > TOML > 既定値です。既定のポーリング間隔は3秒、設定可能範囲は2〜60秒です。

データベースは `HERMES_HOME/state.db`（未設定時は `~/.hermes/state.db`）と検出可能な名前付きprofileを読みます。個別の複数DBを指定するには `--hermes-db PATH` を繰り返すか、`SESSIONGAUGE_HERMES_DB_PATHS` を設定してください。

## プライバシーと安全性

- HermesのSQLite DBは `mode=ro` で接続します。Hermes側のmigration/importを行いません。
- 使用量の更新はHermes側でDBへの書き込みがcommitされた後のポーリングで反映されます。
- 会話本文、メッセージ、system prompt、ツール引数、APIキーは取得・保存・送信しません。ネットワーク送信やテレメトリもありません。
- 独自の監視DBのみを書き込み、所有者限定のディレクトリ/ファイル権限で保護します。
- cwd、タイトル、時刻、直近の利用状況だけからリンクを推測しません。
- herdrサイドバーへの報告は明示的なopt-inです。設定を自動変更しません。
- 任意のアカウントquota表示も既定で無効です。有効時は既存の `agent-resume` CLIだけを呼び出します。認証ストアを直接読み取らず、helperの生JSONはメモリ内で必要な利用率だけを解析し、ログや保存先には出力しません。`account_key` は参照しません。

アカウントquotaの詳細と設定例は[セットアップ・運用](docs/setup.md)を参照してください。表示される `5h` はCodexアカウントのprimary Session quotaで、SessionGaugeのセッショントークン数ではありません。

ライブJSON出力は各pollでflushするため、`live --json` をパイプした場合も行単位で受け取れます。Pane名が汎用的なHermes端末名で、Session IDが確定照合できた場合はHermes Sessionのタイトルを表示名に使います。タイトルはリンク判定には使いません。

## 実装方針

Python標準ライブラリだけで動作し、ランタイム依存はありません。ライブ画面は標準の `curses` を使います。軽量な配布とローカル完結を優先し、別途TUIライブラリを導入しない構成です。ビルドバックエンドは hatchling、開発環境の同期・ビルドにはuvを使います。

確認済み環境・検証結果・未確認事項は[検証記録](docs/verification.md)、データの流れは[アーキテクチャ](docs/architecture.md)を参照してください。
