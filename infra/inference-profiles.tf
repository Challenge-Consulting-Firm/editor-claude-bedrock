# コスト可視化: タグ付きアプリケーション推論プロファイル。
#
# Bedrock のオンデマンド推論はリソース非依存の課金のため、リソースタグだけでは
# 推論コストを配賦できない。システムの jp. プロファイルを複製した
# 「アプリケーション推論プロファイル」にタグを付け、利用者はその ARN 経由で呼ぶことで、
# Cost Explorer でタグ別（Project 等）に推論コストを集計できるようにする。
# プロファイル自体は無償。jp. の +10% プレミアムや推論先（東京+大阪）は元プロファイルを継承する。
#
# タグは provider の default_tags（Project / Phase / ManagedBy）が自動適用される。
# 用途別に配賦を分けたくなったら、この resource を用途分コピーして tags を追加する。

locals {
  # 共有プロファイルの Terraform リソースキー（出力との後方互換のため版を含める）。
  app_profile_models_jp = {
    opus-4-8   = "jp.anthropic.claude-opus-4-8"
    sonnet-4-6 = "jp.anthropic.claude-sonnet-4-6"
    haiku-4-5  = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
  }

  # per-user プロファイルの model タグは既存運用との互換性を維持する。
  # ここを opus-4-8 等へ変えると、既存 opus/sonnet/haiku を別モデルと誤認して
  # 重複作成し、report_usage.py の動的集計からも漏れる。
  user_profile_models_jp = {
    opus   = local.app_profile_models_jp["opus-4-8"]
    sonnet = local.app_profile_models_jp["sonnet-4-6"]
    haiku  = local.app_profile_models_jp["haiku-4-5"]
  }

  # Claude global プロファイル。既定は Opus 5 のみ。⚠️ 国内完結ではない
  # （実測 2026-09-16: opus-5 -> eu-west-1）。
  # globalモデルは jp. プロファイルが存在せず、東京の foundation-model ARN からの
  # アプリ推論プロファイル作成も不可（On Demand 非対応）なため、
  # copy_from は global. プロファイルを指すしかない。
  # 実測済み: global. から複製したアプリプロファイルでも invoke は成功し、
  # CloudWatch の ModelId ディメンションにプロファイル ID が記録される
  # （= タグ配賦と棚卸しは 4.x と同じ仕組みで成立する）。
  app_profile_models_global = var.allow_global_models ? {
    for id in var.global_model_profile_ids :
    trimprefix(id, "global.anthropic.claude-") => id
  } : {}

  # 共有プロファイルは既存の国内3モデルだけを Terraform 管理する。
  # globalモデルの共有プロファイルを作ると per-user ARN と取り違えて棚卸しを迂回し得るため、
  # profile_ui が作る user タグ付き cc-<user>-* のみに限定する。
  app_profile_models = local.app_profile_models_jp

  # プロファイル名のサフィックス（共有プロファイルは全て国内完結）。
  app_profile_suffix = {
    for k in keys(local.app_profile_models_jp) : k => "jp"
  }

  # profile_ui に渡す per-user 作成対象。既存3モデル + 有効化された5系。
  user_profile_models = merge(local.user_profile_models_jp, local.app_profile_models_global)
}

resource "aws_bedrock_inference_profile" "editor" {
  for_each = local.app_profile_models

  name = "editor-claude-${each.key}-${local.app_profile_suffix[each.key]}"
  # description は ASCII のみ許可（日本語を入れると ValidationException — 実測 2026-07-14）
  description = local.app_profile_suffix[each.key] == "jp" ? "Cost-allocation profile for editor use of ${each.value} with Japan-resident inference" : "Cost-allocation profile for editor use of ${each.value} (GLOBAL routing - inference may leave Japan)"

  model_source {
    copy_from = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${each.value}"
  }
}

output "application_inference_profile_arns" {
  description = "エディタ/CLI の model に指定する ARN（コスト配賦つき）"
  value       = { for k, v in aws_bedrock_inference_profile.editor : k => v.arn }
}
