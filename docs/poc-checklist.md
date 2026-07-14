# PoC チェックリスト・実測記録（正）

進め方は Azure 版と同じ: **実測 → 判明した制約を記録 → 構成確定**。
「公式・記事に書いてある」は判定に使わない。本ページの実測記録だけを正とする。

記録ルール: 実施日・実施者・コマンド/手順・生の結果（エラーは全文）・判定を書く。生ログは `logs/` に置き、ここへ要点を転記。

---

## 検証 1: `jp.` プロファイルに Opus 4.8 が実在するか

- 手順: `./scripts/01-list-jp-profiles.sh`
- 期待: `jp.anthropic.claude-opus-4-8-*` が `ACTIVE` で存在し、`inference_to` が `ap-northeast-1` / `ap-northeast-3` のみ
- 通ったら: 出力の ID を `.env` の `JP_PROFILE_ID` に確定。`NON_JP_PROFILE_ID` も実在 ID に設定
- **落ちたら**: 国内完結 × Opus 4.8 不成立。`jp.` 対応の他モデル（Sonnet 系等）での代替可否を記録し、
  「モデル妥協 or 国内完結妥協（Azure 継続）」の判断材料にする

| 項目 | 記録 |
|---|---|
| 実施日 / 実施者 | 2026-07-14 / 運用者（+Claude Code） |
| 実測されたプロファイル ID | `jp.anthropic.claude-opus-4-8`（ACTIVE） |
| 推論先リージョン | ap-northeast-1（東京）+ ap-northeast-3（大阪）のみ |
| 判定 | ✅ **OK** |
| 備考（提供表との乖離など） | 仮置き ID `jp.anthropic.claude-opus-4-8-20260528-v1:0` は**誤り**（日付サフィックスなしが正）。jp. 対応は他に Opus 4.7 / Sonnet 4.6 / Sonnet 4.5 / Haiku 4.5 / Nova 2 Lite。**Claude 5 系（fable-5 / sonnet-5）は東京に提供済みだが jp. 未対応**（Azure と同じ「最新モデルは地域限定が遅れる」構図。要ウォッチ）。モデルアクセスは 3 モデルとも AUTHORIZED 済みでコンソール作業不要だった |

## 検証 2: OpenAI 互換 + Bearer キーで `jp.` プロファイル推論が国内完結するか（本丸）

- 手順: `./scripts/10-issue-api-key.sh` → `.env` にキー設定 → `./scripts/03-invoke-openai-compat.sh`
  → 15 分待って `./scripts/04-check-cloudtrail.sh`
- 期待:
  - (a) `POST /openai/v1/chat/completions` + `Authorization: Bearer` + `model: jp....` が **200**
  - (b) ネガティブテスト（`jp.` 以外のプロファイル）が **403**（IAM 迂回防止の実証）
  - (c) CloudTrail の `inferenceRegion` が **`ap-northeast-1` または `ap-northeast-3` のみ**
- 切り分け: (a) が落ちたら `./scripts/02-invoke-converse.sh`。02 OK で 03 NG → OpenAI 互換レイヤ固有の問題
- **落ちたら（(a) 404 等）**: 「国内完結」と「エディタ互換」の両立不可が確定。
  代替案を検証: ① LiteLLM 等の自前プロキシ（SigV4 変換）を挟む ② エディタのネイティブ Bedrock 対応
  （Zed は不可・Continue は可）に限定する — を記録して比較

