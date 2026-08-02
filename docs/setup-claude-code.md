# Claude Code セットアップ（検証 3 実測済み・2026-07-14）

Claude Code CLI から Bedrock の **jp. プロファイル（国内完結）× Claude Opus 4.8** を Bearer API キーで使う手順。
**エンドツーエンド実測済み**（チャット応答・ツール使用によるファイル生成の両方を確認）。

## 0. 前提（運用者側で完了済みであること）

- アカウントの **Anthropic use case フォーム提出**と **Opus 4.8 の契約作成**（初回のみ。[poc-checklist.md](poc-checklist.md) 参照）
- 利用者用 IAM ユーザー + jp. 限定ポリシー（[infra/main.tf](../infra/main.tf)）
- Bedrock API キーの発行（`scripts/10-issue-api-key.sh`。有効期限つき）
- **利用者ごとのアプリケーション推論プロファイル**（コスト配賦用。次節参照）

## 0.5. 利用者ごとのプロファイル作成（運用者・コスト配賦用）

Bedrock のオンデマンド推論はリソース非依存の課金のため、**誰がいくら使ったか**を割り出すには
利用者ごとにタグ付きアプリケーション推論プロファイルを作り、各自にその ARN を使わせる。
API キーは共有のままでよい（課金は「呼び出したプロファイル」に付いた `user` タグで集計される）。
プロファイル自体は無償で、jp. の +10% プレミアムや推論先（東京+大阪）は元プロファイルを継承する。

利用者 1 名につき **Opus 4.8（主力）＋ Haiku 4.5（軽量）の 2 本**を作る。命名は `cc-<user>-opus` /
`cc-<user>-haiku`（プロファイル名にドットは使えないので `.` は `-` に置換）。

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-northeast-1
OPUS_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-opus-4-8"
HAIKU_SRC="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"

# 利用者を列挙（IAM ユーザー名と一致させると監査しやすい）
for U in takeshi.ohno riku.ibaraki takashi.kuwabara daisuke.kawashima yusuke.kobayashi hiroyuki.eguchi; do
  N=$(echo "$U" | tr '.' '-')   # プロファイル名はドット不可
  aws bedrock create-inference-profile --region "$REGION" \
    --inference-profile-name "cc-${N}-opus" \
    --model-source copyFrom="$OPUS_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus \
    --query 'inferenceProfileArn' --output text | sed "s|^|${U} opus: |"
  aws bedrock create-inference-profile --region "$REGION" \
    --inference-profile-name "cc-${N}-haiku" \
    --model-source copyFrom="$HAIKU_SRC" \
    --tags key=user,value=$U key=app,value=claude-code key=model,value=haiku \
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
$HAIKU_SRC = "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"

# 利用者を列挙（IAM ユーザー名と一致させると監査しやすい）
$users = @("takeshi.ohno","riku.ibaraki","takashi.kuwabara","daisuke.kawashima","yusuke.kobayashi","hiroyuki.eguchi")
foreach ($U in $users) {
  $N = $U.Replace(".", "-")   # プロファイル名はドット不可
  $opus = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-opus" `
    --model-source copyFrom="$OPUS_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=opus `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U opus: $opus"
  $haiku = aws bedrock create-inference-profile --region $REGION `
    --inference-profile-name "cc-$N-haiku" `
    --model-source copyFrom="$HAIKU_SRC" `
    --tags key=user,value=$U key=app,value=claude-code key=model,value=haiku `
    --query 'inferenceProfileArn' --output text
  Write-Output "$U haiku: $haiku"
}
```

> バッククォート（`` ` ``）は PowerShell の行継続文字。`--tags` は `key=...,value=...` を
> スペース区切りで並べる（bash と同一）。
</details>

- **タグ**: `user`（集計軸・IAM ユーザー名に合わせる）/ `app=claude-code`（他用途と分離。**キー通知の
  Lambda はこのタグでプロファイルを列挙する**）/ `model`（opus・haiku の内訳）
- **`--description` は付けない**: ASCII の一部記号（括弧など）で ValidationException になる。不要なら省略が安全
- 作成済み一覧: `aws bedrock list-inference-profiles --region ap-northeast-1 --type-equals APPLICATION`
- **コスト配分タグの有効化**: `user` タグを Billing コンソール（または
  `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=user,Status=Active`）で
  有効化する。**新しいタグキーはそのタグ付きの課金が一度発生してからでないと認識されない**（遡及もしない）。
  `user` が既に Active なら即集計可能
- **集計**: `aws ce get-cost-and-usage --time-period Start=YYYY-MM-01,End=YYYY-MM-DD --granularity MONTHLY --metrics UnblendedCost --filter '{"Tags":{"Key":"app","Values":["claude-code"]}}' --group-by Type=TAG,Key=user`
- **限界（性善説）**: 各自が自分の ARN を設定する前提。強制はできない（厳密に分けたいなら利用者ごとに API キー＝IAM プリンシパルを分ける）。各自に配る ARN はキー通知（Teams）に同梱される（[rotate_key.py](../lambda/rotate_key.py)）

