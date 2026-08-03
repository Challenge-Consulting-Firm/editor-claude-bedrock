# 管理者向け コスト・監査チェックコマンド集

Bedrock（エディタ用 Claude）のコストと国内完結統制を、運用者が随時確認するためのコマンド集。
すべて**読み取り専用**（末尾「タグ有効化」のみ書き込み）。週次レポート（[lambda/report_usage.py](../lambda/report_usage.py)）
の自動投稿とは別に、手元で内訳を掘りたいときに使う。

- **対象アカウント**: `269154581652`（検証用）。`aws sts get-caller-identity` でログイン先を確認してから実行する
- **推論リージョン**: 東京 `ap-northeast-1`（+大阪 `ap-northeast-3` へ jp. ルーティング）
- **課金の確認は us-east-1 前提**: Cost Explorer / Billing はグローバルサービス。コンソールは
  画面右上のリージョンを **バージニア北部 (us-east-1)** にして開く（CLI は影響なし）

> ⚠️ **サービス名の罠（重要）**: Cost Explorer 上、Bedrock の推論は **`Amazon Bedrock` では計上されない**。
> `Claude Opus 4.8 (Amazon Bedrock Edition)` のように**モデルごとの別サービス名**で出る。
> したがって `--filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock"]}}'` は**常に $0 を返す（誤り）**。
> サービス名で絞るなら「(Amazon Bedrock Edition)」を含むものを選ぶ。利用者別に見るなら**タグで集計する**（下記）。

---

## 0. 前提：コスト配分タグが有効か

タグ集計は、そのタグが**コスト配分タグとして Active** になっていないと機能しない。
**有効化前のデータには遡及しない**点に注意（有効化した日以降の課金から集計対象）。

```bash
# user / app / Project / Phase が Active か
aws ce list-cost-allocation-tags --status Active \
  --query 'CostAllocationTags[?TagKey==`user` || TagKey==`app` || TagKey==`Project` || TagKey==`Phase`].[TagKey,Status,LastUpdatedDate]' \
  --output table
```

想定される Active 状態（2026-08 時点）: `Project`(2026-07-15) / `Phase` / `user`(2026-08-02) / `app`(2026-08-03)。

---

## 1. 利用者別コスト（本命）

`user` タグでグループ化する。**サービス名フィルタは付けない**（付けると罠に嵌まる）。

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-08-01,End=2026-08-08 \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=TAG,Key=user \
  --query 'ResultsByTime[].{Date:TimePeriod.Start,By:Groups[].[Keys[0],Metrics.UnblendedCost.Amount]}' \
  --output json
```

- `user$`（値が空）に全額が寄っている場合 = そのデータ期間に `user` タグが**まだ効いていない**
  （有効化日より前、または課金反映前）。数日待って再確認する
- `Start`/`End` は用途に合わせて変更する（End は**排他** = 当日を含めたいなら翌日を指定）

---

## 2. アプリ用途別（Claude Code とその他を分離）

`app=claude-code` で絞って利用者別に見る。手順書・週次集計はこのフィルタ前提。

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-08-01,End=2026-08-08 \
  --granularity MONTHLY --metrics UnblendedCost \
  --filter '{"Tags":{"Key":"app","Values":["claude-code"]}}' \
  --group-by Type=TAG,Key=user \
  --output json
```

---

## 3. プロジェクト全体の実コスト（週次レポートと同じ集計）

[lambda/report_usage.py](../lambda/report_usage.py) の「実コスト」欄はこの `Project` タグフィルタで算出している。
手元で同じ値を再現・照合したいときに使う。

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-08-01,End=2026-08-08 \
  --granularity MONTHLY --metrics UnblendedCost \
  --filter '{"Tags":{"Key":"Project","Values":["editor-claude-bedrock"]}}' \
  --query 'ResultsByTime[].Total.UnblendedCost.Amount' --output json
```

---

## 4. モデル別コスト（想定外モデルの検知）

サービス名 = モデル名なので、SERVICE ディメンションで金額のある行を並べるとモデル別内訳になる。
**PoC 想定は Opus 4.8 / Sonnet 4.6 / Haiku 4.5 の 3 本**。それ以外（Sonnet 5・Opus 5 等）が出たら、
別プロジェクト由来か、統制外の利用がないかを切り分ける。

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-08-01,End=2026-08-08 \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].{Date:TimePeriod.Start,Svc:Groups[?Metrics.UnblendedCost.Amount!=`0`].[Keys[0],Metrics.UnblendedCost.Amount]}' \
  --output json
```

---

## 5. 推論プロファイルのタグ付け確認

利用者別配賦が効くのは、呼び出すアプリ推論プロファイルに `user`/`app`/`model` タグが付いているから
（[docs/setup-claude-code.md](setup-claude-code.md) §0.5）。新メンバー追加後などに検証する。

```bash
# claude-code 用の per-user プロファイル一覧
aws bedrock list-inference-profiles --type-equals APPLICATION --region ap-northeast-1 \
  --query 'inferenceProfileSummaries[?starts_with(inferenceProfileName, `cc-`)].{Name:inferenceProfileName,Arn:inferenceProfileArn}' \
  --output table

# 個別プロファイルのタグ（ARN は上の一覧から）
aws bedrock list-tags-for-resource --region ap-northeast-1 \
  --resource-arn "arn:aws:bedrock:ap-northeast-1:269154581652:application-inference-profile/<ID>" \
  --query 'tags' --output table
```

各 per-user プロファイルに `user=<氏名>` / `app=claude-code` / `model=opus|haiku` が揃っていること。

---

## 6. 国内完結の事後監査（residency）

推論が国内（東京+大阪）から出ていないかを CloudTrail で確認する（[scripts/04-check-cloudtrail.sh](../scripts/04-check-cloudtrail.sh) と同趣旨）。

```bash
# 直近の InvokeModel の実処理リージョン（ap-northeast-1/3 以外が出たら統制破れ）
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=InvokeModel \
  --max-results 20 --region ap-northeast-1 \
  --query 'Events[].{Time:EventTime,User:Username}' --output table
```

`inferenceRegion` の中身まで見たいときは `scripts/04-check-cloudtrail.sh` を使う（CloudTrail は最大数分の記録遅延あり）。

---

## 7. 予算の消化状況

```bash
aws budgets describe-budgets --account-id 269154581652 \
  --query 'Budgets[].{Name:BudgetName,Limit:BudgetLimit.Amount,Actual:CalculatedSpend.ActualSpend.Amount,Forecast:CalculatedSpend.ForecastedSpend.Amount}' \
  --output table
```

---

## 付録：コスト配分タグの有効化（書き込み・遡及なし）

集計に必要なタグが Active でない場合に有効化する。**新しいタグは、そのタグ付き課金が一度発生してからでないと
認識されず、有効化以前の期間には遡及しない**。早めに有効化しておくほど取りこぼしが減る。

```bash
# 例: app タグを有効化
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=app,Status=Active

# 複数まとめて
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=user,Status=Active TagKey=app,Status=Active
```

成功時のレスポンスは `{"Errors": []}`。反映（集計に現れる）まで最大 24h 程度かかる。
