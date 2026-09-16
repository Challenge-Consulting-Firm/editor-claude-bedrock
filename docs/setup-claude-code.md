# Claude Code セットアップ（検証 3 実測済み・2026-07-14）

Claude Code CLI から Bedrock の **国内3モデル（`jp.`）と Opus 5（`global.`）**を Bearer API キーで使う手順。
国内3モデルのエンドツーエンド動作は実測済み。Opus 5 はユーザー別アプリ推論プロファイル経由でのみ許可する。

## 0. 前提（運用者側で完了済みであること）

- アカウントの **Anthropic use case フォーム提出**と **Opus 4.8 / Opus 5 の契約・認可**
  （`scripts/00-preflight.sh` で Opus 5 が `AUTHORIZED` / `AVAILABLE` か確認）
- 利用者用 IAM ユーザー + jp. 限定ポリシー（[infra/main.tf](../infra/main.tf)）
- Bedrock API キーの発行（`scripts/10-issue-api-key.sh`。有効期限つき）
- **利用者ごとのアプリケーション推論プロファイル**（コスト配賦用。次節参照）

## 0.5. 利用者ごとのプロファイル作成（運用者・コスト配賦用）

Bedrock のオンデマンド推論はリソース非依存の課金のため、**誰がいくら使ったか**を割り出すには
利用者ごとにタグ付きアプリケーション推論プロファイルを作り、各自にその ARN を使わせる。
API キーは共有のままでよい（課金は「呼び出したプロファイル」に付いた `user` タグで集計される）。
プロファイル自体は無償。国内3モデルは jp. の推論先（東京+大阪）と +10% プレミアムを継承し、
Opus 5 は global ルーティングを継承する。いずれもユーザー別の `user` タグで棚卸しする。

利用者 1 名につき **Opus 4.8（国内）＋ Sonnet 4.6（国内）＋ Haiku 4.5（国内）＋ Opus 5（global）の4本**を
ポータルから作成する。既存利用者は再度「作成」を押すと、不足している Opus 5 だけが追加される。
以下のCLI例は国内3モデルを手動作成する旧手順であり、Opus 5 はポータル利用を推奨する。

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-northeast-1
OPUS_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-4-8"
SONNET_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-sonnet-4-6"
HAIKU_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"

# 利用者を列挙（IAM ユーザー名と一致させると監査しやすい）
for U in takeshi.ohno riku.ibaraki takashi.kuwabara daisuke.kawashima yusuke.kobayashi hiroyuki.eguchi; do
  N=$(echo "$U" | tr '.' '-')   # プロファイル名はドット不可
  aws bedrock create-inference-profile --region "$REGION" \
    --inference-profile-name "cc-${N}-opus" \
    --model-source copyFrom="$OPUS_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus key=residency,value=jp \
    --query 'inferenceProfileArn' --output text | sed "s|^|${U} opus: |"
  aws bedrock create-inference-profile --region "$REGION" \
    --inference-profile-name "cc-${N}-sonnet" \
    --model-source copyFrom="$SONNET_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=sonnet key=residency,value=jp \
    --query 'inferenceProfileArn' --output text | sed "s|^|${U} sonnet: |"
  aws bedrock create-inference-profile --region "$REGION" \
    --inference-profile-name "cc-${N}-haiku" \
    --model-source copyFrom="$HAIKU_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=haiku key=residency,value=jp \
    --query 'inferenceProfileArn' --output text | sed "s|^|${U} haiku: |"
done
```

<details>
<summary>PowerShell 版（Windows・未実測）</summary>

同じ内容を PowerShell で。`--tags` の指定形式は OS 非依存だが、変数展開とループ構文が異なる。

```powershell
$ACCOUNT_ID = aws sts get-caller-identity --query Account --output text
$REGION = "ap-northeast-1"
$OPUS_SRC = "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-4-8"
$SONNET_SRC = "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-sonnet-4-6"
$HAIKU_SRC = "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"

# 利用者を列挙（IAM ユーザー名と一致させると監査しやすい）
$users = @("takeshi.ohno","riku.ibaraki","takashi.kuwabara","daisuke.kawashima","yusuke.kobayashi","hiroyuki.eguchi")
foreach ($U in $users) {
  $N = $U.Replace(".", "-")   # プロファイル名はドット不可
  $opus = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-opus" `
    --model-source copyFrom="$OPUS_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus key=residency,value=jp `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U opus: $opus"
  $sonnet = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-sonnet" `
    --model-source copyFrom="$SONNET_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=sonnet key=residency,value=jp `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U sonnet: $sonnet"
  $haiku = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-haiku" `
    --model-source copyFrom="$HAIKU_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=haiku key=residency,value=jp `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U haiku: $haiku"
}
```

> バッククォート（`` ` ``）は PowerShell の行継続文字。`--tags` は `key=...,value=...` を
> スペース区切りで並べる（bash と同一）。
</details>

