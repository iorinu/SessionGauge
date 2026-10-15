# 既知の制限

SessionGaugeが示すのはローカルで観測できた値です。欠損期間や曖昧な対応関係を推測で埋めません。

## 履歴・使用量

- **baselineと期間集計**: 初回に読んだ累計値はbaselineです。導入前の日別・期間別使用量は復元できません。
- **観測時点**: 使用量はHermes DBへの書き込み後のpollで更新されます。生成中の逐次token計測ではありません。`recent_60s` は直近60秒内に終了した観測区間の差分で、実時間の速度や発生時刻を示しません。
- **期間境界**: 指定期間やローカル午前0時をまたぐ区間を按分せず除外する場合があります。history/exportはlower boundになり得ます。
- **Taskとprofile**: 正確なherdr Session ID、保存済み関係、明示リンクがない限りTaskに割り当てません。profile間ID衝突、複数Pane競合、session再開、Pane移動、圧縮・分岐では誤帰属を避け、`unlinked` / `unattributed` とする場合があります。
- **modelとcost**: セッション累計のmodelを区間のbest-known値として使うため、途中でmodelが変わった場合に正確なmodel別配分はできません。actual costが提供されない場合は `N/A` で、請求額との一致を保証しません。

## quota表示

- `api` modeはAPIが返す `five_hour_limit_percent` のbudget換算割合を表示します。「現在の5時間windowだけ」の使用率と確認されたものではなく、token数やアカウント全体の残量でもありません。100%超もそのまま表示します。
- `estimate` modeではAPI割合と取得時点のローカルトークンを校正に使った近似を表示します。時点差や集計期間未確認による誤差を許容し、推定は `~` で示します。現在windowの割合やtoken上限を保証しません。校正不能・失効時は `N/A` です。
- native APIは一度に最大100件のroot threadを照会します。対象が101件以上なら全対象が `N/A` になります。子threadの利用量は合算しません。
- 対象はquota設定の `hermes_home` に対応するprofileの、一意に照合できたSIDです。他profile、quota無効時にはquotaを取得しません。割合のみ表示が既定で、旧token数との併記は `percent_tokens` を明示した場合だけです。
- 照会間隔は既定60秒、設定範囲30〜3600秒です。推定校正は既定86400秒で失効し、プロセス内だけに保持します。再起動後は再校正が必要です。`estimate` modeのAPI照会は背景処理で行うため、初回は応答を待たず `N/A` となり得ます。
- quotaは外部APIへのopt-in照会で、monitor DBやexportには保存しません。認証・送信データの範囲は[アーキテクチャ](architecture.md)を参照してください。

## sidebarと動作環境

- autostartはmacOSとherdr 0.9.2の範囲です。Python/runtimeのinstalled pathが変わる場合はLaunchAgentを再生成してください。pluginのlink/enableだけでは既存serverでstartupが発火しないため、`autostart start` が必要です。
- autostartは `HERMES_HOME` とconfigが読む非secretな `SESSIONGAUGE_*` 環境設定、およびinstall時に明示したDB path・poll間隔・timezoneをLaunchAgentへ保存します。これらの設定を変えた場合は `autostart install` を再実行してください。
- 連続TUIの排他leaseは画面が戻った時点ではなく、collector workerがcloseを終えて終了するまで保持します。
- 旧版の手動monitorはOS leaseを持ちません。移行時はそのSessionGauge monitorだけを正常停止し、Hermes/herdr本体は停止しないでください。
- autostartの実API通信、実herdr起動、実launchd反映はこの変更の自動テスト範囲外です。

- metadata送信とherdrの実画面表示は別の確認事項です。metadataが届くことは、pixel上の描画やlayoutが正しい証拠にはなりません。
- profile構成、Linux、未確認version、30 Pane同時稼働の実機負荷は網羅していません。
- 実機Hermes DBはHermes自身が更新し得るため、ファイル全体のhash不変は保証できません。読み取り専用性はfixture DBで検証します。

したがって、history/exportを請求額の確定や完全なTask別使用量として扱わず、baseline、mapping status、集計注記も確認してください。
