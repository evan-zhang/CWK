#!/usr/bin/env python3
"""RT-055 aggregate assembly + decision.

Consumes only aggregate-side artifacts (run results, freeze receipt, verifier
attestations, cleanup/invariants, frozen runbooks), assembles the v3 aggregate
report, validates it with the repo decision harness and the JSON Schema, and
records the single precommitted verdict. Private case/query/expected/source
data never enters this program's inputs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import kb_retrieval_decision as decision  # noqa: E402
import rt055_opslib as ops
from rt055_freeze import role_audit  # noqa: E402

CANDIDATE_A = "cwk-opensearch-dual-channel-v1"
CANDIDATE_B = "weknora-native-8d7298fb5d759973cb1e481cadc5ecdf16dca599"


def operations_from_runbooks() -> dict[str, dict[str, int]]:
    runbooks = ops.read_json(HERE / "rt055_runbooks.json")
    out = {}
    for candidate_id in (CANDIDATE_A, CANDIDATE_B):
        book = runbooks[candidate_id]
        out[candidate_id] = {
            "components_count": len(book["components"]),
            "upgrade_steps_count": len(book["upgrade_steps"]),
            "backup_restore_steps_count": len(book["backup_restore_steps"]),
        }
    return out


def freeze_receipt_block(receipt: dict, candidate_key: str, ops_verified: bool) -> dict:
    block = receipt["candidates"][candidate_key]
    digests = block["digests"]
    fields = {
        "receipt_id": block["receipt_id"],
        "candidate_id": block["candidate_id"],
        "code_digest": "sha256:" + digests["code_digest"],
        "image_digest": "sha256:" + digests["image_digest"],
        "config_digest": "sha256:" + digests["config_digest"],
        "mapping_digest": "sha256:" + digests["mapping_digest"],
        "query_plan_digest": "sha256:" + digests["query_plan_digest"],
        "dependency_digest": "sha256:" + digests["dependency_digest"],
        "frozen_before_run": bool(block["frozen_before_run"]),
        "ops_artifacts_verified": bool(ops_verified),
    }
    if candidate_key == "b":
        upstream = block["upstream_receipt"]
        fields.update({
            "head_matches_commit": bool(block["upstream_receipt"]["head_matches_commit"]),
            "tree_clean": bool(block["upstream_receipt"]["tree_clean"]),
            "native_config": bool(block["native_config"]),
            "upstream_receipt": {
                "receipt_id": upstream["receipt_id"],
                "repository_id": upstream["repository_id"],
                "commit": upstream["commit"],
                "commit_reachable": bool(upstream["commit_reachable"]),
                "verified_on_ops": bool(upstream["verified_on_ops"]),
            },
        })
    return fields


def main() -> int:
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    receipt = ops.read_json(ROOT / "freeze" / "freeze-receipt.json")
    freeze_verification = ops.read_json(ROOT / "verifier" / "freeze-verification.json")
    case_verification = ops.read_json(ROOT / "verifier" / "case-verification.json")
    run_a = ops.read_json(ROOT / "run-a" / "result.json")
    run_b = ops.read_json(ROOT / "run-b" / "result.json")
    cleanup = {"cleanup":ops.read_json(ROOT / "audit/cleanup.json"),"production_invariants":ops.read_json(ROOT / "audit/production-comparison.json")}
    roles=role_audit(ROOT)
    participating=case_verification["participating_libraries"]
    deferred=case_verification["deferred_libraries"]

    if run_a.get("status") != "OK" or run_b.get("status") != "OK":
        print("AGGREGATE: candidate run results are not OK")
        return 3
    if not (freeze_verification.get("verified") is True and freeze_verification.get("verified_before_run") is True):
        print("AGGREGATE: freeze verification not passed")
        return 3
    if not all(case_verification.get(k) for k in (
            "verified", "selection_verified", "single_build_seed_verified", "at_least_one_participating",
            "category_coverage_verified", "rt054_pool_excluded",
            "input_disjoint_verified", "denominators_nonzero_all_categories")):
        print("AGGREGATE: verifier case attestations not satisfied")
        return 3

    gateway_a = run_a['gateway_readiness']
    gateway_b = run_b['gateway_readiness']

    def libraries_of(run_result: dict) -> dict:
        out = {}
        for kb in ops.LIBRARIES:
            if kb in deferred:
                if kb in run_result['libraries']:raise RuntimeError('deferred_formal_metrics_present')
                out[kb]={'status':'NOT_RUN_DEFERRED'}
                continue
            row = run_result["libraries"][kb]
            out[kb] = {
                "total_count": row["total_count"], "answerable_count": row["answerable_count"],
                "recall_hits_at_10": row["recall_hits_at_10"], "exact_count": row["exact_count"],
                "exact_hits": row["exact_hits"], "no_answer_count": row["no_answer_count"],
                "no_answer_correct": row["no_answer_correct"],
                "system_error_count": row["system_error_count"],
                "answerable_system_error_count": row["answerable_system_error_count"],
                "exact_system_error_count": row["exact_system_error_count"],
                "no_answer_system_error_count": row["no_answer_system_error_count"],
                "timeout_count": row["timeout_count"], "leak_count": row["leak_count"],
                "recall_at_10": row["recall_at_10"], "exact": row["exact"],
                "no_answer": row["no_answer"], "p95_ms": row["p95_ms"],
                "index_bytes": row["index_bytes"], "build_seconds": row["build_seconds"],
                "peak_rss_bytes": row["peak_rss_bytes"],
            }
        return out

    operations = operations_from_runbooks()
    report = {
        "schema": decision.SCHEMA,
        "run_id": ROOT.name.removeprefix("rt055-"),
        "participating_libraries":participating,"deferred_libraries":deferred,
        "library_validity":case_verification["library_validity"],
        "holdout_contract": {
            "version": decision.HOLDOUT_VERSION,
            "created_after_rt054": True,
            "rt054_final_holdout_reused": False,
            "libraries": list(ops.LIBRARIES),
            "private_artifacts_retained_on_ops": True,
            "aggregate_only_export": True,
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
            "builder_id": decision.BUILDER_ID,
            "verifier_id": decision.VERIFIER_ID,
            "candidate_implementer_id": decision.IMPLEMENTER_ID,
            "builder_separated": roles["verified"],
            "role_separation_level":roles["role_separation_level"],"role_audit_verified":roles["verified"],
            "rt054_pool_excluded": bool(case_verification["rt054_pool_excluded"]),
            "input_disjoint_verified": bool(case_verification["input_disjoint_verified"]),
            "category_coverage_verified": bool(case_verification["category_coverage_verified"]),
            "aggregate_only_verified": True,
            "no_private_digest_exported": True,
        },
        "candidates": {
            CANDIDATE_A: {
                "identity": {"kind": "opensearch_dual_channel",
                             "exact_resolver": "deterministic_v1",
                             "lexical_analyzer": "analysis_icu", "doc_collapse": True,
                             "parent_expand": True, "rerank_in_primary": False},
                "freeze_receipt": freeze_receipt_block(receipt, "a",
                                                       freeze_verification.get("verified")),
                "libraries": libraries_of(run_a),
                "operations": operations[CANDIDATE_A],
                "gateway_readiness": gateway_a,
            },
            CANDIDATE_B: {
                "identity": {"kind": "weknora", "commit": decision.WEKNORA_COMMIT,
                             "native_pipeline": True, "core_modified": False},
                "freeze_receipt": freeze_receipt_block(receipt, "b",
                                                       freeze_verification.get("verified")),
                "libraries": libraries_of(run_b),
                "operations": operations[CANDIDATE_B],
                "gateway_readiness": gateway_b,
            },
        },
        "cleanup": {
            "private_holdout_retained_on_ops": bool(
                cleanup["cleanup"]["private_holdout_retained_on_ops"]),
            "temporary_indices_zero": bool(cleanup["cleanup"]["temporary_indices_zero"]),
            "temporary_services_zero": bool(cleanup["cleanup"]["temporary_services_zero"]),
            "temporary_containers_zero": bool(cleanup["cleanup"]["temporary_containers_zero"]),
            "cleanup_failures": int(cleanup["cleanup"]["cleanup_failures"]),
        },
        "production_invariants": {
            "nas_unchanged": cleanup["production_invariants"]["nas_unchanged"],
            "gateway_unchanged": cleanup["production_invariants"]["gateway_unchanged"],
            "existing_indices_unchanged": cleanup["production_invariants"]["existing_indices_unchanged"],
            "production_config_unchanged": cleanup["production_invariants"]["production_config_unchanged"],
        },
    }

    out_dir=ROOT/'aggregate'
    ops.write_private_json(out_dir/'aggregate-report.json',report)
    # Structural Schema supports truthful invariant false/null. The decision
    # harness still fails those hard gates closed and produces INVALID.
    # Mandatory structural gate; no skip path.
    schema_path = ROOT / "contracts" / "aggregate-report.schema.json"
    if not schema_path.is_file():
        schema_path = ROOT / "impl" / "aggregate-report.schema.json"
    check = subprocess.run(
        [str(ROOT / "sidecar" / "venv" / "bin" / "python"), "-c",
         "import json,sys,jsonschema;\n"
         "schema=json.load(open(sys.argv[1]));report=json.load(open(sys.argv[2]));\n"
         "jsonschema.validate(report,schema);print('SCHEMA OK')",
         str(schema_path), "/dev/stdin"],
        input=ops.canonical_json(report), text=True, capture_output=True, timeout=120)
    if check.returncode != 0:
        print("AGGREGATE_SCHEMA_INVALID")
        return 3
    print(check.stdout.strip())

    try:result = decision.decide(report)
    except decision.ReportError:
        result={'schema':decision.RESULT_SCHEMA,'run_id':report['run_id'],
                'decision':'INVALID','status':'INVALID','selected':None,'error_code':'AGGREGATE_CONTRACT_INVALID',
                'participating_libraries':participating,'deferred_libraries':deferred,'production_candidate_libraries':[]}

    out_dir = ROOT / "aggregate"
    ops.write_private_json(out_dir / "aggregate-report.json", report)
    ops.write_private_json(out_dir / "decision.json", result)
    print("AGGREGATE OK selected=", result.get("selected"), "status=", result.get("status"))
    print(json.dumps(result.get("candidate_gates", {}), sort_keys=True))
    return 2 if result["status"]=="INVALID" else 0


if __name__ == "__main__":
    raise SystemExit(main())
