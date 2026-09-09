#!/usr/bin/env python3
"""RT-054 OPS benchmark report and privacy-boundary contracts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_stage_b_ops_benchmark as bench  # noqa: E402


class SafeReportTests(unittest.TestCase):
    def test_aggregate_report_accepts_only_content_free_shape(self):
        report = {
            "schema": "cwk.rt054.ops-three-library-benchmark.v1",
            "libraries": {kb: {"documents": 1, "primary_store_bytes": 2}
                          for kb in bench.KBS},
            "decision": {"stage_b": "NO-GO", "reason": "insufficient_manual_gold"},
        }
        encoded = bench.safe_output(report)
        self.assertEqual(json.loads(encoded), report)

    def test_content_keys_and_sensitive_path_shapes_are_rejected(self):
        for report in (
            {"query": "secret"}, {"title": "secret"}, {"doc_id": "secret"},
            {"body": "secret"}, {"nested": {"path": "secret"}},
            {"detail": "/Users/private/source"},
        ):
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    bench.safe_output(report)

    def test_multiline_strings_are_rejected(self):
        with self.assertRaises(ValueError):
            bench.safe_output({"error": "line one\nline two"})

    def test_content_derived_template_fingerprints_are_rejected(self):
        with self.assertRaises(ValueError):
            bench.safe_output({"geometry": {"template_hashes": ["a" * 16]}})

    def test_geometry_projection_keeps_only_aggregate_template_counts(self):
        result = bench.aggregate_geometry({
            "parent": {"count": 2},
            "child": {"count": 3},
            "overlap": {"default_tokens": 0},
            "template_dedup": {
                "template_count": 1,
                "removed_line_instances": 4,
                "template_hashes": ["content-derived-fingerprint"],
            },
        })
        self.assertEqual(result["template_dedup"], {
            "template_count": 1, "removed_line_instances": 4,
        })
        self.assertNotIn("template_hashes", json.dumps(result))


class GoldBoundaryTests(unittest.TestCase):
    def test_synthetic_or_non_target_gold_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gold.json"
            path.write_text(json.dumps({"cases": [{
                "kb_id": "liba", "query": "synthetic question",
                "expected_doc_ids": ["synthetic:1"], "category": "body",
            }]}), encoding="utf-8")
            result = bench.discover_gold([str(path)])
        self.assertEqual(result["summary"]["case_count"], 0)
        self.assertEqual(result["summary"]["status"], "UNKNOWN")
        self.assertEqual(result["summary"]["reason"], "no_ops_manual_gold")

    def test_real_target_cases_still_fail_closed_without_legacy_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gold.json"
            path.write_text(json.dumps({"cases": [{
                "kb_id": "cwork-3m", "query": "private question",
                "expected_doc_ids": ["private-document"], "category": "exact_identifier",
            }]}), encoding="utf-8")
            result = bench.discover_gold([str(path)])
        self.assertEqual(result["summary"]["case_count"], 1)
        self.assertEqual(result["summary"]["status"], "UNKNOWN")
        self.assertEqual(result["summary"]["reason"], "gold_missing_legacy_baseline")


class LuceneStatisticsTests(unittest.TestCase):
    def test_termvectors_keep_only_aggregate_postings_statistics(self):
        client = mock.Mock()
        client.request.side_effect = [
            ({"_all": {"primaries": {
                "store": {"size_in_bytes": 100}, "docs": {"count": 2},
                "indexing": {}, "refresh": {}, "merges": {}, "segments": {},
            }}}, 0),
            ({"term_vectors": {"body": {"field_statistics": {
                "doc_count": 2, "sum_doc_freq": 40, "sum_total_term_freq": 80,
            }, "terms": {"sensitive-term": {"doc_freq": 1}}}}}, 0),
        ]
        child = mock.Mock()
        with mock.patch.object(bench, "source_doc", return_value={"body": "private"}):
            result = bench.index_stats(client, "idx", child)
        self.assertEqual(result["lucene_body_statistics"], {
            "doc_count": 2, "sum_doc_freq": 40, "sum_total_term_freq": 80,
        })
        self.assertNotIn("terms", result)
        self.assertNotIn("sensitive-term", json.dumps(result))


class EvidenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = PROJECT / "RT" / "RT-054" / "evidence" / "stage-b-ops-benchmark-20260909.json"
        cls.report = json.loads(cls.path.read_text(encoding="utf-8"))

    def test_final_evidence_is_scanned_aggregate_and_cleaned(self):
        self.assertEqual(self.report["schema"], "cwk.rt054.ops-three-library-benchmark.v1")
        bench.safe_output(self.report)
        cleanup = self.report["cleanup"]
        self.assertTrue(cleanup["all_zero"])
        self.assertTrue(cleanup["gateway_unchanged"])
        self.assertTrue(cleanup["old_index_metadata_unchanged"])
        self.assertEqual(cleanup["failures"], 0)
        for library in self.report["libraries"].values():
            self.assertNotIn("template_hashes", library["geometry"]["template_dedup"])

    def test_real_runtime_plugins_and_complete_matrix_are_recorded(self):
        env = self.report["environment"]
        self.assertEqual(env["runtime_version"], "3.3.2")
        self.assertEqual(env["lucene_version"], "10.3.1")
        self.assertEqual(env["analysis_plugins"], {
            "analysis-icu": "3.3.2", "analysis-smartcn": "3.3.2",
        })
        self.assertEqual(len(self.report["shared_physical_index_runs"]), 6)
        for kb in bench.KBS:
            runs = self.report["libraries"][kb]["runs"]
            self.assertEqual(len(runs), 6)
            for run in runs:
                stats = run["metrics"]["lucene_body_statistics"]
                self.assertGreater(stats["doc_count"], 0)
                self.assertGreater(stats["sum_doc_freq"], 0)

    def test_denominator_quality_and_decision_fail_closed(self):
        self.assertEqual(self.report["gates"]["storage_reduction_all_libraries"]["status"], "PASS")
        self.assertEqual(self.report["gates"]["cross_kb_leak"], {
            "leaks": 0, "status": "PASS", "threshold": 0,
        })
        self.assertEqual(self.report["gold"]["case_count"], 0)
        for gate in ("real_gold_recall", "exact_top10", "no_answer"):
            self.assertEqual(self.report["gates"][gate]["status"], "UNKNOWN")
        self.assertEqual(self.report["decision"], {
            "stage_b": "NO-GO", "selected_analyzer": None,
            "selected_mapping": None, "reason": "insufficient_manual_gold",
        })


class DenominatorTests(unittest.TestCase):
    def test_reduction_denominators_are_each_library_old_lexical_json(self):
        self.assertEqual(bench.OLD_LEXICAL_BYTES, {
            "cwork-3m": 1_495_220_459,
            "docdb-touqian": 46_513_952,
            "spbp-2027": 264_466_313,
        })
        self.assertEqual(set(bench.OLD_LEXICAL_BYTES), set(bench.KBS))


if __name__ == "__main__":
    unittest.main()