- **タグ**: `user`（集計軸）/ `app=claude-code`（他用途と分離）/ `model`（`opus` / `sonnet` / `haiku` / `opus-5`）/
  `residency`（新規作成分は `jp` / `global`）。Opus 5 は4タグすべてが揃ったper-userプロファイルだけIAMで呼び出し可能
- **`--description` は付けない**: ASCII の一部記号（括弧など）で ValidationException になる。不要なら省略が安全
- 作成済み一覧: `aws bedrock list-inference-profiles --region ap-northeast-1 --type-equals APPLICATION`
- **コスト配分タグの有効化**: `user` / `app` / `model` / `residency` を Billing コンソールまたはCLIで有効化する。
  `user` は利用者合計、`model` は各利用者がOpus 5を使った額、`residency` は国内/globalの区別に使う。
  **新しいタグキーはそのタグ付き課金が一度発生してからでないと認識されず、有効化前へ遡及もしない**
- **集計**: `aws ce get-cost-and-usage --time-period Start=YYYY-MM-01,End=YYYY-MM-DD --granularity MONTHLY --metrics UnblendedCost --filter '{"Tags":{"Key":"app","Values":["claude-code"]}}' --group-by Type=TAG,Key=user`
- **限界（共有キー）**: IAMはper-userタグ付きプロファイルの使用までは強制するが、共有キーでは「本人が自分のARNを使う」ことまでは
  技術的に強制できない。ポータル表示と運用で本人ARNを徹底し、厳密な本人紐付けが必要なら利用者ごとにAPIキー/IAMプリンシパルを分ける

### 全利用者の一括同期（新モデル追加時・推奨）

モデルを追加したときは、各自にポータル操作を依頼する代わりに運用者が一括作成できる:

```bash
./scripts/11-sync-user-profiles.sh --dry-run   # 差分確認
./scripts/11-sync-user-profiles.sh             # 全利用者を同期
./scripts/11-sync-user-profiles.sh --user 新メンバー名  # 1 名だけ
```

- モデル定義は**デプロイ済み profile_ui Lambda の `MODEL_SOURCES_JSON` を参照**するので、
  Terraform の `allow_global_models` / `global_model_profile_ids` と常に一致する（定義の二重管理なし）
- **冪等**: 既存プロファイルはスキップし、不足分だけ作る。`residency` タグが無い旧プロファイルには補完する
- 対象利用者は既存の `app=claude-code` プロファイルから自動検出する（上記 6 名と一致）
- 実績: 2026-09-16 に本スクリプトで **Opus 5 を全 6 名に展開**（5 件新規作成・1 名は既存のため変更なし）

> ### Claude 5 系の制約と Opus 5 の運用 — ⚠️ 国内完結ではない
>
> 5 系は **`jp.`（国内完結）プロファイルが未提供**で、東京リージョンに固定して使う手段も存在しない。
> 実測（2026-09-16）で確認した制約:
>
> - `jp.anthropic.claude-opus-5` は**存在しない**（`The provided model identifier is invalid`）
> - 素のモデル ID `anthropic.claude-opus-5` は **on-demand 非対応**
>   （`Invocation of model ID ... with on-demand throughput isn't supported`）= プロファイル経由が強制
> - 東京の foundation-model ARN からアプリ推論プロファイルを作ろうとしても
>   `The provided foundation model does not support On Demand inference` で**東京ピン留め不可**
> - `global.` プロファイルの実体は**リージョン無し ARN を含む全世界ルーティング**。
>   東京エンドポイントから呼んでも CloudTrail の `inferenceRegion` は
>   **Opus 5 → `eu-west-1`（アイルランド）/ Sonnet 5 → `us-east-1`（バージニア）**
>
> **判断（2026-09-16）**: 開発効率を優先し、Opus 5 は**グローバル利用を前提に許可**する。
> ただし国内完結が要る作業（社内コード・顧客データを含むもの）では **Opus 4.8 / Sonnet 4.6 / Haiku 4.5** を使うこと。
> ポータル上では Opus 5 に <code>国外処理</code> バッジが出るので、それを目印に選び分ける。
>
> 統制面では `allow_global_models = false`（Terraform 変数）で Opus 5 を IAM ごと塞げる。
> 許可対象の基盤モデルはallowlistで明示列挙し、システム `global.` の直指定は許可しない。
> （同じ接頭辞の `global.openai.*` / `global.xai.*` 等を巻き込まないため）。
> Opus 5 は利用者ポータルに表示された `user` タグ付きARNからのみ利用できる。
> jp. 版 Opus 5 が提供されたらコピー元を差し替えて国内完結に戻せる。

