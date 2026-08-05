# 利用者プロファイル管理 Web UI（Lambda Function URL + EntraID 認証）。
#
# 「ユーザプロファイル」= 利用者ごとのコスト配賦用アプリケーション推論プロファイル
# （cc-<user>-opus / cc-<user>-haiku。docs/setup-claude-code.md §0.5 を Web UI 化）。
#
# 構成（design.md の最小構成方針）:
#   - Lambda 1 本が HTML(SPA) と JSON API の両方を Function URL 直で配信（API Gateway 不要）
#   - 認証は EntraID: ブラウザ(MSAL.js)のアクセストークンを Lambda が JWKS 署名検証（tid 一致）
#   - Bedrock 操作は rotate_key と同じタグ規約（user / app=claude-code / model）
#
# ⚠️ Function URL は authtype=NONE（=誰でも URL には到達できる）。実際の認可は Lambda 内の
#    EntraID トークン検証で行う。API 群はトークン必須、SPA/HTML と /api/config のみ無認証で返す。

# ソースは rotation.tf の archive_file.lambda_zip（lambda/ ディレクトリ全体）を共用する。

resource "aws_iam_role" "profile_ui" {
  name = "editor-claude-bedrock-profile-ui"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "profile_ui" {
  # プロファイルの列挙・タグ読み取り（一覧表示）
  statement {
    sid = "ListProfiles"
    actions = [
      "bedrock:ListInferenceProfiles",
      "bedrock:GetInferenceProfile",
      "bedrock:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # 作成: コピー元は jp. システムプロファイル、作成先は application-inference-profile。
  # TagResource はタグ付き作成に付随して必要。
  statement {
    sid = "CreateProfiles"
    actions = [
      "bedrock:CreateInferenceProfile",
      "bedrock:TagResource",
    ]
    resources = [
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/jp.*",
    ]
  }

  # 削除: 作成したアプリケーション推論プロファイルのみ
  statement {
    sid       = "DeleteProfiles"
    actions   = ["bedrock:DeleteInferenceProfile"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
  }

  # account_id 解決用（profile_ui.py の sts:GetCallerIdentity）
  statement {
    sid       = "WhoAmI"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "profile_ui" {
  name   = "profile-ui"
  role   = aws_iam_role.profile_ui.id
  policy = data.aws_iam_policy_document.profile_ui.json
}

resource "aws_lambda_function" "profile_ui" {
  function_name    = "editor-claude-bedrock-profile-ui"
  role             = aws_iam_role.profile_ui.arn
  runtime          = "python3.12"
  handler          = "profile_ui.handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      ENTRA_TENANT_ID      = var.entra_tenant_id
      ENTRA_CLIENT_ID      = var.entra_client_id
      USER_PROFILE_APP_TAG = "claude-code"
    }
  }
}

# 公開経路は API Gateway HTTP API 経由。
# ⚠️ 実測（2026-08-05）: Lambda Function URL の authtype=NONE（匿名パブリック）は
#    このアカウント/組織で一律 403（AccessDeniedException）にブロックされる。
#    リソースポリシー・SCP/RCP・ネットワークいずれも問題なく、AuthType=AWS_IAM+SigV4 なら
#    通るため「匿名 Function URL の禁止」と切り分け済み。SigV4 はブラウザに置けないので、
#    パブリック公開が既定の HTTP API に切り替える。認可は従来どおり Lambda 内 JWT 検証。
resource "aws_apigatewayv2_api" "profile_ui" {
  name          = "editor-claude-bedrock-profile-ui"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "profile_ui" {
  api_id                 = aws_apigatewayv2_api.profile_ui.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.profile_ui.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0" # Function URL と同じイベント形（requestContext.http / rawPath）
}

# 全パス・全メソッドを Lambda へ（ルーティングは profile_ui.py 側で行う）
resource "aws_apigatewayv2_route" "profile_ui" {
  api_id    = aws_apigatewayv2_api.profile_ui.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.profile_ui.id}"
}

resource "aws_apigatewayv2_stage" "profile_ui" {
  api_id      = aws_apigatewayv2_api.profile_ui.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "profile_ui_apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.profile_ui.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.profile_ui.execution_arn}/*/*"
}

output "profile_ui_url" {
  description = "利用者プロファイル管理 UI の URL（EntraID のリダイレクト URI にも登録する）"
  value       = aws_apigatewayv2_stage.profile_ui.invoke_url
}
