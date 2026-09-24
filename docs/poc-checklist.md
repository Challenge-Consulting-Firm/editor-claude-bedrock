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
| Zed: チャット / エージェント | ✅ **OK（ネイティブ Bedrock プロバイダ + API キー認証、Zed 1.10.3・2026-07-14 実測）**。チャット=カスタム jp. Opus 4.8 / エージェント=組み込み Sonnet 4.6（jp. 自動付与）。**プロキシ不要だった**（当初の「Zed 直結不可」は旧情報で誤り）。制約と設定手順は [setup-zed.md](setup-zed.md) — ①settings の region が効かず既定 us-east-1（`launchctl setenv ZED_AWS_REGION` で解決）②カスタムモデルはツール一律無効 ③組み込み jp 対応表に Opus 系が漏れている（upstream 修正候補）。**いずれも Zed 固有の制約で Bedrock 側の制限ではない** — `jp.anthropic.claude-opus-4-8` で Claude Code CLI / VS Code 拡張なら「Opus 4.8 × 国内完結 × ツール」が三方とも成立（上記「案③ Claude Code CLI」行で実測）。Zed 側の制約は `crates/bedrock/src/models.rs`（GitHub main・2026-07 取得）で裏付け済み。詳細・経路別マトリクスは [setup-zed.md](setup-zed.md) §1 |
| VS Code（Claude Code 拡張） | ✅ **OK（2026-07-14 実測）**。チャット + エージェントタスク（hello.py 生成・実行確認）完走。CloudTrail 裏取り済み: 認証主体=editor-claude-poc（Bearer キー）/ **modelId=アプリケーション推論プロファイル ARN（= タグ配賦が効く）** / inferenceRegion=ap-northeast-1。⚠️ 罠: この環境の `code` CLI は Cursor へのリンクだった（[setup-vscode.md](setup-vscode.md)）。Copilot BYOK は Bedrock 非対応で不可、Continue は未実測（需要が出たら） |
| 必要だった設定上の工夫（互換性の罠） | Claude Code は **InvokeModelWithResponseStream** を使う。「model is not available」表示の実体は ①**Anthropic use case フォーム未提出**（404）②Opus 系のみ追加で **Marketplace 契約未完了**（403）だった。**Converse は use case 未提出でも通る**（Haiku/Sonnet で実証）が InvokeModel 系は拒否する — AWS 側の執行不整合のため、**疎通確認を Converse でやると誤判定する**。契約作成後は約 2 分の伝播待ちが必要。診断は `ANTHROPIC_LOG=debug` |
| 判定 | ✅ **OK（Claude Code 経路）** — Zed/VS Code はプロキシ検証（別途）に切出し |

**解除済みの管理者作業（2026-07-14 実施・アカウント初回のみ）**:
1. Anthropic use case フォームを CLI で提出（`aws bedrock put-use-case-for-model-access`。
   `intendedUsers` は数値コード文字列 — `"0"`=Internal。誤ると "Invalid form data"）
2. モデル契約の作成（**全 Claude モデルで必要** — 当初「Haiku/Sonnet 不要」と誤認したが、執行が非同期なだけだった。
   Haiku は契約なしで半日通った後に AccessDenied に変わった）: `list-foundation-model-agreement-offers` で
   offerToken 取得 → `create-foundation-model-agreement` → PENDING→AVAILABLE（約 60-70 秒）→ 伝播約 2 分で invoke 可能。
   Opus 4.8 / Haiku 4.5 / Sonnet 4.6 とも契約済み（2026-07-14）

## コスト可視化（2026-07-14 追加実装）

- ✅ 全リソース共通タグ（default_tags: `Project` / `Phase` / `ManagedBy`）
- ✅ タグ付き**共有アプリケーション推論プロファイル ×3**（Opus 4.8 / Sonnet 4.6 / Haiku 4.5、jp. の複製）を配備し、
  ARN 経由の推論を実測（curl InvokeModel / Claude Code とも OK）。オンデマンド推論コストのタグ配賦は
  この方式が唯一の経路（リソースタグでは配賦不可）
- ✅ **Opus 5 global のper-user経路をE2E実測（2026-09-16）**: 一時IAMユーザー + Bearer APIキー +
  `user=e2e.validation` / `app=claude-code` / `model=opus-5` / `residency=global` 付きアプリプロファイルで
  Converse応答 `OK`。同じキーで `global.anthropic.claude-opus-5` 直指定は AccessDenied。
  一時ユーザー・ポリシー・APIキー・プロファイルは検証後すべて削除済み
