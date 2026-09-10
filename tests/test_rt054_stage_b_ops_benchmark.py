#!/usr/bin/env python3
"""RT-054 OPS confidential known-item quality-closure contracts."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_stage_b_ops_benchmark as bench  # noqa: E402
import kb_stage_b_ops_cases as casegen  # noqa: E402
import kb_stage_b_ops_verify as verifier  # noqa: E402


def sample_docs(count: int = 40) -> list[dict]:
    docs = []
    for i in range(count):
        unique = f"QX-{i:04d}-ZZ"
        table = f"\n|项目|编码|说明|\n|---|---|---|\n|项目{i}|{unique}|独有表格内容{i}|\n" if i % 5 == 0 else ""
        docs.append({
            "doc_id": f"d{i:03d}", "title": f"第{i}号项目 {unique}", "filename": f"project-{i:03d}.md",
            "body": (f"共同邻近主题 计划治理。第{i}号资料包含独有正文片段甲乙{i}丙丁，"
                     f"精确日期 2027-12-{i % 28 + 1:02d}。{table}") * 3,
        })
    return docs


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_stratified_sampling_is_repeatable(self):
        docs = sample_docs()
        first = casegen.locked_expected_documents("lib", docs, "fixed")
        second = casegen.locked_expected_documents("lib", list(reversed(docs)), "fixed")
        self.assertEqual(first, second)
        self.assertEqual(len({row.doc_id for row in first}), len(docs))
        self.assertEqual([row.ordinal for row in first], list(range(len(docs))))

    def test_expected_documents_are_locked_before_query_derivation(self):
        events = []
        original_lock = casegen.locked_expected_documents
        original_candidates = casegen.title_filename_candidates

        def lock(*args, **kwargs):
            events.append("lock")
            return original_lock(*args, **kwargs)

        def candidates(doc):
            events.append("query")
            return original_candidates(doc)

        with mock.patch.object(casegen, "locked_expected_documents", side_effect=lock), \
             mock.patch.object(casegen, "title_filename_candidates", side_effect=candidates):
            casegen.derive_cases_for_library("lib", sample_docs(), "fixed")
        self.assertEqual(events[0], "lock")
        self.assertIn("query", events[1:])

    def test_category_split_is_assigned_before_query_derivation(self):
        events = []
        original_split = casegen.split_for_category_ordinal
        original_candidates = casegen.title_filename_candidates

        def split(*args, **kwargs):
            events.append("split")
            return original_split(*args, **kwargs)

        def candidates(doc):
            events.append("query")
            return original_candidates(doc)

        with mock.patch.object(casegen, "split_for_category_ordinal", side_effect=split), \
             mock.patch.object(casegen, "title_filename_candidates", side_effect=candidates):
            casegen.derive_cases_for_library("lib", sample_docs(), "fixed")
        first_query = events.index("query")
        self.assertIn("split", events[:first_query])

    def test_generator_has_no_search_or_opensearch_dependency(self):
        source = Path(casegen.__file__).read_text(encoding="utf-8")
        self.assertNotIn("OpenSearch", source)
        self.assertNotIn("/_search", source)
        self.assertNotIn("kb_stage_b_opensearch", source)

    def test_verifier_does_not_import_generator_implementation(self):
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import kb_stage_b_ops_cases", source)
        self.assertNotIn("from kb_stage_b_ops_cases", source)
        docs = sample_docs()
        generated_order = [x.doc_id for x in casegen.locked_expected_documents("lib", docs, "fixed")]
        self.assertEqual(verifier.independent_locked_order("lib", docs, "fixed"), generated_order)


class IndependentVerifierTests(unittest.TestCase):
    def setUp(self):
        self.docs = sample_docs()
        self.corpus = {"libraries": {"lib": self.docs}}
        self.generated = casegen.generate(self.corpus, "fixed")

    def test_generator_claim_does_not_count_when_source_disagrees(self):
        row = self.generated["libraries"]["lib"]["cases"][0]
        row["query"] = "definitely-absent-private-query"
        result = verifier.verify(self.corpus, self.generated)["libraries"]["lib"]
        self.assertEqual(result["rejections"]["reason_counts"]["query_not_in_expected"], 1)
        self.assertEqual(result["case_total"], len(self.generated["libraries"]["lib"]["cases"]) - 1)

    def test_expected_lock_is_recomputed_not_trusted(self):
        row = self.generated["libraries"]["lib"]["cases"][0]
        row["expected_doc_id"] = self.docs[-1]["doc_id"]
        result = verifier.verify(self.corpus, self.generated)["libraries"]["lib"]
        self.assertEqual(result["rejections"]["reason_counts"]["expected_lock_mismatch"], 1)

    def test_category_constraints_are_checked_from_corpus(self):
        library = self.generated["libraries"]["lib"]
        body_case = next(row for row in library["cases"] if row["category"] == "body_only_rare_phrase")
        doc = next(doc for doc in self.docs if doc["doc_id"] == body_case["expected_doc_id"])
        doc["title"] += " " + body_case["query"]
        result = verifier.verify(self.corpus, self.generated)["libraries"]["lib"]
        self.assertGreaterEqual(result["rejections"]["reason_counts"]["body_only_constraint"], 1)

    def test_no_answer_mutation_must_be_absent_from_full_library(self):
        library = self.generated["libraries"]["lib"]
        case = next(row for row in library["cases"] if row["category"] == "no_answer_mutation")
        self.docs[-1]["body"] += " " + case["query"]
        result = verifier.verify(self.corpus, self.generated)["libraries"]["lib"]
        self.assertEqual(result["rejections"]["reason_counts"]["no_answer_present"], 1)

    def test_near_neighbour_relation_must_exist(self):
        library = self.generated["libraries"]["lib"]
        case = next(row for row in library["cases"] if row["category"] == "near_neighbour")
        case["neighbour_doc_id"] = "missing"
        result = verifier.verify(self.corpus, self.generated)["libraries"]["lib"]
        self.assertEqual(result["rejections"]["reason_counts"]["near_neighbour_constraint"], 1)


class SafeReportTests(unittest.TestCase):
    def test_private_root_guard_prevents_broad_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="cwk-rt054-quality.") as td:
            root = Path(td)
            root.chmod(0o700)
            (root / ".rt054-owned").touch(mode=0o600)
            scripts = root / "scripts"
            scripts.mkdir(mode=0o700)
            (scripts / Path(bench.__file__).name).touch(mode=0o600)
            with mock.patch.object(bench, "HERE", scripts):
                bench.validate_private_root(root)
            (root / ".rt054-owned").unlink()
            with mock.patch.object(bench, "HERE", scripts), self.assertRaises(RuntimeError):
                bench.validate_private_root(root)
        with self.assertRaises(RuntimeError):
            bench.validate_private_root(PROJECT)

    def minimal_report(self):
        return {
            "schema": "cwk.rt054.ops-quality-closure.v1", "status": "FAIL",
            "run_id": "00000000-0000-4000-8000-000000000000",
            "sampling_strategy_version": casegen.SAMPLING_VERSION,
            "error": "benchmark_error", "failed_phase": "preflight",
            "decision": {"stage_b": "NO-GO", "selected_analyzer": None,
                         "selected_mapping": None, "reason": "benchmark_error"},
            "cleanup": {"case_files_zero": True, "indices_zero": True,
                        "containers_zero": True, "derived_images_zero": True,
                        "workdirs_zero": True, "all_zero": True, "cleanup_failures": 0},
        }

    def test_only_allowlisted_aggregate_shape_is_serialized(self):
        report = self.minimal_report()
        self.assertEqual(json.loads(bench.safe_output(report)), report)
        for mutation in (
            {"query": "secret"}, {"detail": "not allowlisted"},
            {"case_set_sha256": "a" * 64}, {"locator": {"row": 1}},
        ):
            bad = {**report, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                bench.safe_output(bad)

    def test_sensitive_shapes_and_multiline_strings_are_rejected(self):
        for value in ("/Users/private/source", "/Volumes/private", "line one\nline two"):
            report = self.minimal_report()
            report["failed_phase"] = value
            with self.assertRaises(ValueError):
                bench.safe_output(report)


class QueryAndMappingV2Tests(unittest.TestCase):
    def test_icu_exact_query_and_index_values_share_nfkc_case_date_normalization(self):
        identifier = bench.quality_query("analysis_icu", {
            "kb_id": "lib", "category": "exact_identifier_date", "query": "ＡＢＣ－１２３"})
        date = bench.quality_query("analysis_icu", {
            "kb_id": "lib", "category": "exact_identifier_date", "query": "２０２７年３月５日"})
        self.assertEqual(identifier["query"]["bool"]["should"], [
            {"term": {"identifiers": {"value": "abc-123", "boost": 12}}}])
        self.assertEqual(date["query"]["bool"]["should"], [
            {"term": {"date_values": {"value": "2027-03-05", "boost": 12}}}])
        self.assertEqual(bench.normalize_keyword("ＡＢＣ－１２３"), "abc-123")
        self.assertEqual(bench.normalize_date("２０２７年３月５日"), "2027-03-05")

    def test_icu_v2_mapping_has_normalized_keyword_exact_fields_and_body_excluded(self):
        mapping = bench.mapping_for_quality("analysis_icu", False)
        self.assertEqual(mapping["mappings"]["_source"]["excludes"], ["body"])
        props = mapping["mappings"]["properties"]
        for field in ("identifiers", "date_values", "title_exact", "filenames_exact"):
            self.assertEqual(props[field], {"type": "keyword", "normalizer": "cwk_icu_keyword"})
        self.assertIn("icu_normalizer", mapping["settings"]["analysis"]["normalizer"]["cwk_icu_keyword"]["filter"])

    def test_general_icu_rules_are_phrase_and_and_without_corpus_or_ordinal_branching(self):
        for category in ("body_only_rare_phrase", "table_row", "near_neighbour"):
            query = bench.quality_query("analysis_icu", {
                "kb_id": "lib", "category": category, "query": "通用 稀有 短语"})
            should = query["query"]["bool"]["should"]
            self.assertEqual(should[0]["multi_match"]["type"], "phrase")
            self.assertEqual(should[1]["multi_match"]["operator"], "and")
            self.assertEqual(should[0]["multi_match"]["fields"], should[1]["multi_match"]["fields"])
        source = Path(bench.__file__).read_text(encoding="utf-8")
        quality_source = source[source.index("def quality_query"):source.index("def score_cases")]
        self.assertNotIn("failure_id", quality_source)
        self.assertNotIn("cwork-3m", quality_source)
        self.assertNotIn("docdb-touqian", quality_source)
        self.assertNotIn("spbp-2027", quality_source)


class ScoringGateTests(unittest.TestCase):
    def verified(self):
        corpus = {"libraries": {kb: sample_docs() for kb in bench.KBS}}
        return verifier.verify(corpus, casegen.generate(corpus, "fixed"))

    def scores(self, verified, *, icu_exact_miss=False, legacy_recall_low=True):
        result = {analyzer: {} for analyzer in bench.ANALYZERS}
        for analyzer in bench.ANALYZERS:
            for kb in bench.KBS:
                rows = []
                hit_total = hit_ok = exact_total = exact_ok = no_total = no_ok = 0
                holdout = [r for r in verified["libraries"][kb]["cases"] if r["split"] == "holdout"]
                exact_missed = False
                for row in holdout:
                    no = row["category"] == "no_answer_mutation"
                    passed = True
                    if analyzer == "legacy_123gram" and legacy_recall_low and not no:
                        passed = hit_total % 3 != 0
                    if (analyzer == "analysis_icu" and icu_exact_miss
                            and row["category"] == "exact_identifier_date" and not exact_missed):
                        passed = False; exact_missed = True
                    rows.append({"category": row["category"], "outcome": "no_evidence" if no else "hit",
                                 "passed": passed, "rank": None if no or not passed else 1})
                    if no:
                        no_total += 1; no_ok += int(passed)
                    else:
                        hit_total += 1; hit_ok += int(passed)
                        if row["category"] == "exact_identifier_date":
                            exact_total += 1; exact_ok += int(passed)
                result[analyzer][kb] = {
                    "recall": {"hits": hit_ok, "total": hit_total, "value": hit_ok / hit_total},
                    "exact": {"hits": exact_ok, "total": exact_total, "value": exact_ok / exact_total},
                    "no_answer": {"honest": no_ok, "total": no_total, "value": no_ok / no_total},
                    "kb_leaks": 0, "rows": rows,
                }
        return result

    @staticmethod
    def storage(reduction=True):
        return {kb: {"legacy_123gram": 1000, "analysis_icu": 100 if reduction else 300}
                for kb in bench.KBS}

    def test_legacy_is_baseline_only_candidate_must_pass_all_holdout_gates(self):
        verified = self.verified()
        libs, result = bench.aggregate_quality(verified, self.scores(verified), self.storage())
        self.assertTrue(all(row["legacy"]["recall"]["value"] < .90 for row in libs.values()))
        self.assertEqual(result["gates"]["legacy_baseline"]["status"], "BASELINE_ONLY")
        self.assertEqual(result["decision"], {
            "stage_b": "PASS", "selected_analyzer": "analysis_icu",
            "selected_mapping": "cwk-child-mapping-v2-icu-body-excluded",
            "reason": "all_holdout_and_storage_gates_pass",
        })
        _libs, failed = bench.aggregate_quality(
            verified, self.scores(verified, icu_exact_miss=True), self.storage())
        self.assertEqual(failed["decision"]["stage_b"], "NO-GO")
        self.assertEqual(failed["gates"]["icu_quality"]["status"], "FAIL")

    def test_split_is_versioned_stable_per_library_and_category(self):
        verified = self.verified()
        self.assertEqual(verified["split_version"], casegen.SPLIT_VERSION)
        for library in verified["libraries"].values():
            self.assertEqual(sum(r["split"] == "calibration" for r in library["cases"]), 14)
            self.assertEqual(sum(r["split"] == "holdout" for r in library["cases"]), 28)
            for category in casegen.CATEGORIES:
                rows = [r for r in library["cases"] if r["category"] == category]
                self.assertEqual([r["category_ordinal"] for r in rows], list(range(len(rows))))
                self.assertEqual({r["split"] for r in rows}, {"calibration", "holdout"})

    def test_storage_below_eighty_percent_reduction_forces_no_go(self):
        verified = self.verified()
        with mock.patch.dict(bench.ACCEPTED_OLD_LEXICAL_BYTES,
                             {kb: 1000 for kb in bench.KBS}, clear=True):
            storage = {kb: {"legacy_123gram": 1000, "analysis_icu": 300}
                       for kb in bench.KBS}
            _libs, result = bench.aggregate_quality(verified, self.scores(verified), storage)
        self.assertEqual(result["gates"]["storage_reduction"]["status"], "FAIL")
        self.assertEqual(result["decision"]["stage_b"], "NO-GO")

    def test_cleanup_or_invariant_failure_forces_no_go(self):
        report = {"status": "PASS", "decision": {"stage_b": "PASS",
                  "selected_analyzer": "analysis_icu", "selected_mapping": bench.SELECTED_MAPPING,
                  "reason": "all_holdout_and_storage_gates_pass"}}
        cleanup = {"all_zero": True, "cleanup_failures": 0}
        bench.enforce_safety_decision(report, cleanup, gateway_unchanged=False,
                                      old_index_unchanged=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["decision"]["stage_b"], "NO-GO")


class PriorEvidenceIntegrityTests(unittest.TestCase):
    def test_previous_ops_acceptance_sha_is_not_rewritten(self):
        path = PROJECT / "RT" / "RT-054" / "evidence" / "stage-b-ops-benchmark-20260909.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         "69c8b6db3058487ad29ba2025b850780b09a5fb0f25d90da557a1b75bcd12a48")

    def test_final_quality_evidence_is_allowlisted_and_scope_limited(self):
        path = PROJECT / "RT" / "RT-054" / "evidence" / "stage-b-ops-quality-20260909.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        bench.safe_output(report)
        self.assertEqual(report["schema"], "cwk.rt054.ops-quality-closure.v1")
        self.assertEqual(report["sampling_strategy_version"], casegen.SAMPLING_VERSION)
        self.assertEqual(report["split_version"], casegen.SPLIT_VERSION)
        self.assertEqual(report["quality_gate_scope"], "lexical_analyzer_and_mapping_selection_only")
        self.assertNotIn("smartcn", json.dumps(report["environment"]).lower())
        self.assertEqual(report["gates"]["legacy_baseline"]["status"], "BASELINE_ONLY")
        self.assertEqual(report["gates"]["icu_quality"]["status"], "FAIL")
        self.assertEqual(report["gates"]["icu_not_below_legacy"]["status"], "PASS")
        self.assertEqual(report["gates"]["storage_reduction"]["status"], "PASS")
        self.assertEqual(report["decision"], {"stage_b": "NO-GO",
                         "selected_analyzer": None, "selected_mapping": None,
                         "reason": "quality_gate_failed"})
        for row in report["libraries"].values():
            self.assertEqual(row["holdout_case_total"], 28)
            self.assertEqual(row["icu"]["no_answer"]["value"], 1.0)
            self.assertEqual(row["icu"]["kb_leaks"], 0)
            self.assertGreaterEqual(row["storage"]["icu_reduction_vs_old_lexical_percent"], 80.0)
        self.assertEqual({kb: row["icu"]["recall"]["value"]
                          for kb, row in report["libraries"].items()}, {
            "cwork-3m": 0.88, "docdb-touqian": 0.92, "spbp-2027": 0.96})
        self.assertEqual({kb: row["icu"]["exact"]["value"]
                          for kb, row in report["libraries"].items()}, {
            "cwork-3m": 1.0, "docdb-touqian": 0.8, "spbp-2027": 1.0})
        self.assertTrue(report["invariants"]["gateway_unchanged"])
        self.assertTrue(report["invariants"]["old_index_metadata_unchanged"])
        self.assertTrue(report["cleanup"]["all_zero"])
        self.assertEqual(report["cleanup"]["cleanup_failures"], 0)


if __name__ == "__main__":
    unittest.main()
