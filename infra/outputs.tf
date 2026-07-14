output "poc_user_name" {
  description = "PoC 用 IAM ユーザー（scripts/10-issue-api-key.sh が API キーを発行する対象）"
  value       = aws_iam_user.poc.name
}

output "openai_compat_base_url" {
  description = "OpenAI 互換エンドポイント（エディタに設定する api_url）。実測により大阪が既定（東京は jp. プロファイル未マッピング）"
  value       = "https://bedrock-runtime.${var.openai_compat_region}.amazonaws.com/openai/v1"
}

output "jp_profile_arn_patterns" {
  description = "IAM で許可している推論プロファイルの ARN パターン"
  value       = local.jp_profile_arn_patterns
}
