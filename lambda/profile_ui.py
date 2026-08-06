"""利用者プロファイル管理 Web UI（Lambda Function URL・EntraID 認証）。

「ユーザプロファイル」= 利用者ごとのコスト配賦用アプリケーション推論プロファイル
（`cc-<user>-opus` / `cc-<user>-haiku`。タグ user / app=claude-code / model 付き）。
従来 docs/setup-claude-code.md §0.5 の AWS CLI 手動作成だったものを Web UI 化する。

構成（design.md の流儀に合わせ最小構成）:
  - Lambda Function URL（authtype=NONE）1 本で HTML(SPA) と JSON API の両方を配信
  - 認証は EntraID: ブラウザ(MSAL.js)がアクセストークンを取り、API 呼び出しの
    Authorization: Bearer で送る。Lambda は entra_auth で JWKS 署名検証（tid 一致=テナント全員に開放）
  - Bedrock 操作は rotate_key.py の列挙ロジックと同じタグ規約に従う

API:
  GET    /api/config          MSAL 用の公開設定（tenant_id / client_id）。認証不要
  GET    /api/profiles        app=claude-code のプロファイルを {user:{opus,haiku,...}} で返す
  POST   /api/profiles        {"user": "..."} で当該利用者の opus+haiku を作成（既存はスキップ）
  DELETE /api/profiles        {"user": "..."} で当該利用者の全プロファイルを削除
  GET    /api/apikey          現行 Bedrock API キー本文 + 新旧 credential のメタ一覧を返す
  GET    /api/cost            Cost Explorer から今月の利用者別コスト（app=claude-code を user で集計）を返す
                             ?range=weekly で過去4週間分（各7日）の週次内訳を新しい週順に返す
  それ以外の GET              SPA(HTML) を返す
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from entra_auth import AuthError, verify_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_ID = os.environ["ENTRA_TENANT_ID"]
CLIENT_ID = os.environ["ENTRA_CLIENT_ID"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
APP_TAG_VALUE = os.environ.get("USER_PROFILE_APP_TAG", "claude-code")
# 週次ローテ（rotate_key.py）が発行・保管する Bedrock API キーの参照元。
# 本文は SSM SecureString（現行=新キーのみ）、新旧の credential メタは IAM から引く。
POC_USER_NAME = os.environ.get("POC_USER_NAME", "")
API_KEY_PARAM = os.environ.get("API_KEY_PARAM", "")
BEDROCK_CREDENTIAL_SERVICE = "bedrock.amazonaws.com"
# 利用者別コスト集計（report_usage.py と同じタグ規約）: app=claude-code を user タグで GroupBy する。
USER_TAG_KEY = os.environ.get("USER_TAG_KEY", "user")
USER_APP_TAG_KEY = os.environ.get("USER_APP_TAG_KEY", "app")
USER_APP_TAG_VALUE = APP_TAG_VALUE

# 作成対象モデル（inference-profiles.tf / setup-claude-code.md §0.5 と一致させる）。
# model タグ値 -> コピー元 jp. システムプロファイル ID
MODEL_SOURCES = {
    "opus": "jp.anthropic.claude-opus-4-8",
    "haiku": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# 利用者名（= user タグ / IAM ユーザー名想定）。英小文字・数字・. _ - のみ許可。
# create-inference-profile の名前はドット不可のため後段で `.`→`-` に置換する
_USER_RE = re.compile(r"^[a-z0-9._-]{1,64}$")

bedrock = boto3.client("bedrock", region_name=AWS_REGION)
iam = boto3.client("iam")
ssm = boto3.client("ssm")
# Cost Explorer は us-east-1 固定のグローバルサービス
ce = boto3.client("ce", region_name="us-east-1")

_account_id = None


def account_id() -> str:
    global _account_id
    if _account_id is None:
        _account_id = boto3.client("sts").get_caller_identity()["Account"]
    return _account_id


def source_arn(model: str) -> str:
    return f"arn:aws:bedrock:{AWS_REGION}:{account_id()}:inference-profile/{MODEL_SOURCES[model]}"


def collect_user_profiles() -> dict:
    """app=<APP_TAG_VALUE> のプロファイルを {user: {model: {"arn","name"}}} で返す。

    rotate_key.collect_user_profiles と同じタグ規約。ここでは削除に使う name も持つ。
    """
    result: dict = {}
    paginator = bedrock.get_paginator("list_inference_profiles")
    for page in paginator.paginate(typeEquals="APPLICATION"):
        for profile in page.get("inferenceProfileSummaries", []):
            arn = profile["inferenceProfileArn"]
            tags = {
                t["key"]: t["value"]
                for t in bedrock.list_tags_for_resource(resourceARN=arn).get("tags", [])
            }
            if tags.get("app") != APP_TAG_VALUE:
                continue
            user = tags.get("user")
            model = tags.get("model")
            if not user or not model:
                continue
            result.setdefault(user, {})[model] = {
                "arn": arn,
                "name": profile.get("inferenceProfileName", ""),
                "id": profile.get("inferenceProfileId", ""),
            }
    return result


def create_user_profiles(user: str) -> dict:
    """利用者の opus/haiku プロファイルを作成。既存分はスキップ。作成後の状態を返す。"""
    existing = collect_user_profiles().get(user, {})
    name_stem = user.replace(".", "-")  # プロファイル名にドットは使えない
    for model, src_id in MODEL_SOURCES.items():
        if model in existing:
            continue  # 冪等: 既にあれば作らない
        bedrock.create_inference_profile(
            inferenceProfileName=f"cc-{name_stem}-{model}",
            modelSource={"copyFrom": source_arn(model)},
            tags=[
                {"key": "user", "value": user},
                {"key": "app", "value": APP_TAG_VALUE},
                {"key": "model", "value": model},
            ],
        )
    return collect_user_profiles().get(user, {})


def delete_user_profiles(user: str) -> int:
    """利用者の app=<APP_TAG_VALUE> プロファイルを全削除。削除件数を返す。"""
    profiles = collect_user_profiles().get(user, {})
    count = 0
    for model, info in profiles.items():
        # 削除は識別子（ARN でも ID/名前でも可）で行う
        bedrock.delete_inference_profile(inferenceProfileIdentifier=info["arn"])
        count += 1
    return count


def collect_api_key() -> dict:
    """現行 API キー本文 + 新旧 credential のメタ一覧を返す。

    - 本文: rotate_key.py が SSM SecureString に上書き保存した「現行（=新）キー」のみ。
      旧キーの本文は IAM が発行時しか secret を返さない仕様上、後から取得できない（一覧のみ）。
    - 一覧: IAM の service-specific credential（最大 2 本）を作成日時順で返す。
      status（Active/Inactive）・作成日・有効期限つき。current フラグは「本文が現行のもの」を示す。
    """
    result: dict = {"current": None, "credentials": []}
    if not POC_USER_NAME:
        return result

    creds = iam.list_service_specific_credentials(
        UserName=POC_USER_NAME, ServiceName=BEDROCK_CREDENTIAL_SERVICE
    ).get("ServiceSpecificCredentials", [])
    creds.sort(key=lambda c: c["CreateDate"])
    # 最新（作成日が最大）を「現行キー」とみなす。SSM の本文はこの credential のもの。
    newest_id = creds[-1]["ServiceSpecificCredentialId"] if creds else None
    result["credentials"] = [
        {
            "id": c["ServiceSpecificCredentialId"],
            "createDate": c["CreateDate"].isoformat() if c.get("CreateDate") else "",
            "expiration": c["ExpirationDate"].isoformat() if c.get("ExpirationDate") else "",
            "status": c.get("Status", ""),
            "current": c["ServiceSpecificCredentialId"] == newest_id,
        }
        for c in creds
    ]

    if API_KEY_PARAM:
        try:
            result["current"] = ssm.get_parameter(Name=API_KEY_PARAM, WithDecryption=True)[
                "Parameter"
            ]["Value"]
        except ClientError as exc:
            # 本文が未保管（初回ローテ前など）でも一覧は返す
            logger.warning("API キー本文の取得に失敗: %s", exc.response["Error"].get("Code"))
    return result


def _fetch_cost_by_user(start: str, end: str, granularity: str) -> list:
    """[start, end) を granularity で GroupBy=user 集計し、期間ごとの {"period_start","by_user"} 一覧を返す。

    report_usage.get_cost_by_user と同じ集計（app=<APP_TAG_VALUE> を user タグで GroupBy）。
    user タグが空（未配賦: タグ有効化前・課金反映前・タグなし呼出）の分は "" キーに寄る。
    Cost Explorer の TimePeriod.End は排他なので、呼び出し側が当日を含めるには翌日を End にする。
    """
    periods: dict[str, dict[str, float]] = {}
    token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": USER_APP_TAG_KEY, "Values": [USER_APP_TAG_VALUE]}},
            GroupBy=[{"Type": "TAG", "Key": USER_TAG_KEY}],
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            p_start = period.get("TimePeriod", {}).get("Start", start)
            bucket = periods.setdefault(p_start, {})
            for grp in period.get("Groups", []):
                # Keys は ["user$takeshi.ohno"] 形式。空値は "user$"
                raw = grp["Keys"][0]
                user = raw.split("$", 1)[1] if "$" in raw else raw
                amount = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                bucket[user] = bucket.get(user, 0.0) + amount
        token = resp.get("NextPageToken")
        if not token:
            break
    return [{"period_start": ps, "by_user": periods[ps]} for ps in sorted(periods)]


def _summarize(by_user: dict[str, float]) -> dict:
    """{user: amount} を UI 向けの {"users":[{user,amount}],"unallocated","total"} に整形する。"""
    by_user = dict(by_user)
    unallocated = by_user.pop("", 0.0)
    users = [
        {"user": u, "amount": round(v, 2)}
        for u, v in sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)
    ]
    total = sum(by_user.values()) + unallocated
    return {
        "users": users,
        "unallocated": round(unallocated, 2),
        "total": round(total, 2),
    }


def collect_cost_by_user() -> dict:
    """今月（当月1日〜当日）の利用者別コストを {"month","currency","total","users":[{user,amount}],"unallocated"} で返す。"""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1).strftime("%Y-%m-%d")
    # Cost Explorer の TimePeriod.End は排他。当日分を含めるため翌日を End にする
    # （月初=当日でも期間が空にならない）
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    buckets = _fetch_cost_by_user(start, end, "MONTHLY")
    merged: dict[str, float] = {}
    for b in buckets:
        for u, v in b["by_user"].items():
            merged[u] = merged.get(u, 0.0) + v
    return {"month": now.strftime("%Y-%m"), "currency": "USD", **_summarize(merged)}


def collect_weekly_cost_by_user(weeks: int = 4) -> dict:
    """直近 weeks 週間（各7日）の利用者別コストを週ごとに新しい順で返す。

    今日を含む週の末日（排他 End）から 7 日ずつ遡って weeks 個の窓を作る。
    Cost Explorer は WEEKLY 粒度を持たないため DAILY で取得し、日次を各週バケットに合算する。
    返り値: {"currency","weeks":[{"weekStart","weekEnd","users":[...],"unallocated","total"}...]}（新しい週が先頭）。
    """
    now = datetime.now(timezone.utc)
    # End は排他。当日を含めるため「明日 0時」を全体の末端にし、そこから 7 日ずつ遡る
    end_exclusive = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end_exclusive - timedelta(days=7 * weeks)

    # 期間全体を DAILY で 1 回取得し、日次を該当週へ振り分ける（呼び出し回数を抑える）
    daily = _fetch_cost_by_user(
        start.strftime("%Y-%m-%d"), end_exclusive.strftime("%Y-%m-%d"), "DAILY"
    )

    # 週の境界（[week_start, week_end) を末尾側から weeks 本）
    bounds = [
        (end_exclusive - timedelta(days=7 * (i + 1)), end_exclusive - timedelta(days=7 * i))
        for i in range(weeks)
    ]
    week_buckets: list[dict[str, float]] = [{} for _ in bounds]
    for day in daily:
        d = datetime.strptime(day["period_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for idx, (w_start, w_end) in enumerate(bounds):
            if w_start <= d < w_end:
                bucket = week_buckets[idx]
                for u, v in day["by_user"].items():
                    bucket[u] = bucket.get(u, 0.0) + v
                break

    weeks_out = []
    for (w_start, w_end), bucket in zip(bounds, week_buckets):
        weeks_out.append({
            "weekStart": w_start.strftime("%Y-%m-%d"),
            # 表示用の週末日は排他 End の前日（=その週の最終日）
            "weekEnd": (w_end - timedelta(days=1)).strftime("%Y-%m-%d"),
            **_summarize(bucket),
        })
    # bounds は新しい順（i=0 が直近週）なのでそのまま返す
    return {"currency": "USD", "weeks": weeks_out}


# ---- HTTP ヘルパ -------------------------------------------------------------


def _resp(status: int, body, content_type="application/json"):
    if content_type == "application/json" and not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
        },
        "body": body,
    }


def _require_auth(event) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise AuthError("Authorization ヘッダーがありません")
    token = auth[7:].strip()
    return verify_token(token, TENANT_ID, CLIENT_ID)


def _method_and_path(event) -> tuple:
    ctx = event.get("requestContext", {}).get("http", {})
    return ctx.get("method", "GET"), event.get("rawPath", "/")


def handler(event, context):  # noqa: ARG001
    method, path = _method_and_path(event)

    # 認証不要: MSAL 初期化用の公開設定
    if path == "/api/config":
        return _resp(200, {"tenantId": TENANT_ID, "clientId": CLIENT_ID})

    # SPA 本体（認証はブラウザ側 MSAL + 各 API 呼び出しで実施）
    if not path.startswith("/api/"):
        return _resp(200, INDEX_HTML, content_type="text/html; charset=utf-8")

    # ここから API: 全て EntraID トークン必須
    try:
        claims = _require_auth(event)
    except AuthError as exc:
        logger.info("認証失敗: %s", exc)
        return _resp(401, {"error": str(exc)})

    caller = claims.get("preferred_username") or claims.get("upn") or claims.get("oid", "")

    if path == "/api/apikey":
        if method != "GET":
            return _resp(405, {"error": "許可されていないメソッド"})
        try:
            logger.info("API キー参照: caller=%s", caller)
            return _resp(200, collect_api_key())
        except ClientError as exc:
            logger.exception("API キー取得失敗")
            return _resp(502, {"error": f"API キー取得に失敗: {exc.response['Error'].get('Code', 'Unknown')}"})

    if path == "/api/cost":
        if method != "GET":
            return _resp(405, {"error": "許可されていないメソッド"})
        try:
            rng = (event.get("queryStringParameters") or {}).get("range", "monthly")
            logger.info("利用者別コスト参照: caller=%s range=%s", caller, rng)
            if rng == "weekly":
                return _resp(200, collect_weekly_cost_by_user())
            return _resp(200, collect_cost_by_user())
        except ClientError as exc:
            # user/app のコスト配分タグ未有効化・権限不足でも UI 全体は落とさない
            logger.warning("利用者別コスト取得失敗: %s", exc.response["Error"].get("Code"))
            return _resp(502, {"error": f"コスト取得に失敗: {exc.response['Error'].get('Code', 'Unknown')}"})

    if path == "/api/profiles":
        try:
            if method == "GET":
                return _resp(200, {"profiles": collect_user_profiles(), "models": list(MODEL_SOURCES)})

            body = json.loads(event.get("body") or "{}")
            user = (body.get("user") or "").strip().lower()
            if not _USER_RE.match(user):
                return _resp(400, {"error": "user は英小文字・数字・.（ドット）・_ ・- のみ、1〜64 文字"})

            if method == "POST":
                logger.info("プロファイル作成: user=%s caller=%s", user, caller)
                return _resp(201, {"user": user, "profiles": create_user_profiles(user)})
            if method == "DELETE":
                logger.info("プロファイル削除: user=%s caller=%s", user, caller)
                return _resp(200, {"user": user, "deleted": delete_user_profiles(user)})
            return _resp(405, {"error": "許可されていないメソッド"})
        except ClientError as exc:
            logger.exception("Bedrock 操作失敗")
            return _resp(502, {"error": f"Bedrock 操作に失敗: {exc.response['Error'].get('Code', 'Unknown')}"})

    return _resp(404, {"error": "not found"})


# SPA（単一 HTML）。ビルド不要で保守しやすいよう素の JS + MSAL.js(CDN) で書く。
# 設定（tenantId/clientId）は起動時に /api/config から取得する。
INDEX_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>利用者プロファイル管理 — Claude on Bedrock</title>
<script src="https://alcdn.msauth.net/browser/2.38.2/js/msal-browser.min.js"
        integrity="sha384-hhkHFODse2T75wPL7oJ0RZ+0CgRa74LNPhgx6wO6DMNEhU3/fSbTZdVzxsgyUelp"
        crossorigin="anonymous"></script>
<style>
  /* muji.com デザインシステム（DESIGN.md）に準拠したトークン */
  :root {
    --muji-red: #7f0819;      /* ブランドカラー（プライマリ・強調） */
    --red: #d60b14;           /* セール/エラー */
    --kinari: #f4ecde;        /* 生成り。ヘッダー/アクセント面 */
    --beige: #d8ccaa;         /* ベージュ。淡いアクセント */
    --white: #ffffff;
    --gray-100: #f7f7f5;      /* セクション背景 */
    --gray-200: #ebebec;      /* 薄い区切り線 */
    --gray-300: #d0d0d0;      /* 標準ボーダー */
    --text-primary: #3c3c43;
    --text-secondary: #6b6d72;
    --text-tertiary: #767676;
    --radius: 4px;            /* 全体の角丸 4px */
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, "Hiragino Kaku Gothic ProN", "Segoe UI", sans-serif;
    color: var(--text-primary); background: var(--white);
    margin: 0; line-height: 1.6; font-size: 16px;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 0 1.5rem; }

  /* ヘッダー: 生成り背景のヒーロー */
  header { background: var(--kinari); border-bottom: 1px solid var(--beige); }
  .header-inner { max-width: 880px; margin: 0 auto; padding: 2rem 1.5rem 1.75rem;
    display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; }
  h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: .02em; margin: 0; color: var(--muji-red); }
  h2 { font-size: 1.15rem; font-weight: 700; margin: 2rem 0 .75rem; letter-spacing: .01em; }
  .lead { color: var(--text-secondary); font-size: .9rem; margin: .75rem 0 0; }
  .who { color: var(--text-secondary); font-size: .85rem; }

  .muted { color: var(--text-tertiary); font-size: .9rem; }
  .section { margin: 1.75rem 0; }

  button { font: inherit; padding: .45rem 1rem; border-radius: var(--radius);
    border: 1px solid var(--gray-300); cursor: pointer; background: var(--white);
    color: var(--text-primary); transition: background .12s, border-color .12s; }
  button:hover { background: var(--gray-100); }
  button.primary { background: var(--muji-red); color: #fff; border-color: var(--muji-red); }
  button.primary:hover { background: #660614; border-color: #660614; }
  button.danger { background: var(--white); color: var(--red); border-color: var(--red); }
  button.danger:hover { background: #fdf0f0; }
  /* 再読み込み: 生成り面に濃色文字＋MUJI Red の枠で視認性を確保 */
  button.reload { background: var(--kinari); color: var(--muji-red); border-color: var(--beige); font-weight: 600; }
  button.reload:hover { background: var(--beige); }
  button:disabled { opacity: .45; cursor: default; }

  input { font: inherit; padding: .5rem .7rem; border-radius: var(--radius);
    border: 1px solid var(--gray-300); background: var(--white); color: var(--text-primary); }
  input:focus { outline: none; border-color: var(--muji-red); }

  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border-bottom: 1px solid var(--gray-200); padding: .65rem .5rem; text-align: left;
    font-size: .9rem; vertical-align: top; }
  th { background: var(--gray-100); color: var(--text-secondary); font-weight: 600;
    border-bottom: 1px solid var(--gray-300); font-size: .82rem; letter-spacing: .03em; }
  code { font-size: .82rem; word-break: break-all; font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }
  .arn { display: flex; align-items: baseline; gap: .5rem; margin: .2rem 0; }
  .tag { font-size: .72rem; color: #fff; background: var(--text-tertiary);
    border-radius: var(--radius); padding: .05rem .4rem; flex: none; letter-spacing: .03em; }
  /* クリックでコピーできる ARN。ヒント付き */
  .copy { cursor: pointer; border-bottom: 1px dashed var(--gray-300); transition: color .12s; }
  .copy:hover { color: var(--muji-red); border-bottom-color: var(--muji-red); }
  .copied { color: var(--muji-red); font-size: .72rem; margin-left: .3rem; }

  #msg { padding: .65rem 1rem; border-radius: var(--radius); margin: 1rem 0; display: none; font-size: .9rem; }
  #msg.err { background: #fdf0f0; color: var(--red); border: 1px solid #f3c9cc; display: block; }
  #msg.ok { background: var(--kinari); color: var(--muji-red); border: 1px solid var(--beige); display: block; }
  .row { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; margin: 1rem 0; }

  /* API キーカード（Level 1: subtle elevation） */
  .card { background: var(--white); border: 1px solid var(--gray-200); border-radius: var(--radius);
    padding: 1.1rem 1.25rem; box-shadow: 0 1px 3px rgba(60,60,67,.08); margin-top: 1rem; }
  .keybox { display: flex; align-items: center; gap: .6rem; margin: .5rem 0; flex-wrap: wrap; }
  .keybox code { flex: 1; min-width: 12rem; background: var(--gray-100);
    padding: .55rem .7rem; border-radius: var(--radius); border: 1px solid var(--gray-200); }
  .keyval { letter-spacing: .05em; }
  .badge { font-size: .72rem; border-radius: var(--radius); padding: .1rem .5rem; letter-spacing: .02em; }
  .badge.cur { background: var(--muji-red); color: #fff; }
  .badge.active { background: var(--kinari); color: var(--muji-red); border: 1px solid var(--beige); }
  .badge.inactive { background: var(--gray-200); color: var(--text-tertiary); }

  /* コスト月次/週次の切替タブ */
  .tabs { display: flex; gap: .3rem; margin-top: 1rem; border-bottom: 1px solid var(--gray-200); }
  .tab { background: none; border: none; border-bottom: 2px solid transparent; border-radius: 0;
    padding: .5rem .9rem; color: var(--text-secondary); font-weight: 600; margin-bottom: -1px; }
  .tab:hover { background: var(--gray-100); }
  .tab.active { color: var(--muji-red); border-bottom-color: var(--muji-red); }
  /* 週スライド（過去4週間の切替）のナビ */
  .weeknav { display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: .5rem; }
  .weeknav .label { font-weight: 700; color: var(--text-primary); }
  .weeknav .label .sub { font-weight: 400; color: var(--text-tertiary); font-size: .82rem; margin-left: .4rem; }
  .weeknav button { padding: .3rem .8rem; }

  /* セットアップ手順のリンク集 */
  ul.docs { list-style: none; padding: 0; margin: 1rem 0 0; }
  ul.docs li { padding: .55rem 0; border-bottom: 1px solid var(--gray-200); font-size: .9rem; }
  ul.docs a { color: var(--muji-red); text-decoration: none; font-weight: 600; }
  ul.docs a:hover { text-decoration: underline; }
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <h1>利用者プロファイル管理</h1>
      <div class="muted" style="font-size:.8rem;margin-top:.2rem">Claude on Bedrock — 国内完結 PoC</div>
    </div>
    <div id="who" class="who"></div>
  </div>
</header>

<div class="wrap">
<p class="lead">利用者ごとのコスト配賦用アプリケーション推論プロファイル（<code>cc-&lt;user&gt;-opus</code> /
<code>cc-&lt;user&gt;-haiku</code>）を管理します。作成すると Opus 4.8 と Haiku 4.5 の 2 本が
<code>user</code> / <code>app=claude-code</code> / <code>model</code> タグ付きで作られます。</p>

<!-- #msg は signin/app どちらの画面でも見えるよう外に置く（初期化・サインイン失敗も表示するため） -->
<div id="msg"></div>

<div id="signin" style="display:none">
  <button class="primary" onclick="signIn()">EntraID でサインイン</button>
</div>

<div id="app" style="display:none">
  <div class="section">
    <h2>Bedrock API キー</h2>
    <p class="muted">週次ローテーションで発行される現行キーと、IAM 上の新旧クレデンシャルの状態です。
      キー本文をクリックするとコピーできます。旧キー本文は保管されないため一覧のみ表示します。</p>
    <div id="apikey" class="card"><span class="muted">読み込み中…</span></div>
  </div>

  <div class="section">
    <h2>利用者プロファイル</h2>
    <div class="row">
      <input id="user" placeholder="利用者名（例: takeshi.ohno）" size="28">
      <button class="primary" id="createBtn" onclick="createProfiles()">作成</button>
      <button class="reload" id="reloadBtn" onclick="reloadAll()">再読み込み</button>
    </div>
    <table>
      <thead><tr><th>利用者</th><th>プロファイル ARN（クリックでコピー）</th><th></th></tr></thead>
      <tbody id="rows"><tr><td colspan="3" class="muted">読み込み中…</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <h2>利用者別コスト</h2>
    <p class="muted">Cost Explorer のタグ配賦（<code>app=claude-code</code> を <code>user</code> で集計）による実コストです。
      課金反映は最大 24 時間遅れます。Zed 組み込みモデルなどタグなしの呼び出しは含まれません。</p>
    <div class="tabs">
      <button type="button" class="tab active" id="tabMonthly" onclick="switchCostTab('monthly')">月次（今月）</button>
      <button type="button" class="tab" id="tabWeekly" onclick="switchCostTab('weekly')">週次（過去4週間）</button>
    </div>
    <div id="cost" class="card"><span class="muted">読み込み中…</span></div>
  </div>

  <div class="section">
    <h2>セットアップ手順</h2>
    <p class="muted">各エディタの初回セットアップ手順です。上のキー・ARN をコピーして貼り付けてください。</p>
    <ul class="docs">
      <li><a href="https://github.com/Challenge-Consulting-Firm/editor-claude-bedrock/blob/main/docs/setup-claude-code.md" target="_blank" rel="noopener">Claude Code CLI</a>
        <span class="muted">— ANTHROPIC_MODEL に Opus の ARN、ANTHROPIC_SMALL_FAST_MODEL / ANTHROPIC_DEFAULT_HAIKU_MODEL に Haiku の ARN</span></li>
      <li><a href="https://github.com/Challenge-Consulting-Firm/editor-claude-bedrock/blob/main/docs/setup-vscode.md" target="_blank" rel="noopener">VS Code（Claude Code 拡張）</a>
        <span class="muted">— 環境変数 AWS_BEARER_TOKEN_BEDROCK にキー、ANTHROPIC_MODEL に Opus の ARN</span></li>
      <li><a href="https://github.com/Challenge-Consulting-Firm/editor-claude-bedrock/blob/main/docs/setup-zed.md" target="_blank" rel="noopener">Zed</a>
        <span class="muted">— 設定の Bedrock API Key 欄にキー、available_models の name に Opus の ARN</span></li>
    </ul>
  </div>
</div>
</div>

<script>
// MSAL のグローバルは `msal`（CDN 提供）。account はサインイン中のアカウント
let account;

// MSAL 初期化 → サインイン状態に応じて UI 切替
async function init() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    window._msal = new msal.PublicClientApplication({
      auth: {
        clientId: cfg.clientId,
        authority: "https://login.microsoftonline.com/" + cfg.tenantId,
        redirectUri: window.location.origin + window.location.pathname,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
    window._scopes = ["api://" + cfg.clientId + "/access_as_user"];
    await window._msal.initialize();
    // リダイレクトから戻ってきた場合はここでトークンを受け取る。
    // 途中で失敗（例: 返信URL未登録）した場合、interaction_in_progress が
    // sessionStorage に残り次の loginRedirect が無反応になるため必ず握る。
    const resp = await window._msal.handleRedirectPromise();
    if (resp && resp.account) account = resp.account;
    else {
      const accts = window._msal.getAllAccounts();
      if (accts.length) account = accts[0];
    }
    render();
  } catch (e) {
    console.error("init/handleRedirect 失敗:", e);
    showMsg("初期化またはサインインの復帰に失敗: " + (e.errorCode || "") + " " + e.message, false);
    // 中断状態が残っているとボタンが無反応になるので、キャッシュを掃除して再サインインできるようにする
    render();
  }
}

function render() {
  if (account) {
    document.getElementById("who").textContent = account.username;
    document.getElementById("signin").style.display = "none";
    document.getElementById("app").style.display = "block";
    reloadAll();
  } else {
    document.getElementById("signin").style.display = "block";
    document.getElementById("app").style.display = "none";
  }
}

function reloadAll() {
  loadApiKey();
  loadProfiles();
  loadCost();
}

// クリップボードコピー（要素の data-copy を使う。フォールバックあり）。
async function copyText(text, el) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
    }
    if (el) {
      const hint = document.createElement("span");
      hint.className = "copied"; hint.textContent = "コピーしました";
      el.after(hint);
      setTimeout(() => hint.remove(), 1500);
    }
  } catch (e) {
    showMsg("コピーに失敗しました: " + e.message, false);
  }
}

async function signIn() {
  try {
    await window._msal.loginRedirect({ scopes: window._scopes });
  } catch (e) {
    console.error("loginRedirect 失敗:", e);
    // interaction_in_progress が前回の中断で残ると loginRedirect が黙って失敗する。
    // セッションキャッシュを消して再試行できる状態に戻す。
    if (e.errorCode === "interaction_in_progress") {
      sessionStorage.clear();
      showMsg("前回のサインインが中断されていました。もう一度ボタンを押してください。", false);
    } else {
      showMsg("サインイン開始に失敗: " + (e.errorCode || "") + " " + e.message, false);
    }
  }
}

async function token() {
  try {
    const r = await window._msal.acquireTokenSilent({ scopes: window._scopes, account });
    return r.accessToken;
  } catch (e) {
    await window._msal.acquireTokenRedirect({ scopes: window._scopes, account });
    throw e;
  }
}

function showMsg(text, ok) {
  const m = document.getElementById("msg");
  m.textContent = text;
  m.className = ok ? "ok" : "err";
}

async function api(method, body, path) {
  const t = await token();
  const r = await fetch(path || "/api/profiles", {
    method,
    headers: { Authorization: "Bearer " + t, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 現行キー本文 + 新旧 credential のメタ一覧を描画
async function loadApiKey() {
  const box = document.getElementById("apikey");
  box.innerHTML = '<span class="muted">読み込み中…</span>';
  try {
    const data = await api("GET", null, "/api/apikey");
    let html = "";
    if (data.current) {
      html +=
        '<div class="keybox">' +
        '<span class="badge cur">現行キー本文</span>' +
        '<code class="keyval copy" title="クリックでコピー" id="curkey">' + esc(data.current) + "</code>" +
        '<button class="reload" type="button" id="copyKeyBtn">コピー</button>' +
        "</div>";
    } else {
      html += '<p class="muted">現行キー本文は未保管です（初回ローテーション前の可能性）。</p>';
    }
    const creds = data.credentials || [];
    if (creds.length) {
      html += '<table style="margin-top:.75rem"><thead><tr>' +
        "<th>クレデンシャル ID</th><th>状態</th><th>作成</th><th>有効期限</th></tr></thead><tbody>";
      for (const c of creds) {
        const st = (c.status || "").toLowerCase();
        const badge = c.current
          ? '<span class="badge cur">現行</span>'
          : st === "active"
            ? '<span class="badge active">Active</span>'
            : '<span class="badge inactive">' + esc(c.status || "-") + "</span>";
        html +=
          "<tr><td><code>" + esc(c.id) + "</code></td>" +
          "<td>" + badge + "</td>" +
          "<td>" + esc(fmtDate(c.createDate)) + "</td>" +
          "<td>" + esc(fmtDate(c.expiration)) + "</td></tr>";
      }
      html += "</tbody></table>";
    } else {
      html += '<p class="muted">クレデンシャルがありません。</p>';
    }
    box.innerHTML = html;
    const cur = document.getElementById("curkey");
    if (cur) {
      cur.onclick = () => copyText(data.current, cur);
      document.getElementById("copyKeyBtn").onclick = () => copyText(data.current, cur);
    }
  } catch (e) {
    box.innerHTML = '<span class="muted">—</span>';
    showMsg("API キーの読み込み失敗: " + e.message, false);
  }
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

// コストの表示単位（monthly / weekly）と、週次の表示中インデックス（0=直近週）
let costRange = "monthly";
let weeklyData = null;
let weekIndex = 0;

function switchCostTab(range) {
  if (costRange === range) return;
  costRange = range;
  document.getElementById("tabMonthly").classList.toggle("active", range === "monthly");
  document.getElementById("tabWeekly").classList.toggle("active", range === "weekly");
  loadCost();
}

// 利用者別コストの明細テーブル（月次・週次で共通）
function renderCostTable(data, cur) {
  const money = (v) => cur + Number(v).toFixed(2);
  let html = '<table style="margin-top:.5rem"><thead><tr><th>利用者</th><th style="text-align:right">コスト</th></tr></thead><tbody>';
  if (data.users && data.users.length) {
    for (const u of data.users) {
      html += "<tr><td><strong>" + esc(u.user) + "</strong></td>" +
        '<td style="text-align:right">' + esc(money(u.amount)) + "</td></tr>";
    }
  } else {
    html += '<tr><td colspan="2" class="muted">配賦対象のコストはまだありません</td></tr>';
  }
  if (data.unallocated > 0) {
    html += '<tr><td class="muted">(未配賦)</td>' +
      '<td class="muted" style="text-align:right">' + esc(money(data.unallocated)) + "</td></tr>";
  }
  html += "<tr><td><strong>合計</strong></td>" +
    '<td style="text-align:right"><strong>' + esc(money(data.total)) + "</strong></td></tr>";
  html += "</tbody></table>";
  if (data.unallocated > 0) {
    html += '<p class="muted" style="margin-bottom:0">※ (未配賦) = user タグ有効化前・課金反映前（最大24h）・タグなし呼び出し（Zed 組み込みモデル等）の合算</p>';
  }
  return html;
}

// 選択中のタブに応じて月次 or 週次を読み込んで描画
async function loadCost() {
  const box = document.getElementById("cost");
  box.innerHTML = '<span class="muted">読み込み中…</span>';
  try {
    if (costRange === "weekly") {
      weeklyData = await api("GET", null, "/api/cost?range=weekly");
      weekIndex = 0;  // 読み込み直後は直近週を表示
      renderWeekly();
    } else {
      const data = await api("GET", null, "/api/cost");
      const cur = data.currency === "USD" ? "$" : "";
      let html = '<p class="muted" style="margin-top:0">対象月: ' + esc(data.month) + "</p>";
      html += renderCostTable(data, cur);
      box.innerHTML = html;
    }
  } catch (e) {
    box.innerHTML = '<span class="muted">—</span>';
    showMsg("コストの読み込み失敗: " + e.message, false);
  }
}

// 週次: 過去4週間をスライド式に 1 週ずつ表示（◀ 過去 / 未来 ▶）
function renderWeekly() {
  const box = document.getElementById("cost");
  const weeks = (weeklyData && weeklyData.weeks) || [];
  if (!weeks.length) {
    box.innerHTML = '<p class="muted">週次の配賦対象コストはまだありません</p>';
    return;
  }
  if (weekIndex < 0) weekIndex = 0;
  if (weekIndex > weeks.length - 1) weekIndex = weeks.length - 1;
  const w = weeks[weekIndex];
  const cur = weeklyData.currency === "USD" ? "$" : "";
  // weeks は新しい順（index 0 = 直近週）。◀ は過去へ（index++）、▶ は未来へ（index--）
  const olderDisabled = weekIndex >= weeks.length - 1 ? "disabled" : "";
  const newerDisabled = weekIndex <= 0 ? "disabled" : "";
  const posLabel = weekIndex === 0 ? "今週" : weekIndex + " 週前";
  let html = '<div class="weeknav">' +
    '<button type="button" id="weekOlder" ' + olderDisabled + '>◀ 過去</button>' +
    '<span class="label">' + esc(w.weekStart) + " 〜 " + esc(w.weekEnd) +
    '<span class="sub">' + esc(posLabel) + "</span></span>" +
    '<button type="button" id="weekNewer" ' + newerDisabled + ">未来 ▶</button>" +
    "</div>";
  html += renderCostTable(w, cur);
  box.innerHTML = html;
  const older = document.getElementById("weekOlder");
  const newer = document.getElementById("weekNewer");
  if (older) older.onclick = () => { weekIndex++; renderWeekly(); };
  if (newer) newer.onclick = () => { weekIndex--; renderWeekly(); };
}

async function loadProfiles() {
  const rows = document.getElementById("rows");
  rows.innerHTML = '<tr><td colspan="3" class="muted">読み込み中…</td></tr>';
  try {
    const { profiles } = await api("GET");
    const users = Object.keys(profiles).sort();
    if (!users.length) {
      rows.innerHTML = '<tr><td colspan="3" class="muted">プロファイルはまだありません</td></tr>';
      return;
    }
    rows.innerHTML = "";
    for (const u of users) {
      const models = profiles[u];
      const tr = document.createElement("tr");
      const td0 = document.createElement("td");
      td0.innerHTML = "<strong>" + esc(u) + "</strong>";
      const td1 = document.createElement("td");
      Object.keys(models).sort().forEach((m) => {
        const span = document.createElement("span");
        span.className = "arn";
        const arn = models[m].arn;
        span.innerHTML = '<span class="tag">' + esc(m) + "</span>" +
          '<code class="copy" title="クリックでコピー">' + esc(arn) + "</code>";
        const codeEl = span.querySelector("code");
        codeEl.onclick = () => copyText(arn, codeEl);
        td1.appendChild(span);
      });
      const td2 = document.createElement("td");
      const del = document.createElement("button");
      del.className = "danger"; del.textContent = "削除";
      del.onclick = () => removeProfiles(u);
      td2.appendChild(del);
      tr.append(td0, td1, td2);
      rows.appendChild(tr);
    }
  } catch (e) {
    showMsg("読み込み失敗: " + e.message, false);
    rows.innerHTML = '<tr><td colspan="3" class="muted">—</td></tr>';
  }
}

async function createProfiles() {
  const user = document.getElementById("user").value.trim().toLowerCase();
  if (!user) { showMsg("利用者名を入力してください", false); return; }
  const btn = document.getElementById("createBtn");
  btn.disabled = true;
  try {
    await api("POST", { user });
    showMsg("作成しました: " + user, true);
    document.getElementById("user").value = "";
    await loadProfiles();
  } catch (e) {
    showMsg("作成失敗: " + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function removeProfiles(user) {
  if (!confirm(user + " のプロファイルを削除します。よろしいですか？")) return;
  try {
    const { deleted } = await api("DELETE", { user });
    showMsg("削除しました: " + user + "（" + deleted + " 件）", true);
    await loadProfiles();
  } catch (e) {
    showMsg("削除失敗: " + e.message, false);
  }
}

init();
</script>
</body>
</html>
"""
