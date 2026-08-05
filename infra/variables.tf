variable "aws_region" {
  description = "呼び出し元エンドポイントのリージョン。jp. プロファイルの前提で東京固定"
  type        = string
  default     = "ap-northeast-1"
}

variable "poc_user_name" {
  description = "PoC 用 IAM ユーザー名（エディタ利用者を模す。Bedrock API キーはこのユーザーに発行する）"
  type        = string
  default     = "editor-claude-poc"
}

variable "monthly_budget_usd" {
  description = "月次予算（USD）。50/75/90% でソフト通知"
  type        = number
  default     = 200
}

variable "ops_email" {
  description = "Budget 通知の宛先メール"
  type        = string
}

variable "teams_webhook_url" {
  description = "Teams Workflows（Power Automate「Webhook 要求を受信したとき」）の URL。キーローテ通知の投稿先。秘密"
  type        = string
  sensitive   = true
}

variable "rotation_key_age_days" {
  description = "ローテーションで発行するキーの有効期限（日）。週次ローテ + 1 週間の旧キー猶予 + バッファ"
  type        = number
  default     = 15
}

variable "entra_tenant_id" {
  description = "EntraID テナント ID（profile_ui の JWT 検証と MSAL の authority に使う）"
  type        = string
}

variable "entra_client_id" {
  description = "EntraID に登録した SPA アプリの（アプリケーション）クライアント ID。profile_ui の aud 検証と MSAL の clientId に使う"
  type        = string
}

variable "allowed_ips" {
  description = "任意: 接続元グローバル IP の allowlist（CIDR）。空なら IP 制限なし（PoC 既定）。本番化では必須（Azure 版 R2 相当）"
  type        = list(string)
  default     = []
}

variable "openai_compat_region" {
  description = "OpenAI 互換エンドポイントのリージョン。実測（2026-07-14）で東京の /openai/v1 は jp. プロファイルを解決せず、大阪は解決するため既定は大阪"
  type        = string
  default     = "ap-northeast-3"
}

# jp. プロファイル経由の推論だけを許可するための識別子。
# ID そのもの（バージョン日付等）は実測で変わり得るため、ワイルドカードで「jp. で始まる」ことだけを固定する
locals {
  # jp. プロファイルの推論先（実測: 東京 + 大阪）。foundation-model ARN のリージョン部と
  # 呼び出しを受け付けるエンドポイントの範囲を制限する
  jp_inference_regions = ["ap-northeast-1", "ap-northeast-3"]

  # プロファイルはリージョンごとの account スコープ ARN を持つ（実測: 大阪エンドポイント経由の
  # 呼び出しは ap-northeast-3 の profile ARN で IAM 評価される）ため、両リージョン分を許可する
  jp_profile_arn_patterns = [
    for r in local.jp_inference_regions :
    "arn:aws:bedrock:${r}:${data.aws_caller_identity.current.account_id}:inference-profile/jp.*"
  ]
}