- ✅ Opus 5の提供状態: `authorizationStatus=AUTHORIZED` / agreement・entitlement・regionすべて `AVAILABLE`（2026-09-16）
- 実測で踏んだ罠: `CreateInferenceProfile` の **description は ASCII のみ**（日本語で ValidationException）
- 既知の限界: Zed 組み込みモデル（エージェント用 Sonnet 4.6）はタグ配賦不可（システムプロファイル直）
- コスト配分タグ: 利用者×モデル棚卸しには `user` / `app` / `model`、国内/global別には `residency` をActiveにする。
  新規タグは最初のタグ付き課金後に認識され、有効化前へ遡及しない
  - ✅ `user` / `app` / `model` は Active（`model` は 2026-09-16 に有効化）
  - ⬜ `residency`: 初回の Opus 5 課金が Billing に到達してから（最大24h）再実行する。
    現時点は `404 tag key missing`。コマンド:
    `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=residency,Status=Active`
    （未有効でも `model=opus-5` で国外分を判別できるため、棚卸し自体は成立する）
- ⬜ Zed カスタム Opus 4.8 を ARN 指定に変更済み → 次回 Zed 利用時にチャット 1 回で動作確認

## デプロイ実績（2026-09-16）

Opus 5（global）追加を本番適用し、以下を実測で確認した。

| 確認項目 | 結果 |
|---|---|
| `terraform apply` | ✅ 2 added / 5 changed / 0 destroyed、エラーなし |
| IAM 管理ポリシー移行 | ✅ 9 statement をアタッチ。旧インラインは152バイトのスタブへ（権限断なし） |
| Lambda 3本のコード更新 | ✅ profile_ui / report_usage / rotate_key とも新 CodeSha256 |
| 国内3モデルの実推論（本番キー） | ✅ opus / sonnet / haiku とも応答、`inferenceRegion=ap-northeast-1` |
| Opus 5 per-user の実推論（本番キー） | ✅ 応答 `OK`、`inferenceRegion=eu-west-1`（global前提どおり） |
| Opus 5 システムプロファイル直指定 | ✅ AccessDeniedException（棚卸し迂回を遮断） |
| ポータルの冪等性 | ✅ 既存3モデルは再作成されず Opus 5 のみ追加 |
| IAM シミュレーション 8 ケース | ✅ 許可5・拒否3とも期待どおり |
| Teams 通知（rotate_key notify_only） | ✅ `notified: true`、キー未ローテーション |
| Teams 通知（週次レポート） | ✅ `posted: true, models: 4`。Opus 5 行を含む |
| CloudTrail 監査 | ✅ 国内/global許可/違反の3分類が正しく判定 |

⚠️ 週次レポートで**月次予算 $200 に対し今月累計 $1,264.86（632%）**と判明。Opus 5 追加とは無関係の
既存の超過だが、予算見直しか使用量抑制の判断が別途必要。

### Opus 5 の全利用者展開（2026-09-16）

`./scripts/11-sync-user-profiles.sh` で 6 名全員に展開し、実測で確認した。

| 確認項目 | 結果 |
|---|---|
| プロファイル作成 | ✅ 5 件新規（takeshi.ohno は既存のため変更なし）= 冪等性を実証 |
| タグ付与 | ✅ 全 6 名が `user` / `app=claude-code` / `model=opus-5` / `residency=global` |
| IAM シミュレーション | ✅ 6 名とも allowed（eu-west-1 ルーティング時） |
| 実推論（本番 Bearer キー） | ✅ 6 名全員の ARN で応答 `OK` |
| 週次レポートの自動捕捉 | ✅ `Opus 5 (global): 動的発見したプロファイル 6 件` = 棚卸しに全員分が載る |
| CloudTrail 監査 | ✅ 6 名分とも「global許可」と分類、国内モデルは ap-northeast-1 のまま |
| Terraform | ✅ `validate` 成功 / `plan` = No changes（per-user プロファイルは管理外のため差分なし） |

### サブエージェントのモデル指定（2026-09-16 実測）

コスト削減のためサブエージェント（Explore 等）を Haiku に固定できるかを実測した。結論: **可能**。
手順は [setup-claude-code.md](setup-claude-code.md) §1.5。

