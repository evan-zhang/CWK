#!/usr/bin/env python3
"""RT-055 aggregate-only retrieval decision gate.

The confidential holdout runner lives on OPS.  This repository-side program
accepts only its allowlisted aggregate report and returns a deterministic
candidate decision.  It never needs queries, expected documents, source text,
or source locators.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "cwk.rt055.retrieval-decision.aggregate.v1"
LIBRARIES = ("cwork-3m", "docdb-touqian", "spbp-2027")
CANDIDATE_A = "cwk-opensearch-dual-channel-v1"
CANDIDATE_B = "weknora-native-8d7298fb5d759973cb1e481cadc5ecdf16dca599"
WEKNORA_COMMIT = "8d7298fb5d759973cb1e481cadc5ecdf16dca599"
FORBIDDEN_KEYS = {
    "query", "queries", "expected", "expected_doc", "expected_doc_id",
    "source", "source_text", "content", "body", "text", "excerpt", "hit",
    "hits", "title", "filename", "path", "locator", "doc_id", "parent_id",
    "child_id", "chunk_id", "case", "cases", "case_id", "case_hash",
    "query_hash", "expected_hash", "source_hash", "failure_example",
}
ALLOWED_TOP = {
    "schema", "run_id", "holdout_contract", "environment", "candidates",
    "rerank_ablation", "cleanup", "production_invariants",
}
METRICS = {
    "recall_at_10", "exact", "no_answer", "leak_count", "p95_ms",
    "index_bytes", "build_seconds", "peak_rss_bytes",
}


class ReportError(ValueError):
    """Aggregate report violates the confidential decision contract."""


def _finite_number(value: Any, where: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{where} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ReportError(f"{where} must be finite and >= {minimum}")
    return number


def _scan_safe(value: Any, where: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ReportError(f"{where} has a non-string key")
            lowered = key.lower()
            if lowered in FORBIDDEN_KEYS or any(
                token in lowered for token in ("query_text", "expected_", "source_")
            ):
                raise ReportError(f"{where}.{key} is confidential and not aggregate")
            _scan_safe(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_safe(child, f"{where}[{index}]")
    elif isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise ReportError(f"{where} contains multiline text")
        if value.startswith(("/Users/", "/Volumes/", "/home/", "file://")):
            raise ReportError(f"{where} contains a private path")


def _require_keys(obj: Mapping[str, Any], required: set[str], where: str) -> None:
    missing = required - set(obj)
    extra = set(obj) - required
    if missing or extra:
        raise ReportError(f"{where} keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")


def validate_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping):
        raise ReportError("report must be an object")
    _scan_safe(report)
    extra = set(report) - ALLOWED_TOP
    if extra:
        raise ReportError(f"unexpected top-level keys: {sorted(extra)}")
    required_top = ALLOWED_TOP - {"rerank_ablation"}
    missing = required_top - set(report)
    if missing:
        raise ReportError(f"missing top-level keys: {sorted(missing)}")
    if report["schema"] != SCHEMA:
        raise ReportError(f"schema must be {SCHEMA}")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", str(report["run_id"])):
        raise ReportError("run_id must be a lowercase UUID")

    holdout = report["holdout_contract"]
    _require_keys(holdout, {
        "version", "created_after_rt054", "rt054_final_holdout_reused",
        "libraries", "private_artifacts_retained_on_ops", "aggregate_only_export",
        "single_use_frozen_before_candidate_runs",
    }, "holdout_contract")
    if holdout["created_after_rt054"] is not True:
        raise ReportError("holdout must be newly created after RT-054")
    if holdout["rt054_final_holdout_reused"] is not False:
        raise ReportError("RT-054 final holdout must not be reused")
    if holdout["libraries"] != list(LIBRARIES):
        raise ReportError("holdout libraries or order do not match the three-library contract")
    for flag in ("private_artifacts_retained_on_ops", "aggregate_only_export",
                 "single_use_frozen_before_candidate_runs"):
        if holdout[flag] is not True:
            raise ReportError(f"holdout_contract.{flag} must be true")

    environment = report["environment"]
    _require_keys(environment, {
        "hardware_class", "corpus_snapshot_version", "same_corpus",
        "same_hardware_class", "same_timeout_budget", "same_top_k",
        "top_k", "run_order_randomized",
    }, "environment")
    for flag in ("same_corpus", "same_hardware_class", "same_timeout_budget",
                 "same_top_k", "run_order_randomized"):
        if environment[flag] is not True:
            raise ReportError(f"environment.{flag} must be true")
    if environment["top_k"] != 10:
        raise ReportError("environment.top_k must be 10")

    candidates = report["candidates"]
    if not isinstance(candidates, Mapping) or set(candidates) != {CANDIDATE_A, CANDIDATE_B}:
        raise ReportError("report must contain exactly the two frozen candidates")
    for candidate_id, candidate in candidates.items():
        _validate_candidate(candidate_id, candidate)

    if "rerank_ablation" in report:
        ablation = report["rerank_ablation"]
        _require_keys(ablation, {"enabled", "status", "recall_delta", "p95_delta_ms"},
                      "rerank_ablation")
        if ablation["enabled"] is not True or ablation["status"] not in {"PASS", "FAIL"}:
            raise ReportError("rerank_ablation must be an explicit enabled PASS/FAIL experiment")
        _finite_number(ablation["recall_delta"], "rerank_ablation.recall_delta", minimum=-1)
        _finite_number(ablation["p95_delta_ms"], "rerank_ablation.p95_delta_ms", minimum=-1)

    cleanup = report["cleanup"]
    _require_keys(cleanup, {
        "private_holdout_retained_on_ops", "temporary_indices_zero",
        "temporary_services_zero", "temporary_containers_zero", "cleanup_failures",
    }, "cleanup")
    if cleanup["private_holdout_retained_on_ops"] is not True:
        raise ReportError("private holdout must remain on OPS")
    for flag in ("temporary_indices_zero", "temporary_services_zero", "temporary_containers_zero"):
        if cleanup[flag] is not True:
            raise ReportError(f"cleanup.{flag} must be true")
    if cleanup["cleanup_failures"] != 0:
        raise ReportError("cleanup_failures must be zero")

    invariants = report["production_invariants"]
    _require_keys(invariants, {
        "nas_unchanged", "gateway_unchanged", "existing_indices_unchanged",
        "production_config_unchanged",
    }, "production_invariants")
    if not all(value is True for value in invariants.values()):
        raise ReportError("all production invariants must be true")


def _validate_candidate(candidate_id: str, candidate: Mapping[str, Any]) -> None:
    _require_keys(candidate, {"identity", "libraries", "operations", "gateway_readiness"},
                  f"candidates.{candidate_id}")
    identity = candidate["identity"]
    if candidate_id == CANDIDATE_A:
        _require_keys(identity, {
            "kind", "exact_resolver", "lexical_analyzer", "doc_collapse",
            "parent_expand", "rerank_in_primary",
        }, f"candidates.{candidate_id}.identity")
        expected = {
            "kind": "opensearch_dual_channel", "exact_resolver": "deterministic_v1",
            "lexical_analyzer": "analysis_icu", "doc_collapse": True,
            "parent_expand": True, "rerank_in_primary": False,
        }
        if identity != expected:
            raise ReportError("candidate A identity drifted from the frozen mechanism")
    else:
        _require_keys(identity, {"kind", "commit", "native_pipeline", "core_modified"},
                      f"candidates.{candidate_id}.identity")
        if identity != {"kind": "weknora", "commit": WEKNORA_COMMIT,
                         "native_pipeline": True, "core_modified": False}:
            raise ReportError("candidate B must be unmodified native WeKnora at the frozen commit")

    libraries = candidate["libraries"]
    if not isinstance(libraries, Mapping) or set(libraries) != set(LIBRARIES):
        raise ReportError(f"candidate {candidate_id} must report exactly three libraries")
    for kb in LIBRARIES:
        metrics = libraries[kb]
        _require_keys(metrics, METRICS, f"candidates.{candidate_id}.libraries.{kb}")
        for metric in METRICS:
            number = _finite_number(metrics[metric], f"{candidate_id}.{kb}.{metric}")
            if metric in {"recall_at_10", "exact", "no_answer"} and number > 1:
                raise ReportError(f"{candidate_id}.{kb}.{metric} must be <= 1")
        if int(metrics["leak_count"]) != metrics["leak_count"]:
            raise ReportError(f"{candidate_id}.{kb}.leak_count must be an integer")

    operations = candidate["operations"]
    _require_keys(operations, {
        "complexity_score", "components_count", "upgrade_steps_count",
        "backup_restore_steps_count",
    }, f"candidates.{candidate_id}.operations")
    for name, value in operations.items():
        number = _finite_number(value, f"{candidate_id}.operations.{name}")
        if int(number) != number:
            raise ReportError(f"{candidate_id}.operations.{name} must be an integer")
    gateway = candidate["gateway_readiness"]
    _require_keys(gateway, {
        "https_query_api", "per_gateway_identity", "kb_grants_server_side",
        "no_direct_nas_or_search_credentials",
    }, f"candidates.{candidate_id}.gateway_readiness")
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
    """B displaces a passing A only with a broad, explicit operating win."""
    if b["operations"]["complexity_score"] > a["operations"]["complexity_score"]:
        return False
    wins = 0
    for metric in ("p95_ms", "index_bytes", "build_seconds", "peak_rss_bytes"):
        a_total = sum(a["libraries"][kb][metric] for kb in LIBRARIES)
        b_total = sum(b["libraries"][kb][metric] for kb in LIBRARIES)
        if a_total > 0 and b_total <= a_total * .80:
            wins += 1
    return wins >= 3


def decide(report: Mapping[str, Any]) -> dict[str, Any]:
    validate_report(report)
    a = report["candidates"][CANDIDATE_A]
    b = report["candidates"][CANDIDATE_B]
    a_ok, a_failures = candidate_gate(a)
    b_ok, b_failures = candidate_gate(b)
    if a_ok and (not b_ok or not _materially_better_b(a, b)):
        selected, reason = CANDIDATE_A, "A_passes_and_B_has_no_material_three-metric_operating_win"
    elif b_ok:
        selected = CANDIDATE_B
        reason = "B_is_only_eligible_or_has_material_three-metric_operating_win"
    else:
        selected, reason = None, "neither_candidate_passes_hard_gates"
    return {
        "schema": "cwk.rt055.retrieval-decision.result.v1",
        "run_id": report["run_id"],
        "selected": selected,
        "status": "PASS" if selected else "NO-GO",
        "reason": reason,
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
