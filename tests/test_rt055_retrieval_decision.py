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
        "window_id":"87654321-4321-4321-8321-cba987654321",
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


def library_metrics(*, recall_hits: int = 34, exact_hits: int = 8,
                    no_answer_correct: int = 5, leak: int = 0,
                    multiplier: float = 1) -> dict:
    return {
        "total_count": 42, "answerable_count": 37, "recall_hits_at_10": recall_hits,
        "exact_count": 8, "exact_hits": exact_hits, "no_answer_count": 5,
        "no_answer_correct": no_answer_correct, "system_error_count": 0,
        "answerable_system_error_count": 0, "exact_system_error_count": 0,
        "no_answer_system_error_count": 0, "timeout_count": 0,
        "recall_at_10": recall_hits / 37,
        "exact": exact_hits / 8, "no_answer": no_answer_correct / 5,
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
        "formal_window":{
            **{k:"87654321-4321-4321-8321-cba987654321" for k in ('window_id','before_window_id','after_window_id','freeze_window_id','verification_window_id')},
            'privacy_migration_id':'23456789-1234-4234-8234-123456789abc','executioner_commit':'1'*40,
            'privacy_revalidation_verified':True,'before_verified':True,'after_comparison_verified':True,'run_order':['a','b']},
        "participating_libraries": list(decision.LIBRARIES), "deferred_libraries": [],
        "library_validity": {kb: {
            'status':'PARTICIPATING','tier':'T3','category_counts':dict(decision.tiers.TARGETS),'total_count':42,
            'floors':dict(decision.tiers.FLOORS),'total_floor':16,'targets':dict(decision.tiers.TARGETS),
            'trace':[{'tier':'T3','category_counts':dict(decision.tiers.TARGETS),'total_count':42,'floor_pass':True}],
            'same_build_and_seed':True,'complete_pool_verified':True} for kb in decision.LIBRARIES},
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
            "role_separation_level":"PROCESS_LEVEL_SEPARATION_SINGLE_UID", "role_audit_verified":True,
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
        row["total_count"] = 43
        row["no_answer_count"] = 6
        row["no_answer"] = 5 / 6
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
        for field in ("index_bytes", "peak_rss_bytes"):
            report = valid_report()
            report["candidates"][decision.CANDIDATE_A]["libraries"]["cwork-3m"][field] = 1.5
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
        row["exact_hits"] = 7; row["exact"] = 7 / 8
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
            row["recall_hits_at_10"] = 32; row["recall_at_10"] = 32 / 37
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


def simplified_report():
    report = valid_report()
    report['PRIVACY_MODE'] = 'SIMPLIFIED'
    report['privacy_check'] = 'SIMPLIFIED_SPOT_PASS'
    report['caveats'] = list(decision.SIMPLIFIED_CAVEATS)
    for candidate in report['candidates'].values():
        receipt = candidate['freeze_receipt']
        for field in decision.DIGEST_FIELDS:
            del receipt[field]
        receipt.update(artifact_digests_verified_on_ops=True, candidate_artifacts_distinct_on_ops=True)
    report['formal_window'] = {
        'window_id': '12345678-1234-4234-8234-123456789abc',
        'canonical_freeze_window_id': '87654321-4321-4321-8321-cba987654321',
        'run_order': ['a', 'b'], 'same_frozen_paper': True, 'five_digests_verified': True,
        'reconciled_a_trial_count': 126, 'new_a_trial_count': 0, 'new_b_trial_count': 126}
    report['verifier_attestations'] = {k: True for k in ('rt054_pool_excluded',
        'input_disjoint_verified', 'category_coverage_verified', 'aggregate_only_verified', 'no_private_digest_exported')}
    return report


class SimplifiedExamTests(unittest.TestCase):
    def test_no_historical_role_or_firewall_gate(self):
        report = simplified_report()
        self.assertEqual(decision.decide(report)['decision'], 'A')

    def test_reconciled_a_missing_resources_are_caveated_not_zero_or_invalid(self):
        report = simplified_report()
        report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['peak_rss_bytes'] = None
        result = decision.decide(report)
        self.assertEqual(result['decision'], 'A')
        self.assertEqual(result['caveats'], report['caveats'])
        self.assertEqual(result['measurement_gaps'], ['A:cwork-3m:peak_rss_bytes'])
        self.assertIsNone(report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['peak_rss_bytes'])

    def test_schema_accepts_truthful_nulls(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema unavailable')
        report = simplified_report()
        report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['build_seconds'] = None
        schema = json.loads((PROJECT / 'RT/RT-055/contracts/simplified-aggregate-v3.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(report, schema)
        bad = copy.deepcopy(report)
        bad['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['query'] = 'secret'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(bad, schema)

    def test_private_data_rejected(self):
        for key, value in (('query', 'secret'), ('query_hash', 'a'*64), ('locator', '/private/file')):
            report = simplified_report(); report[key] = value
            with self.assertRaises(decision.ReportError): decision.decide(report)

    def test_replayed_or_wrong_order_trials_rejected(self):
        for changes in ({'new_a_trial_count': 1}, {'run_order': ['b', 'a']}, {'new_b_trial_count': 125}):
            report = simplified_report(); report['formal_window'].update(changes)
            with self.assertRaises(decision.ReportError): decision.decide(report)

    def test_wrong_paper_or_freeze_rejected(self):
        for key in ('same_frozen_paper', 'five_digests_verified'):
            report = simplified_report(); report['formal_window'][key] = False
            with self.assertRaises(decision.ReportError): decision.decide(report)
        report = simplified_report()
        report['formal_window']['canonical_freeze_window_id'] = report['formal_window']['window_id']
        with self.assertRaises(decision.ReportError): decision.decide(report)

    def test_measured_drift_is_not_attributed_or_silently_cleared(self):
        report = simplified_report()
        report['production_invariants'].update(nas_unchanged=None, production_config_unchanged=False)
        self.assertEqual(decision.decide(report)['decision'], 'A')
        self.assertIsNone(report['production_invariants']['nas_unchanged'])
        self.assertFalse(report['production_invariants']['production_config_unchanged'])

    def test_privacy_failure_and_unknown_not_pass(self):
        report = simplified_report(); report['privacy_check'] = 'FAIL'
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')
        report['privacy_check'] = 'UNVERIFIED'
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')

    def test_cleanup_failure_prevents_selection(self):
        report = simplified_report(); report['cleanup']['temporary_services_zero'] = False
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')

    def test_quality_and_denominators_unchanged(self):
        report = simplified_report()
        for candidate in report['candidates'].values():
            m = candidate['libraries']['cwork-3m']; m['no_answer_correct'] = 0; m['no_answer'] = 0
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')
        report = simplified_report()
        report['candidates'][decision.CANDIDATE_B]['libraries']['cwork-3m']['total_count'] -= 1
        with self.assertRaises(decision.ReportError): decision.decide(report)

    def test_digest_export_is_rejected_by_schema_and_harness(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema unavailable')
        import re
        report = simplified_report()
        self.assertIsNone(re.search(r'[0-9a-fA-F]{64}', json.dumps(report)))
        schema = json.loads((PROJECT / 'RT/RT-055/contracts/simplified-aggregate-v3.schema.json').read_text())
        for field in decision.DIGEST_FIELDS:
            bad = copy.deepcopy(report)
            bad['candidates'][decision.CANDIDATE_A]['freeze_receipt'][field] = DIGEST_A
            with self.assertRaises(decision.ReportError): decision.decide(bad)
            with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(bad, schema)

    def test_simplified_schema_structural_negatives(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema unavailable')
        schema = json.loads((PROJECT / 'RT/RT-055/contracts/simplified-aggregate-v3.schema.json').read_text())
        mutations = [
            (('privacy_check',), 'PASS'),
            (('PRIVACY_MODE',), 'FULL'),
            (('run_id',), '/private/example'),
            (('query',), 'private fixture'),
            (('formal_window', 'new_b_trial_count'), -1),
            (('formal_window', 'new_a_trial_count'), True),
            (('formal_window', 'same_frozen_paper'), False),
            (('holdout_contract', 'rt054_final_holdout_reused'), True),
            (('environment', 'top_k'), 11),
            (('verifier_attestations', 'no_private_digest_exported'), False),
            (('cleanup', 'cleanup_failures'), -1),
            (('cleanup', 'temporary_services_zero'), 'true'),
            (('production_invariants', 'nas_unchanged'), 0),
            (('candidates', decision.CANDIDATE_B, 'identity', 'core_modified'), True),
            (('candidates', decision.CANDIDATE_A, 'gateway_readiness', 'https_query_api'), 1),
            (('candidates', decision.CANDIDATE_A, 'libraries', 'cwork-3m', 'peak_rss_bytes'), 0),
        ]
        for path, value in mutations:
            with self.subTest(path=path):
                report = simplified_report()
                parent = report
                for key in path[:-1]: parent = parent[key]
                parent[path[-1]] = value
                with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(report, schema)
                with self.assertRaises(decision.ReportError): decision.decide(report)

    def test_ops_only_artifact_proof_is_required(self):
        report = simplified_report()
        for field in ('artifact_digests_verified_on_ops', 'candidate_artifacts_distinct_on_ops'):
            bad = copy.deepcopy(report)
            bad['candidates'][decision.CANDIDATE_B]['freeze_receipt'][field] = False
            with self.assertRaises(decision.ReportError): decision.decide(bad)

    def test_privacy_fail_cannot_be_hidden_by_missing_measurements(self):
        report = simplified_report()
        report['privacy_check'] = 'FAIL'
        report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['build_seconds'] = None
        result = decision.decide(report)
        self.assertEqual(result['decision'], 'NO-GO')
        self.assertEqual(result['privacy_check'], 'FAIL')
        self.assertEqual(result['reason'], 'SIMPLIFIED_PRIVACY_SPOT_FAIL')

    def test_unknown_gateway_prevents_selection_not_contract_validity(self):
        report = simplified_report()
        report['candidates'][decision.CANDIDATE_A]['gateway_readiness']['https_query_api'] = None
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')

    def test_all_reconciled_a_resource_nulls_preserved_with_b_hard_fail(self):
        report = simplified_report()
        for kb in decision.LIBRARIES:
            for key in ('index_bytes', 'build_seconds', 'peak_rss_bytes'):
                report['candidates'][decision.CANDIDATE_A]['libraries'][kb][key] = None
            b = report['candidates'][decision.CANDIDATE_B]['libraries'][kb]
            b.update(no_answer_correct=0, no_answer=0)
        original = copy.deepcopy(report)
        result = decision.decide(report)
        self.assertEqual(result['decision'], 'A')
        self.assertEqual(len(result['measurement_gaps']), 9)
        self.assertEqual(result['PRIVACY_MODE'], 'SIMPLIFIED')
        self.assertEqual(result['caveats'], report['caveats'])
        self.assertFalse(result['candidate_gates'][decision.CANDIDATE_B]['pass'])
        self.assertEqual(report, original)

    def test_missing_resources_cannot_prove_b_displacement(self):
        report = simplified_report()
        a, b = (report['candidates'][cid] for cid in (decision.CANDIDATE_A, decision.CANDIDATE_B))
        b['operations'] = dict(a['operations'])
        for kb in decision.LIBRARIES:
            for key in decision.RESOURCE_FIELDS:
                b['libraries'][kb][key] *= .5
        self.assertEqual(decision.decide(report)['decision'], 'B')
        a['libraries']['cwork-3m']['build_seconds'] = None
        self.assertEqual(decision.decide(report)['decision'], 'A')

    def test_required_missing_measurements_are_no_go(self):
        for cid, key in ((decision.CANDIDATE_A, 'p95_ms'), (decision.CANDIDATE_B, 'build_seconds')):
            report = simplified_report()
            report['candidates'][cid]['libraries']['cwork-3m'][key] = None
            result = decision.decide(report)
            self.assertEqual(result['decision'], 'NO-GO')
            self.assertEqual(result['reason'], 'INCOMPLETE_REQUIRED_MEASUREMENTS')
            self.assertEqual(result['caveats'], report['caveats'])
        report = simplified_report()
        report['formal_window'].update(reconciled_a_trial_count=0, new_a_trial_count=126)
        report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']['build_seconds'] = None
        self.assertEqual(decision.decide(report)['decision'], 'NO-GO')

    def test_required_caveats_schema_and_harness_negatives(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema unavailable')
        schema = json.loads((PROJECT / 'RT/RT-055/contracts/simplified-aggregate-v3.schema.json').read_text())
        mutations = [list(decision.SIMPLIFIED_CAVEATS[:i] + decision.SIMPLIFIED_CAVEATS[i+1:]) for i in range(5)]
        mutations += [list(decision.SIMPLIFIED_CAVEATS) + [decision.SIMPLIFIED_CAVEATS[0]],
                      list(decision.SIMPLIFIED_CAVEATS) + ['UNKNOWN_CAVEAT'], None]
        for caveats in mutations:
            report = simplified_report(); report['caveats'] = caveats
            with self.assertRaises(decision.ReportError): decision.decide(report)
            with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(report, schema)
        report = simplified_report(); del report['caveats']
        with self.assertRaises(decision.ReportError): decision.decide(report)
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(report, schema)

    def test_cli_preserves_simplified_caveats_for_a_b_and_no_go(self):
        import contextlib
        import io
        import tempfile
        for desired in ('A', 'B', 'NO-GO'):
            report = simplified_report()
            if desired != 'A':
                a = report['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m']
                a.update(no_answer_correct=0, no_answer=0)
            if desired == 'NO-GO': report['privacy_check'] = 'FAIL'
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / 'aggregate.json'; p.write_text(json.dumps(report))
                out = io.StringIO()
                with contextlib.redirect_stdout(out): rc = decision.main([str(p)])
            result = json.loads(out.getvalue())
            self.assertEqual(result['decision'], desired)
            self.assertEqual(rc, 3 if desired == 'NO-GO' else 0)
            self.assertEqual(result['PRIVACY_MODE'], 'SIMPLIFIED')
            self.assertEqual(result['caveats'], report['caveats'])


if __name__ == "__main__":
    unittest.main()
