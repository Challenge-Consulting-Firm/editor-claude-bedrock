#!/usr/bin/env bash
# 廃止したモデルの per-user アプリ推論プロファイルを削除する（運用者用・冪等）。
#
# 用途:
#   モデルを廃止したとき（例: Opus 5 の global ルーティング廃止）、
#   IAM で塞ぐだけでは per-user プロファイル（Terraform 管理外）が残骸として残る。
#   残骸は「ポータルに出ないのに ARN を直接使えば呼べる」状態を生みかねないため、
#   実体ごと削除して棚卸しをきれいにする。
#
# 安全側の設計:
#   - 既定は dry-run。--apply を付けたときだけ実際に削除する
#   - 削除対象は app=claude-code かつ model=<廃止モデル> のタグ完全一致のみ
#     （opus-5 と opus-5-5 のような前方一致の取り違えを防ぐ）
#   - 現行モデル定義（デプロイ済み Lambda の MODEL_SOURCES_JSON）に
#     まだ含まれるモデルは、誤削除防止のため拒否する
#
# 使い方:
#   ./scripts/12-retire-model-profiles.sh --model opus-5            # 差分確認（dry-run）
#   ./scripts/12-retire-model-profiles.sh --model opus-5 --apply    # 実削除
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws python3

# 管理 API は運用者の SigV4 で呼ぶ（.env の利用者向け Bearer キーは使わない）
unset AWS_BEARER_TOKEN_BEDROCK

TARGET_MODEL=""
APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --model には廃止するモデルのタグ値が必要です（例: opus-5）" >&2
        exit 1
      fi
      TARGET_MODEL="$2"
      shift 2
      ;;
    --apply) APPLY=1; shift ;;
    --help|-h)
      sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "ERROR: 不明な引数: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET_MODEL" ]]; then
  echo "ERROR: --model は必須です（例: --model opus-5）" >&2
  exit 1
fi

TARGET_MODEL="$TARGET_MODEL" APPLY="$APPLY" AWS_REGION="$AWS_REGION" python3 <<'PY'
import json
import os
import sys

import boto3

region = os.environ["AWS_REGION"]
target = os.environ["TARGET_MODEL"]
apply_changes = os.environ.get("APPLY") == "1"

bedrock = boto3.client("bedrock", region_name=region)
lambda_client = boto3.client("lambda", region_name=region)

# 現行モデル定義に残っているモデルは誤削除防止のため拒否する。
try:
    env = lambda_client.get_function_configuration(
        FunctionName="editor-claude-bedrock-profile-ui"
    ).get("Environment", {}).get("Variables", {})
    current = json.loads(env.get("MODEL_SOURCES_JSON", "{}"))
except Exception as exc:  # noqa: BLE001
    print(f"ERROR: profile_ui Lambda の設定を取得できません: {exc}", file=sys.stderr)
    raise SystemExit(1)

if target in current:
    print(
        f"ERROR: model={target} はまだ現行定義に含まれています（{current[target]}）。\n"
        "       先に Terraform 側から外して apply してください（廃止の順序を守る）。",
        file=sys.stderr,
    )
    raise SystemExit(1)

app_tag = env.get("USER_PROFILE_APP_TAG", "claude-code")

victims = []
paginator = bedrock.get_paginator("list_inference_profiles")
for page in paginator.paginate(typeEquals="APPLICATION"):
    for profile in page.get("inferenceProfileSummaries", []):
        arn = profile["inferenceProfileArn"]
        tags = {
            t["key"]: t["value"]
            for t in bedrock.list_tags_for_resource(resourceARN=arn).get("tags", [])
        }
        if tags.get("app") != app_tag:
            continue
        # 完全一致のみ（opus-5 指定で opus-5-5 を巻き込まない）
        if tags.get("model") != target:
            continue
        victims.append({
            "user": tags.get("user", "(no-user)"),
            "arn": arn,
            "id": profile.get("inferenceProfileId", ""),
            "residency": tags.get("residency", "-"),
        })

print(f"廃止対象モデル: {target}")
print(f"現行モデル定義: {', '.join(sorted(current)) or '(なし)'}")
print(f"該当プロファイル: {len(victims)} 件\n")

if not victims:
    print("削除対象はありません（既に廃止済み）")
    raise SystemExit(0)

for v in sorted(victims, key=lambda x: x["user"]):
    print(f"  {v['user']:22} {v['id']}  residency={v['residency']}")

print()
if not apply_changes:
    print("dry-run のため削除していません。実行するには --apply を付けてください")
    raise SystemExit(0)

deleted = 0
failed = []
for v in sorted(victims, key=lambda x: x["user"]):
    try:
        bedrock.delete_inference_profile(inferenceProfileIdentifier=v["arn"])
        deleted += 1
        print(f"  ✅ 削除: {v['user']:22} {v['id']}")
    except Exception as exc:  # noqa: BLE001 - 1 件の失敗で全体を止めない
        failed.append((v["user"], exc))
        print(f"  ❌ 失敗: {v['user']:22} {v['id']}: {exc}")

print()
if failed:
    print(f"⚠️ {len(failed)} 件で失敗しました。上のエラーを確認してください")
    raise SystemExit(1)
print(f"✅ 廃止完了: {deleted} 件のプロファイルを削除しました")
print("   過去分のコストは Cost Explorer に履歴として残ります（タグ配賦済みのため追跡可能）")
PY
