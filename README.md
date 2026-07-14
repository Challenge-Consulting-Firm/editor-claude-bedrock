# エディタ用 Claude on AWS Bedrock — 国内完結 PoC

姉妹リポジトリ editor-openai-foundry（社内・非公開）（Azure 版・稼働中）で
**実測の結果諦めた「推論の国内完結」**を、AWS Bedrock の **日本国内クロスリージョン推論プロファイル（`jp.` プロファイル）** で
取り戻せるかを実測する PoC。モデルは **Claude Opus 4.8** を前提とする。

> Azure 版の教訓（公式提供表と実環境の乖離を 4 回踏んだ）に従い、本 PoC も
> **「書類上できるはず」を一切信用せず、全て実測で白黒つける**方針。判定は [docs/poc-checklist.md](docs/poc-checklist.md) に記録する。

## PoC で白黒つける 3 点

| # | 検証項目 | 手段 | 結果（2026-07-14 実測） |
|---|---|---|---|
| 1 | **`jp.` プロファイルに Opus 4.8 が実在するか** | `scripts/01-list-jp-profiles.sh`（`list-inference-profiles` 実測） | ✅ **OK** — `jp.anthropic.claude-opus-4-8`（東京+大阪） |
| 2 | **OpenAI 互換エンドポイントから `jp.` プロファイルを指定して国内完結推論が成立するか** | `scripts/03-invoke-openai-compat.sh` + `scripts/04-check-cloudtrail.sh` | ⚠️ **(a)直結 NG / (b)迂回防止 OK / (c)国内完結 OK** — ネイティブ Converse では完全成立。OpenAI 互換×Claude が AWS 側に存在しない（下記「実測で分かった制約」） |
| 3 | **エディタ/CLI から Bedrock API キーで実際に動くか** | [docs/setup-claude-code.md](docs/setup-claude-code.md) / [docs/setup-zed.md](docs/setup-zed.md) | ✅ **Claude Code CLI で OK**（チャット+エージェント、`inferenceRegion=ap-northeast-1` 確認済み）。✅ **Zed もネイティブ Bedrock プロバイダで直結 OK**（チャット=Opus 4.8 / エージェント=Sonnet 4.6。プロキシ不要）。VS Code は需要が出たら Continue で実測（[poc-checklist.md](docs/poc-checklist.md)） |

補助検証: `scripts/02-invoke-converse.sh`（ネイティブ Converse での疎通 = 問題の切り分け用）、
`scripts/03` の**ネガティブテスト**（`jp.` 以外のプロファイルが IAM で拒否されること = 迂回防止の実証）。

## Azure 版との対比（この PoC が検証する差分）

| 軸 | Azure（稼働中） | Bedrock（本 PoC） |
|---|---|---|
| 国内完結 | ❌ 実測で不可（DataZone/APAC 止まり） | ✅ 見込み: `jp.` プロファイルで東京+大阪に限定（**+10% プレミアム**） |
| 迂回防止 | deployment 名の運用規約のみ | **IAM ポリシーで `jp.` 以外を拒否**（本リポジトリの Terraform で実装） |
| 監査 | KQL（利用量） | **CloudTrail の `inferenceRegion` で実処理リージョンを事後監査** |
| エディタからキー利用 | ✅ 実証済み（api-key） | △ **要実測**: OpenAI 互換エンドポイント + Bedrock API キー（Bearer） |
| モデル | gpt-5.2（APAC） | Claude Opus 4.8（国内完結・見込み） |

## 実測で分かった制約（ap-northeast-1/3・2026-07-14）

詳細な経緯・生データは [docs/poc-checklist.md](docs/poc-checklist.md)。要点:

1. **プロファイル ID は `jp.anthropic.claude-opus-4-8`**（日付サフィックスなし。事前推定の `...-20260528-v1:0` は誤りだった）
2. **「国内完結 × Opus 4.8 × API キー(Bearer)」はネイティブ API（Converse）で完全成立**。
   CloudTrail の `inferenceRegion=ap-northeast-1` を確認済み。IAM の jp. 限定ポリシーも機能
   （`global.` プロファイル・素のモデル ID は access_denied。**ただし HTTP は 403 でなく 401 で返る**）
3. ❌ **OpenAI 互換 API × Claude は現時点の AWS に存在しない**（検証2(a) NG の核心）:
   - runtime `/openai/v1`（東京・大阪とも）: カタログが **gpt-oss 系専用**。Claude は jp./global./素の ID 全て `model_not_found`。
     **管理者 SigV4 でも同じ**（= IAM 要因ではない）
   - runtime `/v1`（新パス）: ap-northeast には**未展開**（`UnknownOperationException`）
   - `bedrock-mantle`（東京 `.api.aws` に実在）: 独自カタログ制で**推論プロファイル不可**。
     Claude はカタログに載っているが chat/completions・responses **どちらの API も非対応**
   - エラーの評価順序は「**IAM が先・モデルカタログ照会が後**」。IAM 拒否と 404 を混同して 2 回誤読しかけたので注意
4. **mantle は国内完結統制の穴になり得る**: IAM が project 単位（モデル単位でない）のため、許可すると
   独自カタログの他モデル（DeepSeek/Qwen 等・処理リージョン不明）を jp. 統制の外で呼べてしまう。
   本 PoC のポリシーからは**許可を撤去済み**（[infra/main.tf](infra/main.tf) のコメント参照）
