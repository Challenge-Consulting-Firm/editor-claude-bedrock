# PoC 用 IAM ユーザーと「jp. プロファイル以外は物理的に呼べない」ポリシー。
#
# 迂回防止の設計（docs/design.md §4）:
#   - Allow は (a) jp.* 推論プロファイル と (b) 東京/大阪の foundation-model のみ。
#     (b) には「jp.* プロファイル経由の呼び出しであること」の Condition を付ける
#     → プロファイルを介さないモデル直叩き・global/apac プロファイルはどちらも許可されない
#   - さらに明示 Deny で東京以外のエンドポイントへの推論呼び出しを拒否
#     （us-east-1 等の bedrock-runtime に回り込む迂回を封じる）

data "aws_caller_identity" "current" {}

resource "aws_iam_user" "poc" {
  name = var.poc_user_name
  path = "/editor-claude-bedrock/"
}

data "aws_iam_policy_document" "jp_only_invoke" {
  # (a-1) jp. システム推論プロファイルそのものへの呼び出し。
  #       Zed 組み込みモデル（jp. 直指定）など、コスト配賦タグを介さない経路のために残す。
  statement {
    sid = "AllowInvokeJpInferenceProfile"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = local.jp_profile_arn_patterns
  }

  # (a-2) コスト配賦用アプリケーション推論プロファイル。中身は jp. の複製なので国内完結は保たれる。
  #       ⚠️ user タグが付いたプロファイル（cc-<user>-*）だけを許可する。これにより
  #       user タグの無い共有プロファイル（editor-claude-* / 他アプリの clock-in-out-* 等）経由の
  #       呼び出しを一律ブロックし、コスト配賦を user タグ付きに一本化する。
  #       - タグ条件は Service Authorization Reference で InvokeModel* × application-inference-profile
  #         がサポートすると確認済み。IAM ポリシーシミュレータで tag 有→allow / tag 無→deny を実測（2026-08-06）。
  #       - 共有プロファイルは削除しない（report_usage.py の CloudWatch メトリクス基盤として存続）。invoke だけ塞ぐ。
  #       - 単一共有キーのため「各自が自分の cc- のみ」の強制は不可（design.md §運用者メモ）。共有排除までが到達点。
  statement {
    sid = "AllowInvokeUserTaggedAppProfileOnly"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
    condition {
      test     = "Null"
      variable = "aws:ResourceTag/user"
      values   = ["false"] # false = 「タグが null ではない」= user タグが存在するものだけ許可
    }
  }

  # (b) プロファイルが内部でルーティングする先の foundation-model（東京・大阪のみ）。
  #     jp.* プロファイル経由であることを条件にする → モデル ARN 直指定の呼び出しは不許可
  statement {
    sid = "AllowFoundationModelOnlyViaJpProfile"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      for r in local.jp_inference_regions : "arn:aws:bedrock:${r}::foundation-model/*"
    ]
    condition {
      test     = "ArnLike"
      variable = "bedrock:InferenceProfileArn"
      values = concat(
        local.jp_profile_arn_patterns,
        ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"],
      )
    }
  }

  # Bedrock API キー（Bearer）認証の前提アクション。
  # ⚠️ 実測で判明（2026-07-14）: これが無いと Bearer キーでの呼び出しは
  # bedrock:CallWithBearerToken の AccessDenied になる（Resource は * のみ対応）。
  # 何を呼べるかは上 2 つの Allow と下の Deny が引き続き決める
  statement {
    sid       = "AllowBearerTokenAuth"
    actions   = ["bedrock:CallWithBearerToken"]
    resources = ["*"]
  }

  # ❌ bedrock-mantle:* は意図的に許可しない（実測 2026-07-14 の結論）:
  #   - mantle は独自カタログ制で jp. などの推論プロファイルを一切受け付けない（国内完結を指定できない）
  #   - Claude は mantle の chat/completions・responses どちらの API にも非対応
  #   - IAM リソースが project 単位（モデル単位でない）ため、許可すると独自カタログの
  #     他モデル（DeepSeek/Qwen 等）を jp. 統制の外で呼べてしまう = 国内完結統制の穴になる

  # 検証スクリプト用の読み取り（プロファイル一覧・モデル一覧）
  statement {
    sid = "AllowReadForVerification"
    actions = [
      "bedrock:ListInferenceProfiles",
      "bedrock:GetInferenceProfile",
      "bedrock:ListFoundationModels",
      "bedrock:GetFoundationModel",
    ]
    resources = ["*"]
  }

  # 東京・大阪以外のリージョンへの推論呼び出しを明示 Deny（迂回防止の 2 重目）。
  # ⚠️ 実測で判明（2026-07-14）: jp. プロファイルが大阪(ap-northeast-3)へルーティングする際、
  # この Deny は aws:RequestedRegion=ap-northeast-3 で評価される。東京だけを許すと
  # プロファイル内部のルーティングまで拒否してしまうため、推論先 2 リージョンを許容する。
  # 大阪エンドポイントの「直叩き」は Allow 側の条件（jp.* プロファイル経由のみ）で引き続き塞がる
  statement {
    sid    = "DenyInvokeOutsideJpRegions"
    effect = "Deny"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = local.jp_inference_regions
    }
  }

  # 任意: IP allowlist（Azure 版 R2 相当）。allowed_ips が空なら生成しない
  dynamic "statement" {
    for_each = length(var.allowed_ips) > 0 ? [1] : []
    content {
      sid       = "DenyFromOutsideAllowedIps"
      effect    = "Deny"
      actions   = ["bedrock:*"]
      resources = ["*"]
      condition {
        test     = "NotIpAddress"
        variable = "aws:SourceIp"
        values   = var.allowed_ips
      }
    }
  }
}

resource "aws_iam_user_policy" "jp_only_invoke" {
  name   = "jp-only-bedrock-invoke"
  user   = aws_iam_user.poc.name
  policy = data.aws_iam_policy_document.jp_only_invoke.json
}
