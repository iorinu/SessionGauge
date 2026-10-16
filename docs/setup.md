# セットアップと運用

## 起動

Python 3.11以上とuvを用意し、リポジトリのルートで実行します。

```sh
uv sync --dev
uv run sessiongauge doctor
uv run sessiongauge live
```

`uv sync --dev` は開発依存を含む環境を作ります。環境を同期せずcheckoutから起動する場合は `uv run --no-sync ...` を使えます。Hermesとherdrは別途必要です。`doctor` はCLI、DB、Paneの状態を診断し、インストールやintegration更新はしません。

パッケージとしてinstallせずに使う場合もcheckoutのルートで実行できます。checkoutからuv toolへinstallするには `uv tool install .`、更新には同じcheckoutから `uv tool install --force .` を使います。`uv tool upgrade` は公開済みパッケージの更新向けです。

継続監視はTTY上の `uv run sessiongauge live`、単発表示は `uv run sessiongauge live --once`、JSONは `uv run sessiongauge live --once --json` です。通常のポーリング間隔は3秒（設定範囲2〜60秒）。

## データと設定

監視DBは既定で `~/.local/share/sessiongauge/monitor.db` に作成されます。Hermes DBは通常 `~/.hermes/state.db` と名前付きprofileを読みます。`HERMES_HOME` または `--hermes-db PATH`（複数指定可）で場所を指定できます。設定ファイルは `~/.config/sessiongauge/config.toml` で、必要な場合だけ作成してください。優先順位はCLI、環境変数、TOML、既定値です。

live画面は累計、観測差分、直近60秒に終了した観測区間の差分を表示します。導入時点ですでに存在する使用量はbaselineとして別扱いで、過去の期間使用量には含めません。履歴は観測できた区間の集計です。

## herdr autostart（macOS / herdr 0.9.2）

安定したinstalled PythonとSessionGauge runtimeを使ってください。LaunchAgentはそのPythonを絶対pathで呼ぶため、開発用checkoutを使う場合も移動・削除しない場所に保持します。設定はopt-inのままで、既存設定を変更しません。

```sh
uv run sessiongauge --config ~/.config/sessiongauge/config.toml autostart install
```

installはLaunchAgent plistとherdr plugin manifestを生成・登録し、JSONで `plugin_dir` と `plugin_id` を返します。herdrへのplugin linkは出力を確認して明示的に行います。

LaunchAgentには `load_config` が読む `HERMES_HOME` と非secretな `SESSIONGAUGE_*` 設定環境変数（DB path、data directory、poll間隔、timezone、sidebar表示設定）だけを引き継ぎます。credentialや任意の環境変数は保存しません。install時に `--hermes-db`、`--poll-interval-seconds`、`--time-zone` を明示した値はLaunchAgentの起動引数に保存され、未指定値は起動時にconfig fileから読みます。これらの値や環境設定、config fileの場所を変更した場合は `autostart install` を再実行してください。start/stop/statusは登録済み設定を書き換えません。

```sh
herdr plugin link /absolute/path/printed/as/plugin_dir
```

既に起動中のserverにはstartup hookが遡及発火しないため、link/enable後は一度だけ反映します。

```sh
uv run sessiongauge autostart start
uv run sessiongauge autostart status
```

停止・解除は明示して行います。

```sh
herdr plugin disable <plugin-id>
herdr plugin unlink <plugin-id>
uv run sessiongauge autostart stop
uv run sessiongauge autostart uninstall
```

pluginのstartupはHerdr SERVERの起動・handoff時に実行され、client attach/detachごとには実行されません。起動時は短いone-shot start actionからLaunchAgentへ渡り、monitor自身はherdr serverの一時不在時にもleaseを保持してbackoff/reconnectします。旧版の手動monitorはこのleaseに未対応なので、初回移行前にそのSessionGauge monitorだけ正常停止してください。Hermes/herdr本体は停止しません。

連続TUIもcollector workerが終了してcollectorをcloseするまで同じscopeのleaseを保持します。画面終了後もworkerが処理中なら、同scopeのmonitor起動はworker終了まで拒否されます。

履歴、詳細、出力の例:

```sh
uv run sessiongauge history --days 7 --group-by task
uv run sessiongauge history --days 7 --group-by session
uv run sessiongauge show <task-id>
uv run sessiongauge export --days 7 --format csv --output usage.csv
uv run sessiongauge export --days 7 --format json --output usage.json
```

exportの既存ファイルは既定で保護されます。明示的に上書きするときだけ `--force` を付けてください。出力にはタイトル、Pane ID、プロジェクト情報が含まれる場合があります。

## herdrサイドバー（任意）

サイドバー報告は既定で無効です。有効化には `[sidebar] enabled = true` が必要です。quotaも表示する場合は `[sidebar.quota] enabled = true` も必要です。設定を変えたら、既存のSessionGauge monitorを停止してから再起動してください。同じPaneへ書き込むmonitorを並行稼働させないでください。表示行はherdr側の設定に利用者が追加します。

設定例です。`agent_resume_bin` は実際のagent-resume実行ファイルの場所へ置き換えます。

