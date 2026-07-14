#!/usr/bin/env bash
# 事前確認: CLI・認証・リージョン・Opus 4.8 のモデルアクセス有効化
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_cmd aws jq curl

echo "== 認証情報 =="
aws sts get-caller-identity --output table

echo "== リージョン =="
echo "呼び出し元エンドポイント: ${AWS_REGION}（jp. プロファイルの前提 = ap-northeast-1）"
[[ "$AWS_REGION" == "ap-northeast-1" ]] || echo "⚠️ AWS_REGION が東京ではありません。国内完結の検証にならない可能性"

echo
echo "== 東京リージョンでの Opus 4.8 提供・モデルアクセス =="
# byOutputModality TEXT で十分。アクセス未許可でも一覧には出るため、実際の可否は 02 の実測で確定する
aws bedrock list-foundation-models --region "$AWS_REGION" \
  --by-provider anthropic \
  --query "modelSummaries[?contains(modelId, 'opus-4-8')].{modelId:modelId, lifecycle:modelLifecycle.status, inference:inferenceTypesSupported | join(',', @)}" \
  --output table

cat <<'EOF'

判定:
- 表に opus-4-8 が出ていれば東京リージョンで提供あり
- inference 列に INFERENCE_PROFILE があれば「プロファイル経由での呼び出し」形態（ON_DEMAND 無しはモデル直叩き不可 = jp. プロファイル必須の裏付け）
- モデルアクセス（コンソール > Bedrock > Model access）の有効化を忘れずに。未許可だと 02 で AccessDeniedException になる
次: ./scripts/01-list-jp-profiles.sh
EOF
