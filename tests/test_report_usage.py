import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch


LAMBDA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda"))
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("WEBHOOK_PARAM", "/test/teams-webhook")

with patch("boto3.client", return_value=Mock()):
    report_usage = importlib.import_module("report_usage")


class ReportUsageTests(unittest.TestCase):
    def test_default_cost_scope_matches_user_breakdown_scope(self):
        """Terraform環境変数なしでも、全体と利用者別の母集団を app=claude-code に揃える。"""
        self.assertEqual(report_usage.COST_TAG_KEY, report_usage.USER_APP_TAG_KEY)
        self.assertEqual(report_usage.COST_TAG_VALUE, report_usage.USER_APP_TAG_VALUE)

    def test_opus_5_and_5_5_metric_ids_remain_separate_and_deduplicated(self):
        """前方一致するタグでも、国内 Opus 5.5 と global Opus 5 を混在させない。"""
        models = [
            {
                "name": "Opus 5.5",
                "model_tag": "opus-5-5",
                "metric_ids": ["jp.anthropic.claude-opus-5-5", "shared-55"],
                "in_price": None,
                "out_price": None,
            },
            {
                "name": "Opus 5 (global)",
                "model_tag": "opus-5",
                "metric_ids": ["shared-5"],
                "in_price": None,
                "out_price": None,
            },
        ]
        discovered = {
            "opus-5-5": ["shared-55", "profile-55-a", "profile-55-b"],
            "opus-5": ["shared-5", "profile-5-a"],
        }

        with patch.object(report_usage, "discover_profile_metric_ids", return_value=discovered):
            augmented = report_usage.augment_metric_ids(models)

        by_tag = {model["model_tag"]: model["metric_ids"] for model in augmented}
        self.assertEqual(
            by_tag["opus-5-5"],
            ["jp.anthropic.claude-opus-5-5", "shared-55", "profile-55-a", "profile-55-b"],
        )
        self.assertEqual(by_tag["opus-5"], ["shared-5", "profile-5-a"])
        self.assertNotIn("profile-5-a", by_tag["opus-5-5"])
        self.assertNotIn("profile-55-a", by_tag["opus-5"])

    def test_models_json_keeps_both_opus_models_as_distinct_rows(self):
        """デプロイされる MODELS_JSON でも model_tag の重複がないこと。"""
        payload = json.dumps(
            [
                {"name": "Opus 5.5", "model_tag": "opus-5-5", "metric_ids": []},
                {"name": "Opus 5 (global)", "model_tag": "opus-5", "metric_ids": []},
            ]
        )
        with patch.dict(os.environ, {"MODELS_JSON": payload}, clear=False):
            models = report_usage.load_models()

        self.assertEqual([model["model_tag"] for model in models], ["opus-5-5", "opus-5"])
        self.assertEqual(len({model["model_tag"] for model in models}), 2)

    def test_user_cost_mtd_column_totals_match_month_to_date(self):
        """利用者別の「今月累計」合計が、レポート上部の今月累計と一致すること。

        以前は利用者別が「直近7日」のみで、直上の「今月累計」と桁が違い
        「合計が合わない」と誤解されていた。
        """
        week = {"alice": 58.76, "bob": 13.26}
        mtd = {"alice": 1576.00, "bob": 228.59, "carol": 1.40}
        msg = report_usage.build_message(
            "2026-09-17〜2026-09-24",
            [],
            101.82,
            sum(mtd.values()),
            week,
            mtd,
            "2026-09-01〜2026-09-24",
        )
        # 上部の今月累計と、利用者別テーブルの合計行が同じ金額で並ぶ
        self.assertIn("今月累計（2026-09-01〜2026-09-24）: $1805.99", msg)
        self.assertIn("| **合計** | **$72.02** | **$1805.99** |", msg)
        # 集計期間が異なることを本文で明示する
        self.assertIn("集計期間が異なる", msg)

    def test_user_cost_includes_users_active_only_earlier_in_month(self):
        """今週未利用だが今月は使った利用者を落とさないこと（棚卸しの漏れ防止）。"""
        lines = report_usage.build_user_cost_lines({}, {"ghost": 5.0}, "2026-09-17〜2026-09-24")
        body = "\n".join(lines)
        self.assertIn("| ghost | $0.00 | $5.00 |", body)
        self.assertIn("| **合計** | **$0.00** | **$5.00** |", body)

    def test_user_cost_displays_surviving_period_when_other_fails(self):
        """片方のCE取得が失敗しても、取得できた期間は表示する。"""
        mtd_only = "\n".join(report_usage.build_user_cost_lines(None, {"alice": 2.0}))
        self.assertIn("| 利用者 | 今月累計 |", mtd_only)
        self.assertIn("| alice | $2.00 |", mtd_only)
        self.assertIn("期間中コストの取得に失敗", mtd_only)

        period_only = "\n".join(report_usage.build_user_cost_lines({"alice": 1.0}, None))
        self.assertIn("| alice | $1.00 |", period_only)
        self.assertIn("今月累計コストの取得に失敗", period_only)

        self.assertIn("期間中コストの取得に失敗", "\n".join(
            report_usage.build_user_cost_lines(None, {"probe.user": 0.0001})
        ))
        self.assertIn("今月累計コストの取得に失敗", "\n".join(
            report_usage.build_user_cost_lines({"probe.user": 0.0001}, None)
        ))

    def test_user_cost_falls_back_to_single_column_without_mtd(self):
        """mtd 未指定のときは従来どおり 1 列（後方互換）。"""
        body = "\n".join(report_usage.build_user_cost_lines({"alice": 2.0}))
        self.assertIn("| alice | $2.00 |", body)
        self.assertIn("| **合計** | **$2.00** |", body)
        self.assertNotIn("今月累計", body)

    def test_user_cost_handles_failure_and_empty(self):
        """両期間の取得失敗と両期間空は、表を出さず注記のみ返す。"""
        self.assertIn("取得失敗", "\n".join(report_usage.build_user_cost_lines(None, None)))
        self.assertIn("データなし", "\n".join(report_usage.build_user_cost_lines({}, {})))

    def test_token_section_states_its_period(self):
        """後方互換: 月次行なしでも期間中トークンの期間を明記する。"""
        msg = report_usage.build_message("2026-09-18〜2026-09-24", [], None, None)
        self.assertIn(
            "**■ トークン消費量（期間中）**（CloudWatch Metrics・全呼出含む・**2026-09-18〜2026-09-24**）",
            msg,
        )
        self.assertNotIn("トークン消費量（今月累計）", msg)
        self.assertNotIn("今月累計コストの取得に失敗", msg)

    def test_token_tables_show_period_and_month_to_date(self):
        """期間中と今月累計のトークンを別表で表示し、同じ期間のコストと比較できること。"""
        period_rows = [
            {"name": "Opus 5.5", "input": 10, "output": 2, "cache_read": 3, "cache_write": 4,
             "in_price": None, "out_price": None}
        ]
        mtd_rows = [
            {"name": "Opus 5.5", "input": 100, "output": 20, "cache_read": 30, "cache_write": 40,
             "in_price": None, "out_price": None}
        ]
        msg = report_usage.build_message(
            "2026-09-18〜2026-09-24", period_rows, 1.0, 5.0, {}, {},
            "2026-09-01〜2026-09-24", mtd_rows,
        )
        self.assertIn("**■ トークン消費量（期間中）**", msg)
        self.assertIn("**■ トークン消費量（今月累計）**", msg)
        self.assertIn("| Opus 5.5 | 10 | 2 | 3 | 4 | 未設定 |", msg)
        self.assertIn("| Opus 5.5 | 100 | 20 | 30 | 40 | 未設定 |", msg)
        self.assertIn("同じ期間同士で比較する", msg)

    def test_reporting_windows_are_exact_calendar_days(self):
        """直近7日は8日にならず、月初・月またぎでも日付境界が正しいこと。"""
        now = datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc)
        window = report_usage.reporting_windows(now, 7)
        self.assertEqual(window["period_start"].isoformat(), "2026-09-18T00:00:00+00:00")
        self.assertEqual(window["ce_end"], "2026-09-25")
        self.assertEqual(window["period_label"], "2026-09-18〜2026-09-24")
        self.assertEqual(window["month_label"], "2026-09-01〜2026-09-24")

        month_edge = report_usage.reporting_windows(
            datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc), 7
        )
        self.assertEqual(month_edge["period_label"], "2026-09-26〜2026-10-02")
        self.assertEqual(month_edge["month_label"], "2026-10-01〜2026-10-02")

    def test_reporting_windows_reject_non_positive_days(self):
        with self.assertRaises(ValueError):
            report_usage.reporting_windows(datetime.now(timezone.utc), 0)

    def test_collect_token_rows_uses_same_metric_ids_for_a_window(self):
        """モデル行を期間ごとに再利用し、各metric IDの合計を正しく足す。"""
        models = [{"name": "Opus 5.5", "model_tag": "opus-5-5", "metric_ids": ["a", "b"],
                   "in_price": None, "out_price": None}]
        values = {"a": (1, 2, 3, 4), "b": (10, 20, 30, 40)}
        with patch.object(report_usage, "get_token_totals", side_effect=lambda mid, _s, _e: values[mid]):
            rows = report_usage.collect_token_rows(models, Mock(), Mock())
        self.assertEqual(rows[0]["input"], 11)
        self.assertEqual(rows[0]["output"], 22)
        self.assertEqual(rows[0]["cache_read"], 33)
        self.assertEqual(rows[0]["cache_write"], 44)

    def test_user_cost_omits_all_zero_history_but_keeps_nonzero(self):
        """検証履歴の$0.00行は落とし、実額がある利用者は片期間ゼロでも残す。"""
        body = "\n".join(report_usage.build_user_cost_lines(
            {"probe.user": 0.0001, "active": 1.0},
            {"probe.user": 0.000635, "e2e.validation": 0.00016, "mtd-only": 2.0},
        ))
        self.assertNotIn("probe.user", body)
        self.assertNotIn("e2e.validation", body)
        self.assertIn("active", body)
        self.assertIn("mtd-only", body)

        zero_only = "\n".join(report_usage.build_user_cost_lines(
            {"probe.user": 0.0001}, {"e2e.validation": 0.00016}
        ))
        self.assertIn("データなし（表示対象となる配賦コストなし）", zero_only)
        self.assertNotIn("| 利用者 |", zero_only)

    def test_handler_uses_shared_windows_for_tokens_and_costs(self):
        """期間中/月次のトークンとコストが同じ境界で取得されること。"""
        period_start = datetime(2026, 9, 18, tzinfo=timezone.utc)
        month_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        cw_end = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        window = {
            "period_start": period_start,
            "month_start": month_start,
            "cw_end": cw_end,
            "ce_end": "2026-09-25",
            "period_label": "2026-09-18〜2026-09-24",
            "month_label": "2026-09-01〜2026-09-24",
        }
        models = [{"name": "Opus 5.5", "model_tag": "opus-5-5", "metric_ids": ["id"]}]
        period_rows = [{"name": "Opus 5.5", "input": 1, "output": 2, "cache_read": 3,
                        "cache_write": 4, "in_price": None, "out_price": None}]
        mtd_rows = [{"name": "Opus 5.5", "input": 10, "output": 20, "cache_read": 30,
                     "cache_write": 40, "in_price": None, "out_price": None}]
        with (
            patch.object(report_usage, "reporting_windows", return_value=window),
            patch.object(report_usage, "load_models", return_value=models),
            patch.object(report_usage, "augment_metric_ids", return_value=models) as augment,
            patch.object(report_usage, "collect_token_rows", side_effect=[period_rows, mtd_rows]) as collect,
            patch.object(report_usage, "get_cost", side_effect=[1.0, 5.0]) as get_cost,
            patch.object(report_usage, "get_cost_by_user", side_effect=[{"a": 1.0}, {"a": 5.0}]) as get_user,
            patch.object(report_usage, "build_message", return_value="message") as build,
            patch.object(report_usage.ssm, "get_parameter", return_value={"Parameter": {"Value": "https://example"}}),
            patch.object(report_usage, "post_teams") as post,
        ):
            result = report_usage.handler(event={}, context=None)

        self.assertEqual(result, {"posted": True, "models": 1})
        augment.assert_called_once_with(models)
        self.assertEqual(collect.call_args_list[0].args, (models, period_start, cw_end))
        self.assertEqual(collect.call_args_list[1].args, (models, month_start, cw_end))
        self.assertEqual(get_cost.call_args_list[0].args, ("2026-09-18", "2026-09-25"))
        self.assertEqual(get_cost.call_args_list[1].args, ("2026-09-01", "2026-09-25"))
        self.assertEqual(get_user.call_args_list[0].args, ("2026-09-18", "2026-09-25"))
        self.assertEqual(get_user.call_args_list[1].args, ("2026-09-01", "2026-09-25"))
        self.assertIs(build.call_args.args[-1], mtd_rows)
        post.assert_called_once_with("https://example", "message")


if __name__ == "__main__":
    unittest.main()
