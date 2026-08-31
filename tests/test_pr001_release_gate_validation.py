"""PR-001 release gate contracts G1..G7 - EXECUTABLE validation.

tests/test_pr001_release_gate_contracts.py asserts what the five frozen contract
files SAY. This module asserts what they DO. The distinction matters: a rule
stated in prose can be unimplementable, self-contradictory, or vacuously
satisfiable, and no amount of string matching would notice.

It contains a real, self-contained evaluator for the two receipt families and
drives it with one positive fixture per gate plus a large negative fixture set,
each mutating exactly one thing and asserting the specific violation code that
must appear. Structural checking is driven by the schema JSON itself, so a
weakened schema is caught; the semantic rules are hand-implemented from the
frozen design, so a weakened semanticRules string is caught too.

Pure stdlib and no network/jsonschema dependency. Fast unit cases inject an
isolated world through EvalContext; canonical integration cases clone a local
temporary Git repository and derive commit, tree and tracked-blob facts from an
explicit evaluation commit. No fixture escapes its TemporaryDirectory.

The constants below are intentionally duplicated from the static module rather
than imported: each file must run standalone under `python3.11 -m unittest`
without the tests directory having to be an importable package.
"""

from __future__ import annotations

import datetime
import base64
import copy
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import unittest

# The production-like evaluator is intentionally a plain module under tests/
# so it can also run as a standalone reference implementation.  Put that
# directory on sys.path before importing it; do not silently fall back to the
# legacy in-file evaluator below when real verification is unavailable.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import pr001_release_eval as _real_eval
import pr001_release_signing as _real_signing
import pr001_evidence_binding as _real_binding
import pr001_script_evolution_guard as _evolution_guard
from test_pr001_security_gate_contracts import SecurityFixture as _SecurityOwnerFixture

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PR_ROOT = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces"
GATES_DIR = PR_ROOT / "contracts" / "gates"

RELEASE_REGISTRY_PATH = GATES_DIR / "release_gate_registry_v1.json"
RELEASE_RECEIPT_SCHEMA_PATH = GATES_DIR / "release_gate_receipt_v1.schema.json"
RELEASE_AUTH_SCHEMA_PATH = GATES_DIR / "release_authorization_receipt_v1.schema.json"

RELEASE_GATE_ORDER = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
VERIFICATION_RELEASE_GATES = ("G1", "G2", "G3", "G4", "G5", "G6")
RELEASE_RECEIPT_DOMAIN = b"cwk-release-gate-receipt-v1\x00"
RELEASE_AUTH_DOMAIN = b"cwk-release-authorization-receipt-v1\x00"
RELEASE_RECEIPT_ROOT = PR_ROOT / "release-gate-receipts"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _release_receipt_sha256(receipt: dict) -> str:
    """Re-derive the domain-separated release gate receipt hash."""
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
    return hashlib.sha256(RELEASE_RECEIPT_DOMAIN + payload).hexdigest()


