#!/usr/bin/env bash
# 検証1: 国内限定（jp.）クロスリージョン推論プロファイルの実在確認。
# 記事・ドキュメントは二次情報とし、AWS API の現在値を正とする。
#
# 判定基準（docs/poc-checklist.md #1 / Opus 5.5 国内完結対応）:
#   - Opus 4.8 / Opus 5.5 の jp. プロファイルが ACTIVE
#   - models[] が ap-northeast-1 / ap-northeast-3 の foundation-model のみ
#     （= 推論先が東京+大阪から外れない）
# 条件を満たさない場合は非ゼロで終了し、国内限定の前提崩れをデプロイ前に検知する。
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq

# プロファイル列挙・詳細取得は管理 API。利用者向け Bearer キーではなく運用者 SigV4 を使う。
# .env のキーがローテーション済みでも国内限定の事前検証を壊さない。
unset AWS_BEARER_TOKEN_BEDROCK

echo "== SYSTEM_DEFINED 推論プロファイル一覧（jp. のみ抽出） =="
PROFILES_JSON=$(aws bedrock list-inference-profiles --region "$AWS_REGION" \
  --type-equals SYSTEM_DEFINED --output json)

echo "$PROFILES_JSON" | jq -r '
  .inferenceProfileSummaries[]
  | select(.inferenceProfileId | startswith("jp."))
  | "\(.inferenceProfileId)\t\(.status)"' | column -t -s $'\t' || true

check_jp_profile() {
  local profile_id="$1"
  local label="$2"
  local detail status model_count invalid_count regions

  echo
  echo "== ${label} の jp. プロファイル詳細（国内限定の検証） =="
  if ! detail=$(aws bedrock get-inference-profile --region "$AWS_REGION" \
    --inference-profile-identifier "$profile_id" --output json); then
    echo "❌ ${profile_id} を取得できません" >&2
    return 1
  fi

  echo "$detail" | jq '{id: .inferenceProfileId, status: .status,
    inference_to: [.models[].modelArn | split(":")[3]]}'
  status=$(echo "$detail" | jq -r '.status // ""')
  model_count=$(echo "$detail" | jq '.models | length')
  invalid_count=$(echo "$detail" | jq '[
    .models[]?.modelArn
    | select((test("^arn:aws:bedrock:ap-northeast-(1|3)::foundation-model/") | not))
  ] | length')
  regions=$(echo "$detail" | jq -r '[.models[]?.modelArn | split(":")[3]] | unique | sort | join(",")')

  if [[ "$status" != "ACTIVE" ]]; then
    echo "❌ ${profile_id} は ACTIVE ではありません（status=${status:-不明}）" >&2
    return 1
  fi
  if [[ "$model_count" -eq 0 || "$invalid_count" -ne 0 ]]; then
    echo "❌ ${profile_id} の推論先に東京/大阪以外または不正な ARN が含まれます" >&2
    return 1
  fi
  echo "✅ ${label}: ACTIVE / 推論先=${regions}（国内限定）"
}

OPUS_48_PROFILE_ID="jp.anthropic.claude-opus-4-8"
OPUS_55_PROFILE_ID="jp.anthropic.claude-opus-5-5"
check_jp_profile "$OPUS_48_PROFILE_ID" "Opus 4.8"
check_jp_profile "$OPUS_55_PROFILE_ID" "Opus 5.5"

echo
if [[ "$OPUS_48_PROFILE_ID" != "${JP_PROFILE_ID:-}" ]]; then
  echo "⚠️ .env の JP_PROFILE_ID（${JP_PROFILE_ID:-未設定}）と一致しません。${OPUS_48_PROFILE_ID} に更新してください"
fi

echo
echo "== 参考: ネガティブテスト用の jp. 以外の Opus 4.8 プロファイル（NON_JP_PROFILE_ID に設定） =="
echo "$PROFILES_JSON" | jq -r '
  .inferenceProfileSummaries[]
  | select((.inferenceProfileId | startswith("jp.") | not) and (.inferenceProfileId | contains("opus-4-8")))
  | .inferenceProfileId'

echo
echo "✅ 検証1 OK: Opus 4.8 / Opus 5.5 とも国内限定プロファイルの前提を満たしています"
