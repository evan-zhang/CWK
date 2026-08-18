import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_report_relation  # noqa: E402


class ReportRelationClientTests(unittest.TestCase):
    @patch("cwk_report_relation._post")
    def test_batch_query_detects_missing_items(self, post):
        post.return_value = {
            "relationVersion": "v1",
            "evaluatedAt": "2026-08-12T12:00:00+08:00",
            "items": [{
                "reportId": "1",
                "status": "resolved",
                "visibility": "related",
                "primaryRole": "recipient",
                "roles": ["recipient"],
                "actionRequired": False,
                "pendingActions": [],
            }],
        }
        result = cwk_report_relation.query_relationships(
            base_url="https://example.invalid",
            endpoint_path="/relation",
            app_key="secret",
            report_ids=["1", "2"],
        )
        self.assertEqual(result["provider_status"], "partial")
        self.assertEqual(result["missing_report_ids"], ["2"])
        self.assertEqual(result["resolved_count"], 1)

    @patch("cwk_report_relation._post", side_effect=RuntimeError("relationship API request failed: timeout"))
    def test_batch_failure_is_unavailable_without_secret_leak(self, _post):
        result = cwk_report_relation.query_relationships(
            base_url="https://example.invalid",
            endpoint_path="/relation",
            app_key="top-secret",
            report_ids=["1"],
        )
        self.assertEqual(result["provider_status"], "unavailable")
        self.assertEqual(result["missing_report_ids"], ["1"])
        self.assertNotIn("top-secret", str(result))

    @patch("cwk_report_relation._post")
    def test_duplicate_or_unrequested_items_degrade_batch(self, post):
        item = {"reportId": "1", "status": "resolved", "visibility": "related", "primaryRole": "recipient", "roles": ["recipient"], "actionRequired": False, "pendingActions": []}
        post.return_value = {"items": [item, item, {**item, "reportId": "unexpected"}]}
        result = cwk_report_relation.query_relationships(
            base_url="https://example.invalid",
            endpoint_path="/relation",
            app_key="secret",
            report_ids=["1"],
        )
        self.assertEqual(result["provider_status"], "unavailable")
        self.assertEqual(result["missing_report_ids"], ["1"])
        self.assertTrue(any("duplicate" in error for error in result["errors"]))
        self.assertTrue(any("unrequested" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
