# VS Code セットアップ（検証 3・2026-07-14 時点）

VS Code から Bedrock の jp. プロファイル（国内完結）を使う経路の整理。

| 方法 | 判定 |
|---|---|
| **A. Claude Code 公式拡張**（推奨・チーム方針と一致） | ⏳ **実測中** — Bearer キーで `editor-claude-poc` として推論が通ることまで CloudTrail で確認済み。エージェントタスクの完走確認が残り |
| B. Continue 拡張（`provider: bedrock`、SigV4） | 未実測 — API キー配布運用に乗らない（アクセスキーが別途必要）ため優先度低 |
| C. GitHub Copilot（BYOK） | ❌ 不可 — Copilot の「言語モデル」プロバイダ一覧に Bedrock は存在しない |

## 方法A: Claude Code 拡張

1. 拡張 `anthropic.claude-code` をインストール（Marketplace で「Claude Code」）
2. **ワークスペース側**の `.claude/settings.json` に接続先を書く
   （ユーザーグローバル `~/.claude/settings.json` に書くと**全プロジェクトが Bedrock 行きになる**ので注意）:

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "ap-northeast-1",
    "ANTHROPIC_MODEL": "arn:aws:bedrock:ap-northeast-1:<ACCOUNT_ID>:application-inference-profile/<PROFILE_ID>",
    "ANTHROPIC_SMALL_FAST_MODEL": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
```

3. キー（利用者ポータルの「現行キー本文」。Teams 通知の URL から開く）は**環境変数で渡す**。VS Code は GUI 起動だとシェル環境を継がないため、
   **完全終了してからターミナルで起動**する:

```bash
AWS_BEARER_TOKEN_BEDROCK='<キー>' code -n <ワークスペース>
```

> 恒久化は Zed と同じ考え方（[setup-zed.md](setup-zed.md) の LaunchAgent / Windows `setx` を
> `AWS_BEARER_TOKEN_BEDROCK` に読み替え）。ただしキーを OS 全体の環境変数に置くことになるため、
> 本番展開時はチームでリスク許容を判断すること。

4. サイドバーの **Claude Code アイコン**からチャット/エージェントを利用（Copilot の言語モデル一覧には出ない）

## 実測メモ（罠）

- `code` コマンドが **Cursor に奪われている環境がある**（`/usr/local/bin/code` が Cursor へのリンク）。
  その場合は VS Code 同梱 CLI をフルパスで:
  `"/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"`
- Cursor でも同拡張は動く見込み（VS Code フォークのため）。組織で Cursor 利用者がいる場合は同手順を流用可

## Windows での差分（未実測）

考え方は同じ。OS 依存の差分だけ:

- **設定ファイル**: ワークスペース側 `.claude\settings.json`（中身は上記 JSON と同一）
- **キーの渡し方**: VS Code は GUI 起動でもユーザー環境変数を継ぐため、macOS のような
  「完全終了 → ターミナルから起動」の縛りはない。恒久化するなら PowerShell で
  `setx ANTHROPIC_MODEL "jp.anthropic.claude-opus-4-8"` 等。**ただしキー
  （`AWS_BEARER_TOKEN_BEDROCK`）は `setx` で OS に恒久化しない**（週次ローテの秘密が全プロセスから
  読める）。セッション限定で渡すなら PowerShell から:

  ```powershell
  $env:AWS_BEARER_TOKEN_BEDROCK = "<キー>"; code -n <ワークスペース>
  ```

- **`code` が PATH にない**場合: コマンドパレット →「Shell Command: Install 'code' command in PATH」、
  または `"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"` をフルパス指定
