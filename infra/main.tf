# Bedrock エディタ利用者向け IAM ポリシー。
# 国内3モデルは jp. プロファイル、Opus 5 は user/residency タグ付きの
# per-user application inference profile 経由に限定する。
#
# 迂回防止の設計（docs/design.md §4）:
#   - 国内3モデル: jp.* 推論プロファイル + 東京/大阪の foundation-model のみ
#   - Opus 5: allowlistしたglobal基盤モデルを、user/app/model/residencyタグ付き
#     application profile経由でのみ許可。globalシステムプロファイル直指定は拒否
#   - 明示Denyで、上記の許可済みglobal経路以外は国内リージョン外への迂回を封じる

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
    ]
    resources = local.jp_profile_arn_patterns
  }

  # (a-2) 国内3モデルのコスト配賦用アプリケーション推論プロファイル。
  #       user/app/model の3タグが正しく付いたものだけを許可する。既存プロファイルには
  #       residency タグが無いため、国内モデルは後方互換のため同タグを必須にしない。
  #       - タグ条件は Service Authorization Reference で InvokeModel* × application-inference-profile
  #         がサポートすると確認済み。IAM ポリシーシミュレータで tag 有→allow / tag 無→deny を実測（2026-08-06）。
  #       - 共有プロファイルは削除しない（report_usage.py の CloudWatch メトリクス基盤として存続）。invoke だけ塞ぐ。
  #       - 単一共有キーのため「各自が自分の cc- のみ」の強制は不可（design.md §運用者メモ）。共有排除までが到達点。
  statement {
    sid = "AllowInvokeUserTaggedJpAppProfiles"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/user"
      values   = ["?*"] # 1 文字以上。空 user タグによる未配賦を防止
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/app"
      values   = ["claude-code"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/model"
      values   = keys(local.user_profile_models_jp)
    }
  }

  # (a-3) Opus 5 は user/app/model に加え residency=global を必須にする。
  #       global. システムプロファイル直指定は許可しないため、国外処理も必ず
  #       per-user アプリプロファイルを経由し、利用者別集計と監査に載る。
  dynamic "statement" {
    for_each = local.global_enabled ? [1] : []
    content {
      sid = "AllowInvokeUserTaggedGlobalAppProfiles"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
      condition {
        test     = "StringLike"
        variable = "aws:ResourceTag/user"
        values   = ["?*"]
      }
      condition {
        test     = "StringEquals"
        variable = "aws:ResourceTag/app"
        values   = ["claude-code"]
      }
      condition {
        test     = "StringEquals"
        variable = "aws:ResourceTag/model"
        values   = keys(local.app_profile_models_global)
      }
      condition {
        test     = "StringEquals"
        variable = "aws:ResourceTag/residency"
        values   = ["global"]
      }
    }
  }

  # Opus 5 の global. システムプロファイル直指定は意図的に許可しない。
  # 利用者別棚卸しを保証するため、呼び出し経路は (a-3) の user タグ付き
  # application-inference-profile に一本化する。global. プロファイルへの権限は
  # profile_ui の作成ロールだけが持つ（per-user プロファイルのコピー元として使用）。

  # (b) プロファイルが内部でルーティングする先の foundation-model（東京・大阪のみ）。
  #     jp.* プロファイル経由であることを条件にする → モデル ARN 直指定の呼び出しは不許可
  statement {
    sid = "AllowFoundationModelOnlyViaJpProfile"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
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

  # (b-2) allowlist 済み global モデルの foundation-model。
  #       user/app/model タグ付き application profile の内部ルーティングに限って許可する。
  #       システム global. プロファイルや foundation-model の直指定は許可しない。
  dynamic "statement" {
    for_each = length(local.global_foundation_model_arns) > 0 ? [1] : []
    content {
      sid = "AllowFoundationModelForGlobalProfiles"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      resources = local.global_foundation_model_arns
      condition {
        test     = "ArnLike"
        variable = "bedrock:InferenceProfileArn"
        values   = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
      }
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

  # 国内モデルのアプリプロファイルは、タグを誤って residency=global にしても
  # 国外エンドポイントから呼べないよう model タグで明示Denyする。
  statement {
    sid    = "DenyJpAppProfilesOutsideJpRegions"
    effect = "Deny"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = local.jp_inference_regions
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/model"
      values   = keys(local.user_profile_models_jp)
    }
  }

  # 東京・大阪以外のリージョンへの推論呼び出しを明示 Deny（迂回防止の 2 重目）。
  # ⚠️ 実測で判明（2026-07-14）: jp. プロファイルが大阪(ap-northeast-3)へルーティングする際、
  # この Deny は aws:RequestedRegion=ap-northeast-3 で評価される。東京だけを許すと
  # プロファイル内部のルーティングまで拒否してしまうため、推論先 2 リージョンを許容する。
  # 大阪エンドポイントの「直叩き」は Allow 側の条件（jp.* プロファイル経由のみ）で引き続き塞がる
  #
  # ⚠️ global 許可時の除外（NotResource）: Opus 5 の per-user application profile と
  #    その内部ルーティング先foundation-modelだけをDeny対象外にする。
  #    global. システムプロファイル自体は除外しないため、直指定は引き続き拒否される。
  #    allow_global_models = false なら NotResource は空 = 全リソースが従来どおり Deny 対象。
  statement {
    sid    = "DenyInvokeOutsideJpRegions"
    effect = "Deny"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # global 許可時は allowlist 分を除外し、それ以外の全リソースを Deny 対象にする。
    not_resources = length(local.global_deny_exempt_arns) > 0 ? local.global_deny_exempt_arns : null
    resources     = length(local.global_deny_exempt_arns) > 0 ? null : ["*"]
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

# ⚠️ インラインポリシー（aws_iam_user_policy）ではなく管理ポリシーを使う理由:
#   IAM ユーザのインラインポリシーは上限 2048 バイト。Opus 5用の
#   Allow とDeny例外を加えると上限を超えるため（AWS put-user-policyでLimitExceededを実測）、
#   管理ポリシーは 6144 バイトまで許容されるためこちらに移行する。
#   （ポリシーの中身・評価結果は変わらない。アタッチ先も同じ PoC ユーザ 1 人）
resource "aws_iam_policy" "jp_only_invoke" {
  name        = "jp-only-bedrock-invoke"
  path        = "/editor-claude-bedrock/"
  description = "Bedrock invoke policy: Japan-resident 4.x plus per-user application profiles for allowlisted global Claude 5 models"
  policy      = data.aws_iam_policy_document.jp_only_invoke.json
}

resource "aws_iam_user_policy_attachment" "jp_only_invoke" {
  user       = aws_iam_user.poc.name
  policy_arn = aws_iam_policy.jp_only_invoke.arn
}

# 旧インラインポリシーを小さな互換スタブとして残す。
# 新しい管理ポリシーのアタッチ後に更新する依存関係により、1回の apply 中でも
# 旧 Deny が先に消えて権限断になる／旧 Deny が global 5 を拒否し続ける時間を最小化する。
# 次回以降、全環境の移行完了を確認してからこのスタブは安全に削除できる。
data "aws_iam_policy_document" "jp_only_invoke_migration_stub" {
  statement {
    sid       = "ManagedPolicyMigrationComplete"
    actions   = ["bedrock:ListInferenceProfiles"]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "jp_only_invoke" {
  name       = "jp-only-bedrock-invoke"
  user       = aws_iam_user.poc.name
  policy     = data.aws_iam_policy_document.jp_only_invoke_migration_stub.json
  depends_on = [aws_iam_user_policy_attachment.jp_only_invoke]

  lifecycle {
    create_before_destroy = true
  }
}
