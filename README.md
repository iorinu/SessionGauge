# SessionGauge

Hermes Agentのセッション使用量をローカルで確認し、履歴・集計・エクスポートする読み取り専用ツールです。herdrが報告する正確なSession ID、または利用者が明示したリンクだけをTaskに結び付けます。確かな対応がない使用量は `unlinked` / `unattributed` として残し、推測で配賦しません。

## クイックスタート

Python 3.11以上と[uv](https://docs.astral.sh/uv/)が必要です。

```sh
uv sync --dev
uv run sessiongauge doctor
uv run sessiongauge live
```

`live` はTTYで対話画面を開きます。一度だけ確認する場合は `uv run sessiongauge live --once` を使います。サイドバーとセッション別quotaは既定で無効です。監視DBは `~/.local/share/sessiongauge/monitor.db` に保存されます。

## 主なコマンド

```sh
uv run sessiongauge history --days 7 --group-by task
uv run sessiongauge history --days 7 --group-by model
uv run sessiongauge export --days 7 --format csv --output usage.csv
uv run sessiongauge export --days 7 --format json --output usage.json
uv run sessiongauge link <pane-id> <session-id>
uv run sessiongauge show <task-id>
```

macOSでherdr 0.9.2のserver起動に合わせてmonitorを管理する場合は、[autostart手順](docs/setup.md#herdr-autostartmacos--herdr-092)を参照してください。既定では有効になりません。

exportは既存ファイルを上書きしません。確認のうえ上書きする場合は `--force` を指定します。監視開始時の累計baselineは期間内の使用量ではありません。`N/A` は未取得を示し、ゼロとは異なります。

## サイドバーとプライバシー

herdrサイドバー報告とセッション別quota表示は、設定で個別に明示した場合だけ有効になります。`estimate` modeは精度契約を検証できないため停止しており、`N/A` を表示します。`mode = "api"` はproviderのraw task budget参照値を `API 12%` と表示しますが、現在のquota windowの正確さを示しません。quota有効時はHermes SDKの通常認証経路でOpenAIの許可済みHTTPS endpointへ照会します。通常の収集はローカルで行います。通信の詳細は[アーキテクチャ](docs/architecture.md)を参照してください。`display = "percent_tokens"` にするとローカルtoken数も併記します。設定方法は[セットアップ](docs/setup.md)を参照してください。

HermesのSQLite DBは読み取り専用で開き、会話本文は取得しません。サイドバー設定は自動変更しません。

## ドキュメント

- [セットアップと運用](docs/setup.md): 設定、サイドバー、運用上の注意
- [アーキテクチャ](docs/architecture.md): データの流れとプライバシー境界
- [既知の制限](docs/known-limitations.md): 集計・quota・検証範囲の制約
- [検証記録](docs/verification.md): テスト、ビルド、実環境確認
- [要件トレーサビリティ](docs/requirements-traceability.md): 要件と実装・テストの対応
- [実環境調査（Phase 0）](docs/discovery.md): 初期調査時点の観測事実