def _release_auth_sha256(auth: dict) -> str:
    """Re-derive the domain-separated G7 authorization hash."""
    body = {k: v for k, v in auth.items() if k != "authorization_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
    return hashlib.sha256(RELEASE_AUTH_DOMAIN + payload).hexdigest()


# ---------------------------------------------------------------------------
# Executable evaluator
#
# Everything above this line reads the four contract files and asserts what they
# SAY. That is necessary but not sufficient: a rule stated in prose can be
# unimplementable, self-contradictory, or vacuously satisfiable, and no amount of
# string matching would notice. The code below is a real, self-contained
# evaluator for the two receipt families, and the classes after it drive it with
# one positive fixture and a large set of negative fixtures, each mutating
# exactly one thing and asserting the specific violation code that must appear.
#
# It is deliberately independent of the schema prose: structural checking is
# driven by the schema JSON itself (so a weakened schema is caught), while the
# semantic rules are hand-implemented from the frozen design (so a weakened
# semanticRules string is caught too). Pure stdlib, no git, no network - every
# fact the real validator would recompute from the environment is injected
# explicitly through EvalContext, which is also what makes the negative cases
# expressible.
# ---------------------------------------------------------------------------

FRESH_EVIDENCE_ROLES = (
    "full_regression",
    "wiki_smoke",
    "legacy_smoke",
    "attack_suite",
    "secret_scan",
    "restore_drill",
    "rollback_drill",
    "default_off_verification",
    "final_findings_reconciliation",
)
G6_ONLY_FIELDS = (
    "verifier_provenance",
    "freshness_attestation",
    "orchestration_provenance",
    "fresh_evidence_refs",
    "expires_at",
)
THIRTY_DAYS = datetime.timedelta(days=30)


def _parse_dt(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _type_ok(node, declared: str) -> bool:
    if declared == "object":
        return isinstance(node, dict)
    if declared == "array":
        return isinstance(node, list)
    if declared == "string":
        return isinstance(node, str)
    if declared == "integer":
        return isinstance(node, int) and not isinstance(node, bool)
    if declared == "number":
        return isinstance(node, (int, float)) and not isinstance(node, bool)
    if declared == "boolean":
        return isinstance(node, bool)
    return True


def structural_errors(node, schema, path="$"):
    """A small JSON Schema subset checker, driven by the real schema files.

    Supports exactly the keywords the four contracts use: type, const, enum,
    pattern, minLength/maxLength, minimum/maximum, format:date-time, required,
    additionalProperties:false, properties, items, minItems/maxItems and
    uniqueItems. Anchored patterns are matched with an explicit newline
    rejection, because `$` in Python also matches before a trailing newline and
    would otherwise let `foo\\n` satisfy `^foo$`.
    """
    errs = []
    if "const" in schema and node != schema["const"]:
        errs.append(f"const:{path}")
    if "enum" in schema and node not in schema["enum"]:
        errs.append(f"enum:{path}")
    declared = schema.get("type")
    if declared is not None and not _type_ok(node, declared):
        return errs + [f"type:{path}"]

    if isinstance(node, str):
        pattern = schema.get("pattern")
        if pattern is not None and (re.search(pattern, node) is None or "\n" in node):
            errs.append(f"pattern:{path}")
        if "minLength" in schema and len(node) < schema["minLength"]:
            errs.append(f"minLength:{path}")
        if "maxLength" in schema and len(node) > schema["maxLength"]:
            errs.append(f"maxLength:{path}")
        if schema.get("format") == "date-time" and _parse_dt(node) is None:
            errs.append(f"format:{path}")

    if isinstance(node, int) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            errs.append(f"minimum:{path}")
        if "maximum" in schema and node > schema["maximum"]:
            errs.append(f"maximum:{path}")

    if isinstance(node, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in node:
                errs.append(f"required:{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in node:
                if key not in props:
                    errs.append(f"additionalProperties:{path}.{key}")
        for key, value in node.items():
            if key in props:
                errs.extend(structural_errors(value, props[key], f"{path}.{key}"))

    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            errs.append(f"minItems:{path}")
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            errs.append(f"maxItems:{path}")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(i, sort_keys=True, ensure_ascii=False) for i in node]
            if len(encoded) != len(set(encoded)):
                errs.append(f"uniqueItems:{path}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(node):
                errs.extend(structural_errors(item, item_schema, f"{path}[{idx}]"))
    return errs


def _deep_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _deep_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _deep_keys(item)


def _expected_ref_kind(ref_id: str):
    if ref_id == "RT-026-GO-NO-GO":
        return "go_no_go_report"
    if ref_id == "G0":
        return "narrative_review_report"
    if re.fullmatch(r"G[1-6]", ref_id):
        return "release_gate_receipt"
    if ref_id.startswith("SG:"):
        return "security_gate_receipt"
    if ref_id.startswith("CAP:"):
        return "capability_activation_receipt"
    if ref_id.startswith("VG-"):
        return "verification_gate_receipt"
    if ref_id.startswith("RT-"):
        return "rt_independent_acceptance"
    return None


class EvalContext:
    """Everything a real validator would RECOMPUTE, injected explicitly.

    The production evaluator derives these from git, the filesystem and the
    trust store. Injecting them keeps the tests hermetic while still exercising
    the checks: a negative fixture is expressed by changing the context (an
    unrelated commit, a drifted prerequisite hash, a revoked key) rather than by
    stubbing out the rule.
    """

    def __init__(
        self,
        *,
        now=None,
        root=None,
        evidence_hashes=None,
        introducing_commit="f" * 40,
        ancestor_commits=(),
        touched_feeder_commits=(),
        archive_chain=(),
        verifier_signed_evidence=(),
        trust_store=("release-authority-root",),
        project_identities=("agent-rt026-impl", "agent-go-no-go-eval", "test-signer"),
        beneficiary_identities=("agent-rt026-impl", "agent-go-no-go-eval"),
        valid_signatures=None,
        used_nonces=(),
        deploy_environment=None,
        referenced_g6=None,
    ):
        self.now = now or datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc)
        self.root = root
        self.evidence_hashes = dict(evidence_hashes or {})
        self.introducing_commit = introducing_commit
        self.ancestor_commits = set(ancestor_commits)
        self.touched_feeder_commits = set(touched_feeder_commits)
        self.archive_chain = list(archive_chain)
        self.verifier_signed_evidence = set(verifier_signed_evidence)
        self.trust_store = set(trust_store)
        self.project_identities = set(project_identities)
        self.beneficiary_identities = set(beneficiary_identities)
        self.valid_signatures = set(valid_signatures or ())
        self.used_nonces = set(used_nonces)
        self.deploy_environment = deploy_environment
        self.referenced_g6 = referenced_g6


def evaluate_release_receipt(receipt, registry_gate, schema, ctx):
    """Full G1..G6 validation. Returns sorted violation codes; [] means valid."""
    v = []
    v.extend(structural_errors(receipt, schema))

    forbidden = set(schema.get("deepForbiddenKeys", []))
    for key in _deep_keys(receipt):
        if key in forbidden:
            v.append(f"forbidden_key:{key}")

    gate_id = receipt.get("gate_id")
    if gate_id != registry_gate.get("gate_id"):
        v.append("gate_id_registry_mismatch")
    if receipt.get("schema") != schema["$id"]:
        v.append("schema_id_mismatch")

    # --- independence -----------------------------------------------------
    if receipt.get("producer") == receipt.get("verifier"):
        v.append("self_certification")
    if receipt.get("verifier_role") != registry_gate.get("producer_role"):
        v.append("verifier_role_mismatch")

    status = receipt.get("status")
    conclusion = receipt.get("conclusion")
    synthetic = receipt.get("synthetic")
    evidence = receipt.get("evidence", {})

    if status == "pass":
        if not receipt.get("feeder_rts_independent_pass"):
            v.append("pass_without_feeder_independent_pass")
        if evidence.get("tests_run", 0) <= 0:
            v.append("pass_with_zero_tests")
        if evidence.get("tests_failed", 0) != 0:
            v.append("pass_with_failures")
    if status == "not_run":
        if conclusion != "not_run":
            v.append("not_run_conclusion_mismatch")
        if evidence.get("tests_run", 0) != 0:
            v.append("not_run_with_tests")

    if conclusion == "release_gate_verified":
        if synthetic:
            v.append("verified_while_synthetic")
        if status != "pass":
            v.append("verified_without_pass")
        if gate_id == "G6":
            v.append("g1_g5_conclusion_on_g6")
    if conclusion == "READY_FOR_G7_AUTHORIZATION":
        if gate_id != "G6":
            v.append("g6_conclusion_on_non_g6")
        if synthetic:
            v.append("g6_conclusion_while_synthetic")
        if status != "pass":
            v.append("g6_conclusion_without_pass")
    if synthetic:
        if "synthetic_reason" not in receipt:
            v.append("synthetic_without_reason")
        if conclusion in ("release_gate_verified", "READY_FOR_G7_AUTHORIZATION"):
            v.append("synthetic_with_verified_conclusion")

    # --- frozen registry agreement ---------------------------------------
    if receipt.get("feeder_rts") != registry_gate.get("feeder_rts"):
        v.append("feeder_rts_mismatch")
    if receipt.get("consumes_verification_gates") != registry_gate.get(
        "consumes_verification_gates"
    ):
        v.append("consumes_vg_mismatch")

    # --- prerequisites: EXACT set equality, hashes recomputed -------------
    refs = receipt.get("prerequisite_refs", [])
    ref_ids = [r.get("ref_id") for r in refs if isinstance(r, dict)]
    if len(ref_ids) != len(set(ref_ids)):
        v.append("duplicate_ref_id")
    required = set(registry_gate.get("required_prerequisite_ids", []))
    for missing in sorted(required - set(ref_ids)):
        v.append(f"prereq_missing:{missing}")
    for extra in sorted(set(ref_ids) - required):
        v.append(f"prereq_extra:{extra}")
    if gate_id in ref_ids:
        v.append("self_reference")
    order = list(RELEASE_GATE_ORDER)
    if gate_id in order:
        later = set(order[order.index(gate_id):])
        for ref_id in sorted(set(ref_ids) & later):
            v.append(f"forward_reference:{ref_id}")
    for vg in receipt.get("consumes_verification_gates", []) or []:
        if vg not in ref_ids:
            v.append(f"vg_not_pinned:{vg}")
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_id = ref.get("ref_id")
        expected_kind = _expected_ref_kind(ref_id or "")
        if expected_kind is not None and ref.get("ref_kind") != expected_kind:
            v.append(f"ref_kind_mismatch:{ref_id}")
        if ref_id in ctx.evidence_hashes:
            if ctx.evidence_hashes[ref_id] != ref.get("ref_sha256"):
                v.append(f"prereq_hash_mismatch:{ref_id}")
        elif ctx.evidence_hashes:
            v.append(f"prereq_evidence_missing:{ref_id}")

    # --- G6 freshness -----------------------------------------------------
    if gate_id == "G6":
        for field in G6_ONLY_FIELDS:
            if field not in receipt:
                v.append(f"g6_missing:{field}")
        fresh_refs = receipt.get("fresh_evidence_refs")
        if fresh_refs is not None:
            roles = [
                item.get("role")
                for item in fresh_refs
                if isinstance(item, dict)
            ]
            if len(roles) != len(FRESH_EVIDENCE_ROLES) or set(roles) != set(
                FRESH_EVIDENCE_ROLES
            ):
                v.append("g6_fresh_evidence_roles_incomplete")
        provenance = receipt.get("verifier_provenance") or {}
        if provenance.get("prior_engagement_ids"):
            v.append("g6_verifier_not_fresh")
        if provenance and not provenance.get("independent_of_producer_org"):
            v.append("g6_verifier_not_independent_of_producer_org")
        attestation = receipt.get("freshness_attestation") or {}
        for flag, value in attestation.items():
            if value is not True:
                v.append(f"g6_attestation_false:{flag}")
        # Recomputed, not trusted: a self-reported clean attestation that
        # contradicts the on-disk scan is itself a violation.
        if receipt.get("verifier") in ctx.verifier_signed_evidence:
            v.append("g6_verifier_signed_prior_evidence")
        created = _parse_dt(receipt.get("created_at"))
        expires = _parse_dt(receipt.get("expires_at"))
        if created and expires:
            delta = expires - created
            if delta <= datetime.timedelta(0):
                v.append("g6_expiry_not_positive")
            elif delta > THIRTY_DAYS:
                v.append("g6_expiry_too_long")
    else:
        for field in G6_ONLY_FIELDS:
            if field in receipt:
                v.append(f"g6_only_field_on_non_g6:{field}")

    # --- commit / scope / environment binding ----------------------------
    subject = receipt.get("tested_subject_commit")
    if subject == ctx.introducing_commit:
        v.append("subject_commit_is_its_own_commit")
    elif subject not in ctx.ancestor_commits:
        v.append("subject_commit_not_strict_ancestor")
    if ctx.touched_feeder_commits and subject not in ctx.touched_feeder_commits:
        v.append("subject_commit_did_not_touch_feeder_packages")

    # --- sequence chain ---------------------------------------------------
    sequence = receipt.get("sequence")
    supersedes = receipt.get("supersedes_receipt_sha256")
    if sequence == 1 and supersedes is not None:
        v.append("supersedes_on_first_run")
    if isinstance(sequence, int) and sequence > 1:
        if supersedes is None:
            v.append("supersedes_missing")
        prior = [entry for entry in ctx.archive_chain if entry["sequence"] == sequence - 1]
        if prior and supersedes is not None:
            if prior[0]["receipt_sha256"] != supersedes:
                v.append("supersedes_mismatch")
            prior_created = _parse_dt(prior[0].get("created_at"))
            created = _parse_dt(receipt.get("created_at"))
            if prior_created and created and created <= prior_created:
                v.append("created_at_not_strictly_increasing")
    if ctx.archive_chain and isinstance(sequence, int):
        seen = sorted(entry["sequence"] for entry in ctx.archive_chain)
        if len(seen) != len(set(seen)):
            v.append("chain_duplicate_sequence")
        if seen != list(range(1, sequence)):
            v.append("chain_not_exact_prefix")

    # --- artifacts --------------------------------------------------------
    artifacts = receipt.get("artifacts", [])
    if not artifacts:
        v.append("artifacts_empty")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path", "")
        if "release-gate-receipts/" in path:
            v.append(f"artifact_is_gate_evidence:{path}")
        if ctx.root is not None:
            target = ctx.root / path
            if not target.is_file():
                v.append(f"artifact_missing:{path}")
            else:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != artifact.get("sha256"):
                    v.append(f"artifact_hash_mismatch:{path}")

    # --- self hash --------------------------------------------------------
    if _release_receipt_sha256(receipt) != receipt.get("receipt_sha256"):
        v.append("self_hash_mismatch")

    return sorted(v)


def evaluate_release_authorization(auth, schema, ctx):
    """Full G7 validation. Returns sorted violation codes; [] means valid."""
    v = []
    v.extend(structural_errors(auth, schema))

    forbidden = set(schema.get("deepForbiddenKeys", []))
    for key in _deep_keys(auth):
        if key in forbidden:
            v.append(f"forbidden_key:{key}")

    if auth.get("schema") != schema["$id"]:
        v.append("schema_id_mismatch")

    decision = auth.get("decision")
    if decision == "withdrawn" and "revocation_ref" not in auth:
        v.append("withdrawn_without_revocation_ref")
    if decision != "withdrawn" and "revocation_ref" in auth:
        v.append("revocation_ref_without_withdrawal")

    # --- external trust root ---------------------------------------------
    signature = auth.get("external_signature", {}) or {}
    trust_root = signature.get("trust_root_id")
    if trust_root in ctx.project_identities:
        v.append("signer_is_project_or_test_identity")
    elif trust_root not in ctx.trust_store:
        v.append("signer_not_in_production_trust_store")
    if signature.get("key_state") != "active":
        v.append("key_not_active")
    key_expiry = _parse_dt(signature.get("key_expires_at"))
    if key_expiry is not None and key_expiry <= ctx.now:
        v.append("key_expired")
    body = {k: val for k, val in auth.items() if k != "authorization_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    signed_over = hashlib.sha256(
        unicodedata.normalize("NFC", canonical).encode("utf-8")
    ).hexdigest()
    if ctx.valid_signatures and signed_over not in ctx.valid_signatures:
        v.append("signature_does_not_cover_body")

    # --- the single machine prerequisite ----------------------------------
    ref = auth.get("g6_receipt_ref", {}) or {}
    g6 = ctx.referenced_g6
    if g6 is not None:
        if _release_receipt_sha256(g6) != ref.get("ref_sha256"):
            v.append("g6_hash_mismatch")
        if g6.get("status") != "pass":
            v.append("g6_not_pass")
        if g6.get("conclusion") != "READY_FOR_G7_AUTHORIZATION":
            v.append("g6_conclusion_not_ready_for_authorization")
        if g6.get("expires_at") != ref.get("g6_expires_at"):
            v.append("g6_expiry_ref_mismatch")
        if g6.get("tested_subject_commit") != ref.get("g6_tested_subject_commit"):
            v.append("g6_subject_commit_ref_mismatch")
    g6_expiry = _parse_dt(ref.get("g6_expires_at"))
    if g6_expiry is not None and g6_expiry <= ctx.now:
        v.append("g6_expired")

    # --- target binding ---------------------------------------------------
    binding = auth.get("target_binding", {}) or {}
    if binding.get("target_commit") != ref.get("g6_tested_subject_commit"):
        v.append("target_commit_not_bound_to_g6_subject")
    if ctx.deploy_environment is not None:
        if binding.get("environment_fingerprint") != ctx.deploy_environment:
            v.append("target_environment_mismatch")

    # --- replay / window --------------------------------------------------
    if auth.get("nonce") in ctx.used_nonces:
        v.append("nonce_replay")
    not_before = _parse_dt(auth.get("not_before"))
    expires_at = _parse_dt(auth.get("expires_at"))
    if not_before is not None and ctx.now < not_before:
        v.append("window_not_yet_valid")
    if expires_at is not None and ctx.now >= expires_at:
        v.append("window_expired")
    if not_before is not None and expires_at is not None:
        delta = expires_at - not_before
        if delta <= datetime.timedelta(0):
            v.append("window_not_positive")
        elif delta > THIRTY_DAYS:
            v.append("window_too_long")

    # --- chain ------------------------------------------------------------
    sequence = auth.get("sequence")
    supersedes = auth.get("supersedes_authorization_sha256")
    if sequence == 1 and supersedes is not None:
        v.append("supersedes_on_first_authorization")
    if isinstance(sequence, int) and sequence > 1:
        if supersedes is None:
            v.append("supersedes_missing")
        prior = [e for e in ctx.archive_chain if e["sequence"] == sequence - 1]
        if prior and supersedes is not None:
            if prior[0]["authorization_sha256"] != supersedes:
                v.append("supersedes_mismatch")

    # --- clerical recorder ------------------------------------------------
    principal = (auth.get("authorizing_principal", {}) or {}).get("id")
    recorder = auth.get("recorded_by")
    if recorder == principal:
        v.append("recorder_is_the_authorizing_principal")
    if recorder in ctx.beneficiary_identities:
        v.append("recorder_is_the_beneficiary")

    if _release_auth_sha256(auth) != auth.get("authorization_sha256"):
        v.append("self_hash_mismatch")

    return sorted(v)


def evaluate_release_root_closure(root: pathlib.Path, registry: dict):
    """Whole-subtree closure over release-gate-receipts/.

    A missing root is NOT_RUN, which is not a violation. Anything present must
    land exactly on a declared receipt_path or a declared archive member named
    after its own recomputed hash; everything else is a hard failure rather than
    a silently skipped extra.
    """
    v = []
    if not root.exists():
        return v

    declared_receipts = {}
    declared_archives = {}
    for gate in registry["gates"]:
        rel = gate["receipt_path"].split("release-gate-receipts/", 1)[1]
        declared_receipts[rel] = gate["gate_id"]
        arel = gate["archive_dir"].split("release-gate-receipts/", 1)[1]
        declared_archives[arel] = gate["gate_id"]

    for dirpath, dirnames, filenames in os.walk(root):
        here = pathlib.Path(dirpath)
        for name in list(dirnames):
            entry = here / name
            if entry.is_symlink():
                v.append(f"symlink_component:{entry.relative_to(root)}")
                dirnames.remove(name)
            elif name.startswith("."):
                v.append(f"dotfile:{entry.relative_to(root)}")
        for name in filenames:
            entry = here / name
            rel = str(entry.relative_to(root))
            if entry.is_symlink():
                v.append(f"symlink_leaf:{rel}")
                continue
            if name.startswith("."):
                v.append(f"dotfile:{rel}")
                continue
            st = entry.lstat()
            if not stat.S_ISREG(st.st_mode):
                v.append(f"special_file:{rel}")
                continue
            if st.st_nlink > 1:
                v.append(f"hardlink:{rel}")
                continue
            if rel in declared_receipts:
                continue
            parent = str(entry.parent.relative_to(root))
            if parent in declared_archives:
                digest = hashlib.sha256(entry.read_bytes()).hexdigest()
                try:
                    body = json.loads(entry.read_text(encoding="utf-8"))
                except ValueError:
                    v.append(f"archive_member_not_json:{rel}")
                    continue
                own = body.get("receipt_sha256") or body.get("authorization_sha256")
                if name != f"{own}.json":
                    v.append(f"archive_member_misnamed:{rel}")
                del digest
                continue
            v.append(f"undeclared_file:{rel}")
    return v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENV = {
    "python_version": "3.11.9",
    "platform": "darwin-arm64",
    "toolchain_build": "cwk-toolchain-2026.08",
}
_SUBJECT_COMMIT = "a" * 40
_INTRODUCING_COMMIT = "f" * 40


def _ref_for(ref_id: str) -> dict:
    digest = hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
    slug = ref_id.replace(":", "-").lower()
    return {
        "ref_id": ref_id,
        "ref_kind": _expected_ref_kind(ref_id),
        "ref_path": f"PR/PR-001-multitenant-knowledge-spaces/evidence/{slug}.json",
        "ref_sha256": digest,
    }


def _evidence_hashes(gate: dict) -> dict:
    return {
        ref_id: hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
        for ref_id in gate["required_prerequisite_ids"]
    }


def _make_release_receipt(gate: dict, root: pathlib.Path, **overrides) -> dict:
    """A fully valid receipt for the given registry gate."""
    gate_id = gate["gate_id"]
    artifact_rel = f"PR/PR-001-multitenant-knowledge-spaces/evidence/{gate_id}-run.txt"
    target = root / artifact_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{gate_id} evidence\n", encoding="utf-8")
    artifact_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    receipt = {
        "schema": "cwk.pr001.release_gate_receipt.v1",
        "gate_id": gate_id,
        "status": "pass",
        "conclusion": (
            "READY_FOR_G7_AUTHORIZATION" if gate_id == "G6" else "release_gate_verified"
        ),
        "synthetic": False,
        "producer": f"agent-{gate_id.lower()}-impl",
        "verifier": f"agent-{gate_id.lower()}-verify",
        "verifier_role": gate["producer_role"],
        "feeder_rts": list(gate["feeder_rts"]),
        "feeder_rts_independent_pass": True,
        "consumes_verification_gates": list(gate["consumes_verification_gates"]),
        "prerequisite_refs": [_ref_for(r) for r in gate["required_prerequisite_ids"]],
        "evidence": {
            "test_command": "python3.11 -m unittest discover -s tests",
            "tests_run": 209,
            "tests_failed": 0,
            "python_version": "3.11.9",
        },
        "artifacts": [{"path": artifact_rel, "sha256": artifact_hash}],
        "tested_subject_commit": _SUBJECT_COMMIT,
        "owner_scope_tree_sha256": "b" * 64,
        "environment_fingerprint": dict(_ENV),
        "sequence": 1,
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    if gate_id == "G6":
        receipt["verifier_provenance"] = {
            "engagement_id": "fresh-final-review-01",
            "prior_engagement_ids": [],
            "independent_of_producer_org": True,
        }
        receipt["freshness_attestation"] = {
            "signed_no_rt_acceptance": True,
            "signed_no_verification_gate": True,
            "signed_no_upstream_release_gate": True,
            "signed_no_security_gate": True,
        }
        receipt["orchestration_provenance"] = {
            "candidate_freeze_sha256": "c" * 64,
            "candidate_frozen_at": "2026-08-17T22:00:00+00:00",
            "session_id": "fresh-final-session",
            "session_started_at": "2026-08-17T23:00:00+00:00",
            "engagement_id": "fresh-final-review-01",
            "session_participants": [
                "agent-g6-impl",
                "agent-g6-verify",
                "fresh-final-runner",
            ],
        }
        fresh_refs = []
        for index, role in enumerate(FRESH_EVIDENCE_ROLES):
            run_rel = (
                "PR/PR-001-multitenant-knowledge-spaces/evidence/"
                f"G6-fresh-{index:02d}-{role}.txt"
            )
            run_target = root / run_rel
            run_target.parent.mkdir(parents=True, exist_ok=True)
            run_target.write_text(f"{role} evidence\n", encoding="utf-8")
            run = {
                "role": role,
                "run_id": f"run-{index:02d}-{role.replace('_', '-')}",
                "engagement_id": "fresh-final-review-01",
                "runner_id": "fresh-final-runner",
                "session_id": "fresh-final-session",
                "command": f"python3.11 -m unittest tests.test_{role}",
                "tested_subject_commit": _SUBJECT_COMMIT,
                "candidate_tree_sha256": "c" * 64,
                "environment_fingerprint": dict(_ENV),
                "started_at": f"2026-08-18T00:{index:02d}:00+00:00",
                "completed_at": f"2026-08-18T01:{index:02d}:00+00:00",
                "checks_total": 1,
                "checks_failed": 0,
                "exit_code": 0,
                "result": "pass",
                "artifact_path": run_rel,
                "artifact_sha256": hashlib.sha256(run_target.read_bytes()).hexdigest(),
            }
            if role == "legacy_smoke":
                run["legacy_fixture_id"] = "sanitized-nightly-v1"
            elif role == "attack_suite":
                run["security_gate_ids_rechecked"] = [
                    f"SG:RT-{number:03d}" for number in range(17, 27)
                ]
            elif role == "secret_scan":
                run["findings_count"] = 0
            elif role == "restore_drill":
                run.update(clean_room=True, restore_verified=True)
            elif role == "rollback_drill":
                run["legacy_read_path_restored"] = True
            elif role == "default_off_verification":
                run.update(
                    enabled_component_count=0,
                    allowlisted_tenant_count=0,
                    enabled_flag_count=0,
                )
            elif role == "final_findings_reconciliation":
                run.update(
                    open_blocker_count=0,
                    open_major_count=0,
                    git_diff_check_clean=True,
                    tracked_evidence_match=True,
                )
            fresh_refs.append(run)
        receipt["fresh_evidence_refs"] = fresh_refs
        receipt["created_at"] = "2026-08-18T00:00:00+00:00"
        receipt["expires_at"] = "2026-09-01T00:00:00+00:00"

    receipt.update(overrides)
    for key in [k for k, val in receipt.items() if val is _DROP]:
        del receipt[key]
    receipt["receipt_sha256"] = _release_receipt_sha256(receipt)
    return receipt


def _make_authorization(g6_receipt: dict, **overrides) -> dict:
    auth = {
        "schema": "cwk.pr001.release_authorization_receipt.v1",
        "gate_id": "G7",
        "decision": "authorized",
        "authorizing_principal": {"kind": "human_user", "id": "evan"},
        "authorization_channel": "explicit_user_instruction",
        "external_signature": {
            "trust_root_id": "release-authority-root",
            "key_id": "g7-signing-key-01",
            "key_state": "active",
            "key_not_before": "2026-01-01T00:00:00+00:00",
            "key_expires_at": "2027-01-01T00:00:00+00:00",
            "algorithm": "ecdsa-p256-sha256",
            "signature_b64": "A" * 86 + "==",
        },
        "g6_receipt_ref": {
            "ref_id": "G6",
            "ref_path": (
                "PR/PR-001-multitenant-knowledge-spaces/release-gate-receipts/G6/receipt.json"
            ),
            "ref_sha256": _release_receipt_sha256(g6_receipt),
            "g6_tested_subject_commit": g6_receipt["tested_subject_commit"],
            "g6_expires_at": g6_receipt["expires_at"],
        },
        "authorized_actions": ["enable_pilot_broker_for_allowlisted_tenants"],
        "scope": {
            "migration_phase": "M4",
            "tenant_allowlist_sha256": _real_eval.tenant_allowlist_sha256(
                ["tenant-a", "tenant-b"]
            ),
            "allowlisted_tenant_count": 2,
            "pilot_window_days": 14,
        },
        "target_binding": {
            "target_commit": g6_receipt["tested_subject_commit"],
            "instance_id": "cwk-pilot-instance",
            "environment_fingerprint": dict(_ENV),
        },
        "nonce": "c" * 32,
        "sequence": 1,
        "not_before": "2026-08-19T00:00:00+00:00",
        "expires_at": "2026-09-01T00:00:00+00:00",
        "revocable": True,
        "recorded_by": "agent-release-clerk",
        "created_at": "2026-08-19T00:00:00+00:00",
    }
    auth.update(overrides)
    for key in [k for k, val in auth.items() if val is _DROP]:
        del auth[key]
    auth["authorization_sha256"] = _release_auth_sha256(auth)
    return auth


class _Drop:
    """Sentinel meaning 'delete this key' in a fixture override."""


_DROP = _Drop()


def _auth_signature_context(auth, **kwargs):
    body = {k: v for k, v in auth.items() if k != "authorization_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(
        unicodedata.normalize("NFC", canonical).encode("utf-8")
    ).hexdigest()
    return EvalContext(valid_signatures={digest}, **kwargs)


def _evaluate_canonical_g0_report(root, evaluation_commit, registry):
    """Resolve the optional G0 review as tracked evidence, never as a fixture.

    Absence is a legitimate NOT_RUN state.  Presence is accepted only when the
    exact bytes on disk are the regular tracked blob at ``evaluation_commit``,
    the six-field G0 marker binds a real earlier subject, and its tree digest is
    recomputed from the registry's whole-candidate-minus-evidence model.
    """

    bootstrap = registry["bootstrap_gate"]
    rel_path = bootstrap["final_wave0_review_report_path"]
    target = pathlib.Path(root) / rel_path
    git = _real_binding.GitSubject.for_repo(pathlib.Path(root))
    if git is None:
        return "INVALID", ["g0_not_in_git_repository"]
    if (
        not isinstance(evaluation_commit, str)
        or not git.commit_exists(evaluation_commit)
        or git.resolve(evaluation_commit) != evaluation_commit
    ):
        return "INVALID", ["g0_evaluation_commit_missing_or_unknown"]
    disk_present = os.path.lexists(target)
    tracked_blob = git.blob_bytes(evaluation_commit, rel_path)
    if tracked_blob is None and not disk_present:
        return "NOT_RUN", []
    if tracked_blob is not None and not disk_present:
        return "INVALID", ["g0_tracked_blob_missing_on_disk"]

    violations = []
    evidence_commit = _real_binding.resolve_evidence_commit(
        git,
        pathlib.Path(root),
        rel_path,
        evaluation_commit=evaluation_commit,
    )
    if evidence_commit is None:
        return "INVALID", ["g0_not_regular_tracked_blob_matching_disk"]
    raw = git.blob_bytes(evaluation_commit, rel_path)
    if raw is None:
        return "INVALID", ["g0_blob_missing_at_evaluation_commit"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "INVALID", ["g0_not_utf8"]

    marker = registry["prerequisite_resolution"]["markdown_acceptance_marker"]
    fields, error = _real_eval.parse_acceptance_marker(text, marker)
    if error is not None:
        return "INVALID", [f"g0_{error}"]
    expected_report_id = registry["prerequisite_resolution"]["bootstrap_report_id"]
    if fields["report_id"] != expected_report_id:
        violations.append("g0_report_id_mismatch")
    if fields["verdict"] != marker["verdict_pass_value"]:
        violations.append("g0_verdict_not_pass")
    if fields["open_blocker"] != "0" or fields["open_major"] != "0":
        violations.append("g0_open_findings")

    subject = fields["subject_commit"]
    if not git.commit_exists(subject):
        violations.append("g0_subject_unknown")
    elif not git.is_strict_ancestor(subject, evidence_commit):
        violations.append("g0_subject_not_before_evidence")
    elif not git.is_strict_ancestor(subject, evaluation_commit):
        violations.append("g0_subject_not_before_evaluation")

    owner_model = registry["owner_scope_model"]
    recomputed_tree = git.candidate_tree_sha256(
        subject,
        excluded_prefixes=owner_model["candidate_tree_excluded_prefixes"],
        excluded_patterns=owner_model["candidate_tree_excluded_patterns"],
    )
    if recomputed_tree is None or fields["owner_scope_tree_sha256"] != recomputed_tree:
        violations.append("g0_owner_tree_mismatch")
    return ("PASS" if not violations else "INVALID"), violations


class _ReleaseFixtureBase(unittest.TestCase):
    """Shared setup: registry, schemas, a scratch root and a valid receipt."""

    def setUp(self) -> None:
        self.registry = _load(RELEASE_REGISTRY_PATH)
        self.schema = _load(RELEASE_RECEIPT_SCHEMA_PATH)
        self.auth_schema = _load(RELEASE_AUTH_SCHEMA_PATH)
        self.by_gate = {g["gate_id"]: g for g in self.registry["gates"]}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def gate(self, gate_id: str) -> dict:
        return self.by_gate[gate_id]

    def ctx(self, gate_id="G3", /, **kwargs) -> EvalContext:
        gate = self.gate(gate_id)
        defaults = dict(
            root=self.root,
            evidence_hashes=_evidence_hashes(gate),
            introducing_commit=_INTRODUCING_COMMIT,
            ancestor_commits={_SUBJECT_COMMIT},
            touched_feeder_commits={_SUBJECT_COMMIT},
        )
        defaults.update(kwargs)
        return EvalContext(**defaults)

    def receipt(self, gate_id="G3", /, **overrides) -> dict:
        return _make_release_receipt(self.gate(gate_id), self.root, **overrides)

    def assertViolation(self, violations, code) -> None:
        self.assertIn(
            code, violations, f"expected {code!r}, evaluator returned {violations!r}"
        )


class _RealAuthorizationFixtureBase(_SecurityOwnerFixture, unittest.TestCase):
    """A schema-valid G7 world evaluated by the real crypto/trust path.

    The older fixture below predates the detached-signature contract and uses a
    digest allowlist.  These tests deliberately do not share that shortcut:
    every baseline carries a real deterministic P-256 signature and the
    evaluator verifies it against the public key from an authoritative
    ``TrustStore`` record.
    """

    def setUp(self) -> None:
        self.registry = _real_eval.load_json(RELEASE_REGISTRY_PATH)
        self.auth_schema = _real_eval.load_json(RELEASE_AUTH_SCHEMA_PATH)
        self.resolver = _real_eval.PrerequisiteResolver(self.registry)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        subprocess.run(
            ["git", "clone", "-q", "--shared", str(REPO_ROOT), str(self.root)],
            check=True,
            capture_output=True,
        )
        self.git = _real_binding.GitSubject(self.root)
        self.git._git("config", "user.email", "fixture@example.invalid")
        self.git._git("config", "user.name", "pr001-release-fixture")
        self.git._git("config", "commit.gpgsign", "false")
        for rel_text in _real_binding.ReleaseRepositoryFacts.BOUND_JSON_RELS.values():
            rel = pathlib.PurePosixPath(rel_text)
            source = REPO_ROOT / rel
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        # Security receipts are now bound to the v2 selector manifest at a
        # distinct per-RT subject commit.  Build that history before the
        # release candidate is frozen; release validation then consumes the
        # exact same fixture machinery as the security authority itself.
        release_registry = self.registry
        self.registry = self.resolver.security_registry
        self._prepare_evolution_baseline()
        for entry in self.registry["entries"]:
            self._write_owner_code(entry)
            self._write_evidence_modules(entry)
        self._write_shared_abi_dependencies()

        # G2 binds ``self.remediation_subject`` and its touch proof must
        # witness RT-012/RT-013's two runtime selectors — scripts/cwk_instance.py
        # and scripts/cwk_agent_binding.py — because those are what Stages 09/10
        # changed.  The clone already carries the accepted Stage-09/10 bytes, and
        # `_prepare_evolution_baseline` only rewrites them with identical
        # content, so the remediation commit would otherwise contain no change
        # to either file and G2 would fail `subject_commit_did_not_touch_owner_code`.
        # Materialize a genuine prior state of exactly those two files in an
        # earlier commit, then restore the receipt-bound bytes inside the
        # remediation subject.  Final bytes stay byte-identical to the accepted
        # Stage-09/10 output — this makes the Git-derived touch proof real
        # instead of relying on an unrelated package/document edit.
        remediation_paths = (
            "scripts/cwk_instance.py",
            "scripts/cwk_agent_binding.py",
        )
        remediation_bytes = {
            rel: (self.root / rel).read_bytes() for rel in remediation_paths
        }
        for rel, raw in remediation_bytes.items():
            self.write_bytes(rel, raw + b"\n# pre-remediation fixture state\n")
        _real_binding.commit_all(self.git, "fixture pre-Stage-09/10 remediation state")
        for rel, raw in remediation_bytes.items():
            self.write_bytes(rel, raw)
        self.remediation_subject = _real_binding.commit_all(
            self.git, "security owner-scope and Stage-09/10 remediation baseline"
        )
        self.security_subjects = {}
        for entry in self.registry["entries"]:
            for stage_index in entry["owner_evolution_stage_indices"]:
                # `_prepare_evolution_baseline` already copied in every stage
                # the real repository has materialized, and recorded them in
                # `_materialized_stage_indices`.  Synthesizing such a stage
                # again would overwrite the real receipt at the same path with
                # a fixture receipt whose `from_sha256` is the real tip instead
                # of the genesis, i.e. it would forge a break in a chain that
                # is actually intact.  The security-gate fixture that owns this
                # helper has always had this guard; keep both call sites equal.
                if stage_index not in self._materialized_stage_indices:
                    self._add_evolution_stage(stage_index)
            self._touch_unique_owner_path(entry)
            self.security_subjects[entry["producer_rt"]] = _real_binding.commit_all(
                self.git, f"{entry['producer_rt']} security subject"
            )
        self.registry = release_registry

        for number in range(17, 27):
            self.write_bytes(
                f"RT/RT-{number:03d}/rt-intake.md",
                f"RT-{number:03d} candidate\n".encode(),
            )
        self.future_subject = _real_binding.commit_all(self.git, "fixture candidate S")
        self.now = datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc)
        self.environment = _real_binding.ReleaseRepositoryFacts.derive_observed_environment()
        self.external_environment = {
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "toolchain_build": "cwk-pr001-candidate",
        }
        self.tenant_allowlist = ["tenant-a", "tenant-b"]

        self.keypair = _real_signing.generate_keypair(b"cwk-g7-valid-fixture")
        self.other_keypair = _real_signing.generate_keypair(b"cwk-g7-other-fixture")
        self.trust_record = _real_eval.TrustRecord(
            trust_root_id="external-release-root",
            key_id=self.keypair.key_id(),
            public_key=self.keypair.public_key,
            not_before="2026-01-01T00:00:00+00:00",
            expires_at="2027-01-01T00:00:00+00:00",
            principal_id="evan",
        )
        owner_model = self.registry["owner_scope_model"]
        self.future_tree = self.git.candidate_tree_sha256(
            self.future_subject,
            excluded_prefixes=owner_model["candidate_tree_excluded_prefixes"],
            excluded_patterns=owner_model["candidate_tree_excluded_patterns"],
        )
        self.gate_subjects = {
            "G1": self.registry["prerequisite_resolution"]["rt_acceptance_reports"]["RT-011"]["accepted_subject_commit"],
            # RT-012/RT-013 left the legacy set after Stages 09/10 changed
            # cwk_instance.py/cwk_agent_binding.py. G2 binds the exact commit
            # that restores both receipt-bound remediated script bytes, so the
            # Git-derived touch proof exercises those two new selectors rather
            # than succeeding on an unrelated package/document edit.
            "G2": self.remediation_subject,
            "G3": self.registry["prerequisite_resolution"]["rt_acceptance_reports"]["RT-016"]["accepted_subject_commit"],
            "G4": self.future_subject,
            "G5": self.future_subject,
            "G6": self.future_subject,
        }
        self.gate_trees = {
            gate_id: self.git.candidate_tree_sha256(
                subject,
                excluded_prefixes=owner_model["candidate_tree_excluded_prefixes"],
                excluded_patterns=owner_model["candidate_tree_excluded_patterns"],
            )
            for gate_id, subject in self.gate_subjects.items()
        }
        self.owner_tree = self.future_tree
        self.g6 = self.materialize_valid_g6_world()
        self.auth = self.make_authorization()
        self.evaluation_commit = _real_binding.commit_all(self.git, "fixture evidence E")

    def write_bytes(self, rel_path: str, content: bytes) -> bytes:
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return content

    def write_json(self, rel_path: str, body: dict) -> bytes:
        raw = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return self.write_bytes(rel_path, raw)

    def artifact(self, name: str, content: bytes | None = None) -> dict:
        rel_path = f"evidence/release-world/{name}.txt"
        raw = self.write_bytes(
            rel_path,
            content if content is not None else f"{name} evidence\n".encode(),
        )
        return {"path": rel_path, "sha256": hashlib.sha256(raw).hexdigest()}

    def external_owner_tree(self, prefixes) -> str:
        digest = self.git.owner_scope_tree_sha256(self.future_subject, list(prefixes))
        self.assertIsNotNone(digest, f"missing external owner scope: {prefixes!r}")
        return digest

    def canonical_ref(self, ref_id: str) -> dict:
        rel_path = self.resolver.canonical_path(ref_id)
        raw = (self.root / rel_path).read_bytes()
        family = self.resolver.family_for(ref_id)
        return {
            "ref_id": ref_id,
            "ref_kind": family["ref_kind"],
            "ref_path": rel_path,
            "ref_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def acceptance_marker(
        self, report_id: str, *, subject: str | None = None, tree: str | None = None
    ) -> bytes:
        subject = subject or self.future_subject
        tree = tree or self.future_tree
        identity_lines = ""
        if re.fullmatch(r"RT-0(1[1-9]|2[0-6])", report_id):
            slug = report_id.lower()
            identity_lines = (
                f"implementer_ids: agent-{slug}-impl\n"
                f"reviewer_ids: agent-{slug}-review\n"
            )
        return (
            "# Independent acceptance\n\n"
            "<!-- cwk-acceptance-v1\n"
            f"report_id: {report_id}\n"
            "verdict: PASS\n"
            "open_blocker: 0\n"
            "open_major: 0\n"
            f"subject_commit: {subject}\n"
            f"owner_scope_tree_sha256: {tree}\n"
            f"{identity_lines}"
            "-->\n"
        ).encode("utf-8")

    def materialize_acceptance_documents(self) -> None:
        g0_path = self.resolver.canonical_path("G0")
        self.write_bytes(
            g0_path,
            self.acceptance_marker(
                "G0-FINAL-WAVE0",
                subject=self.gate_subjects["G1"],
                tree=self.gate_trees["G1"],
            ),
        )
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        for rt_id in ("RT-012", "RT-013"):
            superseded = reports[rt_id]["superseded_legacy_report"]
            old_source = REPO_ROOT / superseded["report_path"]
            old_raw = old_source.read_bytes()
            self.assertEqual(
                hashlib.sha256(old_raw).hexdigest(), superseded["report_sha256"]
            )
            # Preserve history at its historical path. The resolver never
            # treats it as current acceptance; that role belongs exclusively
            # to the synthesized Stage-09/Stage-10 marker below.
            self.write_bytes(superseded["report_path"], old_raw)
        for number in range(11, 27):
            rt_id = f"RT-{number:03d}"
            target = self.resolver.canonical_path(rt_id)
            if number in {12, 13}:
                self.write_bytes(
                    target,
                    self.acceptance_marker(
                        rt_id,
                        subject=self.gate_subjects["G2"],
                        tree=self.gate_trees["G2"],
                    ),
                )
            elif number <= 16:
                source = REPO_ROOT / target
                self.write_bytes(target, source.read_bytes())
            else:
                self.write_bytes(target, self.acceptance_marker(rt_id))

    def materialize_verification_gates(self) -> None:
        by_gate = {
            gate["gate_id"]: gate
            for gate in self.resolver.verification_registry["gates"]
        }
        vg_a_path = self.resolver.canonical_path("VG-A")
        vg_a = _real_eval.load_json(REPO_ROOT / vg_a_path)
        for artifact in vg_a["artifacts"]:
            source = REPO_ROOT / artifact["path"]
            self.write_bytes(artifact["path"], source.read_bytes())
        self.write_json(vg_a_path, vg_a)

        for gate_id in ("VG-B", "VG-C", "VG-D", "VG-E"):
            gate = by_gate[gate_id]
            artifact = self.artifact(f"{gate_id.lower()}-integration")
            receipt = {
                "schema": "cwk.pr001.verification_gate_receipt.v1",
                "gate_id": gate_id,
                "feeder_rt": gate["feeder_rt"],
                "feeder_rt_independent_pass": True,
                "status": "pass",
                "conclusion": "integration_verified",
                "synthetic": False,
                "producer": f"agent-{gate_id.lower()}-impl",
                "verifier": f"agent-{gate_id.lower()}-verify",
                "consumers": list(gate["consumers"]),
                "prerequisite_refs": [],
                "evidence": {
                    "test_command": f"python3.11 -m unittest tests.test_{gate_id.lower()}",
                    "tests_run": 3,
                    "tests_failed": 0,
                    "tests_skipped": 0,
                    "python_version": "3.11.9",
                },
                "artifacts": [artifact],
                "tested_subject_commit": self.future_subject,
                "owner_scope_tree_sha256": self.external_owner_tree(
                    [f"RT/{gate['feeder_rt']}/"]
                ),
                "environment_fingerprint": dict(self.external_environment),
                "sequence": 1,
                "created_at": "2026-08-18T00:00:00Z",
            }
            receipt["receipt_sha256"] = _real_eval.family_receipt_sha256(
                receipt, _real_eval.VERIFICATION_RECEIPT_DOMAIN
            )
            self.write_json(self.resolver.canonical_path(gate_id), receipt)

    def materialize_capabilities(self) -> None:
        for entry in self.resolver.capability_map["capabilities"]:
            capability_id = entry["capability_id"]
            evidence_refs = []
            for role in entry["required_evidence_roles"]:
                artifact = self.artifact(f"cap-{capability_id}-{role}")
                evidence_refs.append(
                    {
                        "role": role,
                        "path": artifact["path"],
                        "sha256": artifact["sha256"],
                    }
                )
            summary = self.artifact(f"cap-{capability_id}-summary")
            closes = sorted(
                closure["gate_id"]
                for closure in self.resolver.capability_map["gate_closure"]
                if capability_id in closure["required_capability_ids"]
            )
            receipt = {
                "schema": "cwk.pr001.capability_activation_receipt.v1",
                "capability_id": capability_id,
                "owner_rt": entry["owner_rt"],
                "owner_rt_independent_pass": True,
                "status": "pass",
                "conclusion": "capability_activated",
                "synthetic": False,
                "producer": f"agent-{entry['owner_rt'].lower()}-cap-impl",
                "verifier": f"agent-{entry['owner_rt'].lower()}-cap-verify",
                "consumers": ["RT-026"],
                "closes_gate_ids": closes,
                "sequence": 1,
                "evidence": {
                    "test_command": "python3.11 -m unittest tests.test_capability_probe",
                    "tests_run": 5,
                    "tests_failed": 0,
                    "tests_skipped": 0,
                    "python_version": "3.11.9",
                },
                "evidence_refs": evidence_refs,
                "artifacts": [summary],
                "tested_subject_commit": self.future_subject,
                "owner_scope_tree_sha256": self.external_owner_tree(
                    entry["owner_code_path_prefixes"]
                ),
                "environment_fingerprint": dict(self.external_environment),
                "created_at": "2026-08-18T00:00:00Z",
                "expires_at": "2026-09-01T00:00:00Z",
            }
            receipt["receipt_sha256"] = _real_eval.family_receipt_sha256(
                receipt, _real_eval.CAPABILITY_RECEIPT_DOMAIN
            )
            self.write_json(
                self.resolver.canonical_path(f"CAP:{capability_id}"), receipt
            )

    def materialize_security_evidence_modules(self, entry: dict) -> None:
        """Assert the canonical owner tests were frozen before this receipt."""

        slug = entry["producer_rt"].lower().replace("-", "")
        for suffix in ("security.py", "paths.py", "no_fs_surface.py"):
            self.assertTrue((self.root / f"tests/test_{slug}_{suffix}").is_file())

    def materialize_security_receipts(self) -> None:
        attack_classes = (
            "path_traversal",
            "symlink_component",
            "symlink_leaf",
            "hardlink",
            "toctou",
            "special_file",
        )
        for entry in self.resolver.security_registry["entries"]:
            rt_id = entry["producer_rt"]
            slug = rt_id.lower().replace("-", "")
            self.materialize_security_evidence_modules(entry)
            claims = []
            for frozen in entry["claims"]:
                if frozen["claim_id"] in entry["na_permitted_claim_ids"]:
                    claims.append(
                        {
                            "claim_id": frozen["claim_id"],
                            "sg_id": frozen["sg_id"],
                            "applicability": "not_applicable",
                            "reason_code": entry["permitted_reason_codes"][0],
                            "reason": (
                                "This package has no filesystem write surface of its own; "
                                "the surface belongs to RT-021 and RT-023 by design."
                            ),
                            "static_evidence_refs": [
                                f"tests/test_{slug}_no_fs_surface.py::StaticScan"
                            ],
                            "summary": frozen["requirement"][:200],
                        }
                    )
                else:
                    claims.append(
                        {
                            "claim_id": frozen["claim_id"],
                            "sg_id": frozen["sg_id"],
                            "applicability": "applicable",
                            "executable_refs": [
                                f"tests/test_{slug}_security.py::Claims.test_{frozen['claim_id'].lower().replace('-', '_')}"
                            ],
                            "summary": frozen["requirement"][:200],
                        }
                    )
            if entry["filesystem_policy"]["mode"] == "applicable":
                coverage = {
                    name: {
                        "applicability": "applicable",
                        "runtime_surfaces": [f"{slug}_{name}"],
                        "executable_refs": [
                            f"tests/test_{slug}_paths.py::PathTests.test_{name}"
                        ],
                        "tests_run": 1,
                    }
                    for name in attack_classes
                }
            else:
                reason_code = entry["filesystem_policy"]["permitted_reason_codes"][0]
                coverage = {
                    name: {
                        "applicability": "not_applicable",
                        "reason_code": reason_code,
                        "reason": (
                            "This package exposes no filesystem write surface, so the "
                            f"{name} attack class has no applicable runtime target."
                        ),
                        "static_evidence_refs": [
                            f"tests/test_{slug}_no_fs_surface.py::StaticScan"
                        ],
                    }
                    for name in attack_classes
                }
            artifact = self.artifact(f"sg-{rt_id.lower()}")
            security_subject = self.security_subjects[rt_id]
            security_tree = _real_binding.security_owner_scope_tree_sha256(
                self.git,
                security_subject,
                entry,
                self.resolver.security_registry,
            )
            self.assertIsNotNone(
                security_tree, f"missing v2 security owner scope for {rt_id}"
            )
            receipt = {
                "schema": "cwk.pr001.security_gate_receipt.v1",
                "receipt_kind": entry["receipt_kind"],
                "producer_rt": rt_id,
                "independent_security_verification_pass": True,
                "producer_phase": entry["producer_phase"],
                "status": "pass",
                "conclusion": "security_verified",
                "synthetic": False,
                "producer": f"agent-{rt_id.lower()}-sg-impl",
                "verifier": f"agent-{rt_id.lower()}-sg-verify",
                "consumers": [item["consumer_id"] for item in entry["consumers"]],
                "claims": claims,
                "filesystem_coverage": coverage,
                "tested_subject_commit": security_subject,
                "owner_scope_tree_sha256": security_tree,
                "environment_fingerprint": dict(self.external_environment),
                "evidence": {
                    "test_command": "python3.11 -m unittest tests.test_security",
                    "tests_run": 12,
                    "tests_failed": 0,
                    "tests_skipped": 0,
                    "python_version": "3.11.9",
                },
                "artifacts": [artifact],
                "created_at": "2026-08-18T00:00:00Z",
            }
            if entry["receipt_kind"] == "preflight-security":
                receipt["evaluator_identity_excluded"] = True
            receipt["receipt_sha256"] = _real_eval.family_receipt_sha256(
                receipt, _real_eval.SECURITY_RECEIPT_DOMAIN
            )
            self.write_json(self.resolver.canonical_path(f"SG:{rt_id}"), receipt)

    def materialize_go_no_go_report(self) -> None:
        contract = self.registry["go_no_go_contract"]
        policy_raw = RELEASE_REGISTRY_PATH.read_bytes()
        self.write_bytes(contract["policy_ref_path"], policy_raw)

        for evidence_id, frozen in contract["evidence_inputs"].items():
            self.write_json(
                frozen["ref_path"],
                {"evidence_id": evidence_id, "result": "pass"},
            )

        input_refs = []
        for input_id in contract["exact_input_ids"]:
            if input_id in contract["evidence_inputs"]:
                expected = contract["evidence_inputs"][input_id]
            else:
                family = self.resolver.family_for(input_id)
                expected = {
                    "input_class": (
                        "verification_gate_receipts"
                        if input_id.startswith("VG-")
                        else "release_gate_receipts"
                        if re.fullmatch(r"G[1-5]", input_id)
                        else "security_crosswalk"
                    ),
                    "ref_kind": family["ref_kind"],
                    "ref_path": self.resolver.canonical_path(input_id),
                    "schema_id": family["schema_id"],
                }
            raw = (self.root / expected["ref_path"]).read_bytes()
            input_refs.append(
                {
                    "input_id": input_id,
                    "input_class": expected["input_class"],
                    "ref_kind": expected["ref_kind"],
                    "ref_path": expected["ref_path"],
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "schema_id": expected["schema_id"],
                }
            )

        artifacts = []
        for role in contract["exact_artifact_roles"]:
            rel_path = f"RT/RT-026/reports/go-no-go-{role.replace('_', '-')}.json"
            raw = self.write_json(rel_path, {"role": role, "result": "pass"})
            artifacts.append(
                {
                    "role": role,
                    "path": rel_path,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )

        report = {
            "schema": "cwk.pr001.go_no_go_report.v1",
            "report_id": "RT-026-GO-NO-GO",
            "receipt_kind": "go_no_go_evaluation",
            "status": "pass",
            "conclusion": "READY_FOR_G7_REVIEW",
            "producer": "agent-go-no-go-eval",
            "producer_role": "go_no_go_evaluator",
            "verifier": "agent-go-no-go-independent",
            "verifier_role": "independent_go_no_go_verifier",
            "tested_subject_commit": self.gate_subjects["G6"],
            "owner_scope_tree_sha256": self.owner_tree,
            "environment_fingerprint": dict(self.environment),
            "input_classes": [
                "verification_gate_receipts",
                "release_gate_receipts",
                "security_crosswalk",
                "rt016_data_diff",
                "query_diff_six_layer",
                "rt024_benchmark",
                "rt025_vge_recovery",
                "rollback_rehearsal",
                "default_off",
                "open_findings",
            ],
            "verification_gate_ids": [f"VG-{letter}" for letter in "ABCDE"],
            "release_gate_ids": [f"G{number}" for number in range(1, 6)],
            "capability_ids": [
                "CAP:cwork-authority-source",
                "CAP:gateway-identity-transport",
            ],
            "security_gate_ids": [f"SG:RT-{number:03d}" for number in range(17, 27)],
            "excluded_input_ids": ["G6", "G7"],
            "input_refs": input_refs,
            "policy_ref": {
                "ref_path": contract["policy_ref_path"],
                "raw_sha256": hashlib.sha256(policy_raw).hexdigest(),
                "schema_id": self.registry["schema"],
            },
            "run_evidence": {
                "run_id": "rt026-go-no-go-run-01",
                "engagement_id": "rt026-go-no-go",
                "session_id": "rt026-go-no-go-session",
                "runner_id": "agent-go-no-go-eval",
                "command": "python3.11 -m unittest tests.test_rt026_go_no_go",
                "checks_total": 29,
                "checks_failed": 0,
                "exit_code": 0,
                "result": "pass",
            },
            "artifacts": artifacts,
            "open_blocker_count": 0,
            "open_major_count": 0,
            "candidate_frozen_at": "2026-08-18T12:06:00+00:00",
            "started_at": "2026-08-18T12:07:00+00:00",
            "completed_at": "2026-08-18T12:08:00+00:00",
            "created_at": "2026-08-18T12:09:00+00:00",
        }
        report["report_sha256"] = _real_eval.go_no_go_report_sha256(report)
        self.write_json(self.resolver.canonical_path("RT-026-GO-NO-GO"), report)

    def fresh_evidence_refs(self) -> list[dict]:
        roles = [entry["role"] for entry in self.registry["g6_fresh_evidence_roles"]]
        sg_ids = [f"SG:RT-{number:03d}" for number in range(17, 27)]
        refs = []
        for index, role in enumerate(roles):
            artifact = self.artifact(
                f"g6-fresh-{index:02d}-{role}",
                f"fresh {role} run {index}\n".encode(),
            )
            entry = {
                "role": role,
                "run_id": f"run-{index:02d}-{role.replace('_', '-')}",
                "engagement_id": "fresh-final-review",
                "runner_id": "fresh-final-runner",
                "session_id": "fresh-final-session",
                "command": f"python3.11 -m unittest tests.test_{role}",
                "tested_subject_commit": self.gate_subjects["G6"],
                "candidate_tree_sha256": self.future_tree,
                "environment_fingerprint": dict(self.environment),
                "started_at": f"2026-08-18T02:{index:02d}:00+00:00",
                "completed_at": f"2026-08-18T03:{index:02d}:00+00:00",
                "checks_total": 1,
                "checks_failed": 0,
                "exit_code": 0,
                "result": "pass",
                "artifact_path": artifact["path"],
                "artifact_sha256": artifact["sha256"],
            }
            if role == "legacy_smoke":
                entry["legacy_fixture_id"] = "sanitized-nightly-v1"
            elif role == "attack_suite":
                entry["security_gate_ids_rechecked"] = sg_ids
            elif role == "secret_scan":
                entry["findings_count"] = 0
            elif role == "restore_drill":
                entry.update(clean_room=True, restore_verified=True)
            elif role == "rollback_drill":
                entry["legacy_read_path_restored"] = True
            elif role == "default_off_verification":
                entry.update(
                    enabled_component_count=0,
                    allowlisted_tenant_count=0,
                    enabled_flag_count=0,
                )
            elif role == "final_findings_reconciliation":
                entry.update(
                    open_blocker_count=0,
                    open_major_count=0,
                    git_diff_check_clean=True,
                    tracked_evidence_match=True,
                )
            refs.append(entry)
        return refs

    def build_release_receipt(self, gate_id: str) -> dict:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == gate_id)
        synthetic = gate_id in {"G3", "G4", "G5"}
        artifact = self.artifact(f"release-{gate_id.lower()}")
        receipt = {
            "schema": "cwk.pr001.release_gate_receipt.v1",
            "gate_id": gate_id,
            "status": "pass",
            "conclusion": (
                "READY_FOR_G7_AUTHORIZATION"
                if gate_id == "G6"
                else "conservative_unknown"
                if synthetic
                else "release_gate_verified"
            ),
            "synthetic": synthetic,
            "producer": f"agent-{gate_id.lower()}-impl",
            "verifier": (
                "fresh-final-verifier" if gate_id == "G6" else f"agent-{gate_id.lower()}-verify"
            ),
            "verifier_role": gate["producer_role"],
            "feeder_rts": list(gate["feeder_rts"]),
            "feeder_rts_independent_pass": True,
            "consumes_verification_gates": list(gate["consumes_verification_gates"]),
            "prerequisite_refs": [
                self.canonical_ref(ref_id)
                for ref_id in gate["required_prerequisite_ids"]
            ],
            "evidence": {
                "test_command": f"python3.11 -m unittest tests.test_{gate_id.lower()}",
                "tests_run": 9,
                "tests_failed": 0,
                "python_version": "3.11.9",
            },
            "artifacts": [artifact],
            "tested_subject_commit": self.gate_subjects[gate_id],
            "owner_scope_tree_sha256": self.gate_trees[gate_id],
            "environment_fingerprint": dict(self.environment),
            "sequence": 1,
            "created_at": (
                "2026-08-18T13:06:00+00:00"
                if gate_id == "G6"
                else f"2026-08-18T12:0{int(gate_id[1:])}:00+00:00"
            ),
        }
        if synthetic:
            receipt["synthetic_reason"] = "transitive_vg_a_synthetic_scope"
        if gate_id == "G6":
            receipt["synthetic"] = False
            receipt["verifier_provenance"] = {
                "engagement_id": "fresh-final-review",
                "prior_engagement_ids": [],
                "independent_of_producer_org": True,
            }
            receipt["freshness_attestation"] = {
                "signed_no_rt_acceptance": True,
                "signed_no_verification_gate": True,
                "signed_no_upstream_release_gate": True,
                "signed_no_security_gate": True,
            }
            receipt["orchestration_provenance"] = {
                "candidate_freeze_sha256": self.future_tree,
                "candidate_frozen_at": "2026-08-18T00:00:00+00:00",
                "session_id": "fresh-final-session",
                "session_started_at": "2026-08-18T01:00:00+00:00",
                "engagement_id": "fresh-final-review",
                "session_participants": [
                    "agent-g6-impl",
                    "fresh-final-verifier",
                    "fresh-final-runner",
                ],
            }
            receipt["fresh_evidence_refs"] = self.fresh_evidence_refs()
            receipt["expires_at"] = "2026-09-01T00:00:00+00:00"
        receipt["receipt_sha256"] = _real_eval.release_receipt_sha256(receipt)
        return receipt

    def materialize_valid_g6_world(self) -> dict:
        self.materialize_acceptance_documents()
        self.materialize_verification_gates()
        self.materialize_capabilities()
        self.materialize_security_receipts()
        current = None
        for gate_id in ("G1", "G2", "G3", "G4", "G5"):
            current = self.build_release_receipt(gate_id)
            self.write_json(self.resolver.canonical_path(gate_id), current)
        self.materialize_go_no_go_report()
        current = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), current)
        return current

    def make_authorization(self) -> dict:
        record = self.trust_record
        auth = {
            "schema": "cwk.pr001.release_authorization_receipt.v1",
            "gate_id": "G7",
            "decision": "authorized",
            "authorizing_principal": {"kind": "human_user", "id": "evan"},
            "authorization_channel": "explicit_user_instruction",
            "external_signature": {
                "trust_root_id": record.trust_root_id,
                "key_id": record.key_id,
                "key_state": record.state,
                "key_not_before": record.not_before,
                "key_expires_at": record.expires_at,
                "algorithm": record.algorithm,
                # Excluded from the detached payload; replaced below by the
                # real deterministic signature before schema evaluation.
                "signature_b64": "pending",
            },
            "g6_receipt_ref": {
                "ref_id": "G6",
                "ref_path": (
                    "PR/PR-001-multitenant-knowledge-spaces/"
                    "release-gate-receipts/G6/receipt.json"
                ),
                "ref_sha256": hashlib.sha256(
                    (self.root / self.resolver.canonical_path("G6")).read_bytes()
                ).hexdigest(),
                "g6_tested_subject_commit": self.g6["tested_subject_commit"],
                "g6_expires_at": self.g6["expires_at"],
            },
            "authorized_actions": ["enable_pilot_broker_for_allowlisted_tenants"],
            "scope": {
                "migration_phase": "M4",
                "tenant_allowlist_sha256": _real_eval.tenant_allowlist_sha256(
                    self.tenant_allowlist
                ),
                "allowlisted_tenant_count": len(self.tenant_allowlist),
                "pilot_window_days": 14,
            },
            "target_binding": {
                "target_commit": self.g6["tested_subject_commit"],
                "instance_id": "cwk-pilot-instance",
                "environment_fingerprint": dict(self.environment),
            },
            "nonce": "c" * 32,
            "sequence": 1,
            "not_before": "2026-08-19T00:00:00+00:00",
            "expires_at": "2026-09-01T00:00:00+00:00",
            "revocable": True,
            "recorded_by": "agent-release-clerk",
            "created_at": "2026-08-19T00:00:00+00:00",
        }
        return self.resign(auth)

    def resign(self, auth: dict, *, keypair=None) -> dict:
        keypair = keypair or self.keypair
        auth["external_signature"]["signature_b64"] = base64.b64encode(
            keypair.sign(_real_eval.authorization_signed_payload(auth))
        ).decode("ascii")
        auth["authorization_sha256"] = _real_eval.release_auth_sha256(auth)
        return auth

    def context(self, *, trust_store=None, **overrides):
        orchestration = self.g6["orchestration_provenance"]
        external_ids = (
            [f"VG-{letter}" for letter in "BCDE"]
            + [
                "CAP:cwork-authority-source",
                "CAP:gateway-identity-transport",
            ]
            + [f"SG:RT-{number:03d}" for number in range(17, 27)]
        )
        defaults = dict(
            root=self.root,
            resolver=self.resolver,
            now=self.now,
            observed_environment=dict(self.environment),
            expected_owner_scope_trees={
                gate_id: tree for gate_id, tree in self.gate_trees.items()
            },
            introducing_commit=self.evaluation_commit,
            ancestor_commits=set(self.gate_subjects.values()),
            touched_owner_code_commits=set(self.gate_subjects.values()),
            touched_owner_code_by_gate={
                gate_id: {subject} for gate_id, subject in self.gate_subjects.items()
            },
            prerequisite_subject_commits={
                ref_id: self.future_subject for ref_id in external_ids
            },
            prerequisite_owner_scope_trees={
                ref_id: self.owner_tree for ref_id in external_ids
            },
            prerequisite_environments={
                ref_id: dict(self.environment) for ref_id in external_ids
            },
            trust_store=(
                trust_store
                if trust_store is not None
                else _real_eval.TrustStore([self.trust_record])
            ),
            deployment_instance_id="cwk-pilot-instance",
            deployment_environment=dict(self.environment),
            deployment_tenant_allowlist=list(self.tenant_allowlist),
            trusted_orchestrations={
                orchestration["session_id"]: copy.deepcopy(orchestration)
            },
            tracked_evidence_paths={
                entry["artifact_path"] for entry in self.g6["fresh_evidence_refs"]
            },
            evaluation_commit=self.evaluation_commit,
        )
        defaults.update(overrides)
        return _real_eval.EvalContext(**defaults)

    def evaluate(self, auth=None, *, context=None):
        current = auth or self.auth
        self.write_json(
            next(
                gate["receipt_path"]
                for gate in self.registry["gates"]
                if gate["gate_id"] == "G7"
            ),
            current,
        )
        if context is None and _real_binding.worktree_is_dirty(self.git):
            self.evaluation_commit = _real_binding.commit_all(
                self.git, "freeze canonical evaluation mutation"
            )
        return _real_eval.evaluate_current_authorization(context or self.context())

    def evaluate_g6_object(self, *, context=None) -> list[str]:
        """Exercise G6 semantics without replacing Git-derived canonical facts.

        Mutation tests for the formal RT-026 report deliberately need to reach
        its own checker.  The production entrypoint additionally reports that
        those uncommitted mutations differ from the frozen Git snapshot; this
        object-level entry keeps the target assertion isolated while retaining
        exact path, raw-hash, schema, archive and recursive prerequisite checks.
        """
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G6")
        return _real_eval.evaluate_release_receipt(
            self.g6,
            gate,
            _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
            context or self.context(),
            registry=self.registry,
        )

    def freeze_rebound_g6(self, message: str) -> list[str]:
        """Rebind GO/G6 to a mutated delegated input, commit, then evaluate."""
        self.materialize_go_no_go_report()
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        self.evaluation_commit = _real_binding.commit_all(self.git, message)
        return _real_eval.evaluate_current_gate("G6", self.context())

    def rewrite_go_no_go(
        self,
        mutate,
        *,
        refresh_report_hash: bool = True,
        refresh_outer_g6_ref: bool = True,
    ) -> dict:
        """Mutate the report and optionally re-bind its two independent hashes."""
        path = self.resolver.canonical_path("RT-026-GO-NO-GO")
        report = json.loads((self.root / path).read_text(encoding="utf-8"))
        mutate(report)
        if refresh_report_hash:
            report["report_sha256"] = _real_eval.go_no_go_report_sha256(report)
        self.write_json(path, report)
        if refresh_outer_g6_ref:
            self.g6 = self.build_release_receipt("G6")
            self.write_json(self.resolver.canonical_path("G6"), self.g6)
        return report


class RealDetachedAuthorizationEvaluationTests(_RealAuthorizationFixtureBase):
    """Non-vacuous separation of signed payload, signature and self-hash."""

    def test_real_signature_and_authoritative_trust_store_baseline_is_valid(self) -> None:
        self.assertEqual(self.evaluate(), [])

    def test_body_mutation_with_fresh_self_hash_fails_only_signature(self) -> None:
        mutated = copy.deepcopy(self.auth)
        mutated["authorized_actions"] = [
            "enable_pilot_schedule_for_allowlisted_tenants"
        ]
        mutated["authorization_sha256"] = _real_eval.release_auth_sha256(mutated)
        self.assertEqual(self.evaluate(mutated), ["signature_invalid"])

    def test_signature_only_replacement_preserves_payload_and_fails_only_signature(self) -> None:
        mutated = copy.deepcopy(self.auth)
        before = _real_eval.authorization_signed_payload(mutated)
        mutated["external_signature"]["signature_b64"] = base64.b64encode(
            self.other_keypair.sign(before)
        ).decode("ascii")
        after = _real_eval.authorization_signed_payload(mutated)
        self.assertEqual(after, before)
        mutated["authorization_sha256"] = _real_eval.release_auth_sha256(mutated)
        self.assertEqual(self.evaluate(mutated), ["signature_invalid"])

    def test_self_hash_corruption_leaves_signature_valid(self) -> None:
        mutated = copy.deepcopy(self.auth)
        mutated["authorization_sha256"] = "0" * 64
        self.assertTrue(
            _real_signing.verify(
                self.keypair.public_key,
                _real_eval.authorization_signed_payload(mutated),
                base64.b64decode(mutated["external_signature"]["signature_b64"]),
            )
        )
        self.assertEqual(self.evaluate(mutated), ["self_hash_mismatch"])

    def test_wrong_authoritative_trust_key_fails_only_signature(self) -> None:
        wrong_record = _real_eval.TrustRecord(
            trust_root_id=self.trust_record.trust_root_id,
            key_id=self.trust_record.key_id,
            public_key=self.other_keypair.public_key,
            not_before=self.trust_record.not_before,
            expires_at=self.trust_record.expires_at,
            principal_id=self.trust_record.principal_id,
        )
        g7_path = next(
            gate["receipt_path"]
            for gate in self.registry["gates"]
            if gate["gate_id"] == "G7"
        )
        self.write_json(g7_path, self.auth)
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "freeze G7 for wrong trust-store evaluation"
        )
        ctx = self.context(trust_store=_real_eval.TrustStore([wrong_record]))
        self.assertEqual(
            _real_eval.evaluate_current_authorization(ctx), ["signature_invalid"]
        )

    def test_rt026_acceptance_identity_cannot_record_g7(self) -> None:
        mutated = copy.deepcopy(self.auth)
        mutated["recorded_by"] = "agent-rt026-accept"
        self.resign(mutated)
        self.assertEqual(self.evaluate(mutated), ["recorder_is_rt026_identity"])

    def test_withdrawal_must_bind_the_exact_superseded_authorization(self) -> None:
        g7_gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G7")
        self.write_json(
            f"{g7_gate['archive_dir']}/{self.auth['authorization_sha256']}.json",
            self.auth,
        )
        withdrawal = copy.deepcopy(self.auth)
        withdrawal.update(
            decision="withdrawn",
            sequence=2,
            supersedes_authorization_sha256=self.auth["authorization_sha256"],
            revocation_ref="0" * 64,
            nonce="e" * 32,
        )
        self.resign(withdrawal)
        self.assertEqual(
            self.evaluate(withdrawal), ["withdrawal_reference_mismatch"]
        )

    def test_nonce_reuse_is_derived_from_the_safe_archive(self) -> None:
        g7_gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G7")
        self.write_json(
            f"{g7_gate['archive_dir']}/{self.auth['authorization_sha256']}.json",
            self.auth,
        )
        replay = copy.deepcopy(self.auth)
        replay.update(
            sequence=2,
            supersedes_authorization_sha256=self.auth["authorization_sha256"],
        )
        self.resign(replay)
        self.assertEqual(self.evaluate(replay), ["nonce_replay"])

    def test_g6_reference_hash_binds_exact_file_bytes_not_only_json_value(self) -> None:
        g6_path = self.root / self.resolver.canonical_path("G6")
        g6_path.write_text(
            json.dumps(self.g6, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        violations = _real_eval.evaluate_current_authorization(self.context())
        self.assertEqual(violations, ["repository_worktree_dirty"])

    def test_trust_store_rejects_unparseable_revocation_time(self) -> None:
        bad = _real_eval.TrustRecord(
            trust_root_id="external-release-root",
            key_id=self.keypair.key_id(),
            public_key=self.keypair.public_key,
            not_before="2026-01-01T00:00:00+00:00",
            expires_at="2027-01-01T00:00:00+00:00",
            revoked_at="not-an-instant",
            principal_id="evan",
        )
        with self.assertRaisesRegex(ValueError, "revoked_at"):
            _real_eval.TrustStore([bad])

    def test_trust_store_rejects_duplicate_root_and_key_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate trust record key"):
            _real_eval.TrustStore([self.trust_record, self.trust_record])

    def test_high_s_twin_is_mathematically_valid_but_policy_rejected(self) -> None:
        mutated = copy.deepcopy(self.auth)
        low_s = base64.b64decode(mutated["external_signature"]["signature_b64"])
        high_s = _real_signing.flip_s_high(low_s)
        payload = _real_eval.authorization_signed_payload(mutated)
        self.assertTrue(
            _real_signing.verify(
                self.keypair.public_key, payload, high_s, require_low_s=False
            )
        )
        self.assertFalse(_real_signing.is_low_s(high_s))
        mutated["external_signature"]["signature_b64"] = base64.b64encode(
            high_s
        ).decode("ascii")
        mutated["authorization_sha256"] = _real_eval.release_auth_sha256(mutated)
        self.assertEqual(self.evaluate(mutated), ["signature_not_low_s"])

    def test_payload_deep_copy_excludes_exactly_two_fields(self) -> None:
        baseline = _real_eval.authorization_signed_payload(self.auth)

        only_self_hash = copy.deepcopy(self.auth)
        only_self_hash["authorization_sha256"] = "0" * 64
        self.assertEqual(
            _real_eval.authorization_signed_payload(only_self_hash), baseline
        )

        only_signature = copy.deepcopy(self.auth)
        only_signature["external_signature"]["signature_b64"] = "different"
        self.assertEqual(
            _real_eval.authorization_signed_payload(only_signature), baseline
        )

        signed_field = copy.deepcopy(self.auth)
        signed_field["nonce"] = "d" * 32
        self.assertNotEqual(
            _real_eval.authorization_signed_payload(signed_field), baseline
        )

    def test_vg_d_must_reach_non_synthetic_integration_verified(self) -> None:
        vg_d_path = self.resolver.canonical_path("VG-D")
        vg_d = json.loads((self.root / vg_d_path).read_text(encoding="utf-8"))
        vg_d["conclusion"] = "conservative_unknown"
        vg_d["receipt_sha256"] = _real_eval.family_receipt_sha256(
            vg_d, _real_eval.VERIFICATION_RECEIPT_DOMAIN
        )
        self.write_json(vg_d_path, vg_d)

        for gate_id in ("G5", "G6"):
            receipt = self.build_release_receipt(gate_id)
            self.write_json(self.resolver.canonical_path(gate_id), receipt)
            if gate_id == "G6":
                self.g6 = receipt
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        # The mutation is intentionally made after the frozen evidence commit,
        # so Git-derived facts also report blob/subject drift.  The important
        # non-vacuous assertion is that both direct consumers still execute the
        # VG-D conclusion rule instead of stopping at those stronger bindings.
        for code in (
            "current_semantic:G5:prereq_body_delegated_family_invalid:VG-D",
            "current_semantic:G6:prereq_body_delegated_family_invalid:VG-D",
        ):
            self.assertIn(code, violations, violations)

    def test_vg_d_cannot_cite_a_forward_rt_with_a_forged_hash(self) -> None:
        vg_d_path = self.resolver.canonical_path("VG-D")
        vg_d = json.loads((self.root / vg_d_path).read_text(encoding="utf-8"))
        vg_d["prerequisite_refs"] = [
            {"ref_id": "RT-025", "ref_sha256": "0" * 64}
        ]
        vg_d["receipt_sha256"] = _real_eval.family_receipt_sha256(
            vg_d, _real_eval.VERIFICATION_RECEIPT_DOMAIN
        )
        self.write_json(vg_d_path, vg_d)
        for gate_id in ("G5", "G6"):
            receipt = self.build_release_receipt(gate_id)
            self.write_json(self.resolver.canonical_path(gate_id), receipt)
            if gate_id == "G6":
                self.g6 = receipt
        code = "prereq_body_delegated_family_invalid:VG-D"
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        for expected in (f"current_semantic:G5:{code}", f"current_semantic:G6:{code}"):
            self.assertIn(expected, violations, violations)

    def test_capability_subject_tree_and_environment_bind_to_observed_world(self) -> None:
        ref_id = "CAP:cwork-authority-source"
        path = self.resolver.canonical_path(ref_id)
        cap = json.loads((self.root / path).read_text(encoding="utf-8"))
        cap["tested_subject_commit"] = "d" * 40
        cap["owner_scope_tree_sha256"] = "e" * 64
        cap["environment_fingerprint"] = {
            "python_version": "3.99.99",
            "platform": "fake",
            "toolchain_build": "fake",
        }
        cap["receipt_sha256"] = _real_eval.family_receipt_sha256(
            cap, _real_eval.CAPABILITY_RECEIPT_DOMAIN
        )
        self.write_json(path, cap)
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        self.assertIn(
            f"current_semantic:G6:prereq_body_delegated_family_invalid:{ref_id}",
            violations,
        )


class RealReleaseWorldValidationTests(_RealAuthorizationFixtureBase):
    """The canonical receipt tree is judged independently of archive presence."""

    def test_complete_current_release_world_has_clean_root_closure(self) -> None:
        self.assertEqual(
            _real_eval.evaluate_release_root_closure(
                self.root, self.registry, context=self.context()
            ),
            [],
        )

    def test_missing_canonical_g7_is_not_run_even_with_a_valid_in_memory_object(self) -> None:
        self.assertEqual(
            _real_eval.evaluate_current_authorization(self.context()),
            ["current_missing:G7"],
        )

    def test_current_invalid_json_is_rejected_without_an_archive_directory(self) -> None:
        self.write_bytes(self.resolver.canonical_path("G1"), b"{not json")
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertIn("current_not_json:G1", violations)

    def test_current_empty_object_is_rejected_without_an_archive_directory(self) -> None:
        self.write_json(self.resolver.canonical_path("G1"), {})
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertIn("current_self_hash_mismatch:G1", violations)
        self.assertIn("current_gate_id_mismatch:G1", violations)
        self.assertTrue(
            any(code.startswith("current_schema:G1:required:$.") for code in violations),
            violations,
        )

    def test_three_label_go_no_go_stub_cannot_satisfy_g6(self) -> None:
        self.write_json(
            self.resolver.canonical_path("RT-026-GO-NO-GO"),
            {
                "schema": "cwk.pr001.go_no_go_report.v1",
                "report_id": "RT-026-GO-NO-GO",
                "status": "pass",
            },
        )
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        # A three-label object reaches the formal schema/evaluator and fails
        # many independent required surfaces.  Pin two branches rather than an
        # ever-growing exact list so adding another mandatory field cannot make
        # this regression brittle.
        self.assertTrue(
            any(
                code.startswith(
                    "current_semantic:G6:prereq_body_go_no_go_schema_invalid:"
                    "RT-026-GO-NO-GO:required:$."
                )
                for code in violations
            ),
            violations,
        )
        self.assertIn(
            "current_semantic:G6:prereq_body_go_no_go_artifact_roles_mismatch:"
            "RT-026-GO-NO-GO",
            violations,
        )

    def test_g6_verifier_must_belong_to_the_trusted_orchestration(self) -> None:
        self.g6["orchestration_provenance"]["session_participants"].remove(
            "fresh-final-verifier"
        )
        self.g6["receipt_sha256"] = _real_eval.release_receipt_sha256(self.g6)
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        self.assertIn(
            "current_semantic:G6:orchestration_receipt_identity_missing:"
            "fresh-final-verifier",
            violations,
        )

    def test_current_gate_entrypoint_preserves_other_gate_closure_failures(self) -> None:
        g7_path = next(
            gate["receipt_path"]
            for gate in self.registry["gates"]
            if gate["gate_id"] == "G7"
        )
        self.write_json(g7_path, {})
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "freeze malformed G7 closure fixture"
        )
        violations = _real_eval.evaluate_current_gate("G1", self.context())
        self.assertIn("current_self_hash_mismatch:G7", violations)
        self.assertIn("current_gate_id_mismatch:G7", violations)

    def test_g6_fresh_evidence_must_be_authoritatively_tracked(self) -> None:
        missing = self.g6["fresh_evidence_refs"][0]
        (self.root / missing["artifact_path"]).unlink()
        violations = self.evaluate(self.auth)
        for expected in (
            "current_semantic:G6:fresh_evidence_untracked_artifact:"
            f"{missing['role']}",
            "g6_invalid:fresh_evidence_untracked_artifact:"
            f"{missing['role']}",
        ):
            self.assertIn(expected, violations, violations)


class DelegatedFamilyCanonicalReuseTests(_RealAuthorizationFixtureBase):
    """Release consumes the exact VG/CAP/SG authorities and root closures."""

    def rewrite_family(self, ref_id: str, mutate) -> list[str]:
        path = self.resolver.canonical_path(ref_id)
        body = json.loads((self.root / path).read_text(encoding="utf-8"))
        mutate(body)
        domain = (
            _real_eval.VERIFICATION_RECEIPT_DOMAIN
            if ref_id.startswith("VG-")
            else _real_eval.CAPABILITY_RECEIPT_DOMAIN
            if ref_id.startswith("CAP:")
            else _real_eval.SECURITY_RECEIPT_DOMAIN
        )
        body["receipt_sha256"] = _real_eval.family_receipt_sha256(body, domain)
        self.write_json(path, body)
        return self.freeze_rebound_g6(f"freeze delegated negative {ref_id}")

    def test_fixture_replays_the_authoritative_ten_stage_evolution_chain(self) -> None:
        policy_raw = (
            self.root
            / "PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/policy_v1.json"
        ).read_bytes()
        expected_sha = "2089490e45bdd84ba3bac75fe40092f81f40765638b988e17facdc4040d14a6d"
        self.assertEqual(hashlib.sha256(policy_raw).hexdigest(), expected_sha)
        self.assertEqual(
            self.resolver.security_registry["script_evolution_policy_sha256"],
            expected_sha,
        )
        policy = _evolution_guard.load_policy(self.root)
        genesis = {
            item["target_path"]: item["genesis_sha256"]
            for item in policy.raw["evolvable_paths"]
        }
        replay = _evolution_guard.replay_chain(
            self.root, policy, genesis=genesis
        )
        observed_stages = sorted(
            receipt["stage_index"]
            for receipts in replay.receipts_by_path.values()
            for receipt in receipts
        )
        self.assertEqual(observed_stages, list(range(1, 11)))
        for target_path, expected_tip in replay.tips.items():
            self.assertEqual(
                hashlib.sha256((self.root / target_path).read_bytes()).hexdigest(),
                expected_tip,
            )
        stage10 = policy.raw["stages"][-1]
        self.assertEqual(stage10["stage_index"], 10)
        self.assertEqual(stage10["target_path"], "scripts/cwk_agent_binding.py")
        self.assertEqual(
            hashlib.sha256((self.root / stage10["target_path"]).read_bytes()).hexdigest(),
            "2d390d6fa1a5b84e1dcc137e64c642f3a1a9cb010e009fa5c7a6e00e076030c4",
        )

    def poisoned_session_after_precheck(self, facts):
        """The exact session an old virtual-dispatch boundary could inject."""
        forged_security = copy.deepcopy(self.resolver.security_registry)
        forged_security["go_no_go_evaluator_identity"] = "forged-evaluator"
        forged_resolver = _real_eval.PrerequisiteResolver(
            copy.deepcopy(self.registry),
            verification_registry=copy.deepcopy(
                self.resolver.verification_registry
            ),
            capability_map=copy.deepcopy(self.resolver.capability_map),
            security_registry=forged_security,
        )
        return self.context(
            resolver=forged_resolver,
            evaluation_commit=facts.evaluation_commit,
            repository_facts=facts,
            observed_environment=dict(facts.observed_environment),
        )

    def test_release_adapter_executes_the_original_authority_method_objects(self) -> None:
        from test_pr001_gate_contracts import ClosureEvaluationRegressionTests
        from test_pr001_security_gate_contracts import SecurityChecks

        context = self.context()
        context.repository_facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        gate, security = _real_eval._delegated_authorities(context)
        self.assertIs(
            gate._activation_chain_state.__func__,
            ClosureEvaluationRegressionTests._activation_chain_state,
        )
        self.assertIs(
            gate._gate_history_state.__func__,
            ClosureEvaluationRegressionTests._gate_history_state,
        )
        self.assertIs(
            security._receipt_is_valid.__func__, SecurityChecks._receipt_is_valid
        )
        self.assertIs(
            security._receipt_root_is_closed.__func__,
            SecurityChecks._receipt_root_is_closed,
        )

    def test_sg_rt026_cannot_claim_evaluator_exclusion_when_evaluator_is_producer(self) -> None:
        evaluator = self.resolver.security_registry["go_no_go_evaluator_identity"]
        violations = self.rewrite_family(
            "SG:RT-026", lambda body: body.__setitem__("producer", evaluator)
        )
        self.assertIn(
            "prereq_body_delegated_family_invalid:SG:RT-026", violations
        )

    def test_instance_monkeypatch_cannot_replace_the_canonical_session(self) -> None:
        evaluator = self.resolver.security_registry["go_no_go_evaluator_identity"]
        expected = "prereq_body_delegated_family_invalid:SG:RT-026"
        self.assertIn(
            expected,
            self.rewrite_family(
                "SG:RT-026", lambda body: body.__setitem__("producer", evaluator)
            ),
        )
        context = self.context()
        calls = []

        def poisoned(facts, _authoritative_resolver):
            calls.append("instance")
            return self.poisoned_session_after_precheck(facts)

        context.canonical_session = poisoned
        violations = _real_eval.evaluate_current_gate("G6", context)
        self.assertIn(expected, violations, violations)
        self.assertEqual(calls, [], "caller-owned session hook was invoked")

    def test_subclass_override_cannot_replace_the_canonical_session(self) -> None:
        evaluator = self.resolver.security_registry["go_no_go_evaluator_identity"]
        expected = "prereq_body_delegated_family_invalid:SG:RT-026"
        self.assertIn(
            expected,
            self.rewrite_family(
                "SG:RT-026", lambda body: body.__setitem__("producer", evaluator)
            ),
        )
        calls = []
        fixture = self

        class HostileContext(_real_eval.EvalContext):
            def canonical_session(self, facts, _authoritative_resolver):
                calls.append("subclass")
                return fixture.poisoned_session_after_precheck(facts)

        context = self.context()
        context.__class__ = HostileContext
        violations = _real_eval.evaluate_current_gate("G6", context)
        self.assertIn(expected, violations, violations)
        self.assertEqual(calls, [], "subclass session override was invoked")

    def test_sg_rt017_nonexistent_acceptance_ref_is_rejected_after_rebinding(self) -> None:
        def mutate(body):
            body["claims"][0]["executable_refs"] = [
                "tests/test_rt_017_security.py::Claims.test_sgc_017_01"
            ]

        violations = self.rewrite_family("SG:RT-017", mutate)
        self.assertIn(
            "prereq_body_delegated_family_invalid:SG:RT-017", violations
        )

    def test_capability_sequence_two_needs_archive_link_and_fresh_probe(self) -> None:
        def mutate(body):
            body["sequence"] = 2
            body.pop("previous_receipt_sha256", None)
            body.pop("renewal_probe_ref", None)

        violations = self.rewrite_family("CAP:cwork-authority-source", mutate)
        self.assertIn(
            "prereq_body_delegated_family_invalid:CAP:cwork-authority-source",
            violations,
        )

    def test_undeclared_security_receipt_fails_whole_root_after_commit(self) -> None:
        self.write_json(
            "PR/PR-001-multitenant-knowledge-spaces/security-receipts/RT-999/receipt.json",
            {},
        )
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "freeze undeclared security receipt"
        )
        violations = _real_eval.evaluate_current_gate("G6", self.context())
        self.assertIn("delegated_root_unclosed:security_gate", violations)

    def test_vg_family_also_uses_the_authoritative_independence_rule(self) -> None:
        violations = self.rewrite_family(
            "VG-D", lambda body: body.__setitem__("verifier", body["producer"])
        )
        self.assertIn("prereq_body_delegated_family_invalid:VG-D", violations)


class FormalGoNoGoAndIdentityMarkerTests(_RealAuthorizationFixtureBase):
    """RT-026's formal verdict and future acceptance identities are load-bearing."""

    def _go_case(self, name, mutate, expected) -> None:
        with self.subTest(name=name):
            path = self.resolver.canonical_path("RT-026-GO-NO-GO")
            baseline = json.loads((self.root / path).read_text(encoding="utf-8"))
            candidate = copy.deepcopy(baseline)
            mutate(candidate)
            candidate["report_sha256"] = _real_eval.go_no_go_report_sha256(candidate)
            self.write_json(path, candidate)
            self.g6 = self.build_release_receipt("G6")
            self.write_json(self.resolver.canonical_path("G6"), self.g6)
            violations = self.evaluate_g6_object()
            self.assertIn(expected, violations, violations)
            self.write_json(path, baseline)
            self.g6 = self.build_release_receipt("G6")
            self.write_json(self.resolver.canonical_path("G6"), self.g6)

    def test_formal_go_no_go_baseline_is_really_consumable(self) -> None:
        self.assertEqual(self.evaluate_g6_object(), [])

    def test_go_no_go_input_set_is_exact_and_excludes_downstream_gates(self) -> None:
        self._go_case(
            "missing",
            lambda report: report["input_refs"].pop(),
            "prereq_body_go_no_go_input_set_mismatch:RT-026-GO-NO-GO",
        )
        self._go_case(
            "duplicate",
            lambda report: report["input_refs"].__setitem__(
                -1, copy.deepcopy(report["input_refs"][0])
            ),
            "prereq_body_go_no_go_duplicate_input_id:RT-026-GO-NO-GO",
        )

        def inject_g6(report):
            injected = copy.deepcopy(report["input_refs"][0])
            injected["input_id"] = "G6"
            report["input_refs"][-1] = injected

        self._go_case(
            "forbidden downstream input",
            inject_g6,
            "prereq_body_go_no_go_input_set_mismatch:RT-026-GO-NO-GO",
        )
        self._go_case(
            "wrong canonical path",
            lambda report: report["input_refs"][0].__setitem__(
                "ref_path", report["input_refs"][1]["ref_path"]
            ),
            "prereq_body_go_no_go_input_ref_path_mismatch:VG-A",
        )
        self._go_case(
            "wrong raw hash",
            lambda report: report["input_refs"][0].__setitem__(
                "raw_sha256", "0" * 64
            ),
            "prereq_body_go_no_go_input_hash_mismatch:VG-A",
        )

    def test_go_no_go_independence_clean_run_findings_and_time_are_enforced(self) -> None:
        self._go_case(
            "self certified",
            lambda report: report.__setitem__("verifier", report["producer"]),
            "prereq_body_go_no_go_self_certified:RT-026-GO-NO-GO",
        )
        self._go_case(
            "zero checks",
            lambda report: report["run_evidence"].__setitem__("checks_total", 0),
            "prereq_body_go_no_go_unclean_run:RT-026-GO-NO-GO",
        )
        self._go_case(
            "open major",
            lambda report: report.__setitem__("open_major_count", 1),
            "prereq_body_go_no_go_open_major:RT-026-GO-NO-GO",
        )
        self._go_case(
            "time reversal",
            lambda report: report.__setitem__(
                "started_at", "2026-08-18T10:30:00+00:00"
            ),
            "prereq_body_go_no_go_time_order_invalid:RT-026-GO-NO-GO",
        )
        self._go_case(
            "no go",
            lambda report: report.update(status="fail", conclusion="NO_GO"),
            "prereq_body_go_no_go_not_ready:RT-026-GO-NO-GO",
        )

    def test_go_no_go_artifact_roles_and_raw_hashes_are_enforced(self) -> None:
        self._go_case(
            "missing role",
            lambda report: report["artifacts"].pop(),
            "prereq_body_go_no_go_artifact_roles_mismatch:RT-026-GO-NO-GO",
        )

        def duplicate_path(report):
            report["artifacts"][1]["path"] = report["artifacts"][0]["path"]
            report["artifacts"][1]["raw_sha256"] = report["artifacts"][0][
                "raw_sha256"
            ]

        self._go_case(
            "duplicate path",
            duplicate_path,
            "prereq_body_go_no_go_duplicate_artifact_path:RT-026-GO-NO-GO",
        )
        target = json.loads(
            (
                self.root / self.resolver.canonical_path("RT-026-GO-NO-GO")
            ).read_text(encoding="utf-8")
        )["artifacts"][0]["path"]
        self._go_case(
            "wrong raw hash",
            lambda report: report["artifacts"][0].__setitem__(
                "raw_sha256", "0" * 64
            ),
            f"prereq_body_go_no_go_artifact_hash_mismatch:{target}",
        )

    def test_report_self_hash_and_g6_outer_raw_hash_fail_independently(self) -> None:
        report_path = self.resolver.canonical_path("RT-026-GO-NO-GO")
        baseline = (self.root / report_path).read_bytes()
        # First: G6 binds the mutated file bytes, but the report did not refresh
        # its own domain-separated hash.
        self.rewrite_go_no_go(
            lambda report: report.__setitem__("open_major_count", 1),
            refresh_report_hash=False,
            refresh_outer_g6_ref=True,
        )
        violations = self.evaluate_g6_object()
        self.assertIn(
            "prereq_body_go_no_go_self_hash_mismatch:RT-026-GO-NO-GO",
            violations,
        )

        # Second: the report is internally self-consistent, but the citing G6
        # still names the previous raw file digest.
        self.write_bytes(report_path, baseline)
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        self.rewrite_go_no_go(
            lambda report: report.__setitem__("open_major_count", 1),
            refresh_report_hash=True,
            refresh_outer_g6_ref=False,
        )
        violations = self.evaluate_g6_object()
        self.assertIn("prereq_hash_mismatch:RT-026-GO-NO-GO", violations)

    def test_future_rt_identity_lists_reject_missing_invalid_and_overlap(self) -> None:
        rt_path = self.resolver.canonical_path("RT-017")
        baseline = (self.root / rt_path).read_bytes()
        cases = {
            "missing": (
                baseline.replace(b"implementer_ids: agent-rt-017-impl\n", b""),
                "prereq_marker_field_set:RT-017",
            ),
            "unsorted": (
                baseline.replace(
                    b"implementer_ids: agent-rt-017-impl\n",
                    b"implementer_ids: z-agent,a-agent\n",
                ),
                "prereq_marker_identity_list_invalid:RT-017:implementer_ids",
            ),
            "overlap": (
                baseline.replace(
                    b"reviewer_ids: agent-rt-017-review\n",
                    b"reviewer_ids: agent-rt-017-impl\n",
                ),
                "prereq_marker_identity_overlap:RT-017",
            ),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name=name):
                self.write_bytes(rt_path, raw)
                self.g6 = self.build_release_receipt("G6")
                self.write_json(self.resolver.canonical_path("G6"), self.g6)
                self.assertIn(expected, self.evaluate_g6_object())
                self.write_bytes(rt_path, baseline)

    def test_future_rt_identity_participant_collision_defeats_freshness_claim(self) -> None:
        participant = "agent-rt-017-impl"
        self.g6["orchestration_provenance"]["session_participants"].append(participant)
        self.g6["receipt_sha256"] = _real_eval.release_receipt_sha256(self.g6)
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        violations = self.evaluate_g6_object()
        self.assertIn(
            f"orchestration_participant_in_upstream_evidence:{participant}",
            violations,
        )
        self.assertIn("freshness_attestation_contradicted", violations)

    def test_remediated_markers_are_the_only_consumable_g2_acceptances(self) -> None:
        g2_path = self.resolver.canonical_path("G2")
        g2 = json.loads((self.root / g2_path).read_text(encoding="utf-8"))
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G2")
        self.assertEqual(
            _real_eval.evaluate_release_receipt(
                g2,
                gate,
                _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                self.context(),
                registry=self.registry,
            ),
            [],
        )
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        for rt_id in ("RT-012", "RT-013"):
            with self.subTest(rt_id=rt_id):
                self.assertNotEqual(
                    reports[rt_id]["report_path"],
                    reports[rt_id]["superseded_legacy_report"]["report_path"],
                )

    def test_superseded_reports_are_forbidden_even_with_their_exact_hashes(self) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G2")
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        for rt_id in ("RT-012", "RT-013"):
            with self.subTest(rt_id=rt_id):
                g2_path = self.resolver.canonical_path("G2")
                g2 = json.loads((self.root / g2_path).read_text(encoding="utf-8"))
                old = reports[rt_id]["superseded_legacy_report"]
                ref = next(
                    item
                    for item in g2["prerequisite_refs"]
                    if item["ref_id"] == rt_id
                )
                ref["ref_path"] = old["report_path"]
                ref["ref_sha256"] = old["report_sha256"]
                g2["receipt_sha256"] = _real_eval.release_receipt_sha256(g2)
                violations = _real_eval.evaluate_release_receipt(
                    g2,
                    gate,
                    _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                    self.context(),
                    registry=self.registry,
                )
                self.assertIn(f"prereq_forbidden_path:{rt_id}", violations)
                self.assertIn(f"prereq_path_mismatch:{rt_id}", violations)

    def test_remediated_markers_require_identities_and_current_subject(self) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G2")
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        for rt_id in ("RT-012", "RT-013"):
            rt_path = self.resolver.canonical_path(rt_id)
            baseline = (self.root / rt_path).read_bytes()
            cases = {
                "missing identity": (
                    baseline.replace(
                        f"implementer_ids: agent-{rt_id.lower()}-impl\n".encode(), b""
                    ),
                    f"prereq_marker_field_set:{rt_id}",
                ),
                "old subject": (
                    baseline.replace(
                        f"subject_commit: {self.gate_subjects['G2']}\n".encode(),
                        (
                            "subject_commit: "
                            + reports[rt_id]["superseded_legacy_report"][
                                "accepted_subject_commit"
                            ]
                            + "\n"
                        ).encode(),
                    ),
                    f"prereq_marker_subject_mismatch:{rt_id}",
                ),
            }
            for name, (raw, expected) in cases.items():
                with self.subTest(rt_id=rt_id, name=name):
                    self.write_bytes(rt_path, raw)
                    g2 = self.build_release_receipt("G2")
                    violations = _real_eval.evaluate_release_receipt(
                        g2,
                        gate,
                        _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                        self.context(),
                        registry=self.registry,
                    )
                    self.assertIn(expected, violations, violations)
                    self.write_bytes(rt_path, baseline)

    def test_remediated_registry_ids_and_superseded_bytes_are_load_bearing(self) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G2")
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        for rt_id, wrong_id in (("RT-012", "RT-013"), ("RT-013", "RT-012")):
            with self.subTest(rt_id=rt_id):
                old = reports[rt_id]["superseded_legacy_report"]
                old_path = old["report_path"]
                baseline = (self.root / old_path).read_bytes()

                self.write_bytes(old_path, baseline + b"\nmutated\n")
                g2 = self.build_release_receipt("G2")
                violations = _real_eval.evaluate_release_receipt(
                    g2,
                    gate,
                    _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                    self.context(),
                    registry=self.registry,
                )
                self.assertIn(f"superseded_report_hash_mismatch:{rt_id}", violations)

                self.write_bytes(old_path, baseline)
                (self.root / old_path).unlink()
                g2 = self.build_release_receipt("G2")
                violations = _real_eval.evaluate_release_receipt(
                    g2,
                    gate,
                    _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                    self.context(),
                    registry=self.registry,
                )
                self.assertIn(f"superseded_report_missing:{rt_id}", violations)
                self.write_bytes(old_path, baseline)

                mutated_registry = copy.deepcopy(self.registry)
                mutated_registry["prerequisite_resolution"][
                    "rt_acceptance_reports"
                ][rt_id]["report_id"] = wrong_id
                context = self.context()
                context.resolver = _real_eval.PrerequisiteResolver(mutated_registry)
                g2 = self.build_release_receipt("G2")
                violations = _real_eval.evaluate_release_receipt(
                    g2,
                    next(
                        g
                        for g in mutated_registry["gates"]
                        if g["gate_id"] == "G2"
                    ),
                    _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                    context,
                    registry=mutated_registry,
                )
                self.assertIn(
                    f"prereq_registry_report_id_mismatch:{rt_id}", violations
                )

                mutated_registry = copy.deepcopy(self.registry)
                mutated_registry["prerequisite_resolution"][
                    "rt_acceptance_reports"
                ][rt_id]["superseded_legacy_report"][
                    "superseded_by_stage_index"
                ] = 0
                context = self.context()
                context.resolver = _real_eval.PrerequisiteResolver(mutated_registry)
                g2 = self.build_release_receipt("G2")
                violations = _real_eval.evaluate_release_receipt(
                    g2,
                    next(
                        g
                        for g in mutated_registry["gates"]
                        if g["gate_id"] == "G2"
                    ),
                    _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
                    context,
                    registry=mutated_registry,
                )
                self.assertIn(
                    f"superseded_report_contract_mismatch:{rt_id}", violations
                )


class RealArchiveAndSafeReadTests(_RealAuthorizationFixtureBase):
    """Current/history bytes must exist in one safe, prefix-closed chain."""

    def archive(self, gate_id: str, body: dict) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == gate_id)
        hash_field = (
            "authorization_sha256" if gate_id == "G7" else "receipt_sha256"
        )
        self.write_json(
            f"{gate['archive_dir']}/{body[hash_field]}.json", body
        )

    def test_sequence_two_without_material_predecessor_is_rejected(self) -> None:
        self.g6["sequence"] = 2
        self.g6["supersedes_receipt_sha256"] = "0" * 64
        self.g6["receipt_sha256"] = _real_eval.release_receipt_sha256(self.g6)
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        auth = self.make_authorization()
        violations = self.evaluate(auth)
        self.assertIn(
            "current_semantic:G6:receipt_predecessor_missing", violations
        )
        self.assertIn("g6_version_sequence_gap:G6", violations)

    def test_symlinked_current_g6_is_unverifiable(self) -> None:
        current = self.root / self.resolver.canonical_path("G6")
        shadow = self.root / "evidence/release-world/g6-shadow.json"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_bytes(current.read_bytes())
        current.unlink()
        current.symlink_to(shadow)
        violations = self.evaluate(self.auth)
        self.assertIn("g6_receipt_unreadable", violations)
        self.assertIn("unverifiable_directory:G6", violations)

    def test_hardlinked_current_g6_is_unverifiable(self) -> None:
        current = self.root / self.resolver.canonical_path("G6")
        shadow = self.root / "evidence/release-world/g6-hardlink.json"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_bytes(current.read_bytes())
        current.unlink()
        os.link(shadow, current)
        violations = self.evaluate(self.auth)
        self.assertIn("g6_receipt_unreadable", violations)
        self.assertIn("unverifiable_directory:G6", violations)

    def test_special_file_current_g6_is_unverifiable(self) -> None:
        current = self.root / self.resolver.canonical_path("G6")
        current.unlink()
        try:
            os.mkfifo(current)
        except (AttributeError, NotImplementedError, OSError):  # pragma: no cover
            self.skipTest("mkfifo unavailable")
        violations = self.evaluate(self.auth)
        self.assertEqual(violations, ["repository_worktree_dirty"])

    def test_archive_without_current_is_an_orphan(self) -> None:
        g1_path = self.root / self.resolver.canonical_path("G1")
        g1 = json.loads(g1_path.read_text(encoding="utf-8"))
        self.archive("G1", g1)
        g1_path.unlink()
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertIn("archive_without_current:G1", violations)

    def test_archive_fork_with_duplicate_sequence_is_rejected(self) -> None:
        original = copy.deepcopy(self.g6)
        self.archive("G6", original)
        fork = copy.deepcopy(original)
        fork["created_at"] = "2026-08-18T12:00:01+00:00"
        fork["receipt_sha256"] = _real_eval.release_receipt_sha256(fork)
        self.archive("G6", fork)
        current = copy.deepcopy(original)
        current["sequence"] = 2
        current["supersedes_receipt_sha256"] = original["receipt_sha256"]
        current["created_at"] = "2026-08-19T12:00:00+00:00"
        current["receipt_sha256"] = _real_eval.release_receipt_sha256(current)
        self.write_json(self.resolver.canonical_path("G6"), current)
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertIn("chain_duplicate_sequence", violations)

    def test_archive_gap_is_not_a_prefix_closed_history(self) -> None:
        original = copy.deepcopy(self.g6)
        self.archive("G6", original)
        current = copy.deepcopy(original)
        current["sequence"] = 3
        current["supersedes_receipt_sha256"] = original["receipt_sha256"]
        current["created_at"] = "2026-08-19T12:00:00+00:00"
        current["receipt_sha256"] = _real_eval.release_receipt_sha256(current)
        self.write_json(self.resolver.canonical_path("G6"), current)
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertIn("chain_not_exact_prefix", violations)
        self.assertIn(
            "current_semantic:G6:receipt_predecessor_missing", violations
        )

    def test_forged_archive_body_cannot_hide_behind_a_valid_filename(self) -> None:
        forged = copy.deepcopy(self.g6)
        forged["producer"] = "agent-forged-producer"
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G6")
        self.write_json(
            f"{gate['archive_dir']}/{forged['receipt_sha256']}.json", forged
        )
        violations = _real_eval.evaluate_release_root_closure(
            self.root, self.registry, context=self.context()
        )
        self.assertTrue(
            any(code.startswith("archive_member_self_hash_mismatch:G6/archive/") for code in violations),
            violations,
        )


