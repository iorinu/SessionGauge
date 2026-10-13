# 実環境調査（Phase 0）

調査対象は macOS のローカル環境。個人のSession ID・タスク名・プロジェクトパス・会話本文・認証情報は掲載しない。

## 確認済み
- Hermes Agent v0.20.0 (2026.8.3)、Hermes自身のPython 3.11.15。
- herdr CLI 0.9.2、稼働サーバー0.9.0、protocol 22。endpoint compatible=yes、restart_needed=no。再起動していない。
- Hermes integration current v5。install・設定変更は実行していない。
- OpenCode 1.18.30。openai/gpt-6-luna利用可能。
- HERMES_HOMEはデフォルトホーム。探索で実在するstate.dbは1件。名前付きprofileのstate.dbは今回見つからなかった。
- mode=roでPRAGMA table_info(sessions)を確認。schema_versionテーブルのversion=25、PRAGMA user_version=0、journal_mode=wal。
- sessionsの安全な取得対象: id, source, model, parent_session_id, started_at, ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, cwd, git_repo_root, estimated_cost_usd, actual_cost_usd, title, last_activity_at, profile_name。いずれも存在する。system_prompt, model_config, origin_json, messagesなどは取得対象にしない。
- started_at/ended_atはREALのUnix秒（実測の数値範囲も確認）。actual_cost_usdは調査時全セッションNULL。
- herdr pane listはJSONが既定。--json引数は存在しない。envelope: {id, result:{type:"pane_list",panes:[...]}}。
- pane getは {id,result:{type,pane}}。pane_id, terminal_id, revision, agent_status, agent, cwd, foreground_cwd, terminal_title_stripped等がある。agent_sessionは {agent:"hermes",kind:"id",source:"herdr:hermes",value:"<exact session id>"}。profile/db identityは報告されないため複数DBに同じIDがあれば曖昧として扱う。
- 取得時21 Pane、うちHermes integrationのSession IDが報告された3 Pane。3件とも安全なSELECT idでstate.dbとの完全一致を確認。
- terminal_idはPane再利用を見分ける識別子として利用できる。pane keyはendpoint+pane IDとし、terminal世代も別保存する。
- herdr pane report-metadataのhelpで --source, --token NAME=VALUE, --clear-token NAME, --seq, --ttl-ms, --agent, --applies-to-source を確認。タイトルやstate-labelには書き込まない。
- herdr api schema --jsonはrootにschemasを持つ。schemas.request.$defs.PaneReportMetadataParamsにtokens map（最大16）、seq uint64、ttl_ms 1..86400000。schemas.success_response.$defs.ResponseResultには {"type":"ok"} の成功variantがある。実機metadata書込みは未実施。
- herdr --default-configのコメントでも ui.sidebar.agents.rows / rows_by_agent と "$name" の表示トークンを確認。tokensはpresentation guardと独立に適用されるため、他reporterと名前が衝突しない専用トークン名を利用する。

## DB更新タイミング（インストール済みソースで確認）
agent/conversation_loop.py:3709以降とagent/codex_runtime.py:155以降でAPI応答ごとにqueue_token_countsへ差分をenqueue。バックグラウンドwriterのcommit後に読める。生成途中の逐次tokenは測れない。最終化時にもdrainする。inputは正規化時cache分を除く経路があるが、本仕様のTotal=Input+Outputを守りCache/Reasoningを加算しない。

## 公式資料
- https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/ を取得。現行公開schemaは31で実機25とは異なる。列検出で吸収する。Hermes SessionDBをimportするとmigrationが走り得るため本製品は標準sqlite3のmode=roだけを使う。
- https://herdr.dev/docs/cli-reference/ を取得。JSON既定とschemaコマンドを確認。
- https://herdr.dev/docs/config-reference/ を取得。ui.sidebar.agents.rows / rows_by_agent と "$name" metadata tokenを確認。usage token表示例は "$usage"。既存設定を自動変更しない。
- バージョン固定URL https://herdr.dev/docs/0.9.2/config-reference/ は403で取得不能。現在のWeb資料にある表示設定が稼働0.9.0で同じか未確認。インストールCLI/同梱schemaを優先。

## 未検証・制限
- 実機でのsidebar描画、metadata送信/clearは実施していない（明示ON時のみ書込み）。
- 稼働サーバーの再起動・integrationの更新はしない。
- profile衝突/Pane移動/圧縮分岐は安全なfixtureで検証し、実機のセッション操作はしない。
- DB全体はHermes自身が稼働中に変更するため実機ファイルhash不変を保証しない。本製品の読み取り専用性はfixture DBのhash/スキーマ不変とSQLite write拒否で検証する。
