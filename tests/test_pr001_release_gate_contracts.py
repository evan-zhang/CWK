"""PR-001 release gate contracts G1..G7.

Five frozen JSON contracts are the machine authority:

1. release_gate_registry_v1.json          - the seven entries, config not state
2. release_gate_registry_v1.schema.json   - proves it can never carry status
3. release_gate_receipt_v1.schema.json    - G1..G6, release VERIFICATION
4. release_authorization_receipt_v1.schema.json - G7 alone, release AUTHORIZATION
5. contracts/rt026/schemas/go_no_go_report_v1.schema.json - RT-026 verdict input

Split out of tests/test_pr001_gate_contracts.py (which owns VG-A..VG-E, the
synthetic closure map and capability activation) so each family stays reviewable
on its own. Pure stdlib, hermetic: no network, no jsonschema, no fixtures outside
tempfile.

Two structural facts drive most of these tests:

* G1..G6 answer "was this verified?" and G7 answers "may this be deployed?".
  Different question, different trust root, therefore different schema, different
  filename (authorization.json vs receipt.json) and different domain separator.
  Collapsing them would let a pipeline mint its own deployment permission.
* release-gate-receipts/ is a SEPARATE root from gate-receipts/. The latter is
  already validated as an exact VG closure - every receipt.json under it must sit
  at a VG registry path - so putting G receipts there would register as undeclared
  extras and force that check to be weakened.
"""

from __future__ import annotations

import hashlib
import copy
import json
import pathlib
import re
import unicodedata
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PR_ROOT = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces"
GATES_DIR = PR_ROOT / "contracts" / "gates"
VG_REGISTRY_PATH = GATES_DIR / "gate_registry_v1.json"
VG_REGISTRY_SCHEMA_PATH = GATES_DIR / "gate_registry_v1.schema.json"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _keyed_rt_acceptance_constraints_hold(reports: dict, schema: dict) -> bool:
    """Execute the schema's keyed RT style/path/provenance constraints.

    The repository intentionally has no jsonschema dependency.  This small,
    closed evaluator covers only the keyed surface frozen specifically for
    rt_acceptance_reports: named/nested ``properties``, regex
    ``patternProperties``, ``allOf``/``oneOf``, required keys, const/enum
    properties and ``not: {required: [...]}``. Negative mutations below prove
    the schema nodes are load-bearing rather than prose.
    """

    def accepts(value, constraint: dict) -> bool:
        if "const" in constraint and value != constraint["const"]:
            return False
        if "enum" in constraint and value not in constraint["enum"]:
            return False
        one_of = constraint.get("oneOf") or ()
        if one_of and sum(accepts(value, item) for item in one_of) != 1:
            return False
        if not isinstance(value, dict):
            return True
        if any(key not in value for key in constraint.get("required", ())):
            return False
        forbidden = (constraint.get("not") or {}).get("required", ())
        if forbidden and all(key in value for key in forbidden):
            return False
        for key, spec in (constraint.get("properties") or {}).items():
            if key in value and not accepts(value[key], spec):
                return False
        if any(not accepts(value, item) for item in constraint.get("allOf", ())):
            return False
        return True

    for pattern, constraint in (schema.get("patternProperties") or {}).items():
        compiled = re.compile(pattern)
        for rt_id, value in reports.items():
            if compiled.fullmatch(rt_id) and not accepts(value, constraint):
                return False
    for clause in schema["allOf"]:
        for rt_id, constraint in (clause.get("properties") or {}).items():
            if rt_id in reports and not accepts(reports[rt_id], constraint):
                return False
        for pattern, constraint in (clause.get("patternProperties") or {}).items():
            compiled = re.compile(pattern)
            for rt_id, value in reports.items():
                if compiled.fullmatch(rt_id) and not accepts(value, constraint):
                    return False
    return True


RELEASE_REGISTRY_PATH = GATES_DIR / "release_gate_registry_v1.json"
RELEASE_REGISTRY_SCHEMA_PATH = GATES_DIR / "release_gate_registry_v1.schema.json"
RELEASE_RECEIPT_SCHEMA_PATH = GATES_DIR / "release_gate_receipt_v1.schema.json"
RELEASE_AUTH_SCHEMA_PATH = GATES_DIR / "release_authorization_receipt_v1.schema.json"
GO_NO_GO_SCHEMA_PATH = (
    PR_ROOT / "contracts" / "rt026" / "schemas" / "go_no_go_report_v1.schema.json"
)

RELEASE_CONTRACT_FILES = (
    RELEASE_REGISTRY_PATH,
    RELEASE_REGISTRY_SCHEMA_PATH,
    RELEASE_RECEIPT_SCHEMA_PATH,
    RELEASE_AUTH_SCHEMA_PATH,
    GO_NO_GO_SCHEMA_PATH,
)
RELEASE_CONTRACT_SCHEMAS = (
    RELEASE_REGISTRY_SCHEMA_PATH,
    RELEASE_RECEIPT_SCHEMA_PATH,
    RELEASE_AUTH_SCHEMA_PATH,
    GO_NO_GO_SCHEMA_PATH,
)

RELEASE_GATE_ORDER = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
VERIFICATION_RELEASE_GATES = ("G1", "G2", "G3", "G4", "G5", "G6")
RELEASE_RECEIPT_DOMAIN = b"cwk-release-gate-receipt-v1\x00"
RELEASE_AUTH_DOMAIN = b"cwk-release-authorization-receipt-v1\x00"
RELEASE_RECEIPT_ROOT = PR_ROOT / "release-gate-receipts"

# The frozen DAG, transcribed independently from plans/开发计划.md §24 and the
# release design review. The registry must match this exactly; it is written out
# here rather than derived so that a registry edit cannot silently redefine the
# rule it is supposed to be checked against.
FROZEN_RELEASE_DAG = {
    "G1": {"G0", "RT-011"},
    "G2": {"G1", "RT-012", "RT-013"},
    "G3": {"G2", "RT-014", "RT-015", "RT-016", "VG-A"},
    "G4": {"G3", "RT-019", "RT-020", "RT-021", "VG-C"},
    "G5": {"G4", "RT-022", "RT-023", "VG-D", "CAP:gateway-identity-transport"},
    "G6": (
        {"G1", "G2", "G3", "G4", "G5"}
        | {f"RT-{n:03d}" for n in range(17, 27)}
        | {"VG-A", "VG-B", "VG-C", "VG-D", "VG-E"}
        | {"CAP:cwork-authority-source", "CAP:gateway-identity-transport"}
        | {f"SG:RT-{n:03d}" for n in range(17, 27)}
        | {"RT-026-GO-NO-GO"}
    ),
    "G7": {"G6"},
}

# Release-gate consumption of verification gates, and its inverse. Both are
# spelled out so the two registries can be cross-checked in both directions.
FROZEN_RELEASE_CONSUMES_VG = {
    "G1": [],
    "G2": [],
    "G3": ["VG-A"],
    "G4": ["VG-C"],
    "G5": ["VG-D"],
    "G6": ["VG-A", "VG-B", "VG-C", "VG-D", "VG-E"],
    "G7": [],
}
FROZEN_VG_RELEASE_CONSUMERS = {
    "VG-A": ["G3", "G6"],
    "VG-B": ["G6"],
    "VG-C": ["G4", "G6"],
    "VG-D": ["G5", "G6"],
    "VG-E": ["G6"],
}

FROZEN_RELEASE_FEEDERS = {
    "G1": ["RT-011"],
    "G2": ["RT-012", "RT-013"],
    "G3": ["RT-014", "RT-015", "RT-016"],
    "G4": ["RT-019", "RT-020", "RT-021"],
    "G5": ["RT-022", "RT-023"],
    "G6": ["RT-024", "RT-025", "RT-026"],
    "G7": [],
}


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


