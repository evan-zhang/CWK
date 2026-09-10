#!/usr/bin/env python3
"""Behavior, confidentiality, schema, and fairness gates for RT-055."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import kb_retrieval_decision as decision  # noqa: E402

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def freeze_receipt(*, weknora: bool = False) -> dict:
    digest = DIGEST_B if weknora else DIGEST_A
    candidate_id = decision.CANDIDATE_B if weknora else decision.CANDIDATE_A
    receipt = {
        "receipt_id": decision.FREEZE_RECEIPT_B if weknora else decision.FREEZE_RECEIPT_A,
        "candidate_id": candidate_id, "code_digest": digest, "image_digest": digest,
        "config_digest": digest, "mapping_digest": digest, "query_plan_digest": digest,
        "dependency_digest": digest, "frozen_before_run": True, "ops_artifacts_verified": True,
    }
    if weknora:
        receipt.update({
            "head_matches_commit": True, "tree_clean": True, "native_config": True,
            "upstream_receipt": {
                "receipt_id": decision.UPSTREAM_RECEIPT_B,
                "repository_id": decision.WEKNORA_REPOSITORY,
                "commit": decision.WEKNORA_COMMIT, "commit_reachable": True,
                "verified_on_ops": True,
            },
        })
    return receipt


def library_metrics(*, recall_hits: int = 46, exact_hits: int = 10,
                    no_answer_correct: int = 10, leak: int = 0,
                    multiplier: float = 1) -> dict:
    return {
        "total_count": 60, "answerable_count": 50, "recall_hits_at_10": recall_hits,
        "exact_count": 10, "exact_hits": exact_hits, "no_answer_count": 10,
        "no_answer_correct": no_answer_correct, "system_error_count": 0,
        "answerable_system_error_count": 0, "exact_system_error_count": 0,
        "no_answer_system_error_count": 0, "timeout_count": 0,
        "recall_at_10": recall_hits / 50,
        "exact": exact_hits / 10, "no_answer": no_answer_correct / 10,
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
        "freeze_receipt": freeze_receipt(),
        "libraries": {kb: library_metrics() for kb in decision.LIBRARIES},
        "operations": {"components_count": 3, "upgrade_steps_count": 4,
                       "backup_restore_steps_count": 4},
        "gateway_readiness": {"https_query_api": True, "per_gateway_identity": True,
                              "kb_grants_server_side": True,
                              "no_direct_nas_or_search_credentials": True},
    }


def candidate_b(*, multiplier: float = 1) -> dict:
    return {
        "identity": {"kind": "weknora", "commit": decision.WEKNORA_COMMIT,
                     "native_pipeline": True, "core_modified": False},
        "freeze_receipt": freeze_receipt(weknora=True),
        "libraries": {kb: library_metrics(multiplier=multiplier) for kb in decision.LIBRARIES},
        "operations": {"components_count": 6, "upgrade_steps_count": 8,
                       "backup_restore_steps_count": 7},
        "gateway_readiness": {"https_query_api": True, "per_gateway_identity": True,
                              "kb_grants_server_side": True,
                              "no_direct_nas_or_search_credentials": True},
    }


def valid_report() -> dict:
    return {
        "schema": decision.SCHEMA,
        "run_id": "12345678-1234-4234-8234-123456789abc",
        "holdout_contract": {
            "version": decision.HOLDOUT_VERSION, "created_after_rt054": True,
            "rt054_final_holdout_reused": False, "libraries": list(decision.LIBRARIES),
            "private_artifacts_retained_on_ops": True, "aggregate_only_export": True,
            "single_use_frozen_before_candidate_runs": True,
        },
        "environment": {
            "hardware_class": decision.HARDWARE_CLASS,
            "corpus_snapshot_version": decision.CORPUS_SNAPSHOT_VERSION,
            "same_corpus": True, "same_hardware_class": True,
            "same_timeout_budget": True, "same_top_k": True, "top_k": 10,
            "run_order_randomized": True,
        },
        "verifier_attestations": {
            "builder_id": decision.BUILDER_ID, "verifier_id": decision.VERIFIER_ID,
            "candidate_implementer_id": decision.IMPLEMENTER_ID, "builder_separated": True,
            "rt054_pool_excluded": True, "input_disjoint_verified": True,
            "category_coverage_verified": True, "aggregate_only_verified": True,
            "no_private_digest_exported": True,
        },
        "candidates": {decision.CANDIDATE_A: candidate_a(), decision.CANDIDATE_B: candidate_b()},
        "cleanup": {"private_holdout_retained_on_ops": True,
                    "temporary_indices_zero": True, "temporary_services_zero": True,
                    "temporary_containers_zero": True, "cleanup_failures": 0},
        "production_invariants": {"nas_unchanged": True, "gateway_unchanged": True,
                                  "existing_indices_unchanged": True,
                                  "production_config_unchanged": True},
    }


class ConfidentialAndFreezeTests(unittest.TestCase):
    def test_valid_report_and_json_schema(self):
        report = valid_report()
        decision.validate_report(report)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema unavailable")
        schema = json.loads((PROJECT / "RT/RT-055/contracts/aggregate-report.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)

    def test_no_private_case_shape_or_private_digest(self):
        serialized = json.dumps(valid_report())
        for forbidden in ("query", "expected_doc", "source_text", "locator", "case_id", "case_hash"):
            self.assertNotIn(f'"{forbidden}"', serialized)

    def test_rt054_reuse_and_missing_independent_attestation_rejected(self):
        report = valid_report(); report["holdout_contract"]["rt054_final_holdout_reused"] = True
        with self.assertRaisesRegex(decision.ReportError, "must not be reused"):
            decision.validate_report(report)
        report = valid_report(); report["verifier_attestations"]["input_disjoint_verified"] = False
        with self.assertRaisesRegex(decision.ReportError, "must be true"):
            decision.validate_report(report)

    def test_freeze_must_precede_run_and_real_artifacts_are_verified(self):
        for candidate_id, field in ((decision.CANDIDATE_A, "frozen_before_run"),
                                    (decision.CANDIDATE_B, "ops_artifacts_verified")):
            report = valid_report(); report["candidates"][candidate_id]["freeze_receipt"][field] = False
            with self.subTest(candidate=candidate_id, field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)

    def test_weknora_head_clean_native_and_upstream_provenance_required(self):
        for field in ("head_matches_commit", "tree_clean", "native_config"):
            report = valid_report(); report["candidates"][decision.CANDIDATE_B]["freeze_receipt"][field] = False
            with self.subTest(field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)
        report = valid_report()
        report["candidates"][decision.CANDIDATE_B]["freeze_receipt"]["upstream_receipt"]["commit"] = "0" * 40
        with self.assertRaisesRegex(decision.ReportError, "commit mismatch"):
            decision.validate_report(report)

    def test_roles_receipts_candidate_binding_and_upstream_are_fixed(self):
        report = valid_report(); report["verifier_attestations"]["builder_id"] = decision.VERIFIER_ID
        with self.assertRaises(decision.ReportError):
            decision.validate_report(report)
        report = valid_report(); report["candidates"][decision.CANDIDATE_A]["freeze_receipt"]["candidate_id"] = decision.CANDIDATE_B
        with self.assertRaises(decision.ReportError):
            decision.validate_report(report)
        report = valid_report(); report["candidates"][decision.CANDIDATE_B]["freeze_receipt"]["upstream_receipt"]["repository_id"] = "attacker-repo"
        with self.assertRaises(decision.ReportError):
            decision.validate_report(report)

    def test_critical_candidate_artifacts_cannot_all_be_identical(self):
        report = valid_report()
        a = report["candidates"][decision.CANDIDATE_A]["freeze_receipt"]
        b = report["candidates"][decision.CANDIDATE_B]["freeze_receipt"]
        for field in ("code_digest", "image_digest", "config_digest", "mapping_digest", "query_plan_digest"):
            b[field] = a[field]
        with self.assertRaisesRegex(decision.ReportError, "must not all be identical"):
            decision.validate_report(report)

    def test_arbitrary_strings_paths_and_malformed_uuid_rejected(self):
        mutations = [
            ("hardware_class", "private words from a case"),
            ("hardware_class", "b" * 64),
            ("corpus_snapshot_version", "/Volumes/private"),
        ]
        for field, value in mutations:
            report = valid_report(); report["environment"][field] = value
            with self.subTest(field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)
        for value in ("12345678-1234-4234-8234-123456789ab-", "{12345678-1234-4234-8234-123456789abc}", "12345678123442348234123456789abc"):
            report = valid_report(); report["run_id"] = value
            with self.subTest(value=value), self.assertRaises(decision.ReportError):
                decision.validate_report(report)


class CountAndMeasurementTests(unittest.TestCase):
    def test_empty_category_cannot_claim_perfect_score(self):
        for field in ("total_count", "answerable_count", "exact_count", "no_answer_count"):
            report = valid_report(); report["candidates"][decision.CANDIDATE_A]["libraries"]["cwork-3m"][field] = 0
            with self.subTest(field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)

    def test_denominators_numerators_and_fixed_rates_are_consistent(self):
        edits = [
            ("total_count", 61), ("exact_count", 51), ("recall_hits_at_10", 51),
            ("exact_hits", 11), ("no_answer_correct", 11), ("recall_at_10", 1.0),
            ("timeout_count", 1), ("answerable_system_error_count", 1),
        ]
        for field, value in edits:
            report = valid_report(); report["candidates"][decision.CANDIDATE_A]["libraries"]["cwork-3m"][field] = value
            with self.subTest(field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)

    def test_candidate_denominators_must_match_same_frozen_cases(self):
        report = valid_report()
        row = report["candidates"][decision.CANDIDATE_B]["libraries"]["cwork-3m"]
        row["total_count"] = 61
        row["no_answer_count"] = 11
        row["no_answer"] = 10 / 11
        with self.assertRaisesRegex(decision.ReportError, "must match across candidates"):
            decision.validate_report(report)

    def test_errors_and_timeouts_cannot_coexist_with_perfect_scores(self):
        report = valid_report()
        row = report["candidates"][decision.CANDIDATE_A]["libraries"]["cwork-3m"]
        row.update(system_error_count=1, answerable_system_error_count=1,
                   exact_system_error_count=1, timeout_count=1)
        with self.assertRaisesRegex(decision.ReportError, "system errors as failures"):
            decision.validate_report(report)

    def test_unmeasured_or_zero_resources_and_components_are_invalid(self):
        for field in decision.RESOURCE_FIELDS:
            report = valid_report(); report["candidates"][decision.CANDIDATE_A]["libraries"]["cwork-3m"][field] = 0
            with self.subTest(field=field), self.assertRaises(decision.ReportError):
                decision.validate_report(report)
        report = valid_report(); report["candidates"][decision.CANDIDATE_A]["operations"]["components_count"] = 0
        with self.assertRaises(decision.ReportError):
            decision.validate_report(report)


class DecisionBehaviorTests(unittest.TestCase):
    def test_a_is_incumbent_when_b_has_no_precommitted_material_win(self):
        self.assertEqual(decision.decide(valid_report())["selected"], decision.CANDIDATE_A)

    def test_quality_gates_are_per_library(self):
        report = valid_report()
        row = report["candidates"][decision.CANDIDATE_A]["libraries"]["docdb-touqian"]
        row["exact_hits"] = 9; row["exact"] = .9
        result = decision.decide(report)
        self.assertIn("docdb-touqian:exact", result["candidate_gates"][decision.CANDIDATE_A]["failures"])
        self.assertEqual(result["selected"], decision.CANDIDATE_B)

    def test_b_can_displace_only_with_mechanical_complexity_and_three_wins(self):
        report = valid_report(); b = candidate_b(multiplier=.79)
        b["operations"] = {"components_count": 3, "upgrade_steps_count": 4, "backup_restore_steps_count": 4}
        report["candidates"][decision.CANDIDATE_B] = b
        self.assertEqual(decision.decide(report)["selected"], decision.CANDIDATE_B)
        worse_component = copy.deepcopy(report)
        worse_component["candidates"][decision.CANDIDATE_B]["operations"]["components_count"] = 4
        self.assertEqual(decision.decide(worse_component)["selected"], decision.CANDIDATE_A)

    def test_p95_cannot_be_summed_to_hide_one_library_regression(self):
        report = valid_report(); b = candidate_b(multiplier=.70)
        b["operations"] = {"components_count": 3, "upgrade_steps_count": 4, "backup_restore_steps_count": 4}
        # Total latency is still lower, but one library regresses: B is rejected.
        b["libraries"]["cwork-3m"]["p95_ms"] = 401
        report["candidates"][decision.CANDIDATE_B] = b
        self.assertEqual(decision.decide(report)["selected"], decision.CANDIDATE_A)

    def test_total_resource_win_cannot_hide_over_ten_percent_library_regression(self):
        report = valid_report(); b = candidate_b(multiplier=.70)
        b["operations"] = {"components_count": 3, "upgrade_steps_count": 4, "backup_restore_steps_count": 4}
        b["libraries"]["cwork-3m"]["index_bytes"] = 111_000_000
        report["candidates"][decision.CANDIDATE_B] = b
        self.assertEqual(decision.decide(report)["selected"], decision.CANDIDATE_A)

    def test_neither_passing_is_no_go(self):
        report = valid_report()
        for candidate in report["candidates"].values():
            row = candidate["libraries"]["cwork-3m"]
            row["recall_hits_at_10"] = 44; row["recall_at_10"] = .88
        result = decision.decide(report)
        self.assertEqual(result["status"], "NO-GO"); self.assertIsNone(result["selected"])


class RepositoryEvidenceTests(unittest.TestCase):
    def test_schema_freezes_ids_commit_and_v2(self):
        schema = json.loads((PROJECT / "RT/RT-055/contracts/aggregate-report.schema.json").read_text())
        self.assertEqual(schema["$id"], decision.SCHEMA)
        self.assertEqual(set(schema["properties"]["candidates"]["required"]), {decision.CANDIDATE_A, decision.CANDIDATE_B})
        self.assertEqual(schema["$defs"]["candidateB"]["properties"]["identity"]["const"]["commit"], decision.WEKNORA_COMMIT)

    def test_rt054_is_narrow_historical_projection_not_architecture_failure(self):
        evidence = json.loads((PROJECT / "RT/RT-054/evidence/stage-b-ops-quality-20260909.json").read_text())
        self.assertEqual(evidence["quality_gate_scope"], "lexical_analyzer_and_mapping_selection_only")
        self.assertTrue(all(row["storage"]["icu_reduction_vs_old_lexical_percent"] >= 94 for row in evidence["libraries"].values()))
        self.assertEqual(evidence["decision"]["stage_b"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
