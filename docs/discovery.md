# 実環境調査（Phase 0・初期記録）

この文書は実装前後の初期調査で観測した環境情報です。現行仕様や最新検証結果を網羅するものではありません。現行の[セットアップ](setup.md)、[アーキテクチャ](architecture.md)、[検証記録](verification.md)も参照してください。

調査対象はmacOSのローカル環境です。個人のSession ID、タスク名、プロジェクトパス、会話本文、認証情報は記載しません。

## 初期調査で確認したこと

- Hermes Agent v0.20.0 (2026.8.3)、Hermes Python 3.11.15。
- herdr CLI 0.9.2、server 0.9.0、protocol 22。endpoint compatible=yes、restart_needed=no。再起動していません。
- Hermes integration current v5。install・設定変更はしていません。
- `HERMES_HOME` は既定homeでした。探索で実在する `state.db` は1件で、名前付きprofileのDBは見つかりませんでした。
- `mode=ro` でsessions schemaを確認しました。`schema_version` は25、`PRAGMA user_version` は0、journal modeはWALでした。
- 調査時に存在した安全な列はid、source、model、parent_session_id、started_at、ended_at、input/output/cache/reasoning tokens、cwd、git_repo_root、estimated/actual cost、title、last_activity_at、profile_nameでした。message本文等は取得対象にしません。
- herdr pane listはJSONを既定で返し、各Paneの `agent_session` にHermes Session IDが含まれる形式でした。profile/DB identityは報告されないため、複数DBに同じIDがあれば曖昧です。
- 初期観測では21 panes中3件にSession IDがあり、3件ともDBのIDと完全一致しました。この数は当時の観測値です。
- `pane report-metadata` のhelp/schemaにsource、token、sequence、TTL、成功応答の仕様を確認しました。このPhase 0調査では実機metadata書き込みを行っていません。
- herdr設定の行は利用者が管理し、専用tokenを参照できます。SessionGaugeは設定を自動変更しません。

## 当時のDB更新調査

インストール済みHermesソースを調べ、API応答後にtoken差分がqueueされ、background writerのcommit後にDBから読めることを確認しました。生成途中の逐次tokenはDBから計測できません。初期仕様ではTotalをInput+Outputとし、Cache/Reasoningを加算しません。

## 当時参照した資料

- Hermes session storageの公開資料（当時参照したURL: https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/）。公開schemaは実環境のschemaと異なるため、列検出が必要と判断しました。
- herdr CLI reference（当時参照したURL: https://herdr.dev/docs/cli-reference/）とconfig reference（当時参照したURL: https://herdr.dev/docs/config-reference/）。稼働中CLIと同梱schemaを優先し、公開資料の設定仕様が稼働versionと同じとは仮定しませんでした。

この初期調査ではサイドバーの実画面描画、metadata送信/clear、herdr再起動、integration更新を行っていません。実環境DBはHermes自身が更新するため、ファイル全体のhash不変を保証しません。現在の実環境確認は[検証記録](verification.md)を参照してください。