class ReleaseGateContractFilesTests(unittest.TestCase):
    def test_all_five_release_contract_files_exist(self) -> None:
        for path in RELEASE_CONTRACT_FILES:
            self.assertTrue(path.is_file(), f"missing release contract file: {path}")

    def test_files_are_valid_utf8_json(self) -> None:
        for path in RELEASE_CONTRACT_FILES:
            self.assertIsInstance(_load(path), dict, path.name)

    def test_every_pattern_compiles(self) -> None:
        for path in RELEASE_CONTRACT_SCHEMAS:
            for pattern in _iter_patterns(_load(path)):
                try:
                    re.compile(pattern)
                except re.error as exc:  # pragma: no cover - failure detail
                    self.fail(f"{path.name}: bad pattern {pattern!r}: {exc}")

    def test_no_release_gate_receipt_exists_yet(self) -> None:
        """Wave-0 invariant: the contract is frozen, the evidence is absent.

        Every one of the seven entries must therefore resolve to NOT_RUN. A file
        appearing here would be evidence manufactured by contract work, which is
        exactly the thing the registry forbids.
        """
        self.assertFalse(
            RELEASE_RECEIPT_ROOT.exists(),
            f"{RELEASE_RECEIPT_ROOT} must not be created by contract work",
        )

    def test_release_root_is_disjoint_from_the_verification_gate_root(self) -> None:
        registry = _load(RELEASE_REGISTRY_PATH)
        root = registry["receipt_root"]
        self.assertNotIn("/gate-receipts/", "/" + root)
        self.assertTrue(root.endswith("/release-gate-receipts/"))
        for gate in registry["gates"]:
            self.assertTrue(gate["receipt_path"].startswith(root), gate["gate_id"])
            self.assertNotIn(
                "PR/PR-001-multitenant-knowledge-spaces/gate-receipts/",
                gate["receipt_path"],
            )


class ReleaseRegistrySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load(RELEASE_REGISTRY_SCHEMA_PATH)
        self.registry = _load(RELEASE_REGISTRY_PATH)

    def test_registry_top_level_keys_match_schema(self) -> None:
        self.assertEqual(sorted(self.registry), sorted(self.schema["properties"]))
        self.assertEqual(sorted(self.schema["required"]), sorted(self.schema["properties"]))

    def test_gate_entry_keys_match_schema(self) -> None:
        item_props = sorted(self.schema["properties"]["gates"]["items"]["properties"])
        for gate in self.registry["gates"]:
            self.assertEqual(sorted(gate), item_props, gate["gate_id"])

    def test_schema_is_closed_at_every_level(self) -> None:
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(self.schema["unevaluatedProperties"], False)
        items = self.schema["properties"]["gates"]["items"]
        self.assertIs(items["additionalProperties"], False)
        self.assertIs(items["unevaluatedProperties"], False)
        boot = self.schema["properties"]["bootstrap_gate"]
        self.assertIs(boot["additionalProperties"], False)

    def test_registry_schema_freezes_seven_entries(self) -> None:
        gates_spec = self.schema["properties"]["gates"]
        self.assertEqual(gates_spec["minItems"], 7)
        self.assertEqual(gates_spec["maxItems"], 7)

    def test_schema_forbids_mutable_status_keys(self) -> None:
        """Config-not-state, enforced structurally rather than by convention."""
        forbidden = set(self.schema["forbiddenEntryKeys"])
        for key in (
            "status",
            "conclusion",
            "verdict",
            "result",
            "passed",
            "last_run_at",
            "executed",
            "satisfied",
            "closed",
            "authorized",
        ):
            self.assertIn(key, forbidden, key)
        allowed = set(self.schema["properties"]["gates"]["items"]["properties"])
        allowed |= set(self.schema["properties"])
        self.assertEqual(forbidden & allowed, set(), "forbidden key is also allowed")

    def test_registry_carries_no_status_field_anywhere(self) -> None:
        forbidden = set(self.schema["forbiddenEntryKeys"])

        def walk(node, where):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key, forbidden, f"{where}.{key}")
                    walk(value, f"{where}.{key}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{where}[{i}]")

        walk(self.registry, "registry")

    def test_authorized_is_forbidden_so_requirement_cannot_become_a_grant(self) -> None:
        """`requires_external_authorization` is policy; `authorized` would be state.

        The registry must be able to say G7 NEEDS authorization without ever
        being able to record that it HAS it.
        """
        self.assertIn("authorized", self.schema["forbiddenEntryKeys"])
        props = self.schema["properties"]["gates"]["items"]["properties"]
        self.assertIn("requires_external_authorization", props)
        self.assertNotIn("authorized", props)

    def test_registry_values_satisfy_schema_patterns_and_enums(self) -> None:
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
                if "const" in spec:
                    self.assertEqual(value, spec["const"], f"{gate['gate_id']}.{key}")
                if spec.get("type") == "array":
                    self.assertIsInstance(value, list)
                    self.assertLessEqual(len(value), spec["maxItems"])
                    self.assertGreaterEqual(len(value), spec.get("minItems", 0))
                    inner = spec.get("items", {})
                    for element in value:
                        if "enum" in inner:
                            self.assertIn(element, inner["enum"], element)
                        if "pattern" in inner:
                            self.assertIsNotNone(
                                re.compile(inner["pattern"]).search(element), element
                            )



class ReleaseRegistryContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(RELEASE_REGISTRY_PATH)
        self.by_gate = {g["gate_id"]: g for g in self.registry["gates"]}

    def test_exactly_the_seven_release_gates(self) -> None:
        self.assertEqual(tuple(self.by_gate), RELEASE_GATE_ORDER)

    def test_g0_is_not_an_entry(self) -> None:
        """G0 reviews documents; it has no feeder RT, no tests, no hashes.

        Modelling it as an eighth entry would let a Markdown file impersonate a
        verifiable release gate.
        """
        self.assertNotIn("G0", self.by_gate)
        boot = self.registry["bootstrap_gate"]
        self.assertEqual(boot["gate_id"], "G0")
        self.assertIs(boot["in_registry"], False)

    def test_g0_resolves_only_through_the_frozen_final_wave0_report(self) -> None:
        """The stale r4 report must not be able to authorise G1.

        r4 reviewed a superseded revision of the documents, so trusting it would
        let a review that never saw the current contracts satisfy the gate.
        """
        boot = self.registry["bootstrap_gate"]
        self.assertTrue(boot["historical_narrative_ref"].endswith("审核报告-r4.md"))
        final_path = REPO_ROOT / boot["final_wave0_review_report_path"]
        self.assertNotEqual(
            boot["final_wave0_review_report_path"], boot["historical_narrative_ref"]
        )
        rule = boot["resolution_rule"]
        self.assertIn("NOT sufficient", rule)
        self.assertIn("NOT_RUN", rule)
        # It may legitimately be absent today; absence means G1 is NOT_RUN.
        if not final_path.exists():
            self.assertIn("absent", rule)

    def test_the_frozen_dag_is_exact(self) -> None:
        """Set EQUALITY, not containment.

        A subset rule would let a weakened gate pass with a prerequisite quietly
        dropped; a superset rule would let a padded one pass. Both are rejected.
        """
        for gate_id, expected in FROZEN_RELEASE_DAG.items():
            with self.subTest(gate=gate_id):
                ids = self.by_gate[gate_id]["required_prerequisite_ids"]
                self.assertEqual(set(ids), expected)
                self.assertEqual(len(ids), len(set(ids)), "duplicate prerequisite id")

    def test_no_gate_cites_itself_or_a_later_gate(self) -> None:
        for i, gate_id in enumerate(RELEASE_GATE_ORDER):
            later = set(RELEASE_GATE_ORDER[i:])
            refs = set(self.by_gate[gate_id]["required_prerequisite_ids"])
            self.assertEqual(refs & later, set(), f"{gate_id} cites itself or later")

    def test_nothing_verifies_against_the_g7_authorization(self) -> None:
        self.assertEqual(self.registry["downstream_only_ids"], ["G7"])
        for gate in self.registry["gates"]:
            self.assertNotIn("G7", gate["required_prerequisite_ids"], gate["gate_id"])

    def test_g6_binds_every_upstream_release_gate_individually(self) -> None:
        """Not just G5.

        Chaining transitively would let a revoked, expired or hash-drifted
        mid-chain gate survive unnoticed inside the final verdict; re-binding
        each hash is what makes the drift detectable.
        """
        refs = set(self.by_gate["G6"]["required_prerequisite_ids"])
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            self.assertIn(gid, refs)

    def test_g6_binds_all_ten_security_receipts_and_both_activations(self) -> None:
        refs = set(self.by_gate["G6"]["required_prerequisite_ids"])
        for n in range(17, 27):
            self.assertIn(f"SG:RT-{n:03d}", refs)
            self.assertIn(f"RT-{n:03d}", refs)
        self.assertIn("CAP:cwork-authority-source", refs)
        self.assertIn("CAP:gateway-identity-transport", refs)
        self.assertIn("RT-026-GO-NO-GO", refs)

    def test_g7_authority_is_narrow_by_construction(self) -> None:
        """Separation of duties, expressed as a one-element prerequisite set.

        If the authorizer could cite RT/VG/G1..G5 evidence directly it could form
        its own view of whether the candidate was verified, overruling or
        repairing the verifier. Pinning the set to {G6} means it can only decide
        whether an already-verified candidate may be deployed.
        """
        g7 = self.by_gate["G7"]
        self.assertEqual(g7["required_prerequisite_ids"], ["G6"])
        self.assertEqual(g7["feeder_rts"], [])
        self.assertEqual(g7["consumes_verification_gates"], [])
        self.assertEqual(g7["direct_rt_consumers"], [])
        self.assertIn("NARROW AUTHORITY", g7["static_note"])

    def test_gate_kind_schema_and_authorization_flag_agree(self) -> None:
        for gate in self.registry["gates"]:
            with self.subTest(gate=gate["gate_id"]):
                is_auth = gate["gate_id"] == "G7"
                self.assertEqual(
                    gate["gate_kind"],
                    "release_authorization" if is_auth else "release_verification",
                )
                self.assertEqual(
                    gate["receipt_schema_id"],
                    self.registry["authorization_schema_id"]
                    if is_auth
                    else self.registry["receipt_schema_id"],
                )
                self.assertEqual(gate["requires_external_authorization"], is_auth)

    def test_g7_uses_a_different_filename_from_every_verification_gate(self) -> None:
        """A directory walk keyed on receipt.json must never see an authorization."""
        self.assertTrue(
            self.by_gate["G7"]["receipt_path"].endswith("/G7/authorization.json")
        )
        for gid in VERIFICATION_RELEASE_GATES:
            self.assertTrue(
                self.by_gate[gid]["receipt_path"].endswith(f"/{gid}/receipt.json")
            )

    def test_receipt_paths_are_unique_and_segment_matches_gate_id(self) -> None:
        paths = [g["receipt_path"] for g in self.registry["gates"]]
        self.assertEqual(len(set(paths)), len(paths))
        for gate in self.registry["gates"]:
            self.assertIn(f"/{gate['gate_id']}/", gate["receipt_path"])
            self.assertTrue(gate["archive_dir"].endswith(f"/{gate['gate_id']}/archive"))

    def test_feeder_rts_match_the_plan(self) -> None:
        for gate_id, expected in FROZEN_RELEASE_FEEDERS.items():
            self.assertEqual(self.by_gate[gate_id]["feeder_rts"], expected, gate_id)

    def test_every_feeder_rt_is_covered_by_its_own_gates_prerequisites(self) -> None:
        for gate_id, feeders in FROZEN_RELEASE_FEEDERS.items():
            refs = set(self.by_gate[gate_id]["required_prerequisite_ids"])
            for rt in feeders:
                self.assertIn(rt, refs, f"{gate_id} does not bind feeder {rt}")

    def test_producer_roles_exclude_implementation_agents(self) -> None:
        self.assertEqual(
            self.by_gate["G6"]["producer_role"], "fresh_final_independent_verifier"
        )
        self.assertEqual(
            self.by_gate["G7"]["producer_role"], "external_authorizing_principal"
        )
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            self.assertEqual(self.by_gate[gid]["producer_role"], "wave_release_verifier")

    def test_rt026_consumes_g1_through_g5_and_never_g6_or_g7(self) -> None:
        """The anti-cycle invariant, stated from the consumer side."""
        for gid in ("G1", "G2", "G3", "G4", "G5"):
            self.assertEqual(self.by_gate[gid]["direct_rt_consumers"], ["RT-026"], gid)
        self.assertEqual(self.by_gate["G6"]["direct_rt_consumers"], [])
        self.assertEqual(self.by_gate["G7"]["direct_rt_consumers"], [])

    def test_g6_is_strictly_downstream_of_rt026(self) -> None:
        self.assertIn("RT-026", self.by_gate["G6"]["required_prerequisite_ids"])
        self.assertEqual(self.by_gate["G6"]["direct_rt_consumers"], [])

    def test_g3_scope_ceiling_is_data_and_migration_only(self) -> None:
        """VG-A is permanently synthetic, so a G3 PASS is contagiously capped."""
        ceiling = self.by_gate["G3"]["scope_ceiling"]
        self.assertIn("SCOPE CEILING", ceiling)
        self.assertIn("数据级", ceiling)
        self.assertIn("RT-016", ceiling)
        self.assertIn("VG-A", self.by_gate["G3"]["consumes_verification_gates"])

    def test_g5_binds_the_transport_capability_activation(self) -> None:
        """Without it VG-D can only be synthetic, which is what G5 must exclude."""
        refs = self.by_gate["G5"]["required_prerequisite_ids"]
        self.assertIn("CAP:gateway-identity-transport", refs)
        self.assertIn("VG-D", refs)
        note = self.by_gate["G5"]["static_note"]
        self.assertIn("产出物", note)
        self.assertIn("不是 RT-023 的前置", note)

    def test_registry_states_status_is_derived_not_recorded(self) -> None:
        rule = self.registry["status_resolution_rule"]
        self.assertIn("NOT_RUN", rule)
        self.assertIn("all seven entries are NOT_RUN", rule)
        self.assertIn("never a substitute", rule)

    def test_registry_states_the_root_closure_and_separation_rules(self) -> None:
        closure = self.registry["root_closure_rule"]
        for token in ("ENTIRE", "symlink", "dotfile", "HARD NO_GO", "authorization.json"):
            self.assertIn(token, closure)
        sep = self.registry["root_separation_rule"]
        self.assertIn("gate-receipts/", sep)
        self.assertIn("weakened", sep)

    def test_registry_separates_authorization_from_verification(self) -> None:
        rule = self.registry["authorization_separation_rule"]
        for token in (
            "cwk-release-authorization-receipt-v1",
            "cwk-release-gate-receipt-v1",
            "authorization.json",
            "EXTERNAL",
            "test signer",
        ):
            self.assertIn(token, rule)

    def test_registry_pins_the_rt026_conclusion_gloss(self) -> None:
        """The frozen token names G7 but means G6; the registry says so."""
        rule = self.registry["rt026_terminal_conclusion_rule"]
        self.assertIn("READY_FOR_G7_REVIEW", rule)
        self.assertIn("READY_FOR_G6_FINAL_ACCEPTANCE", rule)
        self.assertIn("misnomer", rule)
        self.assertIn("G7_AUTHORIZED", rule)


