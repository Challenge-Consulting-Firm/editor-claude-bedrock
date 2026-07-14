#!/usr/bin/env bash
# PoC 基盤（IAM 統制 + Budget）のデプロイ: .env 読み込み → terraform plan → 確認 → apply
#
# 使い方:
#   cp .env.sample .env   # 初回のみ。値を実値に置き換える
#   ./scripts/deploy.sh
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_cmd aws terraform
require_var OPS_EMAIL MONTHLY_BUDGET_USD POC_USER_NAME TEAMS_WEBHOOK_URL

if [[ "$OPS_EMAIL" == "ops@example.com" ]]; then
  echo "ERROR: OPS_EMAIL がサンプル値のままです。実メールに置き換えてください" >&2
  exit 1
fi

echo "== デプロイ先 AWS アカウント =="
aws sts get-caller-identity --output table

export TF_VAR_aws_region="$AWS_REGION"
export TF_VAR_poc_user_name="$POC_USER_NAME"
export TF_VAR_monthly_budget_usd="$MONTHLY_BUDGET_USD"
export TF_VAR_ops_email="$OPS_EMAIL"
export TF_VAR_teams_webhook_url="$TEAMS_WEBHOOK_URL"
# 任意: .env に ALLOWED_IPS（カンマ区切り）があれば JSON リストにして渡す
if [[ -n "${ALLOWED_IPS:-}" ]]; then
  TF_VAR_allowed_ips=$(printf '%s' "$ALLOWED_IPS" | jq -R 'split(",") | map(if test("/") then . else . + "/32" end)')
  export TF_VAR_allowed_ips
fi

cd "$REPO_ROOT/infra"
terraform init -input=false
terraform plan -out=poc.tfplan

echo
read -r -p "上記 plan を apply しますか? [y/N] " ans
if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
  echo "中止しました"
  exit 0
fi

terraform apply poc.tfplan
echo
echo "次: ./scripts/00-preflight.sh → ./scripts/01-list-jp-profiles.sh"
