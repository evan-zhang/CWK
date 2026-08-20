"""Executable evaluator for the PR-001 release gate layer (G1..G7).

`tests/test_pr001_release_gate_contracts.py` asserts what the five frozen
contract files SAY. This module is what they DO, and the suites that import it
assert that a well-formed receipt is accepted and that each single-field
mutation is rejected with a specific, named violation.

What changed and why
--------------------
The previous evaluator lived inside the validation test module and was rejected
by independent review on two counts, both of which amounted to the same defect:
it checked a claim against another claim rather than against the world.

* **Prerequisites were bound by id, not by evidence.** `EvalContext` carried a
  ``{ref_id: sha256}`` dictionary supplied by the caller, so any 64-hex string
  filed under the right key satisfied the check. The evaluator never learned
  WHICH FILE an id denotes: ``G0`` could denote any document at all, and the
  superseded round-4 review would have passed as easily as the frozen final
  one. Binding is now three-part - CANONICAL PATH from the registry, RECOMPUTED
  HASH of the bytes a fail-closed reader actually returned, and VALIDATION OF
  THE BODY (family, schema id, own id, status, expiry, revocation, synthetic).

* **Signatures were "verified" against a whitelist of digests.** A caller
  passed the set of blessed digests, so the check accepted exactly what it was
  told to accept and could not tell a real signer from a test harness. G7 now
  carries a real detached ECDSA P-256 signature over an explicitly bounded
  payload, verified with :mod:`pr001_release_signing` against a public key
  taken from the TRUST STORE - never from the artefact, because an artefact may
  never nominate the key that validates it.

Everything else follows from those two: owner scope digests and environment
fingerprints are recomputed from a real git candidate rather than compared to
themselves, G6's nine fresh-evidence runs are cross-checked against a frozen
orchestration, the tenant allowlist is bound by digest rather than by count,
and the receipt root is walked through the openat/O_NOFOLLOW chain in
:mod:`pr001_safe_read` rather than :func:`os.walk`.

Canonical current evaluation constructs :class:`ReleaseRepositoryFacts` from
an explicit Git commit and refuses caller overrides of ancestry, tree, tracked
blob, environment or registry facts. External authorities that Git cannot own
- evaluation time, deployment allowlist, trust store and nonce history - stay
explicit in :class:`EvalContext`; object-level evaluators retain injection only
for isolated mutation tests and are not release-decision entrypoints.

Pure stdlib, hermetic, no network.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import platform
import re
import sys
import unicodedata

import pr001_release_signing as _sig
import pr001_safe_read as _sr
from pr001_evidence_binding import (
    EvaluationClock,
    EvidenceBindingError,
    GitSubject,
    ReleaseRepositoryFacts,
    path_within_any,
    verify_environment_fingerprint_exact,
    worktree_is_dirty,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PR_ROOT = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces"
GATES_DIR = PR_ROOT / "contracts" / "gates"
SECURITY_DIR = PR_ROOT / "contracts" / "security"

RELEASE_REGISTRY_PATH = GATES_DIR / "release_gate_registry_v1.json"
RELEASE_REGISTRY_SCHEMA_PATH = GATES_DIR / "release_gate_registry_v1.schema.json"
RELEASE_RECEIPT_SCHEMA_PATH = GATES_DIR / "release_gate_receipt_v1.schema.json"
RELEASE_AUTH_SCHEMA_PATH = GATES_DIR / "release_authorization_receipt_v1.schema.json"
GO_NO_GO_SCHEMA_PATH = (
    PR_ROOT / "contracts" / "rt026" / "schemas" / "go_no_go_report_v1.schema.json"
)
VERIFICATION_REGISTRY_PATH = GATES_DIR / "gate_registry_v1.json"
CAPABILITY_MAP_PATH = GATES_DIR / "synthetic_closure_map_v1.json"
SECURITY_REGISTRY_PATH = SECURITY_DIR / "security_gate_registry_v1.json"
VERIFICATION_RECEIPT_SCHEMA_PATH = GATES_DIR / "verification_gate_receipt_v1.schema.json"
CAPABILITY_RECEIPT_SCHEMA_PATH = GATES_DIR / "capability_activation_receipt_v1.schema.json"
SECURITY_RECEIPT_SCHEMA_PATH = SECURITY_DIR / "security_gate_receipt_v1.schema.json"

RELEASE_GATE_ORDER = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
VERIFICATION_RELEASE_GATES = ("G1", "G2", "G3", "G4", "G5", "G6")

RELEASE_RECEIPT_DOMAIN = b"cwk-release-gate-receipt-v1\x00"
RELEASE_AUTH_DOMAIN = b"cwk-release-authorization-receipt-v1\x00"
RELEASE_AUTH_SIGNATURE_DOMAIN = b"cwk-release-authorization-signature-v1\x00"
TENANT_ALLOWLIST_DOMAIN = b"cwk-release-tenant-allowlist-v1\x00"
VERIFICATION_RECEIPT_DOMAIN = b"cwk-verification-gate-receipt-v1\x00"
CAPABILITY_RECEIPT_DOMAIN = b"cwk-capability-activation-receipt-v1\x00"
SECURITY_RECEIPT_DOMAIN = b"cwk-security-gate-receipt-v1\x00"
GO_NO_GO_REPORT_DOMAIN = b"cwk-rt026-go-no-go-report-v1\x00"

GO_NO_GO_INPUT_CLASSES = (
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
)
GO_NO_GO_VERIFICATION_GATES = tuple(f"VG-{letter}" for letter in "ABCDE")
GO_NO_GO_RELEASE_GATES = tuple(f"G{number}" for number in range(1, 6))
GO_NO_GO_CAPABILITIES = (
    "CAP:cwork-authority-source",
    "CAP:gateway-identity-transport",
)
GO_NO_GO_SECURITY_GATES = tuple(f"SG:RT-{number:03d}" for number in range(17, 27))
GO_NO_GO_EVIDENCE_IDS = (
    "EVIDENCE:rt016-data-diff",
    "EVIDENCE:query-diff-six-layer",
    "EVIDENCE:rt024-benchmark",
    "EVIDENCE:rt025-vge-recovery",
    "EVIDENCE:rollback-rehearsal",
    "EVIDENCE:default-off",
    "EVIDENCE:open-findings",
)
GO_NO_GO_INPUT_IDS = (
    GO_NO_GO_VERIFICATION_GATES
    + GO_NO_GO_RELEASE_GATES
    + GO_NO_GO_CAPABILITIES
    + GO_NO_GO_SECURITY_GATES
    + GO_NO_GO_EVIDENCE_IDS
)

THIRTY_DAYS = datetime.timedelta(days=30)

#: Filename grammar for an archive directory, declared before it is enumerated.
ARCHIVE_NAME_PATTERN = re.compile(r"[0-9a-f]{64}\.json")
#: Filename grammar for the release receipt root: seven gate directories.
RELEASE_ROOT_NAME_PATTERN = re.compile(r"G[1-7]")


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object has two textual members with one key."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw):
    """Parse JSON while rejecting duplicate keys at every nesting level."""
    return json.loads(raw, object_pairs_hook=_unique_object)


def load_json(path: pathlib.Path):
    return strict_json_loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def nfc_deep(node):
    """Recursively NFC-normalise every object key and every string value.

    Normalising only the serialised string would leave two records that differ
    solely in Unicode composition hashing the same after serialisation but
    sorting differently before it, because `sort_keys` orders the RAW keys. The
    normalisation therefore has to happen before serialisation, per key.
    """
    if isinstance(node, dict):
        return {
            unicodedata.normalize("NFC", k) if isinstance(k, str) else k: nfc_deep(v)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [nfc_deep(item) for item in node]
    if isinstance(node, str):
        return unicodedata.normalize("NFC", node)
    return node


def jcs_bytes(node) -> bytes:
    """RFC 8785-shaped canonical serialisation of an NFC-normalised record."""
    canonical = json.dumps(
        nfc_deep(node), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return canonical.encode("utf-8")


def release_receipt_sha256(receipt: dict) -> str:
    """Domain-separated self-hash of a G1..G6 verification receipt."""
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    return hashlib.sha256(RELEASE_RECEIPT_DOMAIN + jcs_bytes(body)).hexdigest()


def family_receipt_sha256(receipt: dict, domain: bytes) -> str:
    """Domain-separated self-hash shared by VG/CAP/SG receipt families."""
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    return hashlib.sha256(domain + jcs_bytes(body)).hexdigest()


def release_auth_sha256(auth: dict) -> str:
    """Domain-separated self-hash of a G7 authorization.

    Excludes ONLY ``authorization_sha256``, so it covers ``signature_b64`` and
    therefore binds the final signature bytes. That is the asymmetry with
    :func:`authorization_signed_payload`: mutating the signature alone breaks
    this hash while leaving the signed payload untouched.
    """
    body = {k: v for k, v in auth.items() if k != "authorization_sha256"}
    return hashlib.sha256(RELEASE_AUTH_DOMAIN + jcs_bytes(body)).hexdigest()


def go_no_go_report_sha256(report: dict) -> str:
    """Domain-separated hash excluding only ``report_sha256``."""
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    return hashlib.sha256(GO_NO_GO_REPORT_DOMAIN + jcs_bytes(body)).hexdigest()


def authorization_signed_payload(auth: dict) -> bytes:
    """The exact bytes the external trust root signs.

    Two deletions and no others: the top-level ``authorization_sha256`` and the
    nested ``external_signature.signature_b64``. The earlier revision excluded
    only the former, which left the signature inside its own signed payload and
    made the construction unimplementable - producing the signature required
    already knowing it. Every additional exclusion would be a field an attacker
    could then mutate freely, so the list stops at two.

    A previous draft also froze the digest of these bytes in a top-level
    ``signed_payload_sha256``. That was unimplementable for the mirror-image
    reason: the field is part of the record and the exclusion list does not
    remove it, so its value would have had to sit inside the very bytes it
    digests. It was DELETED rather than promoted to a third exclusion. The
    signature is verified over these bytes directly, so there is no cached
    digest that could disagree with them.
    """
    body = {k: v for k, v in auth.items() if k != "authorization_sha256"}
    signature_block = body.get("external_signature")
    if isinstance(signature_block, dict):
        body["external_signature"] = {
            k: v for k, v in signature_block.items() if k != "signature_b64"
        }
    return RELEASE_AUTH_SIGNATURE_DOMAIN + jcs_bytes(body)


def tenant_allowlist_sha256(tenant_ids) -> str:
    """Privacy-preserving digest of the pilot allowlist.

    A count alone is not a binding: two different tenant sets of the same size
    are interchangeable under a count check, which is exactly how an
    authorization for tenants {a,b} is replayed against tenants {c,d}. The ids
    go into the digest and never into the artefact.
    """
    digest = hashlib.sha256(TENANT_ALLOWLIST_DOMAIN)
    for tenant in sorted(unicodedata.normalize("NFC", str(t)) for t in tenant_ids):
        digest.update(tenant.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def parse_instant(value):
    """Timezone-aware ISO-8601 or ``None``. A naive instant is not an instant."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ---------------------------------------------------------------------------
# Structural checking, driven by the real schema files
# ---------------------------------------------------------------------------


def _type_ok(node, declared) -> bool:
    if isinstance(declared, list):
        return bool(declared) and any(_type_ok(node, item) for item in declared)
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
    if declared == "null":
        return node is None
    return True


def structural_errors(node, schema, path="$"):
    """A JSON Schema subset checker, driven by the real schema JSON.

    Supports exactly the keywords the release contracts use: type, const, enum,
    pattern, minLength/maxLength, minimum/maximum, format:date-time, required,
    additionalProperties:false, properties/patternProperties/propertyNames,
    min/maxProperties, items, minItems/maxItems, uniqueItems, allOf, if/then,
    anyOf and not. Anchored patterns are matched with an explicit newline
    rejection, because ``$`` in Python also matches before a trailing newline
    and would otherwise let ``foo\\n`` satisfy ``^foo$``.

    Driving this from the schema file rather than from hand-written assertions
    is what makes a WEAKENED SCHEMA fail the suite: delete a `required` entry
    and the negative fixture that depended on it stops being rejected.
    """
    errs = []
    for branch in schema.get("allOf", []):
        errs.extend(structural_errors(node, branch, path))
    conditional = schema.get("if")
    if isinstance(conditional, dict) and not structural_errors(node, conditional, path):
        consequence = schema.get("then")
        if isinstance(consequence, dict):
            errs.extend(structural_errors(node, consequence, path))
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and not any(
        not structural_errors(node, branch, path) for branch in alternatives
    ):
        errs.append(f"anyOf:{path}")
    forbidden_shape = schema.get("not")
    if isinstance(forbidden_shape, dict) and not structural_errors(
        node, forbidden_shape, path
    ):
        errs.append(f"not:{path}")
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
        if schema.get("format") == "date-time" and parse_instant(node) is None:
            errs.append(f"format:{path}")

    if isinstance(node, int) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            errs.append(f"minimum:{path}")
        if "maximum" in schema and node > schema["maximum"]:
            errs.append(f"maximum:{path}")

    if isinstance(node, dict):
        props = schema.get("properties", {})
        pattern_props = schema.get("patternProperties", {})
        compiled_patterns = [
            (re.compile(pattern), child_schema)
            for pattern, child_schema in pattern_props.items()
        ]
        for key in schema.get("required", []):
            if key not in node:
                errs.append(f"required:{path}.{key}")
        if "minProperties" in schema and len(node) < schema["minProperties"]:
            errs.append(f"minProperties:{path}")
        if "maxProperties" in schema and len(node) > schema["maxProperties"]:
            errs.append(f"maxProperties:{path}")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, dict):
            for key in node:
                errs.extend(
                    structural_errors(key, property_names, f"{path}.<property>")
                )
        if schema.get("additionalProperties") is False:
            for key in node:
                if key not in props and not any(
                    pattern.search(key) for pattern, _child in compiled_patterns
                ):
                    errs.append(f"additionalProperties:{path}.{key}")
        for key, value in node.items():
            if key in props:
                errs.extend(structural_errors(value, props[key], f"{path}.{key}"))
            for pattern, child_schema in compiled_patterns:
                if pattern.search(key):
                    errs.extend(
                        structural_errors(value, child_schema, f"{path}.{key}")
                    )

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


def deep_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from deep_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from deep_keys(item)