class GoNoGoAndVersionContractTests(unittest.TestCase):
    """The RT-026 verdict and historical lookup rules are frozen data."""

    def setUp(self) -> None:
        self.registry = _load(RELEASE_REGISTRY_PATH)
        self.registry_schema = _load(RELEASE_REGISTRY_SCHEMA_PATH)
        self.go_schema = _load(GO_NO_GO_SCHEMA_PATH)

    def test_go_no_go_schema_is_closed_and_has_the_exact_identity(self) -> None:
        self.assertEqual(self.go_schema["$id"], "cwk.pr001.go_no_go_report.v1")
        self.assertIs(self.go_schema["additionalProperties"], False)
        self.assertIs(self.go_schema["unevaluatedProperties"], False)
        self.assertEqual(
            self.go_schema["properties"]["report_id"]["const"],
            "RT-026-GO-NO-GO",
        )
        self.assertEqual(
            self.go_schema["properties"]["excluded_input_ids"]["const"],
            ["G6", "G7"],
        )

    def test_go_no_go_schema_requires_every_binding_surface(self) -> None:
        required = set(self.go_schema["required"])
        for field in (
            "producer",
            "verifier",
            "tested_subject_commit",
            "owner_scope_tree_sha256",
            "environment_fingerprint",
            "input_refs",
            "policy_ref",
            "run_evidence",
            "artifacts",
            "open_blocker_count",
            "open_major_count",
            "candidate_frozen_at",
            "started_at",
            "completed_at",
            "created_at",
            "report_sha256",
        ):
            self.assertIn(field, required, field)

    def test_registry_freezes_the_complete_go_no_go_input_set(self) -> None:
        contract = self.registry["go_no_go_contract"]
        expected = (
            {f"VG-{letter}" for letter in "ABCDE"}
            | {f"G{number}" for number in range(1, 6)}
            | {
                "CAP:cwork-authority-source",
                "CAP:gateway-identity-transport",
            }
            | {f"SG:RT-{number:03d}" for number in range(17, 27)}
            | {
                "EVIDENCE:rt016-data-diff",
                "EVIDENCE:query-diff-six-layer",
                "EVIDENCE:rt024-benchmark",
                "EVIDENCE:rt025-vge-recovery",
                "EVIDENCE:rollback-rehearsal",
                "EVIDENCE:default-off",
                "EVIDENCE:open-findings",
            }
        )
        self.assertEqual(len(contract["exact_input_ids"]), 29)
        self.assertEqual(set(contract["exact_input_ids"]), expected)
        self.assertTrue({"G6", "G7"}.isdisjoint(expected))
        frozen = self.registry_schema["properties"]["go_no_go_contract"][
            "properties"
        ]
        self.assertEqual(frozen["exact_input_ids"]["const"], contract["exact_input_ids"])
        self.assertEqual(frozen["evidence_inputs"]["const"], contract["evidence_inputs"])

    def test_registry_freezes_go_no_go_path_domain_and_three_artifacts(self) -> None:
        contract = self.registry["go_no_go_contract"]
        self.assertEqual(
            contract["schema_ref"], GO_NO_GO_SCHEMA_PATH.relative_to(REPO_ROOT).as_posix()
        )
        self.assertEqual(contract["domain_separator"], "cwk-rt026-go-no-go-report-v1\\0")
        self.assertEqual(
            contract["exact_artifact_roles"],
            ["input_manifest", "evaluation_log", "launcher_run_attestation"],
        )
        self.assertIn("excluding only report_sha256", contract["self_hash_rule"])

    def test_historical_resolution_is_raw_hash_as_of_not_current_only(self) -> None:
        rule = self.registry["release_version_resolution"]
        self.assertEqual(rule["lookup_key_field"], "raw_sha256")
        self.assertEqual(rule["storage_locations"], ["current", "archive"])
        self.assertEqual(rule["as_of_field"], "consumer.created_at")
        self.assertEqual(rule["cycle_guard_fields"], ["gate_id", "receipt_sha256"])
        for token in ("highest sequence", "strictly earlier", "logical ref_path"):
            self.assertIn(token, rule["selection_rule"])

    def test_every_cwk_rt_identity_marker_is_stricter_without_rewriting_legacy(self) -> None:
        marker = self.registry["prerequisite_resolution"]["markdown_acceptance_marker"]
        self.assertEqual(
            marker["rt_acceptance_required_identity_fields"],
            ["implementer_ids", "reviewer_ids"],
        )
        self.assertEqual(
            marker["required_fields"],
            [
                "report_id",
                "verdict",
                "open_blocker",
                "open_major",
                "subject_commit",
                "owner_scope_tree_sha256",
            ],
        )
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        legacy_ids = {
            rt_id
            for rt_id, entry in reports.items()
            if entry["marker_style"] == "legacy_frozen_hash"
        }
        self.assertEqual(
            legacy_ids,
            {"RT-011", "RT-014", "RT-015", "RT-016"},
        )
        for rt_id in legacy_ids:
            self.assertEqual(
                reports[rt_id]["marker_style"],
                "legacy_frozen_hash",
            )

    def test_rt012_freezes_a_distinct_stage09_acceptance_path(self) -> None:
        resolution = self.registry["prerequisite_resolution"]
        entry = resolution["rt_acceptance_reports"]["RT-012"]
        self.assertEqual(entry["marker_style"], "cwk_acceptance_v1")
        self.assertEqual(
            entry["report_path"],
            "RT/RT-012/reports/独立验收报告-stage09.md",
        )
        # This test is deliberately future-safe: the file is absent during
        # contract implementation but may legitimately appear after a frozen
        # candidate receives independent acceptance.  Path, style and marker
        # semantics remain frozen either way.
        self.assertIn(
            "RT/RT-012/reports/独立验收报告.md",
            next(
                family
                for family in resolution["families"]
                if family["family"] == "rt_acceptance"
            )["forbidden_paths"],
        )

    def test_rt013_freezes_a_distinct_stage10_acceptance_path(self) -> None:
        resolution = self.registry["prerequisite_resolution"]
        entry = resolution["rt_acceptance_reports"]["RT-013"]
        self.assertEqual(entry["marker_style"], "cwk_acceptance_v1")
        self.assertEqual(
            entry["report_path"],
            "RT/RT-013/reports/独立验收报告-stage10.md",
        )
        # Future-safe for the same reason as Stage-09: the independent report
        # is a post-freeze evidence output, not a contract-writing artefact.
        self.assertIn(
            "RT/RT-013/reports/独立验收报告.md",
            next(
                family
                for family in resolution["families"]
                if family["family"] == "rt_acceptance"
            )["forbidden_paths"],
        )

    def test_rt012_superseded_legacy_report_is_exact_closed_provenance(self) -> None:
        entry = self.registry["prerequisite_resolution"]["rt_acceptance_reports"][
            "RT-012"
        ]
        self.assertEqual(
            entry["superseded_legacy_report"],
            {
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
        )
        old_path = REPO_ROOT / entry["superseded_legacy_report"]["report_path"]
        self.assertTrue(old_path.is_file())
        self.assertEqual(
            hashlib.sha256(old_path.read_bytes()).hexdigest(),
            entry["superseded_legacy_report"]["report_sha256"],
        )

    def test_rt013_superseded_legacy_report_is_exact_closed_provenance(self) -> None:
        entry = self.registry["prerequisite_resolution"]["rt_acceptance_reports"][
            "RT-013"
        ]
        self.assertEqual(
            entry["superseded_legacy_report"],
            {
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
        )
        old_path = REPO_ROOT / entry["superseded_legacy_report"]["report_path"]
        self.assertTrue(old_path.is_file())
        self.assertEqual(
            hashlib.sha256(old_path.read_bytes()).hexdigest(),
            entry["superseded_legacy_report"]["report_sha256"],
        )

    def test_schema_structurally_keys_remediated_and_legacy_style_sets(self) -> None:
        reports_schema = self.registry_schema["properties"][
            "prerequisite_resolution"
        ]["properties"]["rt_acceptance_reports"]
        reports = self.registry["prerequisite_resolution"]["rt_acceptance_reports"]
        self.assertEqual(len(reports_schema["allOf"]), 4)
        self.assertTrue(_keyed_rt_acceptance_constraints_hold(reports, reports_schema))

        mutations = []
        candidate = copy.deepcopy(reports)
        candidate["RT-012"]["marker_style"] = "legacy_frozen_hash"
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-012"].pop("superseded_legacy_report")
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-012"]["report_path"] = "RT/RT-012/reports/独立验收报告.md"
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-013"]["superseded_legacy_report"] = copy.deepcopy(
            reports["RT-012"]["superseded_legacy_report"]
        )
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-013"]["marker_style"] = "legacy_frozen_hash"
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-013"].pop("superseded_legacy_report")
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-013"]["report_path"] = "RT/RT-013/reports/独立验收报告.md"
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-014"]["superseded_legacy_report"] = copy.deepcopy(
            reports["RT-013"]["superseded_legacy_report"]
        )
        mutations.append(candidate)
        candidate = copy.deepcopy(reports)
        candidate["RT-017"]["marker_style"] = "legacy_frozen_hash"
        mutations.append(candidate)
        for index, mutated in enumerate(mutations):
            with self.subTest(mutation=index):
                self.assertFalse(
                    _keyed_rt_acceptance_constraints_hold(mutated, reports_schema)
                )

    def test_rt012_touch_scope_includes_the_stage09_runtime(self) -> None:
        owner = self.registry["owner_scope_model"]["rt_owner_code_prefixes"]
        self.assertIn("scripts/cwk_instance.py", owner["RT-012"])
        for rt_id, selectors in owner.items():
            if rt_id != "RT-012":
                self.assertNotIn("scripts/cwk_instance.py", selectors, rt_id)

    def test_rt013_touch_scope_includes_the_stage10_runtime(self) -> None:
        owner = self.registry["owner_scope_model"]["rt_owner_code_prefixes"]
        self.assertIn("scripts/cwk_agent_binding.py", owner["RT-013"])
        for rt_id, selectors in owner.items():
            if rt_id != "RT-013":
                self.assertNotIn("scripts/cwk_agent_binding.py", selectors, rt_id)

    def test_owner_scope_normative_surfaces_use_the_same_whole_tree_model(self) -> None:
        """Registry, receipt schema and evaluator must name one digest basis."""
        model = self.registry["owner_scope_model"]
        surfaces = {
            "binding_rule": model["binding_rule"],
            "recomputation_rule": self.registry["owner_scope_recomputation_rule"],
            "receipt_description": _load(RELEASE_RECEIPT_SCHEMA_PATH)["properties"][
                "owner_scope_tree_sha256"
            ]["description"],
        }
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                self.assertIn("candidate_tree_minus_closed_exclusions", text)
                self.assertIn("candidate_tree_excluded_prefixes", text)
                self.assertIn("candidate_tree_excluded_patterns", text)
                self.assertIn("git ls-tree -r -z --full-tree", text)
                self.assertNotIn("records under this gate's feeder RTs", text)
        self.assertEqual(model["scope_mode"], "candidate_tree_minus_closed_exclusions")

    def test_legacy_drift_contract_has_no_fresh_rerun_bypass(self) -> None:
        rule = self.registry["prerequisite_resolution"][
            "legacy_acceptance_drift_rule"
        ]
        self.assertIn("NO-DRIFT", rule)
        self.assertIn("cannot bypass", rule)
        self.assertNotIn("NO-DRIFT or RE-RUN", rule)
        source = (REPO_ROOT / "tests/pr001_release_eval.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("legacy_fresh_rerun", source)

    def test_release_delegation_binds_the_final_ten_stage_policy(self) -> None:
        policy_path = (
            PR_ROOT / "contracts" / "script-evolution" / "policy_v1.json"
        )
        security_registry = _load(
            PR_ROOT / "contracts" / "security" / "security_gate_registry_v1.json"
        )
        policy_raw = policy_path.read_bytes()
        policy = json.loads(policy_raw)
        expected_sha = "2089490e45bdd84ba3bac75fe40092f81f40765638b988e17facdc4040d14a6d"
        self.assertEqual(hashlib.sha256(policy_raw).hexdigest(), expected_sha)
        self.assertEqual(
            security_registry["script_evolution_policy_sha256"], expected_sha
        )
        self.assertEqual(
            [stage["stage_index"] for stage in policy["stages"]], list(range(1, 11))
        )
        self.assertEqual(
            policy["stages"][-2]["target_path"], "scripts/cwk_instance.py"
        )
        self.assertEqual(
            policy["stages"][-1]["target_path"], "scripts/cwk_agent_binding.py"
        )


class ConsumerRelationModelTests(unittest.TestCase):
    """Relation 1 (direct RT consumers) vs relation 2 (release-gate consumers).

    Earlier revisions carried only the first while the plan asserted the second,
    which read as a flat contradiction. Both are now explicit in both files and
    cross-checked in both directions, so a one-sided edit fails loudly.
    """

    def setUp(self) -> None:
        self.vg = _load(VG_REGISTRY_PATH)
        self.vg_schema = _load(VG_REGISTRY_SCHEMA_PATH)
        self.release = _load(RELEASE_REGISTRY_PATH)
        self.vg_by_gate = {g["gate_id"]: g for g in self.vg["gates"]}
        self.rel_by_gate = {g["gate_id"]: g for g in self.release["gates"]}

    def test_direct_rt_consumers_are_only_rts(self) -> None:
        spec = self.vg_schema["properties"]["gates"]["items"]["properties"]["consumers"]
        self.assertEqual(sorted(spec["items"]["enum"]), ["RT-024", "RT-026"])
        for gate in self.vg["gates"]:
            for c in gate["consumers"]:
                self.assertTrue(c.startswith("RT-"), c)

    def test_release_gate_consumers_are_only_release_gates(self) -> None:
        props = self.vg_schema["properties"]["gates"]["items"]["properties"]
        spec = props["release_gate_consumers"]
        self.assertEqual(sorted(spec["items"]["enum"]), ["G3", "G4", "G5", "G6"])
        for gate in self.vg["gates"]:
            for c in gate["release_gate_consumers"]:
                self.assertTrue(re.fullmatch(r"G[3-6]", c), c)

    def test_the_two_relations_have_disjoint_value_spaces(self) -> None:
        props = self.vg_schema["properties"]["gates"]["items"]["properties"]
        rt_space = set(props["consumers"]["items"]["enum"])
        g_space = set(props["release_gate_consumers"]["items"]["enum"])
        self.assertEqual(rt_space & g_space, set())

    def test_vg_release_gate_consumers_match_the_frozen_relation(self) -> None:
        for vg_id, expected in FROZEN_VG_RELEASE_CONSUMERS.items():
            self.assertEqual(
                self.vg_by_gate[vg_id]["release_gate_consumers"], expected, vg_id
            )

    def test_release_registry_consumes_matches_the_frozen_relation(self) -> None:
        for gate_id, expected in FROZEN_RELEASE_CONSUMES_VG.items():
            self.assertEqual(
                self.rel_by_gate[gate_id]["consumes_verification_gates"],
                expected,
                gate_id,
            )

    def test_the_two_registries_are_exact_inverses(self) -> None:
        """Cross-file, both directions. A one-sided edit cannot survive this."""
        derived = {vg_id: [] for vg_id in FROZEN_VG_RELEASE_CONSUMERS}
        for gate in self.release["gates"]:
            for vg_id in gate["consumes_verification_gates"]:
                derived[vg_id].append(gate["gate_id"])
        for vg_id, gates in derived.items():
            self.assertEqual(
                sorted(gates),
                sorted(self.vg_by_gate[vg_id]["release_gate_consumers"]),
                f"{vg_id} inverse mismatch",
            )

    def test_consuming_a_vg_and_being_upstream_of_it_are_mutually_exclusive(self) -> None:
        """A gate cannot be a prerequisite of its own input.

        This is the asymmetry that made the old single-field model look wrong:
        G3 consumes VG-A, and precisely because of that G3 is absent from VG-A's
        prerequisite allowlist.
        """
        for vg_id, consumers in FROZEN_VG_RELEASE_CONSUMERS.items():
            allowed = set(self.vg_by_gate[vg_id]["allowed_prerequisite_ids"])
            for g in consumers:
                self.assertNotIn(g, allowed, f"{g} both consumes and precedes {vg_id}")

    def test_every_consumed_vg_is_also_hash_pinned_as_a_prerequisite(self) -> None:
        """Naming a VG without citing its hash would let the evidence drift."""
        for gate in self.release["gates"]:
            refs = set(gate["required_prerequisite_ids"])
            for vg_id in gate["consumes_verification_gates"]:
                self.assertIn(vg_id, refs, f"{gate['gate_id']} consumes {vg_id} unpinned")

    def test_both_registries_document_the_two_relation_model(self) -> None:
        for text in (
            self.vg["consumer_relation_model"],
            self.release["consumer_relation_model"],
        ):
            self.assertIn("DIRECT RT CONSUMERS", text)
            self.assertIn("RELEASE-GATE CONSUMERS", text)
            self.assertIn("neither", text.lower())

    def test_vg_registry_points_at_the_release_registry(self) -> None:
        self.assertEqual(
            REPO_ROOT / self.vg["release_gate_registry_ref"], RELEASE_REGISTRY_PATH
        )
        self.assertTrue(RELEASE_REGISTRY_PATH.is_file())

    def test_no_release_gate_leaked_into_direct_rt_consumers(self) -> None:
        for gate in self.vg["gates"]:
            for c in gate["consumers"]:
                self.assertNotIn(c, RELEASE_GATE_ORDER, f"{gate['gate_id']}: {c}")

    def test_no_rt_leaked_into_release_gate_consumers(self) -> None:
        for gate in self.vg["gates"]:
            for c in gate["release_gate_consumers"]:
                self.assertFalse(c.startswith("RT-"), f"{gate['gate_id']}: {c}")


class ReleaseReceiptSchemaTests(unittest.TestCase):
    """G1..G6 verification receipt surface."""

    def setUp(self) -> None:
        self.schema = _load(RELEASE_RECEIPT_SCHEMA_PATH)
        self.props = self.schema["properties"]
        self.rules = " || ".join(self.schema["semanticRules"])

    def test_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.schema["$id"], "cwk.pr001.release_gate_receipt.v1")
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(self.schema["unevaluatedProperties"], False)

    def test_gate_id_enum_structurally_excludes_g7(self) -> None:
        """An authorization is not a verification pass, so it cannot be one."""
        self.assertEqual(self.props["gate_id"]["enum"], list(VERIFICATION_RELEASE_GATES))
        self.assertNotIn("G7", self.props["gate_id"]["enum"])

    def test_required_fields_cover_the_full_binding_surface(self) -> None:
        required = set(self.schema["required"])
        for field in (
            "gate_id",
            "status",
            "conclusion",
            "producer",
            "verifier",
            "verifier_role",
            "feeder_rts",
            "feeder_rts_independent_pass",
            "consumes_verification_gates",
            "prerequisite_refs",
            "evidence",
            "artifacts",
            "tested_subject_commit",
            "owner_scope_tree_sha256",
            "environment_fingerprint",
            "sequence",
            "created_at",
            "receipt_sha256",
        ):
            self.assertIn(field, required, field)

    def test_verifier_is_always_required_not_only_on_pass(self) -> None:
        """Self-certification must be impossible even for a fail or a not_run."""
        self.assertIn("verifier", self.schema["required"])
        self.assertIn("strictly different from producer", self.rules)

    def test_domain_separator_is_unique_to_this_family(self) -> None:
        desc = self.props["receipt_sha256"]["description"]
        self.assertIn("cwk-release-gate-receipt-v1", desc)
        for other in (
            "cwk-verification-gate-receipt-v1",
            "cwk-security-gate-receipt-v1",
            "cwk-capability-activation-receipt-v1",
            "cwk-release-authorization-receipt-v1",
        ):
            self.assertIn(other, desc, f"{other} not declared non-interchangeable")

    def test_authorization_fields_are_deep_forbidden(self) -> None:
        """A verification receipt that could authorize itself collapses the split."""
        forbidden = set(self.schema["deepForbiddenKeys"])
        for key in (
            "authorization",
            "authorized",
            "authorized_actions",
            "authorizing_principal",
            "signature",
        ):
            self.assertIn(key, forbidden, key)
        self.assertEqual(forbidden & set(self.props), set())

    def test_credentials_and_host_paths_are_deep_forbidden(self) -> None:
        forbidden = set(self.schema["deepForbiddenKeys"])
        for key in ("tenant_id", "app_key", "credential", "token", "secret",
                    "abs_path", "instance_root", "prompt", "model_output"):
            self.assertIn(key, forbidden, key)

    def test_prerequisite_ref_id_pattern_excludes_g7_and_admits_the_full_space(self) -> None:
        pattern = re.compile(
            self.props["prerequisite_refs"]["items"]["properties"]["ref_id"]["pattern"]
        )
        for good in ("G0", "G6", "RT-011", "RT-026", "VG-A", "VG-E",
                     "CAP:cwork-authority-source", "CAP:gateway-identity-transport",
                     "SG:RT-017", "SG:RT-026", "RT-026-GO-NO-GO"):
            self.assertIsNotNone(pattern.fullmatch(good), good)
        for bad in ("G7", "G8", "RT-010", "RT-027", "VG-F", "CAP:made-up-thing",
                    "SG:RT-016", "RT-026-GO", ""):
            self.assertIsNone(pattern.fullmatch(bad), bad)

    def test_prerequisite_entries_require_kind_path_and_hash(self) -> None:
        item = self.props["prerequisite_refs"]["items"]
        self.assertEqual(
            sorted(item["required"]), ["ref_id", "ref_kind", "ref_path", "ref_sha256"]
        )
        self.assertIs(item["additionalProperties"], False)

    def test_ref_path_pattern_rejects_traversal_and_absolute_paths(self) -> None:
        pattern = re.compile(
            self.props["prerequisite_refs"]["items"]["properties"]["ref_path"]["pattern"]
        )
        self.assertIsNotNone(pattern.fullmatch("PR/x/gate-receipts/VG-A/receipt.json"))
        for bad in ("/etc/passwd", "../secrets.json", "PR/../../x", "~/x.json"):
            self.assertIsNone(pattern.fullmatch(bad), bad)

    def test_prerequisite_set_equality_is_the_rule_not_containment(self) -> None:
        self.assertIn("must EQUAL required_prerequisite_ids", self.rules)
        self.assertIn("A missing id and an extra id are equally invalid", self.rules)

    def test_conclusion_ceilings_are_declared(self) -> None:
        enum = self.props["conclusion"]["enum"]
        self.assertIn("release_gate_verified", enum)
        self.assertIn("READY_FOR_G7_AUTHORIZATION", enum)
        self.assertIn("conservative_unknown", enum)
        self.assertIn("available only to G1..G5", self.rules)
        self.assertIn("requires gate_id=G6", self.rules)
        self.assertIn("not an authorization", self.rules)

    def test_g6_freshness_surface_exists_and_is_all_true(self) -> None:
        attest = self.props["freshness_attestation"]
        for key in (
            "signed_no_rt_acceptance",
            "signed_no_verification_gate",
            "signed_no_upstream_release_gate",
            "signed_no_security_gate",
        ):
            self.assertIs(attest["properties"][key]["const"], True, key)
            self.assertIn(key, attest["required"])

    def test_g6_freshness_is_recomputed_not_trusted(self) -> None:
        self.assertIn("RECOMPUTED, not trusted", self.rules)
        self.assertIn("prior_engagement_ids must be empty", self.rules)

    def test_the_five_unbound_freshness_labels_are_gone(self) -> None:
        # The old fresh_evidence_classes was an enum of five bare strings. It
        # asserted that five KINDS of run had happened and bound none of them
        # to an artefact, a command, a commit, an environment or a time, so a
        # verifier satisfied it by typing five words. It must not come back.
        self.assertNotIn("fresh_evidence_classes", self.props)
        self.assertIn("fresh_evidence_refs", self.props)

    def test_g6_requires_nine_role_keyed_fresh_evidence_refs(self) -> None:
        spec = self.props["fresh_evidence_refs"]
        self.assertEqual(spec["minItems"], 9)
        self.assertEqual(spec["maxItems"], 9)
        self.assertEqual(
            sorted(spec["items"]["properties"]["role"]["enum"]),
            [
                "attack_suite",
                "default_off_verification",
                "final_findings_reconciliation",
                "full_regression",
                "legacy_smoke",
                "restore_drill",
                "rollback_drill",
                "secret_scan",
                "wiki_smoke",
            ],
        )

    def test_each_fresh_evidence_ref_binds_a_real_run(self) -> None:
        required = self.props["fresh_evidence_refs"]["items"]["required"]
        # Every axis a fabricated "we ran it" claim would have to forge.
        for field in (
            "run_id",
            "engagement_id",
            "runner_id",
            "session_id",
            "command",
            "tested_subject_commit",
            "candidate_tree_sha256",
            "environment_fingerprint",
            "started_at",
            "completed_at",
            "checks_total",
            "checks_failed",
            "exit_code",
            "result",
            "artifact_path",
            "artifact_sha256",
        ):
            self.assertIn(field, required, field)

    def test_one_artifact_may_not_satisfy_two_roles(self) -> None:
        self.assertIn("must each be UNIQUE across fresh_evidence_refs", self.rules)
        self.assertIn("manufactures nine fresh runs", self.rules)

    def test_g6_requires_orchestration_provenance(self) -> None:
        spec = self.props["orchestration_provenance"]
        for field in (
            "candidate_freeze_sha256",
            "candidate_frozen_at",
            "session_id",
            "session_started_at",
            "engagement_id",
            "session_participants",
        ):
            self.assertIn(field, spec["required"], field)
        self.assertIn("STRICTLY AFTER candidate_frozen_at", self.rules)

    def test_fresh_evidence_timing_envelope_is_closed_at_both_ends(self) -> None:
        # A run started before the freeze tested something else; a run finished
        # after the receipt was created was not available when it was signed.
        self.assertIn(
            "candidate_frozen_at <= started_at < completed_at <= created_at",
            self.rules,
        )

    def test_role_specific_assertions_carry_the_retired_label_semantics(self) -> None:
        # The renamed roles must not lose what the old names meant.
        for fragment in (
            "secret_scan requires findings_count=0",
            "restore_drill requires clean_room=true",
            "rollback_drill requires legacy_read_path_restored=true",
            "open_blocker_count=0, open_major_count=0",
            "legacy_smoke requires legacy_fixture_id",
        ):
            self.assertIn(fragment, self.rules, fragment)

    def test_g6_expires_so_a_stale_final_verdict_cannot_authorize_later(self) -> None:
        self.assertIn("expires_at", self.props)
        self.assertIn("at most 30 days", self.rules)
        self.assertIn("An expired G6 cannot support a G7 authorization", self.rules)

    def test_sequence_chain_rules_forbid_a_link_at_sequence_one(self) -> None:
        desc = self.props["supersedes_receipt_sha256"]["description"]
        self.assertIn("FORBIDDEN when sequence=1", desc)
        self.assertIn("REQUIRED when sequence>1", desc)
        self.assertIn("proof of a hidden earlier run", desc)

    def test_commit_binding_is_ancestry_not_equality(self) -> None:
        desc = self.props["tested_subject_commit"]["description"]
        self.assertIn("STRICT ancestor", desc)
        self.assertIn("never by equality with a moving head", desc)

    def test_artifacts_are_non_empty_and_cannot_cite_gate_evidence(self) -> None:
        self.assertEqual(self.props["artifacts"]["minItems"], 1)
        self.assertIn("none may point at another release gate receipt", self.rules)

    def test_missing_receipt_is_not_run_and_never_inferred(self) -> None:
        self.assertIn("a missing receipt is NOT_RUN", self.rules)
        self.assertIn("never a substitute for its receipt", self.rules)


