# ADR 0001: token増分によるquota割合の推定

- 日付: 2026-10-15
- ステータス: 採用（初期実装）

## 概要

APIから返る割合を毎回そのまま表示する方式から、同じSIDのAPI割合とリクエスト取得時点のローカルトークンを暫定的に対応づけて校正し、その後はローカルのtoken増分で割合を推定・更新する方式へ移行する。時点差や集計期間未確認による誤差を許容する。API照会は校正・再校正に使い、表示更新をブロックしない。API取得を恒久的に禁止する決定ではない。

## 背景

現状、ローカル収集とquota表示は別経路であり、SID別quotaはAPI応答を表示する。APIが成功しても `unavailable` になる場合や、`available` でも照会時刻より古い `data_as_of` に対して同じ値が返る実測があった。根本原因をAPI制限や集計遅延と断定はしない。

ローカルの `total_tokens` は `input_tokens + output_tokens`。cache/reasoningを追加合算すると二重計上になる。収集開始前の累計値はbaselineとして取得できるが、過去の期間別使用量や時刻ごとの増分は復元できない。

## 決定

1. 自動校正を優先し、API割合と `data_as_of` に取得時点のローカルtoken累計を対応づける。時点差による誤差は許容し、厳密一致は停止条件にしない。
2. 同一profile・SID・modelの期限内校正から、API未更新・失敗時もローカルtoken増分で推定する。
3. 推定値は `~12.3%`、token併記は `~12.3% · 1.45M` と表示する。未校正・失効時は `N/A`。現在のwindowは未確認のため5時間を表示しない。
4. アカウント全体の割合を各SIDへコピーしない。推定は利用傾向の目安であり、請求や正確な制限到達判定には使わない。100%超を契約と異なる形でclampしない。
5. 初期案では同一profile・SID・モデルに校正を限定し、別SID・別モデルへ係数を自動転用しない。モデル変更、tokenカウンタ減少、不明なwindowまたは集計スコープ変更時は推定を無条件に継続しない。

## 推定の考え方

同じSIDのAPI割合と、そのリクエスト取得時点のローカルトークン累計を暫定的に対応づけた校正点を `(T0, P0)` とする。APIとローカル値の時点差や集計期間の未確認による誤差は許容し、API時刻を厳密な集計完了境界とはみなさない。`T0 > 0` かつ `P0` が有限で正の値の場合に限り、単一点の暫定比例係数を求め、同一スコープのローカルtoken増分へ適用して割合を更新する。ゼロ・欠損・無効値は除算せず未校正とする。

この単一点から正確な固定token上限が分かるとは限らない。入力・出力・cache・モデル・推論などで消費特性は異なり、単純比例は近似である。また、期間が確認されていないため表示を「現在の5時間枠の残量」と呼ばない。

**意味上の制約:** `data_as_of` の詳細な集計意味と割合の期間・分母は未確認である。API割合を現在の5時間window残量や正確なtoken上限とは解釈しない。時点差に由来する誤差を許容した近似として表示する。

## 代替案

- API割合のみ表示を維持: 単純だがAPIの鮮度・可用性に依存する。
- pollを短縮: 表示更新機会は増えるが、上流データ更新を促す保証はない。
- token数のみ表示: ローカル値を継続表示できるが、割合の目安を示せない。
- account割合を各SIDへ表示: 値は得られても個別セッションの指標ではなく、誤解を招く。

## 影響と未決事項

- `estimate_max_age_seconds`（既定86400秒）は校正の保持期限と初期サンプルの最大年齢を定める。同じ/古い `data_as_of` は再校正せずTTLも延長しない。新しい時刻の割合減少は補正として受理する。
- 校正はprofile・SID・modelのscopeに限定し、scope変更やgeneration変更で無効化する。校正情報はプロセスメモリだけに保持し、DB/exportへ保存しない。再起動後は未校正から始める。
- `[sidebar.quota] mode` は `estimate` が既定、`api` は従来の実測表示。quota自体は既定無効。
- 初期校正がSIDごとに必要となり、profile・SID・model変更やカウンタ減少時に表示が未校正へ戻る場合がある。

## 受入条件（実装時）

- 有効な対応校正点とtoken増分に推定値が追従し、短期のAPI未更新・失敗時も有効な係数の条件内で更新する。
- `data_as_of` の欠損・不正・future・`estimate_max_age_seconds` を超える古さのサンプルを拒否する。欠損、ゼロ、無効数値も校正として受理しない。
- profile・SID・model変更で係数を誤転用せず、カウンタ減少時は推定を無条件に続けない。
- 表示で推定値とAPI実測値が区別でき、未校正が `N/A` / 未校正と分かる。
- 従来の読み取り専用性とprivacy境界を維持する。
- fixture試験でデータ処理を検証する。fixture試験は実API応答の鮮度・意味を保証せず、実API検証やherdr実画面の表示確認とは別に扱う。

## 根拠

- [アーキテクチャ](../architecture.md): 通常収集とopt-in quota経路、プライバシー境界、token集計。
- [既知の制限](../known-limitations.md): quota値の意味、欠損、永続化方針。
- [README](../../README.md): sidebar/quotaの設定と既定表示。
- [domain.py](../../src/sessiongauge/domain.py): `total_tokens` の定義。
- [quota.py](../../src/sessiongauge/quota.py)、[hermes_usage_bridge.py](../../src/sessiongauge/hermes_usage_bridge.py): 現行API応答のSID照合と割合取得。
- [sidebar.py](../../src/sessiongauge/sidebar.py): 現行のtoken・quota表示経路。
- [OpenAI Codex公開クライアント](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/task_usage.rs)（確認日: 2026-10-15）: `data_status` と `data_as_of` を扱うが、上流での欠損理由や集計期間を保証する根拠にはならない。
