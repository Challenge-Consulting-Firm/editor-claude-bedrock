import importlib
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch


LAMBDA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda"))
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("WEBHOOK_PARAM", "/test/teams-webhook")

with patch("boto3.client", return_value=Mock()):
    report_usage = importlib.import_module("report_usage")


class ReportUsageTests(unittest.TestCase):
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

    def test_user_cost_falls_back_to_single_column_without_mtd(self):
        """mtd 未指定のときは従来どおり 1 列（後方互換）。"""
        body = "\n".join(report_usage.build_user_cost_lines({"alice": 2.0}))
        self.assertIn("| alice | $2.00 |", body)
        self.assertIn("| **合計** | **$2.00** |", body)
        self.assertNotIn("今月累計", body)

    def test_user_cost_handles_failure_and_empty(self):
        """取得失敗（None）と両期間空は、表を出さず注記のみ返す。"""
        self.assertIn("取得失敗", "\n".join(report_usage.build_user_cost_lines(None, {"a": 1.0})))
        self.assertIn("データなし", "\n".join(report_usage.build_user_cost_lines({}, {})))

    def test_token_section_states_its_period(self):
        """トークン消費量の見出しに集計期間を明記する（今月累計との混同防止）。"""
        msg = report_usage.build_message("2026-09-17〜2026-09-24", [], None, None)
        self.assertIn("**■ トークン消費量**（CloudWatch Metrics・全呼出含む・**2026-09-17〜2026-09-24**）", msg)


if __name__ == "__main__":
    unittest.main()
