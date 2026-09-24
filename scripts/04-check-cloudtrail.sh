#!/usr/bin/env bash
# Bedrock 推論の実処理リージョンを CloudTrail で事後監査する。
#
# 判定:
#   - ap-northeast-1 / ap-northeast-3: 国内処理（OK）
#   - user/app/model/residency=global タグ付きアプリ推論プロファイルの国外処理:
#       意図して allowlist した global ルーティング（許可済み例外）
#   - 上記以外の国外処理: 統制違反
#
# 注意:
#   - CloudTrail 反映には数分〜15分かかることがある
#   - modelId がアプリ推論プロファイル ARN の場合、Bedrock のタグを照合して分類する
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq

LOOKBACK_MIN="${1:-60}"
START=$(date -u -v "-${LOOKBACK_MIN}M" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "-${LOOKBACK_MIN} minutes" '+%Y-%m-%dT%H:%M:%SZ')

GLOBAL_MODEL_TAGS_CSV="${GLOBAL_MODEL_TAGS:-opus-5}"
GLOBAL_MODEL_TAGS_JSON=$(printf '%s' "$GLOBAL_MODEL_TAGS_CSV" |
  jq -Rc 'split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)) | unique')

# 許可された global 経路を、タグ付き per-user アプリプロファイルから動的に解決する。
# システム global. プロファイル直指定やタグ不備のプロファイルはここに入らない。
GLOBAL_PROFILE_ARNS='[]'
APP_PROFILE_ARNS=$(env -u AWS_BEARER_TOKEN_BEDROCK \
  aws bedrock list-inference-profiles --region "$AWS_REGION" \
  --type-equals APPLICATION --output json |
  jq -r '.inferenceProfileSummaries[]?.inferenceProfileArn')

while IFS= read -r ARN; do
  [[ -n "$ARN" ]] || continue
  TAGS=$(env -u AWS_BEARER_TOKEN_BEDROCK \
    aws bedrock list-tags-for-resource --region "$AWS_REGION" \
    --resource-arn "$ARN" --output json)
  if printf '%s' "$TAGS" | jq -e --argjson allowedModels "$GLOBAL_MODEL_TAGS_JSON" '
    ([.tags[]? | select(.key == "user" and (.value | length > 0))] | length == 1) and
    ([.tags[]? | select(.key == "app" and .value == "claude-code")] | length == 1) and
    ([.tags[]? | select(.key == "model" and (.value as $model | $allowedModels | index($model) != null))] | length == 1) and
    ([.tags[]? | select(.key == "residency" and .value == "global")] | length == 1)
  ' >/dev/null; then
    GLOBAL_PROFILE_ARNS=$(printf '%s' "$GLOBAL_PROFILE_ARNS" |
      jq -c --arg arn "$ARN" '. + [$arn] | unique')
  fi
done <<EOF
$APP_PROFILE_ARNS
EOF

echo "== 直近 ${LOOKBACK_MIN} 分の Bedrock 推論イベント（呼び出し元: ${AWS_REGION}） =="
printf '%s\n' "eventTime | eventName | 呼出元region | modelId | inferenceRegion | 判定"

FOUND_ANY=0
BAD=0
REVIEW=0
GLOBAL_ALLOWED=0
for EV in InvokeModel InvokeModelWithResponseStream Converse ConverseStream; do
  EVENTS=$(aws cloudtrail lookup-events --region "$AWS_REGION" \
    --lookup-attributes "AttributeKey=EventName,AttributeValue=$EV" \
    --start-time "$START" --max-results 50 \
    --query 'Events[].CloudTrailEvent' --output json)
  ROWS=$(printf '%s' "$EVENTS" | jq -r --argjson globalArns "$GLOBAL_PROFILE_ARNS" '
    .[] | fromjson
    | (.requestParameters.modelId // "-") as $model
    | (.additionalEventData.inferenceRegion // "(記録なし)") as $region
    | (if $region == "(記録なし)" then "記録なし"
       elif ($region | test("^ap-northeast-(1|3)$")) then "国内"
       elif ($globalArns | index($model)) != null then "global許可"
       elif ($model | test("^arn:aws:bedrock:[^:]+:[0-9]+:application-inference-profile/")) then "要確認"
       else "違反"
       end) as $verdict
    | [.eventTime, .eventName, .awsRegion, $model, $region, $verdict]
    | @tsv')
  if [[ -n "$ROWS" ]]; then
    FOUND_ANY=1
    printf '%s\n' "$ROWS" | awk -F '\t' '{ print $1 " | " $2 " | " $3 " | " $4 " | " $5 " | " $6 }'
    N_BAD=$(printf '%s\n' "$ROWS" | awk -F '\t' '$6 == "違反"' | wc -l | tr -d ' ')
    N_REVIEW=$(printf '%s\n' "$ROWS" | awk -F '\t' '$6 == "要確認"' | wc -l | tr -d ' ')
    N_GLOBAL=$(printf '%s\n' "$ROWS" | awk -F '\t' '$6 == "global許可"' | wc -l | tr -d ' ')
    BAD=$((BAD + N_BAD))
    REVIEW=$((REVIEW + N_REVIEW))
    GLOBAL_ALLOWED=$((GLOBAL_ALLOWED + N_GLOBAL))
  fi
done

echo
if [[ "$FOUND_ANY" -eq 0 ]]; then
  echo "⚠️ イベントがまだありません。CloudTrail の反映遅延（数分〜15分）の可能性。時間を置いて再実行:"
  echo "   ./scripts/04-check-cloudtrail.sh 120"
elif [[ "$BAD" -eq 0 ]]; then
  echo "✅ 監査 OK: 国内モデルは ap-northeast-1/3 に収まり、未許可の国外処理はありません"
  if [[ "$GLOBAL_ALLOWED" -gt 0 ]]; then
    echo "   ℹ️ user タグ付き allowlist モデル（${GLOBAL_MODEL_TAGS_CSV}）による許可済み global 処理: ${GLOBAL_ALLOWED} 件"
  fi
  if [[ "$REVIEW" -gt 0 ]]; then
    echo "   ⚠️ 現在のタグを照合できないアプリプロファイルの国外処理: ${REVIEW} 件（削除済みプロファイル等。要確認）"
  fi
  echo "   ※ '(記録なし)' は処理先リージョンを判定できないため、必要に応じ個別確認してください"
else
  echo "❌ 監査 NG: 許可済み per-user global プロファイル以外の国外処理が ${BAD} 件あります。"
  echo "   上の『違反』行を確認し、IAM ポリシー / プロファイルタグ / モデル設定を見直してください"
fi
