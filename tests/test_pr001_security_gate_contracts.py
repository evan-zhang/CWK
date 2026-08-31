"""PR-001 Security Gate contract tests (SG-00~SG-10).

The machine authority for SG-00..SG-10 is exactly three files:

1. `contracts/security/security_gate_registry_v1.json` -- frozen ownership,
   claim IDs, receipt paths, filesystem policy and lifecycle phases;
2. `contracts/security/security_gate_registry_v1.schema.json` -- proves that
   registry is *configuration only* and can never record a verdict;
3. `contracts/security/security_gate_receipt_v1.schema.json` -- the single
   receipt format each producing RT must emit.

The §5.1 Markdown matrix in the central plan is DERIVED documentation and is
never consulted here.

Like the VG gate tests this file is forward-compatible: it deliberately does
not freeze which security receipts exist today. Absence of a receipt is
NOT_RUN, which fails closed to NO_GO. Presence obliges the receipt to sit at a
registry-declared path and be fully valid. `FutureSecurityReceiptTests` proves
a conforming receipt is actually constructible -- otherwise the gate would be
an unreachable dead end -- and then proves every omission and attack shape is
rejected.

Anti-cycle: RT-017..RT-025 produce in `rt_independent_acceptance`; RT-026's own
SG-03/SG-10 receipt is produced by an INDEPENDENT PREFLIGHT VERIFIER in
`preflight_after_candidate_freeze`, strictly before the read-only go/no-go
evaluator consumes it in `go_no_go_evaluation`. The evaluator phase is excluded
from `write_phase_allowlist`, and the evaluator injects its own identity and
recomputes exclusion rather than trusting the receipt's declaration, so it is
not the declared author of what it reads and RT-026 never depends on its own
output.

Scope of that claim: this is INTERFACE-LEVEL authorship/phase exclusion. A phase
allowlist is a declared-phase check and identity exclusion is a string
comparison; neither shows the kernel denied the evaluation process write access.
OS-level write denial -- a trusted launcher imposing the denial, a
launcher-signed run attestation, pre/post exact manifests over the receipt
trees, and real write-denial evidence where create/rewrite/rename/delete are
each attempted and rejected -- remains RT-026 AC-026-11 evidence and is NOT
proven by these tests.
"""

from __future__ import annotations

import ast
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
import pr001_script_evolution_guard as _eg  # noqa: E402
from pr001_evidence_binding import (  # noqa: E402
    GitSubject,
    _security_evolution_state,
    _security_scope_snapshot,
    commit_all,
    index_has_hidden_entries,
    init_fixture_repo,
    resolve_evidence_commit,
    security_owner_scope_tree_sha256,
    security_registry_owner_semantics_ok,
    verify_environment_fingerprint,
    verify_security_subject_commit,
    verify_subject_commit,
    worktree_is_dirty,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PR_ROOT = REPO_ROOT / "PR" / "PR-001-multitenant-knowledge-spaces"
SECURITY_DIR = PR_ROOT / "contracts" / "security"
REGISTRY_PATH = SECURITY_DIR / "security_gate_registry_v1.json"
REGISTRY_SCHEMA_PATH = SECURITY_DIR / "security_gate_registry_v1.schema.json"
RECEIPT_SCHEMA_PATH = SECURITY_DIR / "security_gate_receipt_v1.schema.json"

CONTRACT_FILES = (REGISTRY_PATH, REGISTRY_SCHEMA_PATH, RECEIPT_SCHEMA_PATH)

RECEIPT_DOMAIN = b"cwk-security-gate-receipt-v1\x00"
VG_DOMAIN = b"cwk-verification-gate-receipt-v1\x00"
ACTIVATION_DOMAIN = b"cwk-capability-activation-receipt-v1\x00"

PRODUCER_RTS = tuple(f"RT-0{n}" for n in range(17, 27))
SG_IDS = tuple(f"SG-{n:02d}" for n in range(11))
ATTACK_CLASSES = (
    "path_traversal",
    "symlink_component",
    "symlink_leaf",
    "hardlink",
    "toctou",
    "special_file",
)
PHASE_ORDER = (
    "rt_implementation",
    "rt_independent_acceptance",
    "preflight_after_candidate_freeze",
    "go_no_go_evaluation",
    "rt026_independent_acceptance",
    "final_acceptance",
)
DOWNSTREAM_PHASES = ("go_no_go_evaluation", "rt026_independent_acceptance", "final_acceptance")

EXPECTED_OWNER_SCRIPTS = {
    "RT-017": {
        "scripts/cwk_access_ledger.py",
        "scripts/cwk_collect_live.py",
        "scripts/cwk_collection_state.py",
        "scripts/cwk_tenant_collector.py",
        "scripts/cwk_cwork_source.py",
        "scripts/cwk_tenant_event_evidence.py",
        "scripts/cwk_cwork_authority.py",
        "scripts/cwk_canonical_version_provider.py",
    },
    "RT-018": {
        "scripts/cwk_job_provider.py",
        "scripts/cwk_collector_job_provider.py",
        "scripts/cwk_tenant_scheduler.py",
    },
    "RT-019": {
        "scripts/cwk_profile_sampler.py",
        "scripts/cwk_profile_proposal.py",
        "scripts/cwk_knowledge_profile.py",
        "scripts/cwk_space_id.py",
        "scripts/cwk_tenant_cmd_profile.py",
        "scripts/cwk_tenant_cli.py",
    },
    "RT-020": {
        "scripts/cwk_profile_preview.py",
        "scripts/cwk_router.py",
        "scripts/cwk_route_log.py",
    },
    "RT-021": {
        "scripts/cwk_space_registry.py",
        "scripts/cwk_space_projector.py",
        "scripts/cwk_projector_job_provider.py",
        "scripts/cwk_space_snapshot_adapter.py",
        "scripts/cwk_entity_catalog.py",
        "scripts/cwk_wiki_search_index.py",
    },
    "RT-022": {
        "scripts/cwk_query_contracts.py",
        "scripts/cwk_query_broker.py",
        "scripts/cwk_wiki_query.py",
    },
    "RT-023": {
        "scripts/cwk_gateway_tool_adapter.py",
        "scripts/cwk_query_uds.py",
        "scripts/cwk_capability_trust.py",
        "scripts/cwk_space_index_provider.py",
        "scripts/cwk_sandbox_query_client.py",
    },
    "RT-024": {
        "scripts/cwk_audit.py",
        "scripts/cwk_metrics.py",
        "scripts/cwk_pr001_benchmark.py",
    },
    "RT-025": {
        "scripts/cwk_backup_crypto.py",
        "scripts/cwk_backup.py",
        "scripts/cwk_restore.py",
    },
    "RT-026": {
        "scripts/cwk_release_switch.py",
        "scripts/cwk_shadow_query_diff.py",
        "scripts/cwk_go_no_go.py",
        "scripts/cwk_tenant_cmd_release.py",
        "scripts/cwk_tenant_cli.py",
        "scripts/cwk_nightly_pipeline.py",
        "install.sh",
        "scripts/cwk_doctor.py",
        "scripts/cwk_go_no_go_launcher.py",
    },
}

EXPECTED_EVOLUTION_STAGES = {
    "RT-017": [1, 2],
    "RT-018": [],
    "RT-019": [3],
    "RT-020": [],
    "RT-021": [4, 5],
    "RT-022": [6],
    "RT-023": [],
    "RT-024": [],
    "RT-025": [],
    "RT-026": [7, 8],
}

PILOT_ABI_CONSUMERS = {
    "RT-017", "RT-018", "RT-019", "RT-020", "RT-021", "RT-022", "RT-023", "RT-026"
}


def _load(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


class SecurityChecks:
    """Mechanical validators. Nothing here trusts prose or a caller flag.

    Everything that touches the filesystem goes through `pr001_safe_read`'s
    openat/O_NOFOLLOW/dirfd chain, and everything that claims freshness is
    recomputed through `pr001_evidence_binding` against a real git repository.
    The previous local `_safe_regular_file` was a pathlib check-then-read: it
    stat'd a path, then re-opened it by name, so every artifact carried a TOCTOU
    window and a renamed parent directory between the two steps was invisible.
    It is gone; `_sr` re-verifies dev/ino/nlink/mode/size/mtime/ctime after the
    read and never re-traverses a name it already resolved.
    """

    @staticmethod
    def _hash(receipt: dict) -> str:
        body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload = unicodedata.normalize("NFC", canonical).encode("utf-8")
        return hashlib.sha256(RECEIPT_DOMAIN + payload).hexdigest()

    # --- fail-closed filesystem access ------------------------------------

    @staticmethod
    def _safe_json(root: pathlib.Path, rel: str):
        """Read+parse through the fail-closed chain. None if unsafe or absent."""
        return _sr.try_read_json(root, rel)

    @staticmethod
    def _file_hash_matches(root: pathlib.Path, rel: object, expected: object) -> bool:
        return _sr.hash_matches(root, rel, expected)

    # --- evaluation context: injected, never assumed ----------------------

    def _git_for(self, root: pathlib.Path):
        """The `GitSubject` matching `root`, or None when there is no repo."""
        cache = getattr(self, "_git_cache", None)
        if cache is None:
            cache = self._git_cache = {}
        key = str(root)
        if key not in cache:
            cache[key] = GitSubject.for_repo(root)
        return cache[key]

    def _evaluation_commit(self, root: pathlib.Path):
        """The candidate commit under review. Never `HEAD` by accident.

        A fixture writes receipts and only then asks for a verdict, so its
        evaluation commit is made lazily from whatever is pending. A real
        repository is only ever read.
        """
        git = self._git_for(root)
        if git is None:
            return None
        if str(root) != str(getattr(self, "root", "")):
            return git.head()
        if worktree_is_dirty(git):
            return commit_all(git, "security evidence commit")
        return git.head()

    def _evidence_commit(self, root: pathlib.Path, receipt_rel: str):
        """The derived commit E that introduced `receipt_rel`, or None.

        E is DERIVED from the evaluation commit, never handed in, so a producer
        cannot nominate a convenient commit and call it the one that carried its
        receipt.
        """
        evaluation = self._evaluation_commit(root)
        cache = getattr(self, "_evidence_cache", None)
        if cache is None:
            cache = self._evidence_cache = {}
        key = (str(root), receipt_rel, evaluation)
        if key not in cache:
            cache[key] = resolve_evidence_commit(
                self._git_for(root), root, receipt_rel, evaluation_commit=evaluation
            )
        return cache[key]

    def _observed_environment(self) -> dict:
        """The environment actually running, recomputed -- never pattern-matched.

        `toolchain_build` is deliberately absent: it is not observable from the
        interpreter, so demanding it here would be a format check dressed up as
        a recomputation. Every key that IS observable must match exactly.
        """
        observed = getattr(self, "expected_environment", None)
        if observed is None:
            observed = self.expected_environment = {
                "python_version": platform.python_version(),
                "platform": sys.platform,
            }
        return observed

    def _evaluator_identity(self) -> str:
        """The frozen opaque identity of the read-only go/no-go evaluator.

        Injected from the registry by the evaluator itself. The receipt's own
        `evaluator_identity_excluded` boolean is a declaration to be CHECKED
        against this, never the proof.
        """
        return self.registry["go_no_go_evaluator_identity"]

    # --- executable evidence refs -----------------------------------------

    _SKIP_DECORATORS = frozenset({"skip", "skipIf", "skipUnless", "expectedFailure"})

    @staticmethod
    def _decorator_name(node) -> str:
        target = node.func if isinstance(node, ast.Call) else node
        while isinstance(target, ast.Attribute):
            return target.attr
        return target.id if isinstance(target, ast.Name) else ""

    @classmethod
    def _is_skipped(cls, func: ast.FunctionDef) -> bool:
        return any(cls._decorator_name(d) in cls._SKIP_DECORATORS for d in func.decorator_list)

    @staticmethod
    def _asserts_something(func: ast.FunctionDef) -> bool:
        """At least one `self.assert*` / `self.fail` call in the body.

        A ref that points at an empty or `pass`-bodied placeholder is not
        evidence, and `assert` statements are not counted either: they vanish
        under -O, so a suite could be silently disarmed.
        """
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            if fn.value.id != "self":
                continue
            if fn.attr.startswith("assert") or fn.attr == "fail":
                return True
        return False

    @classmethod
    def _class_is_live_evidence(cls, node: ast.ClassDef) -> bool:
        tests = [
            m
            for m in node.body
            if isinstance(m, ast.FunctionDef) and m.name.startswith("test")
        ]
        if not tests:
            return False
        return all(not cls._is_skipped(m) and cls._asserts_something(m) for m in tests)

    def _evidence_ref_ok(
        self,
        root: pathlib.Path,
        ref: object,
        *,
        executable: bool,
        entry: dict,
        subject_commit: object,
    ) -> bool:
        """A ref must name a node that EXISTS and, if executable, asserts.

        `<repo-relative .py path>[::Class[.test_name]]`. The file is read back
        through the fail-closed chain and parsed; a `::`-qualified node must be
        defined in that AST. An executable ref additionally may not be skipped
        and must actually assert, so it cannot point at a placeholder.
        """
        pattern = self.schema["$defs"]["evidenceRef"]["pattern"]
        if not isinstance(ref, str) or not re.fullmatch(pattern, ref):
            return False
        path, _, node_id = ref.partition("::")
        if not path.endswith(".py"):
            return False
        if not any(
            path.startswith(prefix)
            and "/" not in path[len("tests/") :]
            and re.fullmatch(r"[a-z0-9_]+\.py", path[len(prefix) :])
            for prefix in entry["owner_test_file_prefixes"]
        ):
            return False
        source = _sr.try_read_bytes(root, path)
        if source is None:
            return False
        git = self._git_for(root)
        evaluation = self._evaluation_commit(root)
        if (
            git is None
            or not isinstance(subject_commit, str)
            or not isinstance(evaluation, str)
            or git.blob_bytes(subject_commit, path) != source
            or git.blob_bytes(evaluation, path) != source
        ):
            return False
        try:
            tree = ast.parse(source.decode("utf-8"), filename=path)
        except (SyntaxError, ValueError, UnicodeDecodeError):
            return False
        if executable:
            class_name, separator, method_name = node_id.partition(".")
            if (
                not separator
                or not class_name
                or not method_name
                or not method_name.startswith("test_")
            ):
                return False
            try:
                _eg._assert_canonical_acceptance_test(
                    tree,
                    path,
                    class_name,
                    method_name,
                    "security executable evidence",
                )
            except _eg.ScriptEvolutionError:
                return False
            return True
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        if not node_id:
            return True
        class_name, _, test_name = node_id.partition(".")
        node = classes.get(class_name)
        if node is None:
            return False
        if not test_name:
            return True
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == test_name:
                return True
        return False

    def _refs_ok(
        self, root: pathlib.Path, block: dict, *, entry: dict, subject_commit: object
    ) -> bool:
        for ref in block.get("executable_refs", []):
            if not self._evidence_ref_ok(
                root,
                ref,
                executable=True,
                entry=entry,
                subject_commit=subject_commit,
            ):
                return False
        for ref in block.get("static_evidence_refs", []):
            if not self._evidence_ref_ok(
                root,
                ref,
                executable=False,
                entry=entry,
                subject_commit=subject_commit,
            ):
                return False
        return True

    @staticmethod
    def _no_deep_forbidden(node, forbidden) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in forbidden:
                    return False
                if not SecurityChecks._no_deep_forbidden(value, forbidden):
                    return False
        elif isinstance(node, list):
            for value in node:
                if not SecurityChecks._no_deep_forbidden(value, forbidden):
                    return False
        return True

    def _attack_class_ok(
        self, entry: dict, block, root: pathlib.Path, *, subject_commit: object
    ) -> bool:
        if not isinstance(block, dict):
            return False
        defs = self.schema["$defs"]["attackClass"]
        if set(block) - set(defs["properties"]):
            return False
        applicability = block.get("applicability")
        # The enum is checked against the SCHEMA, not against a local literal,
        # so a value the schema later adds cannot silently fall through to the
        # `else` branch and be treated as not_applicable.
        if applicability not in set(defs["properties"]["applicability"]["enum"]):
            return False
        mode = entry["filesystem_policy"]["mode"]
        # The registry answers "does the SG-03 family apply here?" in two
        # places; both must agree with the receipt, and with each other.
        declared = entry["path_attack_applicability"]
        if (mode == "applicable") != (declared == "applicable"):
            return False
        if applicability != declared:
            return False
        if mode == "applicable" and applicability != "applicable":
            return False
        if mode == "not_applicable_permitted" and applicability != "not_applicable":
            return False
        if applicability == "applicable":
            if {"reason_code", "reason", "static_evidence_refs"} & set(block):
                return False
            if not block.get("runtime_surfaces") or not block.get("executable_refs"):
                return False
            for surface in block["runtime_surfaces"]:
                if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", str(surface)):
                    return False
            if not isinstance(block.get("tests_run"), int) or isinstance(
                block.get("tests_run"), bool
            ):
                return False
            if block["tests_run"] <= 0:
                return False
        else:
            if {"runtime_surfaces", "executable_refs", "tests_run"} & set(block):
                return False
            if block.get("reason_code") not in entry["filesystem_policy"]["permitted_reason_codes"]:
                return False
            if not isinstance(block.get("reason"), str) or len(block["reason"]) < 16:
                return False
            if not block.get("static_evidence_refs"):
                return False
        # Refs used to be regex-matched only, so `tests/does_not_exist.py::X`
        # was accepted as coverage. They are now resolved on disk and in the
        # AST: exist, not skipped, and actually assert.
        return self._refs_ok(
            root, block, entry=entry, subject_commit=subject_commit
        )

    def _receipt_is_valid(self, entry: dict, receipt: dict, root: pathlib.Path) -> bool:
        """Everything a PASSING security receipt must satisfy."""
        schema = self.schema
        props = schema["properties"]
        if not isinstance(receipt, dict):
            return False
        # --- the object judged must BE the object on disk ------------------
        # Previously a caller could hand in any dict and have it blessed while
        # something else entirely sat at receipt_path. The receipt is re-read
        # through the fail-closed chain and must match exactly, which also
        # rejects a receipt file that is itself a symlink, a hardlink, a FIFO,
        # or that sits under a renamed/reused parent directory.
        if self._safe_json(root, entry["receipt_path"]) != receipt:
            return False
        if set(receipt) - set(props):
            return False
        if set(schema["required"]) - set(receipt):
            return False
        if not self._no_deep_forbidden(receipt, set(schema["deepForbiddenKeys"])):
            return False
        if receipt.get("schema") != schema["$id"]:
            return False
        # --- registry agreement -------------------------------------------
        if receipt.get("producer_rt") != entry["producer_rt"]:
            return False
        if receipt.get("receipt_kind") != entry["receipt_kind"]:
            return False
        if receipt.get("producer_phase") != entry["producer_phase"]:
            return False
        if receipt.get("consumers") != [c["consumer_id"] for c in entry["consumers"]]:
            return False
        # --- anti-cycle ----------------------------------------------------
        if receipt["producer_phase"] not in self.registry["write_phase_allowlist"]:
            return False
        producer_index = PHASE_ORDER.index(receipt["producer_phase"])
        for consumer in entry["consumers"]:
            if producer_index >= PHASE_ORDER.index(consumer["consumer_phase"]):
                return False
        # --- status / conclusion ------------------------------------------
        if receipt.get("status") != "pass":
            return False
        if receipt.get("conclusion") != "security_verified":
            return False
        if receipt.get("synthetic") is not False:
            return False
        if receipt.get("independent_security_verification_pass") is not True:
            return False
        if "verifier" not in receipt or receipt["verifier"] == receipt.get("producer"):
            return False
        for field in ("producer", "verifier"):
            if not re.fullmatch(props[field]["pattern"], str(receipt.get(field))):
                return False
        # --- evaluator identity: injected, never self-declared --------------
        # A reviewer showed producer='cwk-pr001-go-no-go-evaluator' with
        # evaluator_identity_excluded=true being accepted, because exclusion was
        # taken from the receipt. The evaluator now injects its own identity and
        # RECOMPUTES exclusion; the boolean is only a declaration that has to
        # agree. SCOPE: this is interface-level read-only enforcement -- it
        # proves the evaluator is not the declared author of what it consumes,
        # not that the OS denied it write access.
        evaluator = self._evaluator_identity()
        excluded = receipt.get("producer") != evaluator and receipt.get("verifier") != evaluator
        if not excluded:
            return False
        if receipt["receipt_kind"] == "preflight-security":
            if receipt.get("evaluator_identity_excluded") is not excluded:
                return False
        elif "evaluator_identity_excluded" in receipt:
            return False
        # --- claims must equal the frozen registry set exactly -------------
        frozen = {c["claim_id"]: c["sg_id"] for c in entry["claims"]}
        claims = receipt.get("claims")
        if not isinstance(claims, list) or not claims:
            return False
        seen = {}
        for claim in claims:
            if not isinstance(claim, dict):
                return False
            allowed = set(props["claims"]["items"]["properties"])
            if set(claim) - allowed:
                return False
            if set(props["claims"]["items"]["required"]) - set(claim):
                return False
            claim_id = claim["claim_id"]
            if claim_id in seen:
                return False
            seen[claim_id] = claim
            if claim_id not in frozen or claim["sg_id"] != frozen[claim_id]:
                return False
            # The enum is checked explicitly. Testing only for the string
            # "not_applicable" let an unknown value like "partially" fall into
            # the `else` branch and be validated as if it were applicable.
            claim_enum = set(props["claims"]["items"]["properties"]["applicability"]["enum"])
            if claim["applicability"] not in claim_enum:
                return False
            if claim["applicability"] == "not_applicable":
                if claim_id not in entry["na_permitted_claim_ids"]:
                    return False
                if claim.get("reason_code") not in entry["permitted_reason_codes"]:
                    return False
                if not isinstance(claim.get("reason"), str) or len(claim["reason"]) < 16:
                    return False
                if not claim.get("static_evidence_refs"):
                    return False
                if "executable_refs" in claim:
                    return False
            else:
                if not claim.get("executable_refs"):
                    return False
                if {"reason_code", "reason", "static_evidence_refs"} & set(claim):
                    return False
            if not self._refs_ok(
                root,
                claim,
                entry=entry,
                subject_commit=receipt.get("tested_subject_commit"),
            ):
                return False
        if set(seen) != set(frozen):  # missing claim OR unowned extra claim
            return False
        # --- filesystem coverage: all six classes, per policy --------------
        coverage = receipt.get("filesystem_coverage")
        if not isinstance(coverage, dict):
            return False
        if set(coverage) != set(ATTACK_CLASSES):
            return False
        for name in ATTACK_CLASSES:
            if not self._attack_class_ok(
                entry,
                coverage[name],
                root,
                subject_commit=receipt.get("tested_subject_commit"),
            ):
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
        # --- freshness: RECOMPUTED, not pattern-matched --------------------
        # `tested_subject_commit` used to be a 40-hex regex and nothing else, so
        # a commit that never existed was accepted; `environment_fingerprint`
        # was a key-set check, so a fabricated python_version/platform passed.
        # Both are now derived from the repository and the live interpreter.
        if not re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("tested_subject_commit"))):
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("owner_scope_tree_sha256"))):
            return False
        evaluation_commit = self._evaluation_commit(root)
        if not verify_security_subject_commit(
            self._git_for(root),
            receipt.get("tested_subject_commit"),
            self._evidence_commit(root, entry["receipt_path"]),
            evaluation_commit=evaluation_commit,
            entry=entry,
            registry=self.registry,
            declared_tree_sha256=receipt.get("owner_scope_tree_sha256"),
        ):
            return False
        env = receipt.get("environment_fingerprint")
        env_schema = props["environment_fingerprint"]
        if not isinstance(env, dict):
            return False
        if set(env_schema["required"]) - set(env) or set(env) - set(env_schema["properties"]):
            return False
        if not verify_environment_fingerprint(env, self._observed_environment()):
            return False
        if not re.fullmatch(props["created_at"]["pattern"], str(receipt.get("created_at"))):
            return False
        # --- artifacts -----------------------------------------------------
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return False
        declared_receipts = {e["receipt_path"] for e in self.registry["entries"]}
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                return False
            rel = artifact["path"]
            if rel in declared_receipts:  # never hash a security receipt
                return False
            # One fail-closed read: hashed through the same openat chain that
            # rejects a symlinked leaf or component, a hardlink, a FIFO and a
            # parent renamed mid-traversal.
            if not self._file_hash_matches(root, rel, artifact["sha256"]):
                return False
        if receipt.get("receipt_sha256") != self._hash(receipt):
            return False
        return True

    def _sg_state(self, entry: dict, root: pathlib.Path) -> str:
        """'NOT_RUN' | 'INVALID' | 'PASS' -- derived only from disk."""
        path = root / entry["receipt_path"]
        if not path.exists() and not path.is_symlink():
            return "NOT_RUN"
        receipt = self._safe_json(root, entry["receipt_path"])
        if not isinstance(receipt, dict):
            return "INVALID"
        return "PASS" if self._receipt_is_valid(entry, receipt, root) else "INVALID"

    # --- closure over the receipt ROOT, not over the registry -------------

    _RECEIPT_ROOT_MAX_ENTRIES = 4096

    def _receipt_root_is_closed(self, root: pathlib.Path) -> bool:
        """Every *.json under receipt_root must sit at a declared receipt_path.

        A reviewer showed SG_SATISFIED being returned while an undeclared
        `security-receipts/RT-999/receipt.json` sat on disk, because the verdict
        only ever iterated registry entries. The scan is fail-closed on its own
        terms too: any symlink, dotfile, or non-regular entry ANYWHERE under the
        receipt root is a hard failure, because none of them can be a receipt or
        a receipt's honest sibling artifact.
        """
        declared = {e["receipt_path"] for e in self.registry["entries"]}
        base_rel = self.registry["receipt_root"]
        base = root / base_rel
        if base.is_symlink():
            return False
        if not base.is_dir():
            return True  # nothing on disk yet: absence is NOT_RUN, not extra
        seen = 0
        stack = [base_rel]
        while stack:
            rel_dir = stack.pop()
            try:
                names = sorted(os.listdir(root / rel_dir))
            except OSError:
                return False
            for name in names:
                seen += 1
                if seen > self._RECEIPT_ROOT_MAX_ENTRIES:
                    return False
                if name.startswith("."):
                    return False  # a hidden entry is never legitimate here
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
                if not stat.S_ISREG(st.st_mode):
                    return False  # FIFO, socket, device: not evidence
                if name.endswith(".json") and rel not in declared:
                    return False
        return True

    def _verdict(self, root: pathlib.Path) -> str:
        if not self._receipt_root_is_closed(root):
            return "NO_GO"
        for entry in self.registry["entries"]:
            if self._sg_state(entry, root) != "PASS":
                return "NO_GO"
        return "SG_SATISFIED"


