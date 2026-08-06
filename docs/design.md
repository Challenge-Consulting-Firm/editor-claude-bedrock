# 設計書: エディタ用 Claude on AWS Bedrock — 国内完結 PoC

原本の要件は Azure 版 editor-openai-foundry（社内・非公開） の
指示書・設計書（R1: キーローテ / R2: IP allowlist / R3: コスト上限、エディタから api-key 利用）を引き継ぐ。
本書は **Azure で実測断念した「推論の国内完結」を Bedrock で成立させられるか**を検証する PoC の設計。

## 1. 目的・スコープ

- **一次目的（Azure 版から不変）**: 社内のコード・ログを外部 LLM SaaS へ送らず、データの所在・処理範囲を
  コントロール下に置く。Azure 版は実測の結果 **APAC 処理（DataZone）止まり**だった
- **本 PoC の目的**: Bedrock の **日本国内クロスリージョン推論プロファイル（`jp.`）× Claude Opus 4.8** で
  「**推論も国内（東京+大阪）完結**」を、エディタ利用（OpenAI 互換 + API キー）と両立できるか実測する
- **スコープ外（本番化で実施）**: 週次キーローテの自動化・Teams 通知、Budget ハードストップ、監査の常設化（§7）

## 2. 全体構成（PoC）

> ⚠️ **実測（2026-07-14）により下図の「エディタ → OpenAI 互換直結」は不成立と確定**
> （`/openai/v1` は gpt-oss 専用カタログで Claude 非対応。mantle も Claude の chat/completions 非対応）。
> ネイティブ Converse + Bearer キー + jp. は成立しているため、本番構成はエディタと Bedrock の間に
> **自社プロキシ（LiteLLM 等の OpenAI 互換 ⇄ Converse 変換）**を挟む形、または Claude Code CLI
> （ネイティブ Bedrock 対応）が候補。詳細は [poc-checklist.md](poc-checklist.md) の代替案表と README「実測で分かった制約」。

```
                              ┌──────────────── AWS アカウント（検証用） ───────────────┐
  Zed / VS Code               │                                                          │
  ── Bearer (Bedrock API キー) ─▶  bedrock-runtime.ap-northeast-1.amazonaws.com          │
      /openai/v1/chat/completions   │                                                    │
      model = jp.anthropic.claude-  │   jp. 推論プロファイル（SYSTEM_DEFINED）            │
      opus-4-8-...                  │   ├─▶ ap-northeast-1 (東京)  Claude Opus 4.8       │
                              │     └─▶ ap-northeast-3 (大阪)  Claude Opus 4.8           │
                              │           ※ 推論はこの 2 リージョンに閉じる（+10%）       │
                              │                                                          │
  IAM ユーザー editor-claude-poc                                                          │
   └ ポリシー: jp.* プロファイル以外の推論を実質不許可（§4）                                 │
   └ Bedrock API キー（長期・期限付き）= IAM service-specific credential                    │
                              │                                                          │
  CloudTrail (Event history)  │  InvokeModel/Converse が管理イベントとして記録され、       │
                              │  additionalEventData.inferenceRegion で実処理先を監査      │
  AWS Budgets                 │  月次 50/75/90% 実績 + 100% 予測 → メール通知              │
                              └──────────────────────────────────────────────────────────┘
```

Azure 版との対応:

| 要件 | Azure 版の実装 | Bedrock PoC の実装 |
|---|---|---|
| エディタから api-key | Foundry の api-key + `openai/v1` | **Bedrock API キー（Bearer）+ OpenAI 互換 `openai/v1`** |
| R1 キーローテ（週次） | Functions timer + Key Vault + Teams | ✅ **実装・実測済み**: EventBridge Scheduler（月曜 09:00 JST）+ Lambda + SSM SecureString + Teams 投稿（[infra/rotation.tf](../infra/rotation.tf) / [lambda/rotate_key.py](../lambda/rotate_key.py)） |
| R2 IP allowlist | Foundry の networkAcls | IAM ポリシー `aws:SourceIp` 条件（`ALLOWED_IPS` 設定時） |
| R3 コスト上限 | Budget ソフト/ハード + TPM | Budgets ソフト通知（PoC）。ハードストップは §7 |
| residency 統制 | deployment 名の規約のみ（統制不可） | **IAM で強制 + CloudTrail で事後監査**（Azure よりも強い） |