class ReleaseAuthorizationSchemaTests(unittest.TestCase):
    """G7 only. Narrow authority, external trust root, no verification surface."""

    def setUp(self) -> None:
        self.schema = _load(RELEASE_AUTH_SCHEMA_PATH)
        self.props = self.schema["properties"]
        self.rules = " || ".join(self.schema["semanticRules"])

    def test_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.schema["$id"], "cwk.pr001.release_authorization_receipt.v1")
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(self.schema["unevaluatedProperties"], False)

    def test_gate_id_is_pinned_to_g7(self) -> None:
        self.assertEqual(self.props["gate_id"]["const"], "G7")

    def test_it_has_no_verification_surface_at_all(self) -> None:
        """status / conclusion / tests / artifacts / verifier / producer are absent.

        An authorization carrying a verification verdict could be mistaken for
        one, which is exactly the collapse the split exists to prevent.
        """
        for key in ("status", "conclusion", "tests_run", "tests_failed",
                    "artifacts", "verifier", "producer"):
            self.assertNotIn(key, self.props, key)
            self.assertIn(key, self.schema["deepForbiddenKeys"], key)

    def test_self_hash_field_and_separator_differ_from_the_receipt_family(self) -> None:
        self.assertIn("authorization_sha256", self.props)
        self.assertNotIn("receipt_sha256", self.props)
        desc = self.props["authorization_sha256"]["description"]
        self.assertIn("cwk-release-authorization-receipt-v1", desc)
        self.assertIn("NOT cwk-release-gate-receipt-v1", desc)

    def test_decision_enum_has_no_pass_and_supports_withdrawal(self) -> None:
        enum = self.props["decision"]["enum"]
        self.assertEqual(sorted(enum), ["authorized", "denied", "withdrawn"])
        self.assertNotIn("pass", enum)

    def test_only_a_human_principal_may_authorize(self) -> None:
        self.assertEqual(self.props["authorizing_principal"]["properties"]["kind"]["const"],
                         "human_user")
        self.assertIn("No agent, service account, pipeline", self.rules)

    def test_there_is_no_inferred_authorization_channel(self) -> None:
        """An authorization that could be inferred is not an authorization."""
        enum = self.props["authorization_channel"]["enum"]
        self.assertEqual(
            sorted(enum), ["explicit_user_instruction", "signed_out_of_band_approval"]
        )
        for forbidden in ("inferred", "derived_from_g6", "implied_by_policy",
                          "agent_relayed"):
            self.assertNotIn(forbidden, enum)
        self.assertIn("Inference is not authorization", self.rules)

    def test_external_trust_root_excludes_project_and_test_signers(self) -> None:
        sig = self.props["external_signature"]
        self.assertEqual(
            sorted(sig["required"]),
            ["algorithm", "key_expires_at", "key_id", "key_not_before", "key_state",
             "signature_b64", "trust_root_id"],
        )
        self.assertIn("must NOT be any project identity", self.rules)
        self.assertIn("test signer is rejected in production mode", self.rules)

    def test_key_state_and_expiry_are_carried_so_revocation_is_detectable(self) -> None:
        sig = self.props["external_signature"]["properties"]
        self.assertEqual(
            sorted(sig["key_state"]["enum"]),
            ["active", "expired", "revoked", "suspended"],
        )
        self.assertIn("key_expires_at", sig)
        self.assertIn("revoked or expired key is INVALID", self.rules)

    def test_signature_fields_are_trust_store_mirrors_not_authorities(self) -> None:
        # An artefact must never nominate the key that validates it.
        self.assertIn("MIRROR", self.props["external_signature"]["description"])
        self.assertIn(
            "verification uses the STORE's public key and algorithm, never the values carried here",
            self.props["external_signature"]["description"],
        )

    def test_only_a_verifiable_algorithm_is_permitted(self) -> None:
        # ed25519 was removed: the reference platform ships LibreSSL 3.3.6,
        # which has no Ed25519, so leaving it in the enum would be fail-OPEN --
        # an artefact declares it and verification silently cannot run.
        algo = self.props["external_signature"]["properties"]["algorithm"]
        self.assertEqual(algo["const"], "ecdsa-p256-sha256")
        self.assertNotIn("enum", algo)

    def test_signature_encoding_is_canonical_and_low_s(self) -> None:
        desc = self.props["external_signature"]["properties"]["signature_b64"]["description"]
        # ECDSA is malleable: (r, n-s) is equally valid, so without low-S an
        # attacker mints a second signature for an unchanged body -- and since
        # authorization_sha256 covers signature_b64, that yields two distinct
        # valid-looking authorizations for one decision.
        self.assertIn("LOW-S normalisation (s <= n/2)", desc)
        self.assertIn("CANONICAL ASN.1 DER", desc)

    def test_detached_signed_payload_excludes_exactly_two_keys(self) -> None:
        # The payload rule lives in the semantic rules, not in a stored digest
        # field.  It must name both exclusions and the signing domain.
        self.assertIn("delete EXACTLY two things", self.rules)
        self.assertIn("top-level authorization_sha256", self.rules)
        self.assertIn("external_signature.signature_b64", self.rules)
        self.assertIn("cwk-release-authorization-signature-v1", self.rules)
        self.assertIn("sign and verify those bytes DIRECTLY", self.rules)

    def test_there_is_no_self_referential_signed_payload_digest_field(self) -> None:
        # A stored signed_payload_sha256 was SELF-REFERENTIAL: the payload
        # removes only authorization_sha256 and signature_b64, so the digest
        # field stayed inside the very bytes it claimed to digest and could not
        # be computed without already knowing it.  The fix is deletion, never a
        # third exclusion -- each extra exclusion is a field an attacker may
        # then mutate with impunity.
        self.assertNotIn("signed_payload_sha256", self.props)
        self.assertNotIn("signed_payload_sha256", self.schema["required"])
        self.assertIn("NO signed_payload_sha256 field", self.rules)
        self.assertIn("SELF-REFERENTIAL", self.rules)
        self.assertIn("NOT to make it a third excluded field", self.rules)

    def test_signature_and_self_hash_fail_independently(self) -> None:
        # The three separating cases are the executable proof that the
        # exclusion is real: each breaks exactly one of the two checks.
        self.assertIn("checked INDEPENDENTLY", self.rules)
        self.assertIn("WITHOUT re-signing", self.rules)
        self.assertIn("leaves the signed payload bytes untouched", self.rules)
        self.assertIn("corrupting authorization_sha256 alone leaves the signature verifiable",
                      self.rules)

    def test_g6_is_the_only_machine_prerequisite(self) -> None:
        ref = self.props["g6_receipt_ref"]
        self.assertEqual(ref["properties"]["ref_id"]["const"], "G6")
        self.assertTrue(ref["properties"]["ref_path"]["const"].endswith("/G6/receipt.json"))
        self.assertIn("does not re-consume RT-011..RT-026", self.rules)
        self.assertIn("separation of duties", self.rules)

    def test_authorized_actions_is_an_exhaustive_closed_pilot_list(self) -> None:
        enum = self.props["authorized_actions"]["items"]["enum"]
        self.assertEqual(len(enum), 4)
        for action in enum:
            self.assertIn("allowlisted_tenants", action)
        joined = " ".join(enum)
        for forbidden in ("expand", "ga", "general_availability", "migrate_schema",
                          "rotate_credentials"):
            self.assertNotIn(forbidden, joined)

    def test_scope_is_pinned_to_m4_and_carries_counts_not_tenant_ids(self) -> None:
        scope = self.props["scope"]
        self.assertEqual(scope["properties"]["migration_phase"]["const"], "M4")
        self.assertIn("allowlisted_tenant_count", scope["properties"])
        self.assertNotIn("tenant_ids", scope["properties"])
        for key in ("tenant_id", "tenant_ids"):
            self.assertIn(key, self.schema["deepForbiddenKeys"])

    def test_scope_bounds_are_finite(self) -> None:
        scope = self.props["scope"]["properties"]
        self.assertEqual(scope["allowlisted_tenant_count"]["minimum"], 1)
        self.assertEqual(scope["allowlisted_tenant_count"]["maximum"], 3)
        self.assertEqual(scope["pilot_window_days"]["maximum"], 30)

    def test_target_binding_prevents_cross_build_replay(self) -> None:
        tb = self.props["target_binding"]
        self.assertEqual(
            sorted(tb["required"]),
            ["environment_fingerprint", "instance_id", "target_commit"],
        )
        self.assertIn("must equal g6_receipt_ref.g6_tested_subject_commit", self.rules)

    def test_nonce_and_window_and_sequence_all_exist(self) -> None:
        for key in ("nonce", "not_before", "expires_at", "sequence"):
            self.assertIn(key, self.props, key)
            self.assertIn(key, self.schema["required"], key)
        self.assertIn("unique across the entire authorization history", self.rules)
        self.assertIn("no perpetual authorization", self.rules)

    def test_authorization_is_always_revocable_and_revocation_is_append_only(self) -> None:
        self.assertIs(self.props["revocable"]["const"], True)
        self.assertIn("append-only", self.rules)
        self.assertIn("Deleting an authorization file is therefore not a valid revocation",
                      self.rules)

    def test_recorder_is_clerical_and_cannot_be_the_beneficiary(self) -> None:
        self.assertIn("recorded_by", self.schema["required"])
        self.assertIn("must differ from authorizing_principal.id", self.rules)
        self.assertIn("never be an RT-026 identity", self.rules)

    def test_rt026_may_never_generate_or_infer_it(self) -> None:
        self.assertIn("RT-026 must never generate, request, infer", self.rules)
        self.assertIn("READY_FOR_G6_FINAL_ACCEPTANCE", self.rules)

    def test_authorization_is_not_execution(self) -> None:
        """A past validation is not standing permission."""
        self.assertIn("AUTHORIZATION IS NOT EXECUTION", self.rules)
        self.assertIn("re-verify signature, key state, window, nonce", self.rules)
        self.assertIn("treating a past validation as standing permission is a replay",
                      self.rules)

    def test_missing_authorization_means_unauthorized(self) -> None:
        self.assertIn("NOT_RUN and unauthorized", self.rules)
        self.assertIn("must never be inferred from G6 being green", self.rules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
