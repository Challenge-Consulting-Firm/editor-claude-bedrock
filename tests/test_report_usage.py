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


if __name__ == "__main__":
    unittest.main()
