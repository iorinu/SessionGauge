# ADR 0002: 5時間枠の条件付きquota使用量範囲

- 日付: 2026-10-16
- ステータス: 採用。実環境の精度保証・適用結果を意味しない
- supersedes: [ADR 0001](0001-token-based-quota-estimation.md)の「常にN/A」とする実装方針

## 背景

`thread_usage/query_v2` の `five_hour_limit_percent` はSID別の値だが、単独ではローカルcounterとのcutoffや重み付けを定めない。旧式 `P0*T/T0` は、古いprovider時刻の割合に別時点の累計tokenを結び付けるため停止した。別途、primary window contextと、provider時刻を挟む履歴snapshotの両方を使った条件付き範囲なら表示できる。これは上流の精度契約を証明しない。

## 決定

- quotaを明示opt-inした `estimate` modeでは、5時間window内の条件付き使用量範囲を表示する。残量、厳密なpercentage、アカウント全体のshareとは呼ばない。
- 基準参照はcurrent SID自身のAPI値を利用できる。利用できない場合は同一profileの6時間以上前に開始したroot sessionを最大3件選ぶ。照会対象総数は最大100 SID。
- APIからはthread usageの `five_hour_limit_percent` とtop-level `data_as_of` を読む。追加のaccount GETでは `rate_limit.primary_window.reset_at`、`limit_window_seconds=18000` だけを採用する。account IDとplanの組合せはopaque hash scopeにする。
- context、API response、推定範囲はmonitor DB・exportへ保存しない。quota履歴読取はprofile限定のSQLite `mode=ro` / `query_only`。単一workerで非同期照会する。初回pending中はN/Aだが、既存のfresh contextがある場合はpending中もTTL・window・referenceの失効まで継続する。close後は未開始処理をcancelし、実行中HTTP callの即時停止は保証しない。close後に完了したfutureの結果は無視する。bridge subprocessには35秒のdeadlineがある。
- 各fresh HTTP contextは独立し、異なるresponse間でreferenceをmergeしない。accountまたはplanの変更は次のsuccessful pollまで観測できない。refreshを待つ間、前contextの表示が継続し得るのはcontext TTL・window・reference TTLの各有効期間内に限る。`account_scope` は検証用のopaque identityであり、credential変更を即時検出するhookではない。request pollは既定60秒、context TTLは既定最大120秒。
- 必要なreference、鮮度、model整合性、window contextのいずれかが成立しなければN/A。経験的誤差値は未検証のまま `None` とし、0で学習・記録しない。

## 算出

四つのcanonical bucketはuncached input、cache read、cache write、output。reasoning tokenはoutputに含まれる。既存表示用の `total_tokens` は従来どおりinput + outputであり、別契約として維持する。

基準SIDのprovider timestamp `t` に対して、直前・直後の正常snapshotで各bucketを括り、基準値の範囲を `F_lo`〜`F_hi` とする。window開始から現在までのbucket差分範囲は `G_lo`〜`G_hi`。provider参照割合を `P` とすると、各bucketの単位重みを非負・安定と仮定した割合範囲を次で求める。

```text
L = P × min(G_lo[i] / F_hi[i] for i where F_hi[i] > 0)
U = P × max(G_hi[i] / F_lo[i] for i where F_lo[i] > 0)
```

いずれかで `F_lo[i] == 0` かつ `G_hi[i] > 0` なら、そのbucketの重み上限を決められず、上限は正の無限大（表示は `~≥L%`）とする。window内usageが全bucketで0なら範囲は0〜0。値は100%でclipしない。snapshotのbracket幅と各bucketの独立した重み幅を使う範囲であり、実際の誤差の包含を保証しない。

## 必要な仮定と適用条件