5. jp. プロファイルは**リージョンごとの account スコープ ARN** を持つ（大阪エンドポイント経由は
   `ap-northeast-3` の ARN で IAM 評価される）。IAM ポリシーは両リージョンの ARN を許可する必要がある
6. jp. の大阪ルーティングに備え、明示 Deny の許容リージョンは**東京+大阪の 2 つ**が必要
   （東京のみにすると jp. プロファイル内部の大阪ルーティングが拒否される — 実際に踏んだ）
7. Bearer キー利用には `bedrock:CallWithBearerToken` の Allow が別途必要
8. Claude 5 系（`claude-fable-5` / `claude-sonnet-5`）は東京に提供済みだが **jp. プロファイル未対応**
   （Azure の「最新モデルほど地域限定が遅い」と同じ構図。国内完結の最上位は当面 Opus 4.8）
9. **Anthropic の use case フォーム提出（Model access の初回手続き）は必須**だが、執行が API で不整合:
   **Converse は未提出でも通る / InvokeModel は 404 で拒否**。`get-foundation-model-availability` が
   AUTHORIZED を返しても手続き完了を意味しない。Claude Code は InvokeModel を使うためここで止まる
10. **Opus 系のみ Marketplace 契約が追加で必要**（403: `aws-marketplace:Subscribe` 系）。Haiku/Sonnet は不要。
    コンソールの Model access フローで管理者が一度完了させれば、利用者側の IAM に Marketplace 権限は不要
11. Claude Code CLI の「model is not available on your bedrock deployment」表示は誤解を招く
    — 実体は上記 9/10 の 404/403（`ANTHROPIC_LOG=debug` で実エラーを確認できる）

**帰結**: OpenAI 互換直結は不可だが、**ネイティブ Bedrock 対応クライアントなら直結できる**ことを実測で確認 —
Claude Code CLI / VS Code 拡張（`CLAUDE_CODE_USE_BEDROCK`）と Zed（ネイティブ Bedrock プロバイダ）で成立。
当初想定していた LiteLLM 等のプロキシは**不要だった**。[docs/poc-checklist.md](docs/poc-checklist.md) を参照

## 使い方

### 0. 前提ツール

```bash
brew install awscli terraform jq
aws configure --profile <PoC用プロファイル>   # 検証用 AWS アカウントの認証情報
```

> **モデルアクセス**: Bedrock コンソール（ap-northeast-1）の Model access で
> Anthropic Claude Opus 4.8 を有効化しておくこと（初回のみ・コンソール操作）。

### 1. 設定 → デプロイ（IAM 統制 + Budget）

```bash
cp .env.sample .env        # .env は .gitignore 済み。値を実値に置き換える
./scripts/deploy.sh        # terraform init → plan → 確認 → apply
```

作られるもの: PoC 用 IAM ユーザー（`jp.` プロファイル以外の推論を拒否するポリシー付き）、月次 Budget（50/75/90% 通知）。

### 2. PoC 実測（番号順に実行）

```bash
./scripts/00-preflight.sh            # CLI・認証・リージョン・モデルアクセスの事前確認
./scripts/01-list-jp-profiles.sh     # 検証1: jp. プロファイルの実在確認 → .env の JP_PROFILE_ID を確定
./scripts/10-issue-api-key.sh        # PoC ユーザーに Bedrock API キー（長期・有効期限付き）を発行 → .env に設定
./scripts/02-invoke-converse.sh      # 補助: ネイティブ Converse 疎通（切り分け用）
./scripts/03-invoke-openai-compat.sh # 検証2前半: OpenAI 互換 + Bearer キー + jp. プロファイル（ネガティブテスト含む）
./scripts/04-check-cloudtrail.sh     # 検証2後半: inferenceRegion が ap-northeast-1/3 であること
```

### 3. エディタ実測（検証 3）

[docs/setup-claude-code.md](docs/setup-claude-code.md) / [docs/setup-zed.md](docs/setup-zed.md) /
[docs/setup-vscode.md](docs/setup-vscode.md) の手順で各 1 回実測し、
結果を [docs/poc-checklist.md](docs/poc-checklist.md) に記録する。

## コストの目安

- Opus 4.8: **$5 入力 / $25 出力**（per 1M トークン）。`jp.` プロファイルは **+10%** → 実効 **$5.5 / $27.5**
- Prompt Caching（読み取り 0.1 倍）は Bedrock でも同単価で有効。エージェント用途では実効コストを大きく下げる
- PoC 中の暴走防止として Budget（既定 $200/月、50/75/90% でメール通知）を Terraform で配備。
  **ハードストップ（Azure 版の disableLocalAuth 相当）は本番化スコープ**（[docs/design.md](docs/design.md) §7）

## ドキュメント

- [docs/design.md](docs/design.md) — PoC アーキテクチャ・IAM 統制設計・キー運用・本番化 TODO
- [docs/poc-checklist.md](docs/poc-checklist.md) — 検証手順・判定基準・**実測記録（正）**
- [docs/setup-claude-code.md](docs/setup-claude-code.md) / [docs/setup-zed.md](docs/setup-zed.md) / [docs/setup-vscode.md](docs/setup-vscode.md) — エディタ/CLI 設定手順