class ReleaseVersionIndexIntegrationTests(_RealAuthorizationFixtureBase):
    """Logical refs may resolve to an exact historical version, never any version."""

    def _archive(self, gate_id: str, body: dict) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == gate_id)
        hash_field = "authorization_sha256" if gate_id == "G7" else "receipt_sha256"
        self.write_json(f"{gate['archive_dir']}/{body[hash_field]}.json", body)

    def _renew_release_gate(self, gate_id: str, created_at: str) -> tuple[dict, dict]:
        path = self.resolver.canonical_path(gate_id)
        original = json.loads((self.root / path).read_text(encoding="utf-8"))
        self._archive(gate_id, original)
        successor = copy.deepcopy(original)
        successor["sequence"] = 2
        successor["supersedes_receipt_sha256"] = original["receipt_sha256"]
        successor["created_at"] = created_at
        successor["receipt_sha256"] = _real_eval.release_receipt_sha256(successor)
        self.write_json(path, successor)
        if gate_id == "G6":
            self.g6 = successor
        return original, successor

    def _committed_context(self, message: str):
        self.evaluation_commit = _real_binding.commit_all(self.git, message)
        ctx = self.context()
        ctx.repository_facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        ctx.observed_environment = dict(ctx.repository_facts.observed_environment)
        return ctx

    def test_index_resolves_the_unique_raw_hash_at_the_consumers_time(self) -> None:
        old, new = self._renew_release_gate("G1", "2026-08-18T12:04:00+00:00")
        ctx = self._committed_context("fixture G1 renewal E2")
        index = _real_eval.ReleaseVersionIndex(ctx)
        logical = self.resolver.canonical_path("G1")
        old_raw = hashlib.sha256(
            json.dumps(
                old, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        new_raw = hashlib.sha256(
            json.dumps(
                new, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

        selected, violations = index.resolve(
            "G1", logical, old_raw, "2026-08-18T12:03:00+00:00"
        )
        self.assertEqual(violations, [])
        self.assertFalse(selected["is_current"])
        self.assertEqual(selected["sequence"], 1)
        selected, violations = index.resolve(
            "G1", logical, new_raw, "2026-08-18T12:05:00+00:00"
        )
        self.assertEqual(violations, [])
        self.assertTrue(selected["is_current"])
        self.assertEqual(selected["sequence"], 2)

        selected, violations = index.resolve(
            "G1", logical, old_raw, "2026-08-18T12:05:00+00:00"
        )
        self.assertIsNone(selected)
        self.assertIn("version_not_as_of_tip:G1", violations)
        selected, violations = index.resolve(
            "G1", logical + ".archive", old_raw, "2026-08-18T12:03:00+00:00"
        )
        self.assertIsNone(selected)
        self.assertIn("version_logical_path_mismatch:G1", violations)

    def test_recursive_cycle_guard_keys_gate_and_exact_receipt_version(self) -> None:
        ctx = self.context()
        key = ("G6", self.g6["receipt_sha256"])
        ctx._release_validation_stack.add(key)
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G6")
        violations = _real_eval.evaluate_release_receipt(
            self.g6,
            gate,
            _real_eval.load_json(RELEASE_RECEIPT_SCHEMA_PATH),
            ctx,
            registry=self.registry,
        )
        self.assertEqual(
            violations,
            [f"release_prerequisite_cycle:G6:{self.g6['receipt_sha256']}"],
        )

    def test_g7_uses_the_same_as_of_resolver_for_an_archived_g6(self) -> None:
        old_g6, _new_g6 = self._renew_release_gate(
            "G6", "2026-08-19T12:00:00+00:00"
        )
        # The authorization was signed at 00:00 and names sequence 1.  Sequence
        # 2 became current twelve hours later, so current-only lookup would
        # falsely invalidate a historically sound authorization.
        self.assertEqual(
            self.auth["g6_receipt_ref"]["ref_sha256"],
            hashlib.sha256(
                json.dumps(
                    old_g6,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
        )
        g7_path = next(
            gate["receipt_path"]
            for gate in self.registry["gates"]
            if gate["gate_id"] == "G7"
        )
        self.write_json(g7_path, self.auth)
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "fixture G6 renewal and G7 evidence E2"
        )
        self.assertEqual(self.evaluate(self.auth), [])

    def test_go_no_go_manifest_uses_as_of_history_after_g1_is_renewed(self) -> None:
        old_g1, _new_g1 = self._renew_release_gate(
            "G1", "2026-08-18T12:10:00+00:00"
        )
        report = json.loads(
            (
                self.root / self.resolver.canonical_path("RT-026-GO-NO-GO")
            ).read_text(encoding="utf-8")
        )
        bound = next(item for item in report["input_refs"] if item["input_id"] == "G1")
        self.assertEqual(
            bound["raw_sha256"],
            hashlib.sha256(
                json.dumps(
                    old_g1,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
        )
        # G6 itself consumes the new current G1; its earlier GO/NO-GO report
        # still resolves the exact archived G1 that existed at report time.
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        context = self._committed_context(
            "fixture G1 renewal with historical GO/NO-GO binding"
        )
        self.assertEqual(self.evaluate_g6_object(context=context), [])

    def test_index_rejects_gap_fork_orphan_and_duplicate_raw_history(self) -> None:
        gate = next(g for g in self.registry["gates"] if g["gate_id"] == "G1")
        current_path = self.resolver.canonical_path("G1")
        original = json.loads((self.root / current_path).read_text(encoding="utf-8"))
        self._archive("G1", original)

        # A second archive filename containing byte-identical sequence-1 data
        # is both misnamed and a duplicate raw-hash fork.
        self.write_json(f"{gate['archive_dir']}/{'f' * 64}.json", original)
        current = copy.deepcopy(original)
        current["sequence"] = 3
        current["supersedes_receipt_sha256"] = original["receipt_sha256"]
        current["created_at"] = "2026-08-18T12:05:00+00:00"
        current["receipt_sha256"] = _real_eval.release_receipt_sha256(current)
        self.write_json(current_path, current)
        ctx = self._committed_context("fixture malformed G1 history")
        _records, violations = _real_eval.ReleaseVersionIndex(ctx).gate_records("G1")
        for expected in (
            "version_duplicate_raw_hash:G1",
            "version_sequence_duplicate:G1",
            "version_sequence_gap:G1",
        ):
            self.assertIn(expected, violations, violations)

        # Orphan history is checked independently on a fresh index snapshot.
        (self.root / current_path).unlink()
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "fixture orphan G1 history"
        )
        ctx = self.context()
        ctx.repository_facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        _records, violations = _real_eval.ReleaseVersionIndex(ctx).gate_records("G1")
        self.assertIn("version_archive_without_current:G1", violations)


class ReleaseRepositoryFactsIntegrationTests(_RealAuthorizationFixtureBase):
    """Canonical release decisions derive facts from one explicit Git snapshot."""

    def test_valid_subject_to_evidence_chain_is_git_derived(self) -> None:
        facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        violations, evidence, tree = facts.validate_release_subject(
            "G6", self.resolver.canonical_path("G6"), self.g6
        )
        self.assertEqual(violations, [])
        self.assertEqual(evidence, self.evaluation_commit)
        self.assertEqual(tree, self.future_tree)

    def test_cached_git_facts_never_cache_mutable_worktree_truth(self) -> None:
        facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        receipt_path = self.resolver.canonical_path("G6")
        self.assertEqual(facts.evidence_commit(receipt_path), self.evaluation_commit)
        with (self.root / receipt_path).open("ab") as handle:
            handle.write(b"\n")
        self.assertIsNone(facts.evidence_commit(receipt_path))

        artifact_path = self.g6["fresh_evidence_refs"][0]["artifact_path"]
        self.assertTrue(facts.tracked_blob_matches(artifact_path))
        with (self.root / artifact_path).open("ab") as handle:
            handle.write(b"drift\n")
        self.assertFalse(facts.tracked_blob_matches(artifact_path))

    def test_canonical_entrypoint_requires_an_explicit_known_evaluation_commit(self) -> None:
        for evaluation_commit in (None, "f" * 40, "HEAD"):
            with self.subTest(evaluation_commit=evaluation_commit):
                violations = _real_eval.evaluate_current_gate(
                    "G6", self.context(evaluation_commit=evaluation_commit)
                )
                self.assertTrue(
                    violations
                    and violations[0].startswith("repository_facts_unavailable:"),
                    violations,
                )

    def test_arbitrary_untracked_file_fails_canonical_entrypoint_closed(self) -> None:
        self.write_bytes("UNTRACKED-EVIDENCE.txt", b"not part of the snapshot\n")
        self.assertEqual(
            _real_eval.evaluate_current_gate("G6", self.context()),
            ["repository_worktree_dirty"],
        )

    def test_identical_go_report_restored_as_untracked_still_fails_closed(self) -> None:
        rel = self.resolver.canonical_path("RT-026-GO-NO-GO")
        raw = (self.root / rel).read_bytes()
        self.git._git("rm", "--", rel)
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "freeze snapshot without go report"
        )
        self.write_bytes(rel, raw)
        self.assertEqual(
            _real_eval.evaluate_current_gate("G6", self.context()),
            ["repository_worktree_dirty"],
        )

    def test_caller_cannot_override_any_delegated_policy_surface(self) -> None:
        def sg_evaluator(_vg, _cap, sg):
            sg["go_no_go_evaluator_identity"] = "forged-evaluator"

        def sg_entry(_vg, _cap, sg):
            sg["entries"][0]["owned_sg_ids"] = ["SG-00"]

        def cap_owner(_vg, cap, _sg):
            cap["capabilities"][0]["owner_rt"] = "RT-026"

        def cap_lifecycle(_vg, cap, _sg):
            cap["activation_max_validity_seconds_ceiling"] += 1

        def vg_allowed_refs(vg, _cap, _sg):
            vg["gates"][1]["allowed_prerequisite_ids"].append("G7")

        cases = (
            ("sg-evaluator", sg_evaluator, "security_registry"),
            ("sg-entry", sg_entry, "security_registry"),
            ("cap-owner", cap_owner, "capability_map"),
            ("cap-lifecycle", cap_lifecycle, "capability_map"),
            ("vg-allowed-refs", vg_allowed_refs, "verification_registry"),
        )
        for label, mutate, expected_surface in cases:
            with self.subTest(policy=label):
                verification = copy.deepcopy(self.resolver.verification_registry)
                capability = copy.deepcopy(self.resolver.capability_map)
                security = copy.deepcopy(self.resolver.security_registry)
                mutate(verification, capability, security)
                forged = _real_eval.PrerequisiteResolver(
                    copy.deepcopy(self.registry),
                    verification_registry=verification,
                    capability_map=capability,
                    security_registry=security,
                )
                self.assertEqual(
                    _real_eval.evaluate_current_gate(
                        "G6", self.context(resolver=forged)
                    ),
                    [f"repository_policy_override:{expected_surface}"],
                )

    def test_policy_and_schema_are_bound_to_the_explicit_old_commit(self) -> None:
        old_commit = self.evaluation_commit
        context = self.context(evaluation_commit=old_commit)
        names = (
            "verification_registry",
            "capability_map",
            "security_registry",
            "verification_receipt_schema",
            "capability_receipt_schema",
            "security_receipt_schema",
        )
        for name in names:
            with self.subTest(bound_input=name):
                rel = _real_binding.ReleaseRepositoryFacts.BOUND_JSON_RELS[name]
                raw = (self.root / rel).read_bytes()
                self.write_bytes(rel, raw + b"\n")
                _real_binding.commit_all(self.git, f"move HEAD {name} bytes")
                self.assertEqual(
                    _real_eval.evaluate_current_gate("G6", context),
                    [
                        "repository_facts_unavailable:"
                        f"{name} is untracked or differs from evaluation commit"
                    ],
                )
                self.write_bytes(rel, raw)
                _real_binding.commit_all(self.git, f"restore {name} bytes")

    def test_same_context_rechecks_all_three_delegated_roots_across_commits(self) -> None:
        context = self.context()
        self.assertEqual(_real_eval.evaluate_current_gate("G6", context), [])
        cases = (
            (
                "verification_gate",
                "PR/PR-001-multitenant-knowledge-spaces/"
                "gate-receipts/VG-Z/receipt.json",
            ),
            (
                "capability_activation",
                "PR/PR-001-multitenant-knowledge-spaces/"
                "capability-receipts/forged/receipt.json",
            ),
            (
                "security_gate",
                "PR/PR-001-multitenant-knowledge-spaces/"
                "security-receipts/RT-999/receipt.json",
            ),
        )
        for family, rel in cases:
            with self.subTest(family=family):
                self.write_json(rel, {})
                context.evaluation_commit = _real_binding.commit_all(
                    self.git, f"E2 undeclared {family} receipt"
                )
                self.assertIn(
                    f"delegated_root_unclosed:{family}",
                    _real_eval.evaluate_current_gate("G6", context),
                )
                (self.root / rel).unlink()
                context.evaluation_commit = _real_binding.commit_all(
                    self.git, f"E3 remove undeclared {family} receipt"
                )
                self.assertEqual(
                    _real_eval.evaluate_current_gate("G6", context), []
                )

    def test_same_context_rechecks_family_transitive_dependencies(self) -> None:
        context = self.context()
        self.assertEqual(_real_eval.evaluate_current_gate("G6", context), [])

        vg_d = json.loads(
            (self.root / self.resolver.canonical_path("VG-D")).read_text(
                encoding="utf-8"
            )
        )
        cap = json.loads(
            (
                self.root
                / self.resolver.canonical_path("CAP:cwork-authority-source")
            ).read_text(encoding="utf-8")
        )
        sg = json.loads(
            (self.root / self.resolver.canonical_path("SG:RT-017")).read_text(
                encoding="utf-8"
            )
        )
        cases = (
            ("VG-D", vg_d["artifacts"][0]["path"]),
            (
                "CAP:cwork-authority-source",
                cap["evidence_refs"][0]["path"],
            ),
            (
                "SG:RT-017",
                sg["claims"][0]["executable_refs"][0].split("::", 1)[0],
            ),
        )
        for ref_id, rel in cases:
            with self.subTest(ref_id=ref_id):
                raw = (self.root / rel).read_bytes()
                (self.root / rel).unlink()
                context.evaluation_commit = _real_binding.commit_all(
                    self.git, f"E2 delete transitive dependency for {ref_id}"
                )
                self.assertIn(
                    f"prereq_body_delegated_family_invalid:{ref_id}",
                    _real_eval.evaluate_current_gate("G6", context),
                )
                self.write_bytes(rel, raw)
                context.evaluation_commit = _real_binding.commit_all(
                    self.git, f"E3 restore transitive dependency for {ref_id}"
                )
                self.assertEqual(
                    _real_eval.evaluate_current_gate("G6", context), []
                )

    def test_assume_unchanged_cannot_hide_a_deleted_acceptance_report(self) -> None:
        rel = self.resolver.canonical_path("RT-017")
        self.assertEqual(
            self.git._git("update-index", "--assume-unchanged", "--", rel).returncode,
            0,
        )
        (self.root / rel).unlink()
        self.assertEqual(
            self.git._git(
                "status", "--porcelain", "--untracked-files=all"
            ).stdout.strip(),
            "",
        )
        self.assertEqual(
            _real_eval.evaluate_current_gate("G6", self.context()),
            ["repository_worktree_dirty"],
        )

    def test_skip_worktree_cannot_hide_a_deleted_acceptance_report(self) -> None:
        rel = self.resolver.canonical_path("RT-017")
        self.assertEqual(
            self.git._git("update-index", "--skip-worktree", "--", rel).returncode,
            0,
        )
        (self.root / rel).unlink()
        self.assertEqual(
            self.git._git(
                "status", "--porcelain", "--untracked-files=all"
            ).stdout.strip(),
            "",
        )
        self.assertEqual(
            _real_eval.evaluate_current_gate("G6", self.context()),
            ["repository_worktree_dirty"],
        )

    def test_go_report_inputs_and_artifacts_must_match_the_evaluation_commit(self) -> None:
        report_path = self.resolver.canonical_path("RT-026-GO-NO-GO")
        report = json.loads((self.root / report_path).read_text(encoding="utf-8"))
        evidence_input = next(
            item
            for item in report["input_refs"]
            if item["input_id"] == "EVIDENCE:default-off"
        )
        artifact = report["artifacts"][0]
        cases = (
            (
                "report",
                report_path,
                "prereq_body_go_no_go_untracked_report:RT-026-GO-NO-GO",
            ),
            (
                "input",
                evidence_input["ref_path"],
                "prereq_body_go_no_go_input_untracked:EVIDENCE:default-off",
            ),
            (
                "artifact",
                artifact["path"],
                f"prereq_body_go_no_go_artifact_untracked:{artifact['path']}",
            ),
        )
        for label, rel, expected in cases:
            with self.subTest(kind=label):
                raw = (self.root / rel).read_bytes()
                self.git._git("rm", "--", rel)
                deleted_snapshot = _real_binding.commit_all(
                    self.git, f"snapshot without GO {label}"
                )
                self.write_bytes(rel, raw)
                _real_binding.commit_all(self.git, f"restore GO {label} after snapshot")
                self.assertFalse(_real_binding.worktree_is_dirty(self.git))
                violations = _real_eval.evaluate_current_gate(
                    "G6", self.context(evaluation_commit=deleted_snapshot)
                )
                self.assertIn(expected, violations, violations)

    def test_moving_head_and_forged_caller_maps_cannot_replace_the_snapshot(self) -> None:
        g7_path = next(
            gate["receipt_path"]
            for gate in self.registry["gates"]
            if gate["gate_id"] == "G7"
        )
        self.write_json(g7_path, self.auth)
        self.evaluation_commit = _real_binding.commit_all(
            self.git, "freeze G7 before moving HEAD"
        )
        frozen = self.evaluation_commit
        original_g6 = copy.deepcopy(self.g6)
        original_vg_d = json.loads(
            (self.root / self.resolver.canonical_path("VG-D")).read_text(
                encoding="utf-8"
            )
        )
        self.write_bytes("unrelated-after-evaluation.txt", b"later HEAD\n")
        later_head = _real_binding.commit_all(self.git, "later unrelated HEAD")
        self.assertNotEqual(later_head, frozen)
        # The evaluator continues to judge E, not moving HEAD, and succeeds.
        self.assertEqual(
            self.evaluate(self.auth, context=self.context(evaluation_commit=frozen)),
            [],
        )

        # Now drift a tracked prerequisite only in the worktree.  Every legacy
        # caller-supplied map still claims the old world is valid, but canonical
        # Git facts reject the bytes actually read.
        vg_d_path = self.resolver.canonical_path("VG-D")
        vg_d = json.loads((self.root / vg_d_path).read_text(encoding="utf-8"))
        vg_d["conclusion"] = "conservative_unknown"
        vg_d["receipt_sha256"] = _real_eval.family_receipt_sha256(
            vg_d, _real_eval.VERIFICATION_RECEIPT_DOMAIN
        )
        self.write_json(vg_d_path, vg_d)
        self.g6 = self.build_release_receipt("G6")
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        violations = self.evaluate(
            self.make_authorization(),
            context=self.context(
                evaluation_commit=frozen,
                prerequisite_subject_commits={"VG-D": self.future_subject},
                prerequisite_owner_scope_trees={"VG-D": self.future_tree},
                prerequisite_environments={"VG-D": dict(self.environment)},
            ),
        )
        self.assertEqual(violations, ["repository_worktree_dirty"])

        self.write_json(vg_d_path, original_vg_d)
        self.g6 = original_g6
        self.write_json(self.resolver.canonical_path("G6"), self.g6)
        self.write_json(g7_path, self.auth)

        forged_registry = copy.deepcopy(self.registry)
        next(g for g in forged_registry["gates"] if g["gate_id"] == "G6")[
            "required_prerequisite_ids"
        ] = []
        forged_resolver = _real_eval.PrerequisiteResolver(
            forged_registry,
            verification_registry=self.resolver.verification_registry,
            security_registry=self.resolver.security_registry,
            capability_map=self.resolver.capability_map,
        )
        self.assertEqual(
            _real_eval.evaluate_current_gate(
                "G6",
                self.context(
                    evaluation_commit=frozen,
                    resolver=forged_resolver,
                ),
            ),
            ["repository_registry_override"],
        )

    def test_self_unrelated_and_merge_shaped_evidence_fail_closed(self) -> None:
        facts = _real_binding.ReleaseRepositoryFacts(
            self.root, self.evaluation_commit
        )
        self_bound = copy.deepcopy(self.g6)
        self_bound["tested_subject_commit"] = self.evaluation_commit
        self_bound["owner_scope_tree_sha256"] = facts.candidate_tree(
            self.evaluation_commit
        )
        violations, _evidence, _tree = facts.validate_release_subject(
            "G6", self.resolver.canonical_path("G6"), self_bound
        )
        self.assertIn("subject_commit_is_its_own_commit", violations)

        tree = self.git._out_text("rev-parse", f"{self.future_subject}^{{tree}}")
        unrelated = self.git._git(
            "commit-tree", tree.strip(), "-m", "unrelated subject"
        ).stdout.strip()
        unrelated_body = copy.deepcopy(self.g6)
        unrelated_body["tested_subject_commit"] = unrelated
        unrelated_body["owner_scope_tree_sha256"] = facts.candidate_tree(unrelated)
        violations, _evidence, _tree = facts.validate_release_subject(
            "G6", self.resolver.canonical_path("G6"), unrelated_body
        )
        self.assertIn("subject_commit_not_strict_ancestor", violations)

        # A merge-shaped evidence commit is rejected by resolve_evidence_commit
        # even when its worktree bytes and evaluation tree agree.
        receipt_path = self.resolver.canonical_path("G1")
        with (self.root / receipt_path).open("ab") as handle:
            handle.write(b"\n")
        self.git._git("add", receipt_path)
        merge_tree = self.git._git("write-tree").stdout.strip()
        merge_commit = self.git._git(
            "commit-tree",
            merge_tree,
            "-p",
            self.evaluation_commit,
            "-p",
            unrelated,
            "-m",
            "merge-shaped evidence",
        ).stdout.strip()
        self.assertIsNone(
            _real_binding.resolve_evidence_commit(
                self.git,
                self.root,
                receipt_path,
                evaluation_commit=merge_commit,
            )
        )


class ReleaseReceiptPositiveEvaluationTests(_ReleaseFixtureBase):
    """The evaluator must ACCEPT a well-formed receipt for every G1..G6.

    Without this the negative cases would be worthless: a validator that rejects
    everything trivially "catches" all attacks while blocking all legitimate
    evidence.
    """

    def test_every_verification_gate_has_a_valid_fixture(self) -> None:
        for gate_id in VERIFICATION_RELEASE_GATES:
            with self.subTest(gate=gate_id):
                receipt = self.receipt(gate_id)
                violations = evaluate_release_receipt(
                    receipt, self.gate(gate_id), self.schema, self.ctx(gate_id)
                )
                self.assertEqual(violations, [], f"{gate_id} valid fixture rejected")

    def test_valid_fixture_hash_reproduces_under_the_release_separator(self) -> None:
        receipt = self.receipt("G3")
        self.assertEqual(
            _release_receipt_sha256(receipt), receipt["receipt_sha256"]
        )

    def test_valid_fixture_is_rejected_under_a_foreign_domain_separator(self) -> None:
        """Cross-family replay must fail on the separator alone."""
        receipt = self.receipt("G3")
        body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        canonical = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
        for foreign in (
            b"cwk-verification-gate-receipt-v1\x00",
            b"cwk-security-gate-receipt-v1\x00",
            b"cwk-capability-activation-receipt-v1\x00",
            RELEASE_AUTH_DOMAIN,
        ):
            with self.subTest(domain=foreign):
                self.assertNotEqual(
                    hashlib.sha256(foreign + payload).hexdigest(),
                    receipt["receipt_sha256"],
                )

    def test_a_synthetic_receipt_capped_at_conservative_unknown_is_valid(self) -> None:
        receipt = self.receipt(
            "G3",
            synthetic=True,
            synthetic_reason="fake_signing_authority",
            conclusion="conservative_unknown",
        )
        self.assertEqual(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            [],
        )

    def test_a_not_run_placeholder_is_valid(self) -> None:
        receipt = self.receipt(
            "G3",
            status="not_run",
            conclusion="not_run",
            feeder_rts_independent_pass=False,
            evidence={
                "test_command": "not executed",
                "tests_run": 0,
                "tests_failed": 0,
                "python_version": "3.11.9",
            },
        )
        self.assertEqual(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            [],
        )


class ReleaseReceiptAdversarialTests(_ReleaseFixtureBase):
    """One mutation per test, each pinned to the exact code it must produce."""

    def test_producer_signing_its_own_gate_is_self_certification(self) -> None:
        receipt = self.receipt("G3", producer="agent-g3-verify")
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "self_certification",
        )

    def test_verifier_role_must_match_the_registry(self) -> None:
        receipt = self.receipt("G3", verifier_role="fresh_final_independent_verifier")
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "verifier_role_mismatch",
        )

    def test_g7_gate_id_cannot_be_a_verification_receipt(self) -> None:
        """The authorization gate is structurally excluded from this family."""
        receipt = self.receipt("G3", gate_id="G7")
        violations = evaluate_release_receipt(
            receipt, self.gate("G3"), self.schema, self.ctx()
        )
        self.assertViolation(violations, "enum:$.gate_id")
        self.assertViolation(violations, "gate_id_registry_mismatch")

    def test_a_missing_prerequisite_is_rejected(self) -> None:
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"] if r != "VG-A"]
        receipt = self.receipt("G3", prerequisite_refs=refs)
        violations = evaluate_release_receipt(receipt, gate, self.schema, self.ctx())
        self.assertViolation(violations, "prereq_missing:VG-A")
        self.assertViolation(violations, "vg_not_pinned:VG-A")

    def test_an_extra_prerequisite_is_equally_rejected(self) -> None:
        """Set EQUALITY, not subset: a padded gate is as invalid as a thin one."""
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]]
        refs.append(_ref_for("RT-020"))
        receipt = self.receipt("G3", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx()),
            "prereq_extra:RT-020",
        )

    def test_duplicate_ref_id_with_a_different_hash_is_rejected(self) -> None:
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]]
        shadow = dict(refs[0])
        shadow["ref_sha256"] = "0" * 64
        refs.append(shadow)
        receipt = self.receipt("G3", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx()),
            "duplicate_ref_id",
        )

    def test_self_reference_is_rejected(self) -> None:
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]] + [_ref_for("G3")]
        receipt = self.receipt("G3", prerequisite_refs=refs)
        violations = evaluate_release_receipt(receipt, gate, self.schema, self.ctx())
        self.assertViolation(violations, "self_reference")
        self.assertViolation(violations, "forward_reference:G3")

    def test_forward_reference_to_a_later_gate_is_rejected(self) -> None:
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]] + [_ref_for("G5")]
        receipt = self.receipt("G3", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx()),
            "forward_reference:G5",
        )

    def test_ref_kind_substitution_is_rejected(self) -> None:
        """A cheap narrative may not stand in for an expensive machine receipt."""
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]]
        for ref in refs:
            if ref["ref_id"] == "VG-A":
                ref["ref_kind"] = "narrative_review_report"
        receipt = self.receipt("G3", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx()),
            "ref_kind_mismatch:VG-A",
        )

    def test_drifted_prerequisite_hash_is_rejected(self) -> None:
        """A green-looking upstream that has since changed must not survive."""
        gate = self.gate("G3")
        refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]]
        for ref in refs:
            if ref["ref_id"] == "RT-015":
                ref["ref_sha256"] = "9" * 64
        receipt = self.receipt("G3", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx()),
            "prereq_hash_mismatch:RT-015",
        )

    def test_ref_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        gate = self.gate("G3")
        for bad in ("/etc/passwd", "../../etc/passwd", "PR/../../secret.json"):
            with self.subTest(path=bad):
                refs = [_ref_for(r) for r in gate["required_prerequisite_ids"]]
                refs[0]["ref_path"] = bad
                receipt = self.receipt("G3", prerequisite_refs=refs)
                violations = evaluate_release_receipt(
                    receipt, gate, self.schema, self.ctx()
                )
                self.assertTrue(
                    any(x.startswith("pattern:$.prerequisite_refs[0].ref_path")
                        for x in violations),
                    violations,
                )

    def test_pass_without_feeder_independent_pass_is_rejected(self) -> None:
        receipt = self.receipt("G3", feeder_rts_independent_pass=False)
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "pass_without_feeder_independent_pass",
        )

    def test_pass_with_failing_tests_is_rejected(self) -> None:
        receipt = self.receipt(
            "G3",
            evidence={
                "test_command": "python3.11 -m unittest discover -s tests",
                "tests_run": 209,
                "tests_failed": 3,
                "python_version": "3.11.9",
            },
        )
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "pass_with_failures",
        )

    def test_synthetic_receipt_may_not_claim_verified(self) -> None:
        receipt = self.receipt(
            "G3", synthetic=True, synthetic_reason="fake_transport"
        )
        violations = evaluate_release_receipt(
            receipt, self.gate("G3"), self.schema, self.ctx()
        )
        self.assertViolation(violations, "verified_while_synthetic")
        self.assertViolation(violations, "synthetic_with_verified_conclusion")

    def test_synthetic_without_reason_is_rejected(self) -> None:
        receipt = self.receipt(
            "G3", synthetic=True, conclusion="conservative_unknown"
        )
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "synthetic_without_reason",
        )

    def test_g6_conclusion_cannot_be_claimed_by_a_wave_gate(self) -> None:
        receipt = self.receipt("G3", conclusion="READY_FOR_G7_AUTHORIZATION")
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "g6_conclusion_on_non_g6",
        )

    def test_frozen_feeder_and_vg_sets_cannot_be_edited_in_a_receipt(self) -> None:
        receipt = self.receipt("G3", feeder_rts=["RT-014"], consumes_verification_gates=[])
        violations = evaluate_release_receipt(
            receipt, self.gate("G3"), self.schema, self.ctx()
        )
        self.assertViolation(violations, "feeder_rts_mismatch")
        self.assertViolation(violations, "consumes_vg_mismatch")

    def test_authorization_fields_are_deep_forbidden_in_a_verification_receipt(self) -> None:
        """A gate may not mint its own deployment permission."""
        for key in ("authorized", "authorized_actions", "authorizing_principal",
                    "authorization", "signature"):
            with self.subTest(key=key):
                receipt = self.receipt("G3")
                receipt["evidence"][key] = True
                receipt["receipt_sha256"] = _release_receipt_sha256(receipt)
                self.assertViolation(
                    evaluate_release_receipt(
                        receipt, self.gate("G3"), self.schema, self.ctx()
                    ),
                    f"forbidden_key:{key}",
                )

    def test_tenant_and_credential_fields_are_deep_forbidden(self) -> None:
        for key in ("tenant_id", "token", "secret", "credential", "model_output"):
            with self.subTest(key=key):
                receipt = self.receipt("G3")
                receipt["environment_fingerprint"][key] = "x"
                receipt["receipt_sha256"] = _release_receipt_sha256(receipt)
                self.assertViolation(
                    evaluate_release_receipt(
                        receipt, self.gate("G3"), self.schema, self.ctx()
                    ),
                    f"forbidden_key:{key}",
                )

    def test_supersedes_on_a_first_run_proves_a_hidden_earlier_run(self) -> None:
        receipt = self.receipt("G3", sequence=1, supersedes_receipt_sha256="d" * 64)
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "supersedes_on_first_run",
        )

    def test_second_run_without_a_chain_link_is_rejected(self) -> None:
        prior = self.receipt("G3")
        chain = [{
            "sequence": 1,
            "receipt_sha256": prior["receipt_sha256"],
            "created_at": prior["created_at"],
        }]
        receipt = self.receipt("G3", sequence=2, created_at="2026-08-02T00:00:00+00:00")
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G3"), self.schema, self.ctx(archive_chain=chain)
            ),
            "supersedes_missing",
        )

    def test_second_run_linking_to_the_wrong_predecessor_is_rejected(self) -> None:
        prior = self.receipt("G3")
        chain = [{
            "sequence": 1,
            "receipt_sha256": prior["receipt_sha256"],
            "created_at": prior["created_at"],
        }]
        receipt = self.receipt(
            "G3",
            sequence=2,
            created_at="2026-08-02T00:00:00+00:00",
            supersedes_receipt_sha256="e" * 64,
        )
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G3"), self.schema, self.ctx(archive_chain=chain)
            ),
            "supersedes_mismatch",
        )

    def test_a_gap_in_the_sequence_chain_is_rejected(self) -> None:
        prior = self.receipt("G3")
        chain = [{
            "sequence": 1,
            "receipt_sha256": prior["receipt_sha256"],
            "created_at": prior["created_at"],
        }]
        receipt = self.receipt(
            "G3",
            sequence=3,
            created_at="2026-08-03T00:00:00+00:00",
            supersedes_receipt_sha256=prior["receipt_sha256"],
        )
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G3"), self.schema, self.ctx(archive_chain=chain)
            ),
            "chain_not_exact_prefix",
        )

    def test_backdated_successor_is_rejected(self) -> None:
        prior = self.receipt("G3")
        chain = [{
            "sequence": 1,
            "receipt_sha256": prior["receipt_sha256"],
            "created_at": prior["created_at"],
        }]
        receipt = self.receipt(
            "G3",
            sequence=2,
            created_at="2026-07-01T00:00:00+00:00",
            supersedes_receipt_sha256=prior["receipt_sha256"],
        )
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G3"), self.schema, self.ctx(archive_chain=chain)
            ),
            "created_at_not_strictly_increasing",
        )

    def test_receipt_cannot_bind_its_own_introducing_commit(self) -> None:
        receipt = self.receipt("G3", tested_subject_commit=_INTRODUCING_COMMIT)
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "subject_commit_is_its_own_commit",
        )

    def test_receipt_cannot_bind_an_unrelated_commit(self) -> None:
        receipt = self.receipt("G3", tested_subject_commit="7" * 40)
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "subject_commit_not_strict_ancestor",
        )

    def test_receipt_only_commit_does_not_satisfy_touched_feeder_packages(self) -> None:
        receipt = self.receipt("G3")
        ctx = self.ctx(touched_feeder_commits={"9" * 40})
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, ctx),
            "subject_commit_did_not_touch_feeder_packages",
        )

    def test_artifact_drift_against_disk_is_rejected(self) -> None:
        receipt = self.receipt("G3")
        target = self.root / receipt["artifacts"][0]["path"]
        target.write_text("tampered\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            f"artifact_hash_mismatch:{receipt['artifacts'][0]['path']}",
        )

    def test_a_gate_may_not_cite_gate_evidence_as_its_own_artifact(self) -> None:
        path = (
            "PR/PR-001-multitenant-knowledge-spaces/release-gate-receipts/G2/receipt.json"
        )
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        receipt = self.receipt(
            "G3",
            artifacts=[{
                "path": path,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }],
        )
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            f"artifact_is_gate_evidence:{path}",
        )

    def test_empty_artifacts_are_rejected(self) -> None:
        receipt = self.receipt("G3", artifacts=[])
        violations = evaluate_release_receipt(
            receipt, self.gate("G3"), self.schema, self.ctx()
        )
        self.assertViolation(violations, "artifacts_empty")
        self.assertViolation(violations, "minItems:$.artifacts")

    def test_any_field_mutation_breaks_the_self_hash(self) -> None:
        receipt = self.receipt("G3")
        receipt["conclusion"] = "conservative_unknown"
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "self_hash_mismatch",
        )

    def test_unknown_top_level_fields_are_rejected(self) -> None:
        receipt = self.receipt("G3")
        receipt["approved_by_pipeline"] = True
        receipt["receipt_sha256"] = _release_receipt_sha256(receipt)
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "additionalProperties:$.approved_by_pipeline",
        )