## 3. モデル・データ所在

| 区分 | 範囲 | 決まり方 |
|---|---|---|
| 保管（at rest） | 呼び出し元リージョン = 東京 | エンドポイントのリージョン |
| 推論（inference） | **東京 + 大阪**（国外へ出ない） | `jp.` クロスリージョン推論プロファイル |

- モデル: **Claude Opus 4.8**（Bedrock 提供開始 2026-05-28、東京リージョン提供・`jp.` プロファイル対応の報告あり）
- プロファイル ID は**実測（`scripts/01`）で確定**。`.env` の `JP_PROFILE_ID` が全スクリプト・エディタ設定の単一の参照点
- 単価: $6 / $30（per 1M、入力/出力・AWS 料金表 2026-07 実測）+ `jp.` プレミアム 10% → **実効 $6.6 / $33.0**。（当初 $5/$25 は誤りだった）
  Prompt Caching（読み取り 0.1 倍）併用でエージェント用途の実効コストを下げる
- Bedrock は既定で入力プロンプトをモデル学習に使用しない（Anthropic にもログを共有しない）

### なぜ Anthropic 直契約ではないか

Anthropic API の `inference_geo` は global / us のみで**日本に限定できない**。
国内完結を要件とする限り、現時点で Bedrock `jp.` プロファイルが唯一の経路。

## 4. 迂回防止（IAM 設計）— Azure 版に対する最大の改善点

Azure 版は「`-apac` という deployment 名で利用者に認識させる」**運用規約**しか手段がなかった。
Bedrock では IAM で**技術的に強制**する（[infra/main.tf](../infra/main.tf)）:

1. **Allow は 2 系統のみ**
   - `arn:...:inference-profile/jp.*` への推論呼び出し
   - 東京/大阪の `foundation-model/*` への呼び出し。ただし
     **条件 `bedrock:InferenceProfileArn` が `jp.*` のときだけ**（= プロファイル内部のルーティング用）
   - → `global.` / `apac.` プロファイルも、モデル ARN 直叩きも、暗黙 Deny で不許可
2. **明示 Deny**: 東京以外のリージョンエンドポイントへの推論呼び出し（`aws:RequestedRegion`）
   → us-east-1 等の bedrock-runtime へ回り込む迂回を封じる
3. **任意**: `aws:SourceIp` による IP allowlist（`ALLOWED_IPS` 設定時。Azure 版 R2 相当）

検証: `scripts/03` のネガティブテスト（`jp.` 以外のプロファイル指定 → 403 AccessDenied を期待）。

## 5. 認証・キー運用

- 認証: **Bedrock API キーのみ**（エディタに SigV4 署名や IAM トークン自動更新の機構が無いため。Azure 版と同じ判断）
- キーの実体は IAM **service-specific credential**（service: `bedrock.amazonaws.com`）。
  `Authorization: Bearer <キー>` で OpenAI 互換/ネイティブ両エンドポイントに使える
- **長期キー vs 短期キー**:
  - 短期キー（12h）が AWS の本番推奨だが、発行に SigV4 認証が必要 = エディタ利用者への配布運用に乗らない
  - 長期キーは AWS 的に「検証用」の位置づけ。**PoC はこれで良い**。
    本番採否は「**有効期限 7 日の長期キー + 週次自動ローテ（R1 相当）**」で AWS の推奨とリスク許容を折り合わせる設計とし、
    その妥当性を本番化判断の論点として残す（§7）
- キーはユーザー単位（PoC は `editor-claude-poc` 1 ユーザー）。本番で利用者別に分けるか共通キーにするかは
  Azure 版（共通キー + KQL でモデル別追跡）との整合も含め本番化で判断

## 6. 監査・コスト

- **residency 監査**: CloudTrail（Event history 90 日・追加設定不要）で `InvokeModel` / `Converse` の
  `additionalEventData.inferenceRegion` を確認（`scripts/04`）。`ap-northeast-1/3` 以外が出たら統制破れ
