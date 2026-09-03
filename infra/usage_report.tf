# 週次利用状況レポート: EventBridge Scheduler（毎週月曜 09:30 JST）→ Lambda → Teams 投稿。
# トークン消費量は CloudWatch Metrics（AWS/Bedrock）、実コストは Cost Explorer（タグ配賦）から取得。
# 既存の Teams webhook（rotation.tf の SSM パラメータ）を再利用。

locals {
  # 利用者別（cc-<user>-<model>）アプリ推論プロファイル ID。
  # これらは Terraform 管理外（手動作成・メモリ 2026-08-02 の方式A）で、#11 以降エディタ/CLI の
  # 実利用はこの cc-* プロファイル経由に一本化されている（user タグ付きのみ invoke 許可）。
  # CloudWatch Metrics の ModelId ディメンションは、この cc-* プロファイル ID そのもので記録される
  # （実測 2026-08-10: lukfc2db79dy / pb395raosf0q / yv7220ge2aqs 等が出現）。
  #
  # ⚠️ このリストは「削除済みプロファイルの過去分を拾うためのフォールバック」。
  # 現存プロファイルは Lambda（report_usage.py）が実行時に app=claude-code で列挙して
  # 自動併合するため、ポータル増分の追記は不要（2026-09-03 から。sonnet 追加も同時に解決）。
  # 新規ユーザーをここに足す必要があるのは「作成→削除→過去分を遡って集計したい」場合のみ。
  cc_profile_ids = {
    opus = [
      "lukfc2db79dy", # takeshi.ohno
      "pb395raosf0q", # riku.ibaraki
      "yv7220ge2aqs", # takashi.kuwabara
      "rtf4lk9miwx2", # daisuke.kawashima
      "vi3bonbhiz4e", # yusuke.kobayashi
      "nhitr7ukojpv", # hiroyuki.eguchi
    ]
    sonnet = [
      "gybbubhb3gum", # takeshi.ohno
      "wcjv53mdszvv", # riku.ibaraki
      "9hrsookqweix", # takashi.kuwabara
      "ssv4daziy1sw", # daisuke.kawashima
      "m9lhprni8fhv", # yusuke.kobayashi
      "5djxutm106di", # hiroyuki.eguchi
    ]
    haiku = [
      "ps3qb3yiseyf", # takeshi.ohno
      "i148uog172qx", # riku.ibaraki
      "ag0coeargks9", # takashi.kuwabara
      "rndy9tr9pvj5", # daisuke.kawashima
      "d19cdw6v9vmp", # yusuke.kobayashi
      "p669eotr5ist", # hiroyuki.eguchi
    ]
  }

  # 概算費用算出用モデル定義。
  # - metric_ids は CloudWatch Metrics の ModelId ディメンションに実測で現れる値を並べる。
  #   Lambda が実行時に app=claude-code のプロファイルを列挙して model_tag 経由で自動併合するため、
  #   ここは過去分（jp. システム ID・旧 editor-claude-*-jp・削除済み cc-*）のフォールバック扱い。
  # - model_tag は動的併合の対応キー（cc-* プロファイルの model タグ値: opus / sonnet / haiku）。
  # - in_price/out_price は 1M トークンあたりの USD 単価（jp. +10% 込み）。未確定なら null で「単価未設定」表示。
  #   実測（2026-09-03、CE の USAGE_TYPE 別単価を CW トークン数で逆算。8/26=Opusのみの日で分離）:
  #     Opus 4.8   = $5.00/$25.00 ×1.1（従来 $6/$30 は誤りだったため訂正）
  #     Sonnet 4.6 = $3.00/$15.00 ×1.1
  #     Haiku 4.5  = $1.00/$5.00  ×1.1
  #   キャッシュ課金（書込 1.25× / 読取 0.1× 入力単価）は Lambda 側で自動計算。
  #   検証: 2026-08-24〜08-30 実績で概算 $291.17 = CE 実コスト $291.17（レポート時点）と一致。
  report_models = [
    {
      name      = "Opus 4.8"
      model_tag = "opus"
      metric_ids = concat([
        "jp.anthropic.claude-opus-4-8",
        aws_bedrock_inference_profile.editor["opus-4-8"].id,
      ], local.cc_profile_ids.opus)
      in_price  = 5.5  # AWS 料金表 $5.00 × jp.+10%（実測 2026-09-03）
      out_price = 27.5 # $25.00 × 1.1
    },
    {
      name      = "Sonnet 4.6"
      model_tag = "sonnet"
      metric_ids = concat([
        "jp.anthropic.claude-sonnet-4-6",
        aws_bedrock_inference_profile.editor["sonnet-4-6"].id,
      ], local.cc_profile_ids.sonnet)
      in_price  = 3.3  # $3.00 × 1.1（実測 2026-09-03）
      out_price = 16.5 # $15.00 × 1.1
    },
    {
      name      = "Haiku 4.5"
      model_tag = "haiku"
      metric_ids = concat([
        "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        aws_bedrock_inference_profile.editor["haiku-4-5"].id,
      ], local.cc_profile_ids.haiku)
      in_price  = 1.1 # $1.00 × 1.1（実測 2026-09-03）
      out_price = 5.5 # $5.00 × 1.1
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

  # metric_ids 動的併合用: app=claude-code のアプリ推論プロファイル列挙 + タグ読み取り
  # （profile_ui と同じパターン。ListInferenceProfiles はリソーススコープを取らない読み取り専用 API）
  statement {
    sid = "ListProfilesForMetrics"
    actions = [
      "bedrock:ListInferenceProfiles",
      "bedrock:ListTagsForResource",
    ]
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
  # メトリクス取得が 4 種 × モデル別 metric_ids（動的併合で増える）+ プロファイル列挙なので余裕を持つ
  timeout = 120

  environment {
    variables = {
      WEBHOOK_PARAM    = local.webhook_param_name
      METRIC_NAMESPACE = "AWS/Bedrock"
      # 実コストは利用者別内訳（app=claude-code）と同じフィルタに揃える。
      # 旧・共有プロファイル（editor-claude-*-jp）は Project タグのみ・実利用はほぼ無いため、
      # Project 基準だと利用者別合計と桁違いにずれる（実測 2026-08-10: Project=$13.84 / app=$124.56）。
      COST_TAG_KEY   = "app"
      COST_TAG_VALUE = "claude-code"
      # 利用者別内訳: app=claude-code を user タグでグループ化（setup-claude-code.md §0.5）
      USER_TAG_KEY       = "user"
      USER_APP_TAG_KEY   = "app"
      USER_APP_TAG_VALUE = "claude-code"
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
