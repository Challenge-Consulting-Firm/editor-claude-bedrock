#!/usr/bin/env bash
# 補助検証: ネイティブ Converse API での jp. プロファイル疎通（SigV4 = 運用者の CLI 認証情報）
#
# 位置づけ: 検証2（OpenAI 互換）が失敗したときの切り分け用。
#   ここが通って 03 が落ちる → OpenAI 互換レイヤ固有の問題
#   ここも落ちる           → モデルアクセス未許可 / プロファイル ID 誤り / リージョン問題
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq
require_var JP_PROFILE_ID

echo "== Converse 疎通: ${JP_PROFILE_ID}（region: ${AWS_REGION}） =="
OUT=$(aws bedrock-runtime converse --region "$AWS_REGION" \
  --model-id "$JP_PROFILE_ID" \
  --messages '[{"role":"user","content":[{"text":"「国内完結PoC疎通OK」とだけ返答してください"}]}]' \
  --inference-config '{"maxTokens":64}' \
  --output json)

echo "$OUT" | jq -r '.output.message.content[0].text'
echo
echo "$OUT" | jq '{stopReason, usage}'
echo
echo "✅ Converse 疎通 OK。次: ./scripts/03-invoke-openai-compat.sh（本丸）"
echo "   ※ この呼び出しも CloudTrail に記録される。04 で inferenceRegion を確認できる"