| 項目 | 記録 |
|---|---|
| 実施日 / 実施者 | 2026-07-14 / 運用者（+Claude Code） |
| (a) 正常系 HTTP status / 応答 | ❌ **404 model_not_found（全経路）**。東京/大阪の `/openai/v1`、東京 mantle（chat/completions・responses）、東京 `/v1`（パス自体未展開）を総当たり。**IAM 要因を排除するため管理者 SigV4 でも再実測 → 同じく 404**。一方 `openai.gpt-oss-120b` は解決される（validation_error）= **`/openai/v1` は gpt-oss 系専用カタログで、Claude はプロファイル形式・素の ID とも非対応** |
| (b) ネガティブテスト結果 | ✅ `global.` プロファイル・素のモデル ID とも **access_denied（IAM 拒否）**。OpenAI 互換レイヤ越しでも jp. 限定統制が機能（※HTTP は 403 でなく **401** で返る） |
| (c) inferenceRegion 実測値 | ✅ ネイティブ Converse（jp. Opus 4.8）で **`ap-northeast-1`** を確認。拒否した呼び出しも監査証跡に残る |
| OpenAI 互換呼び出しの CloudTrail eventName（実測） | 拒否イベントは `Converse` として記録（modelId 欄なし）。成功時の eventName は成立経路がないため未確認 |
| 判定 | ⚠️ **(a) NG / (b)(c) OK** — 「国内完結×API キー×Opus 4.8」はネイティブ API（Converse）で完全成立。**エディタ直結用の OpenAI 互換だけが AWS 側に存在しない** |
| 備考 | 切り分けの経緯: ①東京 runtime で jp. のみ 404・global. は IAM まで到達 → マッピング差異と誤読しかけた ②大阪で jp. が IAM 拒否まで到達 → 大阪はマッピング有りと再誤読 ③IAM 許可後に大阪も 404 → **評価順序が「IAM が先・カタログ照会が後」**と判明 ④管理者 SigV4 で権限要因を排除して確定。**mantle は独自カタログ制（プロファイル不可）+ project 単位 IAM のため国内完結統制の穴になる → 許可を撤去済み**。この一連は「公式・記事と実環境の乖離」の Bedrock 版（クラスメソッド記事の「mantle で使える見込み」は Claude には当てはまらなかった） |

### 検証 2 の帰結: エディタ接続の代替案（検証 3 の前提が変わった）

直結（エディタ → AWS の OpenAI 互換）が不成立のため、検証 3 は以下のいずれかの経路で行う:

| 案 | 経路 | 特徴 |
|---|---|---|
| **①プロキシ（本命）** | エディタ →(OpenAI 互換)→ **LiteLLM 等の自社プロキシ** →(Converse+jp.)→ Bedrock | エディタ体験・API キー配布運用を維持。プロキシは国内（ローカル/社内サーバ/東京 ECS）に置く。Azure 版との 2 本立てでも利用者体験を統一できる |
| ②ネイティブ対応エディタ | Continue の `provider: bedrock`（SigV4） | プロキシ不要だが Zed 不可・API キー運用に乗らない（SigV4 認証が必要） |
| ③Claude Code CLI | `CLAUDE_CODE_USE_BEDROCK=1` + Bedrock API キー | コーディングエージェント用途なら最有力の代替。ネイティブ Bedrock 対応で jp. プロファイル指定可・Bearer キーで動く（要実測） |

## 検証 3: Zed / VS Code から実際に動くか

- 手順: [setup-zed.md](setup-zed.md) / [setup-vscode.md](setup-vscode.md)。各エディタで
  ①簡単なチャット ②ツール使用を伴うエージェントタスク（ファイル編集）を 1 回ずつ
- 期待: 双方で応答が返り、Zed 側でエラー表示（400 系の互換性問題）が出ないこと
- Azure 版の教訓: パラメータ互換の罠（`max_tokens` vs `max_completion_tokens`）が Bedrock/Claude で
  どうなるかを必ず記録（Claude は `max_tokens` を受けるはずだが実測で確定）

