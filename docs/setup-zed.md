# Zed セットアップ（検証 3 実測済み・2026-07-14）

Zed の**ネイティブ Amazon Bedrock プロバイダ**（1.10.3 で実測）から、jp. プロファイル（国内完結）を
Bedrock API キーで使う手順。**チャット実測済み**（Opus 4.8 カスタム / Sonnet 4.6 組み込みとも成功、
CloudTrail で国内ルーティングを確認）。

> 旧版の本ドキュメントは「OpenAI 互換で直結」を想定していたが、それは不成立（README「実測で分かった制約」#3）。
> 現在の Zed はネイティブ Bedrock プロバイダ + Bedrock API キー認証に対応しており、直結できる。

## 1. できること / できないこと（Zed 1.10.3 実測）

| 使い方 | モデル選択 | 可否 |
|---|---|---|
| チャット × Opus 4.8 × 国内完結 | カスタム「社内: Claude Opus 4.8 (国内完結)」 | ✅ 実測 OK |
| エージェント（ツール込み）× 国内完結 | **組み込みの「Claude Sonnet 4.6」**（jp. 自動付与） | ✅ 実測 OK |
| エージェント × Opus 4.8 | — | ❌ 不可（**Zed 固有の制約**。下記参照） |

> **⚠️ 誤読注意**: 「エージェント × Opus 4.8 不可」は **Zed の AI パネル（ネイティブプロバイダ）固有**の話で、
> **Bedrock 自体の制限ではない**。Opus 4.8 × 国内完結 × ツールは Bedrock 上で成立する —
> Claude Code CLI / VS Code 拡張 / ネイティブ Converse API 経由なら**三方とも満たせる**
> （[docs/setup-claude-code.md](setup-claude-code.md) は `jp.anthropic.claude-opus-4-8` で
> エージェントタスク完走を PoC 検証 3 で実測済み）。

### 経路別マトリクス（Opus 4.8 × 国内完結 × ツール）

| 経路 | 国内完結 | ツール | 備考 |
|---|---|---|---|
| Claude Code CLI（`ANTHROPIC_MODEL=jp.anthropic.claude-opus-4-8`） | ✅ | ✅ | InvokeModelWithResponseStream 使用 |
| VS Code Claude Code 拡張（同上 / アプリケーション推論プロファイル ARN） | ✅ | ✅ | PoC 検証 3 で CloudTrail 裏取り済み |
| ネイティブ Converse API（直接呼び出し） | ✅ | ✅ | CloudTrail で `inferenceRegion=ap-northeast-1` 確認済み |
| **Zed: カスタムモデル**（`available_models` に Opus 4.8 ARN） | ✅ | ❌ | `supports_tool_use()` が false 固定 |
| **Zed: 組み込み Opus**（`allow_global=true`） | ❌ | ✅ | `global.` にルーティングされ国内完結が崩れる |
| **Zed: 組み込み Opus**（`allow_global=false`） | △ | ✅ | 素の `anthropic.claude-opus-4-8` になり PoC の jp.限定 IAM が拒否 |

### Zed で「Opus 4.8 × 国内完結 × ツール」が揃わない理由（Zed 側の制約）

