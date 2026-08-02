"""Bedrock API キーの週次ローテーション（Azure 版 R1: rotate_key の移植）。

毎週月曜 09:00 JST に EventBridge Scheduler から起動され:
  1. PoC ユーザーの既存キー（service-specific credential）を列挙
  2. 2 本ある場合は最古を削除（IAM の上限 2 本/ユーザー/サービスを空ける）
  3. 新キーを発行（有効期限 KEY_AGE_DAYS 日 — ローテ失敗時のフェイルセーフ）
  4. 新キーを SSM SecureString へ保存（Azure 版 Key Vault 相当）
  5. Teams Workflows へ新キーを投稿（Azure 版と同じ {"text": ...} 形式・新キー本文を含む）

直前まで配布されていたキーは削除しない = 次回ローテまで有効（Azure 版と同じ 1 週間の猶予）。

設計判断（Azure 版 rotation.py の踏襲）:
  - Teams 投稿失敗は握りつぶさず例外で関数を失敗させる。「発行成功 + 通知失敗」は
    利用者が旧キー失効に気づけない最悪パターンのため、必ずアラートに乗せる
  - 新キー本文を Teams に含める（週次ローテ前提で秘匿性は低いと判断。チャネルは利用者限定が前提）
"""

import logging
import os

import boto3
from teams import post_teams

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USER_NAME = os.environ["POC_USER_NAME"]
KEY_AGE_DAYS = int(os.environ.get("KEY_AGE_DAYS", "15"))
API_KEY_PARAM = os.environ["API_KEY_PARAM"]
WEBHOOK_PARAM = os.environ["WEBHOOK_PARAM"]
# 利用者向け設定値（キーと違って不変なので毎回の投稿に同梱する）
OPUS_PROFILE_ARN = os.environ.get("OPUS_PROFILE_ARN", "")
SONNET_PROFILE_ARN = os.environ.get("SONNET_PROFILE_ARN", "")
HAIKU_MODEL_ID = os.environ.get("HAIKU_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
AWS_REGION_FOR_USERS = os.environ.get("AWS_REGION_FOR_USERS", "ap-northeast-1")
DOCS_URL = os.environ.get("DOCS_URL", "https://github.com/Challenge-Consulting-Firm/editor-claude-bedrock/tree/main/docs")
SERVICE = "bedrock.amazonaws.com"
# コスト配賦用ユーザ別プロファイルをこのタグ値で列挙する（setup-claude-code.md §0.5 で付与）
USER_PROFILE_APP_TAG = os.environ.get("USER_PROFILE_APP_TAG", "claude-code")

ROTATION_MESSAGE_TEMPLATE = (
    "【エディタ用 Claude (Bedrock)】{heading}\n"
    "\n"
    "──────────────────\n"
    "■ APIキー\n"
    "──────────────────\n"
    "\n"
    "VS Code / Claude Code → 環境変数 AWS_BEARER_TOKEN_BEDROCK\n"
    "\n"
    "Zed → 設定の Bedrock API Key 欄\n"
    "\n"
    "{key}\n"
    "\n"
    "──────────────────\n"
    "■ 設定に貼る値（固定・変更なし）\n"
    "──────────────────\n"
    "\n"
    "● リージョン（AWS_REGION / ZED_AWS_REGION）\n"
    "{region}\n"
    "\n"
    "● 主力モデル Opus 4.8（ANTHROPIC_MODEL / Zed モデル name）\n"
    "{opus_arn}\n"
    "\n"
    "● 軽量モデル（ANTHROPIC_SMALL_FAST_MODEL / ANTHROPIC_DEFAULT_HAIKU_MODEL）\n"
    "{haiku_id}\n"
    "\n"
    "──────────────────\n"
    "■ 参考：切替で使える値（固定設定には貼りません）\n"
    "──────────────────\n"
    "\n"
    "● 節約モデル Sonnet 4.6\n"
    "Claude Code は  --model jp.anthropic.claude-sonnet-4-6  で都度切替。\n"
    "Zed はエージェント用の組み込み「Claude Sonnet 4.6」を利用。\n"
    "{sonnet_arn}\n"
    "\n"
    "──────────────────\n"
    "■ 手順書（初回セットアップ）\n"
    "──────────────────\n"
    "\n"
    "{docs_url}\n"
    "\n"
    "Claude Code CLI: setup-claude-code.md\n"
    "VS Code: setup-vscode.md\n"
    "Zed: setup-zed.md\n"
    "\n"
    "{per_user}"
    "{footer}"
)

PER_USER_SECTION_TEMPLATE = (
    "──────────────────\n"
    "■ 利用者ごとの設定値（コスト配賦・各自の分をコピー）\n"
    "──────────────────\n"
    "\n"
    "自分のユーザ名の行だけを設定してください。\n"
    "・ANTHROPIC_MODEL              ← Opus の ARN\n"
    "・ANTHROPIC_SMALL_FAST_MODEL   ← Haiku の ARN\n"
    "\n"
    "{rows}\n"
    "\n"
)


def build_per_user_section() -> str:
    """app=claude-code タグの付いたアプリ推論プロファイルを列挙し、利用者別の ARN 対応表を作る。

    プロファイルは setup-claude-code.md §0.5 の手順で user/app/model タグ付きで作成される。
    列挙に失敗しても通知本体（キー配布）は止めない — 対応表は補助情報のため。
    """
    try:
        users = collect_user_profiles()
    except Exception as exc:  # noqa: BLE001 - 対応表の取得失敗で通知を落とさない
        logger.warning("利用者別プロファイルの列挙に失敗（対応表は省略）: %s", exc)
        return ""
    if not users:
        return ""

    rows = []
    for user in sorted(users):
        arns = users[user]
        rows.append(
            f"● {user}\n"
            f"  Opus : {arns.get('opus', '(未作成)')}\n"
            f"  Haiku: {arns.get('haiku', '(未作成)')}"
        )
    return PER_USER_SECTION_TEMPLATE.format(rows="\n\n".join(rows))


def collect_user_profiles() -> dict:
    """{user: {"opus": arn, "haiku": arn}} を返す。タグは ListTagsForResource で引く。"""
    result: dict[str, dict[str, str]] = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for page in paginator.paginate(typeEquals="APPLICATION"):
        for profile in page.get("inferenceProfileSummaries", []):
            arn = profile["inferenceProfileArn"]
            tags = {
                t["key"]: t["value"]
                for t in bedrock.list_tags_for_resource(resourceARN=arn).get("tags", [])
            }
            if tags.get("app") != USER_PROFILE_APP_TAG:
                continue
            user = tags.get("user")
            model = tags.get("model")
            if not user or model not in ("opus", "haiku"):
                continue
            result.setdefault(user, {})[model] = arn
    return result


def build_message(key: str, *, rotated: bool) -> str:
    return ROTATION_MESSAGE_TEMPLATE.format(
        heading="APIキーをローテーションしました" if rotated else "現在の接続設定のご案内（キーの変更はありません）",
        key=key,
        region=AWS_REGION_FOR_USERS,
        opus_arn=OPUS_PROFILE_ARN or "(運用者に確認)",
        sonnet_arn=SONNET_PROFILE_ARN or "(運用者に確認)",
        haiku_id=HAIKU_MODEL_ID,
        docs_url=DOCS_URL,
        per_user=build_per_user_section(),
        footer=(
            "旧キーは次回ローテーション（1週間後）で削除されます。1週間以内に貼り替えてください。"
            if rotated
            else "お手元のキーが無効な場合は運用者へ連絡してください。"
        ),
    )

iam = boto3.client("iam")
ssm = boto3.client("ssm")
bedrock = boto3.client("bedrock")


def handler(event, context):  # noqa: ARG001
    # notify_only: キーは回さず、SSM の現行キー + 設定値一式を Teams へ再投稿するだけ
    # （新メンバー向けの随時案内・投稿フォーマット確認用。手動 invoke で使う）
    if isinstance(event, dict) and event.get("notify_only"):
        current_key = ssm.get_parameter(Name=API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
        webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
        post_teams(webhook_url, build_message(current_key, rotated=False))
        return {"rotated": False, "notified": True}

    # 1-2. 既存キーを列挙し、上限（2 本）に達していれば最古を削除
    creds = iam.list_service_specific_credentials(UserName=USER_NAME, ServiceName=SERVICE).get(
        "ServiceSpecificCredentials", []
    )
    creds.sort(key=lambda c: c["CreateDate"])
    while len(creds) >= 2:
        oldest = creds.pop(0)
        iam.delete_service_specific_credential(
            UserName=USER_NAME, ServiceSpecificCredentialId=oldest["ServiceSpecificCredentialId"]
        )
        logger.info("旧キー削除: %s (作成 %s)", oldest["ServiceSpecificCredentialId"], oldest["CreateDate"])

    # 3. 新キー発行（有効期限つき）
    created = iam.create_service_specific_credential(
        UserName=USER_NAME, ServiceName=SERVICE, CredentialAgeDays=KEY_AGE_DAYS
    )["ServiceSpecificCredential"]
    new_key = created.get("ServiceCredentialSecret") or created.get("ServicePassword")
    if not new_key:
        raise RuntimeError("新キーの本文を取得できませんでした（API 応答形式を確認）")
    logger.info("新キー発行: %s (期限 %s)", created["ServiceSpecificCredentialId"], created.get("ExpirationDate"))

    # 4. SSM SecureString へ保存（運用者・スクリプトの取得元）
    ssm.put_parameter(Name=API_KEY_PARAM, Value=new_key, Type="SecureString", Overwrite=True)

    # 5. Teams へ投稿（失敗したら関数ごと失敗させる）
    webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    post_teams(webhook_url, build_message(new_key, rotated=True))

    return {
        "rotated": True,
        "credentialId": created["ServiceSpecificCredentialId"],
        "expiration": str(created.get("ExpirationDate", "")),
    }