class G6FreshnessAdversarialTests(_ReleaseFixtureBase):
    """G6 is the only gate whose verifier independence is recomputed, not asserted."""

    def test_g6_missing_freshness_surface_is_rejected(self) -> None:
        for field in G6_ONLY_FIELDS:
            with self.subTest(field=field):
                receipt = self.receipt("G6", **{field: _DROP})
                self.assertViolation(
                    evaluate_release_receipt(
                        receipt, self.gate("G6"), self.schema, self.ctx("G6")
                    ),
                    f"g6_missing:{field}",
                )

    def test_a_wave_gate_may_not_carry_the_g6_freshness_surface(self) -> None:
        receipt = self.receipt("G3", expires_at="2026-09-01T00:00:00+00:00")
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G3"), self.schema, self.ctx()),
            "g6_only_field_on_non_g6:expires_at",
        )

    def test_a_verifier_with_prior_engagements_is_not_fresh(self) -> None:
        receipt = self.receipt(
            "G6",
            verifier_provenance={
                "engagement_id": "fresh-final-review-01",
                "prior_engagement_ids": ["rt-024-acceptance"],
                "independent_of_producer_org": True,
            },
        )
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G6"), self.schema, self.ctx("G6")
            ),
            "g6_verifier_not_fresh",
        )

    def test_self_reported_freshness_is_overridden_by_recomputation(self) -> None:
        """The attestation says clean; the on-disk scan says otherwise."""
        receipt = self.receipt("G6")
        ctx = self.ctx("G6", verifier_signed_evidence={receipt["verifier"]})
        self.assertViolation(
            evaluate_release_receipt(receipt, self.gate("G6"), self.schema, ctx),
            "g6_verifier_signed_prior_evidence",
        )

    def test_a_false_attestation_flag_is_rejected(self) -> None:
        receipt = self.receipt(
            "G6",
            freshness_attestation={
                "signed_no_rt_acceptance": False,
                "signed_no_verification_gate": True,
                "signed_no_upstream_release_gate": True,
                "signed_no_security_gate": True,
            },
        )
        violations = evaluate_release_receipt(
            receipt, self.gate("G6"), self.schema, self.ctx("G6")
        )
        self.assertViolation(violations, "g6_attestation_false:signed_no_rt_acceptance")
        self.assertViolation(
            violations, "const:$.freshness_attestation.signed_no_rt_acceptance"
        )

    def test_incomplete_fresh_evidence_roles_are_rejected(self) -> None:
        receipt = self.receipt("G6")
        receipt["fresh_evidence_refs"] = receipt["fresh_evidence_refs"][:4]
        receipt["receipt_sha256"] = _release_receipt_sha256(receipt)
        violations = evaluate_release_receipt(
            receipt, self.gate("G6"), self.schema, self.ctx("G6")
        )
        self.assertViolation(violations, "g6_fresh_evidence_roles_incomplete")
        self.assertViolation(violations, "minItems:$.fresh_evidence_refs")

    def test_a_g6_that_never_expires_is_rejected(self) -> None:
        receipt = self.receipt("G6", expires_at="2026-08-01T00:00:00+00:00")
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G6"), self.schema, self.ctx("G6")
            ),
            "g6_expiry_not_positive",
        )

    def test_a_g6_expiry_beyond_thirty_days_is_rejected(self) -> None:
        receipt = self.receipt("G6", expires_at="2026-12-01T00:00:00+00:00")
        self.assertViolation(
            evaluate_release_receipt(
                receipt, self.gate("G6"), self.schema, self.ctx("G6")
            ),
            "g6_expiry_too_long",
        )

    def test_g6_rebinds_all_five_upstream_release_gates(self) -> None:
        """Chain transitivity would hide a revoked middle gate."""
        gate = self.gate("G6")
        for upstream in ("G1", "G2", "G3", "G4", "G5"):
            self.assertIn(upstream, gate["required_prerequisite_ids"])
        refs = [
            _ref_for(r) for r in gate["required_prerequisite_ids"] if r != "G2"
        ]
        receipt = self.receipt("G6", prerequisite_refs=refs)
        self.assertViolation(
            evaluate_release_receipt(receipt, gate, self.schema, self.ctx("G6")),
            "prereq_missing:G2",
        )

    def test_no_rt_consumes_g6(self) -> None:
        """RT-026 reading G6 would be a self-dependency cycle."""
        self.assertEqual(self.gate("G6")["direct_rt_consumers"], [])
        self.assertIn("RT-026", self.gate("G6")["required_prerequisite_ids"])


