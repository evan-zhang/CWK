#!/usr/bin/env python3
"""RT-054 Stage A: freeze falsifiable baseline, gold and v3 contracts."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RT = ROOT / "RT" / "RT-054"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class StageAContractTests(unittest.TestCase):
    def test_baseline_keeps_unknowns_explicit(self):
        baseline = load(RT / "evidence" / "stage-a-baseline-20260909.json")
        self.assertEqual(baseline["schema"], "cwk.rt054.stage-a-baseline.v1")
        libs = {row["kb"]: row for row in baseline["libraries"]}
        self.assertEqual(set(libs), {"cwork-3m", "docdb-touqian", "spbp-2027"})
        self.assertEqual(libs["cwork-3m"]["lexical_index_bytes"], 1_495_220_459)
        self.assertEqual(libs["cwork-3m"]["chunks"], 66_500)
        for row in libs.values():
            self.assertIsNone(row["terms"], row["kb"])
            self.assertEqual(row["terms_status"], "unknown_without_full_decode")
            self.assertGreater(row["lexical_share_percent"], 75)
        failure = libs["cwork-3m"]["query_observations"][1]
        self.assertFalse(failure["bm25_entered"])
        self.assertEqual(failure["status"], "lexical_unavailable")

    def test_gold_has_72_reviewable_cases_and_required_strata(self):
        gold = load(RT / "evidence" / "stage-a-gold-20260909.json")
        cases = gold["cases"]
        self.assertEqual(gold["case_count"], 72)
        self.assertEqual(len(cases), 72)
        self.assertEqual(len({c["id"] for c in cases}), 72)
        counts = Counter(c["category"] for c in cases)
        required = {
            "body", "title", "short_cjk", "exact_identifier",
            "company_person", "date", "table", "no_answer", "permission",
        }
        self.assertTrue(required.issubset(counts), counts)
        self.assertGreaterEqual(min(counts[x] for x in required), 6)
        for case in cases:
            self.assertTrue(case["query"] or case["category"] in {"permission", "availability"}, case)
            self.assertIn(case["expected_outcome"], {
                "hits", "no_evidence", "error", "authorized_behavior", "availability_behavior"
            })
            self.assertTrue(case["rationale"], case)
            self.assertEqual(case["review"]["status"], "accepted", case)
        for case in (c for c in cases if c["category"] in {"permission", "availability"}):
            self.assertTrue(case.get("behavior_assertions"), case)
        permission = {c["id"]: c for c in cases if c["category"] == "permission"}
        self.assertEqual(permission["G31"]["behavior_assertions"], {"status": 403, "payload_clean": True})
        self.assertEqual(permission["G33"]["expected_doc_ids"], ["docdb:701"])
        self.assertEqual(permission["G34"]["behavior_assertions"]["status"], 401)

    def test_new_gold_is_backed_by_real_fixture_units_and_distractors(self):
        gold = load(RT / "evidence" / "stage-a-gold-20260909.json")
        fixture = load(RT / "evidence" / "stage-a-gold-fixture-20260909.json")
        docs = {d["doc_id"]: d for d in fixture["documents"]}
        new_cases = [c for c in gold["cases"] if c["origin"] == "RT-054/Stage-A-synthetic"]
        self.assertEqual(len(new_cases), 24)
        self.assertEqual(len(docs), 22)
        for case in new_cases:
            self.assertEqual(case["expected_outcome"], "hits")
            self.assertEqual(len(case["expected_doc_ids"]), 1)
            doc = docs[case["expected_doc_ids"][0]]
            self.assertEqual(doc["kb_id"], case["kb_id"])
            matching_units = [
                u for u in doc["evidence_units"]
                if u["excerpt"] == case["evidence_excerpt"] and u["locator"] == case["expected_locator"]
            ]
            self.assertEqual(len(matching_units), 1, case)
            unit_text = matching_units[0]["excerpt"]
            for atom in case["must_match"]:
                self.assertIn(atom, doc["search_text"], case)
                self.assertIn(atom, unit_text, case)
            for negative_doc_id in case["must_not_match_doc_ids"]:
                self.assertIn(negative_doc_id, docs, case)
                self.assertNotEqual(negative_doc_id, case["expected_doc_ids"][0])
        table_cases = [c for c in new_cases if c["category"] == "table"]
        self.assertEqual(len(table_cases), 8)
        for case in table_cases:
            self.assertIn("sheet_name", case["expected_locator"])
            self.assertEqual(case["expected_locator"]["row_start"], case["expected_locator"]["row_end"])
            # Header and exactly one target row form the evidence unit.
            self.assertEqual(len(case["evidence_excerpt"].splitlines()), 2, case)

    def test_v3_request_forbids_authority_and_backend_inputs(self):
        schema = load(RT / "contracts" / "search-v3-request.schema.json")
        self.assertFalse(schema["additionalProperties"])
        props = set(schema["properties"])
        self.assertEqual(props, {"kb_ids", "query", "top_k", "request_id"})
        forbidden = {"tenant_id", "object_uri", "index", "epoch", "dsl", "filter"}
        self.assertFalse(props & forbidden)
        self.assertEqual(set(schema["required"]), {"kb_ids", "query", "request_id"})

    def test_v3_response_exposes_evidence_not_storage_locators(self):
        schema = load(RT / "contracts" / "search-v3-response.schema.json")
        props = schema["properties"]
        self.assertEqual(props["schema"]["const"], "cwk.kb.search.v3")
        hit = props["hits"]["items"]
        hit_props = set(hit["properties"])
        self.assertTrue({"doc_id", "source_version", "section_path", "locator", "excerpt"}.issubset(hit_props))
        self.assertFalse({"object_uri", "nas_path", "index_name", "token"} & hit_props)
        self.assertIn("no_evidence", props)

    def test_v3_error_codes_distinguish_empty_from_failure(self):
        schema = load(RT / "contracts" / "search-v3-error.schema.json")
        codes = set(schema["properties"]["error"]["properties"]["code"]["enum"])
        self.assertTrue({
            "forbidden", "source_stale", "index_not_ready", "search_timeout",
            "control_plane_unavailable", "object_unavailable", "capacity_exceeded",
        }.issubset(codes))
        self.assertNotIn("no_evidence", codes)


if __name__ == "__main__":
    unittest.main()
