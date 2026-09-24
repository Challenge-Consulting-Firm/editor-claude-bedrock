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

# global. 推論プロファイルを使うモデルの利用可否。
# Opus 5.5 のように jp. プロファイルが提供されたモデルは対象外で、
# allow_global_models = false でも国内モデルとして利用できる。
#
# ⚠️ true にすると「推論の国内完結」は global allowlist 対象モデルに限り成立しない。
# 実測（2026-09-16・東京エンドポイントから global. プロファイルを Converse）:
#   global.anthropic.claude-opus-5   -> inferenceRegion = eu-west-1（アイルランド）
#   global.anthropic.claude-sonnet-5 -> inferenceRegion = us-east-1（バージニア）
# Opus 5 / Sonnet 5 は jp. プロファイルが存在せず、素のモデル ID は on-demand 非対応
# （ValidationException: Retry with the ID or ARN of an inference profile）、
# 東京の foundation-model ARN からのアプリ推論プロファイル作成も不可
# （ValidationException: does not support On Demand inference）。
# よってこれらを東京リージョンに固定して使う手段は現時点で存在しない。
#
# 運用判断（2026-09-24）: **既定を false（= 国内完結のみ）に戻した**。
# Opus 5.5 が jp.anthropic.claude-opus-5-5（東京+大阪）で提供され、
# 「国内完結 vs 最新モデル」のトレードオフが解消されたため、
# 国外ルーティングを許容する理由がなくなった（Opus 5 は廃止）。
# 将来再び jp. 未提供の最新モデルを使う必要が生じたときだけ、
# 明示的に true へ戻して allowlist を見直すこと。
variable "allow_global_models" {
  description = "global. システム推論プロファイルを使うモデルの利用を許可するか。既定 false = 国内完結のみ。true にすると allowlist 対象モデルの推論は国外（実測: eu-west-1 / us-east-1）で行われる。jp. 対応の Opus 5.5 は対象外"
  type        = bool
  default     = false
}

# ⚠️ 意図的に「明示列挙」にしている。global.* のワイルドカード許可にすると
# 同じ接頭辞の他ベンダーモデル（global.openai.* / global.xai.* 等・実測で東京に存在）まで
# 一括開放され統制の穴になるため。モデルを増やすときは明示的に足すこと。
#
# 2026-09-24: Opus 5 の廃止に伴い空リストにした。allow_global_models と二重の安全弁になる
# （フラグを誤って true に戻しても、allowlist が空なら global 経路は生成されない）。
variable "global_model_profile_ids" {
  description = "allow_global_models = true のときに許可する global. システム推論プロファイル ID の allowlist。既定は空（Opus 5 廃止済み）"
  type        = list(string)
  default     = []

  validation {
    condition = (
      length(var.global_model_profile_ids) == length(toset(var.global_model_profile_ids)) &&
      alltrue([
        for id in var.global_model_profile_ids :
        can(regex("^global\\.anthropic\\.claude-[a-z0-9][a-z0-9-]*$", id))
      ])
    )
    error_message = "global_model_profile_ids は重複のない global.anthropic.claude-<model> 形式で指定してください。"
  }
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

  # ---- global. 利用モデルの許可セット。allow_global_models = false なら全て空リストになり、
  #      ポリシー上も statement ごと生成されない（Opus 5.5 等の国内モデルには影響しない）----
  global_enabled = var.allow_global_models && length(var.global_model_profile_ids) > 0

  # 呼び出し元 IAM には付与せず、profile_ui が per-user アプリ推論プロファイルを
  # 作成するときのコピー元としてだけ使う。直接 invoke を許すと user タグを経由せず、
  # 利用者別の棚卸しを迂回できるため。
  global_profile_arns = local.global_enabled ? [
    for id in var.global_model_profile_ids :
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${id}"
  ] : []

  # global. プロファイルがルーティングする先の foundation-model ARN。
  # ⚠️ 実測（2026-09-16 get-inference-profile）: global. プロファイルの models[] には
  #    リージョン無し ARN（arn:aws:bedrock:::foundation-model/<model>）と東京 ARN の両方が含まれる。
  #    前者が「世界中のどのリージョンで処理してもよい」の実体。
  global_model_ids = local.global_enabled ? [
    for id in var.global_model_profile_ids : trimprefix(id, "global.")
  ] : []

  # region の * は globalプロファイルが返す「region無しARN」と実リージョンARNの両方に一致する。
  global_foundation_model_arns = [
    for model in local.global_model_ids :
    "arn:aws:bedrock:*::foundation-model/${model}"
  ]

  global_application_profile_arns = local.global_enabled ? [
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
  ] : []

  # 「国内リージョン以外への推論を Deny」から除外するのは、per-userアプリプロファイルと
  # allowlistモデルの foundation-model ARN だけ。globalシステムプロファイルは含めない。
  # 内部ルーティング時に aws:RequestedRegion が国外として再評価されてもper-user経路は通し、
  # `global.anthropic...` の直指定は引き続きDenyする。
  global_deny_exempt_arns = concat(
    local.global_application_profile_arns,
    local.global_foundation_model_arns,
  )
}
