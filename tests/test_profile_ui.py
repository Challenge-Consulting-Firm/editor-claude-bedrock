import importlib
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch


LAMBDA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda"))
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

os.environ.setdefault("ENTRA_TENANT_ID", "tenant")
os.environ.setdefault("ENTRA_CLIENT_ID", "client")
os.environ.setdefault(
    "MODEL_SOURCES_JSON",
    json.dumps(
        {
            "opus": "jp.anthropic.claude-opus-4-8",
            "sonnet": "jp.anthropic.claude-sonnet-4-6",
            "haiku": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
            "opus-5": "global.anthropic.claude-opus-5",
        }
    ),
)


with patch("boto3.client", return_value=Mock()):
    profile_ui = importlib.import_module("profile_ui")


class FakePaginator:
    def __init__(self, profiles):
        self.profiles = profiles

    def paginate(self, **kwargs):
        return [{"inferenceProfileSummaries": self.profiles}]


class FakeBedrock:
    def __init__(self, profiles=(), tags=None):
        self.profiles = list(profiles)
        self.tags = tags or {}
        self.created = []
        self.tagged = []
        self.deleted = []

    def get_paginator(self, name):
        if name != "list_inference_profiles":
            raise AssertionError(name)
        return FakePaginator(self.profiles)

    def list_tags_for_resource(self, resourceARN):
        return {"tags": self.tags.get(resourceARN, [])}

    def create_inference_profile(self, **kwargs):
        self.created.append(kwargs)

    def tag_resource(self, **kwargs):
        self.tagged.append(kwargs)

    def delete_inference_profile(self, **kwargs):
        self.deleted.append(kwargs)


def summary(profile_id, name):
    return {
        "inferenceProfileId": profile_id,
        "inferenceProfileName": name,
        "inferenceProfileArn": f"arn:aws:bedrock:ap-northeast-1:123:application-inference-profile/{profile_id}",
    }


def tags(user, model, residency=None):
    values = [
        {"key": "user", "value": user},
        {"key": "app", "value": "claude-code"},
        {"key": "model", "value": model},
    ]
    if residency:
        values.append({"key": "residency", "value": residency})
    return values


class ProfileUiTests(unittest.TestCase):
    def setUp(self):
        setattr(profile_ui, "_account_id", "123")

    def test_existing_domestic_models_are_not_recreated_and_only_5x_are_added(self):
        profiles = [
            summary("p1", "cc-user-opus"),
            summary("p2", "cc-user-sonnet"),
            summary("p3", "cc-user-haiku"),
        ]
        fake = FakeBedrock(
            profiles,
            {
                profiles[0]["inferenceProfileArn"]: tags("user.name", "opus"),
                profiles[1]["inferenceProfileArn"]: tags("user.name", "sonnet"),
                profiles[2]["inferenceProfileArn"]: tags("user.name", "haiku"),
            },
        )
        with patch.object(profile_ui, "bedrock", fake):
            profile_ui.create_user_profiles("user.name")

        self.assertEqual(
            [c["tags"][2]["value"] for c in fake.created],
            ["opus-5"],
        )
        self.assertTrue(all(c["tags"][3]["value"] == "global" for c in fake.created))
        self.assertEqual(len(fake.tagged), 3)
        self.assertTrue(all(t["tags"] == [{"key": "residency", "value": "jp"}] for t in fake.tagged))

    def test_long_and_symbol_heavy_user_names_generate_valid_unique_names(self):
        first = profile_ui._profile_name("_" * 64, "opus-5", set())
        second = profile_ui._profile_name("_" * 64, "sonnet-5", {first})
        for name in (first, second):
            self.assertLessEqual(len(name), 64)
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertNotEqual(first, second)

    def test_delete_removes_duplicate_model_profiles(self):
        profiles = [summary("p1", "cc-user-opus"), summary("p2", "cc-user-opus-copy")]
        fake = FakeBedrock(
            profiles,
            {p["inferenceProfileArn"]: tags("user.name", "opus") for p in profiles},
        )
        with patch.object(profile_ui, "bedrock", fake):
            deleted = profile_ui.delete_user_profiles("user.name")
        self.assertEqual(deleted, 2)
        self.assertEqual(len(fake.deleted), 2)

    def test_invalid_json_and_non_string_user_return_400(self):
        auth = patch.object(profile_ui, "_require_auth", return_value={"oid": "caller"})
        with auth:
            for body in ("{", "[]", '{"user": 123}'):
                response = profile_ui.handler(
                    {
                        "requestContext": {"http": {"method": "POST"}},
                        "rawPath": "/api/profiles",
                        "headers": {},
                        "body": body,
                    },
                    None,
                )
                self.assertEqual(response["statusCode"], 400)

    def test_cost_summary_includes_model_breakdown_without_changing_totals(self):
        summary = profile_ui._summarize(
            {"alice": 12.5, "bob": 4.0, "": 1.0},
            {
                "alice": {"opus-5": 10.0, "opus": 2.5},
                "bob": {"haiku": 4.0},
            },
        )
        self.assertEqual(summary["total"], 17.5)
        self.assertEqual(summary["unallocated"], 1.0)
        self.assertEqual(summary["users"][0]["user"], "alice")
        self.assertEqual(summary["users"][0]["models"][0], {"model": "opus-5", "amount": 10.0})

    def test_cost_summary_is_backward_compatible_without_model_data(self):
        summary = profile_ui._summarize({"alice": 2.0})
        self.assertEqual(summary["users"], [{"user": "alice", "amount": 2.0, "models": []}])

    def test_unsupported_method_does_not_parse_body(self):
        with patch.object(profile_ui, "_require_auth", return_value={"oid": "caller"}):
            response = profile_ui.handler(
                {
                    "requestContext": {"http": {"method": "PATCH"}},
                    "rawPath": "/api/profiles",
                    "headers": {},
                    "body": "not json",
                },
                None,
            )
        self.assertEqual(response["statusCode"], 405)


if __name__ == "__main__":
    unittest.main()
