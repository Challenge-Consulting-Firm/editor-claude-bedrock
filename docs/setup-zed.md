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
| エージェント × Opus 4.8 | — | ❌ 不可（下記の 2 制約の合わせ技） |

Opus 4.8 エージェントが不可な理由（Zed 側の制約 2 つ）:
1. カスタムモデル（`available_models`）は Zed がツール使用を一律無効にする（「Tools Unsupported」表示）
2. 組み込みモデルの jp 対応表に Opus 系が入っていない（EU・豪州にはあるのに。`crates/bedrock/src/models.rs` の
   jp match arm は Sonnet 4.6/4.5・Haiku 4.5・Nova 2 Lite のみ）→ upstream 修正候補

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
**Teams のローテ通知のキー**を貼る（keychain に保管される）。
キーは毎週月曜 09:00 JST にローテーションされるため、通知が来たら **Reset Key → 新キーを貼り直す**。

## 3. 動作確認

1. モデルピッカー →「社内: Claude Opus 4.8 (国内完結)」→ チャットで質問
2. 組み込み「Claude Sonnet 4.6」→ エージェントタスク（ツールが有効なことを確認）
3. `jp.` 以外（組み込み Opus 等）を選ぶと AccessDenied になるのは**正常**（迂回防止が効いている）

## 4. 監査・コスト

- Zed の利用も CloudTrail に記録される（eventName は `ConverseStream`。`scripts/04-check-cloudtrail.sh` で
  `inferenceRegion` が ap-northeast-1/3 であることを確認できる）
- コスト配賦: カスタムモデル（上記 ARN 指定）の利用は `Project` タグで Cost Explorer 集計可能。
  **組み込み Sonnet 4.6（エージェント用）はシステムプロファイル直のためタグ配賦されない**（design.md §6）。