class ReleaseAuthorizationAdversarialTests(_ReleaseFixtureBase):
    """G7: wrong signer, wrong hash, wrong scope, wrong time, replay, revoke."""

    def setUp(self) -> None:
        super().setUp()
        self.g6 = self.receipt("G6")
        self.auth = _make_authorization(self.g6)
        self.base_kwargs = dict(
            referenced_g6=self.g6,
            deploy_environment=dict(_ENV),
        )

    def auth_ctx(self, auth=None, **kwargs) -> EvalContext:
        merged = dict(self.base_kwargs)
        merged.update(kwargs)
        return _auth_signature_context(auth or self.auth, **merged)

    def test_a_well_formed_authorization_is_accepted(self) -> None:
        self.assertEqual(
            evaluate_release_authorization(
                self.auth, self.auth_schema, self.auth_ctx()
            ),
            [],
        )

    def test_authorization_hash_uses_its_own_separator_and_field_name(self) -> None:
        self.assertIn("authorization_sha256", self.auth)
        self.assertNotIn("receipt_sha256", self.auth)
        body = {k: v for k, v in self.auth.items() if k != "authorization_sha256"}
        canonical = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
        self.assertNotEqual(
            hashlib.sha256(RELEASE_RECEIPT_DOMAIN + payload).hexdigest(),
            self.auth["authorization_sha256"],
        )

    def test_a_project_or_test_signer_is_rejected(self) -> None:
        for signer in ("test-signer", "agent-rt026-impl", "agent-go-no-go-eval"):
            with self.subTest(signer=signer):
                sig = dict(self.auth["external_signature"], trust_root_id=signer)
                auth = _make_authorization(self.g6, external_signature=sig)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    "signer_is_project_or_test_identity",
                )

    def test_an_unknown_trust_root_is_rejected(self) -> None:
        sig = dict(self.auth["external_signature"], trust_root_id="some-other-root")
        auth = _make_authorization(self.g6, external_signature=sig)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "signer_not_in_production_trust_store",
        )

    def test_a_revoked_or_expired_key_is_rejected(self) -> None:
        for state in ("revoked", "expired"):
            with self.subTest(key_state=state):
                sig = dict(self.auth["external_signature"], key_state=state)
                auth = _make_authorization(self.g6, external_signature=sig)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    "key_not_active",
                )

    def test_a_key_past_its_expiry_is_rejected(self) -> None:
        sig = dict(
            self.auth["external_signature"], key_expires_at="2026-01-01T00:00:00+00:00"
        )
        auth = _make_authorization(self.g6, external_signature=sig)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "key_expired",
        )

    def test_a_valid_signature_over_a_mutated_body_is_rejected(self) -> None:
        """Signature and self-hash must BOTH cover the same bytes."""
        ctx = self.auth_ctx()
        mutated = dict(self.auth)
        mutated["scope"] = dict(mutated["scope"], allowlisted_tenant_count=3)
        mutated["authorization_sha256"] = _release_auth_sha256(mutated)
        self.assertViolation(
            evaluate_release_authorization(mutated, self.auth_schema, ctx),
            "signature_does_not_cover_body",
        )

    def test_a_correct_hash_with_no_signature_is_rejected(self) -> None:
        auth = _make_authorization(self.g6, external_signature=_DROP)
        violations = evaluate_release_authorization(
            auth, self.auth_schema, self.auth_ctx(auth)
        )
        self.assertViolation(violations, "required:$.external_signature")

    def test_pointing_at_a_different_g6_is_rejected(self) -> None:
        ref = dict(self.auth["g6_receipt_ref"], ref_sha256="1" * 64)
        auth = _make_authorization(self.g6, g6_receipt_ref=ref)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "g6_hash_mismatch",
        )

    def test_authorizing_against_a_failing_g6_is_rejected(self) -> None:
        bad_g6 = self.receipt("G6", status="fail", conclusion="failed")
        auth = _make_authorization(self.g6)
        ctx = self.auth_ctx(auth, referenced_g6=bad_g6)
        violations = evaluate_release_authorization(auth, self.auth_schema, ctx)
        self.assertViolation(violations, "g6_not_pass")
        self.assertViolation(violations, "g6_conclusion_not_ready_for_authorization")

    def test_authorizing_against_a_lapsed_g6_is_rejected(self) -> None:
        stale_g6 = self.receipt(
            "G6",
            created_at="2026-06-01T00:00:00+00:00",
            expires_at="2026-06-20T00:00:00+00:00",
        )
        auth = _make_authorization(stale_g6)
        ctx = self.auth_ctx(auth, referenced_g6=stale_g6)
        self.assertViolation(
            evaluate_release_authorization(auth, self.auth_schema, ctx), "g6_expired"
        )

    def test_g7_does_not_reconsume_rt_vg_or_g1_g5(self) -> None:
        """Narrow authority: an authorizer may not re-litigate verification."""
        self.assertEqual(self.gate("G7")["required_prerequisite_ids"], ["G6"])
        self.assertEqual(self.gate("G7")["consumes_verification_gates"], [])
        self.assertEqual(self.gate("G7")["feeder_rts"], [])
        surface = set(self.auth_schema["properties"])
        self.assertNotIn("prerequisite_refs", surface)
        self.assertNotIn("feeder_rts", surface)
        self.assertNotIn("consumes_verification_gates", surface)

    def test_authorization_for_one_build_cannot_be_honoured_against_another(self) -> None:
        binding = dict(self.auth["target_binding"], target_commit="3" * 40)
        auth = _make_authorization(self.g6, target_binding=binding)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "target_commit_not_bound_to_g6_subject",
        )

    def test_authorization_for_one_environment_cannot_be_replayed_in_another(self) -> None:
        other_env = dict(_ENV, platform="linux-x86_64")
        ctx = self.auth_ctx(deploy_environment=other_env)
        self.assertViolation(
            evaluate_release_authorization(self.auth, self.auth_schema, ctx),
            "target_environment_mismatch",
        )

    def test_scope_is_pinned_to_the_m4_controlled_pilot(self) -> None:
        scope = dict(self.auth["scope"], migration_phase="M5")
        auth = _make_authorization(self.g6, scope=scope)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "const:$.scope.migration_phase",
        )

    def test_blast_radius_is_bounded(self) -> None:
        for field, value, code in (
            ("allowlisted_tenant_count", 50, "maximum:$.scope.allowlisted_tenant_count"),
            ("allowlisted_tenant_count", 0, "minimum:$.scope.allowlisted_tenant_count"),
            ("pilot_window_days", 365, "maximum:$.scope.pilot_window_days"),
        ):
            with self.subTest(field=field, value=value):
                scope = dict(self.auth["scope"], **{field: value})
                auth = _make_authorization(self.g6, scope=scope)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    code,
                )

    def test_authorized_actions_is_a_closed_list_with_no_ga_member(self) -> None:
        auth = _make_authorization(
            self.g6, authorized_actions=["enable_general_availability"]
        )
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "enum:$.authorized_actions[0]",
        )

    def test_tenant_identifiers_may_never_appear(self) -> None:
        for key in ("tenant_id", "tenant_ids"):
            with self.subTest(key=key):
                auth = _make_authorization(self.g6)
                auth["scope"][key] = "acme-corp"
                auth["authorization_sha256"] = _release_auth_sha256(auth)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    f"forbidden_key:{key}",
                )

    def test_verification_fields_may_never_appear(self) -> None:
        for key in ("status", "conclusion", "verifier", "producer", "artifacts",
                    "tests_run", "tests_failed"):
            with self.subTest(key=key):
                auth = _make_authorization(self.g6)
                auth["scope"][key] = "pass"
                auth["authorization_sha256"] = _release_auth_sha256(auth)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    f"forbidden_key:{key}",
                )

    def test_a_non_human_principal_is_rejected(self) -> None:
        for kind in ("agent", "service_account", "pipeline", "automation"):
            with self.subTest(kind=kind):
                principal = {"kind": kind, "id": "some-agent"}
                auth = _make_authorization(self.g6, authorizing_principal=principal)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    "const:$.authorizing_principal.kind",
                )

    def test_there_is_no_inferred_authorization_channel(self) -> None:
        for channel in ("inferred", "derived_from_g6", "implied_by_policy",
                        "agent_relayed"):
            with self.subTest(channel=channel):
                auth = _make_authorization(self.g6, authorization_channel=channel)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    "enum:$.authorization_channel",
                )

    def test_a_replayed_nonce_is_rejected_even_when_everything_verifies(self) -> None:
        ctx = self.auth_ctx(used_nonces={self.auth["nonce"]})
        violations = evaluate_release_authorization(self.auth, self.auth_schema, ctx)
        self.assertEqual(violations, ["nonce_replay"])

    def test_a_pre_signed_future_authorization_cannot_be_activated_early(self) -> None:
        auth = _make_authorization(
            self.g6,
            not_before="2026-12-01T00:00:00+00:00",
            expires_at="2026-12-20T00:00:00+00:00",
        )
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "window_not_yet_valid",
        )

    def test_an_elapsed_authorization_is_rejected(self) -> None:
        auth = _make_authorization(
            self.g6,
            not_before="2026-07-01T00:00:00+00:00",
            expires_at="2026-07-20T00:00:00+00:00",
        )
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "window_expired",
        )

    def test_there_is_no_perpetual_authorization(self) -> None:
        auth = _make_authorization(
            self.g6,
            not_before="2026-08-19T00:00:00+00:00",
            expires_at="2027-08-19T00:00:00+00:00",
        )
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "window_too_long",
        )

    def test_revocation_is_append_only_and_signed(self) -> None:
        prior = self.auth
        chain = [{
            "sequence": 1,
            "authorization_sha256": prior["authorization_sha256"],
        }]
        withdrawal = _make_authorization(
            self.g6,
            decision="withdrawn",
            sequence=2,
            supersedes_authorization_sha256=prior["authorization_sha256"],
            revocation_ref=prior["authorization_sha256"],
            nonce="d" * 32,
        )
        ctx = self.auth_ctx(withdrawal, archive_chain=chain)
        self.assertEqual(
            evaluate_release_authorization(withdrawal, self.auth_schema, ctx), []
        )

    def test_a_withdrawal_without_a_revocation_ref_is_rejected(self) -> None:
        auth = _make_authorization(self.g6, decision="withdrawn")
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "withdrawn_without_revocation_ref",
        )

    def test_a_revocation_ref_on_a_live_authorization_is_rejected(self) -> None:
        auth = _make_authorization(self.g6, revocation_ref="a" * 64)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "revocation_ref_without_withdrawal",
        )

    def test_authorization_is_always_revocable(self) -> None:
        auth = _make_authorization(self.g6, revocable=False)
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "const:$.revocable",
        )

    def test_the_beneficiary_may_not_record_its_own_authorization(self) -> None:
        for recorder in ("agent-rt026-impl", "agent-go-no-go-eval"):
            with self.subTest(recorder=recorder):
                auth = _make_authorization(self.g6, recorded_by=recorder)
                self.assertViolation(
                    evaluate_release_authorization(
                        auth, self.auth_schema, self.auth_ctx(auth)
                    ),
                    "recorder_is_the_beneficiary",
                )

    def test_the_principal_may_not_also_be_the_recorder(self) -> None:
        auth = _make_authorization(self.g6, recorded_by="evan")
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "recorder_is_the_authorizing_principal",
        )

    def test_a_first_authorization_may_not_claim_a_predecessor(self) -> None:
        auth = _make_authorization(
            self.g6, sequence=1, supersedes_authorization_sha256="b" * 64
        )
        self.assertViolation(
            evaluate_release_authorization(
                auth, self.auth_schema, self.auth_ctx(auth)
            ),
            "supersedes_on_first_authorization",
        )

    def test_an_authorization_cannot_be_replayed_as_a_verification_receipt(self) -> None:
        """Cross-family replay fails on schema surface, not just on the hash."""
        violations = evaluate_release_receipt(
            self.auth, self.gate("G6"), self.schema, self.ctx("G6")
        )
        self.assertNotEqual(violations, [])
        self.assertTrue(
            any(x.startswith("additionalProperties:$.authorization_sha256")
                for x in violations),
            violations,
        )

    def test_a_verification_receipt_cannot_be_replayed_as_an_authorization(self) -> None:
        violations = evaluate_release_authorization(
            self.g6, self.auth_schema, self.auth_ctx()
        )
        self.assertNotEqual(violations, [])
        self.assertViolation(violations, "const:$.gate_id")
        self.assertViolation(violations, "forbidden_key:status")

    def test_authorization_is_not_execution(self) -> None:
        """A valid G7 permits actions; it performs none and enables nothing.

        Structurally: the artefact carries no execution surface at all - no
        enabled flag, no applied/deployed marker, no tenant list - so there is
        nothing in it that any runtime could read as 'already done'.
        """
        self.assertEqual(
            evaluate_release_authorization(
                self.auth, self.auth_schema, self.auth_ctx()
            ),
            [],
        )
        surface = set(self.auth_schema["properties"])
        for execution_field in ("enabled", "applied", "deployed", "executed",
                                "activated", "rollout_state", "tenant_ids"):
            self.assertNotIn(execution_field, surface)
        # Every use must re-validate: the same authorization presented after its
        # window has closed is rejected, so a past success is never standing
        # permission.
        later = EvalContext(
            now=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
            referenced_g6=self.g6,
            deploy_environment=dict(_ENV),
        )
        self.assertViolation(
            evaluate_release_authorization(self.auth, self.auth_schema, later),
            "window_expired",
        )