#: Codes emitted by :func:`structural_errors` and the two self-hash checks.
#: The acceptance oracle asserts these are ABSENT from a semantic negative, so
#: that a "rejected" fixture cannot be passing for a vacuous reason.
GENERIC_CODE_PREFIXES = (
    "required:",
    "pattern:",
    "type:",
    "const:",
    "enum:",
    "format:",
    "minItems:",
    "maxItems:",
    "minimum:",
    "maximum:",
    "minLength:",
    "maxLength:",
    "uniqueItems:",
    "additionalProperties:",
    "forbidden_key:",
)
GENERIC_CODES = ("self_hash_mismatch", "schema_id_mismatch")


def generic_violations(violations):
    """The subset of `violations` that would reject almost any malformed record.

    A regression that only ever trips these is not testing what it claims: it
    proves the schema checker works, not that the semantic branch under test is
    reachable. The oracle asserts this set is empty.
    """
    return sorted(
        code
        for code in violations
        if code in GENERIC_CODES or code.startswith(GENERIC_CODE_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Canonical prerequisite resolution
# ---------------------------------------------------------------------------


class PrerequisiteResolver:
    """Maps a ``ref_id`` to the ONE path that id is allowed to denote.

    Resolution never consults the receipt. The receipt's ``ref_path`` is then
    compared to the resolved path for equality, which is the whole point: a
    reference that names its own target can name a cheaper one.
    """

    def __init__(
        self,
        registry: dict,
        *,
        verification_registry: dict | None = None,
        security_registry: dict | None = None,
        capability_map: dict | None = None,
    ):
        self.registry = registry
        self.resolution = registry["prerequisite_resolution"]
        self.families = list(self.resolution["families"])
        self.verification_registry = (
            load_json(VERIFICATION_REGISTRY_PATH)
            if verification_registry is None
            else verification_registry
        )
        self.security_registry = (
            load_json(SECURITY_REGISTRY_PATH)
            if security_registry is None
            else security_registry
        )
        self.capability_map = (
            load_json(CAPABILITY_MAP_PATH)
            if capability_map is None
            else capability_map
        )
        self._release_gates = {g["gate_id"]: g for g in registry["gates"]}

    def family_for(self, ref_id: str):
        # The registry patterns carry their own ``^``/``$`` anchors. They are
        # used VERBATIM: stripping anchors would silently corrupt any pattern
        # whose payload legitimately begins or ends with those characters, and
        # ``fullmatch`` already requires the whole string to match.
        if not isinstance(ref_id, str):
            return None
        for family in self.families:
            if re.fullmatch(family["ref_id_pattern"], ref_id or ""):
                return family
        return None

    def canonical_path(self, ref_id: str):
        family = self.family_for(ref_id)
        if family is None:
            return None
        name = family["family"]
        if name == "bootstrap_review":
            return self.registry["bootstrap_gate"]["final_wave0_review_report_path"]
        if name == "release_gate":
            gate = self._release_gates.get(ref_id)
            return gate["receipt_path"] if gate else None
        if name == "rt_acceptance":
            entry = self.resolution["rt_acceptance_reports"].get(ref_id)
            return entry["report_path"] if entry else None
        if name == "verification_gate":
            for gate in self.verification_registry["gates"]:
                if gate["gate_id"] == ref_id:
                    return gate["receipt_path"]
            return None
        if name == "capability_activation":
            capability_id = ref_id.split(":", 1)[1]
            for entry in self.capability_map["capabilities"]:
                if entry["capability_id"] == capability_id:
                    return entry["activation_receipt_path"]
            return None
        if name == "security_gate":
            producer_rt = ref_id.split(":", 1)[1]
            for entry in self.security_registry["entries"]:
                if entry["producer_rt"] == producer_rt:
                    return entry["receipt_path"]
            return None
        if name == "go_no_go":
            return self.resolution["go_no_go_report_path"]
        return None

    def expected_ref_kind(self, ref_id: str):
        family = self.family_for(ref_id)
        return family["ref_kind"] if family else None

    def body_id_field(self, family_name: str):
        return {
            "release_gate": "gate_id",
            "verification_gate": "gate_id",
            "capability_activation": "capability_id",
            "security_gate": "producer_rt",
            "go_no_go": "report_id",
        }.get(family_name)

    def expected_body_id(self, ref_id: str, family_name: str):
        if family_name in ("capability_activation", "security_gate"):
            return ref_id.split(":", 1)[1]
        return ref_id


# ---------------------------------------------------------------------------
# Delegated VG / capability / security authorities
# ---------------------------------------------------------------------------


def _snapshot_schema(ctx, attribute: str, fallback_path: pathlib.Path):
    """Use the explicit evaluation snapshot in canonical mode.

    Object-level mutation tests intentionally have no repository facts and may
    still load the checked-in fixture directly.  A release-decision entrypoint
    always supplies ``ReleaseRepositoryFacts`` and therefore never reads a
    schema from moving HEAD.
    """

    facts = ctx.repository_facts
    if facts is not None:
        return getattr(facts, attribute)
    return load_json(fallback_path)


def _delegated_authorities(ctx):
    """Bind the original family authorities to one explicit release snapshot.

    VG/capability and SG semantics predate the release-gate layer.  Their
    canonical validators intentionally remain the single source of truth in
    the original contract suites; release evaluation calls those exact method
    objects instead of maintaining another semantic subset.  The adapters only
    inject the release evaluation's root, explicit Git commit, clock and live
    environment.  They add no receipt rule of their own.
    """
    if ctx._delegated_authorities is not None:
        return ctx._delegated_authorities

    # Lazy imports avoid making the standalone contract modules depend on the
    # release evaluator.  Both suites and this entrypoint therefore execute the
    # same implementation, with no import cycle and no copied rule table.
    from test_pr001_gate_contracts import (  # noqa: WPS433
        ClosureEvaluationRegressionTests as GateCapabilityAuthority,
    )
    from test_pr001_security_gate_contracts import (  # noqa: WPS433
        SecurityChecks as SecurityAuthority,
    )

    git = (
        ctx.repository_facts.git
        if ctx.repository_facts is not None
        else GitSubject.for_repo(ctx.root)
    )
    expected_external_environment = {
        "python_version": platform.python_version(),
        "platform": sys.platform,
    }

    gate = GateCapabilityAuthority(methodName="runTest")
    gate.map = ctx.resolver.capability_map
    gate.schema = _snapshot_schema(
        ctx, "capability_receipt_schema", CAPABILITY_RECEIPT_SCHEMA_PATH
    )
    gate.vg_schema = _snapshot_schema(
        ctx, "verification_receipt_schema", VERIFICATION_RECEIPT_SCHEMA_PATH
    )
    gate.registry = ctx.resolver.verification_registry
    gate.by_capability = {
        entry["capability_id"]: entry for entry in gate.map["capabilities"]
    }
    gate.by_gate = {entry["gate_id"]: entry for entry in gate.registry["gates"]}
    gate.closure_by_gate = {
        entry["gate_id"]: entry for entry in gate.map["gate_closure"]
    }
    gate.root = ctx.root
    gate.git = git
    gate._git_cache = {str(ctx.root): git}
    gate.expected_environment = expected_external_environment
    gate.clock = EvaluationClock(
        ctx.now,
        max_skew_seconds=gate.map["activation_max_clock_skew_seconds"],
        max_probe_age_seconds=gate.map["activation_max_probe_age_seconds"],
    )
    gate._evaluation_commit = lambda _root: ctx.evaluation_commit
    gate._gate_code_prefixes = lambda gate_id: [
        f"RT/{gate.by_gate[gate_id]['feeder_rt']}/"
    ]

    security = SecurityAuthority()
    security.registry = ctx.resolver.security_registry
    security.schema = _snapshot_schema(
        ctx, "security_receipt_schema", SECURITY_RECEIPT_SCHEMA_PATH
    )
    security.by_rt = {
        entry["producer_rt"]: entry for entry in security.registry["entries"]
    }
    security.root = ctx.root
    security.git = git
    security._git_cache = {str(ctx.root): git}
    security._evidence_cache = {}
    security.expected_environment = expected_external_environment
    security._evaluation_commit = lambda _root: ctx.evaluation_commit

    ctx._delegated_authorities = (gate, security)
    return ctx._delegated_authorities


def evaluate_delegated_root_closure(ctx):
    """Exact whole-root closure for all three delegated receipt families."""
    if ctx._delegated_root_cache is not None:
        return list(ctx._delegated_root_cache)
    gate, security = _delegated_authorities(ctx)
    violations = []
    if not gate._gate_receipt_root_is_closed(ctx.root):
        violations.append("delegated_root_unclosed:verification_gate")
    if not gate._capability_receipt_root_is_closed(ctx.root):
        violations.append("delegated_root_unclosed:capability_activation")
    if not security._receipt_root_is_closed(ctx.root):
        violations.append("delegated_root_unclosed:security_gate")
    ctx._delegated_root_cache = tuple(violations)
    return list(ctx._delegated_root_cache)


def _check_delegated_family_body(ref_id, family_name, body, ctx, violations):
    """Run the original path-bound family validator, never a release subset."""
    cache_key = (
        family_name,
        ref_id,
        hashlib.sha256(jcs_bytes(body)).hexdigest(),
        ctx.evaluation_commit,
    )
    if cache_key in ctx._delegated_family_cache:
        if not ctx._delegated_family_cache[cache_key]:
            violations.append(f"prereq_body_delegated_family_invalid:{ref_id}")
        return
    gate, security = _delegated_authorities(ctx)
    canonical = ctx.resolver.canonical_path(ref_id)
    if family_name == "verification_gate":
        on_disk = gate._safe_json(ctx.root, canonical)
        valid = on_disk == body and gate._gate_history_state(ref_id, ctx.root) == "VALID"
        if valid and ref_id != "VG-A":
            valid = gate._vg_receipt_closes(ref_id, body, ctx.root)
    elif family_name == "capability_activation":
        capability_id = ref_id.split(":", 1)[1]
        valid = (
            gate._safe_json(ctx.root, canonical) == body
            and gate._activation_chain_state(capability_id, ctx.root) == "VALID"
        )
    elif family_name == "security_gate":
        rt_id = ref_id.split(":", 1)[1]
        entry = security.by_rt.get(rt_id)
        valid = (
            entry is not None
            and security._safe_json(ctx.root, canonical) == body
            and security._receipt_is_valid(entry, body, ctx.root)
        )
    else:  # pragma: no cover - caller freezes the three delegated families
        return
    ctx._delegated_family_cache[cache_key] = bool(valid)
    if not valid:
        violations.append(f"prereq_body_delegated_family_invalid:{ref_id}")


# ---------------------------------------------------------------------------
# The frozen acceptance marker
# ---------------------------------------------------------------------------


def parse_acceptance_marker(text: str, marker: dict):
    """Parse the ``cwk-acceptance-v1`` block, or return an error code.

    Naive substring matching on ``PASS`` is explicitly forbidden by the
    registry, and for good reason: the word occurs in ordinary prose ("the
    criteria to PASS were not met"), in quoted contract text, and in tables of
    contents, so a substring test can read a report as saying the opposite of
    what it says. This is an exact line grammar inside exactly one block.
    """
    opens = text.count(marker["block_open"])
    if opens == 0:
        return None, "marker_missing"
    if opens > 1:
        # Picking the first would let an author append a second, contradictory
        # block that humans read and machines ignore.
        return None, "marker_duplicate"
    after = text.split(marker["block_open"], 1)[1]
    if marker["block_close"] not in after:
        return None, "marker_unterminated"
    inner = after.split(marker["block_close"], 1)[0]
    line_re = re.compile(marker["field_line_pattern"])
    fields = {}
    for raw_line in inner.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = line_re.fullmatch(line)
        if match is None:
            return None, "marker_field_grammar"
        key, value = match.group(1), match.group(2)
        if key in fields:
            return None, "marker_duplicate_field"
        fields[key] = value
    required = set(marker["required_fields"])
    if set(fields) != required:
        return None, "marker_field_set"
    return fields, None


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


class TrustRecord:
    """One authoritative (trust_root_id, key_id) record.

    The artefact's ``external_signature`` block MIRRORS this record; every
    mirrored field must match it exactly and verification uses THIS record's
    public key and algorithm. An artefact that could nominate its own key would
    be self-authorising.
    """

    __slots__ = (
        "trust_root_id",
        "key_id",
        "public_key",
        "algorithm",
        "state",
        "not_before",
        "expires_at",
        "revoked_at",
        "purpose",
        "principal_id",
    )

    def __init__(
        self,
        *,
        trust_root_id: str,
        key_id: str,
        public_key,
        algorithm: str = _sig.ALGORITHM,
        state: str = "active",
        not_before: str,
        expires_at: str,
        revoked_at=None,
        purpose: str = "release_authorization",
        principal_id: str,
    ):
        self.trust_root_id = trust_root_id
        self.key_id = key_id
        self.public_key = public_key
        self.algorithm = algorithm
        self.state = state
        self.not_before = not_before
        self.expires_at = expires_at
        self.revoked_at = revoked_at
        self.purpose = purpose
        self.principal_id = principal_id


RELEASE_AUTHORIZATION_PURPOSE = "release_authorization"


class TrustStore:
    """An authoritative mapping keyed by the PAIR (trust_root_id, key_id).

    Not a set of accepted names, and not a lookup by ``key_id`` alone: two
    roots may legitimately number their keys the same way, and collapsing the
    key would let a weaker root's key validate a stronger root's authorization.
    Lookup fails closed - an unknown pair is rejected, never treated as an
    unconstrained key.
    """

    def __init__(self, records=()):
        self._records = {}
        for record in records:
            self.add(record)

    def add(self, record: TrustRecord) -> TrustRecord:
        if not isinstance(record, TrustRecord):
            raise TypeError("trust store accepts TrustRecord only")
        if not record.trust_root_id or not record.key_id or not record.principal_id:
            raise ValueError("trust record identity fields must be non-empty")
        if record.state not in {"active", "revoked", "suspended"}:
            raise ValueError("invalid trust record state")
        not_before = parse_instant(record.not_before)
        expires_at = parse_instant(record.expires_at)
        if not_before is None or expires_at is None or not not_before < expires_at:
            raise ValueError("invalid trust record validity window")
        if record.revoked_at is not None and parse_instant(record.revoked_at) is None:
            raise ValueError("invalid trust record revoked_at")
        if not isinstance(record.purpose, str) or not record.purpose:
            raise ValueError("invalid trust record purpose")
        if record.algorithm != _sig.ALGORITHM:
            raise ValueError("unsupported trust record algorithm")
        if not _sig.point_is_on_curve(record.public_key):
            raise ValueError("invalid trust record public key")
        key = (record.trust_root_id, record.key_id)
        if key in self._records:
            raise ValueError("duplicate trust record key")
        self._records[key] = record
        return record

    def lookup(self, trust_root_id, key_id):
        return self._records.get((trust_root_id, key_id))

    def __len__(self):
        return len(self._records)


# ---------------------------------------------------------------------------
# Evaluation context
# ---------------------------------------------------------------------------


class EvalContext:
    """Facts about the WORLD, recomputed or observed - never taken from the artefact.

    The production validator derives these from git, the filesystem, the trust
    store and the deployment. Injecting them keeps the suite hermetic while
    still exercising every check, because a negative is expressed by changing
    the world (a different commit, a revoked key, a different tenant set)
    rather than by stubbing out the rule.
    """

    def __init__(
        self,
        *,
        root,
        resolver: PrerequisiteResolver,
        now=None,
        observed_environment=None,
        expected_owner_scope_tree=None,
        expected_owner_scope_trees=None,
        introducing_commit="f" * 40,
        ancestor_commits=(),
        touched_owner_code_commits=(),
        touched_owner_code_by_gate=None,
        archive_chain=(),
        trust_store=None,
        project_identities=(
            "agent-rt026-impl",
            "agent-go-no-go-eval",
            "agent-rt026-accept",
            "test-signer",
        ),
        beneficiary_identities=("agent-rt026-impl", "agent-go-no-go-eval"),
        rt026_identities=("agent-rt026-impl", "agent-go-no-go-eval", "agent-rt026-accept"),
        used_nonces=(),
        deployment_instance_id=None,
        deployment_environment=None,
        deployment_tenant_allowlist=None,
        referenced_g6=None,
        trusted_orchestrations=None,
        legacy_owner_scope_drift=(),
        prerequisite_subject_commits=None,
        prerequisite_owner_scope_trees=None,
        prerequisite_environments=None,
        tracked_evidence_paths=None,
        evaluation_commit=None,
        repository_facts=None,
    ):
        self.root = pathlib.Path(root)
        self.resolver = resolver
        self.now = now or datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc)
        self.observed_environment = dict(observed_environment or {})
        self.expected_owner_scope_tree = expected_owner_scope_tree
        self.expected_owner_scope_trees = dict(expected_owner_scope_trees or {})
        self.introducing_commit = introducing_commit
        self.ancestor_commits = set(ancestor_commits)
        self.touched_owner_code_commits = set(touched_owner_code_commits)
        self.touched_owner_code_by_gate = {
            gate_id: set(commits)
            for gate_id, commits in (touched_owner_code_by_gate or {}).items()
        }
        self.archive_chain = list(archive_chain)
        self.trust_store = trust_store if trust_store is not None else TrustStore()
        self.project_identities = set(project_identities)
        self.beneficiary_identities = set(beneficiary_identities)
        self.rt026_identities = set(rt026_identities)
        self.used_nonces = set(used_nonces)
        self.deployment_instance_id = deployment_instance_id
        self.deployment_environment = deployment_environment
        self.deployment_tenant_allowlist = (
            None if deployment_tenant_allowlist is None else list(deployment_tenant_allowlist)
        )
        self.referenced_g6 = referenced_g6
        self.trusted_orchestrations = dict(trusted_orchestrations or {})
        #: RT ids whose owner code changed since the grandfathered acceptance.
        self.legacy_owner_scope_drift = set(legacy_owner_scope_drift)
        self.prerequisite_subject_commits = dict(prerequisite_subject_commits or {})
        self.prerequisite_owner_scope_trees = dict(
            prerequisite_owner_scope_trees or {}
        )
        self.prerequisite_environments = {
            ref_id: dict(environment)
            for ref_id, environment in (prerequisite_environments or {}).items()
        }
        self.tracked_evidence_paths = (
            None if tracked_evidence_paths is None else set(tracked_evidence_paths)
        )
        self.evaluation_commit = evaluation_commit
        self.repository_facts = repository_facts
        self._release_version_index = None
        self._release_validation_stack = set()
        self._delegated_authorities = None
        self._delegated_family_cache = {}
        self._delegated_root_cache = None

    def owner_scope_tree_for(self, gate_id):
        """Return the recomputed candidate digest for one release gate.

        Recursive validation may judge G1 while consuming it from G2, so one
        caller-wide scalar cannot represent every gate in the chain.  The map
        is authoritative when present; the scalar remains the current-gate
        compatibility path for hermetic single-receipt tests.
        """
        if gate_id in self.expected_owner_scope_trees:
            return self.expected_owner_scope_trees[gate_id]
        return self.expected_owner_scope_tree


def _canonical_session_from(ctx, facts, authoritative_resolver):
    """Build a base, call-local context without dispatching through ``ctx``.

    The caller is allowed to supply external facts such as the evaluation
    clock, trust store and deployment target.  It is never allowed to supply
    repository policy, resolver state, Git facts or caches after the
    authoritative precheck.  Reading the instance dictionary through
    ``object.__getattribute__`` also prevents a subclass from virtualising
    those external attributes while this boundary is crossed.
    """

    if not isinstance(ctx, EvalContext):
        raise TypeError("canonical release context must be EvalContext")
    state = object.__getattribute__(ctx, "__dict__")
    return EvalContext(
        root=facts.root,
        resolver=authoritative_resolver,
        now=state["now"],
        observed_environment=dict(facts.observed_environment),
        expected_owner_scope_tree=None,
        expected_owner_scope_trees={},
        introducing_commit=facts.evaluation_commit,
        ancestor_commits=(),
        touched_owner_code_commits=(),
        touched_owner_code_by_gate={},
        archive_chain=(),
        trust_store=state["trust_store"],
        project_identities=state["project_identities"],
        beneficiary_identities=state["beneficiary_identities"],
        rt026_identities=state["rt026_identities"],
        used_nonces=state["used_nonces"],
        deployment_instance_id=state["deployment_instance_id"],
        deployment_environment=state["deployment_environment"],
        deployment_tenant_allowlist=state["deployment_tenant_allowlist"],
        referenced_g6=state["referenced_g6"],
        trusted_orchestrations=state["trusted_orchestrations"],
        legacy_owner_scope_drift=(),
        prerequisite_subject_commits={},
        prerequisite_owner_scope_trees={},
        prerequisite_environments={},
        tracked_evidence_paths=None,
        evaluation_commit=facts.evaluation_commit,
        repository_facts=facts,
    )


class ResolvedPrerequisite:
    """One prerequisite after path resolution, byte read and body validation."""

    __slots__ = ("ref_id", "family", "path", "raw", "body", "text", "synthetic", "identities", "historical")

    def __init__(self, ref_id, family, path):
        self.ref_id = ref_id
        self.family = family
        self.path = path
        self.raw = None
        self.body = None
        self.text = None
        self.synthetic = False
        self.identities = frozenset()
        self.historical = False


def _archive_chain_from_disk(ctx, gate, *, is_authorization=False):
    """Return the declared archive history from fail-closed bytes on disk.

    ``EvalContext.archive_chain`` is deliberately not consulted.  That field
    existed for the first hermetic prototype and allowed a caller to invent a
    predecessor that did not exist in the repository.  The archive directory
    is the authority; if it cannot be enumerated exactly, the chain is
    unverifiable rather than empty.
    """
    gate_id = gate["gate_id"]
    try:
        snapshot = _sr.directory_snapshot(
            ctx.root,
            gate["archive_dir"].rstrip("/"),
            name_pattern=ARCHIVE_NAME_PATTERN,
            allow_dirs=False,
            missing_ok=True,
        )
    except _sr.SafeReadError:
        return [], [f"archive_unverifiable:{gate_id}"]
    if snapshot is None:
        return [], []

    schema = _snapshot_schema(
        ctx,
        (
            "release_authorization_schema"
            if is_authorization
            else "release_receipt_schema"
        ),
        RELEASE_AUTH_SCHEMA_PATH if is_authorization else RELEASE_RECEIPT_SCHEMA_PATH,
    )
    hash_field = "authorization_sha256" if is_authorization else "receipt_sha256"
    recompute = release_auth_sha256 if is_authorization else release_receipt_sha256
    bodies = []
    violations = []
    for name, raw in sorted(snapshot.files.items()):
        tag = f"{gate_id}/archive/{name}"
        source_rel = f"{gate['archive_dir'].rstrip('/')}/{name}"
        if (
            ctx.repository_facts is not None
            and not ctx.repository_facts.tracked_blob_matches(source_rel)
        ):
            violations.append(f"archive_untracked_or_blob_drift:{tag}")
        try:
            body = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            violations.append(f"archive_member_not_json:{tag}")
            continue
        if not isinstance(body, dict):
            violations.append(f"archive_member_not_json:{tag}")
            continue
        structural = structural_errors(body, schema)
        if structural:
            violations.extend(f"archive_schema:{tag}:{code}" for code in structural)
            continue
        actual = recompute(body)
        if body.get(hash_field) != actual:
            violations.append(f"archive_member_self_hash_mismatch:{tag}")
            continue
        if name != f"{actual}.json":
            violations.append(f"archive_member_misnamed:{tag}")
            continue
        if body.get("gate_id") != gate_id:
            violations.append(f"archive_member_gate_id_mismatch:{tag}")
            continue
        bodies.append(body)
    return bodies, violations


class ReleaseVersionIndex:
    """One fail-closed current+archive snapshot for release receipt history."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.registry = ctx.resolver.registry
        self._by_gate = {}

    def _parse_record(self, gate, raw, source_path, *, archive_name=None):
        gate_id = gate["gate_id"]
        schema = _snapshot_schema(
            self.ctx,
            (
                "release_authorization_schema"
                if gate_id == "G7"
                else "release_receipt_schema"
            ),
            (
                RELEASE_AUTH_SCHEMA_PATH
                if gate_id == "G7"
                else RELEASE_RECEIPT_SCHEMA_PATH
            ),
        )
        recompute = release_auth_sha256 if gate_id == "G7" else release_receipt_sha256
        hash_field = "authorization_sha256" if gate_id == "G7" else "receipt_sha256"
        errors = []
        try:
            body = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, [f"version_not_json:{gate_id}:{source_path}"]
        if not isinstance(body, dict):
            return None, [f"version_not_json:{gate_id}:{source_path}"]
        errors.extend(
            f"version_schema:{gate_id}:{code}" for code in structural_errors(body, schema)
        )
        own_hash = recompute(body)
        if body.get(hash_field) != own_hash:
            errors.append(f"version_self_hash_mismatch:{gate_id}:{source_path}")
        if body.get("gate_id") != gate_id:
            errors.append(f"version_gate_mismatch:{gate_id}:{source_path}")
        if archive_name is not None and archive_name != f"{own_hash}.json":
            errors.append(f"version_filename_mismatch:{gate_id}:{source_path}")
        if self.ctx.repository_facts is not None and not self.ctx.repository_facts.tracked_blob_matches(
            source_path
        ):
            errors.append(f"version_untracked_or_blob_drift:{gate_id}:{source_path}")
        record = {
            "gate_id": gate_id,
            "raw": raw,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "body": body,
            "source_path": source_path,
            "logical_path": gate["receipt_path"],
            "receipt_sha256": body.get(hash_field),
            "sequence": body.get("sequence"),
            "created_at": parse_instant(body.get("created_at")),
            "is_current": archive_name is None,
        }
        return record, errors

    def gate_records(self, gate_id):
        if gate_id in self._by_gate:
            return self._by_gate[gate_id]
        gate = next((g for g in self.registry["gates"] if g["gate_id"] == gate_id), None)
        if gate is None:
            result = ([], [f"version_gate_unknown:{gate_id}"])
            self._by_gate[gate_id] = result
            return result
        errors = []
        records = []
        current_raw = _sr.try_read_bytes(self.ctx.root, gate["receipt_path"])
        if current_raw is not None:
            record, record_errors = self._parse_record(
                gate, current_raw, gate["receipt_path"]
            )
            errors.extend(record_errors)
            if record is not None:
                records.append(record)
        try:
            archive = _sr.directory_snapshot(
                self.ctx.root,
                gate["archive_dir"],
                name_pattern=ARCHIVE_NAME_PATTERN,
                missing_ok=True,
            )
        except _sr.SafeReadError:
            archive = None
            errors.append(f"version_archive_unverifiable:{gate_id}")
        if archive is not None:
            if current_raw is None and archive.files:
                errors.append(f"version_archive_without_current:{gate_id}")
            for name, raw in sorted(archive.files.items()):
                source = f"{gate['archive_dir']}/{name}"
                record, record_errors = self._parse_record(
                    gate, raw, source, archive_name=name
                )
                errors.extend(record_errors)
                if record is not None:
                    records.append(record)

        raw_hashes = [record["raw_sha256"] for record in records]
        if len(raw_hashes) != len(set(raw_hashes)):
            errors.append(f"version_duplicate_raw_hash:{gate_id}")
        sequences = [record["sequence"] for record in records]
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in sequences):
            errors.append(f"version_sequence_invalid:{gate_id}")
        elif records:
            if len(sequences) != len(set(sequences)):
                errors.append(f"version_sequence_duplicate:{gate_id}")
            if sorted(sequences) != list(range(1, max(sequences) + 1)):
                errors.append(f"version_sequence_gap:{gate_id}")
            current = [record for record in records if record["is_current"]]
            if len(current) != 1 or current[0]["sequence"] != max(sequences):
                errors.append(f"version_current_not_tip:{gate_id}")
            by_sequence = {record["sequence"]: record for record in records}
            for sequence in range(1, max(sequences) + 1):
                record = by_sequence.get(sequence)
                if record is None:
                    continue
                if record["created_at"] is None:
                    errors.append(f"version_time_invalid:{gate_id}:{sequence}")
                if sequence == 1:
                    if record["body"].get("supersedes_receipt_sha256") is not None:
                        errors.append(f"version_genesis_has_predecessor:{gate_id}")
                else:
                    previous = by_sequence.get(sequence - 1)
                    if previous is None or record["body"].get(
                        "supersedes_receipt_sha256"
                    ) != previous["receipt_sha256"]:
                        errors.append(f"version_predecessor_mismatch:{gate_id}:{sequence}")
                    elif (
                        previous["created_at"] is None
                        or record["created_at"] is None
                        or not previous["created_at"] < record["created_at"]
                    ):
                        errors.append(f"version_time_not_increasing:{gate_id}:{sequence}")
        result = (records, errors)
        self._by_gate[gate_id] = result
        return result

    def resolve(self, ref_id, logical_path, raw_sha256, consumer_created_at):
        records, errors = self.gate_records(ref_id)
        violations = list(errors)
        # No member of a malformed history may be selected merely because its
        # raw hash happens to match.  Besides being fail-open, continuing here
        # could compare schema-invalid sequence values of unlike types.
        if violations:
            return None, violations
        gate = next((g for g in self.registry["gates"] if g["gate_id"] == ref_id), None)
        if gate is None or logical_path != gate["receipt_path"]:
            violations.append(f"version_logical_path_mismatch:{ref_id}")
            return None, violations
        as_of = parse_instant(consumer_created_at)
        if as_of is None:
            violations.append(f"version_consumer_time_invalid:{ref_id}")
            return None, violations
        matches = [record for record in records if record["raw_sha256"] == raw_sha256]
        if len(matches) != 1:
            violations.append(
                f"version_hash_{'missing' if not matches else 'duplicate'}:{ref_id}"
            )
            return None, violations
        eligible = [
            record
            for record in records
            if record["created_at"] is not None and record["created_at"] < as_of
        ]
        if not eligible:
            violations.append(f"version_from_future:{ref_id}")
            return None, violations
        selected = max(eligible, key=lambda record: record["sequence"])
        if matches[0] is not selected:
            violations.append(f"version_not_as_of_tip:{ref_id}")
            return None, violations
        return selected, violations


# ---------------------------------------------------------------------------
# Prerequisite evaluation
# ---------------------------------------------------------------------------


def _resolve_one_prerequisite(ref, receipt, ctx, violations):
    """Bind one ``prerequisite_refs`` entry to real bytes on disk.

    Returns a :class:`ResolvedPrerequisite` when the three-part binding held,
    otherwise ``None`` after appending the specific failure. Each failure stops
    this reference rather than cascading, so a wrong path produces exactly
    ``prereq_path_mismatch`` and not also ``prereq_unreadable`` - the oracle
    needs one code per defect to prove it hit the intended branch.
    """
    ref_id = ref.get("ref_id")
    resolver = ctx.resolver
    family = resolver.family_for(ref_id or "")
    if family is None:
        violations.append(f"prereq_unknown_family:{ref_id}")
        return None

    expected_kind = family["ref_kind"]
    if ref.get("ref_kind") != expected_kind:
        violations.append(f"prereq_kind_mismatch:{ref_id}")

    canonical = resolver.canonical_path(ref_id)
    declared = ref.get("ref_path")
    if declared in (family.get("forbidden_paths") or []):
        # Enumerated because it is the PLAUSIBLE substitute, not because the
        # equality test below would miss it: the round-4 review exists, is a
        # real independent review, and hashes to a stable value, so an
        # evaluator that accepted "some review report" would accept it.
        violations.append(f"prereq_forbidden_path:{ref_id}")
    if canonical is None or declared != canonical:
        violations.append(f"prereq_path_mismatch:{ref_id}")
        return None

    historical = False
    if family["family"] == "release_gate":
        if ctx._release_version_index is None:
            ctx._release_version_index = ReleaseVersionIndex(ctx)
        selected, version_violations = ctx._release_version_index.resolve(
            ref_id,
            canonical,
            ref.get("ref_sha256"),
            receipt.get("created_at"),
        )
        violations.extend(version_violations)
        if selected is None:
            return None
        raw = selected["raw"]
        historical = not selected["is_current"]
    else:
        try:
            raw = _sr.read_checked_bytes(ctx.root, canonical, missing_ok=True)
        except _sr.SafeReadError:
            violations.append(f"prereq_unreadable:{ref_id}")
            return None
        if raw is None:
            violations.append(f"prereq_evidence_missing:{ref_id}")
            return None
        if hashlib.sha256(raw).hexdigest() != ref.get("ref_sha256"):
            violations.append(f"prereq_hash_mismatch:{ref_id}")
            return None

    resolved = ResolvedPrerequisite(ref_id, family, canonical)
    resolved.raw = raw
    resolved.historical = historical
    if family["body_kind"] == "json":
        _check_json_body(resolved, receipt, ctx, violations)
    else:
        _check_markdown_body(resolved, receipt, ctx, violations)
    return resolved


def _check_json_body(resolved, receipt, ctx, violations):
    ref_id = resolved.ref_id
    family = resolved.family
    try:
        body = strict_json_loads(resolved.raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        violations.append(f"prereq_body_not_json:{ref_id}")
        return
    if not isinstance(body, dict):
        violations.append(f"prereq_body_not_json:{ref_id}")
        return
    resolved.body = body

    family_name = family["family"]
    if family_name in {
        "verification_gate",
        "capability_activation",
        "security_gate",
    }:
        _check_delegated_family_body(ref_id, family_name, body, ctx, violations)

    if family_name == "go_no_go":
        _check_go_no_go_body(ref_id, body, receipt, ctx, violations)

    if body.get("schema") != family["schema_id"]:
        # A correctly-hashed body at the correct path but of the wrong family
        # is a cross-family substitution.
        violations.append(f"prereq_body_schema_mismatch:{ref_id}")
    id_field = ctx.resolver.body_id_field(family["family"])
    if id_field is not None:
        expected_id = ctx.resolver.expected_body_id(ref_id, family["family"])
        if body.get(id_field) != expected_id:
            violations.append(f"prereq_body_id_mismatch:{ref_id}")

    if family["family"] == "release_gate":
        # An upstream receipt is a RECEIPT, not a blob. Its own self-hash must
        # recompute under the release separator, so a body edited after signing
        # is caught here rather than laundered into the citing gate on the
        # strength of its path and outer hash alone.
        if body.get("receipt_sha256") != release_receipt_sha256(body):
            violations.append(f"prereq_body_self_hash_mismatch:{ref_id}")
        upstream_gate = next(
            (
                gate
                for gate in ctx.resolver.registry["gates"]
                if gate["gate_id"] == ref_id
            ),
            None,
        )
        if upstream_gate is None:
            violations.append(f"prereq_body_registry_missing:{ref_id}")
        else:
            receipt_schema = _snapshot_schema(
                ctx, "release_receipt_schema", RELEASE_RECEIPT_SCHEMA_PATH
            )
            nested = evaluate_release_receipt(
                body,
                upstream_gate,
                receipt_schema,
                ctx,
                registry=ctx.resolver.registry,
                _historical=resolved.historical,
            )
            violations.extend(
                f"prereq_body_invalid:{ref_id}:{code}" for code in nested
            )

    status = body.get("status")
    if status != "pass":
        # `implementation_done` is called out by the registry because it is the
        # value a self-recording implementer writes, and it is not a pass.
        violations.append(f"prereq_body_not_pass:{ref_id}")

    if body.get("revoked") is True or body.get("revocation_ref") is not None:
        violations.append(f"prereq_body_revoked:{ref_id}")
    expires_at = parse_instant(body.get("expires_at"))
    if expires_at is not None and expires_at <= ctx.now:
        violations.append(f"prereq_body_expired:{ref_id}")

    if body.get("synthetic") is True:
        resolved.synthetic = True

    identities = set()
    for key in ("producer", "verifier", "evaluator", "recorded_by"):
        value = body.get(key)
        if isinstance(value, str):
            identities.add(value)
    resolved.identities = frozenset(identities)


def _check_go_no_go_body(ref_id, body, receipt, ctx, violations):
    """Validate the formal RT-026 machine receipt consumed by G6."""
    schema = _snapshot_schema(ctx, "go_no_go_schema", GO_NO_GO_SCHEMA_PATH)
    violations.extend(
        f"prereq_body_go_no_go_schema_invalid:{ref_id}:{code}"
        for code in structural_errors(body, schema)
    )
    contract = ctx.resolver.registry.get("go_no_go_contract") or {}
    report_path = ctx.resolver.canonical_path(ref_id)
    if ctx.repository_facts is not None and not ctx.repository_facts.tracked_blob_matches(
        report_path
    ):
        violations.append(f"prereq_body_go_no_go_untracked_report:{ref_id}")
    expected_contract_identity = {
        "schema_ref": GO_NO_GO_SCHEMA_PATH.relative_to(REPO_ROOT).as_posix(),
        "schema_id": schema.get("$id"),
        "report_path": report_path,
        "domain_separator": "cwk-rt026-go-no-go-report-v1\\0",
    }
    if any(
        contract.get(field) != expected
        for field, expected in expected_contract_identity.items()
    ):
        violations.append(f"prereq_body_go_no_go_contract_identity_mismatch:{ref_id}")
    if body.get("report_sha256") != go_no_go_report_sha256(body):
        violations.append(f"prereq_body_go_no_go_self_hash_mismatch:{ref_id}")
    if body.get("producer") == body.get("verifier"):
        violations.append(f"prereq_body_go_no_go_self_certified:{ref_id}")
    if body.get("status") != "pass" or body.get("conclusion") != contract.get(
        "terminal_conclusion"
    ):
        violations.append(f"prereq_body_go_no_go_not_ready:{ref_id}")
    if body.get("tested_subject_commit") != receipt.get("tested_subject_commit"):
        violations.append(f"prereq_body_go_no_go_subject_mismatch:{ref_id}")
    if body.get("owner_scope_tree_sha256") != receipt.get("owner_scope_tree_sha256"):
        violations.append(f"prereq_body_go_no_go_owner_tree_mismatch:{ref_id}")
    if not verify_environment_fingerprint_exact(
        body.get("environment_fingerprint"), receipt.get("environment_fingerprint")
    ):
        violations.append(f"prereq_body_go_no_go_environment_mismatch:{ref_id}")
    exact_arrays = (
        ("input_classes", GO_NO_GO_INPUT_CLASSES),
        ("verification_gate_ids", GO_NO_GO_VERIFICATION_GATES),
        ("release_gate_ids", GO_NO_GO_RELEASE_GATES),
        ("capability_ids", GO_NO_GO_CAPABILITIES),
        ("security_gate_ids", GO_NO_GO_SECURITY_GATES),
        ("excluded_input_ids", ("G6", "G7")),
    )
    for field, expected in exact_arrays:
        if tuple(body.get(field) or ()) != tuple(expected):
            violations.append(f"prereq_body_go_no_go_{field}_mismatch:{ref_id}")
    if body.get("open_blocker_count") != 0:
        violations.append(f"prereq_body_go_no_go_open_blocker:{ref_id}")
    if body.get("open_major_count") != 0:
        violations.append(f"prereq_body_go_no_go_open_major:{ref_id}")

    run = body.get("run_evidence") or {}
    if run.get("runner_id") != body.get("producer"):
        violations.append(f"prereq_body_go_no_go_runner_mismatch:{ref_id}")
    if body.get("status") == "pass" and (
        run.get("checks_total", 0) <= 0
        or run.get("checks_failed") != 0
        or run.get("exit_code") != 0
        or run.get("result") != "pass"
    ):
        violations.append(f"prereq_body_go_no_go_unclean_run:{ref_id}")

    frozen = parse_instant(body.get("candidate_frozen_at"))
    started = parse_instant(body.get("started_at"))
    completed = parse_instant(body.get("completed_at"))
    created = parse_instant(body.get("created_at"))
    if None in (frozen, started, completed, created) or not (
        frozen <= started < completed <= created
    ):
        violations.append(f"prereq_body_go_no_go_time_order_invalid:{ref_id}")

    refs = body.get("input_refs") or []
    ref_ids = [entry.get("input_id") for entry in refs if isinstance(entry, dict)]
    ref_paths = [entry.get("ref_path") for entry in refs if isinstance(entry, dict)]
    expected_ids = tuple(contract.get("exact_input_ids") or ())
    if len(ref_ids) != len(expected_ids) or set(ref_ids) != set(expected_ids):
        violations.append(f"prereq_body_go_no_go_input_set_mismatch:{ref_id}")
    if len(ref_ids) != len(set(ref_ids)):
        violations.append(f"prereq_body_go_no_go_duplicate_input_id:{ref_id}")
    if len(ref_paths) != len(set(ref_paths)):
        violations.append(f"prereq_body_go_no_go_duplicate_input_path:{ref_id}")

    evidence_inputs = contract.get("evidence_inputs") or {}
    for entry in refs:
        if not isinstance(entry, dict):
            continue
        input_id = entry.get("input_id")
        expected = None
        if input_id in evidence_inputs:
            expected = dict(evidence_inputs[input_id])
        else:
            family = ctx.resolver.family_for(input_id) if isinstance(input_id, str) else None
            canonical = (
                ctx.resolver.canonical_path(input_id)
                if isinstance(input_id, str)
                else None
            )
            if family is not None:
                if input_id.startswith("VG-"):
                    input_class = "verification_gate_receipts"
                elif re.fullmatch(r"G[1-5]", input_id):
                    input_class = "release_gate_receipts"
                else:
                    input_class = "security_crosswalk"
                expected = {
                    "input_class": input_class,
                    "ref_kind": family["ref_kind"],
                    "ref_path": canonical,
                    "schema_id": family["schema_id"],
                }
        if expected is None:
            violations.append(f"prereq_body_go_no_go_unknown_input:{input_id}")
            continue
        for field in ("input_class", "ref_kind", "ref_path", "schema_id"):
            if entry.get(field) != expected.get(field):
                violations.append(
                    f"prereq_body_go_no_go_input_{field}_mismatch:{input_id}"
                )
        raw_input = None
        tracked_input_path = expected.get("ref_path")
        if isinstance(input_id, str) and re.fullmatch(r"G[1-5]", input_id):
            # A historical G6 must keep validating after an upstream gate is
            # renewed.  The report's logical path therefore resolves through
            # the same current+archive as-of index as receipt prerequisites.
            if ctx._release_version_index is None:
                ctx._release_version_index = ReleaseVersionIndex(ctx)
            selected, version_errors = ctx._release_version_index.resolve(
                input_id,
                expected.get("ref_path"),
                entry.get("raw_sha256"),
                body.get("created_at"),
            )
            violations.extend(
                f"prereq_body_go_no_go_input_{code}" for code in version_errors
            )
            if selected is not None:
                raw_input = selected["raw"]
                tracked_input_path = selected["source_path"]
        else:
            try:
                raw_input = _sr.read_checked_bytes(
                    ctx.root, expected.get("ref_path"), missing_ok=True
                )
            except _sr.SafeReadError:
                raw_input = None
                violations.append(f"prereq_body_go_no_go_input_unreadable:{input_id}")
        if raw_input is None:
            violations.append(f"prereq_body_go_no_go_input_missing:{input_id}")
        elif hashlib.sha256(raw_input).hexdigest() != entry.get("raw_sha256"):
            violations.append(f"prereq_body_go_no_go_input_hash_mismatch:{input_id}")
        if (
            ctx.repository_facts is not None
            and not ctx.repository_facts.tracked_blob_matches(tracked_input_path)
        ):
            violations.append(f"prereq_body_go_no_go_input_untracked:{input_id}")

    policy = body.get("policy_ref") or {}
    if (
        policy.get("ref_path") != contract.get("policy_ref_path")
        or policy.get("schema_id") != ctx.resolver.registry.get("schema")
    ):
        violations.append(f"prereq_body_go_no_go_policy_ref_mismatch:{ref_id}")
    try:
        raw_policy = _sr.read_checked_bytes(
            ctx.root, contract.get("policy_ref_path"), missing_ok=True
        )
    except _sr.SafeReadError:
        raw_policy = None
        violations.append(f"prereq_body_go_no_go_policy_unreadable:{ref_id}")
    if raw_policy is None:
        violations.append(f"prereq_body_go_no_go_policy_missing:{ref_id}")
    elif hashlib.sha256(raw_policy).hexdigest() != policy.get("raw_sha256"):
        violations.append(f"prereq_body_go_no_go_policy_hash_mismatch:{ref_id}")
    if ctx.repository_facts is not None and not ctx.repository_facts.tracked_blob_matches(
        contract.get("policy_ref_path")
    ):
        violations.append(f"prereq_body_go_no_go_policy_untracked:{ref_id}")

    artifacts = body.get("artifacts") or []
    roles = [item.get("role") for item in artifacts if isinstance(item, dict)]
    if tuple(sorted(roles)) != tuple(sorted(contract.get("exact_artifact_roles") or ())):
        violations.append(f"prereq_body_go_no_go_artifact_roles_mismatch:{ref_id}")
    artifact_paths = [item.get("path") for item in artifacts if isinstance(item, dict)]
    if len(artifact_paths) != len(set(artifact_paths)):
        violations.append(f"prereq_body_go_no_go_duplicate_artifact_path:{ref_id}")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path")
        try:
            raw_artifact = _sr.read_checked_bytes(ctx.root, path, missing_ok=True)
        except _sr.SafeReadError:
            raw_artifact = None
            violations.append(f"prereq_body_go_no_go_artifact_unreadable:{path}")
        if raw_artifact is None:
            violations.append(f"prereq_body_go_no_go_artifact_missing:{path}")
        elif hashlib.sha256(raw_artifact).hexdigest() != artifact.get("raw_sha256"):
            violations.append(f"prereq_body_go_no_go_artifact_hash_mismatch:{path}")
        if ctx.repository_facts is not None and not ctx.repository_facts.tracked_blob_matches(
            path
        ):
            violations.append(f"prereq_body_go_no_go_artifact_untracked:{path}")


def _check_markdown_body(resolved, receipt, ctx, violations):
    ref_id = resolved.ref_id
    resolution = ctx.resolver.resolution
    try:
        text = resolved.raw.decode("utf-8")
    except UnicodeDecodeError:
        violations.append(f"prereq_body_not_utf8:{ref_id}")
        return
    resolved.text = text
    if not text.strip():
        violations.append(f"prereq_body_empty:{ref_id}")
        return
    try:
        parsed = strict_json_loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        # A JSON receipt filed under a narrative id would otherwise be read as
        # "a document that happens to contain no marker".
        violations.append(f"prereq_body_is_json:{ref_id}")
        return

    if ref_id == "G0":
        style = "cwk_acceptance_v1"
        entry = {"report_id": resolution["bootstrap_report_id"]}
    else:
        entry = resolution["rt_acceptance_reports"].get(ref_id) or {}
        style = entry.get("marker_style")
        if entry.get("report_id") != ref_id:
            violations.append(f"prereq_registry_report_id_mismatch:{ref_id}")
            return

    if style == "legacy_frozen_hash":
        _check_legacy_acceptance(resolved, entry, receipt, ctx, violations)
        return
    if style != "cwk_acceptance_v1":
        violations.append(f"prereq_unknown_marker_style:{ref_id}")
        return

    marker_violation_start = len(violations)
    marker = resolution["markdown_acceptance_marker"]
    marker_for_parse = marker
    rt_cwk_acceptance = (
        resolved.family.get("family") == "rt_acceptance"
        and style == "cwk_acceptance_v1"
    )
    if rt_cwk_acceptance:
        marker_for_parse = dict(marker)
        marker_for_parse["required_fields"] = list(marker["required_fields"]) + list(
            marker["rt_acceptance_required_identity_fields"]
        )
    fields, error = parse_acceptance_marker(text, marker_for_parse)
    if error is not None:
        violations.append(f"prereq_{error}:{ref_id}")
        return
    if fields["verdict"] != marker["verdict_pass_value"]:
        violations.append(f"prereq_marker_verdict_not_pass:{ref_id}")
    if fields["open_blocker"] != "0" or fields["open_major"] != "0":
        violations.append(f"prereq_marker_open_findings:{ref_id}")
    if fields["report_id"] != entry.get("report_id"):
        violations.append(f"prereq_marker_report_id_mismatch:{ref_id}")
    if fields["subject_commit"] != receipt.get("tested_subject_commit"):
        # Without this a genuine acceptance of an OLD candidate is
        # indistinguishable from an acceptance of the one being released.
        violations.append(f"prereq_marker_subject_mismatch:{ref_id}")
    if fields["owner_scope_tree_sha256"] != receipt.get("owner_scope_tree_sha256"):
        violations.append(f"prereq_marker_owner_tree_mismatch:{ref_id}")
    if rt_cwk_acceptance:
        identity_sets = []
        identity_pattern = re.compile(marker["identity_token_pattern"])
        for field in marker["rt_acceptance_required_identity_fields"]:
            value = fields.get(field, "")
            tokens = value.split(",") if value else []
            valid = (
                value == unicodedata.normalize("NFC", value)
                and value.isascii()
                and not any(character.isspace() for character in value)
                and bool(tokens)
                and all(identity_pattern.fullmatch(token) for token in tokens)
                and tokens == sorted(set(tokens))
            )
            if not valid:
                violations.append(f"prereq_marker_identity_list_invalid:{ref_id}:{field}")
            identity_sets.append(set(tokens) if valid else set())
        if identity_sets[0] & identity_sets[1]:
            violations.append(f"prereq_marker_identity_overlap:{ref_id}")
        resolved.identities = frozenset(identity_sets[0] | identity_sets[1])
    if ref_id in {"RT-012", "RT-013"} and len(violations) == marker_violation_start:
        _check_superseded_legacy_report(resolved, entry, ctx, violations)


def _check_superseded_legacy_report(resolved, entry, ctx, violations):
    """Bind a remediated RT's immutable prior report as provenance only.

    The old document is never parsed for a verdict and can never satisfy its
    RT prerequisite. Once RT-012's Stage-09 or RT-013's Stage-10 canonical
    marker itself is valid, this check proves the corresponding provenance
    object still names real, tracked, byte-identical historical evidence.
    """

    ref_id = resolved.ref_id
    frozen = entry.get("superseded_legacy_report")
    expected_keys = {
        "report_id",
        "report_path",
        "marker_style",
        "report_sha256",
        "accepted_subject_commit",
        "historical_verdict",
        "historical_open_blocker",
        "historical_open_major",
        "superseded_by_stage_index",
    }
    if not isinstance(frozen, dict) or set(frozen) != expected_keys:
        violations.append(f"superseded_report_contract_mismatch:{ref_id}")
        return
    expected_values_by_ref = {
        "RT-012": {
            "report_id": "RT-012",
            "report_path": "RT/RT-012/reports/独立验收报告.md",
            "marker_style": "legacy_frozen_hash",
            "report_sha256": "bce39f7dfaf1ac92b2f9765bb82622e4e0a5b16be2c48f7ed6cb5c0b822791e2",
            "accepted_subject_commit": "1894576b20a9e00d396718daeca0c781a7d233a6",
            "historical_verdict": "PASS",
            "historical_open_blocker": 0,
            "historical_open_major": 0,
            "superseded_by_stage_index": 9,
        },
        "RT-013": {
            "report_id": "RT-013",
            "report_path": "RT/RT-013/reports/独立验收报告.md",
            "marker_style": "legacy_frozen_hash",
            "report_sha256": "ece89870ada955d296fe90c8be3a4b4989e97be6b75cf10bf2863736bac6b76b",
            "accepted_subject_commit": "19f2e37ba8b35b0d98d3e866895d4490747c9e75",
            "historical_verdict": "PASS",
            "historical_open_blocker": 0,
            "historical_open_major": 0,
            "superseded_by_stage_index": 10,
        },
    }
    expected_values = expected_values_by_ref.get(ref_id)
    if expected_values is None:
        violations.append(f"superseded_report_contract_mismatch:{ref_id}")
        return
    if any(frozen.get(key) != value for key, value in expected_values.items()):
        violations.append(f"superseded_report_contract_mismatch:{ref_id}")
        return
    path = frozen["report_path"]
    if path == resolved.path or path == ctx.resolver.canonical_path(ref_id):
        violations.append(f"superseded_report_path_collision:{ref_id}")
        return
    try:
        raw = _sr.read_checked_bytes(ctx.root, path, missing_ok=True)
    except _sr.SafeReadError:
        violations.append(f"superseded_report_unreadable:{ref_id}")
        return
    if raw is None:
        violations.append(f"superseded_report_missing:{ref_id}")
        return
    if hashlib.sha256(raw).hexdigest() != frozen["report_sha256"]:
        violations.append(f"superseded_report_hash_mismatch:{ref_id}")
    if (
        ctx.repository_facts is not None
        and not ctx.repository_facts.tracked_blob_matches(path)
    ):
        violations.append(f"superseded_report_untracked:{ref_id}")


def _check_legacy_acceptance(resolved, entry, receipt, ctx, violations):
    """RT-011 and RT-014..RT-016: bound by frozen raw hash, never parsed.

    These four reports are historical reviewed evidence that predates the
    marker. Editing them in place would invalidate hashes recorded elsewhere
    and would rewrite the record of what a reviewer actually signed, so the
    registry transcribes their outcome and pins their bytes instead. The body
    is deliberately NOT parsed: the document never gets to elect its own
    validation mode.
    """
    ref_id = resolved.ref_id
    frozen = entry.get("report_sha256")
    actual = hashlib.sha256(resolved.raw).hexdigest()
    if frozen != actual:
        violations.append(f"legacy_report_hash_mismatch:{ref_id}")
        return
    if entry.get("historical_verdict") != "PASS":
        violations.append(f"legacy_verdict_not_pass:{ref_id}")
    if entry.get("historical_open_blocker") != 0 or entry.get("historical_open_major") != 0:
        violations.append(f"legacy_open_findings:{ref_id}")
    if ctx.repository_facts is not None:
        violations.extend(
            ctx.repository_facts.validate_legacy_acceptance(
                ref_id,
                entry.get("accepted_subject_commit"),
                receipt.get("tested_subject_commit"),
            )
        )
        return
    # A frozen PASS says nothing about a candidate whose owner code moved.
    # There is deliberately no caller-supplied "fresh rerun" bypass: an RT
    # that evolves leaves the grandfathered set and gets a new canonical
    # cwk_acceptance_v1 report (RT-012 Stage-09 is the first such case).
    if ref_id in ctx.legacy_owner_scope_drift:
        violations.append(f"legacy_drift_unresolved:{ref_id}")


def _check_go_no_go_separation(ctx, violations):
    """The go/no-go report must live OUTSIDE every receipt root.

    An evidence report filed inside a receipt root is a category error with a
    concrete consequence: receipt roots are closed sets validated by exact
    membership, so a report living there is either an undeclared file (the
    closure fails) or a declared one (the closure has been widened to admit
    non-receipts). Checked as a path-boundary containment test rather than a
    substring test, so a sibling directory whose name merely begins with a
    root's cannot be smuggled in.
    """
    resolution = ctx.resolver.resolution
    path = resolution["go_no_go_report_path"]
    roots = resolution["receipt_roots_that_must_not_contain_evidence_reports"]
    if path_within_any(path, roots):
        violations.append("go_no_go_path_inside_receipt_root")


# ---------------------------------------------------------------------------
# G6 orchestration provenance and fresh evidence
# ---------------------------------------------------------------------------


def _check_orchestration_provenance(receipt, ctx, resolved_refs, violations):
    provenance = receipt.get("orchestration_provenance")
    if not isinstance(provenance, dict):
        violations.append("orchestration_provenance_missing")
        return None

    session_id = provenance.get("session_id")
    trusted = ctx.trusted_orchestrations.get(session_id)
    if not isinstance(trusted, dict):
        violations.append("orchestration_attestation_missing")
    else:
        for field in (
            "candidate_freeze_sha256",
            "candidate_frozen_at",
            "session_id",
            "session_started_at",
            "engagement_id",
            "session_participants",
        ):
            if trusted.get(field) != provenance.get(field):
                violations.append(f"orchestration_attestation_mismatch:{field}")

    frozen_at = parse_instant(provenance.get("candidate_frozen_at"))
    started_at = parse_instant(provenance.get("session_started_at"))
    if frozen_at is not None and started_at is not None and started_at <= frozen_at:
        # A session that began before the candidate was frozen cannot have
        # exercised it.
        violations.append("orchestration_session_before_freeze")

    verifier_provenance = receipt.get("verifier_provenance") or {}
    if provenance.get("engagement_id") != verifier_provenance.get("engagement_id"):
        violations.append("orchestration_engagement_mismatch")

    participants = provenance.get("session_participants") or []
    for identity in (receipt.get("producer"), receipt.get("verifier")):
        if identity not in participants:
            violations.append(f"orchestration_receipt_identity_missing:{identity}")
    upstream_identities = set()
    for resolved in resolved_refs:
        upstream_identities |= set(resolved.identities)
    for participant in participants:
        if participant in upstream_identities:
            # Recomputed over the resolved bodies, not asserted: someone who
            # signed the evidence being reviewed is not an independent reviewer
            # of it, whatever the attestation booleans say.
            violations.append(f"orchestration_participant_in_upstream_evidence:{participant}")
        if participant in ctx.rt026_identities:
            violations.append(f"orchestration_participant_is_rt026_identity:{participant}")

    attestation = receipt.get("freshness_attestation") or {}
    contradicted = any(
        code.startswith("orchestration_participant_") for code in violations
    )
    if contradicted and all(value is True for value in attestation.values()):
        # The booleans are a claim; the recomputation is the evidence. Where
        # they disagree the receipt is rejected rather than the claim believed.
        violations.append("freshness_attestation_contradicted")
    return provenance


def _check_fresh_evidence(receipt, registry, ctx, provenance, violations):
    refs = receipt.get("fresh_evidence_refs")
    if not isinstance(refs, list):
        violations.append("fresh_evidence_refs_missing")
        return

    roles = {entry["role"]: entry for entry in registry["g6_fresh_evidence_roles"]}
    declared = [r.get("role") for r in refs if isinstance(r, dict)]
    if sorted(declared) != sorted(roles):
        # A missing role and an unknown role are equally invalid; there is no
        # "covered by another run" allowance.
        violations.append("fresh_evidence_roles_mismatch")

    common_fields = registry["g6_fresh_evidence_common_fields"]
    seen_runs, seen_paths, seen_hashes = set(), set(), set()
    receipt_created = parse_instant(receipt.get("created_at"))
    frozen_at = parse_instant((provenance or {}).get("candidate_frozen_at"))
    session_started = parse_instant((provenance or {}).get("session_started_at"))
    sg_ids = {
        ref.get("ref_id")
        for ref in receipt.get("prerequisite_refs", [])
        if isinstance(ref, dict) and str(ref.get("ref_id", "")).startswith("SG:")
    }
    receipt_roots = ctx.resolver.resolution[
        "receipt_roots_that_must_not_contain_evidence_reports"
    ]

    for entry in refs:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        tag = role if role in roles else "unknown"
        for field in common_fields:
            if field not in entry:
                violations.append(f"fresh_evidence_field_missing:{tag}:{field}")
        spec = roles.get(role)
        if spec is not None:
            for field in spec["required_role_fields"]:
                if field not in entry:
                    violations.append(f"fresh_evidence_role_field_missing:{tag}:{field}")

        run_id = entry.get("run_id")
        if run_id in seen_runs:
            violations.append(f"fresh_evidence_duplicate_run_id:{run_id}")
        seen_runs.add(run_id)
        # One artefact may not satisfy two roles, or a single log file could be
        # cited nine times to manufacture nine fresh runs.
        artifact_path = entry.get("artifact_path")
        if artifact_path in seen_paths:
            violations.append(f"fresh_evidence_duplicate_artifact:{artifact_path}")
        seen_paths.add(artifact_path)
        artifact_hash = entry.get("artifact_sha256")
        if artifact_hash in seen_hashes:
            violations.append(f"fresh_evidence_duplicate_artifact_hash:{tag}")
        seen_hashes.add(artifact_hash)

        if entry.get("tested_subject_commit") != receipt.get("tested_subject_commit"):
            violations.append(f"fresh_evidence_subject_mismatch:{tag}")
        if provenance is not None and entry.get("candidate_tree_sha256") != provenance.get(
            "candidate_freeze_sha256"
        ):
            violations.append(f"fresh_evidence_candidate_mismatch:{tag}")
        if entry.get("environment_fingerprint") != receipt.get("environment_fingerprint"):
            violations.append(f"fresh_evidence_environment_mismatch:{tag}")
        elif ctx.observed_environment and not verify_environment_fingerprint_exact(
            entry.get("environment_fingerprint"), ctx.observed_environment
        ):
            violations.append(f"fresh_evidence_environment_mismatch:{tag}")
        verifier_provenance = receipt.get("verifier_provenance") or {}
        if entry.get("engagement_id") != verifier_provenance.get("engagement_id"):
            violations.append(f"fresh_evidence_engagement_mismatch:{tag}")
        if provenance is not None and entry.get("session_id") != provenance.get("session_id"):
            violations.append(f"fresh_evidence_session_mismatch:{tag}")
        if provenance is not None and entry.get("runner_id") not in (
            provenance.get("session_participants") or []
        ):
            violations.append(f"fresh_evidence_runner_not_participant:{tag}")

        started = parse_instant(entry.get("started_at"))
        completed = parse_instant(entry.get("completed_at"))
        if frozen_at is not None and started is not None and started < frozen_at:
            # A run that started before the candidate was frozen tested
            # something else.
            violations.append(f"fresh_evidence_started_before_freeze:{tag}")
        if session_started is not None and started is not None and started < session_started:
            violations.append(f"fresh_evidence_started_before_session:{tag}")
        if started is not None and completed is not None and not started < completed:
            violations.append(f"fresh_evidence_timing_not_ordered:{tag}")
        if completed is not None and receipt_created is not None and completed > receipt_created:
            # A run that completed after the receipt was created was not
            # evidence available to the verifier when they signed.
            violations.append(f"fresh_evidence_completed_after_receipt:{tag}")

        if entry.get("exit_code") != 0 or entry.get("checks_failed") != 0:
            violations.append(f"fresh_evidence_not_clean:{tag}")
        if entry.get("result") != "pass" and receipt.get("status") == "pass":
            violations.append(f"fresh_evidence_not_pass:{tag}")

        if artifact_path is not None:
            if path_within_any(artifact_path, receipt_roots):
                violations.append(f"fresh_evidence_artifact_in_receipt_root:{tag}")
            if ctx.repository_facts is not None:
                if not ctx.repository_facts.tracked_blob_matches(artifact_path):
                    violations.append(f"fresh_evidence_untracked_artifact:{tag}")
            elif ctx.tracked_evidence_paths is None:
                violations.append(f"fresh_evidence_tracking_unavailable:{tag}")
            elif artifact_path not in ctx.tracked_evidence_paths:
                violations.append(f"fresh_evidence_untracked_artifact:{tag}")
            try:
                _sr.safe_relpath(artifact_path, label=artifact_path)
                raw = _sr.read_checked_bytes(ctx.root, artifact_path, missing_ok=True)
            except _sr.SafeReadError:
                violations.append(f"fresh_evidence_artifact_unsafe:{tag}")
                raw = None
            if raw is None:
                violations.append(f"fresh_evidence_artifact_missing:{tag}")
            elif hashlib.sha256(raw).hexdigest() != artifact_hash:
                violations.append(f"fresh_evidence_artifact_hash_mismatch:{tag}")

        _check_role_assertions(entry, role, sg_ids, violations)


def _check_role_assertions(entry, role, sg_ids, violations):
    """The per-role semantics that the retired bare labels never carried."""
    tag = role
    if role in (
        "full_regression",
        "wiki_smoke",
        "legacy_smoke",
        "attack_suite",
    ):
        if not isinstance(entry.get("checks_total"), int) or entry.get("checks_total", 0) <= 0:
            # A run that executed nothing trivially has no failures.
            violations.append(f"fresh_evidence_no_checks:{tag}")
    if role == "attack_suite":
        rechecked = set(entry.get("security_gate_ids_rechecked") or [])
        if not sg_ids <= rechecked:
            violations.append("fresh_evidence_sg_coverage_incomplete")
    if role == "secret_scan" and entry.get("findings_count") != 0:
        violations.append("fresh_evidence_secret_scan_findings")
    if role == "restore_drill":
        if entry.get("clean_room") is not True:
            violations.append("fresh_evidence_restore_not_clean_room")
        if entry.get("restore_verified") is not True:
            violations.append("fresh_evidence_restore_not_verified")
    if role == "rollback_drill" and entry.get("legacy_read_path_restored") is not True:
        violations.append("fresh_evidence_rollback_incomplete")
    if role == "default_off_verification":
        for field in ("enabled_component_count", "allowlisted_tenant_count", "enabled_flag_count"):
            if entry.get(field) != 0:
                violations.append(f"fresh_evidence_default_off_violated:{field}")
    if role == "final_findings_reconciliation":
        if entry.get("open_blocker_count") != 0 or entry.get("open_major_count") != 0:
            violations.append("fresh_evidence_open_findings")
        if entry.get("git_diff_check_clean") is not True:
            violations.append("fresh_evidence_tree_not_clean")
        if entry.get("tracked_evidence_match") is not True:
            violations.append("fresh_evidence_untracked_evidence")


# ---------------------------------------------------------------------------
# G1..G6 verification receipt
# ---------------------------------------------------------------------------

G6_ONLY_FIELDS = (
    "verifier_provenance",
    "freshness_attestation",
    "orchestration_provenance",
    "fresh_evidence_refs",
    "expires_at",
)


def evaluate_release_receipt(
    receipt, registry_gate, schema, ctx, *, registry=None, _historical=False
):
    """Full G1..G6 validation. Returns sorted violation codes; ``[]`` means valid."""
    registry = registry if registry is not None else ctx.resolver.registry
    v = []
    v.extend(structural_errors(receipt, schema))

    forbidden = set(schema.get("deepForbiddenKeys", []))
    for key in deep_keys(receipt):
        if key in forbidden:
            v.append(f"forbidden_key:{key}")

    gate_id = receipt.get("gate_id")
    cycle_key = (gate_id, receipt.get("receipt_sha256"))
    if cycle_key in ctx._release_validation_stack:
        v.append(f"release_prerequisite_cycle:{gate_id}:{receipt.get('receipt_sha256')}")
        return sorted(v)
    ctx._release_validation_stack.add(cycle_key)
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

    # --- prerequisites: EXACT set equality, then real evidence ------------
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

    resolved_refs = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        resolved = _resolve_one_prerequisite(ref, receipt, ctx, v)
        if resolved is not None:
            resolved_refs.append(resolved)
    if "RT-026-GO-NO-GO" in ref_ids:
        _check_go_no_go_separation(ctx, v)

    # --- synthetic scope propagation --------------------------------------
    # Recomputed from the resolved bodies; a receipt's own synthetic=false is
    # not evidence about its inputs. VG-A is permanently synthetic, so G3 and
    # G6 are capped at conservative_unknown until that gap is closed.
    synthetic_sources = sorted(r.ref_id for r in resolved_refs if r.synthetic)
    if gate_id == "G6" and synthetic_sources:
        # VG-A is intentionally immutable synthetic evidence.  Its gap closes
        # only when BOTH externally-owned capability activations exist and are
        # valid; the synthetic state then propagates through G3 -> G4 -> G5 as
        # provenance, but must not make G7 unreachable forever.  The exact
        # transitive set is frozen by the release DAG and the closure map.
        closure = next(
            (
                item
                for item in ctx.resolver.capability_map["gate_closure"]
                if item["gate_id"] == "VG-A"
            ),
            None,
        )
        required_caps = {
            f"CAP:{capability_id}"
            for capability_id in (closure or {}).get("required_capability_ids", [])
        }
        resolved_ids = {resolved.ref_id for resolved in resolved_refs}
        transitive_vga_sources = {"VG-A", "G3", "G4", "G5"}
        if (
            (closure or {}).get("closure_mode") == "capability_activation_receipts"
            and required_caps
            and required_caps <= resolved_ids
            and set(synthetic_sources) <= transitive_vga_sources
        ):
            synthetic_sources = []
    if synthetic_sources:
        if synthetic is not True:
            v.append("synthetic_propagation_required")
        if conclusion in ("release_gate_verified", "READY_FOR_G7_AUTHORIZATION"):
            v.append("synthetic_conclusion_overclaim")
        if synthetic is True and conclusion not in (
            "conservative_unknown",
            "not_run",
            "blocked",
            "failed",
        ):
            v.append("synthetic_conclusion_not_capped")

    # --- G6 freshness -----------------------------------------------------
    if gate_id == "G6":
        for field in G6_ONLY_FIELDS:
            if field not in receipt:
                v.append(f"g6_missing:{field}")
        provenance = receipt.get("verifier_provenance") or {}
        if provenance.get("prior_engagement_ids"):
            v.append("g6_verifier_not_fresh")
        if provenance and not provenance.get("independent_of_producer_org"):
            v.append("g6_verifier_not_independent_of_producer_org")
        attestation = receipt.get("freshness_attestation") or {}
        for flag, value in attestation.items():
            if value is not True:
                v.append(f"g6_attestation_false:{flag}")
        if receipt.get("verifier") in {i for r in resolved_refs for i in r.identities}:
            v.append("g6_verifier_signed_prior_evidence")
        orchestration = _check_orchestration_provenance(receipt, ctx, resolved_refs, v)
        _check_fresh_evidence(receipt, registry, ctx, orchestration, v)
        created = parse_instant(receipt.get("created_at"))
        expires = parse_instant(receipt.get("expires_at"))
        if created and expires:
            delta = expires - created
            if delta <= datetime.timedelta(0):
                v.append("g6_expiry_not_positive")
            elif delta > THIRTY_DAYS:
                v.append("g6_expiry_too_long")
            if expires <= ctx.now:
                v.append("g6_expired")
    else:
        for field in G6_ONLY_FIELDS:
            if field in receipt:
                v.append(f"g6_only_field_on_non_g6:{field}")

    # --- commit / owner scope / environment binding ----------------------
    subject = receipt.get("tested_subject_commit")
    if ctx.repository_facts is not None:
        subject_violations, _evidence_commit, _tree = (
            ctx.repository_facts.validate_release_subject(
                gate_id, registry_gate["receipt_path"], receipt
            )
        )
        v.extend(subject_violations)
    else:
        if subject == ctx.introducing_commit:
            v.append("subject_commit_is_its_own_commit")
        elif subject not in ctx.ancestor_commits:
            v.append("subject_commit_not_strict_ancestor")
        gate_touches = ctx.touched_owner_code_by_gate.get(gate_id)
        if not gate_touches:
            v.append("owner_touch_evidence_unavailable")
        elif subject not in gate_touches:
            v.append("subject_commit_did_not_touch_owner_code")

        # Object-level mutation tests may inject a hermetic world. Canonical
        # current evaluation never uses these maps; it constructs facts from git.
        expected_owner_tree = ctx.owner_scope_tree_for(gate_id)
        if expected_owner_tree is None:
            v.append("owner_scope_tree_unavailable")
        elif receipt.get("owner_scope_tree_sha256") != expected_owner_tree:
            v.append("owner_scope_tree_hash_mismatch")
        if not ctx.observed_environment:
            v.append("observed_environment_unavailable")
        elif not verify_environment_fingerprint_exact(
            receipt.get("environment_fingerprint"), ctx.observed_environment
        ):
            v.append("environment_fingerprint_mismatch")

    # --- sequence chain ---------------------------------------------------
    archive_chain, archive_violations = _archive_chain_from_disk(
        ctx, registry_gate, is_authorization=False
    )
    v.extend(archive_violations)
    sequence = receipt.get("sequence")
    if _historical and isinstance(sequence, int):
        archive_chain = [
            entry
            for entry in archive_chain
            if isinstance(entry.get("sequence"), int)
            and entry["sequence"] < sequence
        ]
    supersedes = receipt.get("supersedes_receipt_sha256")
    if sequence == 1 and supersedes is not None:
        v.append("supersedes_on_first_run")
    if isinstance(sequence, int) and sequence > 1:
        if supersedes is None:
            v.append("supersedes_missing")
        prior = [entry for entry in archive_chain if entry["sequence"] == sequence - 1]
        if not prior:
            # A non-genesis current receipt with no material predecessor is an
            # orphan even when it names a plausible 64-hex supersedes value.
            # The prior implementation only checked a supplied predecessor and
            # therefore accepted sequence=2 with an empty archive.
            v.append("receipt_predecessor_missing")
        elif supersedes is not None:
            if prior[0]["receipt_sha256"] != supersedes:
                v.append("supersedes_mismatch")
            prior_created = parse_instant(prior[0].get("created_at"))
            created = parse_instant(receipt.get("created_at"))
            if prior_created and created and created <= prior_created:
                v.append("created_at_not_strictly_increasing")
    if archive_chain and isinstance(sequence, int):
        seen = sorted(entry["sequence"] for entry in archive_chain)
        if len(seen) != len(set(seen)):
            v.append("chain_duplicate_sequence")
        if seen != list(range(1, sequence)):
            v.append("chain_not_exact_prefix")

    # --- artifacts --------------------------------------------------------
    v.extend(_check_artifacts(receipt, ctx))

    # --- self hash --------------------------------------------------------
    if release_receipt_sha256(receipt) != receipt.get("receipt_sha256"):
        v.append("self_hash_mismatch")

    ctx._release_validation_stack.discard(cycle_key)
    return sorted(v)


def _check_artifacts(receipt, ctx):
    v = []
    artifacts = receipt.get("artifacts", [])
    if not artifacts:
        v.append("artifacts_empty")
    receipt_roots = ctx.resolver.resolution[
        "receipt_roots_that_must_not_contain_evidence_reports"
    ]
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path", "")
        if path_within_any(path, receipt_roots):
            # A gate may not cite gate evidence as its own artifact: the
            # receipt would then be part of the thing it certifies.
            v.append(f"artifact_is_gate_evidence:{path}")
        try:
            _sr.safe_relpath(path, label=path)
        except _sr.SafeReadError:
            # Absolute paths, traversal, dotted components and symlinked
            # ancestors are refused before any read is attempted.
            v.append(f"artifact_unsafe_path:{path}")
            continue
        try:
            raw = _sr.read_checked_bytes(ctx.root, path, missing_ok=True)
        except _sr.SafeReadError:
            v.append(f"artifact_unsafe_path:{path}")
            continue
        if raw is None:
            v.append(f"artifact_missing:{path}")
        elif hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
            v.append(f"artifact_hash_mismatch:{path}")
    return v


# ---------------------------------------------------------------------------
# G7 authorization
# ---------------------------------------------------------------------------


def _verify_detached_signature(auth, ctx, v):
    """Real detached verification against the STORE's key, or a named refusal.

    There is no "could not verify, assume valid" path. A signature that cannot
    be checked is a signature that has not been checked, and the frozen code
    ``signature_verification_missing`` says exactly that rather than letting an
    unverifiable artefact through on a technicality.
    """
    signature_block = auth.get("external_signature")
    if not isinstance(signature_block, dict):
        v.append("signature_verification_missing")
        return

    trust_root_id = signature_block.get("trust_root_id")
    key_id = signature_block.get("key_id")
    if trust_root_id in ctx.project_identities:
        v.append("signer_is_project_or_test_identity")

    record = ctx.trust_store.lookup(trust_root_id, key_id)
    if record is None:
        # Fails closed on the PAIR: an unknown pair is rejected, never treated
        # as an unconstrained key.
        v.append("trust_record_unknown")
        v.append("signature_verification_missing")
        return

    # Every field the artefact carries except signature_b64 is a MIRROR of the
    # record; verification still uses the record.
    for field, expected in (
        ("key_state", record.state),
        ("algorithm", record.algorithm),
        ("key_not_before", record.not_before),
        ("key_expires_at", record.expires_at),
    ):
        if signature_block.get(field) != expected:
            v.append(f"trust_mirror_mismatch:{field}")

    if record.purpose != RELEASE_AUTHORIZATION_PURPOSE:
        # A key issued for code signing or transport identity may not authorise
        # a deployment.
        v.append("trust_purpose_mismatch")
    principal = (auth.get("authorizing_principal") or {}).get("id")
    if record.principal_id != principal:
        v.append("trust_principal_mismatch")
    if record.principal_id in ctx.project_identities:
        v.append("trust_principal_is_project_identity")

    # Validity is judged at the EVALUATION instant, not the signing instant: a
    # key revoked after a genuine signature still rejects, because the question
    # at use time is whether this authorization may be relied on NOW.
    if record.state != "active":
        v.append("trust_key_not_active")
    not_before = parse_instant(record.not_before)
    expires_at = parse_instant(record.expires_at)
    revoked_at = parse_instant(record.revoked_at) if record.revoked_at else None
    if not_before is not None and ctx.now < not_before:
        v.append("trust_key_not_yet_valid")
    if expires_at is not None and ctx.now >= expires_at:
        v.append("trust_key_expired")
    if revoked_at is not None and revoked_at <= ctx.now:
        v.append("trust_key_revoked")

    # Recomputed from the frozen two-key exclusion rule, never read from the
    # artefact: there is no stored payload digest to trust or to disagree with.
    payload = authorization_signed_payload(auth)

    signature_b64 = signature_block.get("signature_b64")
    if not isinstance(signature_b64, str):
        v.append("signature_verification_missing")
        return
    try:
        raw = _sig.base64.b64decode(signature_b64, validate=True)
    except Exception:
        v.append("signature_not_canonical_der")
        return
    if _sig.base64.b64encode(raw).decode("ascii") != signature_b64:
        # Python's strict decoder still accepts redundant terminal padding for
        # some inputs.  A signature must have one canonical textual spelling,
        # otherwise the same DER value produces multiple authorization hashes.
        v.append("signature_base64_noncanonical")
        return
    if not _sig.is_canonical_der(raw):
        # Non-canonical DER is a second spelling of one signature, and because
        # authorization_sha256 covers signature_b64 that is a second hash for
        # one decision.
        v.append("signature_not_canonical_der")
        return
    if not _sig.is_low_s(raw):
        v.append("signature_not_low_s")
        return
    if record.algorithm != _sig.ALGORITHM:
        v.append("signature_algorithm_unverifiable")
        return
    if not _sig.verify(record.public_key, payload, raw):
        v.append("signature_invalid")


def evaluate_release_authorization(auth, schema, ctx, *, _historical=False):
    """Full G7 validation. Returns sorted violation codes; ``[]`` means valid."""
    v = []
    v.extend(structural_errors(auth, schema))

    forbidden = set(schema.get("deepForbiddenKeys", []))
    for key in deep_keys(auth):
        if key in forbidden:
            v.append(f"forbidden_key:{key}")

    if auth.get("schema") != schema["$id"]:
        v.append("schema_id_mismatch")

    decision = auth.get("decision")
    if decision == "withdrawn" and "revocation_ref" not in auth:
        v.append("withdrawn_without_revocation_ref")
    if decision != "withdrawn" and "revocation_ref" in auth:
        v.append("revocation_ref_without_withdrawal")

    _verify_detached_signature(auth, ctx, v)

    # --- the single machine prerequisite ----------------------------------
    ref = auth.get("g6_receipt_ref", {}) or {}
    canonical_g6_path = ctx.resolver.canonical_path("G6")
    g6 = None
    raw_g6 = None
    if ref.get("ref_path") != canonical_g6_path:
        v.append("g6_path_mismatch")
    if ctx._release_version_index is None:
        ctx._release_version_index = ReleaseVersionIndex(ctx)
    selected_g6, version_violations = ctx._release_version_index.resolve(
        "G6",
        canonical_g6_path,
        ref.get("ref_sha256"),
        auth.get("created_at"),
    )
    v.extend(f"g6_{code}" for code in version_violations)
    if selected_g6 is not None:
        raw_g6 = selected_g6["raw"]
        g6 = selected_g6["body"]
    else:
        try:
            current_probe = _sr.read_checked_bytes(
                ctx.root, canonical_g6_path, missing_ok=True
            )
        except _sr.SafeReadError:
            current_probe = None
            v.append("g6_receipt_unreadable")
        if current_probe is None and "g6_receipt_unreadable" not in v:
            v.append("g6_receipt_missing")
    if g6 is None and raw_g6 is not None:
        try:
            parsed_g6 = strict_json_loads(raw_g6.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed_g6 = None
        if not isinstance(parsed_g6, dict):
            v.append("g6_receipt_not_json")
        else:
            g6 = parsed_g6

    if g6 is not None:
        if hashlib.sha256(raw_g6).hexdigest() != ref.get("ref_sha256"):
            v.append("g6_hash_mismatch")
        if g6.get("status") != "pass":
            v.append("g6_not_pass")
        if g6.get("conclusion") != "READY_FOR_G7_AUTHORIZATION":
            v.append("g6_conclusion_not_ready_for_authorization")
        if g6.get("expires_at") != ref.get("g6_expires_at"):
            v.append("g6_expiry_ref_mismatch")
        if g6.get("tested_subject_commit") != ref.get("g6_tested_subject_commit"):
            v.append("g6_subject_commit_ref_mismatch")
        g6_gate = next(
            gate for gate in ctx.resolver.registry["gates"] if gate["gate_id"] == "G6"
        )
        g6_schema = _snapshot_schema(
            ctx, "release_receipt_schema", RELEASE_RECEIPT_SCHEMA_PATH
        )
        nested = evaluate_release_receipt(
            g6,
            g6_gate,
            g6_schema,
            ctx,
            registry=ctx.resolver.registry,
            _historical=not selected_g6["is_current"],
        )
        v.extend(f"g6_invalid:{code}" for code in nested)
    g6_expiry = parse_instant(ref.get("g6_expires_at"))
    if g6_expiry is not None and g6_expiry <= ctx.now:
        v.append("g6_expired")

    # --- target binding ---------------------------------------------------
    binding = auth.get("target_binding", {}) or {}
    if binding.get("target_commit") != ref.get("g6_tested_subject_commit"):
        v.append("target_commit_not_bound_to_g6_subject")
    if ctx.deployment_instance_id is None:
        v.append("deployment_instance_unavailable")
    elif binding.get("instance_id") != ctx.deployment_instance_id:
        v.append("target_instance_mismatch")
    if ctx.deployment_environment is None:
        v.append("deployment_environment_unavailable")
    elif not verify_environment_fingerprint_exact(
        binding.get("environment_fingerprint"), ctx.deployment_environment
    ):
        v.append("environment_fingerprint_mismatch")

    # --- scope / tenant allowlist ----------------------------------------
    scope = auth.get("scope", {}) or {}
    if ctx.deployment_tenant_allowlist is None:
        v.append("deployment_tenant_allowlist_unavailable")
    else:
        expected_digest = tenant_allowlist_sha256(ctx.deployment_tenant_allowlist)
        if scope.get("tenant_allowlist_sha256") != expected_digest:
            v.append("tenant_allowlist_hash_mismatch")
        if scope.get("allowlisted_tenant_count") != len(ctx.deployment_tenant_allowlist):
            v.append("tenant_allowlist_count_mismatch")

    # --- replay / window --------------------------------------------------
    if auth.get("nonce") in ctx.used_nonces:
        v.append("nonce_replay")
    not_before = parse_instant(auth.get("not_before"))
    expires_at = parse_instant(auth.get("expires_at"))
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
    g7_gate = next(
        gate for gate in ctx.resolver.registry["gates"] if gate["gate_id"] == "G7"
    )
    archive_chain, archive_violations = _archive_chain_from_disk(
        ctx, g7_gate, is_authorization=True
    )
    v.extend(archive_violations)
    sequence = auth.get("sequence")
    if _historical and isinstance(sequence, int):
        archive_chain = [
            entry
            for entry in archive_chain
            if isinstance(entry.get("sequence"), int)
            and entry["sequence"] < sequence
        ]
    archived_nonces = [
        entry.get("nonce") for entry in archive_chain if isinstance(entry, dict)
    ]
    if auth.get("nonce") in archived_nonces and "nonce_replay" not in v:
        v.append("nonce_replay")
    if len(archived_nonces) != len(set(archived_nonces)):
        v.append("archive_nonce_reuse")
    supersedes = auth.get("supersedes_authorization_sha256")
    if sequence == 1 and supersedes is not None:
        v.append("supersedes_on_first_authorization")
    if isinstance(sequence, int) and sequence > 1:
        prior = [e for e in archive_chain if e["sequence"] == sequence - 1]
        if supersedes is None or not prior:
            v.append("authorization_predecessor_missing")
        elif prior[0]["authorization_sha256"] != supersedes:
            v.append("authorization_predecessor_missing")
        if decision == "withdrawn" and prior:
            expected = prior[0]["authorization_sha256"]
            if auth.get("revocation_ref") != expected or supersedes != expected:
                v.append("withdrawal_reference_mismatch")
    if archive_chain and isinstance(sequence, int):
        seen = sorted(entry["sequence"] for entry in archive_chain)
        if len(seen) != len(set(seen)):
            v.append("chain_duplicate_sequence")
        if seen != list(range(1, sequence)):
            v.append("chain_not_exact_prefix")

    # --- clerical recorder ------------------------------------------------
    principal = (auth.get("authorizing_principal", {}) or {}).get("id")
    recorder = auth.get("recorded_by")
    if recorder == principal:
        v.append("recorder_is_the_authorizing_principal")
    if recorder in ctx.beneficiary_identities:
        v.append("recorder_is_the_beneficiary")
    if recorder in ctx.rt026_identities:
        v.append("recorder_is_rt026_identity")

    if release_auth_sha256(auth) != auth.get("authorization_sha256"):
        v.append("self_hash_mismatch")

    return sorted(v)


# ---------------------------------------------------------------------------
# Receipt root closure and archive validation, on the fail-closed reader
# ---------------------------------------------------------------------------


def evaluate_release_root_closure(
    repo_root: pathlib.Path, registry: dict, *, context=None
):
    """Whole-subtree closure over ``release-gate-receipts/``.

    Enumeration goes through :func:`pr001_safe_read.directory_snapshot`, which
    requires the caller to DECLARE each directory's filename grammar before it
    is allowed to look. That is the difference from the previous
    :func:`os.walk` version: a stray ``junk.txt``, a dotfile, a symlinked
    component or leaf, a hardlinked entry, a special file, a name that aliases
    another under NFC+casefold, or an entry rewritten mid-read all make the
    directory UNVERIFIABLE inside the reader, instead of being filtered out by
    a ``*.json`` glob and never reaching this function.

    ``repo_root`` is the repository root: every path in the registry is
    repo-relative, and the reader resolves each component beneath it, so there
    is exactly one base and no second path convention to get wrong.

    A missing root is NOT_RUN, which is not a violation.
    """
    repo_root = pathlib.Path(repo_root)
    receipt_root_rel = registry["receipt_root"].rstrip("/")
    v = []

    try:
        top = _sr.directory_snapshot(
            repo_root,
            receipt_root_rel,
            name_pattern=RELEASE_ROOT_NAME_PATTERN,
            allow_dirs=True,
            missing_ok=True,
        )
    except _sr.SafeReadError:
        return ["unverifiable_directory:."]
    if top is None:
        return v
    if top.files:
        # Only gate directories live at the top level; a file here is neither a
        # receipt nor an archive member.
        for name in sorted(top.files):
            v.append(f"undeclared_file:{name}")

    gates = {g["gate_id"]: g for g in registry["gates"]}
    for gate_id in sorted(top.dirs):
        gate = gates.get(gate_id)
        if gate is None:
            v.append(f"undeclared_directory:{gate_id}")
            continue
        receipt_name = pathlib.PurePosixPath(gate["receipt_path"]).name
        archive_name = pathlib.PurePosixPath(gate["archive_dir"].rstrip("/")).name
        grammar = re.compile(rf"{re.escape(receipt_name)}|{re.escape(archive_name)}")
        try:
            snapshot = _sr.directory_snapshot(
                repo_root,
                f"{receipt_root_rel}/{gate_id}",
                name_pattern=grammar,
                allow_dirs=True,
                missing_ok=True,
            )
        except _sr.SafeReadError:
            v.append(f"unverifiable_directory:{gate_id}")
            continue
        if snapshot is None:
            continue
        for name in sorted(snapshot.dirs):
            if name != archive_name:
                v.append(f"undeclared_directory:{gate_id}/{name}")
        current = snapshot.files.get(receipt_name)
        v.extend(_validate_current_receipt(registry, gate, current, context))
        if archive_name in snapshot.dirs:
            v.extend(
                _validate_archive(
                    repo_root,
                    registry,
                    gate,
                    gate_id,
                    receipt_root_rel,
                    archive_name,
                    current,
                    context,
                )
            )
    return sorted(v)


def _validate_current_receipt(registry, gate, current_bytes, context):
    """Validate the current tip even when the gate has no archive directory."""
    if current_bytes is None:
        return []
    gate_id = gate["gate_id"]
    is_authorization = gate_id == "G7"
    schema = _snapshot_schema(
        context,
        (
            "release_authorization_schema"
            if is_authorization
            else "release_receipt_schema"
        ),
        RELEASE_AUTH_SCHEMA_PATH if is_authorization else RELEASE_RECEIPT_SCHEMA_PATH,
    ) if context is not None else load_json(
        RELEASE_AUTH_SCHEMA_PATH if is_authorization else RELEASE_RECEIPT_SCHEMA_PATH
    )
    recompute = release_auth_sha256 if is_authorization else release_receipt_sha256
    hash_field = "authorization_sha256" if is_authorization else "receipt_sha256"
    try:
        body = strict_json_loads(current_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [f"current_not_json:{gate_id}"]
    if not isinstance(body, dict):
        return [f"current_not_json:{gate_id}"]

    v = [f"current_schema:{gate_id}:{code}" for code in structural_errors(body, schema)]
    if body.get(hash_field) != recompute(body):
        v.append(f"current_self_hash_mismatch:{gate_id}")
    if body.get("gate_id") != gate_id:
        v.append(f"current_gate_id_mismatch:{gate_id}")
    if context is None:
        v.append(f"current_semantic_context_missing:{gate_id}")
    elif is_authorization:
        semantic = evaluate_release_authorization(body, schema, context)
        v.extend(f"current_semantic:{gate_id}:{code}" for code in semantic)
    else:
        semantic = evaluate_release_receipt(
            body, gate, schema, context, registry=registry
        )
        v.extend(f"current_semantic:{gate_id}:{code}" for code in semantic)
    return v


def evaluate_current_gate(gate_id: str, ctx):
    """Production-shaped entrypoint for one canonical current G1..G7 file.

    The object evaluators remain useful for mutation tests and recursive
    prerequisite validation, but callers deciding release state must not pass
    an in-memory dictionary that was never materialised at the registry path.
    This entrypoint binds exact path, fail-closed bytes, whole-root closure and
    the family semantic evaluator in one call.
    """
    try:
        facts = ReleaseRepositoryFacts(ctx.root, ctx.evaluation_commit)
    except EvidenceBindingError as exc:
        return [f"repository_facts_unavailable:{exc}"]
    try:
        if worktree_is_dirty(facts.git):
            return ["repository_worktree_dirty"]
    except EvidenceBindingError as exc:
        return [f"repository_facts_unavailable:{exc}"]
    # Canonical-current evaluation never trusts caller-supplied ancestry/tree/
    # tracking maps.  The release DAG/path policy itself is equally
    # non-overridable: it must be the exact registry blob at the explicit Git
    # snapshot, not a structurally plausible resolver supplied by the caller.
    if getattr(ctx.resolver, "registry", None) != facts.registry:
        return ["repository_registry_override"]
    policy_surfaces = (
        (
            "verification_registry",
            "verification_registry",
            facts.verification_registry,
        ),
        ("capability_map", "capability_map", facts.capability_map),
        ("security_registry", "security_registry", facts.security_registry),
    )
    for code, attribute, authoritative in policy_surfaces:
        if getattr(ctx.resolver, attribute, None) != authoritative:
            return [f"repository_policy_override:{code}"]

    registry_errors = structural_errors(facts.registry, facts.release_registry_schema)
    if registry_errors:
        return [f"repository_registry_schema:{code}" for code in registry_errors]
    delegated_policy_surfaces = (
        (
            "verification_registry",
            facts.verification_registry,
            facts.verification_registry_schema,
        ),
        ("capability_map", facts.capability_map, facts.capability_map_schema),
        ("security_registry", facts.security_registry, facts.security_registry_schema),
    )
    for name, document, schema in delegated_policy_surfaces:
        errors = structural_errors(document, schema)
        if errors:
            return [f"repository_{name}_schema:{code}" for code in errors]

    authoritative_resolver = PrerequisiteResolver(
        facts.registry,
        verification_registry=facts.verification_registry,
        capability_map=facts.capability_map,
        security_registry=facts.security_registry,
    )
    ctx = _canonical_session_from(ctx, facts, authoritative_resolver)
    delegated_closure = evaluate_delegated_root_closure(ctx)

    gate = next(
        (item for item in ctx.resolver.registry["gates"] if item["gate_id"] == gate_id),
        None,
    )
    if gate is None:
        return [f"current_gate_unknown:{gate_id}"]
    rel_path = gate["receipt_path"]
    try:
        raw = _sr.read_checked_bytes(ctx.root, rel_path, missing_ok=True)
    except _sr.SafeReadError:
        return [f"current_unreadable:{gate_id}"]
    if raw is None:
        return [f"current_missing:{gate_id}"]
    if not facts.tracked_blob_matches(rel_path):
        return [f"current_untracked_or_blob_drift:{gate_id}"]
    try:
        body = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [f"current_not_json:{gate_id}"]
    if not isinstance(body, dict):
        return [f"current_not_json:{gate_id}"]

    if gate_id == "G7":
        schema = facts.release_authorization_schema
        semantic = evaluate_release_authorization(body, schema, ctx)
    else:
        schema = facts.release_receipt_schema
        semantic = evaluate_release_receipt(
            body, gate, schema, ctx, registry=ctx.resolver.registry
        )
    closure = evaluate_release_root_closure(
        ctx.root, ctx.resolver.registry, context=ctx
    )
    duplicate_prefixes = (
        f"current_semantic:{gate_id}:",
        f"current_schema:{gate_id}:",
        f"current_self_hash_mismatch:{gate_id}",
        f"current_gate_id_mismatch:{gate_id}",
    )
    closure_only = [
        code for code in closure if not code.startswith(duplicate_prefixes)
    ]
    return sorted(set(semantic + closure_only + delegated_closure))


def evaluate_current_authorization(ctx):
    """Canonical G7 authorization entrypoint; absence means NOT_RUN."""
    return evaluate_current_gate("G7", ctx)


def _validate_archive(
    repo_root,
    registry,
    gate,
    gate_id,
    receipt_root_rel,
    archive_name,
    current_bytes,
    context,
):
    """Archive members are receipts, not blobs.

    Each entry must independently pass self-hash recomputation, gate_id
    agreement with the directory it sits in, and filename-equals-own-hash - so
    a correct receipt filed under a forged name and a forged receipt filed
    under a correct name both fail. The chain must then be a single unique
    PREFIX walk: current sequence N means exactly the sequences 1..N-1, one
    each, no gap, no duplicate, no fork, no orphan.
    """
    v = []
    rel = f"{receipt_root_rel}/{gate_id}/{archive_name}"
    try:
        archive = _sr.directory_snapshot(
            repo_root,
            rel,
            name_pattern=ARCHIVE_NAME_PATTERN,
            allow_dirs=False,
            missing_ok=True,
        )
    except _sr.SafeReadError:
        return [f"unverifiable_directory:{gate_id}/{archive_name}"]
    if archive is None:
        return v

    if current_bytes is None and archive.files:
        # A history with no present is an orphan chain.
        v.append(f"archive_without_current:{gate_id}")

    is_authorization = gate["gate_id"] == "G7"
    hash_field = "authorization_sha256" if is_authorization else "receipt_sha256"
    supersedes_field = (
        "supersedes_authorization_sha256"
        if is_authorization
        else "supersedes_receipt_sha256"
    )
    recompute = release_auth_sha256 if is_authorization else release_receipt_sha256
    schema = _snapshot_schema(
        context,
        (
            "release_authorization_schema"
            if is_authorization
            else "release_receipt_schema"
        ),
        RELEASE_AUTH_SCHEMA_PATH if is_authorization else RELEASE_RECEIPT_SCHEMA_PATH,
    ) if context is not None else load_json(
        RELEASE_AUTH_SCHEMA_PATH if is_authorization else RELEASE_RECEIPT_SCHEMA_PATH
    )

    current_hash = None
    if current_bytes is not None:
        try:
            current_body = strict_json_loads(current_bytes.decode("utf-8"))
            if not isinstance(current_body, dict):
                raise ValueError("current receipt is not an object")
            current_hash = current_body.get(hash_field)
            current_sequence = current_body.get("sequence")
        except (UnicodeDecodeError, ValueError):
            current_body, current_sequence = None, None
    else:
        current_body, current_sequence = None, None

    entries = []
    for name in sorted(archive.files):
        member_rel = f"{gate_id}/{archive_name}/{name}"
        source_rel = f"{receipt_root_rel}/{member_rel}"
        raw = archive.files[name]
        if (
            context is not None
            and context.repository_facts is not None
            and not context.repository_facts.tracked_blob_matches(source_rel)
        ):
            v.append(f"archive_untracked_or_blob_drift:{member_rel}")
        try:
            body = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            v.append(f"archive_member_not_json:{member_rel}")
            continue
        if not isinstance(body, dict):
            v.append(f"archive_member_not_json:{member_rel}")
            continue
        structural = structural_errors(body, schema)
        if structural:
            v.extend(
                f"archive_schema:{member_rel}:{code}" for code in structural
            )
            continue
        recomputed = recompute(body)
        if body.get(hash_field) != recomputed:
            v.append(f"archive_member_self_hash_mismatch:{member_rel}")
            continue
        if name != f"{recomputed}.json":
            v.append(f"archive_member_misnamed:{member_rel}")
        if body.get("gate_id") != gate_id:
            v.append(f"archive_member_gate_id_mismatch:{member_rel}")
        if current_hash is not None and recomputed == current_hash:
            # An archived copy of the live receipt is either a duplicate tip or
            # a rollback staged to look like history.
            v.append(f"archive_contains_current_tip:{member_rel}")
        entries.append((body.get("sequence"), body, member_rel))

        if context is None:
            v.append(f"archive_semantic_context_missing:{member_rel}")
        elif is_authorization:
            semantic = evaluate_release_authorization(
                body, schema, context, _historical=True
            )
            v.extend(f"archive_semantic:{member_rel}:{code}" for code in semantic)
        else:
            semantic = evaluate_release_receipt(
                body,
                gate,
                schema,
                context,
                registry=registry,
                _historical=True,
            )
            v.extend(f"archive_semantic:{member_rel}:{code}" for code in semantic)

    sequences = sorted(seq for seq, _, _ in entries if isinstance(seq, int))
    if len(sequences) != len(set(sequences)):
        v.append("chain_duplicate_sequence")
    if isinstance(current_sequence, int):
        if sequences != list(range(1, current_sequence)):
            v.append("chain_not_exact_prefix")
    elif sequences:
        v.append("chain_not_exact_prefix")

    ordered = sorted(
        (
            (seq, body, rel_)
            for seq, body, rel_ in entries
            if isinstance(seq, int)
        ),
        key=lambda item: (item[0], item[2]),
    )
    previous_created = None
    previous_hash = None
    for seq, body, member_rel in ordered:
        created = parse_instant(body.get("created_at"))
        if previous_created is not None and created is not None and created <= previous_created:
            v.append(f"archive_created_at_not_increasing:{member_rel}")
        previous_created = created if created is not None else previous_created
        supersedes = body.get(supersedes_field)
        if seq == 1:
            if supersedes is not None:
                v.append(f"archive_supersedes_on_genesis:{member_rel}")
        elif previous_hash is None or supersedes != previous_hash:
            v.append(f"archive_supersedes_mismatch:{member_rel}")
        previous_hash = body.get(hash_field)

    if isinstance(current_sequence, int) and isinstance(current_body, dict):
        current_supersedes = current_body.get(supersedes_field)
        if current_sequence == 1:
            if current_supersedes is not None:
                v.append(f"current_supersedes_on_genesis:{gate_id}")
        elif previous_hash is None or current_supersedes != previous_hash:
            v.append(f"current_supersedes_mismatch:{gate_id}")
    return v


def read_release_receipt(root: pathlib.Path, rel: str):
    """Read one receipt through the fail-closed reader, or ``None``."""
    try:
        return _sr.read_checked_json(root, rel)
    except _sr.SafeReadError:
        return None
