#!/usr/bin/env bash
# PoC ユーザーに Bedrock API キー（長期・有効期限付き）を発行する
#
# 実体は IAM の service-specific credential（service: bedrock.amazonaws.com）。
# - 長期キーは AWS 的には「検証用」の位置づけ（本番推奨は短期キー 12h）。PoC 用途はまさにこれ
# - 有効期限（API_KEY_EXPIRY_DAYS、既定 7 日）を付けて発行 = 週次ローテ運用のマップ先
# - キーは発行時に一度しか表示されない。表示された値を .env の AWS_BEARER_TOKEN_BEDROCK に設定する
#
# ⚠️ 通常運用は週次自動ローテ（infra/rotation.tf の Lambda が毎週月曜 09:00 JST に実行し
#    Teams へ投稿）。本スクリプトは初期セットアップ・緊急時の手動発行用。
#    手動で即時ローテしたい場合は Lambda 起動でも可:
#    aws lambda invoke --function-name editor-claude-bedrock-rotate-key --payload '{}' /dev/stdout
#
# 使い方:
#   ./scripts/10-issue-api-key.sh           # 発行（既存キーがあれば一覧表示して確認）
#   ./scripts/10-issue-api-key.sh --rotate  # 旧キーを削除して再発行（ローテーション）
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq
require_var POC_USER_NAME API_KEY_EXPIRY_DAYS

EXISTING=$(aws iam list-service-specific-credentials \
  --user-name "$POC_USER_NAME" --service-name bedrock.amazonaws.com \
  --output json | jq '.ServiceSpecificCredentials // []')
N_EXISTING=$(echo "$EXISTING" | jq 'length')

if [[ "${1:-}" == "--rotate" && "$N_EXISTING" -gt 0 ]]; then
  echo "== ローテーション: 既存キー $N_EXISTING 件を削除 =="
  echo "$EXISTING" | jq -r '.[].ServiceSpecificCredentialId' | while read -r ID; do
    aws iam delete-service-specific-credential --user-name "$POC_USER_NAME" \
      --service-specific-credential-id "$ID"
    echo "削除: $ID"
  done
elif [[ "$N_EXISTING" -gt 0 ]]; then
  echo "⚠️ 既存キーが $N_EXISTING 件あります（値は再表示不可）:"
  echo "$EXISTING" | jq -r '.[] | "  \(.ServiceSpecificCredentialId)  status=\(.Status)  作成=\(.CreateDate)  期限=\(.ExpirationDate // "-")"'
  echo "再発行してローテするには: ./scripts/10-issue-api-key.sh --rotate"
  exit 0
fi

echo "== Bedrock API キー発行（user: $POC_USER_NAME, 期限: ${API_KEY_EXPIRY_DAYS}日） =="
CRED=$(aws iam create-service-specific-credential \
  --user-name "$POC_USER_NAME" \
  --service-name bedrock.amazonaws.com \
  --credential-age-days "$API_KEY_EXPIRY_DAYS" \
  --output json)

KEY=$(echo "$CRED" | jq -r '.ServiceSpecificCredential.ServiceCredentialSecret // .ServiceSpecificCredential.ServicePassword')
EXPIRES=$(echo "$CRED" | jq -r '.ServiceSpecificCredential.ExpirationDate // "-"')

cat <<EOF

発行しました（この値は二度と表示されません）:

  AWS_BEARER_TOKEN_BEDROCK=$KEY

  有効期限: $EXPIRES

次の手順:
  1. 上の 1 行を .env に貼り付ける（.env はコミット禁止・.gitignore 済み）
  2. ./scripts/03-invoke-openai-compat.sh で実測
  3. エディタ設定（docs/setup-zed.md / docs/setup-vscode.md）にも同じキーを設定

本番化メモ: 週次ローテは cron/EventBridge から本スクリプト --rotate 相当を実行し
Teams Workflows へ投稿する形で Azure 版 R1 をマップできる（docs/design.md §7）
EOF