## 1. 利用者の設定

**キーは利用者ポータルの「現行キー本文」をコピーする**（週次 Teams 通知の URL からポータルを開き、
EntraID サインイン後にコピー）。キーは毎週月曜 09:00 JST に自動ローテーションされ、旧キーは次回ローテで
削除されるため、通知が来たら 1 週間以内に貼り替えること。運用者は SSM からも取得できる:
`aws ssm get-parameter --name /editor-claude-bedrock/api-key --with-decryption --query Parameter.Value --output text`

受け取ったキーをシェルまたは `~/.claude/settings.json` に設定する。

**環境変数の場合**（`~/.zshrc` 等）:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=ap-northeast-1
export AWS_BEARER_TOKEN_BEDROCK='<配布されたキー>'
# 主力モデル: 必ず利用者ポータルに表示された自分専用 ARN を指定する
# 国内完結なら opus、最新モデル（国外処理許容）なら opus-5 の ARN
export ANTHROPIC_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のPROFILE_ID>'
# 補助タスク（サマリ等）も利用者ポータルの自分専用 haiku ARN を指定して配賦する
export ANTHROPIC_DEFAULT_HAIKU_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>'
# コスト削減（推奨）: 調査・検索を行うサブエージェントも Haiku に固定する（§1.5）
export CLAUDE_CODE_SUBAGENT_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>'
export CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1
```

**settings.json の場合**（`~/.claude/settings.json` — プロジェクト側 `.claude/settings.json` でも可）:

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "ap-northeast-1",
    "ANTHROPIC_MODEL": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のPROFILE_ID>",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>",
    "CLAUDE_CODE_SUBAGENT_MODEL": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"
  }
}
```

> キー（`AWS_BEARER_TOKEN_BEDROCK`）は settings.json に書かずシェル環境変数で渡すこと
> （設定ファイルの共有・コミット事故を防ぐ）。

