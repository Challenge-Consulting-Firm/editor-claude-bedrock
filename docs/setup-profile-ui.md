# 利用者プロファイル管理 Web UI（EntraID 認証）

利用者ごとのコスト配賦用アプリケーション推論プロファイル（`cc-<user>-opus` /
`cc-<user>-haiku`。タグ `user` / `app=claude-code` / `model` 付き）を、ブラウザから
**表示・作成・削除**するための管理 UI。従来 [setup-claude-code.md](setup-claude-code.md) §0.5 の
AWS CLI 手動作成だった運用を置き換える（列挙・タグ規約は [lambda/rotate_key.py](../lambda/rotate_key.py) と同一）。

## 構成

```
  ブラウザ (MSAL.js)                        AWS（東京）
  ── EntraID サインイン ──▶ login.microsoftonline.com
        │ アクセストークン(JWT)
        ▼
  API Gateway (HTTP API・$default) ──▶ Lambda editor-claude-bedrock-profile-ui
    ├ GET  /            SPA(HTML) を返す（無認証）
    ├ GET  /api/config  MSAL 用の tenantId/clientId（無認証）
    └ /api/profiles     GET/POST/DELETE。Authorization: Bearer <JWT> 必須
         │ entra_auth が JWKS 署名検証（tid=テナント一致で認可）
         ▼
      bedrock:CreateInferenceProfile / DeleteInferenceProfile / ListInferenceProfiles
```

- **認証**: SPA が EntraID からアクセストークンを取得し、API 呼び出しの `Authorization: Bearer` で送る。
  Lambda（[lambda/entra_auth.py](../lambda/entra_auth.py)）が Entra の JWKS 公開鍵で RS256 署名を検証し、
  `iss` / `aud` / `exp` / `nbf` / `tid` を確認する。**認可は「同一テナントのサインインユーザー全員」**
  （`tid` 一致）。特定グループに絞りたくなったら claims の `groups` / `roles` を追加検証する
- **公開経路は API Gateway HTTP API**。API Gateway 側では認証をかけず（HTTP API の JWT オーソライザは
  ステージ全体に効き HTML/`/api/config` の無認証配信ができないため）、認可は Lambda 内トークン検証に一本化。
  HTML と `/api/config` のみ無認証で返す（`/api/config` はテナント ID とクライアント ID の公開値のみ）
- ⚠️ **実測（2026-08-05）で判明**: 当初 Lambda Function URL（authtype=NONE）で作ったが、この
  アカウント/組織では**匿名 Function URL が一律 403（AccessDeniedException）でブロック**される
  （リソースポリシー・SCP/RCP・ネットワークいずれも問題なし。AuthType=AWS_IAM + SigV4 なら通ることで
  切り分け済み）。SigV4 はブラウザに置けないため、パブリック公開が既定の HTTP API に切り替えた
  （[infra/profile_ui.tf](../infra/profile_ui.tf)）。README の「書類上できるはずを信用しない」教訓どおりの実測ずれ
- JWT 検証は**標準ライブラリのみ**で実装（既存 Lambda と同じく依存ゼロ。PyJWT/cryptography の
  ネイティブ依存クロスビルドを避けるため）

## 1. EntraID アプリ登録（SPA）

Microsoft Entra 管理センター → **アプリの登録** → **新規登録**:

1. 名前: 任意（例 `editor-claude-profile-ui`）
2. サポートされるアカウントの種類: **この組織ディレクトリのみ**（シングルテナント）
3. リダイレクト URI: **必ずプラットフォーム「シングルページ アプリケーション (SPA)」で登録する**
   （「Web」ではない — 後述の実測落とし穴を参照）。URI は `terraform apply` 後の `profile_ui_url`。
   初回は URL 未確定なので後で登録・更新してよい。**末尾スラッシュの有無まで一致**させること
   （本 UI の SPA は `redirectUri = origin + pathname`。ルートアクセスなら `https://.../` の形）
4. 登録後、**概要**の以下を控える:
   - アプリケーション (クライアント) ID → `.env` の `ENTRA_CLIENT_ID`
   - ディレクトリ (テナント) ID → `.env` の `ENTRA_TENANT_ID`

