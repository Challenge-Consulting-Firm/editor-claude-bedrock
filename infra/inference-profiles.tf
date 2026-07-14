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
  # 実測済み（2026-07-14, scripts/01）の jp. システムプロファイル
  app_profile_models = {
    opus-4-8   = "jp.anthropic.claude-opus-4-8"
    sonnet-4-6 = "jp.anthropic.claude-sonnet-4-6"
    haiku-4-5  = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}

resource "aws_bedrock_inference_profile" "editor" {
  for_each = local.app_profile_models

  name        = "editor-claude-${each.key}-jp"
  # description は ASCII のみ許可（日本語を入れると ValidationException — 実測 2026-07-14）
  description = "Cost-allocation profile for editor use of ${each.value} with Japan-resident inference"

  model_source {
    copy_from = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${each.value}"
  }
}

output "application_inference_profile_arns" {
  description = "エディタ/CLI の model に指定する ARN（コスト配賦つき）"
  value       = { for k, v in aws_bedrock_inference_profile.editor : k => v.arn }
}