> ※ **`ANTHROPIC_SMALL_FAST_MODEL` は使わない**（旧設定・2026-09-16 削除）。
> 現行のモデル固定は `ANTHROPIC_DEFAULT_OPUS_MODEL` / `..._SONNET_MODEL` / `..._HAIKU_MODEL` /
> `..._FABLE_MODEL` の 4 つで行うのが公式の推奨（[Enterprise deployment overview](https://docs.claude.com/en/docs/claude-code/bedrock-vertex-proxies)
> 「Pin model versions for cloud providers」）。補助タスクは `ANTHROPIC_DEFAULT_HAIKU_MODEL` が後継。
> 旧変数は CLI 2.1.273 でも後方互換で動作する（実測でエラー・警告なし）ため急いで外す必要はないが、
> 新規設定では書かない。

節約したい日常タスクは `--model <自分の cc-<user>-sonnet ARN>` への切替も可
（システム `jp.` の直指定は共有キー利用時のユーザー別棚卸しを迂回するため使わない。単価はOpus $5.5/$27.5、Sonnet $3.3/$16.5、Haiku $1.1/$5.5 per 1M・jp +10%込み。Opus 5は単価確認中）。

## 1.5. サブエージェントのモデルを安いものに固定する（コスト削減・推奨）

Claude Code は調査や検索を**サブエージェント**（Explore / Plan / general-purpose 等）に委譲する。
既定ではサブエージェントも**メイン会話と同じモデル（= Opus）を継承**するため、
「ファイルを探す」だけの作業でも Opus 単価がかかる。ここを Haiku に固定すると
**入力 $5.5 → $1.1、出力 $27.5 → $5.5（約 1/5）**になる。

環境変数 2 つを足すだけでよい。**値には必ず利用者ポータルの自分専用 Haiku ARN を指定する**:

```bash
# サブエージェントを Haiku に固定（メイン会話は ANTHROPIC_MODEL のまま）
export CLAUDE_CODE_SUBAGENT_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>'
# 定義側の model 指定より優先させ、組み込み Explore / Plan を含む全サブエージェントに強制適用する
export CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1
```

`settings.json` なら `env` ブロックに同じ 2 つを追加する:

```json
{
  "env": {
    "CLAUDE_CODE_SUBAGENT_MODEL": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"
  }
}
```

> ⚠️ **`haiku` などのエイリアス名ではなく ARN を指定すること**（本リポジトリ固有の注意点）。
> 実測（2026-09-16・CloudTrail で裏取り）で次の差が出た:
>
> | 指定値 | CloudTrail に記録される modelId | ユーザー別棚卸し |
> |---|---|---|
> | 自分専用 Haiku ARN | `...application-inference-profile/<自分のID>` | ✅ `user` タグで配賦される |
> | `haiku`（エイリアス） | `jp.anthropic.claude-haiku-4-5-...` | ❌ **タグが付かず未配賦になる** |
>
> どちらも動作してコストも下がるが、エイリアスはシステム `jp.` プロファイルを直接呼ぶため
> 週次レポートの利用者別集計から漏れ、「(未配賦)」に入る。

**動作確認**: サブエージェント実行中に `/tasks` を開くと、各行に実際に使われているモデルが表示される。
事後に確かめるなら `./scripts/04-check-cloudtrail.sh 15` で modelId 別の呼び出しを見る。

※ `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` を付けないと `CLAUDE_CODE_SUBAGENT_MODEL` は「既定値」扱いになり、
組み込み Explore / Plan はメイン会話のモデル（Opus）のままになる。全体を安くするなら 2 つセットで設定する。

※ トレードオフ: サブエージェントの推論能力は下がる。単純な検索・列挙なら Haiku で十分だが、
複雑な調査や設計検討を委譲するなら Sonnet ARN にするか、一時的に外す。

### 特定の用途だけモデルを分ける（任意）

カスタムサブエージェントを作るなら `.claude/agents/<name>.md` の frontmatter で個別指定できる:

```markdown
---
name: code-explorer
description: コード検索・調査専用。読み取りのみ。
tools: Read, Grep, Glob
model: arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>
---

コードベースを検索し、要点だけを簡潔に報告する。
```

ただし ARN は利用者ごとに異なるため、プロジェクトにコミットして共有するファイルでは `model: inherit` にし、
実際のモデルは上記の環境変数（各自の設定）で制御する方が安全。

## 2. 動作確認

```bash
claude -p "「国内完結OK」とだけ返答してください"
```

## 3. トラブルシューティング（実測で踏んだ罠）

| 症状 | 実体 | 対処 |
|---|---|---|
| `The model ... is not available on your bedrock deployment` | **表示が誤解を招く**。実体は下 2 行のどちらかが大半 | `ANTHROPIC_LOG=debug claude -p "ping"` で実際の HTTP エラーを確認 |
| （debug で）404 `Model use case details have not been submitted` | アカウントの Anthropic use case フォーム未提出 | 運用者に連絡（管理者作業） |
| （debug で）403 `aws-marketplace:ViewSubscriptions...` | Opus 系の契約未作成 or 作成直後の伝播待ち | 運用者に連絡。作成済みなら **2 分待って再実行** |
| 401 / `access_denied` | キー失効、未許可モデル、またはユーザータグ付きプロファイルを経由していない | 新キーを受領 / 利用者ポータルに表示された自分専用 ARN を設定 |
| `Converse` の疎通確認は通るのに Claude Code が動かない | Claude Code は **InvokeModelWithResponseStream** を使う。**Converse は use case フォーム未提出でも通ってしまう**（AWS の執行不整合）ため疎通確認としては不十分 | 動作確認は本ページ §2 の `claude -p` で行う |

## 4. 運用メモ

- 監査: 成功呼び出しは CloudTrail の `inferenceRegion` で確認する。国内モデルは ap-northeast-1/3、
  Opus 5 は `residency=global` タグ付きper-userプロファイルに限り国外処理を許容。`scripts/04-check-cloudtrail.sh` が両者を分類する
- 迂回防止: Opus 5 の `global.` システムプロファイル直指定は拒否される。必ず利用者ポータルの `opus-5` ARN を使う

## 5. Windows での差分（未実測）

接続情報・IAM 統制・監査は macOS と同一。OS 依存の差分は環境変数の入れ方と設定ファイルの場所だけ。
（PoC 検証 3 の実測は macOS のみ。Windows で完走できたら [poc-checklist.md](poc-checklist.md) に追記すること）

- **設定ファイル**: `~/.claude/settings.json` → `%USERPROFILE%\.claude\settings.json`（中身の `env` ブロックは §1 と同一）
- **環境変数（PowerShell・現在のセッションのみ）**:

  ```powershell
  $env:CLAUDE_CODE_USE_BEDROCK = "1"
  $env:AWS_REGION = "ap-northeast-1"
  $env:AWS_BEARER_TOKEN_BEDROCK = "<配布されたキー>"
  $env:ANTHROPIC_MODEL = "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のPROFILE_ID>"
  ```

- **恒久化**: `setx CLAUDE_CODE_USE_BEDROCK 1`（新しいプロセスから有効。既存ターミナルは再起動が必要）。
  ただし **キー（`AWS_BEARER_TOKEN_BEDROCK`）は `setx` で恒久化しない** — 週次ローテの秘密が全プロセスから
  読めてしまうため、セッション変数（`$env:`）で都度渡す。削除は
  `[Environment]::SetEnvironmentVariable("ANTHROPIC_MODEL", $null, "User")`
- **debug ログ**: `$env:ANTHROPIC_LOG = "debug"; claude -p "ping"`