> Opus 5 について: Marketplace 契約済みでも、現時点で **jp.（国内完結）プロファイルは未提供**
> （`jp.anthropic.claude-opus-5` は存在しない。あるのは `global.` のみ）。国内完結を維持するため
> Opus 4.8 を使う。jp. が提供されたら `OPUS_SRC` を差し替えて同手順で追加できる。

## 1. 利用者の設定

**キーは Teams のローテ通知に記載された「新しいキー」をコピーする**（毎週月曜 09:00 JST に自動投稿。
Azure 版と同じチャネル）。旧キーは次回ローテで削除されるため、通知が来たら 1 週間以内に貼り替えること。
運用者は SSM からも取得できる:
`aws ssm get-parameter --name /editor-claude-bedrock/api-key --with-decryption --query Parameter.Value --output text`

受け取ったキーをシェルまたは `~/.claude/settings.json` に設定する。

**環境変数の場合**（`~/.zshrc` 等）:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=ap-northeast-1
export AWS_BEARER_TOKEN_BEDROCK='<配布されたキー>'
# 主力: Opus 4.8（国内完結・コスト配賦タグつき）— アプリケーション推論プロファイル ARN を推奨
# （ARN は `terraform output application_inference_profile_arns` で確認。実測済み 2026-07-14）
export ANTHROPIC_MODEL='arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<PROFILE_ID>'
# タグ配賦が不要なら jp. システムプロファイル直指定でも可:
# export ANTHROPIC_MODEL='jp.anthropic.claude-opus-4-8'
# 補助タスク用の軽量モデル（どちらの変数名も設定しておく）
export ANTHROPIC_SMALL_FAST_MODEL='jp.anthropic.claude-haiku-4-5-20251001-v1:0'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='jp.anthropic.claude-haiku-4-5-20251001-v1:0'
```

**settings.json の場合**（`~/.claude/settings.json` — プロジェクト側 `.claude/settings.json` でも可）:

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "ap-northeast-1",
    "ANTHROPIC_MODEL": "jp.anthropic.claude-opus-4-8",
    "ANTHROPIC_SMALL_FAST_MODEL": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
```

> キー（`AWS_BEARER_TOKEN_BEDROCK`）は settings.json に書かずシェル環境変数で渡すこと
> （設定ファイルの共有・コミット事故を防ぐ）。

節約したい日常タスクは `--model jp.anthropic.claude-sonnet-4-6` への切替も可
（jp. 対応モデルは全て IAM 許可済み。単価は Opus $6.6/$33.0（AWS 料金表 2026-07 実測）、Sonnet/Haiku は料金表に公開行がなく未確定 per 1M・jp +10% 込み）。

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
| 401 / `access_denied` | キー失効（有効期限切れ）or jp. 以外のモデルを指定 | 新キーを受領 / `jp.` プレフィックスのモデルに戻す |
| `Converse` の疎通確認は通るのに Claude Code が動かない | Claude Code は **InvokeModelWithResponseStream** を使う。**Converse は use case フォーム未提出でも通ってしまう**（AWS の執行不整合）ため疎通確認としては不十分 | 動作確認は本ページ §2 の `claude -p` で行う |

## 4. 運用メモ

- 監査: 利用は CloudTrail に `InvokeModelWithResponseStream`（modelId=jp.…）として記録され、
  成功呼び出しには `inferenceRegion`（ap-northeast-1/3）が付く。`scripts/04-check-cloudtrail.sh` で確認
- 迂回防止: `jp.` 以外のモデル指定は IAM で拒否される（エラーになるのが正常）
- Claude 5 系（fable-5 / sonnet-5）は jp. 未対応のため設定不可。対応され次第 `ANTHROPIC_MODEL` を差し替え

## 5. Windows での差分（未実測）

接続情報・IAM 統制・監査は macOS と同一。OS 依存の差分は環境変数の入れ方と設定ファイルの場所だけ。
（PoC 検証 3 の実測は macOS のみ。Windows で完走できたら [poc-checklist.md](poc-checklist.md) に追記すること）

- **設定ファイル**: `~/.claude/settings.json` → `%USERPROFILE%\.claude\settings.json`（中身の `env` ブロックは §1 と同一）
- **環境変数（PowerShell・現在のセッションのみ）**:

  ```powershell
  $env:CLAUDE_CODE_USE_BEDROCK = "1"
  $env:AWS_REGION = "ap-northeast-1"
  $env:AWS_BEARER_TOKEN_BEDROCK = "<配布されたキー>"
  $env:ANTHROPIC_MODEL = "jp.anthropic.claude-opus-4-8"   # または application-inference-profile ARN
  ```

- **恒久化**: `setx CLAUDE_CODE_USE_BEDROCK 1`（新しいプロセスから有効。既存ターミナルは再起動が必要）。
  ただし **キー（`AWS_BEARER_TOKEN_BEDROCK`）は `setx` で恒久化しない** — 週次ローテの秘密が全プロセスから
  読めてしまうため、セッション変数（`$env:`）で都度渡す。削除は
  `[Environment]::SetEnvironmentVariable("ANTHROPIC_MODEL", $null, "User")`
- **debug ログ**: `$env:ANTHROPIC_LOG = "debug"; claude -p "ping"`
