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

import json
import logging
import os
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USER_NAME = os.environ["POC_USER_NAME"]
KEY_AGE_DAYS = int(os.environ.get("KEY_AGE_DAYS", "15"))
API_KEY_PARAM = os.environ["API_KEY_PARAM"]
WEBHOOK_PARAM = os.environ["WEBHOOK_PARAM"]
SERVICE = "bedrock.amazonaws.com"

ROTATION_MESSAGE_TEMPLATE = (
    "エディタ用 Claude (Bedrock) API キーをローテーションしました。\n"
    "新しいキー:\n{key}\n"
    "Claude Code の AWS_BEARER_TOKEN_BEDROCK をこの値に貼り替えてください"
    "（手順: リポジトリ editor-claude-bedrock の docs/setup-claude-code.md）。\n"
    "旧キーは次回ローテーション（1週間後）で削除されます。"
)

iam = boto3.client("iam")
ssm = boto3.client("ssm")


class TeamsNotificationError(Exception):
    """リトライしても Teams への投稿に失敗した。"""


def post_teams(webhook_url: str, message: str, *, max_attempts: int = 3, backoff_seconds: float = 5.0) -> None:
    payload = json.dumps({"text": message}).encode("utf-8")
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                if res.status >= 300:
                    raise RuntimeError(f"HTTP {res.status}")
            return
        except Exception as exc:  # noqa: BLE001 - リトライ対象を広く取る
            last_error = exc
            logger.warning("Teams 投稿失敗 (%s/%s): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(backoff_seconds * attempt)
    raise TeamsNotificationError(f"Teams への通知に {max_attempts} 回失敗しました") from last_error


def handler(event, context):  # noqa: ARG001
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
    post_teams(webhook_url, ROTATION_MESSAGE_TEMPLATE.format(key=new_key))

    return {
        "rotated": True,
        "credentialId": created["ServiceSpecificCredentialId"],
        "expiration": str(created.get("ExpirationDate", "")),
    }
