#!/usr/bin/env python3
"""RT-054 Stage B falsifiable tests for projection, metrics and gold use."""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_stage_b_poc as poc  # noqa: E402


class ProjectionTests(unittest.TestCase):
    def test_parent_child_geometry_and_forced_overlap_are_real(self):
        sentence = "".join(f"第{i}项连续说明没有句号" for i in range(180)) + "。"
        doc = poc.SourceDocument("liba", "synthetic:long", "长文", "长文.md",
                                 sentence * 5, {"section_path": ["长文"]})
        parents, children, geometry = poc.project_documents([doc])
        self.assertTrue(parents)
        self.assertTrue(children)
        self.assertLessEqual(max(p.token_count for p in parents), poc.PARENT_MAX_TOKENS)
        self.assertLessEqual(max(c.token_count for c in children), poc.CHILD_MAX_TOKENS)
        forced = [c for c in children if c.forced_split]
        self.assertTrue(forced, "超长无安全边界句必须走强制断句")
        self.assertTrue(all(30 <= c.overlap_tokens <= 50 for c in forced if c.ordinal > 0))
        self.assertEqual(geometry["overlap"]["default_tokens"], 0)

    def test_normal_structural_chunks_have_zero_overlap(self):
        text = "\n".join("这是第%d段完整说明。" % i for i in range(160))
        doc = poc.SourceDocument("liba", "synthetic:normal", "说明", "说明.md", text)
        _, children, _ = poc.project_documents([doc])
        self.assertGreater(len(children), 1)
        self.assertTrue(all(c.overlap_tokens == 0 for c in children))

    def test_oversized_long_line_tokenizes_once_not_once_per_chunk(self):
        text = "连续长行" * 3_000
        original = poc._token_spans
        with mock.patch.object(poc, "_token_spans", wraps=original) as spans:
            parts = poc._split_oversized(text, 500, 40)
        self.assertGreater(len(parts), 10)
        spans.assert_called_once_with(text)
        self.assertTrue(all(part for part, _overlap, _forced in parts))

    def test_template_dedup_is_corpus_level_and_content_hash_reported(self):
        boiler = "固定模板签名：本页内容仅供内部合成测试。\n"
        docs = [poc.SourceDocument("liba", f"d{i}", f"标题{i}", f"d{i}.md",
                                   boiler + f"独有正文{i}。\n") for i in range(3)]
        cleaned, report = poc.deduplicate_templates(docs)
        self.assertEqual(report["template_count"], 1)
        self.assertEqual(report["removed_line_instances"], 3)
        self.assertEqual(len(report["template_hashes"]), 1)
        self.assertTrue(all("固定模板签名" not in d.text for d in cleaned))
        self.assertTrue(all(f"独有正文{i}" in cleaned[i].text for i in range(3)))

    def test_exact_field_projection_is_separated(self):
        exact = poc.extract_exact_fields(
            "合同 CT-55，供应商：北京星河科技有限公司；负责人陈雨桐于2027-11-30确认，API已归档。",
            "合同清单.xlsx")
        self.assertIn("CT-55", exact["identifiers"])
        self.assertIn("2027-11-30", exact["date_values"])
        self.assertIn("北京星河科技有限公司", exact["company_names"])
        self.assertIn("陈雨桐", exact["person_names"])
        self.assertIn("合同清单.xlsx", exact["filenames"])
        self.assertIn("API", exact["acronyms"])


class IndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = poc.load_stage_a_corpus()
        cls.parents, cls.children, cls.geometry = poc.project_documents(cls.docs)

    def test_source_body_candidate_changes_only_source_not_postings(self):
        without = poc.build_local_index(self.children, "legacy_123gram", False)
        with_body = poc.build_local_index(self.children, "legacy_123gram", True)
        self.assertEqual(without.postings, with_body.postings)
        self.assertGreater(with_body.source_bytes, without.source_bytes)
        self.assertGreater(with_body.serialized_bytes, without.serialized_bytes)
        self.assertTrue(all("body" not in row for row in without.source.values()))
        self.assertTrue(all("body" in row for row in with_body.source.values()))

    def test_kb_filter_prevents_cross_library_leak(self):
        index = poc.build_local_index(self.children, "legacy_123gram", False)
        allowed = poc.search(index, "基线", ["libb"])
        denied = poc.search(index, "基线", ["liba"])
        self.assertIn("docdb:701", allowed)
        self.assertNotIn("docdb:701", denied)

    def test_g20_merged_fixture_is_contaminated_but_review_scope_is_honest(self):
        for analyzer in poc.ANALYZERS:
            index = poc.build_local_index(self.children, analyzer, False)
            merged = poc.search(index, "甲乙丙丁", ["liba"])
            scoped = poc.search(index, "甲乙丙丁", ["liba"],
                                doc_id_prefixes=["docdb:"])
            self.assertEqual(set(merged), {"synthetic:801", "synthetic:802"}, analyzer)
            self.assertEqual(scoped, [], analyzer)

    def test_out_of_scope_documents_cannot_change_scoped_scores_or_order(self):
        scoped_children = [c for c in self.children if c.doc_id.startswith("synthetic:")]
        seed = next(c for c in scoped_children if c.doc_id == "synthetic:801")
        outsiders = [
            replace(seed, chunk_id=f"outside-chunk-{i}", parent_id=f"outside-parent-{i}",
                    doc_id=f"outside:{i}", body=("星河科技有限公司 " * (i + 1_000)))
            for i in range(3)
        ]
        for analyzer in poc.ANALYZERS:
            base = poc.build_local_index(scoped_children, analyzer, False)
            base_scores = poc.search_scores(
                base, "星河科技有限公司", ["liba"], doc_id_prefixes=["synthetic:"])
            expanded = poc.build_local_index([*scoped_children, *outsiders], analyzer, False)
            expanded_scores = poc.search_scores(
                expanded, "星河科技有限公司", ["liba"], doc_id_prefixes=["synthetic:"])
            self.assertEqual(base_scores, expanded_scores, analyzer)

    def test_all_three_analyzer_contracts_are_executable_and_labeled(self):
        for name, (_fn, level, note) in poc.ANALYZERS.items():
            index = poc.build_local_index(self.children, name, False)
            self.assertGreater(index.terms, 0, name)
            self.assertGreater(index.posting_count, 0, name)
            self.assertIn(level, {"measured", "equivalent_simulation"})
            if name != "legacy_123gram":
                self.assertEqual(level, "equivalent_simulation")
                self.assertIn("no analysis-", note)


class BenchmarkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = poc.run_benchmark()

    def test_consumes_all_72_gold_with_per_case_results_and_structured_skips(self):
        self.assertEqual(self.report["gold_cases_consumed"], 72)
        for run in self.report["runs"]:
            rows = run["quality"]["cases"]
            self.assertEqual(len(rows), 72)
            self.assertEqual(len({r["id"] for r in rows}), 72)
            for row in rows:
                if row["status"] == "SKIP":
                    self.assertTrue(row["excluded_from_quality_denominator"])
                    self.assertTrue(row["reason"])
            self.assertEqual(run["quality"]["quality_denominator"], 53)

    def test_required_metrics_and_analyzer_evidence_are_present(self):
        expected = {(name, source) for name in poc.ANALYZERS for source in
                    ("body_excluded_from_source", "body_in_source")}
        actual = {(r["analyzer"], r["source_model"]) for r in self.report["runs"]}
        self.assertEqual(actual, expected)
        for run in self.report["runs"]:
            metrics = run["metrics"]
            for key in ("index_bytes", "terms", "postings", "chunks", "docs",
                        "build_time_ms", "peak_rss_bytes"):
                self.assertIn(key, metrics)
                self.assertGreater(metrics[key], 0, (run["analyzer"], key))

    def test_report_refuses_to_call_probe_opensearch_evidence(self):
        self.assertEqual(self.report["stage_b_decision"], "NO-GO")
        self.assertFalse(self.report["gates"]["three_library_opensearch_primary_store"]["pass"])
        levels = {r["analyzer"]: r["evidence_level"] for r in self.report["runs"]}
        self.assertEqual(levels["legacy_123gram"], "measured")
        self.assertEqual(levels["icu_equivalent_probe"], "equivalent_simulation")
        self.assertEqual(levels["smartcn_equivalent_probe"], "equivalent_simulation")
        self.assertTrue(any("not OpenSearch primary store" in x
                            for x in self.report["measurement_limits"]))


if __name__ == "__main__":
    unittest.main()
