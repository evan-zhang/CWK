#!/usr/bin/env python3
"""RT-055 aggregate-only retrieval decision gate.

The confidential holdout and real artifact verification stay on OPS. This
program accepts only bounded aggregate counts, measurements, and attestations.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "cwk.rt055.retrieval-decision.aggregate.v2"
LIBRARIES = ("cwork-3m", "docdb-touqian", "spbp-2027")
CANDIDATE_A = "cwk-opensearch-dual-channel-v1"
CANDIDATE_B = "weknora-native-8d7298fb5d759973cb1e481cadc5ecdf16dca599"
WEKNORA_COMMIT = "8d7298fb5d759973cb1e481cadc5ecdf16dca599"
WEKNORA_REPOSITORY = "github.com/Tencent/WeKnora"
HOLDOUT_VERSION = "ops-rt055-confidential-v1"
HARDWARE_CLASS = "ops-rt055-equivalent-a"
CORPUS_SNAPSHOT_VERSION = "ops-rt055-frozen-corpus-v1"
BUILDER_ID = "ops-rt055-builder"
VERIFIER_ID = "ops-rt055-verifier"
IMPLEMENTER_ID = "cwk-rt055-candidate-implementer"
FREEZE_RECEIPT_A = "ops-rt055-freeze-candidate-a"
FREEZE_RECEIPT_B = "ops-rt055-freeze-candidate-b"
UPSTREAM_RECEIPT_B = "ops-rt055-weknora-upstream"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
FORBIDDEN_KEYS = {
    "query", "queries", "expected", "expected_doc", "expected_doc_id",
    "source", "source_text", "content", "body", "text", "excerpt", "hit",
    "hits", "title", "filename", "path", "locator", "doc_id", "parent_id",
    "child_id", "chunk_id", "case", "cases", "case_id", "case_hash",
    "query_hash", "expected_hash", "source_hash", "failure_example",
}
ALLOWED_TOP = {
    "schema", "run_id", "holdout_contract", "environment", "verifier_attestations",
    "candidates", "rerank_ablation", "cleanup", "production_invariants",
}
COUNT_FIELDS = {
    "total_count", "answerable_count", "recall_hits_at_10", "exact_count",
    "exact_hits", "no_answer_count", "no_answer_correct", "system_error_count",
    "answerable_system_error_count", "exact_system_error_count",
    "no_answer_system_error_count", "timeout_count", "leak_count",
}
RATE_FIELDS = {"recall_at_10", "exact", "no_answer"}
RESOURCE_FIELDS = {"p95_ms", "index_bytes", "build_seconds", "peak_rss_bytes"}
DIGEST_FIELDS = {
    "code_digest", "image_digest", "config_digest", "mapping_digest",
    "query_plan_digest", "dependency_digest",
}


class ReportError(ValueError):
    """Aggregate report violates the confidential decision contract."""


def _finite_number(value: Any, where: str, *, minimum: float = 0, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{where} must be a number")
    number = float(value)
    if not math.isfinite(number) or (number <= minimum if strict else number < minimum):
        op = ">" if strict else ">="
        raise ReportError(f"{where} must be finite and {op} {minimum}")
    return number


def _integer(value: Any, where: str, *, minimum: int = 0) -> int:
    number = _finite_number(value, where, minimum=minimum)
    if int(number) != number:
        raise ReportError(f"{where} must be an integer")
    return int(number)


def _constant(value: Any, expected: str, where: str) -> None:
    if value != expected:
        raise ReportError(f"{where} must be the protocol constant {expected}")


def _digest(value: Any, where: str) -> None:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ReportError(f"{where} must be a sha256 digest")


def _scan_safe(value: Any, where: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ReportError(f"{where} has a non-string key")
            lowered = key.lower()
            if lowered in FORBIDDEN_KEYS or any(token in lowered for token in ("query_text", "expected_", "source_")):
                raise ReportError(f"{where}.{key} is confidential and not aggregate")
            _scan_safe(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_safe(child, f"{where}[{index}]")
    elif isinstance(value, str):
        # String-bearing fields are subsequently bound to protocol constants,
        # a canonical run UUID, or non-confidential candidate artifact digests.
        # This first pass only rejects prose/control characters and paths not
        # equal to the one registered public upstream constant.
        if value == WEKNORA_REPOSITORY:
            return
        if len(value) > 80 or "\n" in value or "\r" in value or not re.fullmatch(r"[a-zA-Z0-9:._-]+", value):
            raise ReportError(f"{where} contains non-protocol string data")


def _require_keys(obj: Any, required: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ReportError(f"{where} must be an object")
    missing = required - set(obj)
    extra = set(obj) - required
    if missing or extra:
        raise ReportError(f"{where} keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    return obj


def _true_flags(obj: Mapping[str, Any], fields: tuple[str, ...], where: str) -> None:
    for field in fields:
        if obj[field] is not True:
            raise ReportError(f"{where}.{field} must be true")


def _validate_uuid(value: Any) -> None:
    if not isinstance(value, str):
        raise ReportError("run_id must be a canonical lowercase UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ReportError("run_id must be a canonical lowercase UUID") from exc
    if str(parsed) != value:
        raise ReportError("run_id must be a canonical lowercase UUID")


def _validate_freeze(candidate_id: str, receipt: Any, *, weknora: bool) -> None:
    common = DIGEST_FIELDS | {"receipt_id", "candidate_id", "frozen_before_run", "ops_artifacts_verified"}
    fields = common | ({"head_matches_commit", "tree_clean", "native_config", "upstream_receipt"} if weknora else set())
    receipt = _require_keys(receipt, fields, f"{candidate_id}.freeze_receipt")
    _constant(receipt["candidate_id"], candidate_id, f"{candidate_id}.freeze_receipt.candidate_id")
    expected_receipt = FREEZE_RECEIPT_B if weknora else FREEZE_RECEIPT_A
    _constant(receipt["receipt_id"], expected_receipt, f"{candidate_id}.freeze_receipt.receipt_id")
    for field in DIGEST_FIELDS:
        _digest(receipt[field], f"{candidate_id}.freeze_receipt.{field}")
    _true_flags(receipt, ("frozen_before_run", "ops_artifacts_verified"), f"{candidate_id}.freeze_receipt")
    if weknora:
        _true_flags(receipt, ("head_matches_commit", "tree_clean", "native_config"), f"{candidate_id}.freeze_receipt")
        provenance = _require_keys(receipt["upstream_receipt"], {
            "receipt_id", "repository_id", "commit", "commit_reachable", "verified_on_ops",
        }, f"{candidate_id}.freeze_receipt.upstream_receipt")
        _constant(provenance["receipt_id"], UPSTREAM_RECEIPT_B, "upstream_receipt.receipt_id")
        _constant(provenance["repository_id"], WEKNORA_REPOSITORY, "upstream_receipt.repository_id")
        if provenance["commit"] != WEKNORA_COMMIT:
            raise ReportError("WeKnora upstream receipt commit mismatch")
        _true_flags(provenance, ("commit_reachable", "verified_on_ops"), "upstream_receipt")


def validate_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping):
        raise ReportError("report must be an object")
    _scan_safe(report)
    required_top = ALLOWED_TOP - {"rerank_ablation"}
    missing = required_top - set(report)
    extra = set(report) - ALLOWED_TOP
    if missing or extra:
        raise ReportError(f"report keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    if report["schema"] != SCHEMA:
        raise ReportError(f"schema must be {SCHEMA}")
    _validate_uuid(report["run_id"])

    holdout = _require_keys(report["holdout_contract"], {
        "version", "created_after_rt054", "rt054_final_holdout_reused", "libraries",
        "private_artifacts_retained_on_ops", "aggregate_only_export",
        "single_use_frozen_before_candidate_runs",
    }, "holdout_contract")
    _constant(holdout["version"], HOLDOUT_VERSION, "holdout_contract.version")
    _true_flags(holdout, ("created_after_rt054", "private_artifacts_retained_on_ops", "aggregate_only_export", "single_use_frozen_before_candidate_runs"), "holdout_contract")
    if holdout["rt054_final_holdout_reused"] is not False:
        raise ReportError("RT-054 final holdout must not be reused")
    if holdout["libraries"] != list(LIBRARIES):
        raise ReportError("holdout libraries or order do not match the three-library contract")

    environment = _require_keys(report["environment"], {
        "hardware_class", "corpus_snapshot_version", "same_corpus", "same_hardware_class",
        "same_timeout_budget", "same_top_k", "top_k", "run_order_randomized",
    }, "environment")
    _constant(environment["hardware_class"], HARDWARE_CLASS, "environment.hardware_class")
    _constant(environment["corpus_snapshot_version"], CORPUS_SNAPSHOT_VERSION, "environment.corpus_snapshot_version")
    _true_flags(environment, ("same_corpus", "same_hardware_class", "same_timeout_budget", "same_top_k", "run_order_randomized"), "environment")
    if environment["top_k"] != 10:
        raise ReportError("environment.top_k must be 10")

    attest = _require_keys(report["verifier_attestations"], {
        "builder_id", "verifier_id", "candidate_implementer_id", "builder_separated",
        "rt054_pool_excluded", "input_disjoint_verified", "category_coverage_verified",
        "aggregate_only_verified", "no_private_digest_exported",
    }, "verifier_attestations")
    _constant(attest["builder_id"], BUILDER_ID, "verifier_attestations.builder_id")
    _constant(attest["verifier_id"], VERIFIER_ID, "verifier_attestations.verifier_id")
    _constant(attest["candidate_implementer_id"], IMPLEMENTER_ID, "verifier_attestations.candidate_implementer_id")
    if len({attest["builder_id"], attest["verifier_id"], attest["candidate_implementer_id"]}) != 3:
        raise ReportError("builder, verifier, and candidate implementer roles must be distinct")
    _true_flags(attest, ("builder_separated", "rt054_pool_excluded", "input_disjoint_verified", "category_coverage_verified", "aggregate_only_verified", "no_private_digest_exported"), "verifier_attestations")

    candidates = report["candidates"]
    if not isinstance(candidates, Mapping) or set(candidates) != {CANDIDATE_A, CANDIDATE_B}:
        raise ReportError("report must contain exactly the two frozen candidates")
    for candidate_id, candidate in candidates.items():
        _validate_candidate(candidate_id, candidate)
    a_freeze = candidates[CANDIDATE_A]["freeze_receipt"]
    b_freeze = candidates[CANDIDATE_B]["freeze_receipt"]
    if a_freeze["receipt_id"] == b_freeze["receipt_id"]:
        raise ReportError("candidate freeze receipt ids must differ")
    critical = ("code_digest", "image_digest", "config_digest", "mapping_digest", "query_plan_digest")
    if all(a_freeze[field] == b_freeze[field] for field in critical):
        raise ReportError("candidate critical artifact digests must not all be identical")
    # A and B score the identical frozen cases. Category denominators are
    # therefore invariant across candidates; disagreement means runner drift.
    for kb in LIBRARIES:
        a_row = candidates[CANDIDATE_A]["libraries"][kb]
        b_row = candidates[CANDIDATE_B]["libraries"][kb]
        for field in ("total_count", "answerable_count", "exact_count", "no_answer_count"):
            if a_row[field] != b_row[field]:
                raise ReportError(f"{kb}.{field} must match across candidates")

    if "rerank_ablation" in report:
        ablation = _require_keys(report["rerank_ablation"], {"enabled", "status", "recall_delta", "p95_delta_ms"}, "rerank_ablation")
        if ablation["enabled"] is not True or ablation["status"] not in {"PASS", "FAIL"}:
            raise ReportError("rerank_ablation must be an explicit enabled PASS/FAIL experiment")
        delta = _finite_number(ablation["recall_delta"], "rerank_ablation.recall_delta", minimum=-1)
        if delta > 1:
            raise ReportError("rerank_ablation.recall_delta must be <= 1")
        _finite_number(ablation["p95_delta_ms"], "rerank_ablation.p95_delta_ms", minimum=-1)

    cleanup = _require_keys(report["cleanup"], {"private_holdout_retained_on_ops", "temporary_indices_zero", "temporary_services_zero", "temporary_containers_zero", "cleanup_failures"}, "cleanup")
    _true_flags(cleanup, ("private_holdout_retained_on_ops", "temporary_indices_zero", "temporary_services_zero", "temporary_containers_zero"), "cleanup")
    if cleanup["cleanup_failures"] != 0:
        raise ReportError("cleanup_failures must be zero")

    invariants = _require_keys(report["production_invariants"], {"nas_unchanged", "gateway_unchanged", "existing_indices_unchanged", "production_config_unchanged"}, "production_invariants")
    _true_flags(invariants, tuple(invariants), "production_invariants")


def _validate_candidate(candidate_id: str, candidate: Any) -> None:
    candidate = _require_keys(candidate, {"identity", "freeze_receipt", "libraries", "operations", "gateway_readiness"}, f"candidates.{candidate_id}")
    identity = candidate["identity"]
    if candidate_id == CANDIDATE_A:
        expected = {"kind": "opensearch_dual_channel", "exact_resolver": "deterministic_v1", "lexical_analyzer": "analysis_icu", "doc_collapse": True, "parent_expand": True, "rerank_in_primary": False}
        if identity != expected:
            raise ReportError("candidate A identity drifted from the frozen mechanism")
        _validate_freeze(candidate_id, candidate["freeze_receipt"], weknora=False)
    else:
        expected = {"kind": "weknora", "commit": WEKNORA_COMMIT, "native_pipeline": True, "core_modified": False}
        if identity != expected:
            raise ReportError("candidate B must be unmodified native WeKnora at the frozen commit")
        _validate_freeze(candidate_id, candidate["freeze_receipt"], weknora=True)

    libraries = candidate["libraries"]
    if not isinstance(libraries, Mapping) or set(libraries) != set(LIBRARIES):
        raise ReportError(f"candidate {candidate_id} must report exactly three libraries")
    expected_fields = COUNT_FIELDS | RATE_FIELDS | RESOURCE_FIELDS
    for kb in LIBRARIES:
        metrics = _require_keys(libraries[kb], expected_fields, f"{candidate_id}.libraries.{kb}")
        counts = {field: _integer(metrics[field], f"{candidate_id}.{kb}.{field}") for field in COUNT_FIELDS}
        if min(counts["total_count"], counts["answerable_count"], counts["exact_count"], counts["no_answer_count"]) <= 0:
            raise ReportError(f"{candidate_id}.{kb} category denominators must be positive")
        if counts["total_count"] != counts["answerable_count"] + counts["no_answer_count"]:
            raise ReportError(f"{candidate_id}.{kb} total_count must equal answerable_count + no_answer_count")
        if counts["exact_count"] > counts["answerable_count"]:
            raise ReportError(f"{candidate_id}.{kb} exact_count exceeds answerable_count")
        for numerator, denominator in (("recall_hits_at_10", "answerable_count"), ("exact_hits", "exact_count"), ("no_answer_correct", "no_answer_count")):
            if counts[numerator] > counts[denominator]:
                raise ReportError(f"{candidate_id}.{kb}.{numerator} exceeds {denominator}")
        if counts["system_error_count"] != counts["answerable_system_error_count"] + counts["no_answer_system_error_count"]:
            raise ReportError(f"{candidate_id}.{kb} system errors must partition by answerable/no-answer")
        if counts["exact_system_error_count"] > counts["answerable_system_error_count"]:
            raise ReportError(f"{candidate_id}.{kb} exact errors exceed answerable errors")
        if counts["timeout_count"] > counts["system_error_count"] or counts["system_error_count"] > counts["total_count"]:
            raise ReportError(f"{candidate_id}.{kb} error counts are inconsistent")
        for numerator, denominator, errors in (
            ("recall_hits_at_10", "answerable_count", "answerable_system_error_count"),
            ("exact_hits", "exact_count", "exact_system_error_count"),
            ("no_answer_correct", "no_answer_count", "no_answer_system_error_count"),
        ):
            if counts[numerator] > counts[denominator] - counts[errors]:
                raise ReportError(f"{candidate_id}.{kb}.{numerator} must count system errors as failures")
        computed = {
            "recall_at_10": counts["recall_hits_at_10"] / counts["answerable_count"],
            "exact": counts["exact_hits"] / counts["exact_count"],
            "no_answer": counts["no_answer_correct"] / counts["no_answer_count"],
        }
        for field, expected in computed.items():
            rate = _finite_number(metrics[field], f"{candidate_id}.{kb}.{field}")
            if rate > 1 or not math.isclose(rate, expected, rel_tol=0, abs_tol=1e-12):
                raise ReportError(f"{candidate_id}.{kb}.{field} must equal its fixed count ratio")
        for field in RESOURCE_FIELDS:
            where = f"{candidate_id}.{kb}.{field}"
            if field in {"index_bytes", "peak_rss_bytes"}:
                if _integer(metrics[field], where, minimum=1) < 1:
                    raise ReportError(f"{where} must be >= 1")
            else:
                _finite_number(metrics[field], where, strict=True)

    operations = _require_keys(candidate["operations"], {"components_count", "upgrade_steps_count", "backup_restore_steps_count"}, f"candidates.{candidate_id}.operations")
    for field, value in operations.items():
        if _integer(value, f"{candidate_id}.operations.{field}", minimum=1) < 1:
            raise ReportError(f"{candidate_id}.operations.{field} must be >= 1")
    gateway = _require_keys(candidate["gateway_readiness"], {"https_query_api", "per_gateway_identity", "kb_grants_server_side", "no_direct_nas_or_search_credentials"}, f"candidates.{candidate_id}.gateway_readiness")
    if not all(isinstance(value, bool) for value in gateway.values()):
        raise ReportError(f"candidate {candidate_id} gateway readiness values must be booleans")


def candidate_gate(candidate: Mapping[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for kb in LIBRARIES:
        row = candidate["libraries"][kb]
        if row["recall_at_10"] < .90:
            failures.append(f"{kb}:recall_at_10")
        if row["exact"] != 1:
            failures.append(f"{kb}:exact")
        if row["no_answer"] != 1:
            failures.append(f"{kb}:no_answer")
        if row["leak_count"] != 0:
            failures.append(f"{kb}:leak")
    if not all(candidate["gateway_readiness"].values()):
        failures.append("gateway_readiness")
    return not failures, failures


def _materially_better_b(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Apply the precommitted incumbency utility after unbiased hard gates."""
    # Mechanical complexity vector: no subjective score and no component may grow.
    op_fields = ("components_count", "upgrade_steps_count", "backup_restore_steps_count")
    if any(b["operations"][field] > a["operations"][field] for field in op_fields):
        return False

    # Latency cannot be summed across libraries. It counts as a 20% win only
    # when every library improves; any per-library regression rejects B.
    if any(b["libraries"][kb]["p95_ms"] > a["libraries"][kb]["p95_ms"] for kb in LIBRARIES):
        return False
    wins = int(all(b["libraries"][kb]["p95_ms"] <= a["libraries"][kb]["p95_ms"] * .80 for kb in LIBRARIES))

    # Additive resource dimensions use three-library totals, but a candidate
    # may not hide a >10% per-library regression inside that total.
    for metric in ("index_bytes", "build_seconds", "peak_rss_bytes"):
        if any(b["libraries"][kb][metric] > a["libraries"][kb][metric] * 1.10 for kb in LIBRARIES):
            return False
        a_total = sum(a["libraries"][kb][metric] for kb in LIBRARIES)
        b_total = sum(b["libraries"][kb][metric] for kb in LIBRARIES)
        wins += int(b_total <= a_total * .80)
    return wins >= 3


def decide(report: Mapping[str, Any]) -> dict[str, Any]:
    validate_report(report)
    a, b = report["candidates"][CANDIDATE_A], report["candidates"][CANDIDATE_B]
    a_ok, a_failures = candidate_gate(a)
    b_ok, b_failures = candidate_gate(b)
    if a_ok and (not b_ok or not _materially_better_b(a, b)):
        selected, reason = CANDIDATE_A, "A_passes_precommitted_incumbency_utility"
    elif b_ok:
        selected, reason = CANDIDATE_B, "B_only_eligible_or_meets_precommitted_displacement_utility"
    else:
        selected, reason = None, "neither_candidate_passes_hard_gates"
    return {
        "schema": "cwk.rt055.retrieval-decision.result.v2", "run_id": report["run_id"],
        "selected": selected, "status": "PASS" if selected else "NO-GO", "reason": reason,
        "candidate_gates": {
            CANDIDATE_A: {"pass": a_ok, "failures": a_failures},
            CANDIDATE_B: {"pass": b_ok, "failures": b_failures},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aggregate_report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.aggregate_report.read_text(encoding="utf-8"))
        result = decide(report)
    except (OSError, json.JSONDecodeError, ReportError) as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