| 確認項目 | 結果 |
|---|---|
| `CLAUDE_CODE_SUBAGENT_MODEL` に per-user Haiku ARN | ✅ 動作。CloudTrail / JSON `modelUsage` ともper-user Haiku ARN、メインはper-user Opus ARN |
| `haiku` + `ANTHROPIC_DEFAULT_HAIKU_MODEL=per-user ARN` | ✅ aliasがper-user ARNへ解決され、利用者別に配賦される（隔離環境で追加実測） |
| `haiku` + Haiku固定なし | ⚠️ `jp.anthropic.claude-haiku-4-5-...`（システムプロファイル直）となり、**userタグが付かず未配賦** |
| `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` | 全サブエージェント等へ強制する場合に必要。fork系はメインモデルを使う例外あり |

**重要**: `haiku` エイリアスの利用可否ではなく、`ANTHROPIC_DEFAULT_HAIKU_MODEL` が
per-user ARNへ固定されているかが棚卸しを分ける。固定なしでもIAMには拒否されず（(a-1) の jp.* 許可）、
未配賦へ静かに流れるため注意。事故を避ける推奨例はサブエージェント変数にもARNを直接指定する形。
コスト差は入力 $5.5→$1.1 / 出力 $27.5→$5.5（約 1/5）。

### 再確認（2026-09-17）

Opus 5 全利用者展開後の定常状態を、デプロイ済みリソースに対して再実測した。

| 確認項目 | 結果 |
|---|---|
| `terraform plan` | ✅ exit=0 / **No changes**（前回の Budget エラーは一過性の DNS 障害で、コード起因ではなかった） |
| Opus 5 プロファイル存在 | ✅ 6 名分とも `ACTIVE` |
| Opus 5 実推論（6 名分の per-user ARN） | ✅ 6/6 応答 `OK`（`in=16 / out=4` tokens） |
| Teams 通知（rotate_key `notify_only`） | ✅ `{"rotated": false, "notified": true}` |
| Teams 通知（週次レポート） | ✅ `{"posted": true, "models": 4}` |
| 週次レポートの Opus 5 捕捉 | ✅ Lambda ログに `Opus 5 (global): 動的発見したプロファイル 6 件` |
| `11-sync-user-profiles.sh --dry-run` | ✅ 6 名とも「変更なし（4 モデル）」= 収束済み・冪等 |
| CloudTrail 監査 | ✅ 6 名分の Opus 5 が `eu-west-1 / global許可`、国内モデルは `ap-northeast-1` のまま。未許可の国外処理なし |

実測中に判明した**運用上の罠を 2 件**修正した（いずれも既存コード・設定側の問題）。

| 事象 | 原因 | 対処 |
|---|---|---|
| `aws` が `The config profile () could not be found` | `.env` の `AWS_PROFILE=`（空）を AWS CLI v2 が「空名のプロファイル」と解釈する。`scripts/*.sh` は `lib.sh` が unset するため無害だが、**手で `. ./.env` してから `aws` を叩く手順**では壊れる | `.env.sample` に注意書きを追記。手動時は `env -u AWS_PROFILE` を併用する |
| `02-invoke-converse.sh` が `null` を表示 | `content[0].text` 固定。Opus 5 等は先頭ブロックが `reasoningContent`（thinking）で `text` を持たない | `text` を持つ最初のブロックを取る jq 式へ修正 |

> ℹ️ このとき `.env` の `AWS_BEARER_TOKEN_BEDROCK` は失効していた（週次ローテーション済み・
> サービス固有認証情報自体は 2 本とも Active）。**インフラ側の障害ではない**ため、
> 上記の実推論は運用者の SigV4 で実施した。利用者は次回ポータルでキーを貼り替えれば復旧する。

## Opus 5.5 の国内完結対応（2026-09-24）

**`jp.anthropic.claude-opus-5-5` が提供された**ため、Opus 5.5 を**国内完結モデル**として追加した。
これまでの「5 系は jp. 未提供 = 国内完結不可」という前提は **Opus 5.5 に限って覆された**
（Opus 5 は引き続き global のみ）。

### 前提の実測

| 確認項目 | 結果 |
|---|---|
| `jp.anthropic.claude-opus-5-5` の存在 | ✅ **ACTIVE**（`JP Anthropic Claude Opus 5.5`、作成 2026-09-22） |
| 推論先リージョン（`models[]`） | ✅ `ap-northeast-1` / `ap-northeast-3` のみ = **国内に閉じる** |
| 基盤モデル `anthropic.claude-opus-5-5` | `INFERENCE_PROFILE` のみ（モデル直叩き不可）、`ACTIVE` / `AUTHORIZED` |
| jp. システムプロファイル直呼び | ✅ Converse 成功（応答 `OK`）。Zed 組み込み等の経路も利用可 |
| jp. 由来の per-user アプリ推論プロファイル | ✅ 作成でき、Converse も成功（検証用は削除済み） |