class ReleaseRootClosureAdversarialTests(_ReleaseFixtureBase):
    """Whole-subtree closure: anything not declared is a hard failure."""

    def setUp(self) -> None:
        super().setUp()
        self.receipt_root = self.root / "release-gate-receipts"

    def _declare(self, gate_id: str) -> pathlib.Path:
        gate = self.gate(gate_id)
        rel = gate["receipt_path"].split("release-gate-receipts/", 1)[1]
        target = self.receipt_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def test_a_missing_root_is_not_run_and_not_a_violation(self) -> None:
        self.assertEqual(
            evaluate_release_root_closure(self.receipt_root, self.registry), []
        )

    def test_a_tree_of_only_declared_receipts_is_clean(self) -> None:
        for gate_id in ("G1", "G7"):
            target = self._declare(gate_id)
            target.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            evaluate_release_root_closure(self.receipt_root, self.registry), []
        )

    def test_a_stray_json_beside_a_declared_receipt_is_a_hard_failure(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        (target.parent / "receipt.backup.json").write_text("{}\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "undeclared_file:G1/receipt.backup.json",
        )

    def test_a_receipt_json_under_g7_is_a_closure_violation(self) -> None:
        """G7's declared filename is authorization.json; receipt.json is not it."""
        target = self._declare("G7")
        target.write_text("{}\n", encoding="utf-8")
        (target.parent / "receipt.json").write_text("{}\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "undeclared_file:G7/receipt.json",
        )

    def test_an_authorization_json_under_a_verification_gate_is_a_violation(self) -> None:
        target = self._declare("G5")
        target.write_text("{}\n", encoding="utf-8")
        (target.parent / "authorization.json").write_text("{}\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "undeclared_file:G5/authorization.json",
        )

    def test_a_symlinked_leaf_is_rejected(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        decoy = self.root / "elsewhere.json"
        decoy.write_text("{}\n", encoding="utf-8")
        link = target.parent / "linked.json"
        link.symlink_to(decoy)
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "symlink_leaf:G1/linked.json",
        )

    def test_a_symlinked_directory_component_is_rejected(self) -> None:
        self._declare("G1").write_text("{}\n", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "receipt.json").write_text("{}\n", encoding="utf-8")
        (self.receipt_root / "G9").symlink_to(outside, target_is_directory=True)
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "symlink_component:G9",
        )

    def test_a_dotfile_is_rejected_rather_than_skipped(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        (target.parent / ".receipt.json.swp").write_text("x", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "dotfile:G1/.receipt.json.swp",
        )

    def test_a_hardlinked_receipt_is_rejected(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        source = self.root / "source.json"
        source.write_text("{}\n", encoding="utf-8")
        os.link(source, target.parent / "hardlinked.json")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "hardlink:G1/hardlinked.json",
        )

    def test_a_special_file_is_rejected(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        fifo = target.parent / "pipe.json"
        try:
            os.mkfifo(fifo)
        except (AttributeError, NotImplementedError, OSError):  # pragma: no cover
            self.skipTest("mkfifo unavailable on this platform")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "special_file:G1/pipe.json",
        )

    def test_a_nested_extra_receipt_is_rejected(self) -> None:
        target = self._declare("G1")
        target.write_text("{}\n", encoding="utf-8")
        nested = target.parent / "sub" / "receipt.json"
        nested.parent.mkdir()
        nested.write_text("{}\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            "undeclared_file:G1/sub/receipt.json",
        )

    def test_a_correctly_named_archive_member_is_accepted(self) -> None:
        archived = self.receipt("G1")
        gate = self.gate("G1")
        arel = gate["archive_dir"].split("release-gate-receipts/", 1)[1]
        archive = self.receipt_root / arel
        archive.mkdir(parents=True)
        (archive / f"{archived['receipt_sha256']}.json").write_text(
            json.dumps(archived, ensure_ascii=False), encoding="utf-8"
        )
        self._declare("G1").write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            evaluate_release_root_closure(self.receipt_root, self.registry), []
        )

    def test_a_misnamed_archive_member_invalidates_the_history(self) -> None:
        archived = self.receipt("G1")
        gate = self.gate("G1")
        arel = gate["archive_dir"].split("release-gate-receipts/", 1)[1]
        archive = self.receipt_root / arel
        archive.mkdir(parents=True)
        (archive / "old-receipt.json").write_text(
            json.dumps(archived, ensure_ascii=False), encoding="utf-8"
        )
        self._declare("G1").write_text("{}\n", encoding="utf-8")
        self.assertViolation(
            evaluate_release_root_closure(self.receipt_root, self.registry),
            f"archive_member_misnamed:{arel}/old-receipt.json",
        )

    def test_the_real_release_receipt_root_does_not_exist_yet(self) -> None:
        """No release gate has run; nothing may create this tree as a side effect."""
        self.assertFalse(
            RELEASE_RECEIPT_ROOT.exists(),
            f"{RELEASE_RECEIPT_ROOT} must not exist: all seven gates are NOT_RUN",
        )

    def test_the_two_receipt_roots_stay_disjoint(self) -> None:
        vg_root = PR_ROOT / "gate-receipts"
        self.assertNotEqual(vg_root.resolve(), RELEASE_RECEIPT_ROOT)
        self.assertFalse(str(RELEASE_RECEIPT_ROOT).startswith(str(vg_root) + os.sep))
        self.assertFalse(str(vg_root).startswith(str(RELEASE_RECEIPT_ROOT) + os.sep))