| 項目 | 記録 |
|---|---|
| 実施日 / 実施者 | 2026-07-14 / 運用者（+Claude Code） |
| **案③ Claude Code CLI** | ✅ **OK（エンドツーエンド実測済み）** — Bearer キー + `jp.anthropic.claude-opus-4-8` でチャット応答・**ツール使用（ファイル生成エージェントタスク）**とも成功。設定手順は [setup-claude-code.md](setup-claude-code.md) |
| Zed: チャット / エージェント | ✅ **OK（ネイティブ Bedrock プロバイダ + API キー認証、Zed 1.10.3・2026-07-14 実測）**。チャット=カスタム jp. Opus 4.8 / エージェント=組み込み Sonnet 4.6（jp. 自動付与）。**プロキシ不要だった**（当初の「Zed 直結不可」は旧情報で誤り）。制約と設定手順は [setup-zed.md](setup-zed.md) — ①settings の region が効かず既定 us-east-1（`launchctl setenv ZED_AWS_REGION` で解決）②カスタムモデルはツール一律無効 ③組み込み jp 対応表に Opus 系が漏れている（upstream 修正候補） |
| VS Code（方式: Copilot BYOK / Continue） | ⬜ 未実施（利用者需要が出たら Continue の SigV4 直結を実測） |
| 必要だった設定上の工夫（互換性の罠） | Claude Code は **InvokeModelWithResponseStream** を使う。「model is not available」表示の実体は ①**Anthropic use case フォーム未提出**（404）②Opus 系のみ追加で **Marketplace 契約未完了**（403）だった。**Converse は use case 未提出でも通る**（Haiku/Sonnet で実証）が InvokeModel 系は拒否する — AWS 側の執行不整合のため、**疎通確認を Converse でやると誤判定する**。契約作成後は約 2 分の伝播待ちが必要。診断は `ANTHROPIC_LOG=debug` |
| 判定 | ✅ **OK（Claude Code 経路）** — Zed/VS Code はプロキシ検証（別途）に切出し |

**解除済みの管理者作業（2026-07-14 実施・アカウント初回のみ）**:
1. Anthropic use case フォームを CLI で提出（`aws bedrock put-use-case-for-model-access`。
   `intendedUsers` は数値コード文字列 — `"0"`=Internal。誤ると "Invalid form data"）
2. Opus 4.8 の契約作成: `list-foundation-model-agreement-offers` で offerToken 取得 →
   `create-foundation-model-agreement` → agreement が PENDING→AVAILABLE（約 70 秒）→ さらに約 2 分で invoke 可能に。
   **Haiku/Sonnet は契約不要**（use case フォームのみで開通）

## コスト可視化（2026-07-14 追加実装）

- ✅ 全リソース共通タグ（default_tags: `Project` / `Phase` / `ManagedBy`）
- ✅ タグ付き**アプリケーション推論プロファイル ×3**（Opus 4.8 / Sonnet 4.6 / Haiku 4.5、jp. の複製）を配備し、
  ARN 経由の推論を実測（curl InvokeModel / Claude Code とも OK）。オンデマンド推論コストのタグ配賦は
  この方式が唯一の経路（リソースタグでは配賦不可）
- 実測で踏んだ罠: `CreateInferenceProfile` の **description は ASCII のみ**（日本語で ValidationException）
- 既知の限界: Zed 組み込みモデル（エージェント用 Sonnet 4.6）はタグ配賦不可（システムプロファイル直）
- ⬜ **残タスク（初回のみ・24h 後）**: 課金データにタグが載った後にコスト配分タグを有効化
  `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=Phase,Status=Active`
  （現状は "Tag keys not found" で拒否される — Billing にタグ未着のため）
- ⬜ Zed カスタム Opus 4.8 を ARN 指定に変更済み → 次回 Zed 利用時にチャット 1 回で動作確認

## 付帯確認（判定には含めないが記録する）

- [ ] キー発行の実測: `create-service-specific-credential` の `--credential-age-days` が期待どおり効くか（期限切れ後 401 になるか）
- [ ] レイテンシ体感（東京 vs Azure japaneast との比較メモ）
- [ ] Prompt Caching が OpenAI 互換経由で効くか（`usage` の cache 系フィールド）
- [ ] 大阪（ap-northeast-3）へのルーティングが実際に起きるか（inferenceRegion の分布）

---

## 総合判定

| 判定 | 条件 | 次のアクション |
|---|---|---|
| ⬜ **GO** | 1〜3 すべて OK | 本番化 TODO（design.md §7）着手。2 本立て or 全面移行の判断資料作成 |
| ⬜ **条件付き GO** | 1・2 OK / 3 が一部 NG | 対応エディタを限定 or プロキシ検討で再評価 |
| ⬜ **NO GO** | 1 or 2 が NG | Azure 継続（APAC 許容）。乖離内容を記録して四半期後に再評価 |
