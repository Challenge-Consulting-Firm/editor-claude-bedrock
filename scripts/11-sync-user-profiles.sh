#!/usr/bin/env bash
# 全利用者のアプリ推論プロファイルを現行モデル定義に同期する（運用者用・冪等）。
#
# 用途:
#   - 新モデル（例: Opus 5.5）を追加したとき、全員分を一括で作成する
#   - 新メンバー追加時に 1 名だけ作成する（--user で指定）
#   ※ モデルを**廃止**したときの残骸削除は ./scripts/12-retire-model-profiles.sh
#
# 仕組み:
#   profile_ui Lambda と同じ lambda/profile_ui.py の create_user_profiles() を呼ぶ。
#   モデル定義はデプロイ済み Lambda の MODEL_SOURCES_JSON を読むため、
#   Terraform の allow_global_models / global_model_profile_ids と常に一致する。
#   既存プロファイルはスキップされ、不足分だけが作られる（冪等）。
#   residency タグが無い旧プロファイルにはタグを補完する。
#
# 対象利用者は、既存の app=claude-code プロファイルから自動検出する
# （docs/setup-claude-code.md §0.5 の利用者一覧と一致する）。
#
# 使い方:
#   ./scripts/11-sync-user-profiles.sh              # 全利用者を同期
#   ./scripts/11-sync-user-profiles.sh --user riku.ibaraki
#   ./scripts/11-sync-user-profiles.sh --dry-run    # 作成せず差分だけ表示
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_cmd aws python3

# 管理 API は運用者の SigV4 で呼ぶ（.env の利用者向け Bearer キーは使わない）
unset AWS_BEARER_TOKEN_BEDROCK

TARGET_USER=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --user には利用者名が必要です" >&2
        exit 1
      fi
      TARGET_USER=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "ERROR: 不明な引数: $1" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$REPO_ROOT" TARGET_USER="$TARGET_USER" DRY_RUN="$DRY_RUN" \
  AWS_REGION="$AWS_REGION" python3 <<'PY'
import os
import sys

import boto3

region = os.environ["AWS_REGION"]
repo_root = os.environ["REPO_ROOT"]
target_user = os.environ.get("TARGET_USER") or ""
dry_run = os.environ.get("DRY_RUN") == "1"

sys.path.insert(0, os.path.join(repo_root, "lambda"))

# profile_ui は Lambda 実行時の環境変数を前提にするため、ここで補う。
# モデル定義はデプロイ済み Lambda の設定をそのまま使い、定義の二重管理を避ける。
lambda_client = boto3.client("lambda", region_name=region)
try:
    env = lambda_client.get_function_configuration(
        FunctionName="editor-claude-bedrock-profile-ui"
    ).get("Environment", {}).get("Variables", {})
except Exception as exc:  # noqa: BLE001
    print(f"ERROR: profile_ui Lambda の設定を取得できません: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not env.get("MODEL_SOURCES_JSON"):
    print(
        "ERROR: MODEL_SOURCES_JSON が未設定です。先に ./scripts/deploy.sh を実行してください",
        file=sys.stderr,
    )
    raise SystemExit(1)

os.environ["MODEL_SOURCES_JSON"] = env["MODEL_SOURCES_JSON"]
os.environ.setdefault("ENTRA_TENANT_ID", "unused-for-cli")
os.environ.setdefault("ENTRA_CLIENT_ID", "unused-for-cli")
os.environ.setdefault("USER_PROFILE_APP_TAG", env.get("USER_PROFILE_APP_TAG", "claude-code"))
os.environ.setdefault("AWS_REGION", region)

import profile_ui  # noqa: E402  (環境変数を設定してから読み込む)

print("対象モデル:", ", ".join(sorted(profile_ui.MODEL_SOURCES)))

existing = profile_ui.collect_user_profiles()
# collect_user_profiles() は表示用に residency を推定するため、実タグの欠落/誤値は
# raw records の residency_tagged で別途検出する。
records = profile_ui._list_user_profile_records()
repairs_by_user = {}
for record in records:
    if record["model"] in profile_ui.MODEL_SOURCES and not record["residency_tagged"]:
        repairs_by_user.setdefault(record["user"], []).append(record["model"])

if target_user:
    if not profile_ui._USER_RE.fullmatch(target_user):
        print(f"ERROR: 利用者名形式が不正です: {target_user}", file=sys.stderr)
        raise SystemExit(1)
    users = [target_user]
else:
    users = sorted(existing)

if not users:
    print("対象利用者が見つかりません（--user で明示指定してください）", file=sys.stderr)
    raise SystemExit(1)

print(f"対象利用者: {len(users)} 名\n")

created_total = 0
failed = []
for user in users:
    have = set(existing.get(user, {}))
    missing = [m for m in profile_ui.MODEL_SOURCES if m not in have]
    repairs = sorted(set(repairs_by_user.get(user, [])))
    if not missing and not repairs:
        print(f"  {user:22} 変更なし（{len(have)} モデル）")
        continue
    changes = []
    if missing:
        changes.append(f"作成: {', '.join(missing)}")
    if repairs:
        changes.append(f"residencyタグ修復: {', '.join(repairs)}")
    if dry_run:
        print(f"  {user:22} 変更予定: {' / '.join(changes)}")
        continue
    # create_user_profiles は不足モデル作成と既存 residency タグ修復の両方を冪等に行う。
    try:
        result = profile_ui.create_user_profiles(user)
    except Exception as exc:  # noqa: BLE001 - 1 名の失敗で全体を止めない
        failed.append((user, exc))
        print(f"  {user:22} ❌ 失敗: {exc}")
        continue
    created = [m for m in missing if m in result]
    still_missing = [m for m in missing if m not in result]
    created_total += len(created)
    added = ", ".join(
        f"{m}={result[m]['arn'].rsplit('/', 1)[-1]}" for m in created
    )
    details = []
    if added:
        details.append(f"追加: {added}")
    if repairs:
        details.append(f"residencyタグ修復: {', '.join(repairs)}")
    if still_missing:
        failed.append((user, RuntimeError(f"作成後も未検出: {', '.join(still_missing)}")))
        print(f"  {user:22} ❌ {' / '.join(details)} / 未検出: {', '.join(still_missing)}")
        continue
    print(f"  {user:22} ✅ {' / '.join(details)}")

print()
if dry_run:
    print("dry-run のため作成していません。実行するには --dry-run を外してください")
elif failed:
    print(f"⚠️ {len(failed)} 名で失敗しました。上のエラーを確認してください")
    raise SystemExit(1)
else:
    print(f"✅ 同期完了: {created_total} 件のプロファイルを作成しました")
    print("   各利用者はポータルで自分の ARN をコピーしてエディタに設定してください")
PY