> ⚠️ **リダイレクト URI は「SPA」プラットフォームで登録する（実測 2026-08-05 で 2 回踏んだ）**:
> - 未登録だと `AADSTS500113: No reply address is registered for the application.`
> - 「Web」プラットフォームで登録すると、サインインは通るがトークン交換で
>   `AADSTS9002326: Cross-origin token redemption is permitted only for the 'Single-Page Application' client-type.`
>   になる。MSAL.js は PKCE + `Origin` ヘッダで交換するため、**SPA 型でないと拒否される**。
>   アプリ マニフェストの `replyUrlsWithType` が当該 URL で `"type": "Spa"` になっていること
>   （ポータルで「SPA プラットフォーム」から追加すれば自動でそうなる）

### API の公開（アクセススコープ）

SPA はスコープ `api://<client_id>/access_as_user` でトークンを要求する（[profile_ui.py](../lambda/profile_ui.py) の JS）:

1. **API の公開** → アプリケーション ID の URI（既定 `api://<client_id>`）を設定
2. **スコープの追加** → `access_as_user`（同意できるのは管理者とユーザー、任意の表示名で可）
3. **API のアクセス許可** → 自分の API の `access_as_user` を追加し、必要なら「管理者の同意」を付与

> - Lambda 側は `aud` を `<client_id>` と `api://<client_id>` の両方許容するので、
>   アクセストークンの `aud` がどちらの形式でも通る。
> - **iss も v2.0（`login.microsoftonline.com/<tid>/v2.0`）と v1.0（`sts.windows.net/<tid>/`）の
>   両形式を許容する**（実測 2026-08-05）。カスタム API 向けアクセストークンは
>   `accessTokenAcceptedVersion` が既定（null=v1）だと **v1.0 形式の iss** で発行されるため。
>   v2.0 に固定したい場合はマニフェストで `accessTokenAcceptedVersion: 2` にできるが、
>   本 UI は設定に依存せず動くよう両対応にしてある。認可の実体は `tid`（テナント）一致

## 2. デプロイ

`.env` に EntraID の値を設定して通常どおりデプロイする（[README](../README.md) の手順）:

```bash
# .env（.gitignore 済み）に追記
ENTRA_TENANT_ID=<ディレクトリ (テナント) ID>
ENTRA_CLIENT_ID=<アプリケーション (クライアント) ID>

./scripts/deploy.sh
```

apply 後、出力 `profile_ui_url`（API Gateway の URL）が UI の URL。
**この URL を手順 1-3 の「SPA」リダイレクト URI に登録**（更新）する。

## 3. 使い方

1. `profile_ui_url` をブラウザで開く → 「EntraID でサインイン」
2. 一覧に既存のプロファイルが利用者別に表示される（`app=claude-code` タグのもの）
3. 利用者名（例 `takeshi.ohno`。IAM ユーザー名／`user` タグに合わせる）を入れて **作成**
   → Opus 4.8 と Haiku 4.5 の 2 本がタグ付きで作られる（既存分はスキップ＝冪等）
4. 行の **削除** で当該利用者の `app=claude-code` プロファイルを全削除

作成された ARN は従来どおり週次キーローテ通知（Teams）に利用者別対応表として同梱される
（[rotate_key.py](../lambda/rotate_key.py) が同じタグで列挙）。

## 注意

- **コスト配分タグの有効化**は別途必要（`user` タグ。[setup-claude-code.md](setup-claude-code.md) §0.5 の手順）。
  UI はプロファイルを作るだけで、Billing 側のタグ有効化は行わない
- Opus 5 の `jp.`（国内完結）プロファイルは未提供のため、コピー元は Opus 4.8 のまま
  （[profile_ui.py](../lambda/profile_ui.py) の `MODEL_SOURCES`。jp. 提供後に差し替える）
- **未実測**: 本 UI は 2026-08 追加分で、エディタ疎通（検証 3）のような実機確認は未実施。
  Entra アプリ登録・Function URL の CORS 挙動は初回デプロイ時に要確認
