#!/usr/bin/env bash
# 検証2前半: OpenAI 互換エンドポイント + Bedrock API キー（Bearer）+ jp. プロファイル
#
# ⚠️ 実測済みの結論（2026-07-14、README「実測で分かった制約」参照）:
#   正常系は 404 model_not_found になる。/openai/v1 のカタログは gpt-oss 系専用で
#   Claude は非対応（管理者権限でも同じ = IAM 要因ではない）。AWS 側の提供状況が
#   変わったかを確認する再実測用としてスクリプトは残す。
#   ネガティブテスト（IAM 迂回防止）は有効な検証として機能し続ける。
#
# 前提: ./scripts/10-issue-api-key.sh で発行したキーを .env の AWS_BEARER_TOKEN_BEDROCK に設定済み
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd curl jq
require_var JP_PROFILE_ID AWS_BEARER_TOKEN_BEDROCK

URL="$OPENAI_COMPAT_BASE/chat/completions"
echo "== エンドポイント: $URL =="
echo

call() { # $1=model, 応答は body / 終了コードでなく HTTP status を返す
  local model="$1" body_file="$2"
  curl -s -o "$body_file" -w '%{http_code}' -X POST "$URL" \
    -H "Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK" \
    -H 'Content-Type: application/json' \
    -d "{\"model\": \"$model\", \"messages\": [{\"role\": \"user\", \"content\": \"「OpenAI互換+jpプロファイルOK」とだけ返答してください\"}], \"max_tokens\": 64}"
}

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

echo "== 正常系: jp. プロファイル（${JP_PROFILE_ID}） =="
STATUS=$(call "$JP_PROFILE_ID" "$TMP")
case "$STATUS" in
  200)
    echo "✅ OK ($STATUS): $(jq -r '.choices[0].message.content' "$TMP")"
    jq '{model, usage}' "$TMP"
    ;;
  401|403)
    echo "❌ NG ($STATUS): 認証/認可エラー。キーの有効期限切れ・IAM ポリシー・モデルアクセス未許可を確認"
    jq . "$TMP" || cat "$TMP"
    ;;
  404)
    echo "❌ NG ($STATUS): エンドポイントまたはモデルが見つからない。"
    echo "   → 東京で OpenAI 互換 API が未提供、あるいは model に jp. プロファイル ID を受け付けない可能性（検証2の乖離発見）"
    jq . "$TMP" || cat "$TMP"
    ;;
  *)
    echo "❌ NG ($STATUS):"
    jq . "$TMP" || cat "$TMP"
    ;;
esac

echo
echo "== ネガティブテスト: jp. 以外のプロファイル（${NON_JP_PROFILE_ID:-未設定}）が拒否されること =="
if [[ -z "${NON_JP_PROFILE_ID:-}" ]]; then
  echo "SKIP: .env の NON_JP_PROFILE_ID が未設定（scripts/01 の出力から実在 ID を設定して再実行）"
else
  STATUS=$(call "$NON_JP_PROFILE_ID" "$TMP")
  CODE=$(jq -r '.error.code // ""' "$TMP" 2>/dev/null || true)
  # 実測: IAM 拒否は HTTP 401 + code=access_denied で返る（403 ではない）
  if [[ "$CODE" == "access_denied" ]]; then
    echo "✅ 期待どおり拒否 ($STATUS/access_denied) — IAM による迂回防止が機能（Azure ではできなかった統制）"
  elif [[ "$STATUS" == "200" ]]; then
    echo "❌ 危険: jp. 以外のプロファイルで推論が通ってしまった。IAM ポリシーを見直すこと（国内完結の統制が破れている）"
  else
    echo "⚠️ 想定外 ($STATUS)。access_denied を期待。応答を確認:"
    jq . "$TMP" || cat "$TMP"
  fi
fi

echo
echo "次: ./scripts/04-check-cloudtrail.sh（inferenceRegion の事後監査。CloudTrail 反映まで数分〜15分待つ）"