この結果から、Opus 5.5 は global 扱いではなく `user_profile_models_jp`（国内モデル群）へ
追加するのが正しい設計と判断した。`allow_global_models` の影響を受けない（= 同フラグを
`false` にしても Opus 5.5 は使える）。

### デプロイ実績

| 確認項目 | 結果 |
|---|---|
| `terraform apply` | ✅ 0 added / **4 changed** / 0 destroyed（IAM 管理ポリシー + Lambda 3 本） |
| IAM ポリシーサイズ | ✅ **3052 / 6144 バイト**（余裕 3092。statement 数は 9 のまま） |
| タグ条件への反映 | ✅ `AllowInvokeUserTaggedJpAppProfiles` / `DenyJpAppProfilesOutsideJpRegions` の両方に `opus-5-5` が入る |
| global 側への混入なし | ✅ `AllowInvokeUserTaggedGlobalAppProfiles` は `opus-5` のまま（分離されている） |
| 全利用者への展開 | ✅ `11-sync-user-profiles.sh` で **6 名に 6 件新規作成**。既存 4 モデルは再作成なし（冪等） |
| タグ整合 | ✅ 6 名とも `user` / `app=claude-code` / `model=opus-5-5` / **`residency=jp`** |
| 実推論（6 名分の per-user ARN） | ✅ **6/6** 応答 `OK` |
| CloudTrail 監査 | ✅ `inferenceRegion=ap-northeast-1`（**国内**判定）。未許可の国外処理なし |

### IAM ガードレール（simulate-principal-policy 実測）

| ケース | 結果 |
|---|---|
| `opus-5-5` per-user、3タグ揃い / 東京 | ✅ `allowed` |
| `opus-5-5` per-user / 大阪ルーティング | ✅ `allowed`（jp. が大阪へ振る経路を塞がない） |
| `opus-5-5` だが**国外リージョン**（迂回） | ✅ **`explicitDeny`**（明示 Deny で遮断） |
| `opus-5-5` で `user` タグが空（未配賦） | ✅ `implicitDeny` |

### 実装上の注意点

- **`model` タグは `opus-5-5`**。global の `opus-5` と同一値にすると `residency` が
  jp / global で衝突し、棚卸しと IAM 条件の双方が壊れる。
- 週次レポートの単価は **`null`（単価未設定）**。トークン数のみ集計し、実額は
  Cost Explorer 側で見る（推定単価で概算に誤差を持ち込まない既存方針を踏襲）。
  jp. の +10% プレミアムが乗るため、単価確定時は 4.x と同じ逆算手法を使うこと。
- ポータル UI のコスト内訳バッジが `m.model === "opus-5"` の**ハードコード**だったため、
  `modelMeta.residency` 参照へ修正した（放置するとモデル追加時の判定が名前依存になる）。
  あわせて初回表示やタブ先行操作の競合を避けるため、`modelMeta` を確定してからコストを描画する。
- `scripts/01-list-jp-profiles.sh` は Opus 4.8 / Opus 5.5 について、`ACTIVE` かつ
  `models[]` が東京・大阪の foundation-model だけであることを機械判定し、条件違反なら非ゼロ終了する。
- `allow_global_models=false` の plan JSON も検証し、`opus-5` のglobal用IAM・ポータル・レポート定義だけが消え、
  `opus-5-5` は国内IAM・ポータル・レポートに残ることを確認した。

## Opus 5（global）の廃止（2026-09-24）

Opus 5.5 が国内完結で使えるようになったため、**国外ルーティングを許容していた Opus 5 を廃止**した。
これにより、**全モデルが国内完結**の構成に戻った。

### 廃止の判断材料（実測）

| 確認項目 | 結果 |
|---|---|
| Opus 5 の過去30日利用量 | 計 **583 トークン**（内訳は全て展開時の検証呼び出し）= **実業務利用なし** |
| Opus 5.5 の代替可能性 | ✅ 6 名全員が `opus-5-5` を保持し、実推論 6/6 成功 |

### 実施内容

