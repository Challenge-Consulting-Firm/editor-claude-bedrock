#!/usr/bin/env bash
# 検証2後半: CloudTrail で実際の処理リージョン（inferenceRegion）を事後監査
#
# 判定基準（docs/poc-checklist.md #2）:
#   直近の InvokeModel / Converse イベントの additionalEventData.inferenceRegion が
#   ap-northeast-1 または ap-northeast-3 のみであること（= 推論が国内から出ていない）
#
# 注意:
#   - Bedrock の推論 API は CloudTrail の管理イベントとして記録される（Event history 90日・追加トレイル不要）
#   - 反映まで数分〜15分の遅延あり。03 実行直後は空になることがある
#   - OpenAI 互換 API 経由の呼び出しがどの eventName で記録されるかも本スクリプトで実測する
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq

LOOKBACK_MIN="${1:-60}"
START=$(date -u -v "-${LOOKBACK_MIN}M" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "-${LOOKBACK_MIN} minutes" '+%Y-%m-%dT%H:%M:%SZ')

echo "== 直近 ${LOOKBACK_MIN} 分の Bedrock 推論イベント（region: ${AWS_REGION}） =="
printf '%s\n' "eventTime | eventName | 呼出元region | modelId | inferenceRegion"

FOUND_ANY=0
BAD=0
for EV in InvokeModel InvokeModelWithResponseStream Converse ConverseStream; do
  ROWS=$(aws cloudtrail lookup-events --region "$AWS_REGION" \
    --lookup-attributes "AttributeKey=EventName,AttributeValue=$EV" \
    --start-time "$START" --max-results 50 \
    --query 'Events[].CloudTrailEvent' --output json |
    jq -r '.[] | fromjson
      | [.eventTime, .eventName, .awsRegion,
         (.requestParameters.modelId // "-"),
         (.additionalEventData.inferenceRegion // "(記録なし)")]
      | join(" | ")')
  if [[ -n "$ROWS" ]]; then
    FOUND_ANY=1
    printf '%s\n' "$ROWS"
    # inferenceRegion が記録されていて、かつ国内(ap-northeast-1/3)以外なら NG
    N=$(printf '%s\n' "$ROWS" | awk -F' \\| ' '$5 != "(記録なし)" && $5 !~ /^ap-northeast-(1|3)$/' | wc -l | tr -d ' ')
    BAD=$((BAD + N))
  fi
done

echo
if [[ "$FOUND_ANY" -eq 0 ]]; then
  echo "⚠️ イベントがまだありません。CloudTrail の反映遅延（数分〜15分）の可能性。時間を置いて再実行:"
  echo "   ./scripts/04-check-cloudtrail.sh 120"
elif [[ "$BAD" -eq 0 ]]; then
  echo "✅ 検証2 OK: 全推論の inferenceRegion が ap-northeast-1/3（国内）に収まっています"
  echo "   ※ '(記録なし)' の行はクロスリージョン推論でない呼び出し（一覧系や直叩き）。内容を確認のこと"
else
  echo "❌ 検証2 NG: 国内(ap-northeast-1/3)以外で処理されたイベントが $BAD 件あります。上の一覧を確認し、"
  echo "   docs/poc-checklist.md に記録のうえ IAM ポリシー / プロファイル指定を見直すこと"
fi
