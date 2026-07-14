#!/usr/bin/env bash
# 各スクリプト共通: .env 読み込みと前提チェック。source して使う
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE がありません。'cp .env.sample .env' して値を設定してください" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

AWS_REGION="${AWS_REGION:-ap-northeast-1}"
export AWS_REGION
# AWS_PROFILE は空なら unset（既定の認証情報チェーンに任せる）
if [[ -z "${AWS_PROFILE:-}" ]]; then unset AWS_PROFILE; fi
# AWS_BEARER_TOKEN_BEDROCK が空のまま export されていると、AWS CLI が Bedrock 系 API を
# Bearer 認証で呼ぼうとして SigV4 が壊れる（IncompleteSignatureException）。空なら unset
if [[ -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]]; then unset AWS_BEARER_TOKEN_BEDROCK; fi

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: $c が見つかりません（brew install $c）" >&2; exit 1; }
  done
}

require_var() {
  for v in "$@"; do
    if [[ -z "${!v:-}" ]]; then echo "ERROR: .env の $v が未設定です" >&2; exit 1; fi
  done
}

OPENAI_COMPAT_BASE="https://bedrock-runtime.${AWS_REGION}.amazonaws.com/openai/v1"
