# 週次利用状況レポート: EventBridge Scheduler（毎週月曜 09:30 JST）→ Lambda → Teams 投稿。
# トークン消費量は CloudWatch Metrics（AWS/Bedrock）、実コストは Cost Explorer（タグ配賦）から取得。
# 既存の Teams webhook（rotation.tf の SSM パラメータ）を再利用。

locals {
  # 概算費用算出用モデル定義。
  # metric_ids は CloudWatch Metrics の ModelId ディメンションに実測で現れた値を並べる。
  # 実測（2026-07-15）で、エディタがアプリ推論プロファイル ARN 経由で呼んだ場合のトークンは
  # jp. システムプロファイル ID ではなく「アプリ推論プロファイル ID（ランダム文字列）」で記録される
  # （例: Opus の 283k トークンは jp.anthropic.claude-opus-4-8=24k / app-profile=axtyxbjqdms4=283k）。
  # 両方を並べて合算しないと実利用の大部分を取りこぼすため、システムID + アプリプロファイルID の2つを指定する。
  # アプリプロファイル ID は aws_bedrock_inference_profile リソース属性から動的に参照（ハードコードしない）。
  # in_price/out_price は 1M トークンあたりの USD 単価（jp. +10% 込み）。未確定なら null で「単価未設定」表示。
  report_models = [
    {
      name = "Opus 4.8"
      metric_ids = [
        "jp.anthropic.claude-opus-4-8",
        aws_bedrock_inference_profile.editor["opus-4-8"].id,
      ]
      in_price  = 6.6  # AWS 料金表（2026-07）$6.00 × jp.+10%
      out_price = 33.0 # $30.00 × 1.1
    },
    {
      name = "Sonnet 4.6"
      metric_ids = [
        "jp.anthropic.claude-sonnet-4-6",
        aws_bedrock_inference_profile.editor["sonnet-4-6"].id,
      ]
      in_price  = null # AWS 料金表の Anthropic アコーディオンに公開行がなく未取得。コンソール料金タブで実数を確認して設定
      out_price = null
    },
    {
      name = "Haiku 4.5"
      metric_ids = [
        "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        aws_bedrock_inference_profile.editor["haiku-4-5"].id,
      ]
      in_price  = null # 同上
      out_price = null
    },
  ]
}

resource "aws_iam_role" "report_usage" {
  name = "editor-claude-bedrock-report-usage"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "report_usage" {
  # CloudWatch Metrics はアカウントスコープで細粒度 ARN がないため *
  statement {
    sid       = "ReadBedrockMetrics"
    actions   = ["cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadCostExplorer"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
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

resource "aws_iam_role_policy" "report_usage" {
  name   = "report-usage"
  role   = aws_iam_role.report_usage.id
  policy = data.aws_iam_policy_document.report_usage.json
}

resource "aws_lambda_function" "report_usage" {
  function_name    = "editor-claude-bedrock-report-usage"
  role             = aws_iam_role.report_usage.arn
  runtime          = "python3.12"
  handler          = "report_usage.handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60

  environment {
    variables = {
      WEBHOOK_PARAM      = local.webhook_param_name
      METRIC_NAMESPACE   = "AWS/Bedrock"
      COST_TAG_KEY       = "Project"
      COST_TAG_VALUE     = "editor-claude-bedrock"
      REPORT_DAYS        = "7"
      MONTHLY_BUDGET_USD = tostring(var.monthly_budget_usd)
      MODELS_JSON        = jsonencode(local.report_models)
    }
  }
}

# Scheduler → Lambda 起動用ロール
resource "aws_iam_role" "report_usage_scheduler" {
  name = "editor-claude-bedrock-report-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "report_usage_scheduler" {
  name = "invoke-report-usage"
  role = aws_iam_role.report_usage_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.report_usage.arn
    }]
  })
}

# 毎週月曜 09:30 JST（キーローテ 09:00 と被せない。失敗時は Lambda 側でリトライされる）
resource "aws_scheduler_schedule" "weekly_usage_report" {
  name                         = "editor-claude-bedrock-weekly-usage-report"
  schedule_expression          = "cron(30 9 ? * MON *)"
  schedule_expression_timezone = "Asia/Tokyo"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.report_usage.arn
    role_arn = aws_iam_role.report_usage_scheduler.arn
  }
}