以下はいずれも **Bedrock 側ではなく Zed（upstream 修正候補）** の制約。Zed のソースコード
（[`crates/bedrock/src/models.rs`](https://github.com/zed-industries/zed/blob/main/crates/bedrock/src/models.rs)、
GitHub main ブランチ・2026-07 取得）で裏付け:

1. **カスタムモデルはツールが一律無効** — `ConverseModel::supports_tool_use()` で `Custom { .. }` が
   true を返す分岐に含まれず `_ => false` に落ちる（「Tools Unsupported」表示の正体）。
   → `available_models` に Opus 4.8 ARN を登録しても、名前に関わらずツールは効かない。
2. **組み込みモデルの jp 対応表に Opus 系が漏れている** — `cross_region_inference_id()` の `"jp"` arm は
   `ClaudeSonnet4_6 | ClaudeSonnet4_5 | ClaudeHaiku4_5 | Nova2Lite` の4機種のみ。
   Opus 系（4.8/4.7/4.6/4.5/4.1）は jp arm にない（EU・豪州 arm にはある）。
3. **`allow_global` の分岐** — 同関数で `allow_global=true` かつ `supports_global` なモデル
   （Opus 4.8 含む）の場合、日本リージョンでも `region_group` が `"global"` に切り替わり
   `global.anthropic.claude-opus-4-8` にルーティングされる。これなら組み込みなのでツールは効くが、
   **`global.` は推論を日本国外に逃がすため国内完結要件と両立しない**。
   逆に `allow_global=false` だと jp arm に入らない Opus は `_ => model_id`（素の ID）に落ち、
   PoC の jp.限定 IAM ポリシー（[infra/main.tf](../infra/main.tf)）に拒否される。

> ※ mantle エンドポイント（`MantleModel`）の `Custom` には `supports_tools: bool` フィールドがあり設定可能だが、
> 本 PoC は国内完結統制の観点から mantle を IAM で拒否済み（[infra/main.tf](../infra/main.tf)）。この抜け道は使えない。

→ 使い分け: **エージェント作業は Claude Code CLI（Opus 4.8）**、**Zed 内では Sonnet 4.6 エージェント +
Opus 4.8 チャット**。

## 2. 設定

### settings.json

```jsonc
{
  "language_models": {
    "bedrock": {
      "authentication_method": "api_key",
      "region": "ap-northeast-1",   // 1.10.3 では効かないことがある → 下の環境変数が確実
      "available_models": [
        {
          // コスト配賦タグつきのアプリケーション推論プロファイル ARN を推奨
          // （Custom は name を無加工で送るため ARN も可。jp.anthropic.claude-opus-4-8 直指定でも動く）
          "name": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<PROFILE_ID>",
          "display_name": "社内: Claude Opus 4.8 (国内完結)",
          "max_tokens": 200000,
          "max_output_tokens": 32000
        }
      ]
    }
  }
}
```

### リージョン（重要・実測で踏んだ罠）

**Zed 1.10.3 は API キー認証時に settings.json の `region` を読まず、既定 us-east-1 になる**
（「model is not available in us-east-1」エラーの正体）。環境変数が最優先なので、これで固定する:

```bash
launchctl setenv ZED_AWS_REGION ap-northeast-1   # 即時有効（ただし Mac 再起動で消える）
```

**恒久化（macOS）**: ログイン時に上記を自動実行する LaunchAgent を置く。
`~/Library/LaunchAgents/com.example.zed-aws-region.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.zed-aws-region</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/launchctl</string>
    <string>setenv</string>
    <string>ZED_AWS_REGION</string>
    <string>ap-northeast-1</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.zed-aws-region.plist  # 初回のみ
launchctl getenv ZED_AWS_REGION   # ap-northeast-1 と出れば OK
```

不要になったら（Zed 側で settings の region が効くようになったら）:
`launchctl bootout gui/$(id -u)/com.example.zed-aws-region && rm ~/Library/LaunchAgents/com.example.zed-aws-region.plist`

**恒久化（Windows）**: ユーザー環境変数に設定すれば GUI 起動の Zed にも常に効く（再起動後も持続）:

```powershell
setx ZED_AWS_REGION ap-northeast-1
```

（または システムのプロパティ → 環境変数 → ユーザー環境変数に `ZED_AWS_REGION=ap-northeast-1` を追加。
設定後に Zed を再起動。削除は `[Environment]::SetEnvironmentVariable("ZED_AWS_REGION", $null, "User")`）

設定後は **Zed を完全終了（macOS: ⌘Q / Windows: ウィンドウ全閉じ後タスクトレイも終了）して再起動**。

### API キー

Settings → AI → LLM Providers → **Amazon Bedrock** → Bedrock API Key 欄に
**利用者ポータルの「現行キー本文」**を貼る（keychain に保管される。ポータルは Teams 通知の URL から開く）。
キーは毎週月曜 09:00 JST にローテーションされるため、通知が来たら **Reset Key → ポータルの新キーを貼り直す**。

## 3. 動作確認

1. モデルピッカー →「社内: Claude Opus 4.8 (国内完結)」→ チャットで質問
2. 組み込み「Claude Sonnet 4.6」→ エージェントタスク（ツールが有効なことを確認）
3. `jp.` 以外（組み込み Opus 等）を選ぶと AccessDenied になるのは**正常**（迂回防止が効いている）

## 4. 監査・コスト

- Zed の利用も CloudTrail に記録される（eventName は `ConverseStream`。`scripts/04-check-cloudtrail.sh` で
  `inferenceRegion` が ap-northeast-1/3 であることを確認できる）
- コスト配賦: カスタムモデル（上記 ARN 指定）の利用は `Project` タグで Cost Explorer 集計可能。
  **組み込み Sonnet 4.6（エージェント用）はシステムプロファイル直のためタグ配賦されない**（design.md §6）。

## 5. Windows での差分（未実測）

設定内容・制約は macOS と同一。OS 依存の差分だけ:

- **設定ファイル**: `~/.config/zed/settings.json` → `%APPDATA%\Zed\settings.json`（中身の
  `language_models.bedrock` は §2 と同一）
- **リージョン固定**: `setx ZED_AWS_REGION ap-northeast-1`（§2 に記載。ユーザー環境変数に入り GUI 起動の
  Zed にも常に効く。設定後は Zed を完全終了 = ウィンドウ全閉じ + タスクトレイからも終了 → 再起動）
- **API キー**: Settings → AI → LLM Providers → Amazon Bedrock の欄に貼る（Windows Credential Manager に保管）
- 「Opus 4.8 × 国内完結 × ツールが揃わない」Zed 固有の制約（§1）は OS 共通
