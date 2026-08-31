import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_person_relation import (  # noqa: E402
    SCHEMA,
    classify_person_relation,
    load_relationship_manifest,
    normalize_backend_relation,
)


class PersonRelationTests(unittest.TestCase):
    def test_local_people_lists_are_not_relationship_evidence(self):
        result = classify_person_relation(
            simple={"writeEmpId": "owner-1"},
            node={"nodeList": [{"userList": [{"empId": "owner-1"}]}]},
            source_scopes={"outbox", "todo_pending"},
            owner_emp_id="owner-1",
        )
        self.assertEqual(result["relationship_status"], "unknown")
        self.assertFalse(result["visible_only"])

    def test_backend_visible_only_is_accepted(self):
        result = normalize_backend_relation(
            {
                "reportId": "7",
                "status": "resolved",
                "visibility": "visible_only",
                "primaryRole": "observer",
                "roles": [],
                "actionRequired": False,
                "pendingActions": [],
                "reasonCode": "PERMISSION_ONLY",
            },
            expected_report_id="7",
        )
        self.assertTrue(result["visible_only"])
        self.assertEqual(result["relationship_role"], "observer")

    def test_backend_multiple_roles_and_actions_are_preserved(self):
        result = normalize_backend_relation(
            {
                "reportId": "8",
                "status": "resolved",
                "visibility": "related",
                "primaryRole": "advisor",
                "roles": ["recipient", "advisor"],
                "actionRequired": True,
                "pendingActions": ["submit_advice"],
                "reasonCode": "ACTIVE_SUGGEST_NODE",
            },
            expected_report_id="8",
            relation_version="v1",
        )
        self.assertEqual(result["relationship_role"], "advisor")
        self.assertEqual(result["relationship_roles"], ["recipient", "advisor"])
        self.assertEqual(result["relationship_pending_actions"], ["submit_advice"])
        self.assertTrue(result["relationship_action_required"])

    def test_missing_or_mismatched_backend_result_is_unknown(self):
        self.assertEqual(normalize_backend_relation(None)["relationship_status"], "unknown")
        result = normalize_backend_relation(
            {"reportId": "other", "status": "resolved", "visibility": "related"},
            expected_report_id="expected",
        )
        self.assertEqual(result["relationship_status"], "unknown")

    def test_manifest_loads_raw_backend_items(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "relations.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA,
                        "provider_status": "ok",
                        "relation_version": "v1",
                        "items": [{
                            "reportId": "9",
                            "status": "resolved",
                            "visibility": "related",
                            "primaryRole": "recipient",
                            "roles": ["recipient"],
                            "actionRequired": False,
                            "pendingActions": [],
                        }],
                    }
                ),
                encoding="utf-8",
            )
            items, meta = load_relationship_manifest(path)
        self.assertEqual(meta["provider_status"], "ok")
        self.assertEqual(items["9"]["relationship_role"], "recipient")


if __name__ == "__main__":
    unittest.main()
