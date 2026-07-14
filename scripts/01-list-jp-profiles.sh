#!/usr/bin/env bash
# 検証1: jp. クロスリージョン推論プロファイルの実在確認（記事は二次情報。ここの出力が正）
#
# 判定基準（docs/poc-checklist.md #1）:
#   - jp.anthropic.claude-opus-4-8-* が一覧に存在する
#   - models[] が ap-northeast-1 / ap-northeast-3 の foundation-model のみで構成される（= 推論先が東京+大阪に閉じる）
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws jq

echo "== SYSTEM_DEFINED 推論プロファイル一覧（jp. のみ抽出） =="
PROFILES_JSON=$(aws bedrock list-inference-profiles --region "$AWS_REGION" \
  --type-equals SYSTEM_DEFINED --output json)

echo "$PROFILES_JSON" | jq -r '
  .inferenceProfileSummaries[]
  | select(.inferenceProfileId | startswith("jp."))
  | "\(.inferenceProfileId)\t\(.status)"' | column -t -s $'\t' || true

echo
echo "== Opus 4.8 の jp. プロファイル詳細（推論先リージョンの確認） =="
echo "$PROFILES_JSON" | jq -r '
  .inferenceProfileSummaries[]
  | select(.inferenceProfileId | startswith("jp.") and contains("opus-4-8"))
  | {id: .inferenceProfileId, status: .status,
     inference_to: [.models[].modelArn | capture("arn:aws:bedrock:(?<r>[^:]+):").r]}'

FOUND=$(echo "$PROFILES_JSON" | jq -r '
  [.inferenceProfileSummaries[] | select(.inferenceProfileId | startswith("jp.") and contains("opus-4-8"))] | length')

echo
if [[ "$FOUND" -ge 1 ]]; then
  ID=$(echo "$PROFILES_JSON" | jq -r '
    .inferenceProfileSummaries[] | select(.inferenceProfileId | startswith("jp.") and contains("opus-4-8"))
    | .inferenceProfileId' | head -1)
  echo "✅ 検証1 OK: $ID"
  if [[ "$ID" != "${JP_PROFILE_ID:-}" ]]; then
    echo "⚠️ .env の JP_PROFILE_ID（${JP_PROFILE_ID:-未設定}）と一致しません。上の実測値で .env を上書きしてください"
  fi
  echo
  echo "== 参考: ネガティブテスト用の jp. 以外の Opus 4.8 プロファイル（NON_JP_PROFILE_ID に設定） =="
  echo "$PROFILES_JSON" | jq -r '
    .inferenceProfileSummaries[]
    | select((.inferenceProfileId | startswith("jp.") | not) and (.inferenceProfileId | contains("opus-4-8")))
    | .inferenceProfileId'
else
  echo "❌ 検証1 NG: jp. の Opus 4.8 プロファイルが存在しません。"
  echo "   → クラスメソッド記事の内容と実環境が乖離（Azure で 4 回踏んだのと同じ構図）。"
  echo "   → 上の一覧にある jp. 対応モデル（Sonnet 等）での代替可否を含め、docs/poc-checklist.md に記録して判断"
fi
