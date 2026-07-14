# R1: 週次キーローテーション（Azure 版 Functions rotate_key の移植）。
# EventBridge Scheduler（毎週月曜 09:00 JST）→ Lambda → 新キー発行 + SSM 保管 + Teams 投稿。

locals {
  api_key_param_name = "/editor-claude-bedrock/api-key"
  webhook_param_name = "/editor-claude-bedrock/teams-webhook-url"
}

# Teams Workflows の webhook URL（秘密）。Key Vault の teams-webhook-url 相当
resource "aws_ssm_parameter" "teams_webhook" {
  name  = local.webhook_param_name
  type  = "SecureString"
  value = var.teams_webhook_url
}

data "archive_file" "rotate_key" {
  type        = "zip"
  source_file = "${path.module}/../lambda/rotate_key.py"
  output_path = "${path.module}/rotate_key.zip"
}

resource "aws_iam_role" "rotate_key" {
  name = "editor-claude-bedrock-rotate-key"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "rotate_key" {
  statement {
    sid = "ManagePocUserBedrockApiKeys"
    actions = [
      "iam:ListServiceSpecificCredentials",
      "iam:CreateServiceSpecificCredential",
      "iam:DeleteServiceSpecificCredential",
    ]
    resources = [aws_iam_user.poc.arn]
  }
  statement {
    sid = "StoreApiKey"
    # GetParameter は notify_only モード（現行キーの再案内投稿）が使う
    actions   = ["ssm:PutParameter", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.api_key_param_name}"]
  }
  statement {
    sid       = "ReadWebhookUrl"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.teams_webhook.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "rotate_key" {
  name   = "rotate-key"
  role   = aws_iam_role.rotate_key.id
  policy = data.aws_iam_policy_document.rotate_key.json
}

resource "aws_lambda_function" "rotate_key" {
  function_name    = "editor-claude-bedrock-rotate-key"
  role             = aws_iam_role.rotate_key.arn
  runtime          = "python3.12"
  handler          = "rotate_key.handler"
  filename         = data.archive_file.rotate_key.output_path
  source_code_hash = data.archive_file.rotate_key.output_base64sha256
  timeout          = 60

  environment {
    variables = {
      POC_USER_NAME = aws_iam_user.poc.name
      KEY_AGE_DAYS  = tostring(var.rotation_key_age_days)
      API_KEY_PARAM = local.api_key_param_name
      WEBHOOK_PARAM = local.webhook_param_name
      # Teams 投稿に同梱する利用者向け設定値（キーと違い不変。コピペで設定完了できるように）
      OPUS_PROFILE_ARN     = aws_bedrock_inference_profile.editor["opus-4-8"].arn
      SONNET_PROFILE_ARN   = aws_bedrock_inference_profile.editor["sonnet-4-6"].arn
      HAIKU_MODEL_ID       = local.app_profile_models["haiku-4-5"]
      AWS_REGION_FOR_USERS = var.aws_region
    }
  }
}

# Scheduler → Lambda 起動用ロール
resource "aws_iam_role" "scheduler" {
  name = "editor-claude-bedrock-rotate-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-rotate-key"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.rotate_key.arn
    }]
  })
}

# 毎週月曜 09:00 JST（Azure 版と同時刻）。失敗時は Lambda 側で最大 2 回リトライされる
resource "aws_scheduler_schedule" "weekly_rotation" {
  name                         = "editor-claude-bedrock-weekly-rotation"
  schedule_expression          = "cron(0 9 ? * MON *)"
  schedule_expression_timezone = "Asia/Tokyo"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.rotate_key.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