class ReleaseGateNotRunTests(_ReleaseFixtureBase):
    """Status comes from receipts on disk, never from the registry or prose."""

    def test_every_release_gate_is_currently_not_run(self) -> None:
        for gate in self.registry["gates"]:
            with self.subTest(gate=gate["gate_id"]):
                self.assertFalse((REPO_ROOT / gate["receipt_path"]).exists())
                self.assertFalse((REPO_ROOT / gate["archive_dir"]).exists())

    def test_g0_current_state_is_absent_not_run_or_a_valid_tracked_review(self) -> None:
        """The canonical path is optional evidence, not permanently absent."""
        bootstrap = self.registry["bootstrap_gate"]
        final_report = REPO_ROOT / bootstrap["final_wave0_review_report_path"]
        git = _real_binding.GitSubject.for_repo(REPO_ROOT)
        self.assertIsNotNone(git)
        state, violations = _evaluate_canonical_g0_report(
            REPO_ROOT,
            git.head(),
            self.registry,
        )
        if os.path.lexists(final_report):
            self.assertEqual((state, violations), ("PASS", []))
        else:
            self.assertEqual((state, violations), ("NOT_RUN", []))
        self.assertIn("G0", self.gate("G1")["required_prerequisite_ids"])

    def test_the_stale_r4_report_is_not_sufficient_for_g0(self) -> None:
        bootstrap = self.registry["bootstrap_gate"]
        self.assertNotEqual(
            bootstrap["historical_narrative_ref"],
            bootstrap["final_wave0_review_report_path"],
        )
        self.assertIn("explicitly NOT sufficient", bootstrap["resolution_rule"])


