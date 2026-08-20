"""PR-001 verification-gate contract tests (VG-A~VG-E).

Validates five things RT-024 / RT-026 depend on:

1. the frozen unified gate receipt schema
   (`cwk.pr001.verification_gate_receipt.v1`);
2. the gate registry, which is *configuration only* and must never carry a
   mutable execution status;
3. the VG-A machine receipt actually on disk, including its
   domain-separated receipt_sha256 and its synthetic/conservative_unknown cap;
4. the synthetic closure map, which declares how a permanently-synthetic
   gate's capability gap can be closed -- also configuration only, carrying
   neither a verdict nor a point-in-time observation;
5. the capability activation receipt schema
   (`cwk.pr001.capability_activation_receipt.v1`), the only artifact that can
   close such a gap.

Pure stdlib (no jsonschema dependency): we assert the structural invariants
that matter for the anti-cycle and single-owner rules and re-derive the
receipt hash independently.

VG-A's PASS covers the synthetic host chain only, so its conclusion is capped
at `conservative_unknown`. That cap is permanent: VG-A is never re-run,
re-signed or reinterpreted. What can change is the *capability gap* it names,
which closes only via two separately owned activation receipts (RT-017's
cwork-authority-source and RT-023's gateway-identity-transport). Without that
declared exit, "any synthetic hard gate => NO_GO" would make
READY_FOR_G7_REVIEW unreachable forever, so `ClosureEvaluationRegressionTests`
proves both directions: fail-closed today, reachable once real receipts land.

This file is a *forward-compatible* contract test. It deliberately does NOT
freeze which gates have already run, nor which activation receipts exist:
VG-B~VG-E receipts may legally appear at any time once their owner RT passes
independent acceptance, and either activation receipt may appear once RT-017 /
RT-023 deliver. The invariants are that VG-A exists now, that every receipt
present on disk sits at a registry- or map-declared path, that every present
receipt satisfies the same structural / semantic / hash / artifact rules, and
that OPEN <=> NO_GO holds whatever today's disk state happens to be.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import platform
import re
import stat
import sys
import tempfile
import unicodedata
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pr001_safe_read as _sr  # noqa: E402
from pr001_evidence_binding import (  # noqa: E402
    EvaluationClock,
    GitSubject,
    commit_all,
    init_fixture_repo,
    parse_instant,
    probe_signature,
    resolve_evidence_commit,
    verify_environment_fingerprint,
    verify_probe_manifest,
    verify_subject_commit,
    worktree_is_dirty,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PR_ROOT = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces"
GATES_DIR = PR_ROOT / "contracts" / "gates"
RECEIPT_SCHEMA_PATH = GATES_DIR / "verification_gate_receipt_v1.schema.json"
REGISTRY_PATH = GATES_DIR / "gate_registry_v1.json"
REGISTRY_SCHEMA_PATH = GATES_DIR / "gate_registry_v1.schema.json"
CLOSURE_MAP_PATH = GATES_DIR / "synthetic_closure_map_v1.json"
CLOSURE_MAP_SCHEMA_PATH = GATES_DIR / "synthetic_closure_map_v1.schema.json"
ACTIVATION_SCHEMA_PATH = GATES_DIR / "capability_activation_receipt_v1.schema.json"

CONTRACT_FILES = (
    RECEIPT_SCHEMA_PATH,
    REGISTRY_PATH,
    REGISTRY_SCHEMA_PATH,
    CLOSURE_MAP_PATH,
    CLOSURE_MAP_SCHEMA_PATH,
    ACTIVATION_SCHEMA_PATH,
)
CONTRACT_SCHEMAS = (
    RECEIPT_SCHEMA_PATH,
    REGISTRY_SCHEMA_PATH,
    CLOSURE_MAP_SCHEMA_PATH,
    ACTIVATION_SCHEMA_PATH,
)

FROZEN_FEEDER = {
    "VG-A": "RT-015",
    "VG-B": "RT-018",
    "VG-C": "RT-021",
    "VG-D": "RT-023",
    "VG-E": "RT-025",
}
GATE_ORDER = ("VG-A", "VG-B", "VG-C", "VG-D", "VG-E")
DOWNSTREAM_ONLY = ("G6", "G7", "RT-026")
RECEIPT_DOMAIN = b"cwk-verification-gate-receipt-v1\x00"
MUTABLE_STATUS_KEYS = (
    "status",
    "conclusion",
    "verdict",
    "result",
    "passed",
    "last_run_at",
    "receipt_sha256",
    "tests_run",
)

# The lowest RT ordinal in the id space (RT-011..RT-025).
FIRST_RT = 11

# Release gate -> the VG it consumes. A gate that consumes VG-x is by definition
# NOT upstream of VG-x, so it must not appear in VG-x's prerequisite allowlist.
# Source: plans/开发计划.md §"三轴" (G3 consumes VG-A, G4 consumes VG-C,
# G5 consumes VG-D).
RELEASE_GATE_CONSUMES = {"G3": "VG-A", "G4": "VG-C", "G5": "VG-D"}

# Highest release gate that has already been signed off by the time each VG runs.
# Independently derived from the same plan section:
#   G0 docs, G1 RT-011, G2 RT-012~013, G3 RT-014~016, G4 RT-019~021, G5 RT-022~023.
MAX_UPSTREAM_RELEASE_GATE = {
    "VG-A": 2,  # G3 consumes VG-A
    "VG-B": 3,
    "VG-C": 3,  # G4 consumes VG-C
    "VG-D": 4,  # G5 consumes VG-D
    "VG-E": 5,
}


def _derive_allowed_prerequisite_ids(gate_id: str) -> set:
    """Independently derive a gate's prerequisite allowlist from the rank rule.

    Three constraints and nothing else:
      1. RT rank    - no RT above the gate's own feeder_rt;
      2. VG rank    - strictly earlier gates only (never self, never later);
      3. G rank     - only release gates already upstream at that point.
    """
    feeder_ordinal = int(FROZEN_FEEDER[gate_id].split("-")[1])
    allowed = {f"RT-{n:03d}" for n in range(FIRST_RT, feeder_ordinal + 1)}
    allowed |= {f"G{n}" for n in range(0, MAX_UPSTREAM_RELEASE_GATE[gate_id] + 1)}
    allowed |= set(GATE_ORDER[: GATE_ORDER.index(gate_id)])
    return allowed


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_receipt_sha256(receipt: dict) -> str:
    """Re-derive the domain-separated receipt hash from the record body."""
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
    return hashlib.sha256(RECEIPT_DOMAIN + payload).hexdigest()


def _iter_patterns(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _iter_patterns(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_patterns(item)


class GateContractFilesTests(unittest.TestCase):
    def test_all_contract_files_exist(self) -> None:
        for path in CONTRACT_FILES:
            self.assertTrue(path.is_file(), f"missing contract file: {path}")

    def test_files_are_valid_utf8_json(self) -> None:
        for path in CONTRACT_FILES:
            self.assertIsInstance(_load(path), dict, path.name)

    def test_every_pattern_compiles(self) -> None:
        for path in CONTRACT_SCHEMAS:
            for pattern in _iter_patterns(_load(path)):
                try:
                    re.compile(pattern)
                except re.error as exc:  # pragma: no cover - failure detail
                    self.fail(f"{path.name}: bad pattern {pattern!r}: {exc}")


class ReceiptSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load(RECEIPT_SCHEMA_PATH)

    def test_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.schema["$id"], "cwk.pr001.verification_gate_receipt.v1")
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(self.schema["unevaluatedProperties"], False)

    def test_gate_and_feeder_enums_match_frozen_mapping(self) -> None:
        props = self.schema["properties"]
        self.assertEqual(sorted(props["gate_id"]["enum"]), sorted(FROZEN_FEEDER))
        self.assertEqual(
            sorted(props["feeder_rt"]["enum"]), sorted(set(FROZEN_FEEDER.values()))
        )

    def test_consumers_allowlist_is_exactly_rt024_rt026(self) -> None:
        items = self.schema["properties"]["consumers"]["items"]
        self.assertEqual(sorted(items["enum"]), ["RT-024", "RT-026"])

    def test_prerequisite_ref_pattern_excludes_downstream_ids(self) -> None:
        pattern = self.schema["properties"]["prerequisite_refs"]["items"]["properties"][
            "ref_id"
        ]["pattern"]
        rx = re.compile(pattern)
        for bad in DOWNSTREAM_ONLY:
            self.assertIsNone(
                rx.fullmatch(bad),
                f"{bad} must be structurally excluded from VG prerequisite refs",
            )
        for good in ("RT-015", "RT-023", "RT-025", "G0", "G5", "VG-A", "VG-E"):
            self.assertIsNotNone(rx.fullmatch(good), good)

    def test_artifact_path_pattern_rejects_absolute_and_traversal(self) -> None:
        pattern = self.schema["properties"]["artifacts"]["items"]["properties"]["path"][
            "pattern"
        ]
        rx = re.compile(pattern)
        for bad in ("/etc/passwd", "../../secrets", "a/../../b", "/Users/evan/x"):
            self.assertIsNone(rx.fullmatch(bad), f"must reject {bad!r}")
        for good in ("tests/test_vga_shared_canonical.py", "PR/PR-001-a/b.md"):
            self.assertIsNotNone(rx.fullmatch(good), good)

    def test_required_fields_present(self) -> None:
        required = set(self.schema["required"])
        for field in (
            "gate_id",
            "feeder_rt",
            "feeder_rt_independent_pass",
            "status",
            "conclusion",
            "synthetic",
            "consumers",
            "prerequisite_refs",
            "receipt_sha256",
        ):
            self.assertIn(field, required)

    def test_status_and_conclusion_enums(self) -> None:
        props = self.schema["properties"]
        self.assertEqual(
            sorted(props["status"]["enum"]),
            ["fail", "implementation_done", "not_run", "pass"],
        )
        self.assertIn("conservative_unknown", props["conclusion"]["enum"])
        self.assertIn("not_run", props["conclusion"]["enum"])

    def test_deep_forbidden_keys_cover_secrets_and_bodies(self) -> None:
        forbidden = set(self.schema["deepForbiddenKeys"])
        for key in ("tenant_id", "agent_id", "credential", "token", "body", "raw", "prompt"):
            self.assertIn(key, forbidden)

    def test_semantic_rules_encode_anticycle_and_synthetic_caps(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("G6", rules)
        self.assertIn("RT-026", rules)
        self.assertIn("feeder_rt_independent_pass=true", rules)
        self.assertIn("synthetic=true", rules)
        self.assertIn("integration_verified", rules)

    def test_semantic_rules_forbid_forward_prerequisite_refs(self) -> None:
        """The id-space pattern alone permits VG-B->VG-C; the rules must not."""
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("allowed_prerequisite_ids", rules)
        self.assertIn("gate_registry_v1.json", rules)
        self.assertIn("feeder_rt", rules)
        self.assertIn("later gate", rules)
        self.assertIn("forward reference", rules)

    def test_semantic_rules_require_unique_ref_id(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("ref_id must be unique", rules)
        self.assertIn("ref_sha256", rules)

    def test_prerequisite_refs_uniqueitems_alone_is_documented_as_insufficient(self) -> None:
        description = self.schema["properties"]["prerequisite_refs"]["description"]
        self.assertIn("uniqueItems", description)
        self.assertIn("allowed_prerequisite_ids", description)


class GateRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.gates = self.registry["gates"]

    def test_registry_points_at_the_single_receipt_schema(self) -> None:
        self.assertEqual(
            self.registry["receipt_schema_id"], "cwk.pr001.verification_gate_receipt.v1"
        )
        ref = REPO_ROOT / self.registry["receipt_schema_ref"]
        self.assertTrue(ref.is_file(), f"receipt_schema_ref not found: {ref}")
        self.assertEqual(ref.resolve(), RECEIPT_SCHEMA_PATH.resolve())

    def test_registry_carries_no_mutable_execution_status(self) -> None:
        """The registry is frozen config; status lives only in receipts."""
        for key in MUTABLE_STATUS_KEYS:
            self.assertNotIn(key, self.registry, f"top-level mutable key {key!r}")
        for gate in self.gates:
            for key in MUTABLE_STATUS_KEYS:
                self.assertNotIn(
                    key, gate, f"{gate['gate_id']} carries mutable key {key!r}"
                )

    def test_registry_declares_status_resolution_rule(self) -> None:
        rule = self.registry["status_resolution_rule"]
        self.assertIn("receipt_path", rule)
        self.assertIn("NOT RUN", rule)

    def test_static_notes_do_not_describe_run_outcomes(self) -> None:
        banned = ("已通过", "已 PASS", "本次运行", "运行结果", "tests_run")
        for gate in self.gates:
            note = gate["static_note"]
            for token in banned:
                self.assertNotIn(token, note, f"{gate['gate_id']} note is not static")

    def test_exactly_five_unique_gates(self) -> None:
        self.assertEqual(len(self.gates), 5)
        ids = [g["gate_id"] for g in self.gates]
        self.assertEqual(sorted(ids), sorted(FROZEN_FEEDER))
        self.assertEqual(len(set(ids)), 5)

    def test_feeder_rt_mapping_is_frozen_and_unique(self) -> None:
        feeders = [g["feeder_rt"] for g in self.gates]
        self.assertEqual(len(set(feeders)), 5, "feeder RTs must be 1:1 with gates")
        for gate in self.gates:
            self.assertEqual(
                gate["feeder_rt"],
                FROZEN_FEEDER[gate["gate_id"]],
                f"{gate['gate_id']} feeder drift",
            )

    def test_receipt_paths_are_unique_and_match_gate_id(self) -> None:
        paths = [g["receipt_path"] for g in self.gates]
        self.assertEqual(len(set(paths)), 5)
        for gate in self.gates:
            self.assertEqual(
                gate["receipt_path"],
                f"PR/PR-001-multitenant-knowledge-spaces/gate-receipts/"
                f"{gate['gate_id']}/receipt.json",
            )

    def test_consumers_never_include_upstream_or_downstream_only_ids(self) -> None:
        for gate in self.gates:
            for consumer in gate["consumers"]:
                self.assertIn(consumer, ("RT-024", "RT-026"))
            self.assertNotIn(gate["feeder_rt"], gate["consumers"])

    def test_downstream_only_ids_are_declared(self) -> None:
        self.assertEqual(list(self.registry["downstream_only_ids"]), list(DOWNSTREAM_ONLY))

    def test_declared_narrative_refs_exist_on_disk(self) -> None:
        for gate in self.gates:
            for ref in gate["narrative_refs"]:
                self.assertTrue(
                    (REPO_ROOT / ref).is_file(), f"{gate['gate_id']} narrative missing: {ref}"
                )

    def test_definition_refs_point_into_the_plan(self) -> None:
        plan = PR_ROOT / "plans" / "开发计划.md"
        self.assertTrue(plan.is_file())
        for gate in self.gates:
            self.assertTrue(
                gate["definition_ref"].startswith(
                    "PR/PR-001-multitenant-knowledge-spaces/plans/"
                ),
                gate["gate_id"],
            )

    def test_vga_static_note_caps_conclusion_at_conservative_unknown(self) -> None:
        vga = next(g for g in self.gates if g["gate_id"] == "VG-A")
        self.assertIn("synthetic", vga["static_note"])
        self.assertIn("conservative_unknown", vga["static_note"])

    def test_registry_declares_prerequisite_rank_rule(self) -> None:
        rule = self.registry["prerequisite_rank_rule"]
        self.assertIn("allowed_prerequisite_ids", rule)
        self.assertIn("feeder_rt", rule)
        self.assertIn("ref_id must be unique", rule)

    def test_allowed_prerequisite_ids_match_independently_derived_rank_rule(self) -> None:
        """The frozen allowlist must equal the rank rule re-derived from scratch."""
        for gate in self.gates:
            gate_id = gate["gate_id"]
            with self.subTest(gate=gate_id):
                self.assertEqual(
                    set(gate["allowed_prerequisite_ids"]),
                    _derive_allowed_prerequisite_ids(gate_id),
                    f"{gate_id} allowlist drifted from the frozen rank rule",
                )

    def test_allowed_prerequisite_ids_reject_rt_above_the_feeder(self) -> None:
        for gate in self.gates:
            feeder_ordinal = int(gate["feeder_rt"].split("-")[1])
            for ref in gate["allowed_prerequisite_ids"]:
                if ref.startswith("RT-"):
                    self.assertLessEqual(
                        int(ref.split("-")[1]),
                        feeder_ordinal,
                        f"{gate['gate_id']} may not cite {ref} (above {gate['feeder_rt']})",
                    )

    def test_allowed_prerequisite_ids_reject_self_and_future_gates(self) -> None:
        for gate in self.gates:
            gate_id = gate["gate_id"]
            rank = GATE_ORDER.index(gate_id)
            for ref in gate["allowed_prerequisite_ids"]:
                if ref.startswith("VG-"):
                    self.assertLess(
                        GATE_ORDER.index(ref),
                        rank,
                        f"{gate_id} may not cite same/future gate {ref}",
                    )

    def test_allowed_prerequisite_ids_reject_release_gates_that_consume_them(self) -> None:
        for gate in self.gates:
            gate_id = gate["gate_id"]
            for release_gate, consumed in RELEASE_GATE_CONSUMES.items():
                if consumed == gate_id:
                    self.assertNotIn(
                        release_gate,
                        gate["allowed_prerequisite_ids"],
                        f"{release_gate} consumes {gate_id}; citing it recreates a cycle",
                    )

    def test_allowed_prerequisite_ids_exclude_downstream_only_ids(self) -> None:
        for gate in self.gates:
            self.assertEqual(
                set(gate["allowed_prerequisite_ids"]) & set(DOWNSTREAM_ONLY),
                set(),
                gate["gate_id"],
            )

    def test_allowed_prerequisite_ids_are_monotonic_along_the_wave_order(self) -> None:
        by_gate = {g["gate_id"]: set(g["allowed_prerequisite_ids"]) for g in self.gates}
        for previous, current in zip(GATE_ORDER, GATE_ORDER[1:]):
            expected_floor = by_gate[previous] | {previous}
            self.assertTrue(
                expected_floor <= by_gate[current],
                f"{current} allowlist lost ids that {previous} already had",
            )

    def test_allowed_prerequisite_ids_are_sorted_and_unique(self) -> None:
        for gate in self.gates:
            ids = gate["allowed_prerequisite_ids"]
            self.assertEqual(len(ids), len(set(ids)), gate["gate_id"])
            self.assertEqual(ids, sorted(ids), f"{gate['gate_id']} allowlist not sorted")

    def test_each_gate_may_cite_its_own_feeder_rt(self) -> None:
        for gate in self.gates:
            self.assertIn(
                gate["feeder_rt"], gate["allowed_prerequisite_ids"], gate["gate_id"]
            )


class RegistrySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load(REGISTRY_SCHEMA_PATH)
        self.registry = _load(REGISTRY_PATH)

    def test_registry_top_level_keys_match_schema(self) -> None:
        self.assertEqual(sorted(self.registry), sorted(self.schema["properties"]))
        self.assertEqual(sorted(self.schema["required"]), sorted(self.schema["properties"]))

    def test_gate_entry_keys_match_schema(self) -> None:
        item_props = sorted(self.schema["properties"]["gates"]["items"]["properties"])
        for gate in self.registry["gates"]:
            self.assertEqual(sorted(gate), item_props, gate["gate_id"])

    def test_schema_forbids_mutable_status_keys(self) -> None:
        forbidden = set(self.schema["forbiddenEntryKeys"])
        for key in ("status", "conclusion", "verdict", "last_run_at"):
            self.assertIn(key, forbidden)
        allowed = set(self.schema["properties"]["gates"]["items"]["properties"])
        self.assertEqual(forbidden & allowed, set(), "forbidden key is also allowed")

    def test_registry_values_satisfy_schema_patterns(self) -> None:
        item_props = self.schema["properties"]["gates"]["items"]["properties"]
        for gate in self.registry["gates"]:
            for key, spec in item_props.items():
                value = gate[key]
                if "pattern" in spec:
                    self.assertIsNotNone(
                        re.compile(spec["pattern"]).search(value),
                        f"{gate['gate_id']}.{key}={value!r}",
                    )
                if "enum" in spec:
                    self.assertIn(value, spec["enum"], f"{gate['gate_id']}.{key}")
                if spec.get("type") == "array":
                    self.assertIsInstance(value, list)
                    inner = spec.get("items", {})
                    for element in value:
                        if "enum" in inner:
                            self.assertIn(element, inner["enum"])
                        if "pattern" in inner:
                            self.assertIsNotNone(
                                re.compile(inner["pattern"]).search(element), element
                            )

    def test_registry_schema_freezes_five_entries(self) -> None:
        gates_spec = self.schema["properties"]["gates"]
        self.assertEqual(gates_spec["minItems"], 5)
        self.assertEqual(gates_spec["maxItems"], 5)
        self.assertIs(gates_spec["items"]["additionalProperties"], False)


class ReceiptChecks:
    """Reusable receipt validators, shared by on-disk and synthetic receipts.

    Every rule here applies to any VG-A..VG-E receipt, whenever it appears. The
    `root` argument lets the same checks run against a temporary tree so a
    not-yet-created VG-B~VG-E receipt can be regression-tested today.
    """

    def assert_receipt_surface(self, gate_id, receipt, schema) -> None:
        allowed = set(schema["properties"])
        required = set(schema["required"])
        self.assertTrue(
            set(receipt) <= allowed,
            f"{gate_id} has unknown fields: {sorted(set(receipt) - allowed)}",
        )
        self.assertTrue(
            required <= set(receipt),
            f"{gate_id} missing required: {sorted(required - set(receipt))}",
        )
        self.assertEqual(receipt["gate_id"], gate_id)
        self.assertEqual(receipt["feeder_rt"], FROZEN_FEEDER[gate_id])
        self.assertEqual(receipt["schema"], schema["$id"])

    def assert_receipt_semantics(self, gate_id, receipt, registry_gate) -> None:
        r = receipt
        if r["status"] == "pass":
            self.assertTrue(r["feeder_rt_independent_pass"], f"{gate_id} pass w/o feeder")
            self.assertEqual(r["evidence"]["tests_failed"], 0, gate_id)
        if r["status"] in ("pass", "fail"):
            self.assertIn("verifier", r, gate_id)
            self.assertNotEqual(r["verifier"], r["producer"], gate_id)
        if r["status"] == "not_run":
            self.assertEqual(r["conclusion"], "not_run", gate_id)
            self.assertEqual(r["evidence"]["tests_run"], 0, gate_id)
        if r["synthetic"]:
            self.assertIn("synthetic_reason", r, gate_id)
            self.assertNotEqual(r["conclusion"], "integration_verified", gate_id)
        if r["conclusion"] == "integration_verified":
            self.assertFalse(r["synthetic"], gate_id)
            self.assertEqual(r["status"], "pass", gate_id)
        self.assertTrue(
            set(r["consumers"]) <= set(registry_gate["consumers"]),
            f"{gate_id} consumers outside registry allowlist",
        )

    def assert_prerequisite_refs_are_backward_only(
        self, gate_id, receipt, registry_gate
    ) -> None:
        refs = [ref["ref_id"] for ref in receipt["prerequisite_refs"]]
        self.assertEqual(
            len(refs), len(set(refs)), f"{gate_id} duplicate ref_id: {sorted(refs)}"
        )
        allowed = set(registry_gate["allowed_prerequisite_ids"])
        self.assertEqual(
            set(refs) - allowed,
            set(),
            f"{gate_id} forward/unknown prerequisite refs: {sorted(set(refs) - allowed)}",
        )
        self.assertEqual(set(refs) & set(DOWNSTREAM_ONLY), set(), f"{gate_id} cycle")
        self.assertNotIn(gate_id, refs, f"{gate_id} self-reference")

    def assert_receipt_hash_reproduces(self, gate_id, receipt) -> None:
        self.assertEqual(
            _canonical_receipt_sha256(receipt),
            receipt["receipt_sha256"],
            f"{gate_id} receipt_sha256 mismatch",
        )

    def assert_receipt_artifacts_match_disk(self, gate_id, receipt, root) -> None:
        for artifact in receipt["artifacts"]:
            target = root / artifact["path"]
            self.assertTrue(
                target.is_file(), f"{gate_id} artifact missing: {artifact['path']}"
            )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(
                digest, artifact["sha256"], f"{gate_id} drift: {artifact['path']}"
            )

    def assert_receipt_fully_valid(self, gate_id, receipt, schema, registry_gate, root):
        self.assert_receipt_surface(gate_id, receipt, schema)
        self.assert_receipt_semantics(gate_id, receipt, registry_gate)
        self.assert_prerequisite_refs_are_backward_only(gate_id, receipt, registry_gate)
        self.assert_receipt_hash_reproduces(gate_id, receipt)
        self.assert_receipt_artifacts_match_disk(gate_id, receipt, root)


class GateReceiptsOnDiskTests(ReceiptChecks, unittest.TestCase):
    """Validate whatever receipts exist today, without freezing which ones exist.

    VG-B~VG-E receipts may be created at any time by their owner RT once that RT
    has an independent PASS. When they appear they are picked up here
    automatically and held to the same rules as VG-A; nothing in this class
    asserts their absence.
    """

    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(RECEIPT_SCHEMA_PATH)
        self.by_gate = {g["gate_id"]: g for g in self.registry["gates"]}

    def _present(self):
        for gate_id in GATE_ORDER:
            gate = self.by_gate[gate_id]
            path = REPO_ROOT / gate["receipt_path"]
            if path.exists():
                yield gate_id, gate, _load(path)

    def test_vga_receipt_exists_and_every_present_receipt_is_registry_declared(self) -> None:
        """Forward-compatible presence invariant.

        VG-A must exist now (it is a hard input to RT-024 / RT-026). Any other
        receipt on disk is allowed, but must sit at a registry-declared path so
        no undeclared or stray receipt can be smuggled in.
        """
        declared = {
            (REPO_ROOT / g["receipt_path"]).resolve() for g in self.registry["gates"]
        }
        vga_path = REPO_ROOT / self.by_gate["VG-A"]["receipt_path"]
        self.assertTrue(
            vga_path.is_file(),
            "VG-A machine receipt must exist in Wave-0; RT-024/RT-026 consume it",
        )
        receipts_dir = PR_ROOT / "gate-receipts"
        on_disk = {p.resolve() for p in receipts_dir.rglob("receipt.json")}
        undeclared = on_disk - declared
        self.assertEqual(
            undeclared,
            set(),
            f"receipt.json at non-registry path(s): {sorted(str(p) for p in undeclared)}",
        )

    def test_present_receipts_match_schema_surface(self) -> None:
        for gate_id, _gate, receipt in self._present():
            with self.subTest(gate=gate_id):
                self.assert_receipt_surface(gate_id, receipt, self.schema)

    def test_present_receipts_satisfy_semantic_rules(self) -> None:
        for gate_id, gate, receipt in self._present():
            with self.subTest(gate=gate_id):
                self.assert_receipt_semantics(gate_id, receipt, gate)

    def test_present_receipts_have_backward_only_unique_prerequisite_refs(self) -> None:
        for gate_id, gate, receipt in self._present():
            with self.subTest(gate=gate_id):
                self.assert_prerequisite_refs_are_backward_only(gate_id, receipt, gate)

    def test_present_receipt_hashes_are_reproducible(self) -> None:
        for gate_id, _gate, receipt in self._present():
            with self.subTest(gate=gate_id):
                self.assert_receipt_hash_reproduces(gate_id, receipt)

    def test_present_receipt_artifacts_exist_with_matching_hashes(self) -> None:
        for gate_id, _gate, receipt in self._present():
            with self.subTest(gate=gate_id):
                self.assert_receipt_artifacts_match_disk(gate_id, receipt, REPO_ROOT)


class VgaReceiptTests(unittest.TestCase):
    """VG-A specifics: synthetic PASS, capped conclusion, traceable to narratives."""

    def setUp(self) -> None:
        registry = _load(REGISTRY_PATH)
        gate = next(g for g in registry["gates"] if g["gate_id"] == "VG-A")
        self.path = REPO_ROOT / gate["receipt_path"]
        self.receipt = _load(self.path)

    def test_receipt_exists_at_the_registry_declared_path(self) -> None:
        self.assertTrue(self.path.is_file())

    def test_synthetic_pass_is_capped_at_conservative_unknown(self) -> None:
        self.assertEqual(self.receipt["status"], "pass")
        self.assertTrue(self.receipt["synthetic"])
        self.assertEqual(self.receipt["conclusion"], "conservative_unknown")
        self.assertNotEqual(self.receipt["conclusion"], "integration_verified")

    def test_producer_and_independent_verifier_are_distinct(self) -> None:
        self.assertEqual(self.receipt["producer"], "agent-vga-impl-opus")
        self.assertEqual(self.receipt["verifier"], "agent-vga-verify-opus")
        self.assertNotEqual(self.receipt["producer"], self.receipt["verifier"])

    def test_feeder_rt015_independent_pass_is_recorded(self) -> None:
        self.assertEqual(self.receipt["feeder_rt"], "RT-015")
        self.assertTrue(self.receipt["feeder_rt_independent_pass"])
        refs = {r["ref_id"] for r in self.receipt["prerequisite_refs"]}
        self.assertIn("RT-015", refs)

    def test_evidence_matches_the_vga_test_suite(self) -> None:
        evidence = self.receipt["evidence"]
        self.assertIn("test_vga_", evidence["test_command"])
        self.assertEqual(evidence["tests_run"], 75)
        self.assertEqual(evidence["tests_failed"], 0)
        self.assertEqual(evidence["python_version"], "3.11.14")

    def test_both_narratives_are_bound_as_artifacts(self) -> None:
        paths = {a["path"] for a in self.receipt["artifacts"]}
        for narrative in (
            "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-集成验证.md",
            "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-独立验收.md",
        ):
            self.assertIn(narrative, paths)

    def test_consumers_are_rt024_and_rt026_only(self) -> None:
        self.assertEqual(sorted(self.receipt["consumers"]), ["RT-024", "RT-026"])

    def test_receipt_contains_no_secret_or_absolute_path(self) -> None:
        blob = self.path.read_text(encoding="utf-8")
        for token in ("CWORK_APP_KEY", "AppKey", "app_secret", "Bearer ", "/Users/"):
            self.assertNotIn(token, blob, f"leaked {token!r}")


class FutureReceiptRegressionTests(ReceiptChecks, unittest.TestCase):
    """Prove the contract accepts legally-created VG-B~VG-E receipts.

    VG-B..VG-E do not exist on disk yet, so nothing today exercises the
    validators against them. These tests synthesise such receipts in a temp
    tree and assert (a) a well-formed future receipt passes every check, and
    (b) each anti-cycle / rank / uniqueness rule actually fails closed.
    """

    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(RECEIPT_SCHEMA_PATH)
        self.by_gate = {g["gate_id"]: g for g in self.registry["gates"]}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def _artifact(self, rel_path: str, content: bytes) -> dict:
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {"path": rel_path, "sha256": hashlib.sha256(content).hexdigest()}

    def _build(self, gate_id: str, *, ref_ids=None, **overrides) -> dict:
        gate = self.by_gate[gate_id]
        if ref_ids is None:
            ref_ids = [FROZEN_FEEDER[gate_id]]
        artifact = self._artifact(
            f"tests/test_{gate_id.lower().replace('-', '_')}_synthetic.py",
            f"# synthetic future artifact for {gate_id}\n".encode("utf-8"),
        )
        receipt = {
            "schema": self.schema["$id"],
            "gate_id": gate_id,
            "feeder_rt": FROZEN_FEEDER[gate_id],
            "feeder_rt_independent_pass": True,
            "status": "pass",
            "conclusion": "conservative_unknown",
            "synthetic": True,
            "synthetic_reason": "synthetic_future_receipt_regression",
            "producer": "agent-future-impl",
            "verifier": "agent-future-verify",
            "consumers": list(gate["consumers"]),
            "prerequisite_refs": [
                {"ref_id": ref_id, "ref_sha256": hashlib.sha256(ref_id.encode()).hexdigest()}
                for ref_id in ref_ids
            ],
            "evidence": {
                "test_command": f"python3.11 -m unittest discover -s tests -p 'test_{gate_id.lower()}_*.py'",
                "tests_run": 1,
                "tests_failed": 0,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            },
            "artifacts": [artifact],
            "created_at": "2026-09-01T00:00:00Z",
        }
        receipt.update(overrides)
        receipt["receipt_sha256"] = _canonical_receipt_sha256(receipt)
        return receipt

    def _check(self, gate_id: str, receipt: dict) -> None:
        self.assert_receipt_fully_valid(
            gate_id, receipt, self.schema, self.by_gate[gate_id], self.root
        )

    # --- positive: future receipts are welcome -----------------------------

    def test_every_gate_can_produce_a_valid_future_receipt(self) -> None:
        for gate_id in GATE_ORDER:
            with self.subTest(gate=gate_id):
                self._check(gate_id, self._build(gate_id))

    def test_future_receipt_may_cite_its_full_allowlist(self) -> None:
        for gate_id in GATE_ORDER:
            allowed = sorted(self.by_gate[gate_id]["allowed_prerequisite_ids"])
            with self.subTest(gate=gate_id):
                self._check(gate_id, self._build(gate_id, ref_ids=allowed))

    def test_non_synthetic_future_receipt_may_reach_integration_verified(self) -> None:
        receipt = self._build(
            "VG-E",
            synthetic=False,
            conclusion="integration_verified",
        )
        receipt.pop("synthetic_reason")
        receipt["receipt_sha256"] = _canonical_receipt_sha256(receipt)
        self._check("VG-E", receipt)

    def test_planned_not_run_future_receipt_is_valid(self) -> None:
        receipt = self._build(
            "VG-C",
            status="not_run",
            conclusion="not_run",
            feeder_rt_independent_pass=False,
        )
        receipt["evidence"]["tests_run"] = 0
        receipt.pop("verifier")
        receipt["receipt_sha256"] = _canonical_receipt_sha256(receipt)
        self._check("VG-C", receipt)

    # --- negative: each rank / cycle / uniqueness rule fails closed --------

    def test_forward_gate_reference_is_rejected(self) -> None:
        receipt = self._build("VG-B", ref_ids=["RT-018", "VG-C"])
        with self.assertRaises(AssertionError):
            self._check("VG-B", receipt)

    def test_self_gate_reference_is_rejected(self) -> None:
        receipt = self._build("VG-D", ref_ids=["RT-023", "VG-D"])
        with self.assertRaises(AssertionError):
            self._check("VG-D", receipt)

    def test_rt_above_the_feeder_is_rejected(self) -> None:
        receipt = self._build("VG-B", ref_ids=["RT-018", "RT-025"])
        with self.assertRaises(AssertionError):
            self._check("VG-B", receipt)

    def test_release_gate_that_consumes_this_gate_is_rejected(self) -> None:
        for release_gate, consumed in RELEASE_GATE_CONSUMES.items():
            with self.subTest(gate=consumed, release_gate=release_gate):
                receipt = self._build(
                    consumed, ref_ids=[FROZEN_FEEDER[consumed], release_gate]
                )
                with self.assertRaises(AssertionError):
                    self._check(consumed, receipt)

    def test_duplicate_ref_id_with_different_hashes_is_rejected(self) -> None:
        receipt = self._build("VG-C")
        receipt["prerequisite_refs"] = [
            {"ref_id": "VG-A", "ref_sha256": "a" * 64},
            {"ref_id": "VG-A", "ref_sha256": "b" * 64},
        ]
        receipt["receipt_sha256"] = _canonical_receipt_sha256(receipt)
        # uniqueItems accepts these as distinct objects; the semantic rule must not.
        self.assertNotEqual(receipt["prerequisite_refs"][0], receipt["prerequisite_refs"][1])
        with self.assertRaises(AssertionError):
            self._check("VG-C", receipt)

    def test_synthetic_receipt_cannot_claim_integration_verified(self) -> None:
        receipt = self._build("VG-D", conclusion="integration_verified")
        with self.assertRaises(AssertionError):
            self._check("VG-D", receipt)

    def test_pass_without_independent_feeder_pass_is_rejected(self) -> None:
        receipt = self._build("VG-B", feeder_rt_independent_pass=False)
        with self.assertRaises(AssertionError):
            self._check("VG-B", receipt)

    def test_producer_signing_its_own_pass_is_rejected(self) -> None:
        receipt = self._build("VG-B", verifier="agent-future-impl")
        with self.assertRaises(AssertionError):
            self._check("VG-B", receipt)

    def test_receipt_hash_drift_is_detected(self) -> None:
        receipt = self._build("VG-E")
        receipt["created_at"] = "2027-01-01T00:00:00Z"  # body changed, hash stale
        with self.assertRaises(AssertionError):
            self._check("VG-E", receipt)

    def test_artifact_drift_is_detected(self) -> None:
        receipt = self._build("VG-C")
        target = self.root / receipt["artifacts"][0]["path"]
        target.write_bytes(b"# tampered\n")
        with self.assertRaises(AssertionError):
            self._check("VG-C", receipt)

    def test_consumer_outside_the_registry_allowlist_is_rejected(self) -> None:
        # VG-E's registry allowlist is RT-026 only.
        receipt = self._build("VG-E", consumers=["RT-024", "RT-026"])
        with self.assertRaises(AssertionError):
            self._check("VG-E", receipt)

    def test_downstream_only_ids_never_pass_the_allowlist(self) -> None:
        for gate_id in GATE_ORDER:
            for bad in DOWNSTREAM_ONLY:
                with self.subTest(gate=gate_id, ref=bad):
                    receipt = self._build(gate_id, ref_ids=[FROZEN_FEEDER[gate_id], bad])
                    with self.assertRaises(AssertionError):
                        self._check(gate_id, receipt)


class SyntheticClosureMapTests(unittest.TestCase):
    """The closure map is frozen config: exact surface, no state, no dead end.

    VG-A is permanently synthetic. Without a declared closure path,
    "any synthetic hard gate => NO_GO" would make READY_FOR_G7_REVIEW
    unreachable forever. The map names the exit condition; these tests prove it
    is exact, owner-attributed, and carries no observation that could go stale.
    """

    def setUp(self) -> None:
        self.map = _load(CLOSURE_MAP_PATH)
        self.schema = _load(CLOSURE_MAP_SCHEMA_PATH)
        self.registry = _load(REGISTRY_PATH)
        self.by_gate = {g["gate_id"]: g for g in self.map["gate_closure"]}
        self.by_capability = {c["capability_id"]: c for c in self.map["capabilities"]}

    # --- exact surface -----------------------------------------------------

    def test_map_top_level_keys_match_schema_exactly(self) -> None:
        self.assertEqual(set(self.map), set(self.schema["properties"]))
        self.assertEqual(set(self.schema["required"]), set(self.schema["properties"]))

    def test_map_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.map["schema"], self.schema["$id"])
        self.assertFalse(self.schema["additionalProperties"])
        for section in ("capabilities", "gate_closure"):
            self.assertFalse(self.schema["properties"][section]["items"]["additionalProperties"])

    def test_map_declares_exactly_five_gates_in_wave_order(self) -> None:
        ids = [entry["gate_id"] for entry in self.map["gate_closure"]]
        self.assertEqual(tuple(ids), GATE_ORDER)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.schema["properties"]["gate_closure"]["minItems"], 5)
        self.assertEqual(self.schema["properties"]["gate_closure"]["maxItems"], 5)

    def test_map_declares_exactly_two_unique_capabilities(self) -> None:
        ids = [c["capability_id"] for c in self.map["capabilities"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            set(ids), {"cwork-authority-source", "gateway-identity-transport"}
        )
        self.assertEqual(self.schema["properties"]["capabilities"]["minItems"], 2)
        self.assertEqual(self.schema["properties"]["capabilities"]["maxItems"], 2)

    def test_gate_entry_keys_match_schema(self) -> None:
        allowed = set(self.schema["properties"]["gate_closure"]["items"]["properties"])
        required = set(self.schema["properties"]["gate_closure"]["items"]["required"])
        for entry in self.map["gate_closure"]:
            with self.subTest(gate=entry["gate_id"]):
                self.assertTrue(set(entry) <= allowed, sorted(set(entry) - allowed))
                self.assertTrue(required <= set(entry), sorted(required - set(entry)))

    def test_capability_entry_keys_match_schema(self) -> None:
        allowed = set(self.schema["properties"]["capabilities"]["items"]["properties"])
        required = set(self.schema["properties"]["capabilities"]["items"]["required"])
        for entry in self.map["capabilities"]:
            with self.subTest(capability=entry["capability_id"]):
                self.assertTrue(set(entry) <= allowed, sorted(set(entry) - allowed))
                self.assertTrue(required <= set(entry), sorted(required - set(entry)))

    def test_map_values_satisfy_schema_patterns_and_consts(self) -> None:
        props = self.schema["properties"]
        for key, spec in props.items():
            if "const" in spec:
                self.assertEqual(self.map[key], spec["const"], key)
        cap_props = props["capabilities"]["items"]["properties"]
        for entry in self.map["capabilities"]:
            for key in ("owner_rt", "sg_ref", "activation_receipt_path", "feasibility_ref"):
                self.assertRegex(entry[key], cap_props[key]["pattern"], f"{entry['capability_id']}.{key}")
        gate_props = props["gate_closure"]["items"]["properties"]
        for entry in self.map["gate_closure"]:
            self.assertIn(entry["closure_mode"], gate_props["closure_mode"]["enum"])
            if "expected_synthetic_reason" in entry:
                self.assertRegex(
                    entry["expected_synthetic_reason"],
                    gate_props["expected_synthetic_reason"]["pattern"],
                )

    # --- config, never state ----------------------------------------------

    def test_map_carries_no_mutable_execution_status(self) -> None:
        forbidden = set(self.schema["forbiddenEntryKeys"])
        self.assertTrue(set(MUTABLE_STATUS_KEYS) <= forbidden, sorted(set(MUTABLE_STATUS_KEYS) - forbidden))
        scopes = [("<top>", self.map)]
        scopes += [(e["gate_id"], e) for e in self.map["gate_closure"]]
        scopes += [(e["capability_id"], e) for e in self.map["capabilities"]]
        for name, scope in scopes:
            with self.subTest(scope=name):
                self.assertEqual(set(scope) & forbidden, set())

    def test_map_carries_no_point_in_time_observation_keys(self) -> None:
        """wave0_status / current_state_note would go stale once receipts land."""
        observation_keys = {
            "current_state_note",
            "wave0_status",
            "observed_at",
            "current_state",
            "present",
            "exists",
            "activated",
            "closed",
            "open",
        }
        forbidden = set(self.schema["forbiddenEntryKeys"])
        self.assertTrue(observation_keys <= forbidden, sorted(observation_keys - forbidden))
        self.assertEqual(observation_keys & set(self.schema["properties"]), set())
        cap_props = set(self.schema["properties"]["capabilities"]["items"]["properties"])
        gate_props = set(self.schema["properties"]["gate_closure"]["items"]["properties"])
        self.assertEqual(observation_keys & cap_props, set())
        self.assertEqual(observation_keys & gate_props, set())
        scopes = [self.map] + self.map["gate_closure"] + self.map["capabilities"]
        for scope in scopes:
            self.assertEqual(observation_keys & set(scope), set())

    def test_map_schema_forbids_status_and_observation_keys(self) -> None:
        """The ban list lives in the schema only; the map itself stays pure data."""
        self.assertNotIn("forbiddenEntryKeys", self.map)
        forbidden = set(self.schema["forbiddenEntryKeys"])
        self.assertTrue({"status", "conclusion", "verdict", "closed"} <= forbidden)
        self.assertEqual(forbidden & set(self.schema["properties"]), set())

    def test_static_notes_do_not_describe_run_outcomes(self) -> None:
        banned = ("currently", "as of today", "at present", "已通过", "尚未生成", "not yet exist")
        notes = [e["static_note"] for e in self.map["gate_closure"]]
        notes += [e["static_note"] for e in self.map["capabilities"]]
        notes += [e["closure_condition"] for e in self.map["capabilities"]]
        for note in notes:
            for phrase in banned:
                self.assertNotIn(phrase, note.lower() if phrase.isascii() else note)

    # --- ownership and paths ----------------------------------------------

    def test_capability_owners_are_producers_never_consumers(self) -> None:
        owners = {c["owner_rt"] for c in self.map["capabilities"]}
        self.assertEqual(owners, {"RT-017", "RT-023"})
        self.assertEqual(owners & {"RT-024", "RT-026"}, set())
        pattern = self.schema["properties"]["capabilities"]["items"]["properties"]["owner_rt"]["pattern"]
        for consumer in ("RT-024", "RT-026"):
            self.assertNotRegex(consumer, pattern)

    def test_capability_owner_mapping_is_frozen_and_unique(self) -> None:
        mapping = {c["capability_id"]: c["owner_rt"] for c in self.map["capabilities"]}
        self.assertEqual(
            mapping,
            {"cwork-authority-source": "RT-017", "gateway-identity-transport": "RT-023"},
        )
        self.assertEqual(len(set(mapping.values())), len(mapping))

    def test_activation_receipt_paths_are_unique_and_match_capability_id(self) -> None:
        paths = [c["activation_receipt_path"] for c in self.map["capabilities"]]
        self.assertEqual(len(paths), len(set(paths)))
        for entry in self.map["capabilities"]:
            self.assertEqual(
                entry["activation_receipt_path"].split("/")[-2], entry["capability_id"]
            )

    def test_activation_receipts_never_live_under_gate_receipts(self) -> None:
        """Keeps the registry-declared-path invariant for VG receipts intact."""
        declared_vg = {g["receipt_path"] for g in self.registry["gates"]}
        for entry in self.map["capabilities"]:
            path = entry["activation_receipt_path"]
            self.assertNotIn("/gate-receipts/", path)
            self.assertIn("/capability-receipts/", path)
            self.assertNotIn(path, declared_vg)

    def test_map_points_at_the_registry_and_activation_schema(self) -> None:
        self.assertTrue((REPO_ROOT / self.map["registry_ref"]).is_file())
        self.assertTrue((REPO_ROOT / self.map["activation_receipt_schema_ref"]).is_file())
        self.assertEqual(
            self.map["activation_receipt_schema_id"],
            _load(ACTIVATION_SCHEMA_PATH)["$id"],
        )
        for entry in self.map["capabilities"]:
            self.assertTrue((REPO_ROOT / entry["feasibility_ref"]).is_file(), entry["capability_id"])

    # --- closure semantics -------------------------------------------------

    def test_gate_closure_covers_every_registry_gate(self) -> None:
        self.assertEqual(
            {e["gate_id"] for e in self.map["gate_closure"]},
            {g["gate_id"] for g in self.registry["gates"]},
        )

    def test_only_vga_is_declared_permanently_synthetic(self) -> None:
        synthetic = {e["gate_id"] for e in self.map["gate_closure"] if e["synthetic_expected"]}
        self.assertEqual(synthetic, {"VG-A"})

    def test_vga_expected_reason_matches_the_receipt_on_disk(self) -> None:
        receipt_path = REPO_ROOT / next(
            g["receipt_path"] for g in self.registry["gates"] if g["gate_id"] == "VG-A"
        )
        receipt = _load(receipt_path)
        self.assertTrue(receipt["synthetic"])
        self.assertEqual(
            receipt["synthetic_reason"], self.by_gate["VG-A"]["expected_synthetic_reason"]
        )

    def test_vga_closes_only_via_both_activation_receipts(self) -> None:
        entry = self.by_gate["VG-A"]
        self.assertEqual(entry["closure_mode"], "capability_activation_receipts")
        self.assertEqual(
            sorted(entry["required_capability_ids"]),
            ["cwork-authority-source", "gateway-identity-transport"],
        )

    def test_closure_mode_and_required_capabilities_are_consistent(self) -> None:
        for entry in self.map["gate_closure"]:
            with self.subTest(gate=entry["gate_id"]):
                if entry["closure_mode"] == "capability_activation_receipts":
                    self.assertTrue(entry["required_capability_ids"])
                else:
                    self.assertEqual(entry["required_capability_ids"], [])

    def test_every_required_capability_id_resolves(self) -> None:
        for entry in self.map["gate_closure"]:
            for cap_id in entry["required_capability_ids"]:
                self.assertIn(cap_id, self.by_capability, entry["gate_id"])

    def test_no_gate_is_an_unreachable_dead_end(self) -> None:
        """Every gate has at least one declared way out; else READY is impossible."""
        for entry in self.map["gate_closure"]:
            with self.subTest(gate=entry["gate_id"]):
                has_exit = bool(entry["required_capability_ids"]) or entry["rerun_allowed"]
                self.assertTrue(has_exit, f"{entry['gate_id']} has no closure path")

    def test_rerun_and_synthetic_expectations_are_not_contradictory(self) -> None:
        """rerun_allowed=false <=> activation-receipt closure <=> permanently synthetic."""
        for entry in self.map["gate_closure"]:
            with self.subTest(gate=entry["gate_id"]):
                activation = entry["closure_mode"] == "capability_activation_receipts"
                self.assertEqual(entry["rerun_allowed"], not activation)
                self.assertEqual(entry["synthetic_expected"], activation)
                self.assertEqual("expected_synthetic_reason" in entry, activation)

    def test_history_rule_separates_immutability_from_rerun(self) -> None:
        rule = self.map["receipt_history_rule"]
        for token in ("CURRENT", "archive", "append-only", "receipt_sha256", "created_at"):
            self.assertIn(token, rule)
        self.assertIn("never", self.map["closure_rule"].lower())
        self.assertEqual(
            self.map["archive_path_template"],
            self.schema["properties"]["archive_path_template"]["const"],
        )
        self.assertIn("{superseded_receipt_sha256}", self.map["archive_path_template"])
        self.assertNotIn("/receipt.json", self.map["archive_path_template"])

    def test_archive_template_never_collides_with_a_registry_receipt_path(self) -> None:
        """Archived history must not be picked up as a current VG receipt."""
        template = self.map["archive_path_template"]
        self.assertFalse(template.endswith("/receipt.json"))
        for gate in self.registry["gates"]:
            rendered = template.replace(
                "VG-{gate_letter}", gate["gate_id"]
            ).replace("{superseded_receipt_sha256}", "0" * 64)
            self.assertNotEqual(rendered, gate["receipt_path"])
            self.assertTrue(rendered.startswith(gate["receipt_path"].rsplit("/", 1)[0] + "/archive/"))

    def test_rules_forbid_rt026_from_manufacturing_evidence(self) -> None:
        aggregation = self.map["aggregation_rule"]
        self.assertIn("RT-026", aggregation)
        for token in ("must not create", "backfill", "fails closed"):
            self.assertIn(token, aggregation)

    def test_evaluation_rule_names_every_closure_precondition(self) -> None:
        rule = self.map["evaluation_rule"]
        for token in (
            "status=pass",
            "owner_rt_independent_pass=true",
            "conclusion=capability_activated",
            "receipt_sha256",
            "artifacts[].sha256",
            "NO_GO",
            "OPEN",
        ):
            self.assertIn(token, rule)
        self.assertIn("never as N/A", rule)


class CapabilityActivationReceiptTests(unittest.TestCase):
    """The activation receipt schema is the only thing that can close a gap."""

    def setUp(self) -> None:
        self.schema = _load(ACTIVATION_SCHEMA_PATH)
        self.map = _load(CLOSURE_MAP_PATH)

    def test_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.schema["$id"], "cwk.pr001.capability_activation_receipt.v1")
        self.assertFalse(self.schema["additionalProperties"])
        self.assertFalse(self.schema["unevaluatedProperties"])

    def test_domain_separator_differs_from_the_vg_receipt(self) -> None:
        vg_desc = _load(RECEIPT_SCHEMA_PATH)["properties"]["receipt_sha256"]["description"]
        act_desc = self.schema["properties"]["receipt_sha256"]["description"]
        self.assertIn("cwk-verification-gate-receipt-v1", vg_desc)
        self.assertIn("cwk-capability-activation-receipt-v1", act_desc)
        self.assertNotIn("cwk-verification-gate-receipt-v1", act_desc)

    def test_capability_enum_matches_the_closure_map(self) -> None:
        self.assertEqual(
            set(self.schema["properties"]["capability_id"]["enum"]),
            {c["capability_id"] for c in self.map["capabilities"]},
        )

    def test_owner_pattern_excludes_the_consumer_rts(self) -> None:
        pattern = self.schema["properties"]["owner_rt"]["pattern"]
        for owner in ("RT-017", "RT-023"):
            self.assertRegex(owner, pattern)
        for consumer in ("RT-024", "RT-026"):
            self.assertNotRegex(consumer, pattern)

    def test_consumers_are_rt026_only(self) -> None:
        consumers = self.schema["properties"]["consumers"]
        self.assertEqual(consumers["items"]["enum"], ["RT-026"])
        self.assertEqual(consumers["maxItems"], 1)

    def test_capability_activated_requires_non_synthetic_pass(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("conclusion=capability_activated requires synthetic=false and status=pass", rules)
        self.assertIn("forbids conclusion=capability_activated", rules)

    def test_semantic_rules_state_the_full_closure_conjunction(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        for token in (
            "status=pass",
            "synthetic=false",
            "owner_rt_independent_pass=true",
            "conclusion=capability_activated",
            "receipt_sha256 matches",
            "artifacts[].sha256 matches the file on disk",
            "leaves the gap OPEN and forces NO_GO",
        ):
            self.assertIn(token, rules)

    def test_semantic_rules_forbid_upgrading_the_synthetic_gate(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("never under gate-receipts/", rules.replace(", never", " never"))
        self.assertIn("rerun_allowed=false", rules)
        self.assertIn("conservative_unknown", rules)

    def test_narrative_ref_pattern_points_into_capability_receipts(self) -> None:
        pattern = self.schema["properties"]["evidence"]["properties"]["narrative_ref"]["pattern"]
        good = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/cwork-authority-source/独立验收.md"
        bad = "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-独立验收.md"
        self.assertRegex(good, pattern)
        self.assertNotRegex(bad, pattern)

    def test_deep_forbidden_keys_cover_secrets_and_bodies(self) -> None:
        forbidden = set(self.schema["deepForbiddenKeys"])
        for key in ("app_key", "credential", "token", "secret", "raw_body", "prompt", "absolute_path"):
            self.assertIn(key, forbidden)


class HistoryChecks:
    """Mechanical history/chain validators shared by the evaluation tests.

    Nothing here trusts a caller-supplied flag: every verdict is recomputed
    from the receipts actually on disk. `sequence` is the primary ordering
    proof; `created_at` monotonicity is only an auxiliary backdating check.
    """

    ACTIVATION_DOMAIN = b"cwk-capability-activation-receipt-v1\x00"

    # --- hashing ----------------------------------------------------------

    @staticmethod
    def _domain_hash(domain: bytes, receipt: dict) -> str:
        body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
        return hashlib.sha256(domain + payload).hexdigest()

    def _hash(self, receipt: dict) -> str:
        return self._domain_hash(self.ACTIVATION_DOMAIN, receipt)

    def _vg_hash(self, receipt: dict) -> str:
        return self._domain_hash(RECEIPT_DOMAIN, receipt)

    # --- primitives -------------------------------------------------------

    @staticmethod
    def _ts(value):
        """Strict RFC3339 UTC, second precision, trailing Z. None if malformed."""
        if not isinstance(value, str):
            return None
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
            return None
        try:
            return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            return None

    # --- fail-closed filesystem access ------------------------------------
    #
    # These used to be a pathlib check-then-read (`is_symlink()` / `exists()`
    # / `resolve()` ... then `read_bytes()`), which left a TOCTOU window on
    # every artifact and did not protect the receipt files at all -- a review
    # showed a current receipt could simply BE a symlink. Everything now goes
    # through the openat/O_NOFOLLOW/dirfd chain in `pr001_safe_read`, which
    # re-verifies dev/ino/nlink/size/mode/mtime/ctime after the read.

    def _file_hash_matches(self, root: pathlib.Path, rel: str, expected: str) -> bool:
        return _sr.hash_matches(root, rel, expected)

    @staticmethod
    def _safe_json(root: pathlib.Path, rel: str):
        """Safely read+parse a receipt. None for missing, unsafe or malformed."""
        return _sr.try_read_json(root, rel)

    # The frozen filename grammar of every receipt archive: the entry's own
    # domain-separated hash. Declared here and handed to the snapshot API so
    # the enumeration itself refuses anything else -- a caller cannot choose to
    # filter junk out, because the junk never reaches it.
    _ARCHIVE_NAME_RE = re.compile(r"[0-9a-f]{64}\.json")

    _RECEIPT_ROOT_MAX_ENTRIES = 4096

    @classmethod
    def _json_receipt_root_is_closed(
        cls,
        root: pathlib.Path,
        base_rel: str,
        *,
        declared_current_paths,
        declared_archive_dirs,
    ) -> bool:
        """Exact JSON-receipt membership for a delegated receipt root.

        Narrative/probe evidence may legitimately be regular non-JSON files
        below these roots, but every JSON file is state and therefore must be
        either one registry-declared current receipt or one hash-named member
        of one registry-declared archive directory.  Hidden entries, links,
        special files and ambiguous extra JSON fail the whole root closed.
        The per-history validators still use ``directory_snapshot`` for the
        stronger atomic archive walk and chain semantics.
        """
        declared_current = set(declared_current_paths)
        declared_archives = {path.rstrip("/") for path in declared_archive_dirs}
        base = root / base_rel
        if base.is_symlink():
            return False
        if not base.is_dir():
            return True
        seen = 0
        stack = [base_rel.rstrip("/")]
        while stack:
            rel_dir = stack.pop()
            try:
                names = sorted(os.listdir(root / rel_dir))
            except OSError:
                return False
            for name in names:
                seen += 1
                if seen > cls._RECEIPT_ROOT_MAX_ENTRIES or name.startswith("."):
                    return False
                rel = f"{rel_dir}/{name}"
                try:
                    st = (root / rel).lstat()
                except OSError:
                    return False
                if stat.S_ISLNK(st.st_mode):
                    return False
                if stat.S_ISDIR(st.st_mode):
                    stack.append(rel)
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    return False
                if not name.endswith(".json"):
                    continue
                if rel in declared_current:
                    continue
                if rel_dir in declared_archives and cls._ARCHIVE_NAME_RE.fullmatch(name):
                    continue
                return False
        return True

    def _gate_receipt_root_is_closed(self, root: pathlib.Path) -> bool:
        """Whole-root exact closure for the VG receipt namespace."""
        gate_root = "PR/PR-001-multitenant-knowledge-spaces/gate-receipts"
        gate_current = [gate["receipt_path"] for gate in self.registry["gates"]]
        gate_archives = [
            self.map["archive_dir_template"].format(
                gate_letter=gate["gate_id"].split("-")[1]
            )
            for gate in self.registry["gates"]
        ]
        return self._json_receipt_root_is_closed(
            root,
            gate_root,
            declared_current_paths=gate_current,
            declared_archive_dirs=gate_archives,
        )

    def _capability_receipt_root_is_closed(self, root: pathlib.Path) -> bool:
        """Whole-root exact closure for the capability receipt namespace."""
        capability_root = (
            "PR/PR-001-multitenant-knowledge-spaces/capability-receipts"
        )
        capability_current = [
            entry["activation_receipt_path"] for entry in self.map["capabilities"]
        ]
        capability_archives = [
            entry["activation_archive_dir"] for entry in self.map["capabilities"]
        ]
        return self._json_receipt_root_is_closed(
            root,
            capability_root,
            declared_current_paths=capability_current,
            declared_archive_dirs=capability_archives,
        )

    def _delegated_receipt_roots_are_closed(self, root: pathlib.Path) -> bool:
        """Whole-root exact closure for both VG and capability families."""
        return self._gate_receipt_root_is_closed(
            root
        ) and self._capability_receipt_root_is_closed(root)

    def _archive_chain(self, root: pathlib.Path, rel_dir: str, hasher):
        """Load an archive by EXACT MEMBERSHIP. `False` on any violation.

        Returns `[]` for an absent or empty archive, a list of receipts for a
        clean one, and `False` for anything else. Callers must treat `False` as
        INVALID rather than as "no archive".

        This used to glob `*.json`, which silently ignored a junk file sitting
        next to the receipts and let a `rerun_allowed=false` gate report VALID
        with a non-empty archive. Enumeration now goes through
        `directory_snapshot`, which reads every entry through the fail-closed
        openat chain and raises on a stray extension, a nested directory, a
        dotfile, a symlink, a hardlinked entry, a name that aliases another
        under NFC+casefold, or membership that changes mid-snapshot.
        """
        try:
            snapshot = _sr.directory_snapshot(
                root, rel_dir, name_pattern=self._ARCHIVE_NAME_RE, missing_ok=True
            )
        except _sr.SafeReadError:
            return False
        if snapshot is None:
            return []
        loaded = []
        for name in snapshot.names:
            receipt = snapshot.json(name)
            if not isinstance(receipt, dict):
                return False  # not UTF-8 JSON, or not an object
            if f"{hasher(receipt)}.json" != name:
                return False  # filename is not its own hash
            loaded.append(receipt)
        return loaded

    @staticmethod
    def _no_deep_forbidden(node, forbidden) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in forbidden:
                    return False
                if not HistoryChecks._no_deep_forbidden(value, forbidden):
                    return False
        elif isinstance(node, list):
            for value in node:
                if not HistoryChecks._no_deep_forbidden(value, forbidden):
                    return False
        return True


class ClosureEvaluationRegressionTests(HistoryChecks, unittest.TestCase):
    """Prove the closure rule is fail-closed today AND reachable tomorrow.

    The map declares no observation, so both "has this gate run?" and "is the
    gap closed?" are recomputed here from receipts on disk -- never from a
    caller-supplied `current_synthetic` flag, which was the old shortcut and is
    deliberately gone. These tests exercise the same evaluator against a
    synthetic future tree, so the reachability of READY_FOR_G7_REVIEW is proven
    without pre-creating any real activation or gate receipt.

    The sandbox is a REAL git repository with a real two-generation history:
    owner code is frozen in one commit, receipts land in a later evidence-only
    commit, and the evaluation commit is injected rather than read from HEAD.
    That is not fixture decoration -- ancestry, owner-code scope and the tested
    owner-tree digest cannot be exercised against a bare temp directory, and
    while this suite used a literal `"a" * 40` subject commit every one of
    those rules was structurally unreachable. The clock and the environment
    fingerprint are injected for the same reason: a verdict that depends on
    today's date or on this laptop's Python build is not a verdict.
    """

    # A frozen evaluation instant. Every freshness rule below is judged against
    # THIS, never against wall-clock now: a suite whose verdicts drift with the
    # calendar was never testing freshness, only the date it happened to run.
    EVALUATION_INSTANT = _dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # The fixture's probe trust anchor. Production reads a real signer; the
    # point being tested is only that the evaluator RECOMPUTES an HMAC the
    # receipt never handed it, so a hand-written manifest cannot pass.
    PROBE_SIGNING_KEY = b"pr001-fixture-probe-trust-anchor"

    # Gate evidence output. A commit that touches nothing but a gate receipt is
    # not a commit that touched the feeder RT's code.
    GATE_EVIDENCE_PREFIXES = ("PR/PR-001-multitenant-knowledge-spaces/gate-receipts/",)

    def setUp(self) -> None:
        self.map = _load(CLOSURE_MAP_PATH)
        self.schema = _load(ACTIVATION_SCHEMA_PATH)
        self.vg_schema = _load(RECEIPT_SCHEMA_PATH)
        self.registry = _load(REGISTRY_PATH)
        self.by_capability = {c["capability_id"]: c for c in self.map["capabilities"]}
        self.by_gate = {g["gate_id"]: g for g in self.registry["gates"]}
        self.closure_by_gate = {e["gate_id"]: e for e in self.map["gate_closure"]}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self._git_cache = {}

        # --- a real repository, because ancestry cannot be stubbed ----------
        #
        # The fixture used to write receipts into a bare temp directory and
        # bind `tested_subject_commit` to "a" * 40, which made every freshness
        # rule in the evaluator structurally unreachable: there was no commit
        # to be an ancestor of, no owner tree to digest, and a 40-hex string
        # that matched the schema pattern was the whole proof. The fixture now
        # builds the two-generation history the contract actually requires --
        # owner CODE frozen first, receipts landing in a later evidence-only
        # commit -- so the receipts are judged by the same ancestry rules a
        # production evaluator applies.
        self.git = init_fixture_repo(self.root)
        for prefix in sorted(self._owner_code_prefixes()):
            target = self.root / prefix.rstrip("/") / "src" / "capability.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f'"""owner code for {prefix}"""\n', encoding="utf-8")
        self.subject_commit = commit_all(self.git, "candidate implementation subject")

        self.clock = EvaluationClock(
            self.EVALUATION_INSTANT,
            max_skew_seconds=self.map["activation_max_clock_skew_seconds"],
            max_probe_age_seconds=self.map["activation_max_probe_age_seconds"],
        )
        # OBSERVED, never asserted. `toolchain_build` is deliberately absent:
        # an evaluator may only certify what it can recompute, and there is no
        # way to observe which toolchain built somebody else's receipt, so
        # demanding a match on it would only be theatre.
        self.expected_environment = {
            "python_version": platform.python_version(),
            "platform": sys.platform,
        }

    # ------------------------------------------------------------------
    # owner scope
    # ------------------------------------------------------------------

    def _gate_code_prefixes(self, gate_id: str) -> list:
        """The feeder RT's code scope for a gate. Config-derived, never guessed."""
        return [f"RT/{self.by_gate[gate_id]['feeder_rt']}/"]

    def _owner_code_prefixes(self) -> set:
        """Every owner CODE prefix the fixture must materialise before freezing."""
        prefixes = set()
        for entry in self.map["capabilities"]:
            prefixes.update(entry["owner_code_path_prefixes"])
        for gate_id in GATE_ORDER:
            prefixes.update(self._gate_code_prefixes(gate_id))
        return prefixes

    # ------------------------------------------------------------------
    # writers
    # ------------------------------------------------------------------

    def _write(self, path: pathlib.Path, receipt: dict) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _archive_dir(self, gate_id: str) -> str:
        return self.map["archive_dir_template"].format(gate_letter=gate_id.split("-")[1])

    def _build_activation(self, capability_id: str, **overrides) -> dict:
        entry = self.by_capability[capability_id]
        base = f"PR/PR-001-multitenant-knowledge-spaces/capability-receipts/{capability_id}"
        roles = list(entry["required_evidence_roles"])
        evidence_refs = []
        for role in roles:
            rel = f"{base}/evidence/{role}.md"
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"# {capability_id} :: {role}\n".encode("utf-8"))
            evidence_refs.append(
                {
                    "role": role,
                    "path": rel,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        rel = f"{base}/evidence.md"
        artifact = self.root / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"# {capability_id} activation evidence\n".encode("utf-8"))
        receipt = {
            "schema": self.schema["$id"],
            "capability_id": capability_id,
            "owner_rt": entry["owner_rt"],
            "owner_rt_independent_pass": True,
            "status": "pass",
            "conclusion": "capability_activated",
            "synthetic": False,
            "producer": "agent-activation-impl",
            "verifier": "agent-activation-verify",
            "consumers": ["RT-026"],
            "closes_gate_ids": sorted(
                g["gate_id"]
                for g in self.map["gate_closure"]
                if capability_id in g["required_capability_ids"]
            ),
            "sequence": 1,
            "evidence": {
                "test_command": "python3.11 -m unittest discover -s tests -p 'test_*.py'",
                "tests_run": 12,
                "tests_failed": 0,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            },
            "evidence_refs": evidence_refs,
            "artifacts": [
                {"path": rel, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
            ],
            # The REAL frozen subject, plus the digest of exactly the code that
            # was tested. A literal 40-hex placeholder used to sit here, which
            # meant the ancestry and owner-scope rules were never exercised.
            "tested_subject_commit": self.subject_commit,
            "owner_scope_tree_sha256": self._owner_tree_digest(
                entry["owner_code_path_prefixes"]
            ),
            "environment_fingerprint": self._environment_fingerprint(),
            # 24 days: inside BOTH frozen bounds (90d authority, 30d gateway)
            "created_at": "2026-10-01T00:00:00Z",
            "expires_at": "2026-10-25T00:00:00Z",
        }
        receipt.update(overrides)
        receipt["receipt_sha256"] = self._hash(receipt)
        return receipt

    def _environment_fingerprint(self, **overrides) -> dict:
        """The observed environment, plus the one field nobody can recompute."""
        fingerprint = {
            **self.expected_environment,
            "toolchain_build": "cwk-pr001-candidate",
        }
        fingerprint.update(overrides)
        return fingerprint

    def _owner_tree_digest(self, prefixes) -> str:
        """The owner-scope tree digest at the frozen subject commit."""
        digest = self.git.owner_scope_tree_sha256(self.subject_commit, list(prefixes))
        self.assertIsNotNone(digest, f"fixture has no owner tree for {prefixes!r}")
        return digest

    def _extra_commit(self, rel: str, body: bytes) -> str:
        """Freeze one more commit touching `rel`; returns its sha.

        Used by the attack tests to manufacture a *neighbouring* commit that
        satisfies ancestry, so what is left to reject it is the owner tree
        digest or the path boundary rather than ancestry alone.
        """
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return commit_all(self.git, f"later commit touching {rel}")

    # `capability_id` and `sequence` are positional-only so that an attack test
    # can override the manifest's OWN declared values without colliding with
    # the arguments that decide where the manifest is written.
    def _write_probe(
        self, capability_id: str, sequence: int, /, *, at_sequence: int = None, **overrides
    ) -> dict:
        """A signed renewal probe manifest at the capability's sequence path.

        Written through the same template the evaluator resolves, so a probe
        cannot be replayed under a later sequence just by renaming it.
        `at_sequence` decouples the PATH from the manifest's declared sequence,
        which is what lets an attack test isolate the two rules.
        """
        entry = self.by_capability[capability_id]
        observed = overrides.pop("observed_at", "2026-10-09T23:00:00Z")
        # A caller may force a bogus signature to prove the HMAC is recomputed
        # rather than trusted; otherwise the manifest is honestly signed.
        forced_signature = overrides.pop("signature", None)
        manifest = {
            "capability_id": capability_id,
            "sequence": sequence,
            "challenge": hashlib.sha256(
                f"{capability_id}:{sequence}:{observed}".encode("utf-8")
            ).hexdigest(),
            "observed_at": observed,
            "tested_subject_commit": self.subject_commit,
            "environment_fingerprint": self._environment_fingerprint(),
            "api_version": "cwork-authority-2026-09",
            "result": "pass",
            "verifier": "agent-activation-verify",
        }
        manifest.update(overrides)
        manifest["signature"] = forced_signature or probe_signature(
            manifest, self.PROBE_SIGNING_KEY
        )
        rel = entry["renewal_probe_path_template"].format(
            sequence=sequence if at_sequence is None else at_sequence
        )
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        target.write_bytes(payload)
        return {"path": rel, "sha256": hashlib.sha256(payload).hexdigest()}

    def _write_activation(self, capability_id: str, **overrides) -> pathlib.Path:
        receipt = self._build_activation(capability_id, **overrides)
        entry = self.by_capability[capability_id]
        return self._write(self.root / entry["activation_receipt_path"], receipt)

    def _archive_activation(self, capability_id: str, receipt: dict) -> pathlib.Path:
        entry = self.by_capability[capability_id]
        target = (
            self.root / entry["activation_archive_dir"] / f"{receipt['receipt_sha256']}.json"
        )
        return self._write(target, receipt)

    def _build_vg(self, gate_id: str, **overrides) -> dict:
        gate = self.by_gate[gate_id]
        rel = f"PR/PR-001-multitenant-knowledge-spaces/gate-receipts/{gate_id}/notes.md"
        artifact = self.root / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"# {gate_id} narrative\n".encode("utf-8"))
        receipt = {
            "schema": self.vg_schema["$id"],
            "gate_id": gate_id,
            "feeder_rt": gate["feeder_rt"],
            "feeder_rt_independent_pass": True,
            "status": "pass",
            "conclusion": "integration_verified",
            "synthetic": False,
            "producer": "agent-gate-impl",
            "verifier": "agent-gate-verify",
            # Config-derived, never hardcoded: VG-E's only declared consumer is
            # RT-026, so a fixture that always wrote ["RT-024", "RT-026"] built
            # a receipt naming a consumer the registry does not allow.
            "consumers": list(gate["consumers"]),
            "prerequisite_refs": [],
            "sequence": 1,
            "evidence": {
                "test_command": "python3.11 -m unittest discover -s tests -p 'test_*.py'",
                "tests_run": 9,
                "tests_failed": 0,
                "python_version": "3.11.14",
            },
            "artifacts": [
                {"path": rel, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
            ],
            "created_at": "2026-10-01T00:00:00Z",
        }
        # VG-A is the pinned legacy exception: it predates subject binding and
        # must never be rewritten to carry it, because that would change its
        # receipt_sha256 and break the pin. Every rerun-capable gate binds the
        # real subject, the real owner tree and the observed environment.
        if self.closure_by_gate[gate_id]["rerun_allowed"]:
            receipt["tested_subject_commit"] = self.subject_commit
            receipt["owner_scope_tree_sha256"] = self._owner_tree_digest(
                self._gate_code_prefixes(gate_id)
            )
            receipt["environment_fingerprint"] = self._environment_fingerprint()
        receipt.update(overrides)
        receipt["receipt_sha256"] = self._vg_hash(receipt)
        return receipt

    def _write_vg(self, gate_id: str, **overrides) -> dict:
        receipt = self._build_vg(gate_id, **overrides)
        self._write(self.root / self.by_gate[gate_id]["receipt_path"], receipt)
        return receipt

    def _archive_vg(self, gate_id: str, receipt: dict) -> pathlib.Path:
        target = self.root / self._archive_dir(gate_id) / f"{receipt['receipt_sha256']}.json"
        return self._write(target, receipt)

    # ------------------------------------------------------------------
    # validators
    # ------------------------------------------------------------------

    def _activation_surface_ok(self, receipt: dict) -> bool:
        """Full required / allowed-key / type / pattern / deep-forbidden check."""
        props = self.schema["properties"]
        if set(receipt) - set(props):
            return False
        if set(self.schema["required"]) - set(receipt):
            return False
        if not self._no_deep_forbidden(receipt, set(self.schema["deepForbiddenKeys"])):
            return False
        if receipt.get("schema") != self.schema["$id"]:
            return False
        for field in ("capability_id", "status", "conclusion"):
            if receipt.get(field) not in props[field]["enum"]:
                return False
        for field in ("owner_rt_independent_pass", "synthetic"):
            if not isinstance(receipt.get(field), bool):
                return False
        for field, value in (
            ("owner_rt", receipt.get("owner_rt")),
            ("producer", receipt.get("producer")),
            ("verifier", receipt.get("verifier")),
            ("tested_subject_commit", receipt.get("tested_subject_commit")),
            ("receipt_sha256", receipt.get("receipt_sha256")),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(props[field]["pattern"], value):
                return False
        if not isinstance(receipt.get("sequence"), int) or receipt["sequence"] < 1:
            return False
        if isinstance(receipt.get("sequence"), bool):
            return False
        env = receipt.get("environment_fingerprint")
        if not isinstance(env, dict):
            return False
        env_schema = props["environment_fingerprint"]
        if set(env_schema["required"]) - set(env) or set(env) - set(env_schema["properties"]):
            return False
        ev = receipt.get("evidence")
        if not isinstance(ev, dict):
            return False
        ev_schema = props["evidence"]
        if set(ev_schema["required"]) - set(ev) or set(ev) - set(ev_schema["properties"]):
            return False
        for field in ("tests_run", "tests_failed", "tests_skipped"):
            if field in ev and (not isinstance(ev[field], int) or isinstance(ev[field], bool)):
                return False
        refs = receipt.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            return False
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"role", "path", "sha256"}:
                return False
        arts = receipt.get("artifacts")
        if not isinstance(arts, list) or not arts:
            return False
        for art in arts:
            if not isinstance(art, dict) or set(art) != {"path", "sha256"}:
                return False
        return True

    def _activation_receipt_valid(self, capability_id, receipt, root, evidence_commit) -> bool:
        """Everything a CLOSING activation receipt must satisfy."""
        entry = self.by_capability[capability_id]
        if not self._activation_surface_ok(receipt):
            return False
        if receipt["capability_id"] != capability_id:
            return False
        if receipt["owner_rt"] != entry["owner_rt"]:
            return False
        if receipt["status"] != "pass" or receipt["synthetic"] is not False:
            return False
        if receipt["owner_rt_independent_pass"] is not True:
            return False
        if receipt["conclusion"] != "capability_activated":
            return False
        if "verifier" not in receipt or receipt["verifier"] == receipt["producer"]:
            return False
        if receipt["consumers"] != ["RT-026"]:
            return False
        mapped = sorted(
            g["gate_id"]
            for g in self.map["gate_closure"]
            if capability_id in g["required_capability_ids"]
        )
        if sorted(receipt["closes_gate_ids"]) != mapped:
            return False
        ev = receipt["evidence"]
        if ev["tests_run"] <= 0 or ev["tests_failed"] != 0:
            return False
        if not 0 <= ev.get("tests_skipped", 0) <= ev["tests_run"]:
            return False
        # capability-specific roles, from config
        required_roles = set(entry["required_evidence_roles"])
        if not required_roles <= {r["role"] for r in receipt["evidence_refs"]}:
            return False
        # timestamps + bounded TTL
        created = self._ts(receipt["created_at"])
        expires = self._ts(receipt["expires_at"])
        if created is None or expires is None:
            return False
        lifetime = (expires - created).total_seconds()
        if not 0 < lifetime <= entry["max_validity_seconds"]:
            return False
        # bound evidence must be pre-existing safe regular files with real hashes
        forbidden_prefix = "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/"
        own_paths = {c["activation_receipt_path"] for c in self.map["capabilities"]}
        for item in list(receipt["artifacts"]) + list(receipt["evidence_refs"]):
            rel = item["path"]
            if rel in own_paths or rel.startswith(forbidden_prefix):
                return False
            if not self._file_hash_matches(root, rel, item["sha256"]):
                return False
        # --- freshness: derived E, ancestry, owner CODE scope, exact tree ---
        #
        # `owner_prefixes` used to be the capability's own *evidence* directory,
        # so a commit that changed nothing but the receipt satisfied "the
        # subject modified the owning package" -- evidence bootstrapping its own
        # subject binding. Code scope and evidence scope are now separate
        # arguments, and a path under the latter can never count as the former.
        if not verify_subject_commit(
            self._git_for(root),
            receipt.get("tested_subject_commit"),
            evidence_commit,
            code_prefixes=entry["owner_code_path_prefixes"],
            evidence_prefixes=entry["owner_evidence_path_prefixes"],
            declared_tree_sha256=receipt.get("owner_scope_tree_sha256"),
        ):
            return False
        if not verify_environment_fingerprint(
            receipt.get("environment_fingerprint"), self.expected_environment
        ):
            return False
        if receipt["receipt_sha256"] != self._hash(receipt):
            return False
        return True

    # --- evaluation context: injected, never baked ------------------------

    def _git_for(self, root: pathlib.Path):
        """The `GitSubject` matching `root`. Fixtures get their own real repo."""
        cache = getattr(self, "_git_cache", None)
        if cache is None:
            cache = self._git_cache = {}
        key = str(root)
        if key not in cache:
            cache[key] = GitSubject.for_repo(root)
        return cache[key]

    def _evaluation_commit(self, root: pathlib.Path):
        """The candidate commit under review. Injected, never assumed.

        A fixture writes receipts and only then asks for a verdict, so the
        evaluation commit is made lazily from whatever is pending. It is
        deliberately NOT handed to `resolve_evidence_commit` as the evidence
        commit: E is derived from it, so an evaluator cannot pick a convenient
        commit and call it the one that introduced the receipt.
        """
        git = self._git_for(root)
        if git is None:
            return None
        if str(root) != str(getattr(self, "root", "")):
            return git.head()  # a real repository is read, never written to
        if worktree_is_dirty(git):
            return commit_all(git, "evidence commit")
        return git.head()

    def _evidence_commit(self, root: pathlib.Path, receipt_rel: str):
        """The derived commit E that introduced `receipt_rel`, or None."""
        return resolve_evidence_commit(
            self._git_for(root),
            root,
            receipt_rel,
            evaluation_commit=self._evaluation_commit(root),
        )

    def _probe_instant(self, root: pathlib.Path, receipt: dict):
        """`observed_at` of a receipt's own probe, or None when it has none."""
        probe = receipt.get("renewal_probe_ref")
        if not isinstance(probe, dict):
            return None
        manifest = self._safe_json(root, probe.get("path"))
        if not isinstance(manifest, dict):
            return None
        return parse_instant(manifest.get("observed_at"))

    def _renewal_probe_is_fresh(self, root, capability_id, receipt, previous, evidence_commit):
        """A renewal needs a fresh, signed, sequence-bound probe MANIFEST.

        `renewal_probe_ref` used to be checked for presence only, so a renewal
        naming `does/not/exist/anywhere.md` was accepted. Making it hash-bound
        fixed that but not the real hole: hash-DIFFERENCE is not freshness,
        because any single flipped byte in a copied file satisfies it. The
        reference must now resolve to a manifest that says WHAT was observed,
        WHEN, against WHICH subject and BY WHOM, signed over exactly those
        fields, sitting at this sequence's own path so an earlier probe cannot
        be replayed under a later sequence.
        """
        entry = self.by_capability[capability_id]
        probe = receipt.get("renewal_probe_ref")
        if not isinstance(probe, dict) or set(probe) != {"path", "sha256"}:
            return False
        expected_path = entry["renewal_probe_path_template"].format(
            sequence=receipt["sequence"]
        )
        if probe["path"] != expected_path:
            return False
        data = _sr.try_read_bytes(root, probe["path"])
        if not data:  # missing, unsafe, or empty
            return False
        if hashlib.sha256(data).hexdigest() != probe["sha256"]:
            return False
        # A renewal that re-binds the superseded issue's bytes is not a probe.
        stale = {item["sha256"] for item in previous.get("evidence_refs", [])}
        stale |= {item["sha256"] for item in previous.get("artifacts", [])}
        stale.add(previous.get("receipt_sha256"))
        if probe["sha256"] in stale:
            return False
        # The probe must have landed with the receipt it justifies, not been
        # sitting in the tree from some earlier run.
        git = self._git_for(root)
        if git is None or evidence_commit is None:
            return False
        if probe["path"] not in git.paths_touched(evidence_commit):
            return False
        created = self._ts(receipt["created_at"])
        if created is None:
            return False
        why = verify_probe_manifest(
            self._safe_json(root, probe["path"]),
            capability_id=capability_id,
            sequence=receipt["sequence"],
            subject_commit=receipt.get("tested_subject_commit"),
            expected_environment=self.expected_environment,
            clock=self.clock,
            signing_key=self.PROBE_SIGNING_KEY,
            certified_at=created,
            previous_observed_at=self._probe_instant(root, previous),
        )
        return why is None

    def _activation_chain_state(self, capability_id: str, root: pathlib.Path) -> str:
        """'NOT_RUN' | 'INVALID' | 'EXPIRED' | 'VALID' -- recomputed, never asserted."""
        entry = self.by_capability[capability_id]
        rel_current = entry["activation_receipt_path"]
        archived = self._archive_chain(root, entry["activation_archive_dir"], self._hash)
        if archived is False:
            return "INVALID"
        current = self._safe_json(root, rel_current)
        if current is None:
            # An absent receipt is NOT_RUN only when nothing was ever archived;
            # an archive with no current receipt is a broken chain, not a
            # clean slate. An unsafe (symlinked/hardlinked) current receipt
            # lands here too and must never be read as "not run".
            if archived:
                return "INVALID"
            return "NOT_RUN" if _sr.try_read_bytes(root, rel_current) is None else "INVALID"
        # Derived once, from an explicitly injected evaluation commit, and
        # reused for both the subject binding and the renewal probe: the two
        # must agree on which commit introduced this receipt.
        evidence_commit = self._evidence_commit(root, rel_current)
        if not self._activation_receipt_valid(capability_id, current, root, evidence_commit):
            return "INVALID"
        chain = {}
        for receipt in archived:
            if not self._activation_surface_ok(receipt):
                return "INVALID"
            if receipt["capability_id"] != capability_id:
                return "INVALID"
            if receipt["receipt_sha256"] != self._hash(receipt):
                return "INVALID"
            created = self._ts(receipt["created_at"])
            expires = self._ts(receipt["expires_at"])
            if created is None or expires is None:
                return "INVALID"
            lifetime = (expires - created).total_seconds()
            if not 0 < lifetime <= entry["max_validity_seconds"]:
                return "INVALID"
            if receipt["sequence"] in chain:
                return "INVALID"  # duplicate / fork
            chain[receipt["sequence"]] = receipt
        n = current["sequence"]
        if sorted(chain) != list(range(1, n)):  # exactly 1..N-1, no gap/orphan
            return "INVALID"
        chain[n] = current
        for seq in range(1, n + 1):
            receipt = chain[seq]
            link = receipt.get("previous_receipt_sha256")
            renewal = receipt.get("renewal_probe_ref")
            if seq == 1:
                if link is not None or renewal is not None:
                    return "INVALID"
            else:
                if link is None or renewal is None:
                    return "INVALID"
                if link != chain[seq - 1]["receipt_sha256"]:
                    return "INVALID"  # broken link / fork / cycle
                if not self._renewal_probe_is_fresh(
                    root, capability_id, receipt, chain[seq - 1], evidence_commit
                ):
                    return "INVALID"  # missing / stale / replayed / forged probe
                if not (
                    self._ts(chain[seq - 1]["created_at"]) < self._ts(receipt["created_at"])
                    and self._ts(chain[seq - 1]["expires_at"]) < self._ts(receipt["expires_at"])
                ):
                    return "INVALID"  # auxiliary backdating check
        # NOT-BEFORE: a future-dated receipt is invalid, not merely unexpired.
        clock = self.clock
        for receipt in chain.values():
            if clock.is_future_dated(self._ts(receipt["created_at"])):
                return "INVALID"
        if clock.is_expired(self._ts(current["expires_at"])):
            return "EXPIRED"
        return "VALID"

    def _activation_is_valid(self, capability_id: str, root: pathlib.Path) -> bool:
        return self._activation_chain_state(capability_id, root) == "VALID"

    def _vg_surface_ok(self, gate_id: str, receipt) -> bool:
        """Structural surface every VG receipt must satisfy, current or archived."""
        props = self.vg_schema["properties"]
        if not isinstance(receipt, dict):
            return False
        if set(receipt) - set(props):
            return False
        if set(self.vg_schema["required"]) - set(receipt):
            return False
        if not self._no_deep_forbidden(receipt, set(self.vg_schema["deepForbiddenKeys"])):
            return False
        if receipt.get("schema") != self.vg_schema["$id"]:
            return False
        if receipt.get("gate_id") != gate_id:
            return False
        for field in ("gate_id", "feeder_rt", "status", "conclusion"):
            if receipt.get(field) not in props[field]["enum"]:
                return False
        for field in ("feeder_rt_independent_pass", "synthetic"):
            if not isinstance(receipt.get(field), bool):
                return False
        # The frozen 1:1 feeder mapping is config, not something a receipt
        # gets to restate differently.
        if receipt["feeder_rt"] != FROZEN_FEEDER[gate_id]:
            return False
        if receipt.get("receipt_sha256") != self._vg_hash(receipt):
            return False
        return True

    def _vg_receipt_closes(self, gate_id: str, receipt: dict, root: pathlib.Path) -> bool:
        """Full PASS semantics required before `synthetic=false` may close a gap.

        `_gap_state` used to close a gap on `synthetic is False and conclusion
        != conservative_unknown` alone. A review showed that let a receipt
        carrying status=fail / conclusion=failed / synthetic=false report
        CLOSED, and that producer==verifier, tests_failed=1, a wrong feeder_rt,
        a forward prerequisite ref and artifact hash drift were all accepted.
        """
        gate = self.by_gate[gate_id]
        props = self.vg_schema["properties"]
        if not self._vg_surface_ok(gate_id, receipt):
            return False
        # --- status / conclusion / independence ----------------------------
        if receipt["status"] != "pass":
            return False
        if receipt["conclusion"] != "integration_verified":
            return False
        if receipt["synthetic"] is not False:
            return False
        if receipt["feeder_rt_independent_pass"] is not True:
            return False
        if "verifier" not in receipt or receipt["verifier"] == receipt.get("producer"):
            return False
        for field in ("producer", "verifier"):
            if not re.fullmatch(props[field]["pattern"], str(receipt.get(field))):
                return False
        # --- evidence ------------------------------------------------------
        ev = receipt.get("evidence")
        ev_schema = props["evidence"]
        if not isinstance(ev, dict):
            return False
        if set(ev_schema["required"]) - set(ev) or set(ev) - set(ev_schema["properties"]):
            return False
        for field in ("tests_run", "tests_failed", "tests_skipped"):
            if field in ev and (not isinstance(ev[field], int) or isinstance(ev[field], bool)):
                return False
        if ev["tests_run"] <= 0 or ev["tests_failed"] != 0:
            return False
        if not 0 <= ev.get("tests_skipped", 0) <= ev["tests_run"]:
            return False
        # --- consumers + anti-cycle prerequisite allowlist -----------------
        allowed_consumers = set(gate["consumers"])
        consumers = receipt.get("consumers")
        if not isinstance(consumers, list) or not consumers:
            return False
        if not set(consumers) <= allowed_consumers:
            return False
        allowed_refs = set(gate["allowed_prerequisite_ids"])
        refs = receipt.get("prerequisite_refs")
        if not isinstance(refs, list):
            return False
        seen_refs = set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"ref_id", "ref_sha256"}:
                return False
            ref_id = ref["ref_id"]
            if ref_id == gate_id:  # no self-reference
                return False
            if ref_id not in allowed_refs:  # forward / downstream reference
                return False
            if ref_id in seen_refs:  # duplicate id with a different hash
                return False
            seen_refs.add(ref_id)
        # --- artifacts must hash to real, safely-read files ----------------
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, list):
            return False
        declared = {g["receipt_path"] for g in self.registry["gates"]}
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                return False
            if artifact["path"] in declared:  # never hash a VG receipt
                return False
            if not self._file_hash_matches(root, artifact["path"], artifact["sha256"]):
                return False
        # --- freshness: only rerun-capable gates carry a subject binding ---
        #
        # VG-A is the pinned legacy exception and is skipped here deliberately:
        # adding the fields would change its receipt_sha256 and break the pin.
        if self.closure_by_gate[gate_id]["rerun_allowed"]:
            if not verify_subject_commit(
                self._git_for(root),
                receipt.get("tested_subject_commit"),
                self._evidence_commit(root, gate["receipt_path"]),
                code_prefixes=self._gate_code_prefixes(gate_id),
                evidence_prefixes=self.GATE_EVIDENCE_PREFIXES,
                declared_tree_sha256=receipt.get("owner_scope_tree_sha256"),
            ):
                return False
            if not verify_environment_fingerprint(
                receipt.get("environment_fingerprint"), self.expected_environment
            ):
                return False
        return True

    def _gate_history_state(self, gate_id: str, root: pathlib.Path) -> str:
        """'NOT_RUN' | 'INVALID' | 'VALID', derived only from what is on disk."""
        gate = self.by_gate[gate_id]
        closure = self.closure_by_gate[gate_id]
        rel_current = gate["receipt_path"]
        rel_archive = self._archive_dir(gate_id)
        archived = self._archive_chain(root, rel_archive, self._vg_hash)
        if archived is False:
            return "INVALID"
        current = self._safe_json(root, rel_current)
        if current is None:
            if archived:
                return "INVALID"
            # An unsafe current receipt (symlink, hardlink, special file) is
            # INVALID, never "not run": absence and refusal must not look alike.
            return "NOT_RUN" if _sr.try_read_bytes(root, rel_current) is None else "INVALID"
        if not self._vg_surface_ok(gate_id, current):
            return "INVALID"
        if not closure["rerun_allowed"]:
            # pinned legacy shape: no rotation, no sequence link, exact hash
            if archived:
                return "INVALID"
            if "supersedes_receipt_sha256" in current:
                return "INVALID"
            if "sequence" in current:
                return "INVALID"
            if current["receipt_sha256"] != closure["pinned_current_receipt_sha256"]:
                return "INVALID"
            return "VALID"
        chain = {}
        for receipt in archived:
            if not self._vg_surface_ok(gate_id, receipt):
                return "INVALID"
            seq = receipt.get("sequence")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 or seq in chain:
                return "INVALID"
            chain[seq] = receipt
        n = current.get("sequence")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            return "INVALID"
        if sorted(chain) != list(range(1, n)):
            return "INVALID"
        chain[n] = current
        for seq in range(1, n + 1):
            receipt = chain[seq]
            link = receipt.get("supersedes_receipt_sha256")
            if seq == 1:
                if link is not None:
                    return "INVALID"
            else:
                if link is None or link != chain[seq - 1]["receipt_sha256"]:
                    return "INVALID"
                previous = self._ts(chain[seq - 1]["created_at"])
                this = self._ts(receipt["created_at"])
                if previous is None or this is None or not previous < this:
                    return "INVALID"
        return "VALID"

    def _gap_state(self, gate_id: str, root: pathlib.Path) -> str:
        """OPEN / CLOSED, derived from the CURRENT receipt -- no caller flag."""
        entry = self.closure_by_gate[gate_id]
        history = self._gate_history_state(gate_id, root)
        if history != "VALID":
            return "OPEN"  # NOT_RUN and INVALID both fail closed
        current = self._safe_json(root, self.by_gate[gate_id]["receipt_path"])
        if current is None:
            return "OPEN"
        # A non-synthetic receipt only closes the gap when the WHOLE receipt is
        # a valid PASS. `synthetic=false` on its own proves nothing.
        if self._vg_receipt_closes(gate_id, current, root):
            return "CLOSED"
        if entry["closure_mode"] == "capability_activation_receipts":
            required = entry["required_capability_ids"]
            if required and all(self._activation_is_valid(c, root) for c in required):
                return "CLOSED"
            return "OPEN"
        return "OPEN"  # non_synthetic_rerun: only a valid non-synthetic PASS closes it

    def _verdict(self, gate_id: str, root: pathlib.Path) -> str:
        return "NO_GO" if self._gap_state(gate_id, root) == "OPEN" else "READY_FOR_G7_REVIEW"

    # ------------------------------------------------------------------
    # today's real repository state
    # ------------------------------------------------------------------

    def test_real_repo_state_is_evaluated_fail_closed_and_consistently(self) -> None:
        """No frozen expectation: whatever exists, OPEN <=> NO_GO must hold."""
        missing = [
            c for c in self.by_capability if not self._activation_is_valid(c, REPO_ROOT)
        ]
        verdict = self._verdict("VG-A", REPO_ROOT)
        if missing:
            self.assertEqual(verdict, "NO_GO", f"missing/invalid activation receipts: {missing}")
        else:
            self.assertEqual(verdict, "READY_FOR_G7_REVIEW")

    def test_real_vga_history_is_valid_against_the_pin(self) -> None:
        self.assertEqual(self._gate_history_state("VG-A", REPO_ROOT), "VALID")

    def test_every_present_gate_history_on_disk_is_valid_or_absent(self) -> None:
        for gate_id in GATE_ORDER:
            with self.subTest(gate=gate_id):
                self.assertIn(
                    self._gate_history_state(gate_id, REPO_ROOT), {"NOT_RUN", "VALID"}
                )

    def test_any_activation_receipt_present_sits_at_a_map_declared_path(self) -> None:
        declared = {
            (REPO_ROOT / c["activation_receipt_path"]).resolve()
            for c in self.map["capabilities"]
        }
        root = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces" / "capability-receipts"
        if not root.is_dir():
            return
        on_disk = {p.resolve() for p in root.rglob("receipt.json")}
        self.assertEqual(on_disk - declared, set(), "undeclared capability receipt path")

    def test_real_delegated_receipt_roots_have_exact_json_membership(self) -> None:
        self.assertTrue(self._gate_receipt_root_is_closed(REPO_ROOT))
        self.assertTrue(self._capability_receipt_root_is_closed(REPO_ROOT))

    def test_undeclared_json_in_either_delegated_root_fails_whole_root_closed(self) -> None:
        cases = (
            (
                "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-X/receipt.json",
                self._gate_receipt_root_is_closed,
            ),
            (
                "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/unknown/receipt.json",
                self._capability_receipt_root_is_closed,
            ),
        )
        for rel, validator in cases:
            with self.subTest(path=rel):
                target = self.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}\n", encoding="utf-8")
                self.assertFalse(validator(self.root))
                target.unlink()

    def test_vga_receipt_is_never_rewritten_by_closure(self) -> None:
        """VG-A stays synthetic/conservative_unknown regardless of activation."""
        vga = _load(REPO_ROOT / self.by_gate["VG-A"]["receipt_path"])
        self.assertTrue(vga["synthetic"])
        self.assertEqual(vga["conclusion"], "conservative_unknown")
        self.assertFalse(self.closure_by_gate["VG-A"]["rerun_allowed"])
        self.assertNotIn("sequence", vga, "pinned legacy receipt must not be mutated")
        self.assertNotIn("supersedes_receipt_sha256", vga)

    # ------------------------------------------------------------------
    # M1: gate history / rotation chain
    # ------------------------------------------------------------------

    def test_absent_gate_receipt_is_not_run_and_open(self) -> None:
        for gate_id in GATE_ORDER:
            with self.subTest(gate=gate_id):
                self.assertEqual(self._gate_history_state(gate_id, self.root), "NOT_RUN")
                self.assertEqual(self._gap_state(gate_id, self.root), "OPEN")

    def test_first_run_gate_receipt_is_valid_and_closes_a_rerun_gate(self) -> None:
        self._write_vg("VG-B")
        self.assertEqual(self._gate_history_state("VG-B", self.root), "VALID")
        self.assertEqual(self._gap_state("VG-B", self.root), "CLOSED")

    def test_valid_rerun_chain_of_three_is_accepted(self) -> None:
        first = self._write_vg("VG-B", sequence=1, created_at="2026-10-01T00:00:00Z")
        self._archive_vg("VG-B", first)
        second = self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self._archive_vg("VG-B", second)
        self._write_vg(
            "VG-B",
            sequence=3,
            created_at="2026-10-03T00:00:00Z",
            supersedes_receipt_sha256=second["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "VALID")

    def test_archive_without_a_current_receipt_is_invalid(self) -> None:
        first = self._build_vg("VG-B")
        self._archive_vg("VG-B", first)
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")
        self.assertEqual(self._gap_state("VG-B", self.root), "OPEN")

    def test_rerun_with_missing_archive_entry_is_invalid(self) -> None:
        first = self._build_vg("VG-B", sequence=1)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )  # archive never written -> gap at sequence 1
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_archive_filename_not_equal_to_its_own_hash_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._write(
            self.root / self._archive_dir("VG-B") / f"{'b' * 64}.json", first
        )  # wrong filename
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_archived_receipt_named_receipt_json_is_never_scanned_as_current(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._write(self.root / self._archive_dir("VG-B") / "receipt.json", first)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        # archive/receipt.json is not <hash>.json, so the archive scan finds no
        # sequence-1 receipt and the chain has a gap
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_archived_receipt_body_tampering_is_detected(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        archived = self._archive_vg("VG-B", first)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "VALID")
        tampered = _load(archived)
        tampered["conclusion"] = "conservative_unknown"  # body edited, hash left stale
        archived.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_broken_supersedes_link_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._archive_vg("VG-B", first)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256="c" * 64,  # links to nothing
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_missing_supersedes_link_on_a_rerun_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._archive_vg("VG-B", first)
        self._write_vg("VG-B", sequence=2, created_at="2026-10-02T00:00:00Z")
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_supersedes_link_on_a_first_run_is_invalid(self) -> None:
        self._write_vg("VG-B", sequence=1, supersedes_receipt_sha256="d" * 64)
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_sequence_gap_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._archive_vg("VG-B", first)
        self._write_vg(
            "VG-B",
            sequence=3,  # 2 is missing
            created_at="2026-10-03T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_forked_duplicate_sequence_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1, created_at="2026-10-01T00:00:00Z")
        self._archive_vg("VG-B", first)
        fork = self._build_vg("VG-B", sequence=1, created_at="2026-10-01T12:00:00Z")
        self._archive_vg("VG-B", fork)  # two receipts both claiming sequence 1
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_orphan_archive_beyond_the_tip_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        self._archive_vg("VG-B", first)
        orphan = self._build_vg("VG-B", sequence=5, created_at="2026-10-09T00:00:00Z")
        self._archive_vg("VG-B", orphan)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_non_increasing_created_at_is_invalid_even_with_sound_sequence(self) -> None:
        """created_at is auxiliary, but backdating still fails closed."""
        first = self._write_vg("VG-B", sequence=1, created_at="2026-10-05T00:00:00Z")
        self._archive_vg("VG-B", first)
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-01T00:00:00Z",  # earlier than what it supersedes
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_archived_receipt_for_another_gate_is_invalid(self) -> None:
        first = self._write_vg("VG-B", sequence=1)
        foreign = self._build_vg("VG-C", sequence=1)
        self._archive_vg("VG-B", foreign)  # VG-C receipt inside VG-B's archive
        self._write_vg(
            "VG-B",
            sequence=2,
            created_at="2026-10-02T00:00:00Z",
            supersedes_receipt_sha256=first["receipt_sha256"],
        )
        self.assertEqual(self._gate_history_state("VG-B", self.root), "INVALID")

    def test_alternate_vga_current_receipt_is_rejected_against_the_pin(self) -> None:
        """A self-consistent but different VG-A receipt is an illegal replacement."""
        pinned = self.closure_by_gate["VG-A"]["pinned_current_receipt_sha256"]
        alternate = self._build_vg(
            "VG-A",
            feeder_rt="RT-015",
            synthetic=True,
            synthetic_reason="synthetic_authority_fake_signer",
            conclusion="conservative_unknown",
        )
        alternate.pop("sequence", None)
        alternate["receipt_sha256"] = self._vg_hash(alternate)
        self._write(self.root / self.by_gate["VG-A"]["receipt_path"], alternate)
        self.assertNotEqual(alternate["receipt_sha256"], pinned)
        self.assertEqual(self._gate_history_state("VG-A", self.root), "INVALID")
        self.assertEqual(self._gap_state("VG-A", self.root), "OPEN")

    def test_vga_with_a_non_empty_archive_is_rejected(self) -> None:
        """rerun_allowed=false means no rotation ever, not 'rotation we ignore'."""
        real = _load(REPO_ROOT / self.by_gate["VG-A"]["receipt_path"])
        self._write(self.root / self.by_gate["VG-A"]["receipt_path"], real)
        self.assertEqual(self._gate_history_state("VG-A", self.root), "VALID")
        self._write(
            self.root / self._archive_dir("VG-A") / f"{real['receipt_sha256']}.json", real
        )
        self.assertEqual(self._gate_history_state("VG-A", self.root), "INVALID")

    def test_vga_carrying_a_supersedes_link_is_rejected(self) -> None:
        real = _load(REPO_ROOT / self.by_gate["VG-A"]["receipt_path"])
        rotated = dict(real)
        rotated["supersedes_receipt_sha256"] = "e" * 64
        rotated["receipt_sha256"] = self._vg_hash(rotated)
        self._write(self.root / self.by_gate["VG-A"]["receipt_path"], rotated)
        self.assertEqual(self._gate_history_state("VG-A", self.root), "INVALID")

    # ------------------------------------------------------------------
    # closure evaluation
    # ------------------------------------------------------------------

    def _synthetic_vga(self) -> None:
        """Put the real pinned VG-A receipt into the sandbox root."""
        real = _load(REPO_ROOT / self.by_gate["VG-A"]["receipt_path"])
        self._write(self.root / self.by_gate["VG-A"]["receipt_path"], real)

    def test_no_activation_receipt_means_open_and_no_go(self) -> None:
        self._synthetic_vga()
        self.assertEqual(self._gap_state("VG-A", self.root), "OPEN")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_partial_activation_still_means_no_go(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_both_valid_activation_receipts_close_the_gap(self) -> None:
        """READY_FOR_G7_REVIEW must be reachable, or the design is a dead end."""
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport")
        self.assertEqual(self._gap_state("VG-A", self.root), "CLOSED")
        self.assertEqual(self._verdict("VG-A", self.root), "READY_FOR_G7_REVIEW")

    def test_synthetic_activation_receipt_cannot_close_the_gap(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation(
            "gateway-identity-transport",
            synthetic=True,
            synthetic_reason="mock_transport_only",
            conclusion="conservative_unknown",
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipt_without_owner_independent_pass_is_rejected(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport", owner_rt_independent_pass=False)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipt_signed_by_the_wrong_owner_is_rejected(self) -> None:
        self._synthetic_vga()
        self._write_activation("gateway-identity-transport")
        self._write_activation("cwork-authority-source", owner_rt="RT-019")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipt_hash_drift_is_rejected(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        path = self._write_activation("gateway-identity-transport")
        receipt = _load(path)
        receipt["created_at"] = "2026-09-30T00:00:00Z"  # body edited, hash left stale
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipt_artifact_drift_is_rejected(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport")
        artifact = (
            self.root
            / "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/gateway-identity-transport/evidence.md"
        )
        artifact.write_bytes(b"# tampered\n")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_conservative_conclusion_alone_does_not_close_the_gap(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source", conclusion="conservative_unknown")
        self._write_activation("gateway-identity-transport")
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipts_do_not_close_a_non_synthetic_rerun_gate(self) -> None:
        """VG-B~VG-E may not borrow VG-A's mapping."""
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport")
        for gate_id in ("VG-B", "VG-C", "VG-D", "VG-E"):
            with self.subTest(gate=gate_id):
                self._write_vg(
                    gate_id, synthetic=True, synthetic_reason="fake_provider",
                    conclusion="conservative_unknown",
                )
                self.assertEqual(self._gap_state(gate_id, self.root), "OPEN")

    def test_non_synthetic_current_receipt_needs_no_closure(self) -> None:
        for gate_id in ("VG-B", "VG-C", "VG-D", "VG-E"):
            with self.subTest(gate=gate_id):
                self._write_vg(gate_id)
                self.assertEqual(self._gap_state(gate_id, self.root), "CLOSED")

    # ------------------------------------------------------------------
    # B2: activation receipt validation hardening
    # ------------------------------------------------------------------

    def _assert_rejected(self, **overrides) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport", **overrides)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_zero_tests_run_is_rejected(self) -> None:
        self._assert_rejected(
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 0,
                "tests_failed": 0,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            }
        )

    def test_tests_failed_above_zero_is_rejected(self) -> None:
        self._assert_rejected(
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 5,
                "tests_failed": 1,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            }
        )

    def test_more_skipped_than_run_is_rejected(self) -> None:
        self._assert_rejected(
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 2,
                "tests_failed": 0,
                "tests_skipped": 3,
                "python_version": "3.11.14",
            }
        )

    def test_empty_artifacts_is_rejected(self) -> None:
        self._assert_rejected(artifacts=[])

    def test_missing_verifier_is_rejected(self) -> None:
        receipt = self._build_activation("gateway-identity-transport")
        receipt.pop("verifier")
        receipt["receipt_sha256"] = self._hash(receipt)
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write(
            self.root / self.by_capability["gateway-identity-transport"]["activation_receipt_path"],
            receipt,
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_verifier_equal_to_producer_is_rejected(self) -> None:
        self._assert_rejected(producer="agent-same", verifier="agent-same")

    def test_wrong_consumer_is_rejected(self) -> None:
        self._assert_rejected(consumers=["RT-024"])

    def test_extra_consumer_is_rejected(self) -> None:
        self._assert_rejected(consumers=["RT-026", "G6"])

    def test_wrong_closes_gate_ids_is_rejected(self) -> None:
        self._assert_rejected(closes_gate_ids=["VG-D"])

    def test_extra_closes_gate_id_is_rejected(self) -> None:
        self._assert_rejected(closes_gate_ids=["VG-A", "VG-D"])

    def test_undeclared_extra_field_is_rejected(self) -> None:
        self._assert_rejected(bonus_field="anything")

    def test_deep_forbidden_field_is_rejected(self) -> None:
        env = {
            "python_version": "3.11.14",
            "platform": "darwin-arm64",
            "toolchain_build": "cwk",
        }
        self._assert_rejected(environment_fingerprint={**env, "secret": "x"})

    def test_missing_required_evidence_role_is_rejected(self) -> None:
        receipt = self._build_activation("gateway-identity-transport")
        receipt["evidence_refs"] = receipt["evidence_refs"][:-1]  # drop one required role
        receipt["receipt_sha256"] = self._hash(receipt)
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write(
            self.root / self.by_capability["gateway-identity-transport"]["activation_receipt_path"],
            receipt,
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_the_two_capabilities_do_not_share_one_role_list(self) -> None:
        a = set(self.by_capability["cwork-authority-source"]["required_evidence_roles"])
        b = set(self.by_capability["gateway-identity-transport"]["required_evidence_roles"])
        self.assertTrue(a)
        self.assertTrue(b)
        self.assertNotEqual(a, b)

    def test_absolute_artifact_path_is_rejected(self) -> None:
        self._assert_rejected(artifacts=[{"path": "/etc/passwd", "sha256": "0" * 64}])

    def test_traversal_artifact_path_is_rejected(self) -> None:
        self._assert_rejected(
            artifacts=[{"path": "PR/../../etc/passwd", "sha256": "0" * 64}]
        )

    def test_artifact_pointing_at_a_directory_is_rejected(self) -> None:
        rel = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts"
        (self.root / rel).mkdir(parents=True, exist_ok=True)
        self._assert_rejected(artifacts=[{"path": rel, "sha256": "0" * 64}])

    def test_symlinked_artifact_leaf_is_rejected(self) -> None:
        real = self.root / "real_evidence.md"
        real.write_bytes(b"# real\n")
        rel = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/link.md"
        link = self.root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        digest = hashlib.sha256(real.read_bytes()).hexdigest()
        self._assert_rejected(artifacts=[{"path": rel, "sha256": digest}])

    def test_symlinked_path_component_is_rejected(self) -> None:
        """O_NOFOLLOW defends only the leaf, so components are checked too."""
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "evidence.md").write_bytes(b"# planted\n")
        base = self.root / "PR/PR-001-multitenant-knowledge-spaces/capability-receipts"
        base.mkdir(parents=True, exist_ok=True)
        (base / "hop").symlink_to(outside, target_is_directory=True)
        rel = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/hop/evidence.md"
        digest = hashlib.sha256((outside / "evidence.md").read_bytes()).hexdigest()
        self._assert_rejected(artifacts=[{"path": rel, "sha256": digest}])

    def test_hardlinked_artifact_is_rejected(self) -> None:
        original = self.root / "original.md"
        original.write_bytes(b"# original\n")
        rel = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/hardlink.md"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(original, target)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self._assert_rejected(artifacts=[{"path": rel, "sha256": digest}])

    def test_special_file_artifact_is_rejected(self) -> None:
        rel = "PR/PR-001-multitenant-knowledge-spaces/capability-receipts/pipe"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(target)
        self._assert_rejected(artifacts=[{"path": rel, "sha256": "0" * 64}])

    def test_activation_receipt_may_not_bind_a_gate_receipt(self) -> None:
        """Binding a VG receipt would invert the dependency order."""
        rel = "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-D/receipt.json"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{}\n")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        receipt = self._build_activation("gateway-identity-transport")
        receipt["artifacts"].append({"path": rel, "sha256": digest})
        receipt["receipt_sha256"] = self._hash(receipt)
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write(
            self.root / self.by_capability["gateway-identity-transport"]["activation_receipt_path"],
            receipt,
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_activation_receipt_may_not_bind_its_own_path(self) -> None:
        entry = self.by_capability["gateway-identity-transport"]
        rel = entry["activation_receipt_path"]
        receipt = self._build_activation("gateway-identity-transport")
        receipt["artifacts"].append({"path": rel, "sha256": "0" * 64})
        receipt["receipt_sha256"] = self._hash(receipt)
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write(self.root / rel, receipt)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_malformed_timestamp_is_rejected(self) -> None:
        for bad in (
            "2026-10-01 00:00:00Z",
            "2026-10-01T00:00:00+08:00",
            "2026-10-01T00:00:00",
            "2026-13-01T00:00:00Z",
            "2026-10-01T00:00:00.000Z",
        ):
            with self.subTest(created_at=bad):
                self.setUp()
                self._assert_rejected(created_at=bad)

    # ------------------------------------------------------------------
    # activation lifecycle: expiry, bounded TTL, renewal chain
    # ------------------------------------------------------------------

    def test_expired_current_activation_receipt_reopens_the_gap(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation(
            "gateway-identity-transport",
            created_at="2026-08-01T00:00:00Z",
            # before the injected evaluation instant, TTL inside the bound
            expires_at="2026-08-20T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("gateway-identity-transport", self.root), "EXPIRED"
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_created_at_not_before_expires_at_is_rejected(self) -> None:
        self._assert_rejected(
            created_at="2026-10-01T00:00:00Z", expires_at="2026-10-01T00:00:00Z"
        )

    def test_ttl_exactly_at_the_frozen_bound_is_accepted(self) -> None:
        """The upper bound is inclusive: exactly max_validity_seconds is valid."""
        self._synthetic_vga()
        for capability_id in self.by_capability:
            entry = self.by_capability[capability_id]
            created = _dt.datetime(2026, 10, 10, tzinfo=_dt.timezone.utc)
            expires = created + _dt.timedelta(seconds=entry["max_validity_seconds"])
            self._write_activation(
                capability_id,
                created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        self.assertEqual(self._gap_state("VG-A", self.root), "CLOSED")
        self.assertEqual(self._verdict("VG-A", self.root), "READY_FOR_G7_REVIEW")

    def test_ttl_one_second_over_the_frozen_bound_is_rejected(self) -> None:
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        entry = self.by_capability["gateway-identity-transport"]
        created = _dt.datetime(2026, 10, 10, tzinfo=_dt.timezone.utc)
        expires = created + _dt.timedelta(seconds=entry["max_validity_seconds"] + 1)
        self._write_activation(
            "gateway-identity-transport",
            created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_nine_thousand_year_ttl_is_rejected(self) -> None:
        """An unbounded TTL would re-create the dead end expiry exists to remove."""
        self._assert_rejected(
            created_at="2026-10-01T00:00:00Z", expires_at="9999-12-31T23:59:59Z"
        )

    def test_every_capability_declares_a_bounded_ttl_within_the_ceiling(self) -> None:
        ceiling = self.map["activation_max_validity_seconds_ceiling"]
        for entry in self.map["capabilities"]:
            with self.subTest(capability=entry["capability_id"]):
                self.assertGreater(entry["max_validity_seconds"], 0)
                self.assertLessEqual(entry["max_validity_seconds"], ceiling)

    def test_first_issue_activation_chain_is_valid(self) -> None:
        self._write_activation("cwork-authority-source")
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "VALID"
        )

    def test_valid_activation_renewal_chain_is_accepted(self) -> None:
        """Expiry is not a dead end: a fresh probe renews at sequence+1."""
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        first = self._build_activation(
            "gateway-identity-transport",
            sequence=1,
            created_at="2026-08-01T00:00:00Z",
            expires_at="2026-08-20T00:00:00Z",
        )
        self._archive_activation("gateway-identity-transport", first)
        probe = self._write_probe("gateway-identity-transport", 2)
        self._write_activation(
            "gateway-identity-transport",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=probe,
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-10-30T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("gateway-identity-transport", self.root), "VALID"
        )
        self.assertEqual(self._verdict("VG-A", self.root), "READY_FOR_G7_REVIEW")

    def test_renewal_without_archiving_the_superseded_receipt_is_invalid(self) -> None:
        first = self._build_activation("cwork-authority-source", sequence=1)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=self._write_probe("cwork-authority-source", 2),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_renewal_with_a_broken_previous_link_is_invalid(self) -> None:
        first = self._build_activation("cwork-authority-source", sequence=1)
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256="f" * 64,
            renewal_probe_ref=self._write_probe("cwork-authority-source", 2),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_renewal_without_a_fresh_probe_ref_is_invalid(self) -> None:
        first = self._build_activation("cwork-authority-source", sequence=1)
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_activation_archive_filename_must_equal_its_own_hash(self) -> None:
        entry = self.by_capability["cwork-authority-source"]
        first = self._build_activation("cwork-authority-source", sequence=1)
        self._write(self.root / entry["activation_archive_dir"] / f"{'a' * 64}.json", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=self._write_probe("cwork-authority-source", 2),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_backdated_activation_renewal_is_invalid(self) -> None:
        first = self._build_activation(
            "cwork-authority-source",
            sequence=1,
            created_at="2026-10-05T00:00:00Z",
            expires_at="2026-11-05T00:00:00Z",
        )
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            # An honest probe for THIS receipt's created_at, so the only thing
            # wrong with the renewal is the backdating under test.
            renewal_probe_ref=self._write_probe(
                "cwork-authority-source", 2, observed_at="2026-09-30T23:00:00Z"
            ),
            created_at="2026-10-01T00:00:00Z",  # earlier than what it supersedes
            expires_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_first_issue_carrying_a_previous_link_is_invalid(self) -> None:
        self._write_activation(
            "cwork-authority-source", sequence=1, previous_receipt_sha256="a" * 64
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_activation_archive_without_a_current_receipt_is_invalid(self) -> None:
        first = self._build_activation("cwork-authority-source", sequence=1)
        self._archive_activation("cwork-authority-source", first)
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    # ------------------------------------------------------------------
    # subject binding: ancestry, owner CODE scope, exact tested tree
    #
    # Every test below builds a receipt that is fully schema-legal and whose
    # own hash recomputes. What rejects it is the binding the evaluator
    # RECOMPUTES from the repository, so a passing assertion here means the
    # rule is enforced rather than merely written down.
    # ------------------------------------------------------------------

    def _write_gateway(self, **overrides) -> None:
        """VG-A plus one honest activation, plus a gateway receipt under attack."""
        entry = self.by_capability["gateway-identity-transport"]
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        receipt = self._build_activation("gateway-identity-transport", **overrides)
        self._write(self.root / entry["activation_receipt_path"], receipt)

    def test_a_schema_valid_but_nonexistent_subject_commit_is_rejected(self) -> None:
        """The old fixture's 'a' * 40 matched the pattern and proved nothing."""
        self._write_gateway(tested_subject_commit="a" * 40)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_a_subject_commit_that_touched_only_evidence_is_rejected(self) -> None:
        """Evidence may never bootstrap its own subject binding.

        The capability's own `capability-receipts/<id>/` directory used to be
        passed as owner scope, so a commit that changed nothing but the receipt
        satisfied 'the subject modified the owning package'.
        """
        entry = self.by_capability["gateway-identity-transport"]
        self._synthetic_vga()
        self._write_activation("cwork-authority-source")
        self._write_activation("gateway-identity-transport")
        # Freeze what is on disk: this commit touches receipts and evidence and
        # no owner CODE whatsoever.
        evidence_only = self._evaluation_commit(self.root)
        self.assertIsNotNone(evidence_only)
        receipt = self._build_activation(
            "gateway-identity-transport",
            tested_subject_commit=evidence_only,
            # Computed AT the attacker's commit, so the tree digest matches and
            # owner CODE scope is the only thing left to reject it.
            owner_scope_tree_sha256=self.git.owner_scope_tree_sha256(
                evidence_only, entry["owner_code_path_prefixes"]
            ),
        )
        self._write(self.root / entry["activation_receipt_path"], receipt)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_a_receipt_repointed_at_a_neighbouring_commit_is_rejected(self) -> None:
        """Ancestry and owner scope leave every qualifying commit interchangeable.

        The neighbour satisfies BOTH: it is an ancestor of the evidence commit
        and it touches the owner package. Only the tree digest notices that it
        is not the code the receipt describes.
        """
        neighbour = self._extra_commit(
            "RT/RT-023/src/capability.py", b'"""drifted after the probe"""\n'
        )
        self.assertNotEqual(neighbour, self.subject_commit)
        self._write_gateway(tested_subject_commit=neighbour)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_owner_scope_tree_digest_drift_is_rejected(self) -> None:
        self._write_gateway(owner_scope_tree_sha256="c" * 64)
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_a_fabricated_environment_fingerprint_is_rejected(self) -> None:
        """'It worked somewhere' may never stand in for 'it worked here'."""
        self._write_gateway(
            environment_fingerprint=self._environment_fingerprint(
                platform="some-machine-that-is-not-this-one"
            )
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    def test_a_fabricated_python_version_is_rejected(self) -> None:
        observed = platform.python_version()
        forged = "3.99.99" if observed != "3.99.99" else "3.98.98"
        self._write_gateway(
            environment_fingerprint=self._environment_fingerprint(python_version=forged)
        )
        self.assertEqual(self._verdict("VG-A", self.root), "NO_GO")

    # --- the same attacks against a VG receipt --------------------------

    def test_vg_receipt_with_a_nonexistent_subject_commit_is_rejected(self) -> None:
        self._write_vg("VG-B", tested_subject_commit="a" * 40)
        self.assertEqual(self._gate_history_state("VG-B", self.root), "VALID")
        self.assertEqual(self._gap_state("VG-B", self.root), "OPEN")

    def test_rerun_gate_receipt_without_a_subject_binding_does_not_close(self) -> None:
        """VG-A's pinned legacy shape is not a shape VG-B~VG-E may borrow."""
        receipt = self._build_vg("VG-B")
        for field in (
            "tested_subject_commit",
            "owner_scope_tree_sha256",
            "environment_fingerprint",
        ):
            receipt.pop(field, None)
        receipt["receipt_sha256"] = self._vg_hash(receipt)
        self._write(self.root / self.by_gate["VG-B"]["receipt_path"], receipt)
        # Structurally legal -- the fields are optional at schema level -- and
        # still unable to close anything.
        self.assertEqual(self._gate_history_state("VG-B", self.root), "VALID")
        self.assertEqual(self._gap_state("VG-B", self.root), "OPEN")

    def test_vg_subject_commit_from_a_lookalike_sibling_package_is_rejected(self) -> None:
        """`startswith` would have accepted RT-023-evil as RT-023's own scope."""
        sibling = self._extra_commit(
            "RT/RT-023-evil/src/capability.py", b'"""planted next door"""\n'
        )
        self._write_vg("VG-D", tested_subject_commit=sibling)
        self.assertEqual(self._gap_state("VG-D", self.root), "OPEN")

    def test_vg_owner_scope_tree_digest_drift_is_rejected(self) -> None:
        self._write_vg("VG-B", owner_scope_tree_sha256="d" * 64)
        self.assertEqual(self._gap_state("VG-B", self.root), "OPEN")

    def test_vg_fabricated_environment_fingerprint_is_rejected(self) -> None:
        self._write_vg(
            "VG-B",
            environment_fingerprint=self._environment_fingerprint(
                platform="some-machine-that-is-not-this-one"
            ),
        )
        self.assertEqual(self._gap_state("VG-B", self.root), "OPEN")

    # ------------------------------------------------------------------
    # renewal probes: a manifest that is recomputed, not merely present
    # ------------------------------------------------------------------

    def _renewal_with_probe(self, **probe_overrides) -> str:
        """A sequence-2 renewal whose probe manifest carries `probe_overrides`."""
        first = self._build_activation(
            "cwork-authority-source",
            sequence=1,
            created_at="2026-10-05T00:00:00Z",
            expires_at="2026-11-05T00:00:00Z",
        )
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=self._write_probe(
                "cwork-authority-source", 2, **probe_overrides
            ),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-10T00:00:00Z",
        )
        return self._activation_chain_state("cwork-authority-source", self.root)

    def test_an_honestly_signed_renewal_probe_is_accepted(self) -> None:
        """The control. Without it every rejection below could be incidental."""
        self.assertEqual(self._renewal_with_probe(), "VALID")

    def test_a_forged_probe_signature_is_rejected(self) -> None:
        """The evaluator recomputes the HMAC with its own key."""
        self.assertEqual(self._renewal_with_probe(signature="0" * 64), "INVALID")

    def test_a_probe_declaring_an_earlier_sequence_is_rejected(self) -> None:
        """Correctly signed, correctly placed, and still a replay."""
        first = self._build_activation(
            "cwork-authority-source",
            sequence=1,
            created_at="2026-10-05T00:00:00Z",
            expires_at="2026-11-05T00:00:00Z",
        )
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=self._write_probe(
                "cwork-authority-source", 1, at_sequence=2
            ),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-10T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_a_probe_replayed_from_an_earlier_sequence_path_is_rejected(self) -> None:
        first = self._build_activation(
            "cwork-authority-source",
            sequence=1,
            created_at="2026-10-05T00:00:00Z",
            expires_at="2026-11-05T00:00:00Z",
        )
        self._archive_activation("cwork-authority-source", first)
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=self._write_probe("cwork-authority-source", 1),
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-10T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    def test_a_probe_certifying_a_different_subject_is_rejected(self) -> None:
        self.assertEqual(
            self._renewal_with_probe(tested_subject_commit="b" * 40), "INVALID"
        )

    def test_a_probe_older_than_the_frozen_maximum_age_is_rejected(self) -> None:
        self.assertEqual(
            self._renewal_with_probe(observed_at="2026-01-01T00:00:00Z"), "INVALID"
        )

    def test_a_probe_observed_after_the_receipt_it_certifies_is_rejected(self) -> None:
        """A probe dated after its own renewal is backfill, not observation."""
        self.assertEqual(
            self._renewal_with_probe(observed_at="2026-10-11T00:00:00Z"), "INVALID"
        )

    def test_a_failing_probe_cannot_certify_a_renewal(self) -> None:
        self.assertEqual(self._renewal_with_probe(result="fail"), "INVALID")

    def test_a_probe_from_a_foreign_capability_is_rejected(self) -> None:
        self.assertEqual(
            self._renewal_with_probe(capability_id="gateway-identity-transport"),
            "INVALID",
        )

    def test_a_probe_with_a_fabricated_environment_is_rejected(self) -> None:
        self.assertEqual(
            self._renewal_with_probe(
                environment_fingerprint=self._environment_fingerprint(
                    platform="some-machine-that-is-not-this-one"
                )
            ),
            "INVALID",
        )

    def test_a_probe_that_did_not_land_with_its_receipt_is_rejected(self) -> None:
        """A probe already sitting in the tree did not certify THIS renewal."""
        first = self._build_activation(
            "cwork-authority-source",
            sequence=1,
            created_at="2026-10-05T00:00:00Z",
            expires_at="2026-11-05T00:00:00Z",
        )
        self._archive_activation("cwork-authority-source", first)
        probe = self._write_probe("cwork-authority-source", 2)
        # Freeze the probe in a commit of its own, BEFORE the receipt exists.
        commit_all(self.git, "probe landed ahead of the renewal")
        self._write_activation(
            "cwork-authority-source",
            sequence=2,
            previous_receipt_sha256=first["receipt_sha256"],
            renewal_probe_ref=probe,
            created_at="2026-10-10T00:00:00Z",
            expires_at="2026-11-10T00:00:00Z",
        )
        self.assertEqual(
            self._activation_chain_state("cwork-authority-source", self.root), "INVALID"
        )

    # ------------------------------------------------------------------
    # B1: no downstream VG receipt may be an activation prerequisite
    # ------------------------------------------------------------------

    def test_no_capability_requires_or_names_a_vg_gate_as_a_prerequisite(self) -> None:
        """VG-B is not upstream of RT-017's receipt; VG-D is not upstream of RT-023's."""
        forbidden = {"VG-A", "VG-B", "VG-C", "VG-D", "VG-E"}
        for entry in self.map["capabilities"]:
            with self.subTest(capability=entry["capability_id"]):
                for role in entry["required_evidence_roles"]:
                    self.assertNotIn(role.upper().replace("_", "-"), forbidden)
                    self.assertNotIn("vg", role.split("_"))
                for condition in entry["production_conditions"]:
                    for gate in forbidden:
                        self.assertNotIn(
                            gate, condition, f"{entry['capability_id']} binds {gate}"
                        )

    def test_closure_condition_never_requires_a_downstream_gate(self) -> None:
        pairs = {
            "cwork-authority-source": "VG-B",
            "gateway-identity-transport": "VG-D",
        }
        for capability_id, gate in pairs.items():
            with self.subTest(capability=capability_id):
                entry = self.by_capability[capability_id]
                text = entry["closure_condition"]
                if gate in text:
                    self.assertTrue(
                        re.search(rf"{gate}\s*不是本\s*receipt\s*的前置", text)
                        or f"{gate} 不是本 receipt 的前置" in text,
                        f"{capability_id} closure_condition still requires {gate}",
                    )

    def test_generation_timing_is_acyclic_and_names_the_owner_pass_first(self) -> None:
        for entry in self.map["capabilities"]:
            with self.subTest(capability=entry["capability_id"]):
                timing = entry["generation_timing"]
                self.assertIn(entry["owner_rt"], timing)
                self.assertIn("RT-026", timing)
                self.assertRegex(timing, r"acyclic")

    def test_map_declares_the_ttl_bound_and_subject_binding_rules(self) -> None:
        self.assertIn("max_validity_seconds", self.map["activation_validity_bound_rule"])
        self.assertIn("ancestor", self.map["activation_subject_binding_rule"].lower())
        self.assertRegex(
            self.map["activation_subject_binding_rule"], r"NOT required to equal"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