# ---------------------------------------------------------------------------
# files & schema surface
# ---------------------------------------------------------------------------


class SecurityContractFilesTests(unittest.TestCase):
    def test_all_contract_files_exist(self) -> None:
        for path in CONTRACT_FILES:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_files_are_valid_utf8_json(self) -> None:
        for path in CONTRACT_FILES:
            with self.subTest(path=path.name):
                self.assertIsInstance(_load(path), dict)

    def test_every_pattern_compiles(self) -> None:
        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str):
                    re.compile(node["pattern"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for path in CONTRACT_FILES:
            with self.subTest(path=path.name):
                walk(_load(path))


class SecurityReceiptSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load(RECEIPT_SCHEMA_PATH)

    def test_schema_id_and_closed_object(self) -> None:
        self.assertEqual(self.schema["$id"], "cwk.pr001.security_gate_receipt.v1")
        self.assertFalse(self.schema["additionalProperties"])
        self.assertFalse(self.schema["unevaluatedProperties"])

    def test_domain_separator_is_distinct_from_the_other_two_families(self) -> None:
        """A security receipt must never be swappable for a VG or activation one."""
        rule = self.schema["properties"]["receipt_sha256"]["description"]
        self.assertIn("cwk-security-gate-receipt-v1", rule)
        self.assertNotEqual(RECEIPT_DOMAIN, VG_DOMAIN)
        self.assertNotEqual(RECEIPT_DOMAIN, ACTIVATION_DOMAIN)

    def test_producer_rt_enum_is_exactly_rt017_to_rt026(self) -> None:
        self.assertEqual(
            tuple(self.schema["properties"]["producer_rt"]["enum"]), PRODUCER_RTS
        )

    def test_receipt_kind_enum_is_frozen(self) -> None:
        self.assertEqual(
            set(self.schema["properties"]["receipt_kind"]["enum"]),
            {"rt-security", "preflight-security"},
        )

    def test_filesystem_coverage_requires_all_six_classes(self) -> None:
        coverage = self.schema["properties"]["filesystem_coverage"]
        self.assertEqual(set(coverage["required"]), set(ATTACK_CLASSES))
        self.assertEqual(set(coverage["properties"]), set(ATTACK_CLASSES))
        self.assertFalse(coverage["additionalProperties"])

    def test_symlink_is_split_into_component_and_leaf_on_purpose(self) -> None:
        text = self.schema["properties"]["filesystem_coverage"]["description"]
        self.assertIn("component", text)
        self.assertIn("leaf", text)
        self.assertIn("O_NOFOLLOW", text)

    def test_required_fields_present(self) -> None:
        for field in (
            "schema",
            "receipt_kind",
            "producer_rt",
            "independent_security_verification_pass",
            "producer_phase",
            "status",
            "conclusion",
            "synthetic",
            "producer",
            "consumers",
            "claims",
            "filesystem_coverage",
            "tested_subject_commit",
            "environment_fingerprint",
            "evidence",
            "artifacts",
            "created_at",
            "receipt_sha256",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.schema["required"])
                self.assertIn(field, self.schema["properties"])

    def test_pass_flag_is_not_named_as_a_full_rt_pass(self) -> None:
        """RT-026's preflight receipt must not imply final acceptance happened."""
        self.assertNotIn("producer_rt_independent_pass", self.schema["properties"])
        text = self.schema["properties"]["independent_security_verification_pass"]["description"]
        self.assertIn("NOT a claim", text)
        for token in ("G6", "G7"):
            self.assertIn(token, text)

    def test_claim_id_pattern_is_scoped_to_rt017_rt026(self) -> None:
        pattern = self.schema["properties"]["claims"]["items"]["properties"]["claim_id"]["pattern"]
        for good in ("SGC-017-01", "SGC-022-04", "SGC-026-02"):
            self.assertRegex(good, pattern)
        for bad in ("SGC-016-01", "SGC-027-01", "SGC-17-01", "sgc-017-01"):
            self.assertNotRegex(bad, pattern)

    def test_artifact_path_pattern_rejects_absolute_and_traversal(self) -> None:
        pattern = self.schema["properties"]["artifacts"]["items"]["properties"]["path"]["pattern"]
        for bad in ("/etc/passwd", "../secret", "a/../../b", "a//b"):
            self.assertNotRegex(bad, pattern)
        self.assertRegex("PR/PR-001-multitenant-knowledge-spaces/x.md", pattern)

    def test_artifacts_must_be_non_empty(self) -> None:
        self.assertEqual(self.schema["properties"]["artifacts"]["minItems"], 1)

    def test_deep_forbidden_keys_cover_secrets_and_bodies(self) -> None:
        forbidden = set(self.schema["deepForbiddenKeys"])
        for key in ("tenant_id", "credential", "token", "secret", "raw_body", "abs_path"):
            self.assertIn(key, forbidden)

    def test_semantic_rules_state_the_machine_authority(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("machine authority", rules)
        self.assertIn("Markdown matrix", rules)
        self.assertIn("never a source of truth", self.schema["description"])

    def test_semantic_rules_encode_the_anticycle_and_preflight_rules(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        for token in (
            "strictly later",
            "go_no_go_evaluation is deliberately excluded",
            "never assert, require or depend on RT-026's final independent PASS",
            "evaluator_identity_excluded=true is required",
        ):
            self.assertIn(token, rules)

    def test_semantic_rules_require_exact_claim_equality(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("must equal the registry entry's claim_id set EXACTLY", rules)

    def test_semantic_rules_make_absence_not_run_and_extras_a_failure(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("NOT_RUN and fails closed", rules)
        self.assertIn("undeclared or extra receipt is a hard failure", rules)

    def test_semantic_rules_state_ancestry_not_equality_for_the_subject_commit(self) -> None:
        rules = " ".join(self.schema["semanticRules"])
        self.assertIn("ANCESTOR of the commit that introduces this receipt", rules)


# ---------------------------------------------------------------------------
# registry: configuration, not state
# ---------------------------------------------------------------------------


class SecurityRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(REGISTRY_SCHEMA_PATH)
        self.entries = self.registry["entries"]

    def test_registry_points_at_the_single_receipt_schema(self) -> None:
        self.assertEqual(
            self.registry["receipt_schema_ref"],
            "PR/PR-001-multitenant-knowledge-spaces/contracts/security/security_gate_receipt_v1.schema.json",
        )
        self.assertEqual(
            self.registry["receipt_schema_id"], _load(RECEIPT_SCHEMA_PATH)["$id"]
        )

    def test_registry_carries_no_mutable_execution_status(self) -> None:
        forbidden = set(self.schema["forbiddenKeys"])

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key, forbidden, f"status-like key {key} at {path}")
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(self.registry)

    def test_registry_declares_status_resolution_rule(self) -> None:
        rule = self.registry["status_resolution_rule"]
        self.assertIn("NOT RUN iff receipt_path does not exist", rule)
        self.assertIn("never satisfied by another RT", rule)

    def test_exactly_ten_unique_producer_rts_in_ascending_order(self) -> None:
        ids = [e["producer_rt"] for e in self.entries]
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10)
        self.assertEqual(tuple(ids), PRODUCER_RTS)
        self.assertEqual(ids, sorted(ids))

    def test_receipt_paths_are_unique_and_match_producer_rt(self) -> None:
        paths = [e["receipt_path"] for e in self.entries]
        self.assertEqual(len(set(paths)), len(paths))
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertEqual(
                    entry["receipt_path"],
                    f"{self.registry['receipt_root']}/{entry['producer_rt']}/receipt.json",
                )

    def test_owner_scope_v2_exact_map_is_frozen(self) -> None:
        self.assertTrue(security_registry_owner_semantics_ok(self.registry))
        by_rt = {entry["producer_rt"]: entry for entry in self.entries}
        for rt, scripts in EXPECTED_OWNER_SCRIPTS.items():
            with self.subTest(rt=rt):
                entry = by_rt[rt]
                ordinal = rt[-3:]
                expected_non_scripts = {
                    f"PR/PR-001-multitenant-knowledge-spaces/contracts/rt{ordinal}/schemas/",
                    f"RT/{rt}/specs/需求契约.md",
                    f"RT/{rt}/specs/技术方案.md",
                    f"RT/{rt}/tasks/开发任务.md",
                }
                if rt == "RT-025":
                    expected_non_scripts.add("tests/fixtures/rt025/")
                self.assertEqual(set(entry["owner_code_path_prefixes"]), scripts | expected_non_scripts)
                prefix = f"tests/test_rt{ordinal}_"
                self.assertEqual(entry["owner_test_file_prefixes"], [prefix])
                required = {f"{prefix}paths.py", f"{prefix}security.py"}
                if rt == "RT-022":
                    required.add(f"{prefix}no_fs_surface.py")
                self.assertEqual(set(entry["required_security_test_files"]), required)
                self.assertEqual(entry["owner_evolution_stage_indices"], EXPECTED_EVOLUTION_STAGES[rt])
                expected_deps = ["pilot-admission-v1"] if rt in PILOT_ABI_CONSUMERS else []
                self.assertEqual(entry["required_shared_abi_ids"], expected_deps)

    def test_managed_script_inventory_is_a_closed_three_family_partition(self) -> None:
        inventory = self.registry["managed_script_inventory"]
        self.assertEqual(inventory["schema"], "cwk.pr001.managed_script_inventory.v1")
        self.assertEqual(inventory["explicit_managed_paths"], ["install.sh"])
        namespace = re.compile(inventory["namespace_pattern"])
        owner = {
            path
            for entry in self.entries
            for path in entry["owner_code_path_prefixes"]
            if namespace.fullmatch(path) or path == "install.sh"
        }
        central = {
            binding["path"]
            for dependency in self.registry["shared_abi_dependencies"]
            for binding in dependency["exact_paths"]
            if namespace.fullmatch(binding["path"]) or binding["path"] == "install.sh"
        }
        legacy = {row["path"] for row in inventory["legacy_frozen_files"]}
        self.assertEqual((len(owner), len(central), len(legacy)), (48, 1, 53))
        self.assertEqual(len(owner | central | legacy), 102)
        self.assertFalse(owner & central)
        self.assertFalse(owner & legacy)
        self.assertFalse(central & legacy)

    def test_every_legacy_inventory_pin_matches_the_current_regular_file(self) -> None:
        rows = self.registry["managed_script_inventory"]["legacy_frozen_files"]
        self.assertEqual([row["path"] for row in rows], sorted(row["path"] for row in rows))
        for row in rows:
            with self.subTest(path=row["path"]):
                path = REPO_ROOT / row["path"]
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
                self.assertEqual(mode, row["mode"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])

    def test_rt025_clean_room_fixture_is_an_exact_nonempty_owner_selector(self) -> None:
        entry = next(item for item in self.entries if item["producer_rt"] == "RT-025")
        self.assertIn("tests/fixtures/rt025/", entry["owner_code_path_prefixes"])
        self.assertEqual(entry["unresolved_owner_surface_requirements"], [])

    def test_inventory_rejects_missing_or_colliding_categories(self) -> None:
        def clone() -> dict:
            return json.loads(json.dumps(self.registry))

        missing = clone()
        missing["managed_script_inventory"]["legacy_frozen_files"].pop()
        self.assertFalse(security_registry_owner_semantics_ok(missing))

        collision = clone()
        collision["managed_script_inventory"]["legacy_frozen_files"][0]["path"] = (
            "scripts/cwk_access_ledger.py"
        )
        self.assertFalse(security_registry_owner_semantics_ok(collision))


    def test_reports_receipts_and_capability_outputs_are_evidence_not_code(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                evidence = entry["owner_evidence_path_prefixes"]
                self.assertIn(f"RT/{entry['producer_rt']}/reports/", evidence)
                self.assertIn(f"RT/{entry['producer_rt']}/receipts/", evidence)
                self.assertFalse(
                    any(
                        component in selector.split("/")
                        for selector in entry["owner_code_path_prefixes"]
                        for component in (
                            "reports", "receipts", "security-receipts",
                            "capability-receipts", "gate-receipts", "release-gate-receipts",
                        )
                    )
                )

    def test_pilot_admission_is_hash_pinned_as_neutral_shared_abi(self) -> None:
        self.assertEqual(len(self.registry["shared_abi_dependencies"]), 1)
        dependency = self.registry["shared_abi_dependencies"][0]
        self.assertEqual(dependency["dependency_id"], "pilot-admission-v1")
        self.assertEqual(set(dependency["consumer_rts"]), PILOT_ABI_CONSUMERS)
        for binding in dependency["exact_paths"]:
            with self.subTest(path=binding["path"]):
                raw = (REPO_ROOT / binding["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), binding["sha256"])
                self.assertFalse(
                    any(binding["path"] in entry["owner_code_path_prefixes"] for entry in self.entries)
                )
        central_test = REPO_ROOT / "tests/test_pr001_pilot_admission_contract.py"
        self.assertTrue(central_test.is_file())
        self.assertEqual(
            hashlib.sha256(central_test.read_bytes()).hexdigest(),
            "1416d8d273868b91eeb0f0f563a1e3103cba8ec27c8a15d5209dd6eb1395386e",
        )

    def test_only_tenant_cli_may_overlap_and_only_at_stages_three_and_seven(self) -> None:
        owners = {}
        for entry in self.entries:
            for selector in entry["owner_code_path_prefixes"]:
                owners.setdefault(selector, []).append(entry["producer_rt"])
        overlaps = {path: rts for path, rts in owners.items() if len(rts) > 1}
        self.assertEqual(overlaps, {"scripts/cwk_tenant_cli.py": ["RT-019", "RT-026"]})
        self.assertEqual(
            self.registry["shared_evolution_paths"],
            [{
                "path": "scripts/cwk_tenant_cli.py",
                "owner_stage_indices": {"RT-019": [3], "RT-026": [7]},
            }],
        )
        policy = _load(
            PR_ROOT / "contracts" / "script-evolution" / "policy_v1.json"
        )
        stage9 = next(stage for stage in policy["stages"] if stage["stage_index"] == 9)
        self.assertEqual(
            {
                key: stage9[key]
                for key in ("owner_rt", "target_path", "ordinal", "receipt_path")
            },
            {
                "owner_rt": "RT-012",
                "target_path": "scripts/cwk_instance.py",
                "ordinal": 1,
                "receipt_path": (
                    "RT/RT-012/receipts/script-evolution/"
                    "stage-09-cwk-instance-ord1.json"
                ),
            },
        )
        instance_pin = next(
            row
            for row in self.registry["managed_script_inventory"]["legacy_frozen_files"]
            if row["path"] == "scripts/cwk_instance.py"
        )
        self.assertEqual(
            instance_pin["sha256"],
            "827a3dacafd746ab760360c7a872362fb9c9327a2622c13dccb95c1bdec59d4f",
        )
        stage10 = next(stage for stage in policy["stages"] if stage["stage_index"] == 10)
        self.assertEqual(
            {
                key: stage10[key]
                for key in ("owner_rt", "target_path", "ordinal", "receipt_path")
            },
            {
                "owner_rt": "RT-013",
                "target_path": "scripts/cwk_agent_binding.py",
                "ordinal": 1,
                "receipt_path": (
                    "RT/RT-013/receipts/script-evolution/"
                    "stage-10-cwk-agent-binding-ord1.json"
                ),
            },
        )
        binding_pin = next(
            row
            for row in self.registry["managed_script_inventory"]["legacy_frozen_files"]
            if row["path"] == "scripts/cwk_agent_binding.py"
        )
        self.assertEqual(
            binding_pin["sha256"],
            "2d390d6fa1a5b84e1dcc137e64c642f3a1a9cb010e009fa5c7a6e00e076030c4",
        )
        producer_rts = {entry["producer_rt"] for entry in self.entries}
        self.assertTrue({"RT-012", "RT-013"}.isdisjoint(producer_rts))

    def test_rt026_exact_runtime_paths_are_frozen_and_no_surface_is_unresolved(self) -> None:
        entry = next(item for item in self.entries if item["producer_rt"] == "RT-026")
        self.assertEqual(entry["unresolved_owner_surface_requirements"], [])
        self.assertTrue(
            {
                "install.sh",
                "scripts/cwk_doctor.py",
                "scripts/cwk_go_no_go_launcher.py",
            }
            <= set(entry["owner_code_path_prefixes"])
        )

    def test_sg_coverage_union_is_exactly_the_closed_set(self) -> None:
        union = set()
        for entry in self.entries:
            self.assertTrue(entry["owned_sg_ids"], f"{entry['producer_rt']} owns nothing")
            union |= set(entry["owned_sg_ids"])
        self.assertEqual(union, set(SG_IDS))
        self.assertEqual(tuple(self.registry["sg_ids"]), SG_IDS)

    def test_sg03_is_owned_by_all_ten_packages(self) -> None:
        """Filesystem coverage must be provable package by package."""
        owners = [e["producer_rt"] for e in self.entries if "SG-03" in e["owned_sg_ids"]]
        self.assertEqual(tuple(owners), PRODUCER_RTS)

    def test_claim_ids_are_globally_unique_and_rt_scoped(self) -> None:
        seen = {}
        for entry in self.entries:
            for claim in entry["claims"]:
                with self.subTest(claim=claim["claim_id"]):
                    self.assertNotIn(claim["claim_id"], seen)
                    seen[claim["claim_id"]] = entry["producer_rt"]
                    self.assertRegex(claim["claim_id"], r"^SGC-0(1[7-9]|2[0-6])-[0-9]{2}$")
                    self.assertEqual(
                        claim["claim_id"].split("-")[1], entry["producer_rt"].split("-")[1]
                    )
        self.assertEqual(len(seen), sum(len(e["claims"]) for e in self.entries))

    def test_claim_sg_ids_equal_the_owned_sg_set(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertEqual(
                    {c["sg_id"] for c in entry["claims"]}, set(entry["owned_sg_ids"])
                )

    def test_claim_requirements_are_requirements_not_verdicts(self) -> None:
        for entry in self.entries:
            for claim in entry["claims"]:
                with self.subTest(claim=claim["claim_id"]):
                    text = claim["requirement"].lower()
                    self.assertGreaterEqual(len(claim["requirement"]), 16)
                    for banned in ("passed", "verified on", "not run", "no_go"):
                        self.assertNotIn(banned, text)

    def test_na_permissions_are_subsets_and_paired_with_reason_codes(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                ids = {c["claim_id"] for c in entry["claims"]}
                self.assertTrue(set(entry["na_permitted_claim_ids"]) <= ids)
                self.assertEqual(
                    bool(entry["na_permitted_claim_ids"]),
                    bool(entry["permitted_reason_codes"]),
                )

    def test_only_rt022_may_declare_the_path_family_not_applicable(self) -> None:
        na = [
            e["producer_rt"]
            for e in self.entries
            if e["path_attack_applicability"] == "not_applicable"
        ]
        self.assertEqual(na, ["RT-022"])

    def test_filesystem_policy_agrees_with_path_attack_applicability(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                policy = entry["filesystem_policy"]
                self.assertEqual(
                    entry["path_attack_applicability"] == "applicable",
                    policy["mode"] == "applicable",
                )
                self.assertEqual(
                    policy["mode"] == "applicable", policy["permitted_reason_codes"] == []
                )
                self.assertEqual(policy["permitted_reason_codes"], entry["permitted_reason_codes"])

    def test_no_entry_may_narrow_the_six_attack_classes(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertEqual(
                    tuple(entry["filesystem_policy"]["required_classes"]), ATTACK_CLASSES
                )
        self.assertEqual(tuple(self.registry["filesystem_attack_classes"]), ATTACK_CLASSES)

    def test_every_entry_states_a_path_attack_note_in_both_directions(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertGreaterEqual(len(entry["path_attack_note"]), 16)
                if entry["path_attack_applicability"] == "not_applicable":
                    self.assertIn("N/A with reason", entry["path_attack_note"])

    def test_producer_tasks_live_inside_the_producing_rt_package(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertTrue(
                    entry["producer_task_ref"].startswith(f"RT/{entry['producer_rt']}/tasks/")
                )

    def test_declared_producer_task_refs_exist_on_disk(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertTrue(
                    (REPO_ROOT / entry["producer_task_ref"]).is_file(),
                    f"missing {entry['producer_task_ref']}",
                )

    def test_every_declared_producer_task_id_exists_in_its_rt_task_list(self) -> None:
        """The registry may not name a task that no RT package actually declares.

        Without this, the registry could point at ``D-99`` forever and nothing
        would ever be produced -- absence would look like a planning state
        rather than a broken contract.
        """
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                text = (REPO_ROOT / entry["producer_task_ref"]).read_text(encoding="utf-8")
                task_id = entry["producer_task_id"]
                # Both separators are in live use across the RT packages.
                self.assertTrue(
                    any(f"- [ ] {task_id}{sep}" in text for sep in (" ", "：", ":")),
                    f"{entry['producer_rt']} does not declare task {task_id}",
                )

    def test_every_rt_package_declares_its_exact_receipt_path(self) -> None:
        """Each planned RT must spell out the exact path it is responsible for."""
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                text = (REPO_ROOT / entry["producer_task_ref"]).read_text(encoding="utf-8")
                self.assertIn(entry["receipt_path"], text)

    def test_every_rt_package_declares_its_exact_claim_ids(self) -> None:
        for entry in self.entries:
            text = (REPO_ROOT / entry["producer_task_ref"]).read_text(encoding="utf-8")
            for claim in entry["claims"]:
                with self.subTest(claim=claim["claim_id"]):
                    self.assertIn(claim["claim_id"], text)

    def test_applicable_rt_packages_name_every_filesystem_attack_class(self) -> None:
        """Generic prose is not coverage: the six classes must be named by ID."""
        for entry in self.entries:
            if entry["filesystem_policy"]["mode"] != "applicable":
                continue
            text = (REPO_ROOT / entry["producer_task_ref"]).read_text(encoding="utf-8")
            for cls in entry["filesystem_policy"]["required_classes"]:
                with self.subTest(rt=entry["producer_rt"], cls=cls):
                    self.assertIn(cls, text)

    def test_na_permitting_rt_package_names_its_permitted_claims_and_reasons(self) -> None:
        for entry in self.entries:
            if entry["filesystem_policy"]["mode"] == "applicable":
                continue
            text = (REPO_ROOT / entry["producer_task_ref"]).read_text(encoding="utf-8")
            for claim_id in entry["na_permitted_claim_ids"]:
                with self.subTest(rt=entry["producer_rt"], claim=claim_id):
                    self.assertIn(claim_id, text)
            for code in entry["permitted_reason_codes"]:
                with self.subTest(rt=entry["producer_rt"], code=code):
                    self.assertIn(code, text)

    def test_rt026_package_states_the_preflight_anti_cycle(self) -> None:
        """RT-026 must never be the declared AUTHOR of the receipt it consumes.

        The claim is interface-level: a declared-phase exclusion plus an
        authorship exclusion recomputed against the injected evaluator
        identity. The task file must state it that way -- the older wording
        ("consumes but never writes") overclaimed OS-level write denial.
        """
        text = (REPO_ROOT / "RT/RT-026/tasks/开发任务.md").read_text(encoding="utf-8")
        self.assertIn("preflight_after_candidate_freeze", text)
        self.assertIn("independent_preflight_verifier", text)
        self.assertIn("evaluator_identity_excluded", text)
        self.assertIn("write_phase_allowlist", text)
        self.assertIn("go_no_go_evaluator_identity", text)
        self.assertIn("署名作者", text)

    def test_rt026_package_does_not_overclaim_os_level_write_denial(self) -> None:
        """The qualified wording must be present and the overclaim absent."""
        text = (REPO_ROOT / "RT/RT-026/tasks/开发任务.md").read_text(encoding="utf-8")
        self.assertNotIn("消费但绝不写入", text)
        self.assertIn("接口级", text)
        self.assertIn("AC-026-11", text)

    def test_rt026_package_calls_the_exclusion_flag_a_declaration(self) -> None:
        """evaluator_identity_excluded is a declaration checked by recomputation."""
        text = (REPO_ROOT / "RT/RT-026/tasks/开发任务.md").read_text(encoding="utf-8")
        self.assertIn("声明", text)
        self.assertIn("重算", text)
        self.assertNotIn("证明签发者不是评估器本身", text)

    def test_producer_roles_are_always_independent(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertIn(
                    entry["producer_role"],
                    {"independent_acceptance_verifier", "independent_preflight_verifier"},
                )

    def test_static_notes_do_not_describe_run_outcomes(self) -> None:
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                text = entry["static_note"].lower()
                for banned in ("today", "currently passed", "已通过", "已执行"):
                    self.assertNotIn(banned, text)

    def test_plan_matrix_is_declared_as_derived_only(self) -> None:
        self.assertIn("剩余工作执行计划", self.registry["plan_matrix_ref"])
        self.assertIn("derived documentation only", self.registry["description"])


class SecurityRegistrySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(REGISTRY_SCHEMA_PATH)

    def test_registry_top_level_keys_match_schema(self) -> None:
        self.assertEqual(set(self.registry), set(self.schema["properties"]))
        self.assertEqual(set(self.schema["required"]) - set(self.registry), set())

    def test_managed_inventory_schema_is_closed_and_freezes_all_legacy_rows(self) -> None:
        frozen = self.schema["properties"]["managed_script_inventory"]
        self.assertFalse(frozen["additionalProperties"])
        self.assertEqual(
            set(frozen["required"]), set(frozen["properties"])
        )
        legacy = frozen["properties"]["legacy_frozen_files"]
        self.assertEqual((legacy["minItems"], legacy["maxItems"]), (53, 53))
        self.assertFalse(legacy["items"]["additionalProperties"])

    def test_entry_keys_match_schema(self) -> None:
        entry_schema = self.schema["$defs"]["entry"]
        for entry in self.registry["entries"]:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertEqual(set(entry), set(entry_schema["properties"]))
                self.assertEqual(set(entry_schema["required"]) - set(entry), set())

    def test_schema_freezes_exactly_ten_entries(self) -> None:
        self.assertEqual(self.schema["properties"]["entries"]["minItems"], 10)
        self.assertEqual(self.schema["properties"]["entries"]["maxItems"], 10)

    def test_schema_is_a_closed_object_with_no_status_field(self) -> None:
        self.assertFalse(self.schema["additionalProperties"])
        self.assertFalse(self.schema["unevaluatedProperties"])
        for key in ("status", "conclusion", "verdict", "result", "passed"):
            with self.subTest(key=key):
                self.assertIn(key, self.schema["forbiddenKeys"])
                self.assertNotIn(key, self.schema["properties"])
                self.assertNotIn(key, self.schema["$defs"]["entry"]["properties"])

    def test_schema_excludes_the_evaluator_phase_from_producers(self) -> None:
        producer_enum = set(self.schema["$defs"]["entry"]["properties"]["producer_phase"]["enum"])
        self.assertNotIn("go_no_go_evaluation", producer_enum)
        self.assertEqual(producer_enum, set(self.registry["write_phase_allowlist"]))

    def test_registry_values_satisfy_schema_patterns(self) -> None:
        entry_props = self.schema["$defs"]["entry"]["properties"]
        for entry in self.registry["entries"]:
            for field in ("receipt_path", "producer_task_id", "producer_task_ref"):
                with self.subTest(rt=entry["producer_rt"], field=field):
                    self.assertRegex(entry[field], entry_props[field]["pattern"])

    def test_phase_order_is_frozen(self) -> None:
        self.assertEqual(tuple(self.registry["phase_order"]), PHASE_ORDER)
        self.assertEqual(
            set(self.schema["properties"]["phase_order"]["items"]["enum"]), set(PHASE_ORDER)
        )


# ---------------------------------------------------------------------------
# anti-cycle
# ---------------------------------------------------------------------------


class SecurityAntiCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.entries = self.registry["entries"]

    def test_every_consumer_sits_strictly_later_than_its_producer(self) -> None:
        for entry in self.entries:
            producer = PHASE_ORDER.index(entry["producer_phase"])
            for consumer in entry["consumers"]:
                with self.subTest(rt=entry["producer_rt"], consumer=consumer["consumer_id"]):
                    self.assertLess(
                        producer,
                        PHASE_ORDER.index(consumer["consumer_phase"]),
                        "producer phase must precede every consumer phase",
                    )

    def test_the_evaluator_phase_is_excluded_from_the_declared_write_allowlist(
        self,
    ) -> None:
        """DECLARED-PHASE exclusion, not OS-level write denial.

        The allowlist proves only that no registry entry names
        go_no_go_evaluation as the phase in which its receipt is authored.
        Combined with the authorship exclusion recomputed against the injected
        go_no_go_evaluator_identity, that is an interface-level guarantee. It
        does NOT show the kernel refused the evaluating process write access;
        that claim belongs to AC-026-11 (T026-15a~d) and is not proven here.
        """
        self.assertNotIn("go_no_go_evaluation", self.registry["write_phase_allowlist"])
        for entry in self.entries:
            with self.subTest(rt=entry["producer_rt"]):
                self.assertIn(entry["producer_phase"], self.registry["write_phase_allowlist"])

    def test_registry_states_the_interface_level_scope_of_that_exclusion(self) -> None:
        """The machine authority itself must carry the scope-honesty caveat."""
        rule = self.registry["evaluator_identity_rule"]
        self.assertIn("INTERFACE-LEVEL", rule)
        self.assertIn("does NOT prove", rule)
        self.assertIn("OS level", rule)
        self.assertIn("declaration", rule)
        self.assertIn("no longer TRUSTED", rule)
        self.assertIn(
            "INTERFACE-LEVEL authorship exclusion only", self.registry["anti_cycle_rule"]
        )

    def test_rt026_is_the_only_preflight_entry(self) -> None:
        preflight = [
            e["producer_rt"]
            for e in self.entries
            if e["producer_phase"] == "preflight_after_candidate_freeze"
        ]
        self.assertEqual(preflight, ["RT-026"])
        rt026 = next(e for e in self.entries if e["producer_rt"] == "RT-026")
        self.assertEqual(rt026["receipt_kind"], "preflight-security")
        self.assertEqual(rt026["producer_role"], "independent_preflight_verifier")

    def test_rt026_does_not_depend_on_its_own_output(self) -> None:
        """Produced in preflight, consumed in the strictly later evaluator phase."""
        rt026 = next(e for e in self.entries if e["producer_rt"] == "RT-026")
        producer = PHASE_ORDER.index(rt026["producer_phase"])
        consumer = next(c for c in rt026["consumers"] if c["consumer_id"] == "RT-026")
        self.assertLess(producer, PHASE_ORDER.index(consumer["consumer_phase"]))
        self.assertEqual(consumer["consumer_phase"], "go_no_go_evaluation")

    def test_no_entry_is_consumed_before_it_is_produced(self) -> None:
        """The whole graph, not just RT-026."""
        for entry in self.entries:
            for consumer in entry["consumers"]:
                self.assertNotEqual(entry["producer_phase"], consumer["consumer_phase"])

    def test_rt017_to_rt025_all_produce_at_independent_acceptance(self) -> None:
        for entry in self.entries:
            if entry["producer_rt"] == "RT-026":
                continue
            with self.subTest(rt=entry["producer_rt"]):
                self.assertEqual(entry["producer_phase"], "rt_independent_acceptance")
                self.assertEqual(entry["receipt_kind"], "rt-security")

    def test_anti_cycle_rule_is_stated_mechanically(self) -> None:
        rule = self.registry["anti_cycle_rule"]
        self.assertIn("phase_order.index(producer_phase) < phase_order.index(consumer_phase)", rule)
        self.assertIn("never depends on its own output", rule)

    def test_freshness_rule_is_ancestry_not_head_equality(self) -> None:
        rule = self.registry["evidence_freshness_rule"]
        self.assertIn("ANCESTRY rule, not an equality rule", rule)
        self.assertIn("NOT required to equal the final RT-026 HEAD", rule)
        self.assertIn("no two artifacts ever hash each other", rule)


# ---------------------------------------------------------------------------
# on-disk state today
# ---------------------------------------------------------------------------


class SecurityReceiptsOnDiskTests(SecurityChecks, unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(RECEIPT_SCHEMA_PATH)

    def test_absence_is_not_run_and_never_inferred_as_satisfied(self) -> None:
        for entry in self.registry["entries"]:
            with self.subTest(rt=entry["producer_rt"]):
                state = self._sg_state(entry, REPO_ROOT)
                self.assertIn(state, {"NOT_RUN", "PASS"})
                if not (REPO_ROOT / entry["receipt_path"]).is_file():
                    self.assertEqual(state, "NOT_RUN")

    def test_repo_verdict_is_fail_closed_and_consistent(self) -> None:
        not_pass = [
            e["producer_rt"]
            for e in self.registry["entries"]
            if self._sg_state(e, REPO_ROOT) != "PASS"
        ]
        verdict = self._verdict(REPO_ROOT)
        if not_pass:
            self.assertEqual(verdict, "NO_GO", f"not satisfied: {not_pass}")
        else:
            self.assertEqual(verdict, "SG_SATISFIED")

    def test_no_undeclared_receipt_exists_under_the_receipt_root(self) -> None:
        declared = {
            (REPO_ROOT / e["receipt_path"]).resolve() for e in self.registry["entries"]
        }
        root = REPO_ROOT / self.registry["receipt_root"]
        if root.is_dir():
            on_disk = {p.resolve() for p in root.rglob("*.json")}
            self.assertEqual(on_disk - declared, set(), "undeclared security receipt")
        # The verdict must reach the same conclusion by itself, without the
        # test doing the enumeration for it.
        self.assertTrue(self._receipt_root_is_closed(REPO_ROOT))

    def test_planned_rts_are_not_marked_pass_anywhere_on_disk(self) -> None:
        """A planned RT must not have a PASS receipt sitting in the tree."""
        for entry in self.registry["entries"]:
            path = REPO_ROOT / entry["receipt_path"]
            if not path.is_file():
                continue
            with self.subTest(rt=entry["producer_rt"]):
                self.assertTrue(self._receipt_is_valid(entry, _load(path), REPO_ROOT))


# ---------------------------------------------------------------------------
# future receipts: constructible, and every omission rejected
# ---------------------------------------------------------------------------


class SecurityFixture(SecurityChecks):
    """A buildable future repository: real git, real code, real test modules.

    Shared by the reachability/omission suite and by the binding-attack suite
    so the expensive git fixture is described once and the attacks cannot
    quietly diverge from the shape they are attacking.

    The sandbox is a REAL git repository with a real two-generation history,
    because every freshness claim in a security receipt is now recomputed
    rather than pattern-matched. Generation one commits each package's owner
    code under `RT/RT-0xx/` plus the executable evidence modules the receipts
    point at; that commit is the `tested_subject_commit`. The receipts land in
    a later evidence-only commit made lazily when a verdict is first asked
    for, and the evidence commit `E` is DERIVED from it. Without this a
    receipt could name a commit that never existed, declare a Python that was
    never running, and cite tests that were never written.
    """

    def setUp(self) -> None:
        self.registry = _load(REGISTRY_PATH)
        self.schema = _load(RECEIPT_SCHEMA_PATH)
        self.by_rt = {e["producer_rt"]: e for e in self.registry["entries"]}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name).resolve()
        self._git_cache = {}
        self._evidence_cache = {}
        self.git = init_fixture_repo(self.root)
        self._prepare_evolution_baseline()
        for entry in self.registry["entries"]:
            self._write_owner_code(entry)
            self._write_evidence_modules(entry)
        self._write_shared_abi_dependencies()
        self._write_placeholder_module()
        commit_all(self.git, "planned owner-scope baseline")
        self.subject_commit_by_rt = {}
        for entry in self.registry["entries"]:
            for stage_index in entry["owner_evolution_stage_indices"]:
                if stage_index not in self._materialized_stage_indices:
                    self._add_evolution_stage(stage_index)
            self._touch_unique_owner_path(entry)
            self.subject_commit_by_rt[entry["producer_rt"]] = commit_all(
                self.git, f"{entry['producer_rt']} implementation candidate"
            )
        self.expected_environment = {
            "python_version": platform.python_version(),
            "platform": sys.platform,
        }

    # --- the repository the receipts will describe ------------------------

    def _prepare_evolution_baseline(self) -> None:
        policy_dir = PR_ROOT / "contracts" / "script-evolution"
        for name in ("policy_v1.json", "policy_v1.schema.json", "receipt_v1.schema.json"):
            rel = f"PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/{name}"
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((policy_dir / name).read_bytes())
        self._evolution_policy = _load(policy_dir / "policy_v1.json")
        self._evolution_tips = {
            item["target_path"]: item["genesis_sha256"]
            for item in self._evolution_policy["evolvable_paths"]
        }
        self._evolution_previous_raw = {}
        self._evolution_test_methods = {}
        self._materialized_stage_indices = set()
        stages_by_target = {
            rel: sorted(
                (
                    stage
                    for stage in self._evolution_policy["stages"]
                    if stage["target_path"] == rel
                ),
                key=lambda stage: stage["ordinal"],
            )
            for rel in self._evolution_tips
        }
        for rel, genesis in list(self._evolution_tips.items()):
            stages = stages_by_target[rel]
            present = [
                (REPO_ROOT / stage["receipt_path"]).is_file()
                for stage in stages
            ]
            self.assertNotIn(
                (False, True),
                set(zip(present, present[1:])),
                f"materialized evolution is not prefix-closed for {rel}",
            )
            raw = (REPO_ROOT / rel).read_bytes()
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            tip = genesis
            previous_raw = None
            for stage, exists in zip(stages, present):
                if not exists:
                    break
                receipt_source = REPO_ROOT / stage["receipt_path"]
                receipt_raw = receipt_source.read_bytes()
                receipt = json.loads(receipt_raw)
                self.assertEqual(
                    receipt["policy_sha256"],
                    self.registry["script_evolution_policy_sha256"],
                )
                self.assertEqual(receipt["stage_index"], stage["stage_index"])
                self.assertEqual(receipt["owner_rt"], stage["owner_rt"])
                self.assertEqual(receipt["target_path"], rel)
                self.assertEqual(receipt["from_sha256"], tip)
                expected_link = (
                    hashlib.sha256(previous_raw).hexdigest()
                    if previous_raw is not None
                    else _eg.genesis_link(
                        domain=self._evolution_policy["genesis_link_domain"],
                        policy_sha256=self.registry["script_evolution_policy_sha256"],
                        target_path=rel,
                    )
                )
                self.assertEqual(receipt["previous_receipt_sha256"], expected_link)
                source_rels = [
                    stage["receipt_path"],
                    stage["migration_note_path"],
                    *(ref.split("::", 1)[0] for ref in receipt["acceptance_test_refs"]),
                ]
                for source_rel in source_rels:
                    source = REPO_ROOT / source_rel
                    self.assertTrue(source.is_file(), source_rel)
                    destination = self.root / source_rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
                tip = receipt["to_sha256"]
                previous_raw = receipt_raw
                self._materialized_stage_indices.add(stage["stage_index"])
            expected = tip if any(present) else genesis
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
            self._evolution_tips[rel] = expected
            if previous_raw is not None:
                self._evolution_previous_raw[rel] = previous_raw
        for companion in self._evolution_policy["companion_immutable_paths"]:
            rel = companion["target_path"]
            raw = (REPO_ROOT / rel).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), companion["sha256"])
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        for legacy in self.registry["managed_script_inventory"]["legacy_frozen_files"]:
            rel = legacy["path"]
            raw = (REPO_ROOT / rel).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), legacy["sha256"])
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

    def _write_shared_abi_dependencies(self) -> None:
        for dependency in self.registry["shared_abi_dependencies"]:
            for binding in dependency["exact_paths"]:
                rel = binding["path"]
                target = self.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                raw = (REPO_ROOT / rel).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), binding["sha256"])
                target.write_bytes(raw)

    @staticmethod
    def _rt_slug(rt_id: str) -> str:
        return rt_id.lower().replace("-", "")

    def _write_owner_code(self, entry: dict) -> None:
        for selector in entry["owner_code_path_prefixes"]:
            if selector.endswith("/"):
                target = self.root / selector / "scope.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"owner": entry["producer_rt"]}, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                continue
            target = self.root / selector
            if target.exists():
                continue  # an evolution target already has its exact genesis bytes
            target.parent.mkdir(parents=True, exist_ok=True)
            if selector.endswith(".py"):
                target.write_text(
                    f'"""owner code for {entry["producer_rt"]}."""\n'
                    'def open_scoped(name):\n'
                    '    return "denied"\n',
                    encoding="utf-8",
                )
            elif selector.endswith(".json"):
                target.write_text(json.dumps({"owner": entry["producer_rt"]}) + "\n")
            else:
                target.write_text(f"# {entry['producer_rt']} frozen contract\n", encoding="utf-8")

    def _touch_unique_owner_path(self, entry: dict) -> None:
        shared = {item["path"] for item in self.registry["shared_evolution_paths"]}
        evolvable = {item["target_path"] for item in self._evolution_policy["stages"]}
        for selector in entry["owner_code_path_prefixes"]:
            if selector.endswith("/") or selector in shared or selector in evolvable:
                continue
            target = self.root / selector
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"\n# frozen by {entry['producer_rt']} candidate\n")
            return
        self.fail(f"fixture has no unique owner path for {entry['producer_rt']}")

    def _add_evolution_stage(self, stage_index: int) -> None:
        stage = next(
            item for item in self._evolution_policy["stages"] if item["stage_index"] == stage_index
        )
        target = self.root / stage["target_path"]
        if stage["adds_provider_slot"] is not None:
            source = target.read_text(encoding="utf-8")
            marker = (
                f'    # {stage["owner_rt"]} will ship: '
                f'"{stage["adds_provider_slot"]}",'
            )
            replacement = f'    "{stage["adds_provider_slot"]}",'
            self.assertIn(marker, source)
            target.write_text(source.replace(marker, replacement, 1), encoding="utf-8")
        else:
            target.write_bytes(
                target.read_bytes() + f"\n# evolved by stage {stage_index}\n".encode("utf-8")
            )
        to_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        note = self.root / stage["migration_note_path"]
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f"# Stage {stage_index} migration\n\nOwner: {stage['owner_rt']}\n"
            f"Target: {stage['target_path'].rsplit('/', 1)[-1]}\n\n"
            "This fixture note binds a real append-only evolution receipt and is "
            "long enough for the migration-note contract. It is synthetic test evidence only.\n",
            encoding="utf-8",
        )
        rt_slug = self._rt_slug(stage["owner_rt"])
        test_rel = f"tests/test_{rt_slug}_evolution.py"
        method = f"test_stage_{stage_index:02d}"
        self._evolution_test_methods.setdefault(test_rel, set()).add(method)
        methods = self._evolution_test_methods[test_rel]
        source = ["import unittest", "", "", "class EvolutionTests(unittest.TestCase):"]
        for name in sorted(methods):
            source.extend([f"    def {name}(self):", "        self.assertTrue(True)", ""])
        test_target = self.root / test_rel
        test_target.parent.mkdir(parents=True, exist_ok=True)
        test_target.write_text("\n".join(source) + "\n", encoding="utf-8")
        previous = self._evolution_previous_raw.get(stage["target_path"])
        link = (
            hashlib.sha256(previous).hexdigest()
            if previous is not None
            else _eg.genesis_link(
                domain=self._evolution_policy["genesis_link_domain"],
                policy_sha256=self.registry["script_evolution_policy_sha256"],
                target_path=stage["target_path"],
            )
        )
        receipt = {
            "schema": "cwk.pr001.script_evolution_receipt.v1",
            "policy_id": self._evolution_policy["policy_id"],
            "policy_sha256": self.registry["script_evolution_policy_sha256"],
            "stage_index": stage_index,
            "owner_rt": stage["owner_rt"],
            "target_path": stage["target_path"],
            "ordinal": stage["ordinal"],
            "adds_provider_slot": stage["adds_provider_slot"],
            "from_sha256": self._evolution_tips[stage["target_path"]],
            "to_sha256": to_sha,
            "previous_receipt_sha256": link,
            "migration_note_path": stage["migration_note_path"],
            "migration_note_sha256": hashlib.sha256(note.read_bytes()).hexdigest(),
            "acceptance_test_refs": [f"{test_rel}::EvolutionTests::{method}"],
            "recorded_at": "2026-08-21T00:00:00Z",
        }
        raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
        receipt_target = self.root / stage["receipt_path"]
        receipt_target.parent.mkdir(parents=True, exist_ok=True)
        receipt_target.write_bytes(raw)
        self._evolution_tips[stage["target_path"]] = to_sha
        self._evolution_previous_raw[stage["target_path"]] = raw

    def _write_module(self, rel: str, body: str) -> str:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return rel

    _MODULE_HEADER = (
        '"""Generated security-evidence module for {rt}."""\n'
        "import unittest\n"
        "\n"
        "\n"
        "def surface(name):\n"
        '    return "denied"\n'
        "\n"
        "\n"
    )

    def _write_evidence_modules(self, entry: dict) -> None:
        """Write the test nodes this entry's receipt is allowed to cite.

        `executable_refs` are resolved on disk and in the AST, so a fixture
        that cited `tests/test_rt017_security.py::Claims.test_x` without ever
        writing it would be citing evidence that does not exist -- which is
        precisely the shortcut these contracts exist to forbid.
        """
        rt_id = entry["producer_rt"]
        rt = self._rt_slug(rt_id)
        header = self._MODULE_HEADER.format(rt=rt_id)

        claims = ["class Claims(unittest.TestCase):\n"]
        for frozen in entry["claims"]:
            claim_id = frozen["claim_id"]
            if claim_id in entry["na_permitted_claim_ids"]:
                continue
            name = claim_id.lower().replace("-", "_")
            claims.append(
                f"    def test_{name}(self):\n"
                f'        self.assertEqual(surface("{claim_id}"), "denied")\n'
                "\n"
            )
        if len(claims) > 1:
            self._write_module(f"tests/test_{rt}_security.py", header + "".join(claims))

        paths = ["class PathTests(unittest.TestCase):\n"]
        for name in ATTACK_CLASSES:
            paths.append(
                f"    def test_{name}(self):\n"
                f'        self.assertEqual(surface("{name}"), "denied")\n'
                "\n"
            )
        self._write_module(f"tests/test_{rt}_paths.py", header + "".join(paths))

        self._write_module(
            f"tests/test_{rt}_no_fs_surface.py",
            header
            + "class StaticScan(unittest.TestCase):\n"
            "    def test_no_write_surface(self):\n"
            "        self.assertEqual(surface(\"static\"), \"denied\")\n",
        )

    def _write_placeholder_module(self) -> None:
        """Refs that EXIST but prove nothing -- used by the attack tests."""
        self._write_module(
            "tests/test_security_placeholders.py",
            '"""Refs that exist on disk but assert nothing."""\n'
            "import unittest\n"
            "\n"
            "\n"
            "class Placeholder(unittest.TestCase):\n"
            '    @unittest.skip("deferred")\n'
            "    def test_skipped(self):\n"
            "        self.assertTrue(False)\n"
            "\n"
            "    def test_empty(self):\n"
            "        pass\n",
        )

    def _environment_fingerprint(self, **overrides) -> dict:
        """Observed values plus the one field that is not observable.

        `toolchain_build` is a declaration; `python_version` and `platform` are
        recomputed against the live interpreter, so a receipt cannot claim it
        passed on a machine that was never running it.
        """
        fingerprint = {**self.expected_environment, "toolchain_build": "cwk-pr001-candidate"}
        fingerprint.update(overrides)
        return fingerprint

    def _owner_tree_digest(self, entry: dict) -> str:
        subject = self.subject_commit_by_rt[entry["producer_rt"]]
        digest = security_owner_scope_tree_sha256(
            self.git, subject, entry, self.registry
        )
        self.assertIsNotNone(digest, f"fixture has no owner tree for {entry['producer_rt']}")
        return digest

    # --- builders ---------------------------------------------------------

    def _artifact(self, entry: dict) -> dict:
        rel = f"{self.registry['receipt_root']}/{entry['producer_rt']}/narrative.md"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"# {entry['producer_rt']} security narrative\n".encode("utf-8"))
        return {"path": rel, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    def _coverage(self, entry: dict) -> dict:
        rt = self._rt_slug(entry["producer_rt"])
        if entry["filesystem_policy"]["mode"] == "applicable":
            return {
                name: {
                    "applicability": "applicable",
                    "runtime_surfaces": [f"{rt}_{name}_surface"],
                    "executable_refs": [f"tests/test_{rt}_paths.py::PathTests.test_{name}"],
                    "tests_run": 3,
                }
                for name in ATTACK_CLASSES
            }
        reason_code = entry["filesystem_policy"]["permitted_reason_codes"][0]
        return {
            name: {
                "applicability": "not_applicable",
                "reason_code": reason_code,
                "reason": (
                    "The Broker core opens, creates and renames no filesystem path at all, "
                    f"so the {name} class has no surface to attack in this package."
                ),
                "static_evidence_refs": [f"tests/test_{rt}_no_fs_surface.py::StaticScan"],
            }
            for name in ATTACK_CLASSES
        }

    def _claims(self, entry: dict) -> list:
        claims = []
        for frozen in entry["claims"]:
            claim_id = frozen["claim_id"]
            rt = self._rt_slug(entry["producer_rt"])
            if claim_id in entry["na_permitted_claim_ids"]:
                claims.append(
                    {
                        "claim_id": claim_id,
                        "sg_id": frozen["sg_id"],
                        "applicability": "not_applicable",
                        "reason_code": entry["permitted_reason_codes"][0],
                        "reason": (
                            "This package has no filesystem write surface of its own; "
                            "the surface belongs to RT-021 and RT-023 by design."
                        ),
                        "static_evidence_refs": [f"tests/test_{rt}_no_fs_surface.py::StaticScan"],
                        "summary": frozen["requirement"][:200],
                    }
                )
            else:
                claims.append(
                    {
                        "claim_id": claim_id,
                        "sg_id": frozen["sg_id"],
                        "applicability": "applicable",
                        "executable_refs": [
                            f"tests/test_{rt}_security.py::Claims.test_{claim_id.lower().replace('-', '_')}"
                        ],
                        "summary": frozen["requirement"][:200],
                    }
                )
        return claims

    def _build(self, rt: str, **overrides) -> dict:
        entry = self.by_rt[rt]
        receipt = {
            "schema": self.schema["$id"],
            "receipt_kind": entry["receipt_kind"],
            "producer_rt": rt,
            "independent_security_verification_pass": True,
            "producer_phase": entry["producer_phase"],
            "status": "pass",
            "conclusion": "security_verified",
            "synthetic": False,
            "producer": f"agent-{rt.lower()}-impl",
            "verifier": f"agent-{rt.lower()}-verify",
            "consumers": [c["consumer_id"] for c in entry["consumers"]],
            "claims": self._claims(entry),
            "filesystem_coverage": self._coverage(entry),
            "tested_subject_commit": self.subject_commit_by_rt[rt],
            "owner_scope_tree_sha256": self._owner_tree_digest(entry),
            "environment_fingerprint": self._environment_fingerprint(),
            "evidence": {
                "test_command": "python3.11 -m unittest discover -s tests -p 'test_*.py'",
                "tests_run": 42,
                "tests_failed": 0,
                "tests_skipped": 0,
                "python_version": platform.python_version(),
            },
            "artifacts": [self._artifact(entry)],
            "created_at": "2026-11-01T00:00:00Z",
        }
        if entry["receipt_kind"] == "preflight-security":
            receipt["evaluator_identity_excluded"] = True
        receipt.update(overrides)
        receipt["receipt_sha256"] = self._hash(receipt)
        return receipt

    def _write(self, rt: str, receipt: dict) -> pathlib.Path:
        target = self.root / self.by_rt[rt]["receipt_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def _valid(self, rt: str, **overrides) -> bool:
        receipt = self._build(rt, **overrides)
        self._write(rt, receipt)
        return self._receipt_is_valid(self.by_rt[rt], receipt, self.root)

    def _reject(self, rt: str, **overrides) -> None:
        self.assertFalse(self._valid(rt, **overrides))

class FutureSecurityReceiptTests(SecurityFixture, unittest.TestCase):
    """Prove the SG design is reachable AND that every shortcut fails closed."""

    # --- positive ---------------------------------------------------------

    def test_every_rt_can_produce_a_fully_valid_future_receipt(self) -> None:
        """All ten owner scopes are now concrete and mechanically reachable."""
        for rt in PRODUCER_RTS:
            with self.subTest(rt=rt):
                self.assertTrue(self._valid(rt), f"{rt} cannot produce a conforming receipt")

    def test_v2_manifest_binds_the_pinned_policy_and_exact_owner_stage_tuples(self) -> None:
        policy = self._evolution_policy
        for rt in PRODUCER_RTS:
            with self.subTest(rt=rt):
                entry = self.by_rt[rt]
                snapshot = _security_scope_snapshot(
                    self.git, self.subject_commit_by_rt[rt], entry, self.registry
                )
                self.assertIsNotNone(snapshot)
                manifest = snapshot[0]
                self.assertEqual(
                    manifest["script_evolution_policy_ref"],
                    self.registry["script_evolution_policy_ref"],
                )
                self.assertEqual(
                    manifest["script_evolution_policy_sha256"],
                    self.registry["script_evolution_policy_sha256"],
                )
                expected = [
                    {
                        key: stage[key]
                        for key in sorted(
                            {"stage_index", "owner_rt", "target_path", "receipt_path", "ordinal"}
                        )
                    }
                    for stage in policy["stages"]
                    if stage["owner_rt"] == rt
                ]
                self.assertEqual(manifest["owner_evolution_stages"], expected)

    def test_full_evolution_replay_observes_real_profile_and_release_slots(self) -> None:
        state = _security_evolution_state(
            self.git, self.subject_commit_by_rt["RT-026"], self.registry
        )
        self.assertIsNotNone(state)
        self.assertEqual(state[0], frozenset(range(1, 11)))
        source = (self.root / "scripts/cwk_tenant_cli.py").read_text(encoding="utf-8")
        policy = _eg.load_policy(
            self.root,
            expected_policy_sha256=self.registry["script_evolution_policy_sha256"],
        )
        shape = _eg.verify_tenant_cli(
            self.root,
            policy,
            (
                "cwk_tenant_cmd_core",
                "cwk_tenant_cmd_binding",
                "cwk_tenant_cmd_profile",
                "cwk_tenant_cmd_release",
            ),
        )
        self.assertIn('"cwk_tenant_cmd_profile"', source)
        self.assertIn('"cwk_tenant_cmd_release"', source)
        self.assertEqual(shape.slots[-2:], ("cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"))

    def test_all_ten_receipts_satisfy_the_closed_security_family(self) -> None:
        for rt in PRODUCER_RTS:
            self._write(rt, self._build(rt))
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")

    def test_absence_of_any_single_receipt_forces_no_go(self) -> None:
        for rt in PRODUCER_RTS:
            self._write(rt, self._build(rt))
        for rt in PRODUCER_RTS:
            with self.subTest(missing=rt):
                path = self.root / self.by_rt[rt]["receipt_path"]
                body = path.read_text(encoding="utf-8")
                path.unlink()
                self.assertEqual(self._sg_state(self.by_rt[rt], self.root), "NOT_RUN")
                self.assertEqual(self._verdict(self.root), "NO_GO")
                path.write_text(body, encoding="utf-8")

    def test_a_sibling_receipt_never_discharges_another_rts_sg(self) -> None:
        """RT-024 also owns SG-03, but that cannot cover RT-020's SG-03."""
        for rt in PRODUCER_RTS:
            if rt == "RT-020":
                continue
            self._write(rt, self._build(rt))
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "NOT_RUN")
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_rt022_na_receipt_is_valid_only_because_the_registry_permits_it(self) -> None:
        self.assertTrue(self._valid("RT-022"))
        entry = self.by_rt["RT-022"]
        self.assertEqual(entry["na_permitted_claim_ids"], ["SGC-022-02"])
        self.assertEqual(entry["filesystem_policy"]["mode"], "not_applicable_permitted")

    # --- claim set --------------------------------------------------------

    def test_missing_claim_is_rejected(self) -> None:
        for rt in ("RT-017", "RT-023", "RT-026"):
            with self.subTest(rt=rt):
                claims = self._claims(self.by_rt[rt])[:-1]
                self._reject(rt, claims=claims)

    def test_extra_unowned_claim_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-020"])
        claims.append(
            {
                "claim_id": "SGC-020-09",
                "sg_id": "SG-05",
                "applicability": "applicable",
                "executable_refs": ["tests/test_rt020_security.py::Extra"],
                "summary": "an SG this package does not own",
            }
        )
        self._reject("RT-020", claims=claims)

    def test_claim_bound_to_the_wrong_sg_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-019"])
        claims[0]["sg_id"] = "SG-09"
        self._reject("RT-019", claims=claims)

    def test_duplicate_claim_id_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-021"])
        claims.append(dict(claims[0]))
        self._reject("RT-021", claims=claims)

    def test_not_applicable_claim_without_registry_permission_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-017"])
        claims[0] = {
            "claim_id": claims[0]["claim_id"],
            "sg_id": claims[0]["sg_id"],
            "applicability": "not_applicable",
            "reason_code": "in_memory_only",
            "reason": "claimed out of scope without any registry permission at all",
            "static_evidence_refs": ["tests/test_rt017_security.py::Static"],
            "summary": claims[0]["summary"],
        }
        self._reject("RT-017", claims=claims)

    def test_not_applicable_claim_without_static_evidence_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-022"])
        for claim in claims:
            if claim["applicability"] == "not_applicable":
                claim.pop("static_evidence_refs")
        self._reject("RT-022", claims=claims)

    def test_applicable_claim_without_executable_refs_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0].pop("executable_refs")
        self._reject("RT-018", claims=claims)

    def test_applicable_claim_with_an_na_reason_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["reason_code"] = "in_memory_only"
        claims[0]["reason"] = "trying to have it both ways at once here"
        self._reject("RT-018", claims=claims)

    # --- filesystem coverage ----------------------------------------------

    def test_omitting_any_one_of_the_six_attack_classes_is_rejected(self) -> None:
        for name in ATTACK_CLASSES:
            with self.subTest(omitted=name):
                coverage = self._coverage(self.by_rt["RT-025"])
                coverage.pop(name)
                self._reject("RT-025", filesystem_coverage=coverage)

    def test_symlink_component_and_leaf_must_both_be_present(self) -> None:
        """Collapsing the two into one 'symlink' answer is rejected."""
        coverage = self._coverage(self.by_rt["RT-025"])
        merged = coverage.pop("symlink_leaf")
        coverage["symlink"] = merged
        self._reject("RT-025", filesystem_coverage=coverage)

    def test_hardlink_omission_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-023"])
        coverage.pop("hardlink")
        self._reject("RT-023", filesystem_coverage=coverage)

    def test_toctou_omission_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-024"])
        coverage.pop("toctou")
        self._reject("RT-024", filesystem_coverage=coverage)

    def test_attack_class_with_zero_tests_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-017"])
        coverage["toctou"]["tests_run"] = 0
        self._reject("RT-017", filesystem_coverage=coverage)

    def test_attack_class_without_runtime_surfaces_is_rejected(self) -> None:
        """Generic prose is not a runtime surface."""
        coverage = self._coverage(self.by_rt["RT-017"])
        coverage["symlink_component"].pop("runtime_surfaces")
        self._reject("RT-017", filesystem_coverage=coverage)

    def test_attack_class_without_executable_refs_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-017"])
        coverage["symlink_leaf"].pop("executable_refs")
        self._reject("RT-017", filesystem_coverage=coverage)

    def test_attack_class_with_a_prose_only_note_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-018"])
        coverage["path_traversal"] = {
            "applicability": "applicable",
            "note": "we carefully considered path traversal and are confident",
        }
        self._reject("RT-018", filesystem_coverage=coverage)

    def test_unpermitted_na_on_an_applicable_package_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-021"])
        coverage["hardlink"] = {
            "applicability": "not_applicable",
            "reason_code": "in_memory_only",
            "reason": "asserting N/A where the registry says the family applies",
            "static_evidence_refs": ["tests/test_rt021_security.py::Static"],
        }
        self._reject("RT-021", filesystem_coverage=coverage)

    def test_mixing_applicable_and_na_classes_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-022"])
        coverage["path_traversal"] = {
            "applicability": "applicable",
            "runtime_surfaces": ["broker_cache"],
            "executable_refs": ["tests/test_rt022_security.py::Paths"],
            "tests_run": 1,
        }
        self._reject("RT-022", filesystem_coverage=coverage)

    def test_na_class_with_an_unpermitted_reason_code_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-022"])
        coverage["toctou"]["reason_code"] = "external_api_semantics_only"
        self._reject("RT-022", filesystem_coverage=coverage)

    # --- status / evidence -------------------------------------------------

    def test_zero_tests_run_is_rejected(self) -> None:
        self._reject(
            "RT-019",
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 0,
                "tests_failed": 0,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            },
        )

    def test_tests_failed_above_zero_is_rejected(self) -> None:
        self._reject(
            "RT-019",
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 10,
                "tests_failed": 2,
                "tests_skipped": 0,
                "python_version": "3.11.14",
            },
        )

    def test_more_skipped_than_run_is_rejected(self) -> None:
        self._reject(
            "RT-019",
            evidence={
                "test_command": "python3.11 -m unittest",
                "tests_run": 3,
                "tests_failed": 0,
                "tests_skipped": 4,
                "python_version": "3.11.14",
            },
        )

    def test_empty_artifacts_is_rejected(self) -> None:
        self._reject("RT-020", artifacts=[])

    def test_missing_verifier_is_rejected(self) -> None:
        receipt = self._build("RT-020")
        receipt.pop("verifier")
        receipt["receipt_sha256"] = self._hash(receipt)
        self._write("RT-020", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-020"], receipt, self.root))

    def test_verifier_equal_to_producer_is_rejected(self) -> None:
        self._reject("RT-020", producer="agent-solo", verifier="agent-solo")

    def test_pass_without_independent_verification_is_rejected(self) -> None:
        self._reject("RT-020", independent_security_verification_pass=False)

    def test_synthetic_receipt_cannot_be_security_verified(self) -> None:
        self._reject("RT-023", synthetic=True, synthetic_reason="fake_trust_store")

    def test_non_pass_status_is_not_satisfied(self) -> None:
        for status in ("not_run", "implementation_done", "fail"):
            with self.subTest(status=status):
                self._reject("RT-024", status=status)

    def test_hash_drift_is_detected(self) -> None:
        receipt = self._build("RT-018")
        receipt["created_at"] = "2026-12-01T00:00:00Z"  # body edited, hash left stale
        self._write("RT-018", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-018"], receipt, self.root))

    def test_malformed_timestamp_is_rejected(self) -> None:
        for bad in ("2026-11-01 00:00:00Z", "2026-11-01T00:00:00+08:00", "2026-11-01T00:00:00"):
            with self.subTest(created_at=bad):
                self._reject("RT-018", created_at=bad)

    def test_missing_tested_subject_commit_is_rejected(self) -> None:
        receipt = self._build("RT-018")
        receipt.pop("tested_subject_commit")
        receipt["receipt_sha256"] = self._hash(receipt)
        self._write("RT-018", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-018"], receipt, self.root))

    def test_short_subject_commit_is_rejected(self) -> None:
        self._reject("RT-018", tested_subject_commit="abc1234")

    def test_missing_environment_fingerprint_field_is_rejected(self) -> None:
        self._reject(
            "RT-018",
            environment_fingerprint={"python_version": "3.11.14", "platform": "darwin-arm64"},
        )

    def test_extra_undeclared_field_is_rejected(self) -> None:
        self._reject("RT-018", bonus_claim="trust me")

    def test_deep_forbidden_field_is_rejected(self) -> None:
        self._reject(
            "RT-018",
            environment_fingerprint={
                "python_version": "3.11.14",
                "platform": "darwin-arm64",
                "toolchain_build": "cwk",
                "secret": "leaked",
            },
        )

    # --- ownership / phase --------------------------------------------------

    def test_receipt_claiming_another_rt_is_rejected(self) -> None:
        receipt = self._build("RT-019", producer_rt="RT-020")
        self._write("RT-019", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-019"], receipt, self.root))

    def test_wrong_receipt_kind_is_rejected(self) -> None:
        self._reject("RT-019", receipt_kind="preflight-security")
        self._reject("RT-026", receipt_kind="rt-security")

    def test_wrong_producer_phase_is_rejected(self) -> None:
        self._reject("RT-019", producer_phase="preflight_after_candidate_freeze")

    def test_evaluator_phase_as_producer_is_rejected(self) -> None:
        """A receipt written by the read-only evaluator is structurally illegal."""
        for rt in ("RT-019", "RT-026"):
            with self.subTest(rt=rt):
                self._reject(rt, producer_phase="go_no_go_evaluation")

    def test_downstream_phase_as_producer_is_rejected(self) -> None:
        for phase in DOWNSTREAM_PHASES:
            with self.subTest(phase=phase):
                self._reject("RT-026", producer_phase=phase)

    def test_widened_consumer_set_is_rejected(self) -> None:
        self._reject("RT-019", consumers=["RT-026", "G6", "G7"])

    def test_narrowed_consumer_set_is_rejected(self) -> None:
        self._reject("RT-019", consumers=["G6"])

    def test_rt_security_receipt_may_not_claim_evaluator_exclusion(self) -> None:
        self._reject("RT-019", evaluator_identity_excluded=True)

    def test_preflight_receipt_must_claim_evaluator_exclusion(self) -> None:
        receipt = self._build("RT-026")
        receipt.pop("evaluator_identity_excluded")
        receipt["receipt_sha256"] = self._hash(receipt)
        self._write("RT-026", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-026"], receipt, self.root))

    def test_preflight_receipt_never_implies_rt026_final_pass(self) -> None:
        """It asserts a preflight verdict on a frozen commit, nothing later."""
        entry = self.by_rt["RT-026"]
        self.assertEqual(entry["producer_phase"], "preflight_after_candidate_freeze")
        self.assertLess(
            PHASE_ORDER.index(entry["producer_phase"]),
            PHASE_ORDER.index("rt026_independent_acceptance"),
        )
        self.assertNotIn(
            "final_acceptance", [c["consumer_phase"] for c in entry["consumers"]][:1]
        )
        note = entry["static_note"]
        self.assertIn("never that RT-026 final acceptance, G6 or G7 has occurred", note)

    # --- path attacks on the artifacts themselves ---------------------------

    def test_absolute_artifact_path_is_rejected(self) -> None:
        self._reject("RT-025", artifacts=[{"path": "/etc/passwd", "sha256": "0" * 64}])

    def test_traversal_artifact_path_is_rejected(self) -> None:
        self._reject("RT-025", artifacts=[{"path": "PR/../../etc/passwd", "sha256": "0" * 64}])

    def test_symlinked_artifact_leaf_is_rejected(self) -> None:
        real = self.root / "real.md"
        real.write_bytes(b"# real\n")
        rel = f"{self.registry['receipt_root']}/RT-025/link.md"
        link = self.root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        digest = hashlib.sha256(real.read_bytes()).hexdigest()
        self._reject("RT-025", artifacts=[{"path": rel, "sha256": digest}])

    def test_symlinked_artifact_component_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "note.md").write_bytes(b"# planted\n")
        base = self.root / self.registry["receipt_root"] / "RT-025"
        base.mkdir(parents=True, exist_ok=True)
        (base / "hop").symlink_to(outside, target_is_directory=True)
        rel = f"{self.registry['receipt_root']}/RT-025/hop/note.md"
        digest = hashlib.sha256((outside / "note.md").read_bytes()).hexdigest()
        self._reject("RT-025", artifacts=[{"path": rel, "sha256": digest}])

    def test_hardlinked_artifact_is_rejected(self) -> None:
        original = self.root / "original.md"
        original.write_bytes(b"# original\n")
        rel = f"{self.registry['receipt_root']}/RT-025/hardlink.md"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(original, target)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self._reject("RT-025", artifacts=[{"path": rel, "sha256": digest}])

    def test_special_file_artifact_is_rejected(self) -> None:
        rel = f"{self.registry['receipt_root']}/RT-025/pipe"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(target)
        self._reject("RT-025", artifacts=[{"path": rel, "sha256": "0" * 64}])

    def test_artifact_hash_drift_is_rejected(self) -> None:
        entry = self.by_rt["RT-024"]
        receipt = self._build("RT-024")
        self._write("RT-024", receipt)
        self.assertTrue(self._receipt_is_valid(entry, receipt, self.root))
        (self.root / receipt["artifacts"][0]["path"]).write_bytes(b"# tampered\n")
        self.assertFalse(self._receipt_is_valid(entry, receipt, self.root))

    def test_receipt_may_not_bind_another_security_receipt(self) -> None:
        """No receipt-to-receipt hash cycle."""
        other = self.by_rt["RT-024"]["receipt_path"]
        target = self.root / other
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{}\n")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        entry = self.by_rt["RT-025"]
        receipt = self._build("RT-025")
        receipt["artifacts"].append({"path": other, "sha256": digest})
        receipt["receipt_sha256"] = self._hash(receipt)
        self._write("RT-025", receipt)
        self.assertFalse(self._receipt_is_valid(entry, receipt, self.root))


# ---------------------------------------------------------------------------
# attacks on the bindings themselves
# ---------------------------------------------------------------------------


class SecurityBindingAttackTests(SecurityFixture, unittest.TestCase):
    """Every binding the receipt claims, attacked directly.

    The tests above prove the SHAPE of a receipt is checked. These prove the
    CONTENT is recomputed: a subject commit that never existed, a Python that
    was never running, a test node that was never written, a receipt file that
    is really a symlink, and an undeclared receipt sitting next to the declared
    ten all had to be individually shown to fail closed, because each of them
    was accepted at some point by a validator that only pattern-matched.
    """

    # --- scaffolding ------------------------------------------------------

    def _write_all(self) -> None:
        for rt in PRODUCER_RTS:
            self._write(rt, self._build(rt))

    def _sideline_commit(self) -> str:
        """A real commit that never joined the line the receipt landed on.

        Made on a detached HEAD and then abandoned, so it exists as an object
        and passes `commit_exists` while failing `is_strict_ancestor`. That is
        the exact shape of the attack: a real-looking 40-hex sha that is not
        on the reviewed history.
        """
        self._evaluation_commit(self.root)  # flush pending writes first
        git = self.git
        branch = git._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(git._git("checkout", "-q", "--detach").returncode, 0)
        target = self.root / "RT" / "RT-020" / "src" / "sideline.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('"""never merged into the reviewed line"""\n', encoding="utf-8")
        side = commit_all(git, "sideline candidate")
        self.assertEqual(git._git("checkout", "-q", "--force", branch).returncode, 0)
        self._evidence_cache.clear()
        return side

    def _evidence_only_commit(self, rt: str) -> str:
        """A commit that changed nothing but this RT's own receipt directory."""
        self.assertTrue(self._valid(rt))
        commit = self._evidence_commit(self.root, self.by_rt[rt]["receipt_path"])
        self.assertIsNotNone(commit)
        return commit

    @staticmethod
    def _registry_copy(registry: dict) -> dict:
        """A JSON-deep copy so declaration attacks never mutate the fixture."""

        return json.loads(json.dumps(registry))

    @staticmethod
    def _entry_in(registry: dict, rt: str) -> dict:
        return next(entry for entry in registry["entries"] if entry["producer_rt"] == rt)

    def _bound_verification_args(self, rt: str) -> tuple[dict, dict, str, str]:
        entry = self.by_rt[rt]
        receipt = self._build(rt)
        self._write(rt, receipt)
        self.assertTrue(self._receipt_is_valid(entry, receipt, self.root))
        evaluation = self.git.head()
        evidence = self._evidence_commit(self.root, entry["receipt_path"])
        self.assertIsNotNone(evaluation)
        self.assertIsNotNone(evidence)
        return entry, receipt, evidence, evaluation

    def _commit_test_subject(self, rt: str, rel: str, source: str, message: str) -> None:
        target = self.root / rel
        target.write_text(source, encoding="utf-8")
        self.subject_commit_by_rt[rt] = commit_all(self.git, message)
        self._evidence_cache.clear()

    # --- applicability ----------------------------------------------------

    def test_a_claim_applicability_outside_the_enum_is_rejected(self) -> None:
        """'partially' used to fall through to the applicable branch."""
        for bogus in ("partially", "APPLICABLE", "", None):
            with self.subTest(applicability=bogus):
                claims = self._claims(self.by_rt["RT-019"])
                claims[0]["applicability"] = bogus
                self._reject("RT-019", claims=claims)

    def test_an_attack_class_applicability_outside_the_enum_is_rejected(self) -> None:
        for bogus in ("partially", "not-applicable", True):
            with self.subTest(applicability=bogus):
                coverage = self._coverage(self.by_rt["RT-021"])
                coverage["hardlink"]["applicability"] = bogus
                self._reject("RT-021", filesystem_coverage=coverage)

    def test_coverage_must_match_the_registrys_own_applicability_answer(self) -> None:
        """RT-017 is declared `applicable`; an NA-shaped coverage cannot stand."""
        entry = self.by_rt["RT-017"]
        self.assertEqual(entry["path_attack_applicability"], "applicable")
        coverage = {
            name: {
                "applicability": "not_applicable",
                "reason_code": "in_memory_only",
                "reason": "claiming the whole family away without registry permission",
                "static_evidence_refs": ["tests/test_rt017_no_fs_surface.py::StaticScan"],
            }
            for name in ATTACK_CLASSES
        }
        self._reject("RT-017", filesystem_coverage=coverage)

    def test_rt022_may_not_flip_its_family_to_applicable(self) -> None:
        """The NA answer is frozen in the registry, not chosen by the receipt."""
        entry = self.by_rt["RT-022"]
        self.assertEqual(entry["path_attack_applicability"], "not_applicable")
        coverage = {
            name: {
                "applicability": "applicable",
                "runtime_surfaces": [f"rt_022_{name}_surface"],
                "executable_refs": [f"tests/test_rt022_paths.py::PathTests.test_{name}"],
                "tests_run": 2,
            }
            for name in ATTACK_CLASSES
        }
        self._reject("RT-022", filesystem_coverage=coverage)

    # --- environment ------------------------------------------------------

    def test_an_empty_environment_fingerprint_is_rejected(self) -> None:
        self._reject("RT-018", environment_fingerprint={})

    def test_a_fabricated_python_version_is_rejected(self) -> None:
        """Format-only checking accepted any 3.x.y string that never ran."""
        bogus = "3.99.0" if platform.python_version() != "3.99.0" else "3.98.0"
        self._reject("RT-018", environment_fingerprint=self._environment_fingerprint(
            python_version=bogus
        ))

    def test_a_fabricated_platform_is_rejected(self) -> None:
        bogus = "plan9" if sys.platform != "plan9" else "haiku"
        self._reject("RT-018", environment_fingerprint=self._environment_fingerprint(
            platform=bogus
        ))

    def test_a_receipt_from_another_machine_cannot_certify_this_one(self) -> None:
        self._reject("RT-024", environment_fingerprint=self._environment_fingerprint(
            python_version="3.9.6", platform="linux"
        ))

    # --- subject commit ---------------------------------------------------

    def test_a_subject_commit_that_never_existed_is_rejected(self) -> None:
        """40 hex characters is a format, not a commit."""
        self._reject("RT-018", tested_subject_commit="b" * 40)

    def test_a_nonancestor_subject_commit_is_rejected(self) -> None:
        side = self._sideline_commit()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", side))
        self.assertTrue(self.git.commit_exists(side))
        self._reject("RT-020", tested_subject_commit=side)

    def test_an_evidence_only_commit_cannot_be_its_own_subject(self) -> None:
        """A commit that only wrote the receipt never tested any owner code."""
        evidence = self._evidence_only_commit("RT-019")
        self._reject("RT-019", tested_subject_commit=evidence)

    def test_a_commit_touching_a_lookalike_package_is_rejected(self) -> None:
        """`RT/RT-023-evil/` must never satisfy `RT/RT-023/`."""
        self._evaluation_commit(self.root)
        target = self.root / "RT" / "RT-023-evil" / "src" / "paths.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('"""a neighbouring package, not the owner"""\n', encoding="utf-8")
        impostor = commit_all(self.git, "lookalike sibling package")
        self._evidence_cache.clear()
        self._reject("RT-023", tested_subject_commit=impostor)

    def test_owner_scope_tree_drift_is_rejected(self) -> None:
        """Ancestry plus package is not enough; the exact content is pinned."""
        entry = self.by_rt["RT-021"]
        honest = self._owner_tree_digest(entry)
        drifted = hashlib.sha256(honest.encode("utf-8")).hexdigest()
        self.assertNotEqual(honest, drifted)
        self._reject("RT-021", owner_scope_tree_sha256=drifted)

    def test_missing_exact_file_is_not_satisfied_by_a_lookalike(self) -> None:
        target = self.root / "scripts/cwk_missing_guard.py.evil"
        target.write_text('"""lookalike, not the declared file"""\n', encoding="utf-8")
        candidate = commit_all(self.git, "exact-file lookalike")
        mutated = self._registry_copy(self.registry)
        entry = self._entry_in(mutated, "RT-024")
        entry["owner_code_path_prefixes"].append("scripts/cwk_missing_guard.py")
        self.assertTrue(security_registry_owner_semantics_ok(mutated))
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, mutated)
        )

    def test_missing_directory_is_not_satisfied_by_a_prefix_lookalike(self) -> None:
        lookalike = self.root / (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/expected-evil/"
            "scope.json"
        )
        lookalike.parent.mkdir(parents=True, exist_ok=True)
        lookalike.write_text("{}\n", encoding="utf-8")
        candidate = commit_all(self.git, "directory-selector lookalike")
        mutated = self._registry_copy(self.registry)
        entry = self._entry_in(mutated, "RT-024")
        entry["owner_code_path_prefixes"].append(
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/expected/"
        )
        self.assertTrue(security_registry_owner_semantics_ok(mutated))
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, mutated)
        )

    def test_test_prefix_rejects_subdirectories_and_backup_suffixes(self) -> None:
        entry = self.by_rt["RT-020"]
        for path in list((self.root / "tests").glob("test_rt020_*.py")):
            path.unlink()
        backup = self.root / "tests/test_rt020_security.py.bak"
        backup.write_text("not a Python evidence module\n", encoding="utf-8")
        nested = self.root / "tests/nested/test_rt020_paths.py"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("import unittest\n", encoding="utf-8")
        candidate = commit_all(self.git, "test-prefix lookalikes only")
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, self.registry)
        )

    def test_test_prefix_rejects_backup_even_while_required_tests_remain_valid(self) -> None:
        entry = self.by_rt["RT-020"]
        (self.root / "tests/test_rt020_reviewed.py.bak").write_text(
            "not executable evidence\n", encoding="utf-8"
        )
        candidate = commit_all(self.git, "tracked backup beside valid owner tests")
        self.assertTrue((self.root / "tests/test_rt020_security.py").is_file())
        self.assertTrue((self.root / "tests/test_rt020_paths.py").is_file())
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, self.registry)
        )

    def test_selected_test_symlink_is_not_filtered_out_when_valid_tests_remain(self) -> None:
        entry = self.by_rt["RT-020"]
        target = self.root / "tests/test_rt020_no_fs_surface.py"
        target.unlink()
        target.symlink_to("test_rt020_security.py")
        candidate = commit_all(self.git, "selected test symlink")
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, self.registry)
        )

    def test_selected_contract_gitlink_is_not_filtered_out(self) -> None:
        entry = self.by_rt["RT-020"]
        rel = (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt020/"
            "schemas/nested-repository"
        )
        target_commit = self.git.head()
        self.assertIsNotNone(target_commit)
        proc = self.git._git(
            "update-index", "--add", "--cacheinfo", "160000", target_commit, rel
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.git._git(
            "commit", "-q", "--no-verify", "-m", "selected contract gitlink"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        candidate = self.git.head()
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, self.registry)
        )

    def test_extra_specs_or_tasks_member_breaks_the_exact_authority_closure(self) -> None:
        entry = self.by_rt["RT-024"]
        for rel in (
            "RT/RT-024/specs/草稿.md",
            "RT/RT-024/tasks/notes.md",
        ):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("undeclared authority sibling\n", encoding="utf-8")
        candidate = commit_all(self.git, "extra authority members")
        self.assertIsNone(
            security_owner_scope_tree_sha256(self.git, candidate, entry, self.registry)
        )

    def test_undeclared_managed_script_is_rejected_by_global_inventory(self) -> None:
        target = self.root / "scripts/cwk_unassigned_future.py"
        target.write_text('"""no frozen owner"""\n', encoding="utf-8")
        candidate = commit_all(self.git, "undeclared managed script")
        self.assertIsNone(
            security_owner_scope_tree_sha256(
                self.git, candidate, self.by_rt["RT-024"], self.registry
            )
        )

    def test_legacy_inventory_content_or_mode_drift_is_rejected(self) -> None:
        legacy = self.registry["managed_script_inventory"]["legacy_frozen_files"][0]
        target = self.root / legacy["path"]
        target.write_bytes(target.read_bytes() + b"\n# legacy drift\n")
        target.chmod(0o755 if legacy["mode"] == "100644" else 0o644)
        candidate = commit_all(self.git, "legacy inventory drift")
        self.assertIsNone(
            security_owner_scope_tree_sha256(
                self.git, candidate, self.by_rt["RT-024"], self.registry
            )
        )

    def test_selector_manifest_change_changes_the_digest(self) -> None:
        extra = self.root / "scripts/rt024_extra.py"
        extra.write_text('"""extra selected blob"""\n', encoding="utf-8")
        candidate = commit_all(self.git, "additional exact selector subject")
        original_entry = self.by_rt["RT-024"]
        original = security_owner_scope_tree_sha256(
            self.git, candidate, original_entry, self.registry
        )
        mutated = self._registry_copy(self.registry)
        mutated_entry = self._entry_in(mutated, "RT-024")
        mutated_entry["owner_code_path_prefixes"].append(
            "scripts/rt024_extra.py"
        )
        self.assertTrue(security_registry_owner_semantics_ok(mutated))
        expanded = security_owner_scope_tree_sha256(
            self.git, candidate, mutated_entry, mutated
        )
        self.assertIsNotNone(original)
        self.assertIsNotNone(expanded)
        self.assertNotEqual(original, expanded)

    def test_registry_rejects_nested_and_evidence_overlapping_selectors(self) -> None:
        nested = self._registry_copy(self.registry)
        nested_entry = self._entry_in(nested, "RT-024")
        nested_entry["owner_code_path_prefixes"].append(
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/schemas/nested/"
        )
        self.assertFalse(security_registry_owner_semantics_ok(nested))

        evidence = self._registry_copy(self.registry)
        evidence_entry = self._entry_in(evidence, "RT-024")
        evidence_entry["owner_code_path_prefixes"].append("RT/RT-024/")
        self.assertFalse(security_registry_owner_semantics_ok(evidence))

        shared_abi = self._registry_copy(self.registry)
        shared_entry = self._entry_in(shared_abi, "RT-024")
        shared_entry["owner_code_path_prefixes"].append(
            "scripts/cwk_pilot_admission_api.py"
        )
        self.assertFalse(security_registry_owner_semantics_ok(shared_abi))

    def test_post_acceptance_script_modification_is_rejected(self) -> None:
        target = self.root / "scripts/cwk_audit.py"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# drift after RT-024 acceptance\n")
        commit_all(self.git, "post-acceptance script drift")
        self._evidence_cache.clear()
        self.assertFalse(self._valid("RT-024"))

    def test_post_acceptance_contract_addition_is_rejected(self) -> None:
        target = self.root / (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt020/schemas/late.json"
        )
        target.write_text('{"late":true}\n', encoding="utf-8")
        commit_all(self.git, "post-acceptance contract addition")
        self._evidence_cache.clear()
        self.assertFalse(self._valid("RT-020"))

    def test_post_acceptance_required_test_deletion_is_rejected(self) -> None:
        (self.root / "tests/test_rt025_paths.py").unlink()
        commit_all(self.git, "post-acceptance required test deletion")
        self._evidence_cache.clear()
        self.assertFalse(self._valid("RT-025"))

    def test_post_acceptance_specification_modification_is_rejected(self) -> None:
        target = self.root / "RT/RT-018/specs/需求契约.md"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\nlate incompatible requirement\n")
        commit_all(self.git, "post-acceptance specification drift")
        self._evidence_cache.clear()
        self.assertFalse(self._valid("RT-018"))

    def test_dirty_selected_worktree_bytes_are_rejected_without_moving_evaluation_commit(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        unrelated = self.root / "later-unrelated.txt"
        unrelated.write_text("outside every security owner scope\n", encoding="utf-8")
        later_head = commit_all(self.git, "clean descendant outside owner scope")
        self.assertNotEqual(later_head, evaluation)
        self.assertTrue(self.git.is_ancestor(evaluation, later_head))
        self.assertFalse(worktree_is_dirty(self.git))
        self.assertTrue(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )
        target = self.root / "scripts/cwk_audit.py"
        target.write_bytes(target.read_bytes() + b"\n# dirty after evaluation freeze\n")
        self.assertTrue(worktree_is_dirty(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_assume_unchanged_cannot_hide_selected_worktree_drift(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        rel = "scripts/cwk_audit.py"
        self.assertEqual(self.git._git("update-index", "--assume-unchanged", rel).returncode, 0)
        (self.root / rel).write_bytes((self.root / rel).read_bytes() + b"\n# hidden drift\n")
        self.assertTrue(index_has_hidden_entries(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_skip_worktree_cannot_hide_selected_worktree_drift(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        rel = "scripts/cwk_audit.py"
        self.assertEqual(self.git._git("update-index", "--skip-worktree", rel).returncode, 0)
        (self.root / rel).write_bytes((self.root / rel).read_bytes() + b"\n# skipped drift\n")
        self.assertTrue(index_has_hidden_entries(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_ignored_untracked_member_inside_selected_directory_is_rejected(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        rel = (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/"
            "schemas/ignored.json"
        )
        exclude = self.root / ".git/info/exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("/" + rel + "\n")
        target = self.root / rel
        target.write_text("{}\n", encoding="utf-8")
        self.assertFalse(worktree_is_dirty(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_ignored_symlink_inside_selected_directory_is_rejected(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        rel = (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/"
            "schemas/ignored-link.json"
        )
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as handle:
            handle.write("/" + rel + "\n")
        (self.root / rel).symlink_to("scope.json")
        self.assertFalse(worktree_is_dirty(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_ignored_special_file_inside_selected_directory_is_rejected(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        rel = (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt024/"
            "schemas/ignored-pipe"
        )
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as handle:
            handle.write("/" + rel + "\n")
        os.mkfifo(self.root / rel)
        self.assertFalse(worktree_is_dirty(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_hardlinked_selected_file_is_rejected_even_when_extra_name_is_ignored(self) -> None:
        entry, receipt, evidence, evaluation = self._bound_verification_args("RT-024")
        source_rel = "scripts/cwk_audit.py"
        extra_rel = "scripts/cwk_audit.hidden-link"
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as handle:
            handle.write("/" + extra_rel + "\n")
        os.link(self.root / source_rel, self.root / extra_rel)
        self.assertFalse(worktree_is_dirty(self.git))
        self.assertFalse(
            verify_security_subject_commit(
                self.git,
                receipt["tested_subject_commit"],
                evidence,
                evaluation_commit=evaluation,
                entry=entry,
                registry=self.registry,
                declared_tree_sha256=receipt["owner_scope_tree_sha256"],
            )
        )

    def test_neutral_shared_abi_cannot_satisfy_owner_touch(self) -> None:
        target = self.root / "scripts/cwk_pilot_admission_api.py"
        target.chmod(0o755)
        subject = commit_all(self.git, "shared ABI mode-only touch")
        entry = self.by_rt["RT-018"]
        digest = security_owner_scope_tree_sha256(
            self.git, subject, entry, self.registry
        )
        self.assertIsNotNone(digest)
        self._reject(
            "RT-018",
            tested_subject_commit=subject,
            owner_scope_tree_sha256=digest,
        )

    def test_unreceipted_shared_evolution_edit_is_rejected(self) -> None:
        target = self.root / "scripts/cwk_tenant_cli.py"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# unreceipted edit after stage 7\n")
        commit_all(self.git, "unreceipted tenant CLI edit")
        self._evidence_cache.clear()
        self.assertFalse(self._valid("RT-019"))

    def test_bootstrap_receipt_or_companion_drift_breaks_evolution_closure(self) -> None:
        for stage_index in (9, 10):
            with self.subTest(stage_index=stage_index):
                stage = next(
                    item
                    for item in self._evolution_policy["stages"]
                    if item["stage_index"] == stage_index
                )
                receipt_path = self.root / stage["receipt_path"]
                receipt_raw = receipt_path.read_bytes()
                receipt_path.unlink()
                missing = commit_all(
                    self.git, f"remove completed bootstrap stage {stage_index}"
                )
                self.assertIsNone(
                    _security_evolution_state(self.git, missing, self.registry)
                )
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_bytes(receipt_raw)
                restored = commit_all(
                    self.git, f"restore completed bootstrap stage {stage_index}"
                )
                self.assertIsNotNone(
                    _security_evolution_state(self.git, restored, self.registry)
                )

        companion = self._evolution_policy["companion_immutable_paths"][0]
        target = self.root / companion["target_path"]
        target.write_bytes(target.read_bytes() + b"\n# companion drift\n")
        candidate = commit_all(self.git, "companion immutable drift")
        self.assertIsNone(_security_evolution_state(self.git, candidate, self.registry))

    def test_tenant_cli_slot_reorder_breaks_full_ast_slot_verification(self) -> None:
        target = self.root / "scripts/cwk_tenant_cli.py"
        source = target.read_text(encoding="utf-8")
        source = source.replace(
            '    "cwk_tenant_cmd_profile",\n    "cwk_tenant_cmd_release",',
            '    "cwk_tenant_cmd_release",\n    "cwk_tenant_cmd_profile",',
            1,
        )
        target.write_text(source, encoding="utf-8")
        candidate = commit_all(self.git, "tenant CLI slot reorder")
        self.assertIsNone(_security_evolution_state(self.git, candidate, self.registry))

    def test_a_missing_owner_scope_tree_binding_is_rejected(self) -> None:
        receipt = self._build("RT-021")
        receipt.pop("owner_scope_tree_sha256")
        receipt["receipt_sha256"] = self._hash(receipt)
        self._write("RT-021", receipt)
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-021"], receipt, self.root))

    # --- executable refs --------------------------------------------------

    def test_a_ref_naming_a_file_that_does_not_exist_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["executable_refs"] = ["tests/test_rt018_imaginary.py::Claims.test_nothing"]
        self._reject("RT-018", claims=claims)

    def test_an_rt_may_not_borrow_another_rts_executable_ref(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["executable_refs"] = [
            "tests/test_rt019_security.py::Claims.test_sgc_019_01"
        ]
        self._reject("RT-018", claims=claims)

    def test_a_ref_naming_a_class_that_does_not_exist_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["executable_refs"] = ["tests/test_rt018_security.py::Ghost.test_nothing"]
        self._reject("RT-018", claims=claims)

    def test_a_ref_naming_a_skipped_test_is_rejected(self) -> None:
        """A skipped placeholder is not evidence that anything was exercised."""
        self.assertFalse(
            self._evidence_ref_ok(
                self.root,
                "tests/test_security_placeholders.py::Placeholder",
                executable=False,
                entry=self.by_rt["RT-018"],
                subject_commit=self.subject_commit_by_rt["RT-018"],
            )
        )
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["executable_refs"] = [
            "tests/test_security_placeholders.py::Placeholder.test_skipped"
        ]
        self._reject("RT-018", claims=claims)

    def test_a_ref_naming_a_test_that_asserts_nothing_is_rejected(self) -> None:
        claims = self._claims(self.by_rt["RT-018"])
        claims[0]["executable_refs"] = [
            "tests/test_security_placeholders.py::Placeholder.test_empty"
        ]
        self._reject("RT-018", claims=claims)

    def test_canonical_executable_ref_rejects_noncanonical_unittest_shapes(self) -> None:
        rt = "RT-018"
        entry = self.by_rt[rt]
        rel = "tests/test_rt018_security.py"
        good = (self.root / rel).read_text(encoding="utf-8")
        claim_id = entry["claims"][0]["claim_id"]
        method = f"test_{claim_id.lower().replace('-', '_')}"
        method_line = f"    def {method}(self):"
        cases = {
            "non_testcase_base": (
                good.replace("class Claims(unittest.TestCase):", "class Claims(object):", 1),
                method,
            ),
            "duplicate_method_overwrite": (
                good
                + f"\n    def {method}(self):\n"
                + "        pass\n",
                method,
            ),
            "aliased_skip": (
                good.replace("import unittest\n", "import unittest\nfrom unittest import skip as alias_skip\n", 1)
                .replace(method_line, f'    @alias_skip("disabled")\n{method_line}', 1),
                method,
            ),
            "aliased_expected_failure": (
                good.replace(
                    "import unittest\n",
                    "import unittest\nfrom unittest import expectedFailure as alias_expected_failure\n",
                    1,
                ).replace(method_line, f"    @alias_expected_failure\n{method_line}", 1),
                method,
            ),
            "class_level_skip": (
                good.replace(
                    "class Claims(unittest.TestCase):",
                    '@unittest.skip("disabled class")\nclass Claims(unittest.TestCase):',
                    1,
                ),
                method,
            ),
            "inherited_skip": (
                good.replace(
                    "class Claims(unittest.TestCase):",
                    "class SkippedBase(unittest.TestCase):\n"
                    "    __unittest_skip__ = True\n\n\n"
                    "class Claims(SkippedBase):",
                    1,
                ),
                method,
            ),
            "non_test_helper": (
                good.replace(method_line, "    def evidence_helper(self):", 1),
                "evidence_helper",
            ),
        }
        for name, (source, cited_method) in cases.items():
            with self.subTest(shape=name):
                self._commit_test_subject(rt, rel, source, f"noncanonical evidence {name}")
                claims = self._claims(entry)
                claims[0]["executable_refs"] = [
                    f"{rel}::Claims.{cited_method}"
                ]
                receipt = self._build(rt, claims=claims)
                self._write(rt, receipt)
                self.assertFalse(self._receipt_is_valid(entry, receipt, self.root))

    def test_an_attack_class_ref_that_does_not_exist_is_rejected(self) -> None:
        coverage = self._coverage(self.by_rt["RT-024"])
        coverage["toctou"]["executable_refs"] = ["tests/test_rt024_missing.py::PathTests.test_x"]
        self._reject("RT-024", filesystem_coverage=coverage)

    def test_a_non_python_ref_is_rejected(self) -> None:
        """A Markdown narrative is not an executable test node."""
        rel = self._artifact(self.by_rt["RT-024"])["path"]
        self.assertIsNotNone(_sr.try_read_bytes(self.root, rel))
        coverage = self._coverage(self.by_rt["RT-024"])
        coverage["toctou"]["executable_refs"] = [rel]
        self._reject("RT-024", filesystem_coverage=coverage)

    def test_a_static_evidence_ref_must_still_exist(self) -> None:
        claims = self._claims(self.by_rt["RT-022"])
        for claim in claims:
            if claim["applicability"] == "not_applicable":
                claim["static_evidence_refs"] = ["tests/test_rt022_never_written.py::StaticScan"]
        self._reject("RT-022", claims=claims)

    # --- receipt presence and closure over the root -----------------------

    def test_an_undeclared_extra_receipt_is_a_hard_no_go(self) -> None:
        """SG_SATISFIED used to be returned with RT-999 sitting on disk."""
        self._write_all()
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")
        planted = self.root / self.registry["receipt_root"] / "RT-999" / "receipt.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(json.dumps(self._build("RT-020")), encoding="utf-8")
        self.assertFalse(self._receipt_root_is_closed(self.root))
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_a_nested_extra_receipt_is_a_hard_no_go(self) -> None:
        self._write_all()
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")
        nested = self.root / self.by_rt["RT-020"]["receipt_path"]
        planted = nested.parent / "backup" / "receipt.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(nested.read_bytes())
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_a_hidden_entry_under_the_receipt_root_is_a_hard_no_go(self) -> None:
        self._write_all()
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")
        base = self.root / self.by_rt["RT-021"]["receipt_path"]
        (base.parent / ".receipt.json.swp").write_bytes(b"{}\n")
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_a_symlink_anywhere_under_the_receipt_root_is_a_hard_no_go(self) -> None:
        self._write_all()
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")
        base = self.root / self.by_rt["RT-021"]["receipt_path"]
        (base.parent / "shortcut.md").symlink_to(base)
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_a_special_file_under_the_receipt_root_is_a_hard_no_go(self) -> None:
        self._write_all()
        self.assertEqual(self._verdict(self.root), "SG_SATISFIED")
        base = self.root / self.by_rt["RT-021"]["receipt_path"]
        os.mkfifo(base.parent / "channel")
        self.assertEqual(self._verdict(self.root), "NO_GO")

    def test_junk_json_beside_a_declared_receipt_is_a_hard_no_go(self) -> None:
        self._write_all()
        base = self.root / self.by_rt["RT-025"]["receipt_path"]
        (base.parent / "notes.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self._verdict(self.root), "NO_GO")

    # --- attacks on the receipt FILE itself -------------------------------

    def test_a_receipt_that_is_really_a_symlink_is_rejected(self) -> None:
        """The receipt used to be read with plain pathlib and followed."""
        receipt = self._build("RT-020")
        planted = self.root / "elsewhere" / "receipt.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        target = self.root / self.by_rt["RT-020"]["receipt_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(planted)
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "INVALID")
        self.assertFalse(self._receipt_is_valid(self.by_rt["RT-020"], receipt, self.root))

    def test_a_receipt_under_a_symlinked_parent_is_rejected(self) -> None:
        """A symlinked COMPONENT, not merely a symlinked leaf.

        The receipt bytes are untouched and still hash correctly; only the way
        the evaluator reaches them changed. A `resolve()`-then-`read()` check
        cannot tell the difference, which is why the whole descent is O_NOFOLLOW.
        """
        self._write("RT-020", self._build("RT-020"))
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "PASS")
        base = self.root / self.registry["receipt_root"]
        stash = self.root / "stashed-receipt-root"
        os.rename(base, stash)
        base.symlink_to(stash, target_is_directory=True)
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "INVALID")
        self.assertFalse(self._receipt_root_is_closed(self.root))

    def test_a_hardlinked_receipt_is_rejected(self) -> None:
        """A second name for the same inode can be swapped underneath us."""
        receipt = self._build("RT-020")
        original = self.root / "elsewhere" / "receipt.json"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        target = self.root / self.by_rt["RT-020"]["receipt_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(original, target)
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "INVALID")

    def test_a_fifo_in_place_of_a_receipt_is_rejected(self) -> None:
        target = self.root / self.by_rt["RT-020"]["receipt_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(target)
        self.assertEqual(self._sg_state(self.by_rt["RT-020"], self.root), "INVALID")

    def test_a_case_aliased_artifact_spelling_is_rejected(self) -> None:
        """Declaring `NARRATIVE.MD` while `narrative.md` is on disk.

        On a case-insensitive filesystem `open()` resolves the declared
        spelling straight to the real inode, so a plain read would hash the
        file and agree. The exact-name membership test against the parent's
        real listing is what refuses it -- and on a case-SENSITIVE filesystem
        the same declaration is simply a missing file. Rejected either way,
        which is why this test needs no platform skip.
        """
        artifact = self._artifact(self.by_rt["RT-025"])
        base, _, leaf = artifact["path"].rpartition("/")
        aliased = f"{base}/{leaf.upper()}"
        self.assertNotEqual(aliased, artifact["path"])
        self._reject("RT-025", artifacts=[{"path": aliased, "sha256": artifact["sha256"]}])

    def test_a_non_nfc_unicode_artifact_spelling_is_rejected(self) -> None:
        """NFD and NFC spellings of the same name are not interchangeable."""
        base_rel = f"{self.registry['receipt_root']}/RT-025"
        base = self.root / base_rel
        base.mkdir(parents=True, exist_ok=True)
        nfc = unicodedata.normalize("NFC", "narrativé.md")
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        (base / nfc).write_bytes(b"# nfc\n")
        digest = hashlib.sha256(b"# nfc\n").hexdigest()
        self.assertTrue(self._file_hash_matches(self.root, f"{base_rel}/{nfc}", digest))
        self._reject("RT-025", artifacts=[{"path": f"{base_rel}/{nfd}", "sha256": digest}])

    def test_two_entries_that_alias_each_other_make_a_directory_unusable(self) -> None:
        """The guard itself, checked without depending on the host filesystem.

        No filesystem here can hold both spellings at once -- APFS folds them
        together -- so the collision is presented to the guard directly. If a
        bound directory ever did contain both, we could not say which one a
        hash covers, and refusing the directory is the only honest answer.
        """
        with self.assertRaises(_sr.SafeReadError):
            _sr._reject_alias_collisions(["receipt.json", "Receipt.json"], label="d")
        with self.assertRaises(_sr.SafeReadError):
            _sr._reject_alias_collisions(
                [unicodedata.normalize("NFC", "é.md"), unicodedata.normalize("NFD", "é.md")],
                label="d",
            )
        _sr._reject_alias_collisions(["receipt.json", "narrative.md"], label="d")

    def test_a_receipt_rewritten_under_the_evaluator_is_rejected(self) -> None:
        """TOCTOU: the object judged must be the object still on disk."""
        entry = self.by_rt["RT-024"]
        receipt = self._build("RT-024")
        self._write("RT-024", receipt)
        self.assertTrue(self._receipt_is_valid(entry, receipt, self.root))
        tampered = dict(receipt)
        tampered["created_at"] = "2026-12-25T00:00:00Z"
        tampered["receipt_sha256"] = self._hash(tampered)
        self._write("RT-024", tampered)
        self.assertFalse(self._receipt_is_valid(entry, receipt, self.root))

    def test_a_parent_directory_swapped_for_a_symlink_is_rejected(self) -> None:
        """The receipt validated a moment ago must not survive a re-parenting."""
        entry = self.by_rt["RT-024"]
        receipt = self._build("RT-024")
        self._write("RT-024", receipt)
        self.assertTrue(self._receipt_is_valid(entry, receipt, self.root))
        base = (self.root / entry["receipt_path"]).parent
        stash = self.root / "stash-RT-024"
        os.rename(base, stash)
        base.symlink_to(stash, target_is_directory=True)
        self.assertFalse(self._receipt_is_valid(entry, receipt, self.root))
        self.assertEqual(self._sg_state(entry, self.root), "INVALID")

    # --- evaluator identity -----------------------------------------------

    def test_the_evaluator_may_not_author_the_preflight_it_consumes(self) -> None:
        """Self-declared exclusion used to be enough; it is recomputed now."""
        evaluator = self.registry["go_no_go_evaluator_identity"]
        self._reject("RT-026", producer=evaluator, evaluator_identity_excluded=True)
        self._reject("RT-026", verifier=evaluator, evaluator_identity_excluded=True)

    def test_the_exclusion_declaration_must_agree_with_the_recomputation(self) -> None:
        self._reject("RT-026", evaluator_identity_excluded=False)

    def test_no_rt_receipt_may_be_authored_by_the_evaluator_either(self) -> None:
        evaluator = self.registry["go_no_go_evaluator_identity"]
        self._reject("RT-023", producer=evaluator)
        self._reject("RT-023", verifier=evaluator)

    def test_the_evaluator_identity_is_injected_not_read_from_the_receipt(self) -> None:
        """Proof the check is not satisfiable by editing the receipt."""
        self.assertEqual(
            self._evaluator_identity(), self.registry["go_no_go_evaluator_identity"]
        )
        self.assertNotIn(
            "go_no_go_evaluator_identity", set(self.schema["properties"])
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
