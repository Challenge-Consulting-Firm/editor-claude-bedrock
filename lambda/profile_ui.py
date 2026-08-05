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
  それ以外の GET              SPA(HTML) を返す
"""

import json
import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

from entra_auth import AuthError, verify_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_ID = os.environ["ENTRA_TENANT_ID"]
CLIENT_ID = os.environ["ENTRA_CLIENT_ID"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
APP_TAG_VALUE = os.environ.get("USER_PROFILE_APP_TAG", "claude-code")

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
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 880px; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }
  h1 { font-size: 1.4rem; }
  .muted { color: #888; font-size: .9rem; }
  button { font: inherit; padding: .4rem .8rem; border-radius: 6px; border: 1px solid #8888; cursor: pointer; background: #f5f5f5; }
  button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
  button.danger { background: #fff; color: #dc2626; border-color: #dc2626; }
  button:disabled { opacity: .5; cursor: default; }
  input { font: inherit; padding: .4rem .6rem; border-radius: 6px; border: 1px solid #8888; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #8883; padding: .5rem; text-align: left; font-size: .9rem; vertical-align: top; }
  th { background: #8881; }
  code { font-size: .82rem; word-break: break-all; }
  .arn { display: block; margin: .1rem 0; }
  .tag { font-size: .75rem; color: #888; }
  #msg { padding: .6rem 1rem; border-radius: 6px; margin: 1rem 0; display: none; }
  #msg.err { background: #fee; color: #b00; display: block; }
  #msg.ok { background: #efe; color: #060; display: block; }
  .row { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin: 1rem 0; }
  header { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
</style>
</head>
<body>
<header>
  <h1>利用者プロファイル管理</h1>
  <div id="who" class="muted"></div>
</header>
<p class="muted">利用者ごとのコスト配賦用アプリケーション推論プロファイル（<code>cc-&lt;user&gt;-opus</code> /
<code>cc-&lt;user&gt;-haiku</code>）を管理します。作成すると Opus 4.8 と Haiku 4.5 の 2 本が
<code>user</code> / <code>app=claude-code</code> / <code>model</code> タグ付きで作られます。</p>

<!-- #msg は signin/app どちらの画面でも見えるよう外に置く（初期化・サインイン失敗も表示するため） -->
<div id="msg"></div>

<div id="signin" style="display:none">
  <button class="primary" onclick="signIn()">EntraID でサインイン</button>
</div>

<div id="app" style="display:none">
  <div class="row">
    <input id="user" placeholder="利用者名（例: takeshi.ohno）" size="28">
    <button class="primary" id="createBtn" onclick="createProfiles()">作成</button>
    <button id="reloadBtn" onclick="loadProfiles()">再読み込み</button>
  </div>
  <table>
    <thead><tr><th>利用者</th><th>プロファイル ARN</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="3" class="muted">読み込み中…</td></tr></tbody>
  </table>
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
    loadProfiles();
  } else {
    document.getElementById("signin").style.display = "block";
    document.getElementById("app").style.display = "none";
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

async function api(method, body) {
  const t = await token();
  const r = await fetch("/api/profiles", {
    method,
    headers: { Authorization: "Bearer " + t, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
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
      const arns = Object.keys(models).sort().map(
        (m) => '<span class="arn"><span class="tag">' + m + '</span> <code>' + models[m].arn + '</code></span>'
      ).join("");
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td><strong>" + u + "</strong></td>" +
        "<td>" + arns + "</td>" +
        '<td><button class="danger" onclick="removeProfiles(\\'' + u + '\\')">削除</button></td>';
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
