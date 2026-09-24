"""週次利用状況レポート（トークン消費量 + 概算費用 + 実コスト）を Teams へ投稿。

EventBridge Scheduler から毎週起動され:
  1. CloudWatch Metrics（AWS/Bedrock）からモデル別の入出力・キャッシュ読み書きトークン数を
     集計（REPORT_DAYS 日分）
  2. トークン×単価で概算費用を算出（単価設定済みのモデルのみ）。
     キャッシュ課金（書込 1.25× / 読取 0.1× 入力単価）も込み。実測（2026-09-03）では
     この式が CE 実コストと一致する（2026-08-24〜08-30: 概算 $291.17 = 実コスト $291.17）
  3. Cost Explorer からタグ配賦された実コスト（週次 + 月次累計）を取得
  4. Teams へ週次レポートを投稿

設計（design.md §6）:
  - トークン数は CloudWatch Metrics が全呼出（Zed 組み込みモデル含む）を捕捉する一次情報源。
    ただし CW の InputTokenCount にはキャッシュトークンが含まれず、課金の大半が
    キャッシュ（実績: 週の 78%）のため CacheRead/CacheWriteInputTokenCount も集計する
  - metric_ids は MODELS_JSON の静的定義（過去分のフォールバック）に加え、実行時に
    Bedrock API で app=claude-code のアプリ推論プロファイル（cc-<user>-<model>）を列挙して
    model タグ経由で自動併合する。ポータル（profile_ui）や手動追加での増分の取りこぼし防止
  - 実コストは Cost Explorer のタグ（app=claude-code）配賦分。Zed 組み込みモデル等の
    タグなし呼出は含まれない（= 概算と実コストの差の要因）

前提:
  - コスト配分タグ app / user を事前に有効化しておくこと（design.md §6 の手順）
  - Teams 投稿失敗は関数ごと失敗させ、EventBridge Scheduler のリトライ/検知に乗せる（rotate_key と同じ方針）
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from teams import post_teams

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEBHOOK_PARAM = os.environ["WEBHOOK_PARAM"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AWS/Bedrock")
COST_TAG_KEY = os.environ.get("COST_TAG_KEY", "Project")
COST_TAG_VALUE = os.environ.get("COST_TAG_VALUE", "editor-claude-bedrock")
# 利用者別内訳用: app タグで絞り込み、user タグでグループ化する（setup-claude-code.md §0.5 の per-user プロファイル）。
# user タグの有効化以前・課金反映前のデータは空値（=未配賦）に寄るため、その分は「(未配賦)」として表示する。
USER_TAG_KEY = os.environ.get("USER_TAG_KEY", "user")
USER_APP_TAG_KEY = os.environ.get("USER_APP_TAG_KEY", "app")
USER_APP_TAG_VALUE = os.environ.get("USER_APP_TAG_VALUE", "claude-code")
REPORT_DAYS = int(os.environ.get("REPORT_DAYS", "7"))
MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "0"))

cw = boto3.client("cloudwatch")
ce = boto3.client("ce")
ssm = boto3.client("ssm")
# metric_ids 動的併合用（app=claude-code のプロファイル列挙）。profile_ui と同じパターン
bedrock = boto3.client("bedrock", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))

# プロンプトキャッシュの課金倍率（入力単価に対する倍数）。
# 実測（2026-09-03、CE USAGE_TYPE 別単価を CW トークン数で逆算）で確認。
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


def load_models():
    """MODELS_JSON をパース。形式: [{"name","model_tag","metric_ids":[...],"in_price":float|None,"out_price":float|None}]"""
    return json.loads(os.environ.get("MODELS_JSON", "[]"))


def discover_profile_metric_ids() -> dict:
    """app=claude-code のアプリ推論プロファイルを列挙し {modelタグ値: [profileId, ...]} を返す。

    ポータル（profile_ui）や手動作成で増えた cc-<user>-<model> を集計対象に自動で含めるためのもの。
    Bedrock API 失敗時は空 dict を返し、MODELS_JSON の静的 metric_ids（フォールバック）のみで集計する。
    """
    result: dict = {}
    try:
        paginator = bedrock.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for profile in page.get("inferenceProfileSummaries", []):
                arn = profile["inferenceProfileArn"]
                tags = {
                    t["key"]: t["value"]
                    for t in bedrock.list_tags_for_resource(resourceARN=arn).get("tags", [])
                }
                if tags.get("app") != USER_APP_TAG_VALUE:
                    continue
                model = tags.get("model")
                if model:
                    result.setdefault(model, []).append(profile["inferenceProfileId"])
    except ClientError as exc:
        logger.warning(
            "プロファイル動的列挙に失敗（MODELS_JSON の静的 metric_ids のみで集計）: %s", exc
        )
        return {}
    return result


def augment_metric_ids(models) -> list:
    """MODELS_JSON 各モデルの metric_ids に、動的発見した cc-* プロファイル ID を併合する。

    model タグ（"opus" / "sonnet" / "haiku" / "opus-5-5" / "opus-5" 等）が対応キー。
    完全一致で併合し、`opus-5` と `opus-5-5` のような前方一致する名前も混在させない。
    併合は重複除去つき。MODELS_JSON に対応行のない model タグ（新モデルのポータル追加など）は警告を出す。
    """
    discovered = discover_profile_metric_ids()
    if not discovered:
        return models
    known_tags = {m.get("model_tag") for m in models}
    for tag, ids in sorted(discovered.items()):
        if tag not in known_tags:
            logger.warning(
                "app=%s で model=%s タグのプロファイルに MODELS_JSON の対応行がありません（集計外）: %s",
                USER_APP_TAG_VALUE,
                tag,
                ",".join(ids),
            )
    for m in models:
        extra = discovered.get(m.get("model_tag"), [])
        if extra:
            m["metric_ids"] = list(dict.fromkeys(list(m.get("metric_ids", [])) + extra))
            logger.info("%s: 動的発見したプロファイル %d 件を集計対象に追加", m.get("name"), len(extra))
    return models


def get_token_totals(model_id, start, end):
    """指定期間の ModelId=model_id のトークン合計を (入力, 出力, キャッシュ読取, キャッシュ書込) で返す。

    CW の InputTokenCount にはキャッシュトークンが含まれず、課金の大半がキャッシュ分のため
    CacheRead/CacheWriteInputTokenCount も別途取得する（Period=1日で取得して合算）。
    """
    totals = {}
    for metric_name, key in (
        ("InputTokenCount", "input"),
        ("OutputTokenCount", "output"),
        ("CacheReadInputTokenCount", "cache_read"),
        ("CacheWriteInputTokenCount", "cache_write"),
    ):
        resp = cw.get_metric_statistics(
            Namespace=METRIC_NAMESPACE,
            MetricName=metric_name,
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
        totals[key] = sum(dp.get("Sum", 0) for dp in resp.get("Datapoints", []))
    return totals["input"], totals["output"], totals["cache_read"], totals["cache_write"]


def get_cost(start_date, end_date):
    """Cost Explorer からタグ配賦コスト（USD）を取得。失敗時は None（実コストはオプション扱い）。"""
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": COST_TAG_KEY, "Values": [COST_TAG_VALUE]}},
        )
        return sum(float(r["Total"]["UnblendedCost"]["Amount"]) for r in resp.get("ResultsByTime", []))
    except ClientError as exc:
        logger.warning("Cost Explorer 取得失敗（未有効化/権限の可能性）: %s", exc)
        return None


def get_cost_by_user(start_date, end_date):
    """app=claude-code の実コストを user タグでグループ化し {user: usd} を返す。失敗時は None。

    user タグが空（未配賦: タグ有効化前・課金反映前・タグなし呼出）の分は "" キーに集約する。
    """
    try:
        by_user: dict[str, float] = {}
        token = None
        while True:
            kwargs = dict(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Tags": {"Key": USER_APP_TAG_KEY, "Values": [USER_APP_TAG_VALUE]}},
                GroupBy=[{"Type": "TAG", "Key": USER_TAG_KEY}],
            )
            if token:
                kwargs["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**kwargs)
            for period in resp.get("ResultsByTime", []):
                for grp in period.get("Groups", []):
                    # Keys は ["user$takeshi.ohno"] のように "<tagkey>$<value>" 形式。空値は "user$"
                    raw = grp["Keys"][0]
                    user = raw.split("$", 1)[1] if "$" in raw else raw
                    amount = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                    by_user[user] = by_user.get(user, 0.0) + amount
            token = resp.get("NextPageToken")
            if not token:
                break
        return by_user
    except ClientError as exc:
        logger.warning("利用者別コスト取得失敗（user/app タグ未有効化の可能性）: %s", exc)
        return None


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_usd(v):
    return f"${v:.2f}"


def build_user_cost_lines(cost_by_user):
    """利用者別コストの Markdown 行を組む。取得失敗/空なら注記のみ返す。"""
    lines = ["**■ 利用者別コスト**（Cost Explorer・`app=claude-code` を `user` タグで集計）", ""]
    if cost_by_user is None:
        lines += ["取得失敗（`user` / `app` コスト配分タグ未有効化または権限不足の可能性）", ""]
        return lines
    if not cost_by_user:
        lines += ["データなし（期間中の配賦対象コストなし）", ""]
        return lines
    lines += ["| 利用者 | コスト |", "|:--|--:|"]
    # 未配賦（空 user）は末尾にまとめ、それ以外は金額降順
    unallocated = cost_by_user.get("", 0.0)
    named = {u: v for u, v in cost_by_user.items() if u != ""}
    for user, usd in sorted(named.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {user} | {fmt_usd(usd)} |")
    if unallocated > 0:
        lines.append(f"| (未配賦) | {fmt_usd(unallocated)} |")
    lines.append(f"| **合計** | **{fmt_usd(sum(cost_by_user.values()))}** |")
    lines.append("")
    if unallocated > 0:
        lines.append(
            "※ (未配賦) = `user` タグ有効化前・課金反映前（最大24h）・タグなし呼出（Zed 組み込みモデル等）の合算"
        )
    return lines


def build_message(period_label, rows, weekly_cost, mtd_cost, cost_by_user=None):
    # Teams（Power Automate 経由）は webhook の text を Markdown 描画する:
    #   - 単独 \n はスペースに潰れる（＝ソフト改行）
    #   - 空行(\n\n) は段落区切りとして効く
    #   - コードブロック(```)は非対応（文字のまま出る）
    # したがって空白での桁揃えは不可能。整列は Markdown テーブル、改行は空行で組む。
    lines = [
        f"**【エディタ用 Claude (Bedrock)】週次利用状況レポート（{period_label}）**",
        "",
        "**■ トークン消費量**（CloudWatch Metrics・全呼出含む）",
        "",
        "| モデル | 入力 | 出力 | キャッシュ読取 | キャッシュ書込 | 概算費用 |",
        "|:--|--:|--:|--:|--:|:--|",
    ]
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    est_total = 0.0
    est_has_any = False
    unpriced = []
    for row in rows:
        ti, to = row["input"], row["output"]
        tcr, tcw = row["cache_read"], row["cache_write"]
        total_input += ti
        total_output += to
        total_cache_read += tcr
        total_cache_write += tcw
        if row["in_price"] is not None and row["out_price"] is not None:
            # キャッシュ課金込み: 書込は入力単価の 1.25倍、読取は 0.1倍
            est = (
                ti / 1_000_000 * row["in_price"]
                + to / 1_000_000 * row["out_price"]
                + tcw / 1_000_000 * row["in_price"] * _CACHE_WRITE_MULTIPLIER
                + tcr / 1_000_000 * row["in_price"] * _CACHE_READ_MULTIPLIER
            )
            est_total += est
            est_has_any = True
            cost_str = fmt_usd(est)
        else:
            cost_str = "未設定"
            unpriced.append(row["name"])
        lines.append(
            f"| {row['name']} | {fmt_tokens(ti)} | {fmt_tokens(to)} | {fmt_tokens(tcr)} | {fmt_tokens(tcw)} | {cost_str} |"
        )
    total_est = fmt_usd(est_total) if est_has_any else "—"
    lines.append(
        f"| **合計** | **{fmt_tokens(total_input)}** | **{fmt_tokens(total_output)}** | "
        f"**{fmt_tokens(total_cache_read)}** | **{fmt_tokens(total_cache_write)}** | **{total_est}** |"
    )
    if unpriced:
        lines += [
            "",
            f"※ 概算費用は単価設定済みモデルのみ（{', '.join(unpriced)} は単価未設定のため未計上）。"
            "全モデルの実額は下記「実コスト」を参照",
        ]

    lines += [
        "",
        "**■ 実コスト**（Cost Explorer・タグ配賦分）",
        "",
    ]
    if weekly_cost is not None:
        weekly_line = f"期間中: {fmt_usd(weekly_cost)}"
    else:
        weekly_line = "期間中: 取得失敗（Cost Explorer 未有効化または権限不足の可能性）"
    if mtd_cost is not None:
        if MONTHLY_BUDGET_USD > 0:
            pct = mtd_cost / MONTHLY_BUDGET_USD * 100
            mtd_line = f"今月累計: {fmt_usd(mtd_cost)}（月次予算 {fmt_usd(MONTHLY_BUDGET_USD)} の {pct:.0f}%）"
        else:
            mtd_line = f"今月累計: {fmt_usd(mtd_cost)}"
    else:
        mtd_line = "今月累計: 取得失敗"
    # 空行で段落区切りを入れ、各行が確実に別行になるようにする
    lines += ["", weekly_line, "", mtd_line, ""]
    lines.append(
        "⚠️ 概算費用はトークン×単価＋キャッシュ課金（読取 0.1×・書込 1.25×入力単価）の参考値。"
        "実コストとの差は集計境界（メトリクスは直近168h vs CE は日次）と CE の最大24h反映遅延による。"
        "実コスト・利用者別コストは app=claude-code タグ配賦分のみで、"
        "タグなし呼出（Zed 組み込みモデル等）は含まれない"
    )

    # 利用者別内訳（期間中 = 直近 REPORT_DAYS 日）
    lines += ["", *build_user_cost_lines(cost_by_user)]
    return "\n".join(lines)


def handler(event, context):  # noqa: ARG001
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=REPORT_DAYS)
    # Cost Explorer の TimePeriod.End は排他（当日含むには翌日を指定）
    ce_start = start.strftime("%Y-%m-%d")
    ce_end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1-2. モデル別トークン集計（入出力 + キャッシュ読み書き）+ 概算費用。
    # metric_ids は MODELS_JSON 静的定義 + 実行時の app=claude-code プロファイル動的列挙の併合
    rows = []
    for m in augment_metric_ids(load_models()):
        ti_sum = to_sum = tcr_sum = tcw_sum = 0
        for mid in m.get("metric_ids", []):
            ti, to, tcr, tcw = get_token_totals(mid, start, now)
            ti_sum += ti
            to_sum += to
            tcr_sum += tcr
            tcw_sum += tcw
        rows.append({
            "name": m["name"],
            "input": ti_sum,
            "output": to_sum,
            "cache_read": tcr_sum,
            "cache_write": tcw_sum,
            "in_price": m.get("in_price"),
            "out_price": m.get("out_price"),
        })

    # 3. 実コスト（週次 + 月次累計）
    weekly_cost = get_cost(ce_start, ce_end)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd_cost = get_cost(month_start.strftime("%Y-%m-%d"), ce_end)

    # 3b. 利用者別内訳（期間中 = 直近 REPORT_DAYS 日）
    cost_by_user = get_cost_by_user(ce_start, ce_end)

    # 4. Teams 投稿（失敗したら関数ごと失敗させる）
    message = build_message(f"{ce_start}〜{now.strftime('%Y-%m-%d')}", rows, weekly_cost, mtd_cost, cost_by_user)
    webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    post_teams(webhook_url, message)
    logger.info("週次レポート投稿完了:\n%s", message)
    return {"posted": True, "models": len(rows)}
