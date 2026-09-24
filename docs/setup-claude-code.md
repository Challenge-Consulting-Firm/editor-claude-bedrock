# Claude Code セットアップ（検証 3 実測済み・2026-07-14）

Claude Code CLI から Bedrock の **国内4モデル（すべて `jp.` = 国内完結）**を Bearer API キーで使う手順。
エンドツーエンド動作は実測済み。（国外ルーティングの Opus 5 は 2026-09-24 に廃止済み）

## 0. 前提（運用者側で完了済みであること）

- アカウントの **Anthropic use case フォーム提出**と **Opus 4.8 / Opus 5 / Opus 5.5 の提供・認可確認**
  （`scripts/00-preflight.sh` で Opus 5.5 の `jp.` プロファイルと国内推論先、Opus 5 のglobal利用条件を確認）
- 利用者用 IAM ユーザー + jp. 限定ポリシー（[infra/main.tf](../infra/main.tf)）
- Bedrock API キーの発行（`scripts/10-issue-api-key.sh`。有効期限つき）
- **利用者ごとのアプリケーション推論プロファイル**（コスト配賦用。次節参照）

## 0.5. 利用者ごとのプロファイル作成（運用者・コスト配賦用）

Bedrock のオンデマンド推論はリソース非依存の課金のため、**誰がいくら使ったか**を割り出すには
利用者ごとにタグ付きアプリケーション推論プロファイルを作り、各自にその ARN を使わせる。
API キーは共有のままでよい（課金は「呼び出したプロファイル」に付いた `user` タグで集計される）。
プロファイル自体は無償。国内モデルは jp. の推論先（東京+大阪）と +10% プレミアムを継承し、
Opus 5 は global ルーティングを継承する。いずれもユーザー別の `user` タグで棚卸しする。

利用者 1 名につき **Opus 4.8 ＋ Sonnet 4.6 ＋ Haiku 4.5 ＋ Opus 5.5（すべて国内完結）の4本**を
ポータルから作成する。既存利用者は再度「作成」を押すと、不足しているモデルだけが追加される。
以下のCLI例は国内モデルを手動作成する旧手順であり、現在はポータル利用を推奨する。

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-northeast-1
OPUS_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-4-8"
OPUS_55_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-5-5"
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
    --inference-profile-name "cc-${N}-opus-5-5" \
    --model-source copyFrom="$OPUS_55_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus-5-5 key=residency,value=jp \
    --query 'inferenceProfileArn' --output text | sed "s|^|${U} opus-5-5: |"
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
$OPUS_55_SRC = "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-5-5"
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
  $opus55 = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-opus-5-5" `
    --model-source copyFrom="$OPUS_55_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus-5-5 key=residency,value=jp `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U opus-5-5: $opus55"
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

- **タグ**: `user`（集計軸）/ `app=claude-code`（他用途と分離）/ `model`（`opus` / `sonnet` / `haiku` / `opus-5-5`）/
  `residency`（現在は全件 `jp`）。3タグが揃ったper-userプロファイルだけIAMで呼び出し可能
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
- 実績: 2026-09-24 に **Opus 5.5（国内完結）を全 6 名に展開**（6 件新規作成。既存 4 モデルは再作成なし）

> ### Opus 5.5 は国内完結で使える（✅ 2026-09-24 実測）
>
> **`jp.anthropic.claude-opus-5-5` が提供された**ため、Opus 5.5 は国内完結モデルとして扱う。
> 実測で確認した内容:
>
> - `jp.anthropic.claude-opus-5-5` は **ACTIVE**。`models[]` は
>   `ap-northeast-1` / `ap-northeast-3` の foundation-model のみ = **推論先が国内に閉じる**
> - jp. システムプロファイル直呼び・jp. 由来の per-user アプリ推論プロファイル経由とも **Converse 成功**
> - CloudTrail の `inferenceRegion` は **`ap-northeast-1`**（国内判定）
> - 国外リージョンからの呼び出しは IAM の明示 Deny で遮断（`explicitDeny` を実測）
>
> したがって Opus 5.5 は `residency=jp` タグで作成され、ポータルでも
> <code>国外処理</code> バッジは付かない。**機密度の高い作業でも使える**。
>
> ⚠️ `model` タグは `opus-5-5` で、global の `opus-5` とは**別モデル扱い**。
> 同一視すると `residency` が jp / global で衝突し、棚卸しと IAM 条件の双方が壊れる。

