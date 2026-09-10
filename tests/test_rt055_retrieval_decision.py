#!/usr/bin/env python3
"""Behavior gates for the RT-055 aggregate-only decision harness."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_retrieval_decision as decision  # noqa: E402


def library_metrics(*, recall: float = .92, exact: float = 1,
                    no_answer: float = 1, leak: int = 0,
                    multiplier: float = 1) -> dict:
    return {
        "recall_at_10": recall, "exact": exact, "no_answer": no_answer,
        "leak_count": leak, "p95_ms": 400 * multiplier,
        "index_bytes": int(100_000_000 * multiplier),
        "build_seconds": 1000 * multiplier,
        "peak_rss_bytes": int(2_000_000_000 * multiplier),
    }


def candidate_a() -> dict:
    return {
        "identity": {
            "kind": "opensearch_dual_channel", "exact_resolver": "deterministic_v1",
            "lexical_analyzer": "analysis_icu", "doc_collapse": True,
            "parent_expand": True, "rerank_in_primary": False,
        },
        "libraries": {kb: library_metrics() for kb in decision.LIBRARIES},
        "operations": {"complexity_score": 4, "components_count": 3,
                       "upgrade_steps_count": 4, "backup_restore_steps_count": 4},
        "gateway_readiness": {"https_query_api": True, "per_gateway_identity": True,
                              "kb_grants_server_side": True,
                              "no_direct_nas_or_search_credentials": True},
    }


def candidate_b(*, multiplier: float = 1) -> dict:
    return {
        "identity": {"kind": "weknora", "commit": decision.WEKNORA_COMMIT,
                     "native_pipeline": True, "core_modified": False},
        "libraries": {kb: library_metrics(multiplier=multiplier)
                      for kb in decision.LIBRARIES},
        "operations": {"complexity_score": 6, "components_count": 6,
                       "upgrade_steps_count": 8, "backup_restore_steps_count": 7},
        "gateway_readiness": {"https_query_api": True, "per_gateway_identity": True,
                              "kb_grants_server_side": True,
                              "no_direct_nas_or_search_credentials": True},
    }


def valid_report() -> dict:
    return {
        "schema": decision.SCHEMA,
        "run_id": "12345678-1234-4234-8234-123456789abc",
        "holdout_contract": {
            "version": "ops-rt055-private-v1", "created_after_rt054": True,
            "rt054_final_holdout_reused": False,
            "libraries": list(decision.LIBRARIES),
            "private_artifacts_retained_on_ops": True,
            "aggregate_only_export": True,
            "single_use_frozen_before_candidate_runs": True,
        },
        "environment": {
            "hardware_class": "ops-equivalent-a", "corpus_snapshot_version": "private-v1",
            "same_corpus": True, "same_hardware_class": True,
            "same_timeout_budget": True, "same_top_k": True, "top_k": 10,
            "run_order_randomized": True,
        },
        "candidates": {decision.CANDIDATE_A: candidate_a(),
                       decision.CANDIDATE_B: candidate_b()},
        "cleanup": {"private_holdout_retained_on_ops": True,
                    "temporary_indices_zero": True, "temporary_services_zero": True,
                    "temporary_containers_zero": True, "cleanup_failures": 0},
        "production_invariants": {"nas_unchanged": True, "gateway_unchanged": True,
                                  "existing_indices_unchanged": True,
                                  "production_config_unchanged": True},
    }


class ConfidentialContractTests(unittest.TestCase):
    def test_valid_aggregate_contains_no_private_case_shape(self):
        report = valid_report()
        decision.validate_report(report)
        serialized = json.dumps(report)
        for forbidden in ("query", "expected_doc", "source_text", "locator", "case_id"):
            self.assertNotIn(f'"{forbidden}"', serialized)

    def test_rt054_holdout_reuse_is_rejected(self):
        report = valid_report()
        report["holdout_contract"]["rt054_final_holdout_reused"] = True
        with self.assertRaisesRegex(decision.ReportError, "must not be reused"):
            decision.validate_report(report)

    def test_query_or_private_path_leak_is_rejected(self):
        for key, value in (("query", "private words"), ("note", "/Volumes/private/source")):
            report = valid_report()
            report["environment"][key] = value
            with self.subTest(key=key), self.assertRaises(decision.ReportError):
                decision.validate_report(report)

    def test_candidate_identity_drift_is_rejected(self):
        report = valid_report()
        report["candidates"][decision.CANDIDATE_A]["identity"]["rerank_in_primary"] = True
        with self.assertRaisesRegex(decision.ReportError, "candidate A identity"):
            decision.validate_report(report)
        report = valid_report()
        report["candidates"][decision.CANDIDATE_B]["identity"]["core_modified"] = True
        with self.assertRaisesRegex(decision.ReportError, "unmodified native WeKnora"):
            decision.validate_report(report)

    def test_cleanup_or_production_mutation_is_rejected(self):
        report = valid_report()
        report["cleanup"]["temporary_indices_zero"] = False
        with self.assertRaisesRegex(decision.ReportError, "must be true"):
            decision.validate_report(report)
        report = valid_report()
        report["production_invariants"]["gateway_unchanged"] = False
        with self.assertRaisesRegex(decision.ReportError, "production invariants"):
            decision.validate_report(report)


class DecisionBehaviorTests(unittest.TestCase):
    def test_a_is_default_when_b_has_no_material_operating_win(self):
        result = decision.decide(valid_report())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["selected"], decision.CANDIDATE_A)

    def test_per_library_exact_failure_cannot_hide_in_averages(self):
        report = valid_report()
        report["candidates"][decision.CANDIDATE_A]["libraries"]["docdb-touqian"]["exact"] = .99
        result = decision.decide(report)
        self.assertFalse(result["candidate_gates"][decision.CANDIDATE_A]["pass"])
        self.assertIn("docdb-touqian:exact",
                      result["candidate_gates"][decision.CANDIDATE_A]["failures"])
        self.assertEqual(result["selected"], decision.CANDIDATE_B)

    def test_recall_no_answer_leak_and_gateway_are_independent_hard_gates(self):
        mutations = [
            ("libraries", "cwork-3m", "recall_at_10", .89, "cwork-3m:recall_at_10"),
            ("libraries", "spbp-2027", "no_answer", .9, "spbp-2027:no_answer"),
            ("libraries", "docdb-touqian", "leak_count", 1, "docdb-touqian:leak"),
        ]
        for _kind, kb, metric, value, failure in mutations:
            report = valid_report()
            report["candidates"][decision.CANDIDATE_A]["libraries"][kb][metric] = value
            with self.subTest(failure=failure):
                result = decision.decide(report)
                self.assertIn(failure, result["candidate_gates"][decision.CANDIDATE_A]["failures"])
        report = valid_report()
        report["candidates"][decision.CANDIDATE_A]["gateway_readiness"]["https_query_api"] = False
        self.assertIn("gateway_readiness",
                      decision.decide(report)["candidate_gates"][decision.CANDIDATE_A]["failures"])

    def test_neither_candidate_passing_returns_no_go(self):
        report = valid_report()
        for candidate in report["candidates"].values():
            candidate["libraries"]["cwork-3m"]["recall_at_10"] = .89
        result = decision.decide(report)
        self.assertEqual(result["status"], "NO-GO")
        self.assertIsNone(result["selected"])

    def test_b_displaces_a_only_with_three_twenty_percent_wins_and_no_more_complexity(self):
        report = valid_report()
        b = candidate_b(multiplier=.79)
        b["operations"]["complexity_score"] = 4
        report["candidates"][decision.CANDIDATE_B] = b
        self.assertEqual(decision.decide(report)["selected"], decision.CANDIDATE_B)

        not_enough = copy.deepcopy(report)
        for kb in decision.LIBRARIES:
            not_enough["candidates"][decision.CANDIDATE_B]["libraries"][kb]["peak_rss_bytes"] = \
                report["candidates"][decision.CANDIDATE_A]["libraries"][kb]["peak_rss_bytes"]
            not_enough["candidates"][decision.CANDIDATE_B]["libraries"][kb]["build_seconds"] = \
                report["candidates"][decision.CANDIDATE_A]["libraries"][kb]["build_seconds"]
        self.assertEqual(decision.decide(not_enough)["selected"], decision.CANDIDATE_A)


class RepositoryEvidenceTests(unittest.TestCase):
    def test_schema_and_runtime_freeze_same_candidate_ids_and_commit(self):
        schema = json.loads((PROJECT / "RT/RT-055/contracts/aggregate-report.schema.json")
                            .read_text(encoding="utf-8"))
        required = schema["properties"]["candidates"]["required"]
        self.assertEqual(set(required), {decision.CANDIDATE_A, decision.CANDIDATE_B})
        identity = schema["$defs"]["candidateB"]["properties"]["identity"]["const"]
        self.assertEqual(identity["commit"], decision.WEKNORA_COMMIT)
        self.assertFalse(identity["core_modified"])

    def test_rt054_final_evidence_still_has_narrow_scope_and_storage_win(self):
        evidence = json.loads((PROJECT / "RT/RT-054/evidence/stage-b-ops-quality-20260909.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(evidence["quality_gate_scope"],
                         "lexical_analyzer_and_mapping_selection_only")
        reductions = [row["storage"]["icu_reduction_vs_old_lexical_percent"]
                      for row in evidence["libraries"].values()]
        self.assertTrue(all(value >= 94 for value in reductions))
        self.assertEqual(evidence["decision"]["stage_b"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