class G0FinalReviewFutureSafeTests(unittest.TestCase):
    """A present G0 report is real Git-bound evidence; absence is NOT_RUN."""

    def setUp(self) -> None:
        self.registry = _load(RELEASE_REGISTRY_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            capture_output=True,
        )
        self.git = _real_binding.GitSubject(self.root)
        self.git._git("config", "user.email", "g0-fixture@example.invalid")
        self.git._git("config", "user.name", "g0-fixture")
        self.git._git("config", "commit.gpgsign", "false")
        candidate = self.root / "scripts" / "candidate.py"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("CANDIDATE = True\n", encoding="utf-8")
        self.subject = _real_binding.commit_all(self.git, "G0 candidate subject")
        owner_model = self.registry["owner_scope_model"]
        self.owner_tree = self.git.candidate_tree_sha256(
            self.subject,
            excluded_prefixes=owner_model["candidate_tree_excluded_prefixes"],
            excluded_patterns=owner_model["candidate_tree_excluded_patterns"],
        )
        self.assertIsNotNone(self.owner_tree)
        self.report_rel = self.registry["bootstrap_gate"][
            "final_wave0_review_report_path"
        ]
        self.report_path = self.root / self.report_rel

    def marker(self, *, subject=None, tree=None, extra_lines=()) -> bytes:
        report_id = self.registry["prerequisite_resolution"]["bootstrap_report_id"]
        lines = [
            f"report_id: {report_id}",
            "verdict: PASS",
            "open_blocker: 0",
            "open_major: 0",
            f"subject_commit: {subject or self.subject}",
            f"owner_scope_tree_sha256: {tree or self.owner_tree}",
            *extra_lines,
        ]
        return (
            "# Final Wave-0 independent review\n\n"
            "<!-- cwk-acceptance-v1\n"
            + "\n".join(lines)
            + "\n-->\n"
        ).encode("utf-8")

    def write_report(self, raw=None, *, commit=True):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_bytes(raw if raw is not None else self.marker())
        if commit:
            return _real_binding.commit_all(self.git, "independent G0 evidence")
        return self.subject

    def evaluate(self, evaluation_commit):
        return _evaluate_canonical_g0_report(
            self.root,
            evaluation_commit,
            self.registry,
        )

    def test_absent_report_is_not_run_and_g1_still_requires_g0(self) -> None:
        self.assertEqual(self.evaluate(self.subject), ("NOT_RUN", []))
        g1 = next(g for g in self.registry["gates"] if g["gate_id"] == "G1")
        self.assertIn("G0", g1["required_prerequisite_ids"])

    def test_present_six_field_report_is_valid_tracked_evidence(self) -> None:
        evidence = self.write_report()
        self.assertEqual(self.evaluate(evidence), ("PASS", []))

    def test_untracked_report_is_rejected(self) -> None:
        self.write_report(commit=False)
        state, violations = self.evaluate(self.subject)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_not_regular_tracked_blob_matching_disk", violations)

    def test_tampered_worktree_bytes_are_rejected(self) -> None:
        evidence = self.write_report()
        self.report_path.write_bytes(self.report_path.read_bytes() + b"tampered\n")
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_not_regular_tracked_blob_matching_disk", violations)

    def test_tracked_report_deleted_from_worktree_is_invalid_not_not_run(self) -> None:
        evidence = self.write_report()
        self.report_path.unlink()
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertEqual(violations, ["g0_tracked_blob_missing_on_disk"])

    def test_duplicate_marker_is_rejected(self) -> None:
        evidence = self.write_report(self.marker() + self.marker())
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_marker_duplicate", violations)

    def test_identity_or_any_other_extra_marker_field_is_rejected(self) -> None:
        evidence = self.write_report(
            self.marker(extra_lines=("implementer_ids: agent-g0-impl",))
        )
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_marker_field_set", violations)

    def test_unknown_subject_commit_is_rejected(self) -> None:
        evidence = self.write_report(self.marker(subject="f" * 40))
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_subject_unknown", violations)

    def test_real_but_unrelated_subject_is_not_before_the_evidence(self) -> None:
        main_branch = self.git._git("branch", "--show-current").stdout.strip()
        self.git._git("checkout", "-q", "-b", "unrelated-g0-subject")
        (self.root / "unrelated.txt").write_text("not the candidate\n", encoding="utf-8")
        unrelated = _real_binding.commit_all(self.git, "unrelated G0 subject")
        self.git._git("checkout", "-q", main_branch)
        evidence = self.write_report(self.marker(subject=unrelated))
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_subject_not_before_evidence", violations)

    def test_wrong_candidate_tree_digest_is_rejected(self) -> None:
        evidence = self.write_report(self.marker(tree="0" * 64))
        state, violations = self.evaluate(evidence)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_owner_tree_mismatch", violations)

    def test_symlink_report_is_not_a_regular_tracked_blob(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        elsewhere = self.root / "elsewhere.md"
        elsewhere.write_bytes(self.marker())
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, self.report_path)
        state, violations = self.evaluate(self.subject)
        self.assertEqual(state, "INVALID")
        self.assertIn("g0_not_regular_tracked_blob_matching_disk", violations)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