| 層 | 変更 |
|---|---|
| Terraform | `allow_global_models` の既定を **false**、`global_model_profile_ids` を**空リスト**へ（二重の安全弁） |
| IAM | `AllowInvokeUserTaggedGlobalAppProfiles` / `AllowFoundationModelForGlobalProfiles` の **2 statement が消失**。9→7 statement、**3052→2158 バイト** |
| ポータル / レポート | `MODEL_SOURCES_JSON` ・`MODELS_JSON` から `opus-5` が消え、国内4モデルのみに |
| per-user プロファイル | `cc-<user>-opus-5` **6 件を削除**（新規 `scripts/12-retire-model-profiles.sh`） |
| 監査スクリプト | `04-check-cloudtrail.sh` の `GLOBAL_MODEL_TAGS` 既定を `opus-5` → **空**へ（国外処理を誤って「許可済み」分類しない） |

⚠️ **IAM で塞ぐだけでは per-user プロファイルが残骸として残る**（Terraform 管理外のため）。
廃止時は実体の削除までセットで行うこと。

### 廃止後の検証

| 確認項目 | 結果 |
|---|---|
| `terraform apply` | ✅ 0 added / **4 changed** / 0 destroyed |
| IAM: Opus 5 の国外ルーティング | ✅ **`explicitDeny`** |
| IAM: Opus 5 を東京で呼ぶ | ✅ **`implicitDeny`**（許可 statement 自体が消失） |
| IAM: Opus 5.5 東京 / 大阪 | ✅ `allowed`（デグレなし） |
| IAM: Opus 5.5 を国外で呼ぶ | ✅ `explicitDeny` |
| プロファイル削除 | ✅ 6 件削除。`opus-5-5` は全件無傷（完全一致での選別） |
| 残存モデル | ✅ haiku / opus / opus-5-5 / sonnet が各 6 名、**全件 `residency=jp`** |
| Opus 5.5 実推論 | ✅ **6/6** 応答 `OK` |

### probe.user / e2e.validation について

利用者から問い合わせのあった 2 件は、**すでに不要・削除済み**だった。

- アプリ推論プロファイル: **存在しない**（`app=claude-code` は実利用者 6 名のみ）
- IAM ユーザー: **存在しない**（検証時の一時ユーザーは削除済み）
- 週次レポートに出るのは **Cost Explorer の過去課金履歴**（いずれも **$0.00**）。
  タグ別課金履歴は削除できない仕様で、集計期間がずれれば自然に消える。
  **追加作業不要**。

## 週次レポートの集計期間・今月累計整合（2026-09-24）

「利用者別コスト、トークン消費量が今月累計と合わない」との指摘を受け、集計期間と表示を修正した。

### 原因

旧レポートは以下を同時に表示していたが、期間を十分明示していなかった:

- トークン消費量・利用者別コスト: 直近7日
- 実コストの今月累計: 月初から

さらに Cost Explorer の期間は `now - 7日` の日付から翌日（排他）までとしていたため、
実際には **8暦日を含み得る境界ずれ**もあった。

### 修正

- UTC暦日境界を共通関数で作り、期間中は**常に正確な7暦日**へ統一
- トークン消費量を「期間中」と「今月累計」の**2表**で表示
- 利用者別コストを「期間中」「今月累計」の**2列**で表示
- 今週未利用でも今月利用がある利用者を和集合で残す
- `probe.user` / `e2e.validation` のように両期間とも表示上 `$0.00` の微小な検証履歴は除外
- 期間中／月次の一方だけCE取得に失敗した場合は、取得できた側だけを表示して失敗理由も残す
- CloudWatch のモデルID集合は一度だけ動的発見し、2期間で同一集合を使う
- API呼び出し倍増に備え report Lambda timeout を 120→180 秒へ延長

### 実測

| 指標 | 期間中（2026-09-18〜09-24） | 今月累計（2026-09-01〜09-24） |
|---|---:|---:|
| トークン概算費用（単価設定済みモデル） | **$45.91** | **$1,939.69** |
| CE 実コスト（`app=claude-code` タグ配賦） | **$45.91** | **$1,930.81** |
| 利用者別コスト合計 | **$45.91** | **$1,930.81** |

期間中は概算と実コストが一致。利用者別合計は両期間ともCE全体と完全一致した。
今月累計の概算と実コストの差 `$8.88` は、**Opus 5.5 の単価未設定・CloudWatchは全呼出・
CEはタグ配賦分のみ・CE最大24h反映遅延**による既知差で、利用者別集計の不整合ではない。

Teams 投稿は `{"posted": true, "models": 4}`、最新版の実行時間約16秒（timeout 180秒）で成功。
回帰テストは 19→27 件に増加。

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