- APIの `data_as_of` はthread rowごとの完了counter cutoffを保証するとは確認できない。上流ではtop-level時刻として提供されるため、そこが正確なcutoffであるとの扱いは未検証仮定。
- `P` は `data_as_of` 時点のplan-normalized lifetime referenceとして扱う。providerがこの意味を保証する根拠は得ていない。
- context単位のcache隔離はresponse間のreference混合を防ぐが、account/plan変更はpollまで観測できない。旧contextの短時間表示は上記TTL/window/reference TTLの範囲内で起こり得る。
- account GETのprimary windowが対象SIDの5時間accounting windowと一致するという意味づけも未検証。reset時刻と18000秒長を観測できてもprovider保証ではない。
- 同一modelの4 bucket単位rateは非負かつ安定と仮定する。同一modelでも生成速度、reasoning量、service係数などで変化し得る。
- referenceとtargetはいずれもsource DB上でちょうど1 modelを持つ必要がある。欠落・混在model、観測されたcounter reset/decrease、future/malformed/expired timestamp、bracket欠落、context不整合、参照間矛盾はN/A。snapshot範囲は観測間でcounterが非減少と仮定するが、long poll gap内の未観測resetと回復は端点から検出できない。観測値が非減少でもreset不存在は保証されず、隠れたresetの検出・保証や自動最大gap policyはない。
- 参照鮮度は `data_as_of` 起点で既定86400秒。重複応答で延長しない。取得contextの有効期間は `max(35秒, 2 × poll_interval)`。

範囲はモデル仮定とsnapshot時刻の不確かさを条件付きで伝える表示である。経験的信頼区間でも、認証済みerror boundでもない。実際の誤差は未知である。

## プライバシーと運用

quota opt-in時、同じSDK認証情報・許可済みhostへの既存thread POSTに加えてaccount GETを実施する。履歴参照としてcurrent SIDに加え最大3件の過去root SIDと `created_at` を送る場合がある。空のdescendant一覧も送る。token数、model、本文、title、cwdは送らない。認証情報を新たに保存せず、既存helper経由で使う。native API値・account contextはmonitor DB/exportへ複写しない。

## 検証と限界

実装と現行テストの参照先は[要件トレーサビリティ](../requirements-traceability.md)と[検証記録](../verification.md)に記載する。合成rateに対するfixture算術が一致しても数式実装の確認に限られ、provider意味や実利用精度の証拠ではない。実際の誤差は未知である。検証記録に記載したread-only NEW DRIVER確認は通常bridge経由で2 targetに有限範囲を返したが、metadata・pixel表示確認や精度検証を意味しない。close後のfresh engine再構築での再確認であり、実monitorの再起動ではない。

synthetic ratesを使うE2E期待値はtest fixtureの期待値計算にのみ使用し、productionではrateを使わない。実際のprovider bucket weightsと経験的誤差は不明であり、精度への承認を意味しない。先行するread-only probeとは別に、その後、明示的な適用承認を受けてruntimeに適用し、SID照合済みpaneでのmetadata readbackを確認した。適用内容と確認範囲は[検証記録](../verification.md#その後のruntime適用)を参照。実pixel表示と経験的精度は未確認。

## 主なリスク

- 上流の `data_as_of` とaccount windowの意味が仮定と異なると、算術上の範囲が実windowを表さない。
- 同一model内の実効bucket rateが変動すると、単位rateモデルが成立しない。
- monitor snapshotが疎い場合、bracketが広がるかN/Aが増える。
- この方式は旧推定を精度面で「解決済み」とするものではなく、外部API opt-inのprivacy範囲も過去root SIDとaccount GET分だけ広がる。

## 実装参照

- [`window_quota.py`](../../src/sessiongauge/window_quota.py): bucket範囲式
- [`quota_history.py`](../../src/sessiongauge/quota_history.py): read-only履歴・bracket
- [`window_estimator.py`](../../src/sessiongauge/window_estimator.py): context、参照選択、鮮度、async lifecycle
- [`hermes_usage_bridge.py`](../../src/sessiongauge/hermes_usage_bridge.py) と [`quota.py`](../../src/sessiongauge/quota.py): API/context取得とvalidation
- [`sidebar.py`](../../src/sessiongauge/sidebar.py) と [`cli.py`](../../src/sessiongauge/cli.py): 表示・factory注入