- **利用量・コスト**: PoC は Budgets のソフト通知（50/75/90% 実績 + 100% 予測）+ **タグ配賦**。
  - 全リソースに共通タグ `Project=editor-claude-bedrock` / `Phase=poc` / `ManagedBy=terraform`（provider の default_tags）
  - **推論コストの配賦はタグ付きアプリケーション推論プロファイル経由**（[infra/inference-profiles.tf](../infra/inference-profiles.tf)。
    Bedrock のオンデマンド課金はリソース非依存のため、リソースタグだけでは配賦できない — これが AWS の公式解）。
    Opus 4.8 / Sonnet 4.6 / Haiku 4.5 の 3 本を配備済み。エディタ/CLI は ARN を model に指定する（実測済み）
  - ⚠️ 制約: **Zed の組み込みモデル（エージェント用 Sonnet 4.6）はシステム jp. プロファイル直なのでタグ配賦されない**
    （Cost Explorer では「Bedrock 全体 −（タグ付き合計）」として把握）。Claude Code は ARN 指定でフル配賦可能
  - 初回のみ: 課金データにタグが現れた後（利用開始から最大 24h）、コスト配分タグを有効化する。
    プロジェクト全体は `Project` / `Phase`、**利用者別内訳には `user` / `app` も**有効化する（遡及しない＝有効化日以降の課金のみ集計対象）:
    `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=Phase,Status=Active TagKey=user,Status=Active TagKey=app,Status=Active`
  - ⚠️ Cost Explorer 上、Bedrock 推論は `Amazon Bedrock` ではなく `Claude Opus 4.8 (Amazon Bedrock Edition)` 等の
    **モデル別サービス名**で計上される。SERVICE=`Amazon Bedrock` で絞ると $0 になるため、利用者別はタグで集計する
  - 管理者が随時コスト・監査を確認するコマンド集は [cost-admin-checks.md](cost-admin-checks.md) に集約
  - Azure 版 KQL 相当の「ユーザー別集計」は本番化で Model invocation logging（CloudWatch Logs/S3）を追加して実装
- **週次利用状況レポート（実装）**: EventBridge Scheduler（月曜 09:30 JST）→ Lambda → Teams。
  CloudWatch Metrics（`AWS/Bedrock`）でモデル別トークン消費量＋概算費用、Cost Explorer でタグ配賦の実コスト
  （週次 + 月次累計・月次予算対比）を集計して投稿（[infra/usage_report.tf](../infra/usage_report.tf) /
  [lambda/report_usage.py](../lambda/report_usage.py)）。トークン系は全呼出（Zed 組み込みモデル含む）を捕捉するが、
  実コストはタグ配賦分のみ（Zed 組み込みモデルは抜ける = 既知の差）。⚠️ 実コスト取得にはコスト配分タグの有効化が前提

## 7. 本番化 TODO（PoC 通過後）

PoC の 3 点が通ったら、Azure 版で作った防御一式をここへ移植する:

- [x] **R1**: 週次キーローテ自動化 — **実装・実測済み（2026-07-14）**。EventBridge Scheduler（月曜 09:00 JST）
      → Lambda（最古キー削除 → 新キー発行・期限 15 日 → SSM SecureString 保管 → Teams 投稿）。
      Azure 版と同じく旧キーは 1 世代（1 週間）温存、Teams 投稿失敗は関数ごと失敗させて検知可能にする。
      **Teams 投稿は利用者ポータル（profile_ui）URL の案内のみ**で、キー本文・モデル ARN は載せない
      （本人が認証後にポータルで閲覧・コピーする）。
      ⚠️ 運用注意: webhook URL は**署名（sig）付きの完全な URL** を使うこと（不完全だと 401。実測で踏んだ）
- [ ] **R3 ハード**: Budget 100% 実績 → Lambda でキー無効化（`update-service-specific-credential --status Inactive`）。
      Azure 版 `disableLocalAuth` 相当。復旧 runbook も移植
- [ ] Model invocation logging 常設化 + 月次レポート（KQL → CloudWatch Logs Insights / Athena）
- [ ] CloudTrail の常設トレイル（90 日を超える監査保管が要る場合）
- [ ] Service Quotas（TPM/RPM）の確認と必要なら引き下げ申請 — Azure 版の「capacity で封じ込め」相当。
      mantle 系 quota は既定が大きい（入力 20M TPM 報告）ため、コスト封じ込めは Budget ハード側を主とする
- [ ] IaC の本番リポジトリ整備（state のリモート化、環境分離）
- [ ] 2 本立て運用（Azure=APAC 許容の一般用途 / Bedrock=国内完結+Opus 4.8）か全面移行かの判断資料作成
