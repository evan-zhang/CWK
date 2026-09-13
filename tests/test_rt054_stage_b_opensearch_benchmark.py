#!/usr/bin/env python3
"""RT-054 tests for the real OpenSearch Stage B benchmark harness."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_stage_b_opensearch_benchmark as bench  # noqa: E402


class MappingContractTests(unittest.TestCase):
    def test_legacy_mapping_is_real_cjk_123gram_and_whole_ascii(self):
        mapping = bench.mapping_for("legacy_123gram", False)
        analysis = mapping["settings"]["analysis"]
        grams = analysis["filter"]["cwk_legacy_123gram"]
        self.assertEqual((grams["min_gram"], grams["max_gram"]), (1, 3))
        self.assertEqual(grams["type"], "ngram")
        self.assertEqual(analysis["analyzer"]["cwk_legacy_ascii"]["tokenizer"], "whitespace")
        self.assertEqual(analysis["analyzer"]["cwk_legacy_ascii"]["char_filter"],
                         ["cwk_ascii_only"])
        body = mapping["mappings"]["properties"]["body"]
        self.assertEqual(body["analyzer"], "cwk_legacy_cjk")
        self.assertEqual(body["fields"]["ascii"]["analyzer"], "cwk_legacy_ascii")
        self.assertEqual(mapping["mappings"]["_source"]["excludes"], ["body"])

    def test_icu_mapping_requires_official_icu_components(self):
        mapping = bench.mapping_for("analysis_icu", True)
        analyzer = mapping["settings"]["analysis"]["analyzer"]["cwk_official_icu"]
        self.assertEqual(analyzer["tokenizer"], "icu_tokenizer")
        self.assertIn("icu_normalizer", analyzer["filter"])
        self.assertNotIn("excludes", mapping["mappings"]["_source"])
        self.assertTrue(mapping["mappings"]["dynamic"] == "strict")

    def test_smartcn_mapping_requires_official_smartcn_analyzer(self):
        mapping = bench.mapping_for("analysis_smartcn", False)
        analyzer = mapping["settings"]["analysis"]["analyzer"]["cwk_official_smartcn"]
        self.assertEqual(analyzer, {"type": "smartcn"})
        self.assertEqual(mapping["mappings"]["properties"]["body"]["analyzer"],
                         "cwk_official_smartcn")

    def test_query_filter_carries_kb_and_fixture_scope(self):
        case = {"kb_id": "liba", "query": "天枢"}
        body = bench.query_body("analysis_icu", case, "rt051_a11")
        filters = body["query"]["bool"]["filter"]
        self.assertIn({"term": {"kb_id": "liba"}}, filters)
        self.assertIn({"term": {"fixture_scope": "rt051_a11"}}, filters)
        self.assertEqual(body["collapse"], {"field": "doc_id"})

    def test_identifier_fragment_does_not_fall_back_to_fulltext(self):
        case = {"kb_id": "liba", "query": "017"}
        body = bench.query_body("analysis_icu", case, "rt051_a11")
        should = body["query"]["bool"]["should"]
        self.assertEqual(should, [{"term": {"identifiers": {"value": "017", "boost": 12}}}])

    def test_iso_date_routes_to_date_field_before_identifier_shape(self):
        case = {"kb_id": "liba", "query": "2027-03-15"}
        body = bench.query_body("analysis_icu", case, "rt054_extension")
        should = body["query"]["bool"]["should"]
        self.assertEqual(should, [{"term": {"date_values": {"value": "2027-03-15", "boost": 12}}}])

    def test_only_loopback_plain_http_is_accepted(self):
        bench.OpenSearchClient("http://127.0.0.1:19200")
        with self.assertRaises(ValueError):
            bench.OpenSearchClient("https://search.example.test:9200")
        with self.assertRaises(ValueError):
            bench.OpenSearchClient("http://user:secret@127.0.0.1:19200")

    def test_rest_http_and_transport_errors_are_bounded(self):
        client = bench.OpenSearchClient("http://127.0.0.1:19200")
        http_error = urllib.error.HTTPError(
            client.base_url + "/bad", 500, "boom", {}, io.BytesIO(b'{"error":"bounded"}'))
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(bench.OpenSearchError, "HTTP 500"):
                client.request("GET", "/bad")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(bench.OpenSearchError, "URLError"):
                client.request("GET", "/")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(bench.OpenSearchError, "non-JSON"):
                client.request("GET", "/")

    def test_unsafe_index_prefix_is_rejected_before_network(self):
        with self.assertRaises(ValueError):
            bench.run_benchmark("http://127.0.0.1:1", "CWK/unsafe*")

    def test_metric_schema_rejects_incomplete_artifacts(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            bench.validate_report({})


class FixtureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parents, cls.children, cls.geometry = bench.project_fixture()

    def test_all_analyzers_use_the_same_frozen_50_doc_projection(self):
        self.assertEqual(len({child.doc_id for child in self.children}), 50)
        self.assertEqual(len(self.parents), 50)
        self.assertEqual(len(self.children), 50)
        self.assertEqual(
            {bench.fixture_scope(child.doc_id) for child in self.children},
            {"rt051_a11", "rt054_extension"},
        )

    def test_bulk_payload_has_one_action_and_document_per_child(self):
        payload = bench.bulk_payload("cwk-test-index", self.children)
        lines = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
        self.assertEqual(len(lines), len(self.children) * 2)
        actions, docs = lines[::2], lines[1::2]
        self.assertTrue(all(row["index"]["_index"] == "cwk-test-index" for row in actions))
        self.assertEqual({row["doc_id"] for row in docs}, {child.doc_id for child in self.children})
        self.assertTrue(all("body" in row for row in docs))
        self.assertTrue(all(row["fixture_scope"] in {"rt051_a11", "rt054_extension"} for row in docs))


class RealOpenSearchIntegrationTests(unittest.TestCase):
    def test_real_official_analyzers_shared_vs_isolated_and_cleanup(self):
        url = os.environ.get("CWK_RT054_OPENSEARCH_URL")
        if not url:
            self.skipTest("structured SKIP: CWK_RT054_OPENSEARCH_URL is not set")
        try:
            bench.OpenSearchClient(url).request("GET", "/")
        except bench.OpenSearchError as exc:
            self.skipTest(f"structured SKIP: OpenSearch service unavailable: {exc}")
        prefix = "cwk-rt054-it-" + os.urandom(5).hex()
        report = bench.run_benchmark(
            url, prefix, repeats=1, timeout=30, expected_runtime_version="99.0.0")
        self.assertEqual(report["stage_b_decision"], "NO-GO")
        shared = report["lanes"]["shared_filter"]["runs"]
        isolated = report["lanes"]["isolated_scope"]["runs"]
        self.assertEqual(len(shared), 6)
        self.assertEqual(len(isolated), 6)
        self.assertTrue(report["gates"]["official_analysis_icu"]["pass"])
        self.assertTrue(report["gates"]["official_analysis_smartcn"]["pass"])
        self.assertTrue(report["gates"]["same_50_doc_projection"]["pass"])
        self.assertFalse(report["gates"]["three_library_equivalent_primary_store"]["pass"])
        self.assertFalse(report["gates"]["expected_runtime_version"]["pass"])
        self.assertFalse(report["gates"]["synthetic_quality"]["pass"])
        self.assertTrue(report["environment"]["resources"]["boundary_note"])
        components = {row["component"] for row in report["environment"]["plugins"]}
        self.assertTrue({"analysis-icu", "analysis-smartcn"}.issubset(components))
        runs = {(row["analyzer"], row["source_model"]): row for row in shared}
        for analyzer in bench.ANALYZERS:
            excluded = runs[(analyzer, "body_excluded_from_source")]
            included = runs[(analyzer, "body_in_source")]
            self.assertEqual(excluded["metrics"]["analyzed_unique_terms_by_field"],
                             included["metrics"]["analyzed_unique_terms_by_field"])
            excluded_ranks = [(row["id"], row.get("returned_doc_ids"), row.get("status"))
                              for row in excluded["quality"]["cases"]]
            included_ranks = [(row["id"], row.get("returned_doc_ids"), row.get("status"))
                              for row in included["quality"]["cases"]]
            self.assertEqual(excluded_ranks, included_ranks)
            g20 = next(row for row in excluded["quality"]["cases"] if row["id"] == "G20")
            self.assertTrue(g20["honest_no_evidence"], analyzer)
        smartcn_g05 = next(row for row in runs[("analysis_smartcn", "body_excluded_from_source")]
                           ["quality"]["cases"] if row["id"] == "G05")
        self.assertFalse(smartcn_g05["recall_at_10"])

        isolated_by_key = {(row["analyzer"], row["fixture_scope"]): row
                           for row in isolated}
        for analyzer in bench.ANALYZERS:
            shared_stats = runs[(analyzer, "body_excluded_from_source")][
                "lucene_statistics_evidence"]["field_statistics"]
            isolated_stats = isolated_by_key[(analyzer, "rt051_a11")][
                "lucene_statistics_evidence"]["field_statistics"]
            self.assertGreater(shared_stats["doc_count"], isolated_stats["doc_count"])
            self.assertLessEqual(shared_stats["doc_count"], 50)
            self.assertLessEqual(isolated_stats["doc_count"], 28)
        self.assertTrue(report["statistics_semantics"][
            "shared_filter_uses_global_lucene_statistics"])
        self.assertEqual(len(report["cleanup"]["deleted"]), 12)
        self.assertFalse(report["cleanup"]["failures"])
        broken = copy.deepcopy(report)
        broken["lanes"]["shared_filter"]["runs"][0]["metrics"]["primary_store_bytes"] = -1
        with self.assertRaisesRegex(ValueError, "primary_store_bytes"):
            bench.validate_report(broken)
        client = bench.OpenSearchClient(url)
        leftovers, _ = client.request("GET", f"/_cat/indices/{prefix}-*?format=json")
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
