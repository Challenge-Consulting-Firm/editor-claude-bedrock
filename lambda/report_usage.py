"""週次利用状況レポート（トークン消費量 + 概算費用 + 実コスト）を Teams へ投稿。

EventBridge Scheduler から毎週起動され:
  1. CloudWatch Metrics（AWS/Bedrock）からモデル別の入出力・キャッシュ読み書きトークン数を
     「期間中（REPORT_DAYS 日）」と「今月累計（月初から）」の両方で集計
  2. トークン×単価で概算費用を算出（単価設定済みのモデルのみ）。
     キャッシュ課金（書込 1.25× / 読取 0.1× 入力単価）も込み。実測（2026-09-03）では
     この式が CE 実コストと一致する（2026-08-24〜08-30: 概算 $291.17 = 実コスト $291.17）
  3. Cost Explorer からタグ配賦された実コスト（期間中 + 月次累計）と利用者別内訳を取得
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
# 実コストと利用者別内訳は、既定でも同じ app=claude-code の母集団に揃える。
# 旧既定（全体=Project / 利用者別=app）では、Terraform外で起動した際に合計が一致しない。
USER_TAG_KEY = os.environ.get("USER_TAG_KEY", "user")
USER_APP_TAG_KEY = os.environ.get("USER_APP_TAG_KEY", "app")
USER_APP_TAG_VALUE = os.environ.get("USER_APP_TAG_VALUE", "claude-code")
COST_TAG_KEY = os.environ.get("COST_TAG_KEY", USER_APP_TAG_KEY)
COST_TAG_VALUE = os.environ.get("COST_TAG_VALUE", USER_APP_TAG_VALUE)
# user タグの有効化以前・課金反映前のデータは空値（=未配賦）に寄るため、その分は「(未配賦)」として表示する。
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
_UNSET = object()


def reporting_windows(now: datetime, report_days: int = REPORT_DAYS) -> dict:
    """CloudWatch / Cost Explorer で共有する集計窓を UTC 日付境界で返す。

    Cost Explorer の End は日付単位かつ排他なので、当日を含めるには翌日を指定する。
    従来は ``now - 7日`` の日付から翌日までを CE に渡しており、実際には8暦日を
    集計し得た。翌日0時を排他末端としてそこから report_days 日戻すことで、
    「直近7日」なら常に7つのUTC日付に揃える。

    CloudWatch は未到来の翌日0時ではなく ``now`` までを取得するが、開始日はCEと同じ。
    当日分は両サービスとも反映途中になり得るため、レポートに遅延注記を表示する。
    """
    if report_days < 1:
        raise ValueError("REPORT_DAYS は1以上で指定してください")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    ce_end_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    period_start = ce_end_dt - timedelta(days=report_days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    label_end = (ce_end_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "cw_end": now,
        "period_start": period_start,
        "month_start": month_start,
        "ce_end": ce_end_dt.strftime("%Y-%m-%d"),
        "period_label": f"{period_start.strftime('%Y-%m-%d')}〜{label_end}",
        "month_label": f"{month_start.strftime('%Y-%m-%d')}〜{label_end}",
    }


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


def collect_token_rows(models, start, end) -> list:
    """モデル定義と期間から、モデル別トークン・概算単価行を作る。

    ``models`` は ``augment_metric_ids`` 済みの同一リストを期間中/月次で共有する。
    これにより、動的発見した per-user プロファイルが片方の期間だけ漏れることを防ぐ。
    """
    rows = []
    for model in models:
        ti_sum = to_sum = tcr_sum = tcw_sum = 0
        for metric_id in model.get("metric_ids", []):
            ti, to, tcr, tcw = get_token_totals(metric_id, start, end)
            ti_sum += ti
            to_sum += to
            tcr_sum += tcr
            tcw_sum += tcw
        rows.append({
            "name": model["name"],
            "input": ti_sum,
            "output": to_sum,
            "cache_read": tcr_sum,
            "cache_write": tcw_sum,
            "in_price": model.get("in_price"),
            "out_price": model.get("out_price"),
        })
    return rows


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
            kwargs = {
                "TimePeriod": {"Start": start_date, "End": end_date},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": {"Tags": {"Key": USER_APP_TAG_KEY, "Values": [USER_APP_TAG_VALUE]}},
                "GroupBy": [{"Type": "TAG", "Key": USER_TAG_KEY}],
            }
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


def is_display_zero_usd(value: float) -> bool:
    """2桁USD表示で $0.00 / $-0.00 になる微小額か。"""
    return fmt_usd(value) in ("$0.00", "$-0.00")


def build_user_cost_lines(cost_by_user, cost_by_user_mtd=_UNSET, period_label=""):
    """利用者別コストの Markdown 行を組む。取得失敗/空なら注記のみ返す。

    cost_by_user_mtd を渡すと「期間中」と「今月累計」を並べて表示する。
    省略した場合は従来の期間中1列を維持する。明示的な ``None`` は取得失敗として扱い、
    もう片方の期間が取得できていればその列だけを表示する。
    レポート上部の「今月累計」と同じ期間の列を並べることで、
    「利用者別の合計が今月累計と合わない」という誤解を防ぐ。
    """
    mtd_requested = cost_by_user_mtd is not _UNSET
    has_period = isinstance(cost_by_user, dict)
    has_mtd = isinstance(cost_by_user_mtd, dict)
    span = f"期間中 = {period_label}" if period_label else "期間中"
    header = "**■ 利用者別コスト**（Cost Explorer・`app=claude-code` を `user` タグで集計）"
    lines = [header, ""]
    if not has_period and not has_mtd:
        lines += ["取得失敗（`user` / `app` コスト配分タグ未有効化または権限不足の可能性）", ""]
        return lines

    period = cost_by_user if has_period else {}
    mtd = cost_by_user_mtd if has_mtd else {}
    if not period and not mtd:
        if mtd_requested and not has_period:
            lines += ["期間中コストの取得に失敗。今月累計の配賦対象コストはありません", ""]
        elif mtd_requested and not has_mtd:
            lines += ["今月累計コストの取得に失敗。期間中の配賦対象コストはありません", ""]
        else:
            lines += ["データなし（配賦対象コストなし）", ""]
        return lines

    # どちらかの期間に出てくる利用者をすべて網羅する
    # （今週未利用だが今月は使った人を落とさない）。ただし両期間とも表示上 $0.00 の
    # 検証履歴はノイズなので除外する。Cost Explorer には $0.00016 のような微小額が残るため、
    # raw値の == 0 ではなく実際の2桁表示で判定する。
    all_users = {
        user for user in (set(period) | set(mtd))
        if not (
            is_display_zero_usd(period.get(user, 0.0))
            and is_display_zero_usd(mtd.get(user, 0.0))
        )
    }

    unallocated = period.get("", 0.0)
    unallocated_mtd = mtd.get("", 0.0)
    show_unallocated = not (
        is_display_zero_usd(unallocated) and is_display_zero_usd(unallocated_mtd)
    )
    if not all_users and not show_unallocated:
        if mtd_requested and not has_period:
            lines += ["期間中コストの取得に失敗。今月累計に表示対象となる配賦コストはありません", ""]
        elif mtd_requested and not has_mtd:
            lines += ["今月累計コストの取得に失敗。期間中に表示対象となる配賦コストはありません", ""]
        else:
            lines += ["データなし（表示対象となる配賦コストなし）", ""]
        return lines

    if has_period and has_mtd:
        lines += [f"| 利用者 | {span} | 今月累計 |", "|:--|--:|--:|"]
    elif has_period:
        lines += [f"| 利用者 | {span} |", "|:--|--:|"]
    else:
        lines += ["| 利用者 | 今月累計 |", "|:--|--:|"]

    # 未配賦（空 user）は末尾にまとめ、それ以外は金額降順
    # （並び順は「今月累計ありならそちら、無ければ期間中」を主キーにする）
    named = [u for u in all_users if u != ""]
    sort_key = (lambda u: (mtd.get(u, 0.0), period.get(u, 0.0))) if has_mtd \
        else (lambda u: period.get(u, 0.0))
    for user in sorted(named, key=sort_key, reverse=True):
        if has_period and has_mtd:
            lines.append(
                f"| {user} | {fmt_usd(period.get(user, 0.0))} | {fmt_usd(mtd.get(user, 0.0))} |"
            )
        elif has_period:
            lines.append(f"| {user} | {fmt_usd(period.get(user, 0.0))} |")
        else:
            lines.append(f"| {user} | {fmt_usd(mtd.get(user, 0.0))} |")

    if show_unallocated:
        if has_period and has_mtd:
            lines.append(f"| (未配賦) | {fmt_usd(unallocated)} | {fmt_usd(unallocated_mtd)} |")
        elif has_period:
            lines.append(f"| (未配賦) | {fmt_usd(unallocated)} |")
        else:
            lines.append(f"| (未配賦) | {fmt_usd(unallocated_mtd)} |")

    if has_period and has_mtd:
        lines.append(
            f"| **合計** | **{fmt_usd(sum(period.values()))}** | **{fmt_usd(sum(mtd.values()))}** |"
        )
    elif has_period:
        lines.append(f"| **合計** | **{fmt_usd(sum(period.values()))}** |")
    else:
        lines.append(f"| **合計** | **{fmt_usd(sum(mtd.values()))}** |")
    lines.append("")
    if has_period and has_mtd:
        lines.append(
            "※ 「今月累計」列の合計は上記「実コスト › 今月累計」と一致する。"
            f"「{span}」列は集計期間が短いため少なくなる（差分は月初〜期間開始前の利用）"
        )
    elif mtd_requested and not has_period:
        lines.append("※ 期間中コストの取得に失敗したため、取得できた今月累計だけを表示")
    elif mtd_requested and not has_mtd:
        lines.append("※ 今月累計コストの取得に失敗したため、取得できた期間中だけを表示")
    if show_unallocated:
        lines.append(
            "※ (未配賦) = `user` タグ有効化前・課金反映前（最大24h）・タグなし呼出（Zed 組み込みモデル等）の合算"
        )
    return lines


def build_token_lines(title, period_label, rows) -> list:
    """モデル別トークン表と概算費用を組み立てる。"""
    lines = [
        f"**■ {title}**（CloudWatch Metrics・全呼出含む・**{period_label}**）",
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
            (
                f"※ 概算費用は単価設定済みモデルのみ（{', '.join(unpriced)} は単価未設定のため未計上）。"
                "全モデルの実額は下記「実コスト」を参照"
            ),
        ]
    return lines


def build_message(period_label, rows, weekly_cost, mtd_cost, cost_by_user=None, cost_by_user_mtd=_UNSET,
                  month_label="月初から今日", rows_mtd=None):
    # Teams（Power Automate 経由）は webhook の text を Markdown 描画する:
    #   - 単独 \n はスペースに潰れる（＝ソフト改行）
    #   - 空行(\n\n) は段落区切りとして効く
    #   - コードブロック(```)は非対応（文字のまま出る）
    # したがって空白での桁揃えは不可能。整列は Markdown テーブル、改行は空行で組む。
    #
    # ⚠️ 各セクションの見出しには必ず集計期間を明記する。
    # 期間中と今月累計を両方表示し、同じ期間同士を直接比較できるようにする。
    lines = [
        f"**【エディタ用 Claude (Bedrock)】週次利用状況レポート（{period_label}）**",
        "",
        *build_token_lines("トークン消費量（期間中）", period_label, rows),
    ]
    if rows_mtd is not None:
        lines += ["", *build_token_lines("トークン消費量（今月累計）", month_label, rows_mtd)]

    lines += [
        "",
        "**■ 実コスト**（Cost Explorer・タグ配賦分）",
        "",
    ]
    if weekly_cost is not None:
        weekly_line = f"期間中（{period_label}）: {fmt_usd(weekly_cost)}"
    else:
        weekly_line = "期間中: 取得失敗（Cost Explorer 未有効化または権限不足の可能性）"
    if mtd_cost is not None:
        if MONTHLY_BUDGET_USD > 0:
            pct = mtd_cost / MONTHLY_BUDGET_USD * 100
            mtd_line = f"今月累計（{month_label}）: {fmt_usd(mtd_cost)}（月次予算 {fmt_usd(MONTHLY_BUDGET_USD)} の {pct:.0f}%）"
        else:
            mtd_line = f"今月累計（{month_label}）: {fmt_usd(mtd_cost)}"
    else:
        mtd_line = "今月累計: 取得失敗"
    # 空行で段落区切りを入れ、各行が確実に別行になるようにする
    lines += ["", weekly_line, "", mtd_line, ""]
    lines.append(
        "⚠️ 概算費用はトークン×単価＋キャッシュ課金（読取 0.1×・書込 1.25×入力単価）の参考値。"
        "実コストとの差は、CloudWatch と CE の対象範囲（全呼出 vs タグ配賦分）・"
        "当日分の集計途中・CE の最大24h反映遅延による。"
        "実コスト・利用者別コストは app=claude-code タグ配賦分のみで、"
        "タグなし呼出（Zed 組み込みモデル等）は含まれない"
    )
    lines.append("")
    if rows_mtd is not None:
        lines.append(
            f"ℹ️ 同じ期間同士で比較する: 「トークン消費量（期間中）」↔「実コスト 期間中」= **{period_label}**、"
            f"「トークン消費量（今月累計）」↔「実コスト 今月累計」= **{month_label}**。"
            "CloudWatch は全呼出、Cost Explorer はタグ配賦分のみで、反映遅延もあるため費用は完全一致しない"
        )
    else:
        # 後方互換: 月次トークンを渡さない呼び出しでは、期間差を明示する。
        lines.append(
            f"ℹ️ 上の「トークン消費量」と「期間中」は **{period_label}** の値、"
            f"「今月累計」は **{month_label}** の値で、集計期間が異なる（不一致ではない）。"
            "両方を突き合わせるなら下記「利用者別コスト」の 2 列を見る"
        )

    # 利用者別内訳（期間中 = 直近 REPORT_DAYS 日 / 今月累計 = 月初から）
    lines += ["", *build_user_cost_lines(cost_by_user, cost_by_user_mtd, period_label)]
    return "\n".join(lines)


def handler(event, context):
    # Lambda の公開ハンドラシグネチャを維持する（キーワード呼び出しとの後方互換）。
    del event, context
    now = datetime.now(timezone.utc)
    window = reporting_windows(now)
    period_start = window["period_start"]
    month_start = window["month_start"]
    ce_start = period_start.strftime("%Y-%m-%d")
    ce_month_start = month_start.strftime("%Y-%m-%d")
    ce_end = window["ce_end"]

    # 1-2. モデル別トークン集計（入出力 + キャッシュ読み書き）+ 概算費用。
    # metric_ids は一度だけ動的発見し、期間中/月次で同じ集合を共有する。
    models = augment_metric_ids(load_models())
    rows = collect_token_rows(models, period_start, window["cw_end"])
    rows_mtd = collect_token_rows(models, month_start, window["cw_end"])

    # 3. 実コスト（期間中 + 月次累計）
    weekly_cost = get_cost(ce_start, ce_end)
    mtd_cost = get_cost(ce_month_start, ce_end)

    # 3b. 利用者別内訳。「期間中（直近 REPORT_DAYS 日）」と「今月累計」の両方を取る。
    # 後者が無いと、上部の「今月累計」と利用者別合計が一致せず「合わない」と見えてしまう。
    cost_by_user = get_cost_by_user(ce_start, ce_end)
    cost_by_user_mtd = get_cost_by_user(ce_month_start, ce_end)

    # 4. Teams 投稿（失敗したら関数ごと失敗させる）
    message = build_message(
        window["period_label"],
        rows,
        weekly_cost,
        mtd_cost,
        cost_by_user,
        cost_by_user_mtd,
        window["month_label"],
        rows_mtd,
    )
    webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    post_teams(webhook_url, message)
    logger.info("週次レポート投稿完了:\n%s", message)
    return {"posted": True, "models": len(rows)}
