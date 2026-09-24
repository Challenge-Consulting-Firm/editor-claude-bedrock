#!/usr/bin/env bash
# 事前確認: CLI・認証・リージョン・国内モデル（Opus 4.8 / Opus 5.5）+ Opus 5 global の提供/契約状態
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_cmd aws jq curl

# .env に利用者向けBearerキーが入っていても、管理APIの事前確認は運用者のSigV4認証を使う。
unset AWS_BEARER_TOKEN_BEDROCK

echo "== 認証情報 =="
aws sts get-caller-identity --output table

echo "== リージョン =="
echo "呼び出し元エンドポイント: ${AWS_REGION}（jp. プロファイルの前提 = ap-northeast-1）"
[[ "$AWS_REGION" == "ap-northeast-1" ]] || echo "⚠️ AWS_REGION が東京ではありません。国内完結の検証にならない可能性"

echo
echo "== 東京リージョンでの Opus 4.8 / Opus 5.5 / Opus 5 提供形態 =="
# アクセス未許可でも一覧には出るため、Opus 5 は下段の availability でも確認する
aws bedrock list-foundation-models --region "$AWS_REGION" \
  --by-provider anthropic \
  --query "modelSummaries[?contains(modelId, 'opus-4-8') || modelId == 'anthropic.claude-opus-5' || modelId == 'anthropic.claude-opus-5-5'].{modelId:modelId, lifecycle:modelLifecycle.status, inference:inferenceTypesSupported | join(',', @)}" \
  --output table

echo
echo "== Opus 5.5 の jp. プロファイル（国内完結の要・2026-09-24 提供確認） =="
# models[] が ap-northeast-1/3 のみ = 推論先が国内に閉じることの確認
aws bedrock get-inference-profile --region "$AWS_REGION" \
  --inference-profile-identifier jp.anthropic.claude-opus-5-5 \
  --query '{id:inferenceProfileId,status:status,inference_to:models[].modelArn}' \
  --output json

echo
echo "== Opus 5 の契約・認可・リージョン状態 =="
aws bedrock get-foundation-model-availability --region "$AWS_REGION" \
  --model-id anthropic.claude-opus-5 \
  --query '{modelId:modelId,authorization:authorizationStatus,agreement:agreementAvailability.status,entitlement:entitlementAvailability,region:regionAvailability}' \
  --output table

echo
echo "== Opus 5 global プロファイル =="
aws bedrock get-inference-profile --region "$AWS_REGION" \
  --inference-profile-identifier global.anthropic.claude-opus-5 \
  --query '{id:inferenceProfileId,status:status,models:models[].modelArn}' \
  --output json

cat <<'EOF'

判定:
- Opus 4.8 / Opus 5 / Opus 5.5 の inference が INFERENCE_PROFILE ならモデル直叩き不可
- Opus 5.5 は jp. プロファイルが ACTIVE かつ models[] が ap-northeast-1/3 のみ = 国内完結
- Opus 5 は authorization=AUTHORIZED、agreement/entitlement/region=AVAILABLE、globalプロファイル=ACTIVE が前提
- Opus 5 の推論先はglobalであり国内固定ではない。利用者はポータルが作るper-user ARNからのみ呼ぶ
次: ./scripts/01-list-jp-profiles.sh
EOF