```toml
[sidebar]
enabled = true
display = "percent"
ttl_ms = 15000

[sidebar.quota]
enabled = true
agent_resume_bin = "/path/to/agent-resume/target/release/agent-resume"
hermes_home = "~/.hermes"
hermes_root = "~/.hermes/hermes-agent"
poll_interval_seconds = 60
mode = "estimate"
estimate_max_age_seconds = 86400
# 既定配置から検出できないときだけ指定する
# hermes_python = "/path/to/hermes-agent/venv/bin/python"
# auth_helper = "/path/to/agent-resume/adapters/hermes_bridge.py"
```

`display = "percent_tokens"` にすると表示値とローカルtoken数を併記します。`ttl_ms` の既定値は15000です。`estimate` は5時間枠の条件付き使用量範囲です。表示例 `~2.6–9.1%` は合成例で、実利用者の値ではありません。正確な割合・残量・アカウント全体のshareではなく、経験的信頼区間や誤差保証でもありません。参照やmodel整合性が不十分ならN/Aです。`api` はprovider raw task budgetを `API 12%` と表示します。`estimate_max_age_seconds` の既定は86400秒で、基準時刻 `data_as_of` からの鮮度を制限します。重複照会で鮮度は延長されません。poll間隔は既定60秒、設定範囲30〜3600秒です。SDK Pythonは `hermes_root/venv/bin/python`、認証helperはagent-resume配置から既定検出します。検出できない場合だけ任意のパスを指定してください。

herdr設定の `[ui.sidebar.agents.rows_by_agent]` では、既定の先頭行を保ち、専用tokenを別行に追加できます。agent名はherdrが提供するため、metadataに重ねて含めない例です。

```toml
[ui.sidebar.agents.rows_by_agent]
hermes = [["state_icon", "machine", "workspace", "tab"], ["agent", "$sessiongauge_usage"]]
```

表示側の設定方法はherdrの設定に従ってください。SessionGaugeはその設定を自動変更しません。

### foregroundでの動作確認

設定を反映するために、既存monitorを停止してから次の1組だけを起動します。専用のdata directoryを使い、JSON出力を破棄します。

```sh
uv run --no-sync sessiongauge --data-dir ~/.local/share/sessiongauge/sidebar live --json --sidebar >/dev/null
```

Ctrl-CでSessionGaugeを終了します。終了時にSessionGauge自身のsidebar tokenをclearします。Hermesやherdr serverは停止しません。バックグラウンドmonitorを使う場合もproducerは1つだけにしてください。

古いmonitorが残った可能性がある場合、管理ツールのstatus/listやherdr側の表示だけではworker終了を確認できません。macOSでは `pgrep -fl sessiongauge` または `ps -o pid,ppid,command -p <PID>` で実際のPID・親PID・commandを確認します。launcherの `uv` とPythonプロセスは同じmonitorの構成要素の場合があります。古いPythonの実際のcommand lineを特定し、そのmonitorのPIDへだけSIGINTを送り、古いmonitorが終了したことと新しいmonitorが継続していることを確認してください。広範囲なpkillやプロセスグループへのkill、Hermes agentの停止は行いません。

同一tokenへの古いmonitorと新しいmonitorの同時書き込みは、サイドバー表示がちらつく原因になります。古いmonitorだけを正常終了させてから新しい1組で確認します。metadataの送受信確認は画面上のpixel表示確認とは異なり、実際の描画や行レイアウトを保証しません。

## quota照会とプライバシー

quota機能はSIDごとにCodex nativeの `five_hour_limit_percent` を取得します。`api` modeはprovider raw task budget参照を表示します。`estimate` modeでは追加で同じ許可済みhostへaccount GETを行い、primary windowのreset時刻と5時間長を使います。POST本文は対象thread ID、開始時刻、空のdescendant一覧です。current SIDに加え、同一profileの過去root session最大3件を参照候補として照会する場合があります。送るのはSID・開始時刻・空のdescendant一覧だけで、token数、model、会話本文、タイトル、cwdは含めません。認証情報はAuthorizationヘッダーに使われ、利用可能な場合はChatGPT-Account-Idもヘッダーに含まれます。credential storeをSessionGaugeが直接解析・保存せず、raw responseやcredentialをログ・永続化しません。

このopt-in機能は外部ネットワークを使います。通常のローカル収集はネットワーク送信をしません。API response・window context・推定値はmonitor DBやexportへ保存しません。履歴DBは読み取り専用で参照します。詳細な制約は[既知の制限](known-limitations.md)を参照してください。

ブリッジには `.env` / `.ssh` へのファイルアクセスを拒否する監査ガードがあります。これは補助的なプロセス内ガードであり、OS sandboxではありません。Hermes SDKが相対パスでファイルを開く構成では照会が失敗し、quotaは `N/A` になります。

## 手動リンク

herdrからSession IDを取得できないPaneは、対象が正しいと確認したうえで明示的にリンクします。

```sh
uv run sessiongauge link <pane-id> <session-id> [--profile <profile>]
```

profile間で同じSession IDがあり曖昧な場合はprofileを指定します。正確なID、既存リンク、利用者の明示操作に基づかないTask割当は行いません。
