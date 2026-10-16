# ADR 0001: token増分によるquota割合の推定

- 日付: 2026-10-15
- ステータス: superseded（2026-10-16、ADR 0002）。当時の決定は歴史記録。

## 概要

当初のtoken比例推定案は、API値とローカルtokenの対応だけでは正確性を検証できないためIssue #2の対応として停止した。`estimate` は常に `N/A` とする。`api` はprovider raw task budget参照を `API` ラベル付きで表示するだけで、現在windowのquota率とは主張しない。ローカルtoken数は測定値、trusted quotaは利用不可、API値はraw referenceであり、現時点で検証済みの推定方法はない。

## 背景

現状、ローカル収集とquota表示は別経路であり、SID別quotaはAPI応答を表示する。APIが成功しても `unavailable` になる場合や、`available` でも照会時刻より古い `data_as_of` に対して同じ値が返る実測があった。根本原因をAPI制限や集計遅延と断定はしない。

ローカルの従来表示用 `total_tokens` は `input_tokens + output_tokens` として維持する。現行SDKのcanonical bucketはuncached input、cache read、cache write、outputを別々に扱うため、cacheを一律に追加合算すれば二重計上になるとは宣言できない。現在のquota推定はADR 0002に従い4 bucketを使う。この記述は現行実装についてのclarificationであり、当時の決定の書き換えではない。収集開始前の累計値はbaselineとして取得できるが、過去の期間別使用量や時刻ごとの増分は復元できない。

## 決定

1. 有効な校正sampleを得ても `TokenQuotaEstimator` は受理せず、比例計算を行わない。
2. `estimate` は常に `N/A`。ローカルtoken数を表示する `percent_tokens` は利用できる。
3. `api` の数値は `API 12%`、併記は `API 12% · 1K` と表示し、raw provider referenceと識別する。無効値はN/A、100超をclampしない。
4. アカウント全体の割合を各SIDへコピーしない。

## 推定の考え方

比例式は採用しない。API responseは `data_as_of` しか示さず、同一account/SID/windowで完了したcounter reference、denominator、local counterとのtimestamp alignmentを規定しない。

単純比例はaccounting contractのない近似であり、採用しない。期間が確認されていないため表示を「現在の5時間枠の残量」と呼ばない。

**停止理由:** window境界・分母・input/output/cache/reasoning weighting・counter alignmentが不明で、session-specific completed cutoffもない。近似タイミングの受入は行わない。

## 代替案

- API割合のみ表示を維持: 単純だがAPIの鮮度・可用性に依存する。
- pollを短縮: 表示更新機会は増えるが、上流データ更新を促す保証はない。
- token数のみ表示: ローカル値を継続表示できるが、割合の目安を示せない。
- account割合を各SIDへ表示: 値は得られても個別セッションの指標ではなく、誤解を招く。

## 影響と未決事項

- `estimate_max_age_seconds`（既定86400秒）は互換性・設定検証のため残すが、sampleを校正に使わない。
- 既存のscope/lifecycle機構は互換性のため保持するが、quota精度を与えない。
- `[sidebar.quota] mode` の既定は互換性のため `estimate` のままだが、値は常にN/A。`api` は raw provider referenceを表示する。quota自体は既定無効。

## Issue #2への対応と検証の限界

- 推定が全sampleに対してfail-closedである。
- 妥当なsampleもすべて拒否し、estimate表示をN/Aにする。
- APIの数値をraw referenceとして `API` ラベルで表示する。
- 従来の読み取り専用性とprivacy境界を維持する。
- fixture試験でデータ処理を検証する。fixture試験は実API応答の鮮度・意味を保証せず、実API検証やherdr実画面の表示確認とは別に扱う。

正確なaccuracy contractと、同一account/SID/期間の独立検証可能な参照値は未取得（BLOCKED）。[公開Codex task_usage.rs](https://github.com/openai/codex/blob/8f0d2459ac753e22cf0cf09999c3feac7b80b589/codex-rs/backend-client/src/client/task_usage.rs) は `data_as_of` とpercentageを扱うが、window start/end、denominator、timestamp-to-local-counter alignment、input/output/cache weighting、session-specific completed cutoffを提供しない。このsourceから分かるのは公開responseのフィールドと取り扱いまでで、上流の意味・鮮度・window契約は保証しない。以前の `P0*T/T0` は古いprovider時刻のpercentageに後のtoken countを組み合わせ、baselineを過小評価する可能性があった。履歴時刻を揃えるだけでもwindowやweightsは証明できない。実API/live monitorはこの検証で実行していないため、検証済み推定は現時点で存在しない。

## 根拠

- [アーキテクチャ](../architecture.md): 通常収集とopt-in quota経路、プライバシー境界、token集計。
- [既知の制限](../known-limitations.md): quota値の意味、欠損、永続化方針。
- [README](../../README.md): sidebar/quotaの設定と既定表示。
- [domain.py](../../src/sessiongauge/domain.py): `total_tokens` の定義。
- [quota.py](../../src/sessiongauge/quota.py)、[hermes_usage_bridge.py](../../src/sessiongauge/hermes_usage_bridge.py): 現行API応答のSID照合と割合取得。
- [sidebar.py](../../src/sessiongauge/sidebar.py): 現行のtoken・quota表示経路。
- [OpenAI Codex公開クライアント](https://github.com/openai/codex/blob/8f0d2459ac753e22cf0cf09999c3feac7b80b589/codex-rs/backend-client/src/client/task_usage.rs)（確認日: 2026-10-15）: `data_status` と `data_as_of` を扱うが、上流での欠損理由や集計期間を保証する根拠にはならない。
