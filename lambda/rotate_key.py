"""Bedrock API キーの週次ローテーション（Azure 版 R1: rotate_key の移植）。

毎週月曜 09:00 JST に EventBridge Scheduler から起動され:
  1. PoC ユーザーの既存キー（service-specific credential）を列挙
  2. 2 本ある場合は最古を削除（IAM の上限 2 本/ユーザー/サービスを空ける）
  3. 新キーを発行（有効期限 KEY_AGE_DAYS 日 — ローテ失敗時のフェイルセーフ）
  4. 新キーを SSM SecureString へ保存（Azure 版 Key Vault 相当）
  5. Teams Workflows へ「更新した旨 + 利用者ポータル URL」を投稿（{"text": ...} 形式）

直前まで配布されていたキーは削除しない = 次回ローテまで有効（Azure 版と同じ 1 週間の猶予）。

設計判断:
  - Teams 投稿失敗は握りつぶさず例外で関数を失敗させる。「発行成功 + 通知失敗」は
    利用者が旧キー失効に気づけない最悪パターンのため、必ずアラートに乗せる
  - キー本文・利用者別モデル ARN・セットアップ手順は Teams には載せず、利用者ポータル
    （profile_ui / EntraID 認証）に集約する。Teams 通知はポータル URL の案内のみに一本化した
    （キーとモデル ARN は本人だけが認証後に閲覧・コピーできる形にするため）。
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
# 利用者ポータル（profile_ui）の URL。キー本文・モデル ARN・手順はここに集約し、
# Teams 通知はこの URL の案内のみにする。infra が API Gateway の invoke_url を渡す。
APP_URL = os.environ.get("APP_URL", "")
SERVICE = "bedrock.amazonaws.com"

ROTATION_MESSAGE_TEMPLATE = (
    "【エディタ用 Claude (Bedrock)】{heading}\n"
    "\n"
    "APIキー・モデル設定・セットアップ手順は利用者ポータルでご確認ください（EntraID サインイン）。\n"
    "\n"
    "──────────────────\n"
    "■ 利用者ポータル\n"
    "──────────────────\n"
    "\n"
    "{app_url}\n"
    "\n"
    "・現行の Bedrock API キー（クリックでコピー）\n"
    "・自分のモデル ARN（Opus / Haiku・コスト配賦つき）\n"
    "・各エディタのセットアップ手順\n"
    "\n"
    "{footer}"
)


def build_message(*, rotated: bool) -> str:
    return ROTATION_MESSAGE_TEMPLATE.format(
        heading="APIキーをローテーションしました" if rotated else "現在の接続設定のご案内（キーの変更はありません）",
        app_url=APP_URL or "(未設定: 運用者へ連絡してください)",
        footer=(
            "旧キーは次回ローテーション（1週間後）で削除されます。1週間以内にポータルで新キーへ貼り替えてください。"
            if rotated
            else "お手元のキーが無効な場合はポータルで現行キーを確認してください。"
        ),
    )

iam = boto3.client("iam")
ssm = boto3.client("ssm")


def handler(event, context):  # noqa: ARG001
    # notify_only: キーは回さず、ポータル案内だけを Teams へ再投稿する
    # （新メンバー向けの随時案内・投稿フォーマット確認用。手動 invoke で使う）
    if isinstance(event, dict) and event.get("notify_only"):
        webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
        post_teams(webhook_url, build_message(rotated=False))
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

    # 5. Teams へ投稿（失敗したら関数ごと失敗させる）。本文はポータル案内のみ = 新キーは含めない
    webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    post_teams(webhook_url, build_message(rotated=True))

    return {
        "rotated": True,
        "credentialId": created["ServiceSpecificCredentialId"],
        "expiration": str(created.get("ExpirationDate", "")),
    }