> ### Opus 5（global）は廃止しました（✅ 2026-09-24）
>
> **Opus 5.5 が国内完結で使えるようになったため、国外ルーティングを許容していた
> Opus 5（`opus-5`）を廃止しました。** 「国内完結 vs 最新モデル」の二択が解消され、
> 国外処理を許容する理由がなくなったためです。
>
> 実施内容:
> - `allow_global_models` の既定を **false** へ、allowlist を**空**へ変更
> - IAM から global 用 statement 2 つを削除（ポリシー 3052 → 2158 バイト）
> - per-user の `cc-<user>-opus-5` プロファイル **6 件を削除**
>   （`./scripts/12-retire-model-profiles.sh --model opus-5 --apply`）
>
> 廃止前の Opus 5 の利用実績は 30 日で計 583 トークン（すべて検証由来）で、
> 実業務利用はありませんでした。**現在は全モデルが国内完結**で、
> ポータルに <code>国外処理</code> バッジが出るモデルはありません。
>
> <details><summary>参考: Opus 5 を廃止するに至った制約（実測 2026-09-16）</summary>
>
> `jp.`（国内完結）プロファイルが未提供で、東京固定で使う手段がなかった:
>
> - `jp.anthropic.claude-opus-5` は**存在しない**（`The provided model identifier is invalid`）
> - 素のモデル ID `anthropic.claude-opus-5` は **on-demand 非対応**
> - 東京の foundation-model ARN からのアプリ推論プロファイル作成も不可
> - `global.` の実処理先は **Opus 5 → `eu-west-1` / Sonnet 5 → `us-east-1`**
>
> 将来再び `jp.` 未提供の最新モデルを使う必要が生じた場合は、
> `allow_global_models = true` と allowlist への明示列挙を復活させる（ワイルドカードは使わない）。
> </details>

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
# 国内完結の最新モデルなら opus-5-5、従来モデルなら opus、国外処理を許容する場合だけ opus-5
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

> ※ **`ANTHROPIC_SMALL_FAST_MODEL` は新規設定で使わない**（設定例から 2026-09-16 削除）。
> 現行のモデル固定は `ANTHROPIC_DEFAULT_OPUS_MODEL` / `..._SONNET_MODEL` / `..._HAIKU_MODEL` /
> `..._FABLE_MODEL` の 4 つで行う。公式リファレンスも旧変数を
> 「deprecated in favor of `ANTHROPIC_DEFAULT_HAIKU_MODEL`」と明記している
> （[Model configuration — Environment variables](https://code.claude.com/docs/en/model-config#environment-variables)）。
> 旧変数は CLI 2.1.273 でも後方互換で動作する（実測でエラー・警告なし）ため急いで外す必要はないが、
> 新規設定では書かない。

節約したい日常タスクは `--model <自分の cc-<user>-sonnet ARN>`、国内完結で高性能を優先するなら
`--model <自分の cc-<user>-opus-5-5 ARN>` へ切替できる。システム `jp.` の直指定は共有キー利用時の
ユーザー別棚卸しを迂回するため使わない。単価は Opus 4.8 $5.5/$27.5、Sonnet $3.3/$16.5、
Haiku $1.1/$5.5 per 1M（jp +10%込み）。Opus 5 / Opus 5.5 は単価確認中。

## 1.5. サブエージェントのモデルを安いものに固定する（コスト削減・推奨）

Claude Code は調査や検索を**サブエージェント**（Explore / Plan / general-purpose 等）に委譲する。
既定ではサブエージェントも**メイン会話と同じモデル（= Opus）を継承**するため、
「ファイルを探す」だけの作業でも Opus 単価がかかる。ここを Haiku に固定すると
**入力 $5.5 → $1.1、出力 $27.5 → $5.5（約 1/5）**になる。

環境変数 2 つを足すだけでよい。推奨は**利用者ポータルの自分専用 Haiku ARN を直接指定**する方法:

```bash
# サブエージェントを Haiku に固定（メイン会話は ANTHROPIC_MODEL のまま）
export CLAUDE_CODE_SUBAGENT_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<自分のHAIKU_PROFILE_ID>'
# サブエージェント定義や呼び出し側のmodel指定より優先し、全体を同じモデルへ固定する
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

> ⚠️ **棚卸し漏れを防ぐには、最終的なmodelIdがper-user ARNになっていることを確認する。**
> 実測（2026-09-16・隔離した `CLAUDE_CONFIG_DIR` + JSON出力 + CloudTrail）では次の3ケースになった:
>
> | 設定 | 実際の modelId / `modelUsage` | ユーザー別棚卸し |
> |---|---|---|
> | `CLAUDE_CODE_SUBAGENT_MODEL=<自分専用Haiku ARN>` | per-user ARN | ✅ 配賦される（最も明示的・推奨） |
> | `...SUBAGENT_MODEL=haiku` + `ANTHROPIC_DEFAULT_HAIKU_MODEL=<自分専用ARN>` | per-user ARN | ✅ aliasが固定先ARNへ解決される |
> | `...SUBAGENT_MODEL=haiku` + Haikuの固定なし | `jp.anthropic.claude-haiku-4-5-...` | ❌ システムプロファイル直で未配賦 |
>
> したがって `haiku` エイリアス自体が問題なのではなく、**`ANTHROPIC_DEFAULT_HAIKU_MODEL` の固定漏れ**が問題。
> 本手順の完全な設定例（§1）は同変数もper-user ARNへ固定しているため、エイリアス方式でも配賦されるが、
> 部分的な設定・他環境へのコピーで固定が抜ける事故を避けるため、サブエージェント変数にもARNを直接書く例を推奨する。

**動作確認**: サブエージェント実行中に `/tasks` を開くと、各行に実際に使われているモデルが表示される。
事後に確かめるなら `./scripts/04-check-cloudtrail.sh 15` で modelId 別の呼び出しを見る。

※ `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`（Claude Code **v2.1.257 以降**）を付けないと
`CLAUDE_CODE_SUBAGENT_MODEL` は「既定値」扱いになり、
サブエージェント定義の `model` や呼び出し時model指定が優先される。全体を確実に安くするなら2つセットで設定する。
`FORCE=1` は組み込み Explore / Plan、カスタムサブエージェント、agent team、workflowにも同じモデルを強制する。
例外は会話をforkするサブエージェントと、`model: inherit` のfork型skillで、メイン会話のモデルを使う（公式仕様）。

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
また `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` が有効な間は、このfrontmatterの `model` 値も無視される。
用途ごとに個別モデルを使い分けたい場合は `FORCE` を外し、各定義の `model` で指定する。

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

- 監査: 成功呼び出しは CloudTrail の `inferenceRegion` で確認する。**全モデルが国内完結**のため
  ap-northeast-1/3 以外はすべて異常。`scripts/04-check-cloudtrail.sh` が国内/違反/要確認を分類する
- 迂回防止: `jp.` システムプロファイル直指定でも呼べるが、共有キー運用では利用者別の棚卸しを
  迂回するため使わない。必ず利用者ポータルの自分専用 ARN（`opus` / `sonnet` / `haiku` / `opus-5-5`）を使う。
  廃止済みの `opus-5` ARN は削除済みで、IAM でも拒否される

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
