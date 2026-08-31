"""Attack tests for the PR-001 Wave-0 script-evolution guard.

Almost every test here runs against a **synthetic repository** built in a
``tempfile.TemporaryDirectory``: the guard was written so that no function
derives its root from ``__file__``, which is what makes the whole attack
surface reachable without ever touching the real worktree.  ``setUpModule`` /
``tearDownModule`` hash the real genesis files plus the three central
artefacts and assert they are unchanged, so a buggy attack test cannot
silently mutate the repository it is guarding.

Together with ``tests/pr001_script_evolution_guard.py`` this file is the
human-review trust root: nothing pins it (two files cannot pin each other),
so its diff must be read by a human.  See the SECURITY POSTURE section of the
helper's docstring — this is tamper *evidence*, not tamper *proofing*.

Python 3.11+, pure stdlib ``unittest``, no pytest, no third-party deps.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REAL_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pr001_script_evolution_guard as EG  # noqa: E402

# The guard helper's own bytes are pinned here and (independently) in
# tests/test_rt016_schemas.py.  A later RT must not refresh either pin.
_GUARD_HELPER_SHA256 = "01abe94109d21ffbbfbf84aa8672058455237099d4625ebb5c5577986dabd32a"


def _load_genesis_table() -> dict[str, str]:
    """Import the RT-016 genesis table without collecting its tests."""

    spec = importlib.util.spec_from_file_location(
        "_pr001_rt016_genesis_view", _HERE / "test_rt016_schemas.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module._FROZEN_RT011_015_BASELINE_SHAS)


_REAL_GENESIS = _load_genesis_table()

_CENTRAL_PATHS = (
    EG.POLICY_REL,
    EG.POLICY_SCHEMA_REL,
    EG.RECEIPT_SCHEMA_REL,
)

_REAL_SENTINEL: dict[str, str] = {}


def _snapshot_real_repo() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for rel in sorted(_REAL_GENESIS):
        snapshot[rel] = EG.file_sha256(_REAL_ROOT, rel)
    for rel in _CENTRAL_PATHS:
        snapshot[rel] = EG.file_sha256(_REAL_ROOT, rel)
    return snapshot


def setUpModule() -> None:
    _REAL_SENTINEL.update(_snapshot_real_repo())


def tearDownModule() -> None:
    after = _snapshot_real_repo()
    if after != _REAL_SENTINEL:
        changed = sorted(k for k in after if after[k] != _REAL_SENTINEL.get(k))
        raise AssertionError(
            "attack tests mutated the real worktree, which they must never do: " f"{changed}"
        )


# ---------------------------------------------------------------------------
# Synthetic repository fixture
# ---------------------------------------------------------------------------

_IMMUTABLE_FILLERS = tuple(f"scripts/cwk_frozen_{i:02d}.py" for i in range(1, 18))
_COMPANION_PATHS = ("scripts/cwk_tenant_cli_api.py", "scripts/cwk_tenant_cmd_core.py")

_TENANT_CLI_DOCSTRING = "Synthetic tenant CLI fixture."


def synth_tenant_cli(
    slots: tuple[str, ...] | list[str],
    *,
    symbol: str = "FROZEN_PROVIDER_SLOTS",
    annotation: str = "tuple[str, ...]",
    docstring: str = _TENANT_CLI_DOCSTRING,
    header_comment: str = "# module header comment (outside the slot span)",
    span_comment: str = "    # future slots land here",
    error_class: str = "ProviderLoadError",
    guard_expression: str = "spec is None or spec.loader is None",
    trailer_comment: str = "# trailing comment (outside the slot span)",
    use_annassign: bool = True,
    extra_body: str = "",
) -> str:
    """Build a synthetic ``cwk_tenant_cli.py`` with controllable drift knobs."""

    lines = [
        f'"""{docstring}"""',
        "",
        "from __future__ import annotations",
        "",
        "import importlib.util",
        "",
        header_comment,
    ]
    assign_op = f"{symbol}: {annotation} = (" if use_annassign else f"{symbol} = ("
    lines.append(assign_op)
    for slot in slots:
        lines.append(f'    "{slot}",')
    if span_comment:
        lines.append(span_comment)
    lines.append(")")
    lines.extend(
        [
            "",
            "",
            f"class {error_class}(RuntimeError):",
            '    """Raised when a provider cannot be loaded."""',
            "",
            "",
            "def load_provider(name: str):",
            "    spec = importlib.util.find_spec(name)",
            f"    if {guard_expression}:",
            f'        raise {error_class}(f"cannot load {{name}}")',
            "    return spec",
            "",
            "",
            trailer_comment,
            "def main() -> int:",
            "    return len(" + symbol + ")",
        ]
    )
    if extra_body:
        lines.extend(["", "", extra_body])
    return "\n".join(lines) + "\n"


class SyntheticRepo:
    """A throwaway repo root that the guard treats exactly like the real one.

    The policy is a structural clone of the real ``policy_v1.json`` — same 9
    paths, same 10 stages, same owners and slots — with only the pinned SHAs
    swapped for the synthetic files' SHAs.  That keeps every semantic rule
    under test identical to production while letting a test mutate anything.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.policy_raw: dict[str, Any] = json.loads(
            (_REAL_ROOT / EG.POLICY_REL).read_text(encoding="utf-8")
        )
        self.tenant_cli_path: str = self.policy_raw["tenant_cli"]["target_path"]
        self.baseline_slots: tuple[str, ...] = tuple(
            self.policy_raw["tenant_cli"]["baseline_slots"]
        )
        self.slots: list[str] = list(self.baseline_slots)
        self._tips: dict[str, str] = {}
        self._prev_bytes: dict[str, bytes] = {}
        self._acceptance: dict[str, set[str]] = {}
        self.genesis: dict[str, str] = {}
        self.policy_sha: str = ""
        self.ast_fingerprint: str = ""
        self.comment_fingerprint: str = ""
        self._build()

    # -- construction ----------------------------------------------------

    def write(self, rel: str, data: bytes | str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def sha(self, rel: str) -> str:
        return hashlib.sha256(self.read(rel)).hexdigest()

    def _build(self) -> None:
        for rel in _CENTRAL_PATHS[1:]:  # both schemas, verbatim from the repo
            self.write(rel, (_REAL_ROOT / rel).read_bytes())

        evolvable = [entry["target_path"] for entry in self.policy_raw["evolvable_paths"]]
        for rel in evolvable:
            if rel == self.tenant_cli_path:
                self.write(rel, synth_tenant_cli(self.baseline_slots))
            else:
                self.write(rel, f'"""Synthetic {rel}."""\n\nVERSION = 1\n')
        for rel in _IMMUTABLE_FILLERS:
            self.write(rel, f'"""Synthetic immutable {rel}."""\n')
        for rel in _COMPANION_PATHS:
            self.write(rel, f'"""Synthetic companion {rel}."""\n\nAPI_VERSION = 1\n')

        for entry in self.policy_raw["evolvable_paths"]:
            entry["genesis_sha256"] = self.sha(entry["target_path"])
        for entry in self.policy_raw["companion_immutable_paths"]:
            entry["sha256"] = self.sha(entry["target_path"])

        self.genesis = {rel: self.sha(rel) for rel in list(evolvable) + list(_IMMUTABLE_FILLERS)}
        assert len(self.genesis) == EG.GENESIS_ENTRY_COUNT, len(self.genesis)
        self._tips = dict(self.genesis)
        self.policy_raw["genesis_manifest_sha256"] = EG.C.canonical_sha256(dict(self.genesis))
        self.write_policy()
        self.refresh_fingerprints()

    def write_policy(self, raw: Mapping[str, Any] | None = None) -> None:
        payload = self.policy_raw if raw is None else raw
        data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        self.write(EG.POLICY_REL, data)
        self.policy_sha = hashlib.sha256(data).hexdigest()

    def refresh_fingerprints(self) -> None:
        spec = self.policy_raw["tenant_cli"]
        shape = EG.tenant_cli_shape(
            self.read(self.tenant_cli_path).decode("utf-8"),
            slot_symbol=spec["slot_symbol"],
            slot_name_pattern=spec["slot_name_pattern"],
            max_span_lines=spec["max_slot_span_lines"],
        )
        self.ast_fingerprint = shape.ast_fingerprint
        self.comment_fingerprint = shape.comment_fingerprint

    # -- guard entry points ----------------------------------------------

    def load(self, **overrides: Any) -> EG.Policy:
        kwargs: dict[str, Any] = {
            "expected_policy_sha256": self.policy_sha,
            "expected_policy_schema_sha256": EG.PINNED_POLICY_SCHEMA_SHA256,
            "expected_receipt_schema_sha256": EG.PINNED_RECEIPT_SCHEMA_SHA256,
        }
        kwargs.update(overrides)
        return EG.load_policy(self.root, **kwargs)

    def verify(self, **overrides: Any) -> EG.Report:
        policy = overrides.pop("policy", None) or self.load()
        kwargs: dict[str, Any] = {
            "genesis": dict(self.genesis),
            "policy": policy,
            "tenant_cli_ast_fingerprint": self.ast_fingerprint,
            "tenant_cli_comment_fingerprint": self.comment_fingerprint,
        }
        kwargs.update(overrides)
        return EG.verify_evolution(self.root, **kwargs)

    # -- receipts ---------------------------------------------------------

    def stage(self, index: int) -> Mapping[str, Any]:
        for stage in self.policy_raw["stages"]:
            if stage["stage_index"] == index:
                return stage
        raise KeyError(index)

    def _evolve_target(self, stage: Mapping[str, Any]) -> str:
        rel = stage["target_path"]
        if rel == self.tenant_cli_path:
            self.slots.append(stage["adds_provider_slot"])
            self.write(rel, synth_tenant_cli(self.slots))
        else:
            text = self.read(rel).decode("utf-8")
            self.write(rel, text + f"\n# evolved by stage {stage['stage_index']}\n")
        return self.sha(rel)

    def _write_migration_note(self, stage: Mapping[str, Any]) -> str:
        rel = stage["migration_note_path"]
        basename = stage["target_path"].rsplit("/", 1)[-1]
        body = (
            f"# Migration note — stage {stage['stage_index']}\n\n"
            f"Owner: {stage['owner_rt']}\n"
            f"Target: {basename}\n\n"
            "This synthetic note exists so the guard's migration-note gate has real "
            "bytes to hash.  It deliberately exceeds the minimum length so that the "
            "'note too short' rule is exercised separately rather than by accident.\n"
        )
        self.write(rel, body)
        return self.sha(rel)

    def _write_acceptance_test(self, stage: Mapping[str, Any]) -> str:
        digits = stage["owner_rt"][-2:]
        rel = f"tests/test_rt0{digits}_evolution.py"
        method = f"test_stage_{stage['stage_index']:02d}"
        self._acceptance.setdefault(rel, set()).add(method)
        lines = ["import unittest", "", "", "class EvolutionTests(unittest.TestCase):"]
        for name in sorted(self._acceptance[rel]):
            lines.extend([f"    def {name}(self):", "        self.assertTrue(True)", ""])
        self.write(rel, "\n".join(lines) + "\n")
        return f"{rel}::EvolutionTests::{method}"

    def build_receipt(self, index: int, **overrides: Any) -> dict[str, Any]:
        stage = self.stage(index)
        rel = stage["target_path"]
        from_sha = self._tips[rel]
        to_sha = self._evolve_target(stage)
        note_sha = self._write_migration_note(stage)
        ref = self._write_acceptance_test(stage)
        previous = self._prev_bytes.get(rel)
        if previous is None:
            link = EG.genesis_link(
                domain=self.policy_raw["genesis_link_domain"],
                policy_sha256=self.policy_sha,
                target_path=rel,
            )
        else:
            link = hashlib.sha256(previous).hexdigest()
        receipt = {
            "schema": "cwk.pr001.script_evolution_receipt.v1",
            "policy_id": self.policy_raw["policy_id"],
            "policy_sha256": self.policy_sha,
            "stage_index": stage["stage_index"],
            "owner_rt": stage["owner_rt"],
            "target_path": rel,
            "ordinal": stage["ordinal"],
            "adds_provider_slot": stage["adds_provider_slot"],
            "from_sha256": from_sha,
            "to_sha256": to_sha,
            "previous_receipt_sha256": link,
            "migration_note_path": stage["migration_note_path"],
            "migration_note_sha256": note_sha,
            "acceptance_test_refs": [ref],
            "recorded_at": "2026-08-21T00:00:00Z",
        }
        receipt.update(overrides)
        return receipt

    def add_receipt(self, index: int, *, raw: bytes | None = None, **overrides: Any) -> bytes:
        stage = self.stage(index)
        receipt = self.build_receipt(index, **overrides)
        data = raw if raw is not None else _dump(receipt)
        self.write(stage["receipt_path"], data)
        self._tips[stage["target_path"]] = receipt["to_sha256"]
        self._prev_bytes[stage["target_path"]] = data
        return data


def _dump(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# State-independent repository invariants
# ---------------------------------------------------------------------------
#
# The original Wave-0 baseline had zero receipts; RT-012/013/017/019/021/022/026
# may append only their policy-declared receipts.
# Any assertion here that hard-codes "0 receipts" or "exactly the two baseline
# slots" would force a downstream RT to edit this file — and editing this file
# is precisely what the guard forbids, because it is the human-review trust
# root that nothing else pins.  So every expectation below is *derived* from
# the policy plus the receipts actually on disk: true at 0 receipts, still
# true at 10, without a single line changing.


def receipts_on_disk(root: Path, policy: EG.Policy) -> list[str]:
    """Policy-declared receipt paths that currently exist, in stage order."""

    return [
        stage["receipt_path"]
        for stage in policy.stages
        if EG.read_checked_bytes(root, stage["receipt_path"], missing_ok=True) is not None
    ]


def expected_slots_for(policy: EG.Policy, present: Sequence[str]) -> tuple[str, ...]:
    """Baseline slots plus one slot per landed tenant-CLI stage, in ordinal order."""

    slots = list(policy.tenant_cli["baseline_slots"])
    for stage in policy.stages_for_path(policy.tenant_cli["target_path"]):
        if stage["receipt_path"] in present:
            slots.append(stage["adds_provider_slot"])
    return tuple(slots)


def assert_repo_invariants(
    case: unittest.TestCase,
    root: Path,
    genesis: Mapping[str, str],
    policy: EG.Policy,
    report: EG.Report,
) -> None:
    """Assert what must hold for *any* legal receipt state of a repo."""

    present = receipts_on_disk(root, policy)

    # 1. The report counts exactly the receipts that are on disk.
    case.assertEqual(report.receipt_count, len(present))

    # 2. Shape of the pinned surface: 9 evolvable + 17 permanently immutable.
    case.assertEqual(len(policy.evolvable), 9)
    case.assertEqual(len(report.tips), len(policy.evolvable))
    case.assertEqual(report.immutable_count, len(genesis) - len(policy.evolvable))
    case.assertEqual(len(genesis), EG.GENESIS_ENTRY_COUNT)

    # 3. Every receipt on disk is one the policy predeclared.
    declared = {stage["receipt_path"] for stage in policy.stages}
    case.assertTrue(set(present) <= declared)

    # 4. Per-path prefix closure: ordinals present are 1..k, never gapped.
    for rel in sorted(policy.evolvable):
        landed = [s for s in policy.stages_for_path(rel) if s["receipt_path"] in present]
        case.assertEqual(
            [s["ordinal"] for s in landed],
            list(range(1, len(landed) + 1)),
            f"{rel}: receipts must form a closed prefix",
        )
        # 5. Each tip is the worktree's current SHA, and equals genesis only
        #    while the path has no receipt.
        case.assertEqual(report.tips[rel], EG.file_sha256(root, rel), rel)
        if not landed:
            case.assertEqual(report.tips[rel], genesis[rel], rel)

    # 6. Immutable paths never move off genesis, whatever the chain does.
    for rel in sorted(set(genesis) - set(policy.evolvable)):
        case.assertEqual(EG.file_sha256(root, rel), genesis[rel], rel)

    # 7. Slots grow only by the stages that actually landed.
    case.assertEqual(report.tenant_cli_slots, expected_slots_for(policy, present))
    baseline = tuple(policy.tenant_cli["baseline_slots"])
    case.assertEqual(report.tenant_cli_slots[: len(baseline)], baseline)


class GuardTestCase(unittest.TestCase):
    """Base class: a fresh synthetic repo per test, provably not the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.assertNotEqual(root, _REAL_ROOT)
        self.assertNotIn(_REAL_ROOT, root.parents)
        self.assertNotIn(root, _REAL_ROOT.parents)
        self.root = root
        self.repo = SyntheticRepo(root)

    def assertGuardFails(self, needle: str, callable_obj, *args: Any, **kwargs: Any) -> str:
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            callable_obj(*args, **kwargs)
        message = str(ctx.exception)
        self.assertIn(needle, message)
        return message


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class SafePathTests(GuardTestCase):
    def test_safe_relpath_absolute_path_rejected(self):
        self.assertGuardFails("not absolute", EG.safe_relpath, "/etc/passwd")

    def test_safe_relpath_parent_traversal_rejected(self):
        self.assertGuardFails("traversal", EG.safe_relpath, "scripts/../../etc/passwd")

    def test_safe_relpath_single_dot_component_rejected(self):
        self.assertGuardFails("traversal", EG.safe_relpath, "scripts/./cwk_wiki_query.py")

    def test_safe_relpath_backslash_rejected(self):
        self.assertGuardFails("backslash", EG.safe_relpath, "scripts\\cwk_wiki_query.py")

    def test_safe_relpath_nul_byte_rejected(self):
        self.assertGuardFails("control character", EG.safe_relpath, "scripts/cwk\x00.py")

    def test_safe_relpath_non_nfc_rejected(self):
        decomposed = unicodedata.normalize("NFD", "scripts/café.py")
        self.assertGuardFails("NFC", EG.safe_relpath, decomposed)

    def test_safe_relpath_empty_component_rejected(self):
        self.assertGuardFails("empty component", EG.safe_relpath, "scripts//cwk_wiki_query.py")

    def test_safe_relpath_non_string_rejected(self):
        self.assertGuardFails("must be a string", EG.safe_relpath, 17)

    def test_safe_relpath_home_expansion_rejected(self):
        self.assertGuardFails("not absolute", EG.safe_relpath, "~/secrets.json")

    def test_safe_relpath_accepts_a_plain_repo_path(self):
        self.assertEqual(
            EG.safe_relpath("scripts/cwk_wiki_query.py"), ("scripts", "cwk_wiki_query.py")
        )

    def test_read_checked_bytes_symlink_leaf_rejected(self):
        (self.root / "scripts" / "cwk_alias.py").symlink_to(self.root / "scripts" / "cwk_frozen_01.py")
        self.assertGuardFails(
            "is a symlink", EG.read_checked_bytes, self.root, "scripts/cwk_alias.py"
        )

    def test_read_checked_bytes_symlink_component_rejected(self):
        (self.root / "linkdir").symlink_to(self.root / "scripts", target_is_directory=True)
        self.assertGuardFails(
            "is a symlink", EG.read_checked_bytes, self.root, "linkdir/cwk_frozen_01.py"
        )

    def test_read_checked_bytes_symlink_escaping_the_root_rejected(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name)
        (outside / "loot.py").write_text("SECRET = 1\n", encoding="utf-8")
        (self.root / "scripts" / "cwk_escape.py").symlink_to(outside / "loot.py")
        self.assertGuardFails(
            "is a symlink", EG.read_checked_bytes, self.root, "scripts/cwk_escape.py"
        )

    @unittest.skipUnless(hasattr(os, "link"), "platform cannot create hard links")
    def test_read_checked_bytes_hardlink_rejected(self):
        try:
            os.link(self.root / "scripts" / "cwk_frozen_01.py", self.root / "scripts" / "cwk_hard.py")
        except (OSError, NotImplementedError):
            self.skipTest("filesystem does not support hard links")
        self.assertGuardFails("hard links", EG.read_checked_bytes, self.root, "scripts/cwk_hard.py")

    def test_read_checked_bytes_case_alias_rejected(self):
        probe = self.root / "scripts" / "CWK_FROZEN_01.PY"
        if not probe.exists():
            self.skipTest("filesystem is case-sensitive; no aliasing to defeat")
        self.assertGuardFails(
            "aliasing", EG.read_checked_bytes, self.root, "scripts/CWK_FROZEN_01.PY"
        )

    def test_read_checked_bytes_directory_rejected(self):
        self.assertGuardFails("not a regular file", EG.read_checked_bytes, self.root, "scripts")

    def test_read_checked_bytes_oversize_rejected(self):
        self.repo.write("scripts/cwk_big.py", b"x" * 4096)
        self.assertGuardFails(
            "larger than",
            EG.read_checked_bytes,
            self.root,
            "scripts/cwk_big.py",
            max_bytes=1024,
        )

    def test_read_checked_bytes_missing_ok_returns_none(self):
        self.assertIsNone(
            EG.read_checked_bytes(self.root, "scripts/cwk_absent.py", missing_ok=True)
        )

    def test_read_checked_bytes_missing_required_rejected(self):
        self.assertGuardFails("is missing", EG.read_checked_bytes, self.root, "scripts/cwk_absent.py")


# ---------------------------------------------------------------------------
# B2: the read window itself
# ---------------------------------------------------------------------------


class ReaderRaceTests(GuardTestCase):
    """Deterministic TOCTOU tests — no threads, no sleeps, no timing.

    Each race is reproduced by patching the exact syscall the reader is about
    to make and mutating the filesystem *inside* that call, which pins the
    interleaving instead of hoping for it.  The previous string-path reader
    (``Path.read_bytes`` after a separate ``lstat``) passed every one of these;
    the dir-fd reader must fail all of them closed.
    """

    TARGET = "scripts/cwk_frozen_01.py"

    def _stat_hook(self, trigger: str, action) -> Any:
        """Patch ``os.stat`` so *action* runs once, inside the matching call.

        The call still returns the *pre-mutation* stat result, which is
        precisely the stale value a TOCTOU attacker relies on.
        """

        real_stat = os.stat
        fired: list[str] = []

        def fake_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            result = real_stat(path, *args, **kwargs)
            if not fired and path == trigger and kwargs.get("dir_fd") is not None:
                fired.append(trigger)
                action()
            return result

        return mock.patch("os.stat", side_effect=fake_stat), fired

    def _read_hook(self, action) -> Any:
        """Patch ``os.read`` so *action* runs once, after the first chunk."""

        real_read = os.read
        fired: list[int] = []

        def fake_read(fd: int, length: int) -> bytes:
            chunk = real_read(fd, length)
            if not fired:
                fired.append(fd)
                action()
            return chunk

        return mock.patch("os.read", side_effect=fake_read), fired

    def test_parent_directory_swapped_between_stat_and_open_rejected(self):
        """Rename ``scripts/`` away and drop an impostor in, mid-traversal."""

        evil = self.root / "evil"
        evil.mkdir()
        (evil / "cwk_frozen_01.py").write_text('"""impostor."""\n', encoding="utf-8")
        honest_ino = (self.root / "scripts").stat().st_ino

        def swap() -> None:
            (self.root / "scripts").rename(self.root / "scripts_real")
            evil.rename(self.root / "scripts")

        patcher, fired = self._stat_hook("scripts", swap)
        with patcher:
            with self.assertRaises(EG.ScriptEvolutionError) as ctx:
                EG.read_checked_bytes(self.root, self.TARGET)
        self.assertEqual(fired, ["scripts"], "the race never fired; the test proves nothing")
        self.assertNotEqual(honest_ino, (self.root / "scripts").stat().st_ino)
        self.assertIn("was swapped between stat and open (TOCTOU)", str(ctx.exception))

    def test_leaf_file_swapped_between_stat_and_open_rejected(self):
        impostor = self.root / "scripts" / "cwk_impostor.py"
        impostor.write_text('"""impostor."""\n', encoding="utf-8")

        def swap() -> None:
            os.replace(impostor, self.root / "scripts" / "cwk_frozen_01.py")

        patcher, fired = self._stat_hook("cwk_frozen_01.py", swap)
        with patcher:
            with self.assertRaises(EG.ScriptEvolutionError) as ctx:
                EG.read_checked_bytes(self.root, self.TARGET)
        self.assertEqual(fired, ["cwk_frozen_01.py"])
        self.assertIn("file was swapped between stat and open (TOCTOU)", str(ctx.exception))

    @unittest.skipUnless(hasattr(os, "link"), "platform cannot create hard links")
    def test_hard_link_created_after_the_open_rejected(self):
        """``st_nlink`` is checked at open *and* after the last read."""

        target = self.root / "scripts" / "cwk_frozen_01.py"
        self.assertEqual(target.stat().st_nlink, 1)

        def link() -> None:
            try:
                os.link(target, self.root / "scripts" / "cwk_late_link.py")
            except (OSError, NotImplementedError):  # pragma: no cover - fs dependent
                pass

        patcher, fired = self._read_hook(link)
        with patcher:
            with self.assertRaises(EG.ScriptEvolutionError) as ctx:
                EG.read_checked_bytes(self.root, self.TARGET)
        self.assertEqual(len(fired), 1)
        if target.stat().st_nlink == 1:  # pragma: no cover - fs dependent
            self.skipTest("filesystem does not support hard links")
        self.assertIn("st_nlink: 1 -> 2", str(ctx.exception))

    def test_file_rewritten_during_the_read_rejected(self):
        target = self.root / "scripts" / "cwk_frozen_01.py"
        original_ino = target.stat().st_ino

        def rewrite() -> None:
            with open(target, "ab") as handle:
                handle.write(b"# appended mid-read\n")

        patcher, fired = self._read_hook(rewrite)
        with patcher:
            with self.assertRaises(EG.ScriptEvolutionError) as ctx:
                EG.read_checked_bytes(self.root, self.TARGET)
        self.assertEqual(len(fired), 1)
        self.assertEqual(original_ino, target.stat().st_ino, "same inode: a true in-place rewrite")
        message = str(ctx.exception)
        self.assertIn("file changed while it was being read", message)
        self.assertIn("st_size", message)

    def test_stable_file_reads_cleanly_under_the_same_hooks(self):
        """Control: the hooks above are what fail, not the instrumentation."""

        patcher, fired = self._read_hook(lambda: None)
        with patcher:
            data = EG.read_checked_bytes(self.root, self.TARGET)
        self.assertEqual(len(fired), 1)
        self.assertEqual(data, (self.root / "scripts" / "cwk_frozen_01.py").read_bytes())

    def test_assert_stat_unchanged_detects_every_invariant_field(self):
        self.assertEqual(
            EG._STAT_INVARIANTS,
            (
                "st_dev",
                "st_ino",
                "st_nlink",
                "st_size",
                "st_mode",
                "st_mtime_ns",
                "st_ctime_ns",
            ),
        )
        one = os.stat(self.root / "scripts" / "cwk_frozen_01.py")
        two = os.stat(self.root / "scripts" / "cwk_frozen_02.py")
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG._assert_stat_unchanged(one, two, label="probe")
        self.assertIn("probe: file changed while it was being read", str(ctx.exception))

    def test_assert_stat_unchanged_accepts_a_stable_file(self):
        path = self.root / "scripts" / "cwk_frozen_01.py"
        EG._assert_stat_unchanged(os.stat(path), os.stat(path), label="probe")

    def test_reader_refuses_to_run_without_dir_fd_support(self):
        """Fail closed, never fall back to a string-path read."""

        with mock.patch.object(EG, "_DIR_FD_SUPPORTED", False):
            self.assertGuardFails(
                "does not support openat()", EG.read_checked_bytes, self.root, self.TARGET
            )
            self.assertGuardFails(
                "Refusing to verify rather than verifying weakly",
                EG.read_checked_bytes,
                self.root,
                self.TARGET,
                missing_ok=True,
            )

    def test_dir_fd_support_is_present_on_this_platform(self):
        self.assertTrue(
            EG._DIR_FD_SUPPORTED,
            "this platform cannot run the guard; os.lstat is deliberately NOT used "
            "because it is absent from os.supports_dir_fd",
        )
        self.assertIn(os.open, os.supports_dir_fd)
        self.assertIn(os.stat, os.supports_dir_fd)
        self.assertIn(os.stat, os.supports_follow_symlinks)
        self.assertIn(os.listdir, os.supports_fd)

    def test_leaf_is_opened_relative_to_the_parent_fd_not_by_path(self):
        """The leaf ``open`` must pass ``dir_fd``; a bare path re-traverses."""

        real_open = os.open
        seen: list[dict[str, Any]] = []

        def fake_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            seen.append({"path": path, "flags": flags, "dir_fd": kwargs.get("dir_fd")})
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=fake_open):
            EG.read_checked_bytes(self.root, self.TARGET)

        self.assertEqual(seen[0]["dir_fd"], None, "only the root is opened by path")
        self.assertEqual(seen[0]["path"], str(self.root))
        for call in seen[1:]:
            self.assertIsNotNone(call["dir_fd"], call)
            self.assertNotIn("/", str(call["path"]))
            self.assertTrue(call["flags"] & EG._O_NOFOLLOW, "O_NOFOLLOW missing")
        self.assertTrue(seen[1]["flags"] & EG._O_DIRECTORY, "parent must be O_DIRECTORY")
        self.assertFalse(seen[-1]["flags"] & EG._O_DIRECTORY, "leaf must not be O_DIRECTORY")

    def test_case_alias_defence_survives_the_dir_fd_rewrite(self):
        """macOS/APFS exact-case check, re-asserted against the new reader."""

        probe = self.root / "scripts" / "CWK_FROZEN_01.PY"
        if not probe.exists():
            self.skipTest("filesystem is case-sensitive; no aliasing to defeat")
        message = self.assertGuardFails(
            "aliasing", EG.read_checked_bytes, self.root, "scripts/CWK_FROZEN_01.PY"
        )
        self.assertIn("cwk_frozen_01.py", message)

    def test_unicode_alias_defence_survives_the_dir_fd_rewrite(self):
        self.repo.write("scripts/cwk_café.py", 'X = 1\n')
        decomposed = unicodedata.normalize("NFD", "scripts/cwk_café.py")
        self.assertGuardFails("NFC", EG.read_checked_bytes, self.root, decomposed)


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


class StrictJsonTests(unittest.TestCase):
    def assertJsonRejected(self, needle: str, data: bytes) -> None:
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.strict_json_bytes(data, label="probe")
        self.assertIn(needle, str(ctx.exception))

    def test_strict_json_accepts_a_canonical_object(self):
        self.assertEqual(EG.strict_json_bytes(b'{"a": 1}', label="probe"), {"a": 1})

    def test_strict_json_duplicate_key_rejected(self):
        self.assertJsonRejected("duplicate", b'{"a": 1, "a": 2}')

    def test_strict_json_bom_rejected(self):
        self.assertJsonRejected("BOM", b"\xef\xbb\xbf" + b'{"a": 1}')

    def test_strict_json_trailing_data_rejected(self):
        self.assertJsonRejected("invalid JSON", b'{"a": 1} {"b": 2}')

    def test_strict_json_nan_rejected(self):
        self.assertJsonRejected("floating-point", b'{"a": NaN}')

    def test_strict_json_infinity_rejected(self):
        self.assertJsonRejected("floating-point", b'{"a": Infinity}')

    def test_strict_json_plain_float_rejected(self):
        self.assertJsonRejected("floating-point", b'{"a": 1.5}')

    def test_strict_json_non_object_root_rejected(self):
        self.assertJsonRejected("must be an object", b"[1, 2, 3]")

    def test_strict_json_invalid_utf8_rejected(self):
        self.assertJsonRejected("not valid UTF-8", b'{"a": "\xff\xfe"}')

    def test_strict_json_non_nfc_string_rejected(self):
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertJsonRejected("not NFC", json.dumps({"a": decomposed}).encode("utf-8"))

    def test_strict_json_non_nfc_key_rejected(self):
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertJsonRejected("not NFC", json.dumps({decomposed: "x"}).encode("utf-8"))

    def test_strict_json_control_character_in_string_rejected(self):
        self.assertJsonRejected("control character", b'{"a": "line\\u0007bell"}')

    def test_strict_json_deep_nesting_rejected(self):
        payload: Any = "leaf"
        for _ in range(EG.MAX_JSON_DEPTH + 4):
            payload = {"n": payload}
        self.assertJsonRejected("nested deeper", json.dumps(payload).encode("utf-8"))

    def test_strict_json_embedded_bom_rejected(self):
        self.assertJsonRejected("byte-order mark", '{"a": "x﻿y"}'.encode("utf-8"))


# ---------------------------------------------------------------------------
# Central pins and policy structure
# ---------------------------------------------------------------------------


class PolicyPinTests(GuardTestCase):
    def test_fixture_policy_loads(self):
        policy = self.repo.load()
        self.assertEqual(policy.sha256, self.repo.policy_sha)
        self.assertEqual(len(policy.stages), 10)

    def test_policy_sha_pin_mismatch_rejected(self):
        self.assertGuardFails("central pin drift: policy_v1.json", self.repo.load,
                              expected_policy_sha256="0" * 64)

    def test_policy_schema_pin_mismatch_rejected(self):
        self.assertGuardFails("central pin drift: policy_v1.schema.json", self.repo.load,
                              expected_policy_schema_sha256="0" * 64)

    def test_receipt_schema_pin_mismatch_rejected(self):
        self.assertGuardFails("central pin drift: receipt_v1.schema.json", self.repo.load,
                              expected_receipt_schema_sha256="0" * 64)

    def test_policy_edited_after_pinning_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][0]["owner_rt"] = "RT-026"
        self.repo.write(EG.POLICY_REL, _dump(raw))
        self.assertGuardFails("central pin drift: policy_v1.json", self.repo.load)

    def test_policy_extra_field_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["sneaky_escape_hatch"] = True
        self.repo.write_policy(raw)
        self.assertGuardFails("schema violation", self.repo.load,
                              expected_policy_sha256=self.repo.policy_sha)

    def test_policy_with_nine_stages_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"] = raw["stages"][:9]
        self.repo.write_policy(raw)
        self.assertGuardFails("schema violation", self.repo.load)

    def test_policy_with_tenth_evolvable_path_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["evolvable_paths"].append(
            {
                "target_path": "scripts/cwk_frozen_01.py",
                "genesis_sha256": self.repo.sha("scripts/cwk_frozen_01.py"),
                "owner_rts": ["RT-017"],
                "max_ordinal": 1,
            }
        )
        self.repo.write_policy(raw)
        self.assertGuardFails("schema violation", self.repo.load)

    def test_policy_receipt_path_not_owned_by_stage_owner_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][0]["receipt_path"] = (
            "RT/RT-026/receipts/script-evolution/stage-01-cwk-access-ledger-ord1.json"
        )
        self.repo.write_policy(raw)
        self.assertGuardFails("is not owned by RT-017", self.repo.load)

    def test_policy_stage_owner_not_an_owner_of_the_path_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        for entry in raw["evolvable_paths"]:
            if entry["target_path"] == "scripts/cwk_access_ledger.py":
                entry["owner_rts"] = ["RT-021"]
        self.repo.write_policy(raw)
        self.assertGuardFails("is not an owner of", self.repo.load)

    def test_policy_stage_index_out_of_order_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][0]["stage_index"] = 2
        raw["stages"][1]["stage_index"] = 1
        self.repo.write_policy(raw)
        self.assertGuardFails("stage_index must be", self.repo.load)

    def test_policy_duplicate_provider_slot_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][6]["adds_provider_slot"] = raw["stages"][2]["adds_provider_slot"]
        self.repo.write_policy(raw)
        self.assertGuardFails("duplicate adds_provider_slot", self.repo.load)

    def test_policy_stage_readding_a_baseline_slot_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][2]["adds_provider_slot"] = raw["tenant_cli"]["baseline_slots"][0]
        self.repo.write_policy(raw)
        self.assertGuardFails("re-adds a baseline provider slot", self.repo.load)

    def test_policy_non_tenant_cli_stage_adding_a_slot_rejected(self):
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["stages"][0]["adds_provider_slot"] = "cwk_tenant_cmd_sneak"
        self.repo.write_policy(raw)
        self.assertGuardFails("must not add a provider slot", self.repo.load)

    def test_genesis_manifest_mismatch_rejected(self):
        """A batch 'refresh' of unrelated baseline SHAs must fail closed."""

        genesis = dict(self.repo.genesis)
        genesis["scripts/cwk_frozen_05.py"] = "b" * 64
        self.assertGuardFails("genesis manifest drift", self.repo.verify, genesis=genesis)

    def test_genesis_entry_count_mismatch_rejected(self):
        genesis = dict(self.repo.genesis)
        genesis.pop("scripts/cwk_frozen_05.py")
        self.assertGuardFails("exactly 26 entries", self.repo.verify, genesis=genesis)

    def test_genesis_missing_an_evolvable_path_rejected(self):
        genesis = dict(self.repo.genesis)
        genesis.pop("scripts/cwk_wiki_query.py")
        genesis["scripts/cwk_frozen_20.py"] = "c" * 64
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["genesis_manifest_sha256"] = EG.C.canonical_sha256(dict(genesis))
        self.repo.write_policy(raw)
        self.assertGuardFails(
            "not present in the genesis table", self.repo.verify, genesis=genesis
        )


# ---------------------------------------------------------------------------
# B3: companion_immutable_paths is an exact set, not a suggestion
# ---------------------------------------------------------------------------


class CompanionImmutableTests(GuardTestCase):
    """The two CommandProviderV1 ABI files are outside the RT-016 genesis table.

    Nothing else pins them, so a policy edit that drops, duplicates or
    substitutes an entry silently unguards the ABI the tenant CLI loads
    through.  ``REQUIRED_COMPANION_PATHS`` makes the set exact.
    """

    def _rewrite_companions(self, entries: list[dict[str, Any]]) -> None:
        raw = copy.deepcopy(self.repo.policy_raw)
        raw["companion_immutable_paths"] = entries
        self.repo.write_policy(raw)

    def _entries(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.repo.policy_raw["companion_immutable_paths"])

    def test_required_companion_paths_are_exactly_the_two_abi_files(self):
        self.assertEqual(
            EG.REQUIRED_COMPANION_PATHS,
            ("scripts/cwk_tenant_cli_api.py", "scripts/cwk_tenant_cmd_core.py"),
        )

    def test_real_policy_pins_each_companion_exactly_once(self):
        policy = EG.load_policy(_REAL_ROOT)
        declared = [e["target_path"] for e in policy.raw["companion_immutable_paths"]]
        self.assertEqual(sorted(declared), sorted(EG.REQUIRED_COMPANION_PATHS))
        self.assertEqual(len(declared), len(set(declared)))
        for rel in EG.REQUIRED_COMPANION_PATHS:
            self.assertEqual(declared.count(rel), 1)

    def test_companions_are_absent_from_the_genesis_table(self):
        """This is *why* they need their own pin."""

        for rel in EG.REQUIRED_COMPANION_PATHS:
            self.assertNotIn(rel, _REAL_GENESIS)
            self.assertTrue((_REAL_ROOT / rel).is_file(), rel)

    def test_duplicate_companion_entry_rejected(self):
        """Both slots pointing at the same file leaves the other unpinned."""

        for keep, drop in ((0, 1), (1, 0)):
            with self.subTest(duplicated=keep):
                entries = self._entries()
                entries[drop] = copy.deepcopy(entries[keep])
                self._rewrite_companions(entries)
                self.assertGuardFails(
                    "duplicate target_path in companion_immutable_paths", self.repo.load
                )

    def test_dropped_companion_rejected(self):
        """The schema fixes the array length; the semantics fix its contents."""

        for index in (0, 1):
            with self.subTest(dropped=index):
                entries = self._entries()
                entries.pop(index)
                self._rewrite_companions(entries)
                self.assertGuardFails(
                    "$.companion_immutable_paths: array shorter than 2", self.repo.load
                )

    def test_substituted_companion_rejected(self):
        """Swap one ABI file for a harmless decoy, keeping the count at two."""

        for index in (0, 1):
            with self.subTest(replaced=index):
                entries = self._entries()
                self.repo.write("scripts/cwk_decoy.py", "DECOY = 1\n")
                entries[index] = {
                    "target_path": "scripts/cwk_decoy.py",
                    "sha256": self.repo.sha("scripts/cwk_decoy.py"),
                    "reason": entries[index]["reason"],
                }
                self._rewrite_companions(entries)
                message = self.assertGuardFails("must pin exactly", self.repo.load)
                self.assertIn("cwk_decoy.py", message)

    def test_extra_third_companion_rejected(self):
        entries = self._entries()
        self.repo.write("scripts/cwk_decoy.py", "DECOY = 1\n")
        entries.append(
            {
                "target_path": "scripts/cwk_decoy.py",
                "sha256": self.repo.sha("scripts/cwk_decoy.py"),
                "reason": "not an ABI file",
            }
        )
        self._rewrite_companions(entries)
        self.assertGuardFails("$.companion_immutable_paths: array longer than 2", self.repo.load)

    def test_companion_that_is_also_evolvable_rejected(self):
        entries = self._entries()
        entries[0] = {
            "target_path": self.repo.tenant_cli_path,
            "sha256": self.repo.sha(self.repo.tenant_cli_path),
            "reason": entries[0]["reason"],
        }
        self._rewrite_companions(entries)
        self.assertGuardFails(
            "cannot be both evolvable and companion-immutable", self.repo.load
        )

    def test_each_companion_is_checked_for_drift_independently(self):
        self.repo.verify()
        for rel in _COMPANION_PATHS:
            with self.subTest(tampered=rel):
                pristine = self.repo.read(rel)
                self.repo.write(rel, pristine + b"\nBACKDOOR = 1\n")
                try:
                    message = self.assertGuardFails(
                        "companion immutable file drifted", self.repo.verify
                    )
                finally:
                    self.repo.write(rel, pristine)
                self.assertIn(rel, message)
                other = [p for p in _COMPANION_PATHS if p != rel][0]
                self.assertNotIn(other, message)
                self.repo.verify()  # restoring the bytes clears the failure

    def test_companion_drift_is_caught_even_with_a_full_legal_chain(self):
        """A companion cannot ride in on someone else's receipt."""

        for index in (1, 2, 3, 6):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        self.repo.verify()
        self.repo.write(
            "scripts/cwk_tenant_cli_api.py",
            self.repo.read("scripts/cwk_tenant_cli_api.py") + b"\nAPI_VERSION = 2\n",
        )
        self.assertGuardFails("companion immutable file drifted", self.repo.verify)


# ---------------------------------------------------------------------------
# Receipt chain replay
# ---------------------------------------------------------------------------


class ChainReplayTests(GuardTestCase):
    def test_no_receipt_and_no_drift_passes(self):
        report = self.repo.verify()
        self.assertEqual(report.receipt_count, 0)
        self.assertEqual(report.immutable_count, 17)
        self.assertEqual(report.tenant_cli_slots, self.repo.baseline_slots)

    def test_no_receipt_drift_rejected(self):
        self.repo.write("scripts/cwk_access_ledger.py", "VERSION = 2\n")
        message = self.assertGuardFails("frozen file drift detected", self.repo.verify)
        self.assertIn("no evolution receipt yet", message)

    def test_immutable_path_drift_rejected(self):
        self.repo.write("scripts/cwk_frozen_07.py", "TAMPERED = True\n")
        message = self.assertGuardFails("frozen file drift detected", self.repo.verify)
        self.assertIn("NOT evolvable", message)

    def test_companion_immutable_drift_rejected(self):
        self.repo.write("scripts/cwk_tenant_cli_api.py", "API_VERSION = 2\n")
        self.assertGuardFails("companion immutable file drifted", self.repo.verify)

    def test_single_receipt_chain_passes(self):
        self.repo.add_receipt(1)
        report = self.repo.verify()
        self.assertEqual(report.receipt_count, 1)
        self.assertEqual(
            report.tips["scripts/cwk_access_ledger.py"], self.repo.sha("scripts/cwk_access_ledger.py")
        )

    def test_receipt_present_but_file_reverted_rejected(self):
        self.repo.add_receipt(1)
        self.repo.write("scripts/cwk_access_ledger.py", '"""Synthetic scripts/cwk_access_ledger.py."""\n\nVERSION = 1\n')
        message = self.assertGuardFails("frozen file drift detected", self.repo.verify)
        self.assertIn("tip of 1 receipt(s)", message)

    def test_actual_bytes_beyond_the_tip_rejected(self):
        self.repo.add_receipt(1)
        self.repo.write(
            "scripts/cwk_access_ledger.py", self.repo.read("scripts/cwk_access_ledger.py") + b"# extra\n"
        )
        self.assertGuardFails("frozen file drift detected", self.repo.verify)

    def test_gap_in_chain_rejected(self):
        """Ordinal 2 present while ordinal 1 is absent.

        Stage 7 declares ``requires_stage_index: 3`` in the real policy, and
        that rule now fires *before* the generic gap rule so the diagnostic can
        name the skipped RT.  To keep the gap branch itself under test this
        variant clears ``requires_stage_index``, leaving prefix closure as the
        only thing standing between ordinal 2 and the chain.
        """

        raw = copy.deepcopy(self.repo.policy_raw)
        for stage in raw["stages"]:
            if stage["stage_index"] == 7:
                stage["requires_stage_index"] = None
        self.repo.write_policy(raw)  # rotates self.policy_sha

        stage7 = self.repo.stage(7)
        self.repo.slots.append(stage7["adds_provider_slot"])
        self.repo.write(self.repo.tenant_cli_path, synth_tenant_cli(self.repo.slots))
        receipt = {
            "schema": "cwk.pr001.script_evolution_receipt.v1",
            "policy_id": self.repo.policy_raw["policy_id"],
            "policy_sha256": self.repo.policy_sha,
            "stage_index": 7,
            "owner_rt": "RT-026",
            "target_path": self.repo.tenant_cli_path,
            "ordinal": 2,
            "adds_provider_slot": stage7["adds_provider_slot"],
            "from_sha256": self.repo.genesis[self.repo.tenant_cli_path],
            "to_sha256": self.repo.sha(self.repo.tenant_cli_path),
            "previous_receipt_sha256": EG.genesis_link(
                domain=self.repo.policy_raw["genesis_link_domain"],
                policy_sha256=self.repo.policy_sha,
                target_path=self.repo.tenant_cli_path,
            ),
            "migration_note_path": stage7["migration_note_path"],
            "migration_note_sha256": "0" * 64,
            "acceptance_test_refs": ["tests/test_rt026_evolution.py::EvolutionTests::test_stage_07"],
            "recorded_at": "2026-08-21T00:00:00Z",
        }
        self.repo.write(stage7["receipt_path"], _dump(receipt))
        self.assertGuardFails("is gapped", self.repo.verify)

    def test_wrong_from_sha_rejected(self):
        self.repo.add_receipt(1, from_sha256="a" * 64)
        self.assertGuardFails("does not continue the chain", self.repo.verify)

    def test_from_equals_to_rejected(self):
        genesis_sha = self.repo.genesis["scripts/cwk_access_ledger.py"]
        self.repo.add_receipt(1, from_sha256=genesis_sha, to_sha256=genesis_sha)
        self.assertGuardFails("no-op receipt", self.repo.verify)

    def test_wrong_to_sha_rejected(self):
        self.repo.add_receipt(1, to_sha256="d" * 64)
        self.assertGuardFails("frozen file drift detected", self.repo.verify)

    def test_wrong_previous_link_rejected(self):
        self.repo.add_receipt(1, previous_receipt_sha256="e" * 64)
        message = self.assertGuardFails("previous_receipt_sha256 is broken", self.repo.verify)
        self.assertIn("genesis link", message)

    def test_genesis_link_copied_from_another_path_rejected(self):
        stolen = EG.genesis_link(
            domain=self.repo.policy_raw["genesis_link_domain"],
            policy_sha256=self.repo.policy_sha,
            target_path="scripts/cwk_collect_live.py",
        )
        self.repo.add_receipt(1, previous_receipt_sha256=stolen)
        self.assertGuardFails("previous_receipt_sha256 is broken", self.repo.verify)

    def test_second_receipt_with_broken_previous_bytes_link_rejected(self):
        self.repo.add_receipt(3)
        self.repo.add_receipt(7, previous_receipt_sha256="f" * 64)
        message = self.assertGuardFails("previous_receipt_sha256 is broken", self.repo.verify)
        self.assertIn("previous receipt's raw bytes", message)

    def test_receipt_bytes_edited_after_the_fact_breaks_the_next_link(self):
        self.repo.add_receipt(3)
        self.repo.add_receipt(7)
        stage3 = self.repo.stage(3)
        tampered = self.repo.read(stage3["receipt_path"]).replace(
            b'"recorded_at": "2026-08-21T00:00:00Z"', b'"recorded_at": "2026-08-22T00:00:00Z"'
        )
        self.repo.write(stage3["receipt_path"], tampered)
        self.assertGuardFails("previous_receipt_sha256 is broken", self.repo.verify)

    def test_wrong_owner_rt_rejected(self):
        self.repo.add_receipt(1, owner_rt="RT-026")
        self.assertGuardFails("owner_rt is 'RT-026'", self.repo.verify)

    def test_wrong_stage_index_rejected(self):
        self.repo.add_receipt(1, stage_index=2)
        self.assertGuardFails("stage_index is 2", self.repo.verify)

    def test_wrong_target_path_rejected(self):
        self.repo.add_receipt(1, target_path="scripts/cwk_wiki_query.py")
        self.assertGuardFails("target_path is", self.repo.verify)

    def test_wrong_ordinal_rejected(self):
        self.repo.add_receipt(1, ordinal=2)
        self.assertGuardFails("ordinal is 2", self.repo.verify)

    def test_receipt_bound_to_a_different_policy_rejected(self):
        self.repo.add_receipt(1, policy_sha256="9" * 64)
        self.assertGuardFails("bound to a different policy", self.repo.verify)

    def test_receipt_with_wrong_policy_id_rejected(self):
        self.repo.add_receipt(1, policy_id="pr001-script-evolution-v2")
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_receipt_with_duplicate_json_key_rejected(self):
        receipt = self.repo.build_receipt(1)
        text = json.dumps(receipt, indent=2, sort_keys=True)
        injected = text.replace('{\n', '{\n  "ordinal": 1,\n', 1)
        self.repo.write(self.repo.stage(1)["receipt_path"], injected.encode("utf-8"))
        self.assertGuardFails("duplicate", self.repo.verify)

    def test_receipt_with_extra_field_rejected(self):
        self.repo.add_receipt(1, note="please ignore the chain")
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_receipt_with_bom_rejected(self):
        receipt = self.repo.build_receipt(1)
        self.repo.write(self.repo.stage(1)["receipt_path"], b"\xef\xbb\xbf" + _dump(receipt))
        self.assertGuardFails("BOM", self.repo.verify)

    def test_receipt_missing_required_field_rejected(self):
        receipt = self.repo.build_receipt(1)
        del receipt["recorded_at"]
        self.repo.write(self.repo.stage(1)["receipt_path"], _dump(receipt))
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_receipt_with_uppercase_sha_rejected(self):
        self.repo.add_receipt(1, to_sha256="A" * 64)
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_undeclared_receipt_file_rejected(self):
        self.repo.write(
            "RT/RT-017/receipts/script-evolution/stage-99-forged.json", _dump({"schema": "x"})
        )
        self.assertGuardFails("undeclared script-evolution receipt", self.repo.verify)

    def test_receipt_written_at_another_stages_declared_path_rejected(self):
        receipt = self.repo.build_receipt(1)
        self.repo.write(self.repo.stage(2)["receipt_path"], _dump(receipt))
        self.assertGuardFails("stage_index is 1", self.repo.verify)

    def test_receipt_symlink_rejected(self):
        stage = self.repo.stage(1)
        real = self.repo.build_receipt(1)
        self.repo.write("scripts/cwk_decoy.json", _dump(real))
        target = self.root / stage["receipt_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(self.root / "scripts" / "cwk_decoy.json")
        self.assertGuardFails("must not be a symlink", self.repo.verify)

    def test_full_chain_of_all_ten_stages_passes(self):
        for index in range(1, 11):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        report = self.repo.verify()
        self.assertEqual(report.receipt_count, 10)
        self.assertEqual(
            report.tenant_cli_slots,
            self.repo.baseline_slots + ("cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"),
        )


# ---------------------------------------------------------------------------
# Migration notes
# ---------------------------------------------------------------------------


class MigrationNoteTests(GuardTestCase):
    def test_missing_migration_note_rejected(self):
        self.repo.add_receipt(1)
        (self.root / self.repo.stage(1)["migration_note_path"]).unlink()
        self.assertGuardFails("migration note", self.repo.verify)

    def test_migration_note_sha_mismatch_rejected(self):
        self.repo.add_receipt(1)
        note = self.repo.stage(1)["migration_note_path"]
        self.repo.write(note, self.repo.read(note) + b"quietly appended\n")
        self.assertGuardFails("migration note SHA mismatch", self.repo.verify)

    def test_migration_note_path_not_the_declared_one_rejected(self):
        other = self.repo.stage(2)["migration_note_path"]
        self.repo.add_receipt(1, migration_note_path=other)
        self.assertGuardFails("!= policy-declared", self.repo.verify)

    def test_migration_note_too_short_rejected(self):
        self.repo.add_receipt(1)
        note = self.repo.stage(1)["migration_note_path"]
        self.repo.write(note, "RT-017 cwk_access_ledger.py ok\n")
        self.assertGuardFails("migration note SHA mismatch", self.repo.verify)

    def test_migration_note_short_but_hash_matched_rejected(self):
        stage = self.repo.stage(1)
        self.repo.add_receipt(1)
        body = "RT-017 touched cwk_access_ledger.py.\n"
        self.repo.write(stage["migration_note_path"], body)
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["migration_note_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("bytes; at least", self.repo.verify)

    def test_migration_note_never_mentioning_the_owner_rejected(self):
        stage = self.repo.stage(1)
        self.repo.add_receipt(1)
        body = "Some unrelated prose about cwk_access_ledger.py. " * 8 + "\n"
        self.repo.write(stage["migration_note_path"], body)
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["migration_note_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("never mentions RT-017", self.repo.verify)

    def test_migration_note_never_mentioning_the_target_rejected(self):
        stage = self.repo.stage(1)
        self.repo.add_receipt(1)
        body = "RT-017 changed something, trust me, honestly, really. " * 6 + "\n"
        self.repo.write(stage["migration_note_path"], body)
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["migration_note_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("never mentions cwk_access_ledger.py", self.repo.verify)


# ---------------------------------------------------------------------------
# Acceptance test references
# ---------------------------------------------------------------------------


class AcceptanceTestRefTests(GuardTestCase):
    def test_missing_acceptance_test_file_rejected(self):
        self.repo.add_receipt(1)
        (self.root / "tests" / "test_rt017_evolution.py").unlink()
        self.assertGuardFails("does not exist", self.repo.verify)

    def test_acceptance_ref_naming_another_rt_rejected(self):
        self.repo.write(
            "tests/test_rt026_evolution.py",
            "import unittest\n\n\nclass EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n",
        )
        self.repo.add_receipt(
            1, acceptance_test_refs=["tests/test_rt026_evolution.py::EvolutionTests::test_stage_01"]
        )
        self.assertGuardFails("does not belong to RT-017", self.repo.verify)

    def test_acceptance_ref_with_unknown_method_rejected(self):
        self.repo.add_receipt(
            1, acceptance_test_refs=["tests/test_rt017_evolution.py::EvolutionTests::test_imaginary"]
        )
        self.assertGuardFails("has no method test_imaginary", self.repo.verify)

    def test_acceptance_ref_with_unknown_class_rejected(self):
        self.repo.add_receipt(
            1, acceptance_test_refs=["tests/test_rt017_evolution.py::GhostTests::test_stage_01"]
        )
        self.assertGuardFails("has no class GhostTests", self.repo.verify)

    def test_acceptance_ref_out_of_grammar_rejected(self):
        self.repo.add_receipt(1, acceptance_test_refs=["tests/test_rt017_evolution.py"])
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_acceptance_ref_with_traversal_rejected(self):
        self.repo.add_receipt(
            1,
            acceptance_test_refs=[
                "tests/test_rt017_../../etc.py::EvolutionTests::test_stage_01"
            ],
        )
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_empty_acceptance_refs_rejected(self):
        self.repo.add_receipt(1, acceptance_test_refs=[])
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_duplicate_acceptance_refs_rejected(self):
        ref = "tests/test_rt017_evolution.py::EvolutionTests::test_stage_01"
        self.repo.add_receipt(1, acceptance_test_refs=[ref, ref])
        self.assertGuardFails("schema violation", self.repo.verify)

    def test_acceptance_test_file_that_does_not_parse_rejected(self):
        self.repo.add_receipt(1)
        self.repo.write("tests/test_rt017_evolution.py", "class EvolutionTests(:\n")
        self.assertGuardFails("does not parse", self.repo.verify)


# ---------------------------------------------------------------------------
# M2: a referenced acceptance test must be collectable, runnable and non-empty
# ---------------------------------------------------------------------------


class AcceptanceTestRigorTests(GuardTestCase):
    """A receipt cites its acceptance test as evidence the migration works.

    Name-resolution alone let a class that ``unittest`` never collects, a
    method decorated ``@skip``, or a body of ``pass`` count as that evidence.
    These checks are deliberately **static only** — see
    ``test_the_guard_never_executes_the_acceptance_test`` for why, and note
    that they establish *shape*, never *result*: actually running the test
    stays the owner RT's job.
    """

    REL = "tests/test_rt017_evolution.py"
    REF = "tests/test_rt017_evolution.py::EvolutionTests::test_stage_01"

    def _accept(self, source: str) -> None:
        """Point a stage-1 receipt's only acceptance ref at *source*.

        The receipt is written once per test; subsequent calls (from
        ``subTest`` loops) only swap the acceptance file, so the chain stays
        valid and the acceptance gate is the sole thing under test.
        """

        if not getattr(self, "_receipt_written", False):
            self.repo.add_receipt(1, acceptance_test_refs=[self.REF])
            self._receipt_written = True
        self.repo.write(self.REL, source)

    def _class(self, body: str, *, decorator: str = "", bases: str = "unittest.TestCase") -> str:
        head = f"{decorator}\n" if decorator else ""
        return f"import unittest\n\n\n{head}class EvolutionTests({bases}):\n{body}"

    def test_a_well_formed_acceptance_test_is_accepted(self):
        self._accept(
            self._class("    def test_stage_01(self):\n        self.assertEqual(1, 1)\n")
        )
        self.repo.verify()

    def test_class_that_unittest_would_never_collect_rejected(self):
        self._accept(
            "import unittest\n\n\nclass EvolutionTests:\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_class_deriving_from_a_plain_object_base_rejected(self):
        self._accept(
            "import unittest\n\n\nclass Helper:\n    pass\n\n\n"
            "class EvolutionTests(Helper):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_class_deriving_through_an_intermediate_base_rejected(self):
        """A working shape, refused on purpose -- see the canonical surface.

        ``unittest`` would collect this one.  It is still refused because a
        local intermediate base is exactly where an inherited ``@unittest.skip``
        hides, and telling the two apart statically is what kept failing
        review.
        """

        source = (
            "import unittest\n\n\nclass Base(unittest.TestCase):\n    pass\n\n\n"
            "class EvolutionTests(Base):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 1)
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_skipped_class_rejected(self):
        for decorator in ("@unittest.skip('later')", "@unittest.skipUnless(False, 'nope')"):
            with self.subTest(decorator=decorator):
                self._accept(
                    self._class(
                        "    def test_stage_01(self):\n        self.assertTrue(True)\n",
                        decorator=decorator,
                    )
                )
                self.assertGuardFails("is decorated with skip/expectedFailure", self.repo.verify)

    def test_skipped_method_rejected(self):
        for decorator in (
            "@unittest.skip('later')",
            "@unittest.skipIf(True, 'nope')",
            "@unittest.expectedFailure",
        ):
            with self.subTest(decorator=decorator):
                self._accept(
                    self._class(
                        f"    {decorator}\n"
                        "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                    )
                )
                self.assertGuardFails(
                    "a skipped test is not evidence that the migration works", self.repo.verify
                )

    def test_method_calling_skiptest_rejected(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n        self.skipTest('not today')\n"
            )
        )
        self.assertGuardFails("calls skipTest", self.repo.verify)

    def test_async_method_rejected(self):
        self._accept(
            self._class(
                "    async def test_stage_01(self):\n        self.assertTrue(True)\n"
            )
        )
        self.assertGuardFails("is an async def; unittest cannot run it", self.repo.verify)

    def test_method_without_self_rejected(self):
        self._accept(
            self._class(
                "    @staticmethod\n"
                "    def test_stage_01():\n        raise AssertionError('x')\n"
            )
        )
        self.assertGuardFails("does not take 'self' as its first parameter", self.repo.verify)

    def test_method_with_extra_required_parameters_rejected(self):
        for signature in ("self, fixture", "self, *values"):
            with self.subTest(signature=signature):
                self._accept(
                    self._class(
                        f"    def test_stage_01({signature}):\n        self.assertTrue(True)\n"
                    )
                )
                self.assertGuardFails("takes extra required parameters", self.repo.verify)

    def test_method_with_required_keyword_only_parameter_rejected(self):
        self._accept(
            self._class(
                "    def test_stage_01(self, *, fixture):\n        self.assertTrue(True)\n"
            )
        )
        self.assertGuardFails("has required keyword-only parameters", self.repo.verify)

    def test_method_with_default_arguments_accepted(self):
        """Defaults are fine — unittest can still invoke it."""

        self._accept(
            self._class(
                "    def test_stage_01(self, fixture=None):\n"
                "        self.assertIsNone(fixture)\n"
            )
        )
        self.repo.verify()

    def test_no_op_method_body_rejected(self):
        for body in ("        pass\n", "        ...\n", '        """Documented, not tested."""\n'):
            with self.subTest(body=body.strip()):
                self._accept(self._class(f"    def test_stage_01(self):\n{body}"))
                self.assertGuardFails("has an empty body", self.repo.verify)

    def test_method_that_makes_no_assertion_rejected(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n"
                "        value = 1 + 1\n"
                "        print(value)\n"
            )
        )
        self.assertGuardFails("makes no assertion", self.repo.verify)

    def test_docstring_plus_a_real_assertion_accepted(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n"
                '        """Stage 1 keeps the ledger readable."""\n'
                "        self.assertGreater(2, 1)\n"
            )
        )
        self.repo.verify()

    def test_self_fail_counts_as_an_assertion(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n"
                "        if 1 + 1 != 2:\n"
                "            self.fail('arithmetic broke')\n"
            )
        )
        self.repo.verify()

    def test_the_guard_never_executes_the_acceptance_test(self):
        """Executing refs would recurse: RT-016's test calls this guard.

        Proof by construction — the referenced test raises on import *and*
        fails on execution, yet the guard still accepts its shape.  Anything
        that imported or ran it would blow up here instead of passing.
        """

        self._accept(
            "import unittest\n\n"
            "raise RuntimeError('importing this module must never happen')\n\n\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n"
            "        self.assertTrue(False)\n"
        )
        self.repo.verify()
        doc = " ".join((EG._assert_runnable_test_method.__doc__ or "").split())
        self.assertIn("deliberately never imports or executes an acceptance test", doc)
        self.assertIn("would recurse", doc)
        self.assertIn("remains the owner RT's job", doc)

    # -- bypass 1: base-class provenance ---------------------------------
    #
    # Matching the *name* ``TestCase`` accepted a class unittest never
    # collects.  Every case below is checked twice: the guard's verdict, and
    # what unittest actually does with the same source.

    def _load_fixture(self, source: str):
        """Import *source* as a throwaway module (outside the guarded repo)."""

        directory = tempfile.mkdtemp(prefix="pr001-fixture-")
        self.addCleanup(shutil.rmtree, directory, True)
        name = f"pr001_fixture_{len(sys.modules):x}_{os.getpid():x}"
        path = Path(directory) / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    def _unittest_collects(self, source: str) -> int:
        """How many tests unittest really finds in *source*."""

        suite = unittest.TestLoader().loadTestsFromModule(self._load_fixture(source))
        return suite.countTestCases()

    def _unittest_run(self, source: str) -> unittest.TestResult:
        suite = unittest.TestLoader().loadTestsFromModule(self._load_fixture(source))
        return unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0
        ).run(suite)

    FAKE_TESTCASE = (
        "import unittest\n\n\n"
        "class TestCase:\n"
        "    pass\n\n\n"
        "class EvolutionTests(TestCase):\n"
        "    def test_stage_01(self):\n"
        "        self.assertTrue(True)\n"
    )

    def test_locally_faked_testcase_base_is_rejected_and_collects_nothing(self):
        """The reported bypass: a home-made ``TestCase`` read as the real one.

        ``unittest`` collects zero tests from this module, so citing it as
        acceptance evidence proved nothing at all.
        """

        self.assertEqual(self._unittest_collects(self.FAKE_TESTCASE), 0)
        self._accept(self.FAKE_TESTCASE)
        message = self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)
        self.assertIn("locally defined or aliased base of the same name is refused", message)

    def test_the_well_formed_control_really_is_collected(self):
        """Counterpart to the bypass above: the accepted shape collects one."""

        source = self._class("    def test_stage_01(self):\n        self.assertEqual(1, 1)\n")
        self.assertEqual(self._unittest_collects(source), 1)
        self._accept(source)
        self.repo.verify()

    def test_from_unittest_import_testcase_accepted(self):
        source = (
            "from unittest import TestCase\n\n\n"
            "class EvolutionTests(TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 1)
        self._accept(source)
        self.repo.verify()

    def test_imported_testcase_rebound_later_is_rejected(self):
        """A legal import does not stay legal if the name is reassigned."""

        source = (
            "from unittest import TestCase\n\n\n"
            "class EvolutionTests(TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n\n\n"
            "TestCase = object\n"
        )
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_imported_testcase_shadowed_by_a_local_class_is_rejected(self):
        source = (
            "from unittest import TestCase\n\n\n"
            "class TestCase:  # noqa: F811 - deliberate shadow\n"
            "    pass\n\n\n"
            "class EvolutionTests(TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_aliased_testcase_bases_are_refused_conservatively(self):
        """Aliases are refused, not resolved -- see ``_is_unittest_testcase_base``."""

        for header, base in (
            ("from unittest import TestCase as Base", "Base"),
            ("import unittest as ut", "ut.TestCase"),
            ("from unittest import case as _c", "_c.TestCase"),
        ):
            with self.subTest(base=base):
                self._accept(
                    f"{header}\n\n\nclass EvolutionTests({base}):\n"
                    "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                )
                self.assertGuardFails(
                    "does not derive from unittest.TestCase", self.repo.verify
                )

    def test_unittest_module_name_rebound_is_rejected(self):
        source = (
            "import unittest\n\n\n"
            "class _Fake:\n"
            "    TestCase = object\n\n\n"
            "unittest = _Fake\n\n\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_intermediate_base_shadowed_by_an_assignment_is_rejected(self):
        source = (
            "import unittest\n\n\n"
            "class Base(unittest.TestCase):\n    pass\n\n\n"
            "Base = object\n\n\n"
            "class EvolutionTests(Base):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_multiple_inheritance_is_rejected(self):
        """Any second base could carry a skip marker into the MRO."""

        self._accept(
            "import unittest\n\n\nclass Mixin:\n    pass\n\n\n"
            "class EvolutionTests(unittest.TestCase, Mixin):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        message = self.assertGuardFails(
            "does not derive from unittest.TestCase", self.repo.verify
        )
        self.assertIn("it declares 2 bases", message)

    def test_keyword_base_arguments_are_rejected(self):
        self._accept(
            "import unittest\n\n\n"
            "class EvolutionTests(unittest.TestCase, metaclass=type):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_unittest_imported_inside_a_try_block_is_rejected(self):
        """The import must be a direct top-level statement, not a guarded one."""

        self._accept(
            "try:\n    import unittest\nexcept ImportError:\n    unittest = None\n\n\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    # -- inherited skip markers (the reason local bases are refused) -------

    def test_a_base_class_skip_decorator_really_skips_and_is_rejected(self):
        """``@unittest.skip`` on the base skips a target with no decorator."""

        source = (
            "import unittest\n\n\n"
            "@unittest.skip('later')\n"
            "class Base(unittest.TestCase):\n    pass\n\n\n"
            "class EvolutionTests(Base):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1, "the skip is inherited through the MRO")
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_a_base_class_aliased_skip_decorator_is_rejected(self):
        source = (
            "import unittest\n\noff = unittest.skipIf(True, 'later')\n\n\n"
            "@off\n"
            "class Base(unittest.TestCase):\n    pass\n\n\n"
            "class EvolutionTests(Base):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1)
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    def test_a_base_class_skip_flag_is_rejected(self):
        """Two independent gates catch this: the flag scan and the base rule."""

        source = (
            "import unittest\n\n\n"
            "class Base(unittest.TestCase):\n"
            "    __unittest_skip__ = True\n\n\n"
            "class EvolutionTests(Base):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1)
        self._accept(source)
        self.assertGuardFails("assigns __unittest_skip__", self.repo.verify)

    # -- conditional and dynamic definitions -------------------------------

    def test_class_hidden_behind_a_false_branch_is_rejected(self):
        """``if False:`` binds the name to nothing unittest ever collects."""

        source = (
            "import unittest\n\n"
            "if False:\n"
            "    class EvolutionTests(unittest.TestCase):\n"
            "        def test_stage_01(self):\n            self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        message = self.assertGuardFails(
            "not as a direct top-level class statement", self.repo.verify
        )
        self.assertIn("cannot be decided statically", message)

    def test_class_defined_inside_a_truthy_branch_is_also_rejected(self):
        """Refused even though this one does collect -- the surface is shape-based."""

        source = (
            "import unittest\n\n"
            "if True:\n"
            "    class EvolutionTests(unittest.TestCase):\n"
            "        def test_stage_01(self):\n            self.assertTrue(True)\n"
        )
        self.assertEqual(self._unittest_collects(source), 1)
        self._accept(source)
        self.assertGuardFails("not as a direct top-level class statement", self.repo.verify)

    def test_class_defined_inside_a_try_block_is_rejected(self):
        self._accept(
            "import unittest\n\n"
            "try:\n"
            "    class EvolutionTests(unittest.TestCase):\n"
            "        def test_stage_01(self):\n            self.assertTrue(True)\n"
            "except Exception:\n    pass\n"
        )
        self.assertGuardFails("not as a direct top-level class statement", self.repo.verify)

    def test_conditionally_rebound_base_is_rejected(self):
        """A rebinding hidden in a branch still rebinds at import time."""

        source = (
            "import os\n"
            "from unittest import TestCase\n\n\n"
            "if os.environ.get('CWK_UNSET_SENTINEL'):\n"
            "    TestCase = object\n\n\n"
            "class EvolutionTests(TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self._accept(source)
        self.assertGuardFails("does not derive from unittest.TestCase", self.repo.verify)

    # -- bypass 2: skip flags and aliased skip decorators ------------------

    def test_class_body_skip_flag_is_rejected_and_really_skips(self):
        """``__unittest_skip__ = True`` disables a class with no decorator."""

        source = self._class(
            "    __unittest_skip__ = True\n"
            "    __unittest_skip_why__ = 'later'\n\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1)
        self._accept(source)
        self.assertGuardFails("assigns __unittest_skip__", self.repo.verify)

    def test_method_skip_flag_assigned_from_module_scope_is_rejected(self):
        source = (
            self._class("    def test_stage_01(self):\n        self.assertTrue(True)\n")
            + "\n\nEvolutionTests.test_stage_01.__unittest_skip__ = True\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1)
        self._accept(source)
        self.assertGuardFails("assigns __unittest_skip__", self.repo.verify)

    def test_destructured_skip_flag_is_rejected_and_really_skips(self):
        """Reported bypass: the flag bound through a tuple-unpacking target.

        Reading only ``Name``/``Attribute`` assignment targets walked straight
        past this; ``unittest`` binds and honours it all the same.
        """

        source = self._class(
            "    (__unittest_skip__, marker) = (True, 1)\n\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        result = self._unittest_run(source)
        self.assertEqual(len(result.skipped), 1, "unittest honours the destructured flag")
        self._accept(source)
        self.assertGuardFails("assigns __unittest_skip__", self.repo.verify)

    def test_skip_flag_bound_through_every_destructuring_shape_is_rejected(self):
        for target in (
            "[__unittest_skip__, marker]",
            "(marker, (__unittest_skip__, other))",
            "(marker, *__unittest_skip__)",
            "marker, __unittest_skip_why__",
        ):
            with self.subTest(target=target):
                self._accept(
                    self._class(
                        f"    {target} = (True, True)\n\n"
                        "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                    )
                )
                self.assertGuardFails("that flag skips the test", self.repo.verify)

    def test_skip_flag_bound_by_a_for_target_or_walrus_is_rejected(self):
        for statement in (
            "for __unittest_skip__ in (True,):\n        pass",
            "_ = (__unittest_skip__ := True)",
            "with open(__file__) as __unittest_skip__:\n        pass",
        ):
            with self.subTest(statement=statement.splitlines()[0]):
                self._accept(
                    self._class(
                        f"    {statement}\n\n"
                        "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                    )
                )
                self.assertGuardFails("that flag skips the test", self.repo.verify)

    def test_expecting_failure_flag_is_rejected(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n        self.assertTrue(True)\n"
            )
            + "\n\nEvolutionTests.test_stage_01.__unittest_expecting_failure__ = True\n"
        )
        self.assertGuardFails("assigns __unittest_expecting_failure__", self.repo.verify)

    def test_aliased_skip_decorator_on_the_method_is_rejected(self):
        """``disable = unittest.skip(...)`` then ``@disable`` names no skip."""

        for alias in (
            "disable = unittest.skip('later')",
            "disable = unittest.skipIf(True, 'later')",
            "disable = unittest.skipUnless(False, 'later')",
            "disable = unittest.expectedFailure",
        ):
            with self.subTest(alias=alias):
                self._accept(
                    f"import unittest\n\n{alias}\n\n\n"
                    "class EvolutionTests(unittest.TestCase):\n"
                    "    @disable\n"
                    "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                )
                self.assertGuardFails(
                    "is decorated with skip/expectedFailure", self.repo.verify
                )

    def test_aliased_skip_decorator_on_the_class_is_rejected(self):
        self._accept(
            "import unittest\n\ndisable = unittest.skip('later')\n\n\n"
            "@disable\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n"
        )
        self.assertGuardFails("is decorated with skip/expectedFailure", self.repo.verify)

    def test_skip_helpers_imported_under_an_alias_are_rejected(self):
        for helper in ("skip", "skipIf", "skipUnless", "expectedFailure"):
            call = "_off" if helper == "expectedFailure" else "_off(True, 'later')"
            if helper == "skip":
                call = "_off('later')"
            with self.subTest(helper=helper):
                self._accept(
                    f"import unittest\nfrom unittest import {helper} as _off\n\n\n"
                    "class EvolutionTests(unittest.TestCase):\n"
                    f"    @{call}\n"
                    "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                )
                self.assertGuardFails(
                    "is decorated with skip/expectedFailure", self.repo.verify
                )

    def test_bare_skip_names_are_rejected(self):
        for helper, call in (
            ("skip", "skip('later')"),
            ("skipIf", "skipIf(True, 'later')"),
            ("skipUnless", "skipUnless(False, 'later')"),
            ("expectedFailure", "expectedFailure"),
        ):
            with self.subTest(helper=helper):
                self._accept(
                    f"import unittest\nfrom unittest import {helper}\n\n\n"
                    "class EvolutionTests(unittest.TestCase):\n"
                    f"    @{call}\n"
                    "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                )
                self.assertGuardFails(
                    "is decorated with skip/expectedFailure", self.repo.verify
                )

    def test_a_decorator_of_unknown_provenance_is_refused(self):
        """Unresolvable decorators are refused rather than guessed at."""

        for decorator in ("@_helpers.wrap", "@wrap", "@wrap(1)"):
            with self.subTest(decorator=decorator):
                self._accept(
                    "import unittest\nimport _helpers\nfrom _helpers import wrap\n\n\n"
                    "class EvolutionTests(unittest.TestCase):\n"
                    f"    {decorator}\n"
                    "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                )
                message = self.assertGuardFails(
                    "cannot be statically proven not to be a skip", self.repo.verify
                )
                self.assertIn("Cite an undecorated test", message)

    # -- bypass 3: duplicate bindings --------------------------------------

    DUPLICATE_METHOD = (
        "import unittest\n\n\n"
        "class EvolutionTests(unittest.TestCase):\n"
        "    def test_stage_01(self):\n"
        "        self.fail('stage 1 never ran')\n\n"
        "    def test_stage_01(self):  # noqa: F811 - deliberate override\n"
        "        pass\n"
    )

    def test_duplicate_method_is_rejected_and_the_no_op_really_wins(self):
        """The reported bypass: an asserting def overwritten by a no-op.

        Running the fixture proves the override is real -- the ``self.fail``
        never executes and the suite passes green.
        """

        result = self._unittest_run(self.DUPLICATE_METHOD)
        self.assertEqual(result.testsRun, 1)
        self.assertTrue(result.wasSuccessful(), "the no-op definition is the one that runs")
        self._accept(self.DUPLICATE_METHOD)
        message = self.assertGuardFails("is bound 2 times", self.repo.verify)
        self.assertIn("Python keeps only the last", message)

    def test_class_deleted_after_definition_is_rejected(self):
        """Reported bypass: ``del`` unbinds a name the AST still shows defined."""

        source = (
            self._class("    def test_stage_01(self):\n        self.assertTrue(True)\n")
            + "\n\ndel EvolutionTests\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        message = self.assertGuardFails("deletes EvolutionTests", self.repo.verify)
        self.assertIn("unittest collects nothing", message)

    def test_method_deleted_from_the_class_body_is_rejected(self):
        source = self._class(
            "    def test_stage_01(self):\n        self.assertTrue(True)\n\n"
            "    del test_stage_01\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        self.assertGuardFails("deletes test_stage_01", self.repo.verify)

    def test_method_deleted_through_an_attribute_is_rejected(self):
        source = (
            self._class("    def test_stage_01(self):\n        self.assertTrue(True)\n")
            + "\n\ndel EvolutionTests.test_stage_01\n"
        )
        self.assertEqual(self._unittest_collects(source), 0)
        self._accept(source)
        self.assertGuardFails("deletes test_stage_01", self.repo.verify)

    def test_delete_hidden_in_a_branch_or_destructured_is_rejected(self):
        for tail in (
            "if True:\n    del EvolutionTests\n",
            "try:\n    del EvolutionTests\nexcept NameError:\n    pass\n",
            "_marker = 1\ndel EvolutionTests, _marker\n",
        ):
            with self.subTest(tail=tail.splitlines()[0]):
                self._accept(
                    self._class(
                        "    def test_stage_01(self):\n        self.assertTrue(True)\n"
                    )
                    + f"\n\n{tail}"
                )
                self.assertGuardFails("deletes EvolutionTests", self.repo.verify)

    def test_duplicate_class_is_rejected(self):
        self._accept(
            "import unittest\n\n\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n\n\n"
            "class EvolutionTests(unittest.TestCase):  # noqa: F811\n"
            "    def test_stage_01(self):\n        pass\n"
        )
        message = self.assertGuardFails("binds EvolutionTests 2 times", self.repo.verify)
        self.assertIn("the last binding wins at import time", message)

    def test_class_rebound_by_an_assignment_is_rejected(self):
        self._accept(
            self._class("    def test_stage_01(self):\n        self.assertTrue(True)\n")
            + "\n\nEvolutionTests = object\n"
        )
        self.assertGuardFails("binds EvolutionTests 2 times", self.repo.verify)

    def test_method_rebound_by_an_assignment_is_rejected(self):
        self._accept(
            self._class(
                "    def test_stage_01(self):\n        self.assertTrue(True)\n\n"
                "    test_stage_01 = lambda self: None\n"
            )
        )
        self.assertGuardFails("is bound 2 times", self.repo.verify)

    def test_method_rebound_inside_a_class_body_branch_is_rejected(self):
        """A conditional override binds the name just the same."""

        self._accept(
            "import os\nimport unittest\n\n\n"
            "class EvolutionTests(unittest.TestCase):\n"
            "    def test_stage_01(self):\n        self.assertTrue(True)\n\n"
            "    if os.environ.get('CWK_UNSET_SENTINEL'):\n"
            "        def test_stage_01(self):  # noqa: F811\n            pass\n"
        )
        self.assertGuardFails("is bound 2 times", self.repo.verify)

    def test_a_method_bound_as_something_other_than_a_def_is_rejected(self):
        self._accept(
            self._class("    test_stage_01 = staticmethod(lambda: None)\n")
        )
        self.assertGuardFails(
            "is bound as assignment, not as a direct def in the class body", self.repo.verify
        )

    def test_a_class_name_bound_as_something_other_than_a_class_is_rejected(self):
        self._accept(
            "import unittest\n\n\n"
            "def _make():\n"
            "    class Inner(unittest.TestCase):\n"
            "        def test_stage_01(self):\n            self.assertTrue(True)\n"
            "    return Inner\n\n\n"
            "EvolutionTests = _make()\n"
        )
        self.assertGuardFails(
            "binds EvolutionTests as assignment, not as a direct top-level class statement",
            self.repo.verify,
        )

    def test_the_static_check_cannot_detect_a_tautological_assertion(self):
        """Documented residual limitation, asserted so it is not forgotten.

        ``self.assertTrue(True)`` is indistinguishable from a real assertion
        at AST level.  This gate raises the floor from "no test at all" to "a
        test that at least calls an assertion"; only the owner RT's own run
        can establish that the assertion means anything.
        """

        self._accept(
            self._class("    def test_stage_01(self):\n        self.assertTrue(True)\n")
        )
        self.repo.verify()


# ---------------------------------------------------------------------------
# Tenant CLI AST / comment / slot gates
# ---------------------------------------------------------------------------


class TenantCliAstTests(GuardTestCase):
    def _rewrite_cli(self, **kwargs: Any) -> None:
        self.repo.write(self.repo.tenant_cli_path, synth_tenant_cli(**kwargs))

    def test_baseline_slots_pass_without_receipts(self):
        report = self.repo.verify()
        self.assertEqual(report.tenant_cli_slots, self.repo.baseline_slots)

    def test_slot_appended_without_a_receipt_rejected(self):
        self._rewrite_cli(slots=list(self.repo.baseline_slots) + ["cwk_tenant_cmd_profile"])
        self.assertGuardFails("frozen file drift detected", self.repo.verify)

    def test_slot_reorder_rejected(self):
        self._rewrite_cli(slots=list(reversed(self.repo.baseline_slots)))
        message = self.assertGuardFails("frozen file drift detected", self.repo.verify)
        self.assertIn("no evolution receipt yet", message)

    def test_slot_reorder_with_a_receipt_rejected(self):
        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        reordered = ["cwk_tenant_cmd_profile"] + list(self.repo.baseline_slots)
        self._rewrite_cli(slots=reordered)
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("no longer a prefix", self.repo.verify)

    def test_slot_deletion_with_a_receipt_rejected(self):
        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        self._rewrite_cli(slots=[self.repo.baseline_slots[0], "cwk_tenant_cmd_profile"])
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("no longer a prefix", self.repo.verify)

    def test_duplicate_slot_rejected(self):
        self._rewrite_cli(slots=list(self.repo.baseline_slots) + [self.repo.baseline_slots[0]])
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                self.repo.read(self.repo.tenant_cli_path).decode("utf-8"),
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("duplicate provider slot", str(ctx.exception))

    def test_two_slots_appended_under_one_receipt_rejected(self):
        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        self._rewrite_cli(
            slots=list(self.repo.baseline_slots)
            + ["cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"]
        )
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        message = self.assertGuardFails("do not match the receipt chain", self.repo.verify)
        self.assertIn("adding two at once is rejected", message)

    def test_unknown_slot_name_rejected(self):
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                synth_tenant_cli(list(self.repo.baseline_slots) + ["totally_unrelated_module"]),
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("out of grammar", str(ctx.exception))

    def test_plain_assign_instead_of_annassign_rejected(self):
        text = synth_tenant_cli(self.repo.baseline_slots, use_annassign=False)
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                text,
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("exactly one module-level annotated assignment", str(ctx.exception))

    def test_annotation_retype_rejected(self):
        self._rewrite_cli(slots=self.repo.baseline_slots, annotation="list[str]")
        policy = self.repo.load(expected_policy_sha256=self.repo.policy_sha)
        self.assertGuardFails(
            "annotation drifted",
            EG.verify_tenant_cli,
            self.root,
            policy,
            self.repo.baseline_slots,
            ast_fingerprint=EG.tenant_cli_shape(
                self.repo.read(self.repo.tenant_cli_path).decode("utf-8"),
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            ).ast_fingerprint,
        )

    def test_symbol_rebound_elsewhere_rejected(self):
        text = synth_tenant_cli(
            self.repo.baseline_slots, extra_body="FROZEN_PROVIDER_SLOTS = ()"
        )
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                text,
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("rebound elsewhere", str(ctx.exception))

    def test_slot_span_longer_than_the_policy_bound_rejected(self):
        long_comment = "\n".join(f"    # filler {i}" for i in range(20))
        text = synth_tenant_cli(self.repo.baseline_slots, span_comment=long_comment)
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                text,
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("at most 16 are allowed", str(ctx.exception))

    def _fingerprint(self, **kwargs: Any) -> EG.TenantCliShape:
        return EG.tenant_cli_shape(
            synth_tenant_cli(self.repo.baseline_slots, **kwargs),
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        )

    def test_error_class_rename_changes_the_ast_fingerprint(self):
        self.assertNotEqual(
            self._fingerprint().ast_fingerprint,
            self._fingerprint(error_class="LoaderProblem").ast_fingerprint,
        )

    def test_weakened_loader_guard_changes_the_ast_fingerprint(self):
        self.assertNotEqual(
            self._fingerprint().ast_fingerprint,
            self._fingerprint(guard_expression="spec is None").ast_fingerprint,
        )

    def test_docstring_edit_changes_the_ast_fingerprint(self):
        self.assertNotEqual(
            self._fingerprint().ast_fingerprint,
            self._fingerprint(docstring="Synthetic tenant CLI fixture!").ast_fingerprint,
        )

    def test_nfd_literal_substitution_changes_the_ast_fingerprint(self):
        composed = self._fingerprint(docstring="Fixture café").ast_fingerprint
        decomposed = self._fingerprint(
            docstring=unicodedata.normalize("NFD", "Fixture café")
        ).ast_fingerprint
        self.assertNotEqual(composed, decomposed)

    def test_appending_a_slot_does_not_change_the_ast_fingerprint(self):
        base = self._fingerprint().ast_fingerprint
        grown = EG.tenant_cli_shape(
            synth_tenant_cli(list(self.repo.baseline_slots) + ["cwk_tenant_cmd_profile"]),
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        ).ast_fingerprint
        self.assertEqual(base, grown)

    def test_out_of_span_comment_drift_changes_the_comment_fingerprint(self):
        self.assertNotEqual(
            self._fingerprint().comment_fingerprint,
            self._fingerprint(trailer_comment="# rewritten trailing comment").comment_fingerprint,
        )

    def test_in_span_comment_edit_keeps_both_fingerprints(self):
        base = self._fingerprint()
        edited = self._fingerprint(span_comment="    # RT-019 shipped its slot here")
        self.assertEqual(base.ast_fingerprint, edited.ast_fingerprint)
        self.assertEqual(base.comment_fingerprint, edited.comment_fingerprint)

    def test_logic_drift_is_caught_end_to_end(self):
        self.repo.write(
            self.repo.tenant_cli_path,
            synth_tenant_cli(self.repo.baseline_slots, guard_expression="spec is None"),
        )
        message = self.assertGuardFails("frozen file drift detected", self.repo.verify)
        self.assertIn("no evolution receipt yet", message)

    def test_logic_drift_hidden_behind_a_receipt_is_still_caught(self):
        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        self.repo.write(
            self.repo.tenant_cli_path,
            synth_tenant_cli(
                list(self.repo.baseline_slots) + ["cwk_tenant_cmd_profile"],
                guard_expression="spec is None",
            ),
        )
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        self.assertGuardFails("AST fingerprint drifted", self.repo.verify)

    def test_unknown_ast_node_type_rejected(self):
        text = synth_tenant_cli(
            self.repo.baseline_slots, extra_body="async def later():\n    pass"
        )
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                text,
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("unsupported node type", str(ctx.exception))

    def test_carriage_return_line_endings_rejected(self):
        text = synth_tenant_cli(self.repo.baseline_slots).replace("\n", "\r\n")
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                text,
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("LF line endings", str(ctx.exception))


# ---------------------------------------------------------------------------
# M1: the comment fingerprint binds position and adjacency, not just text
# ---------------------------------------------------------------------------


class CommentPositionTests(GuardTestCase):
    """Hashing the comment *texts* alone was invariant under a pure move.

    ``# type: ignore`` and ``# noqa`` change which statement they suppress
    purely by moving, so an attacker could disable a checker on the loader
    guard without touching a single character the old fingerprint hashed.  The
    fingerprint now binds each out-of-span comment to a normalised position
    (absolute above the slot span, span-relative below it) plus its
    inline/own-line adjacency.
    """

    def _fp(self, **kwargs: Any) -> str:
        return EG.tenant_cli_shape(
            synth_tenant_cli(self.repo.baseline_slots, **kwargs),
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        ).comment_fingerprint

    def _ast(self, **kwargs: Any) -> str:
        return EG.tenant_cli_shape(
            synth_tenant_cli(self.repo.baseline_slots, **kwargs),
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        ).ast_fingerprint

    def test_identical_comment_text_moved_across_the_span_is_caught(self):
        """Same text, same line count, same AST — only the position moved."""

        text = "# audited: loader guard reviewed 2026-08"
        above = dict(header_comment=text, trailer_comment="")
        below = dict(header_comment="", trailer_comment=text)
        self.assertEqual(self._ast(**above), self._ast(**below), "AST must be blind to this")
        self.assertNotEqual(self._fp(**above), self._fp(**below))

    def test_noqa_moved_to_the_next_statement_is_caught(self):
        """The classic bypass: slide the suppression onto a different line."""

        before = self._fp(extra_body="AUDITED = 1  # noqa: E501\nCHECKED = 2")
        after = self._fp(extra_body="AUDITED = 1\nCHECKED = 2  # noqa: E501")
        self.assertNotEqual(before, after)

    def test_type_ignore_moved_to_the_next_statement_is_caught(self):
        before = self._fp(extra_body="AUDITED = 1  # type: ignore[assignment]\nCHECKED = 2")
        after = self._fp(extra_body="AUDITED = 1\nCHECKED = 2  # type: ignore[assignment]")
        self.assertNotEqual(before, after)

    def test_inline_to_own_line_move_is_caught(self):
        inline = self._fp(extra_body="AUDITED = 1  # reviewed\nCHECKED = 2")
        own_line = self._fp(extra_body="AUDITED = 1\n# reviewed\nCHECKED = 2")
        self.assertNotEqual(inline, own_line)

    def test_column_shift_alone_is_caught(self):
        near = self._fp(extra_body="AUDITED = 1  # reviewed")
        far = self._fp(extra_body="AUDITED = 1      # reviewed")
        self.assertNotEqual(near, far)

    def test_comment_deletion_is_caught(self):
        self.assertNotEqual(self._fp(), self._fp(trailer_comment=""))

    def test_comment_reordering_between_two_sites_is_caught(self):
        first, second = "# alpha note", "# beta note"
        self.assertNotEqual(
            self._fp(header_comment=first, trailer_comment=second),
            self._fp(header_comment=second, trailer_comment=first),
        )

    def test_appending_a_slot_keeps_the_comment_fingerprint(self):
        """Position normalisation: growth inside the span must be free.

        RT-019 and RT-026 each append one slot line.  If that rotated the
        fingerprint, every owner RT would have to refresh the central pin —
        which is exactly the escape hatch this guard must not have.
        """

        base = EG.tenant_cli_shape(
            synth_tenant_cli(self.repo.baseline_slots),
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        )
        for extra in (
            ["cwk_tenant_cmd_profile"],
            ["cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"],
        ):
            with self.subTest(added=len(extra)):
                grown = EG.tenant_cli_shape(
                    synth_tenant_cli(list(self.repo.baseline_slots) + extra),
                    slot_symbol="FROZEN_PROVIDER_SLOTS",
                    slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                    max_span_lines=16,
                )
                self.assertEqual(base.comment_fingerprint, grown.comment_fingerprint)
                self.assertEqual(base.ast_fingerprint, grown.ast_fingerprint)
                self.assertGreater(grown.span[1], base.span[1], "the span really did grow")

    def test_comments_inside_the_span_stay_unpinned(self):
        """In-span comments are the RT's scratch space, by design."""

        base = self._fp()
        for variant in (
            "    # RT-019 landed its slot here",
            "    # TODO(RT-026): release command",
            "",
        ):
            with self.subTest(span_comment=variant):
                self.assertEqual(base, self._fp(span_comment=variant))

    def test_source_that_does_not_tokenise_is_rejected(self):
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG.tenant_cli_shape(
                'FROZEN_PROVIDER_SLOTS: tuple[str, ...] = (\n    "cwk_tenant_cmd_core",\n)\n'
                'X = "unterminated\n',
                slot_symbol="FROZEN_PROVIDER_SLOTS",
                slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
                max_span_lines=16,
            )
        self.assertIn("does not parse", str(ctx.exception))

    def test_comment_move_is_caught_end_to_end_behind_a_receipt(self):
        """The full attack: a legal stage-3 diff that also relocates a comment."""

        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        self.repo.write(
            self.repo.tenant_cli_path,
            synth_tenant_cli(
                list(self.repo.baseline_slots) + ["cwk_tenant_cmd_profile"],
                header_comment="",
                trailer_comment="# module header comment (outside the slot span)",
            ),
        )
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        message = self.assertGuardFails(
            "comments outside the provider-slot assignment drifted", self.repo.verify
        )
        self.assertIn("Only comments inside the slot tuple may be edited", message)


# ---------------------------------------------------------------------------
# B1: fields the serialiser skips must provably hold their defaults
# ---------------------------------------------------------------------------


class AstSkippedFieldTests(GuardTestCase):
    """PEP 695 ``type_params`` is a fingerprint bypass if it is merely skipped.

    On 3.12+ ``class C[T]:`` and ``def f[T]():`` carry their whole generic
    signature in ``type_params``, a field the frozen ``{node_type: fields}``
    table does not serialise.  Rewriting ``class E(RuntimeError)`` into
    ``class E[T](RuntimeError)`` would therefore have kept the fingerprint
    byte-identical.  The guard now demands the field hold its default instead
    of skipping it blindly, so the file simply may not use generics.
    """

    def _shape(self, text: str) -> EG.TenantCliShape:
        return EG.tenant_cli_shape(
            text,
            slot_symbol="FROZEN_PROVIDER_SLOTS",
            slot_name_pattern=self.repo.policy_raw["tenant_cli"]["slot_name_pattern"],
            max_span_lines=16,
        )

    def _assert_generic_rejected(self, body: str) -> None:
        """Rejected on every supported version, for a version-appropriate reason.

        3.11 cannot even parse PEP 695 syntax, so it fails at ``ast.parse``;
        3.12+ parses it and must then fail the default-only field check.  Both
        are ``ScriptEvolutionError``, which is what makes this deterministic.
        """

        text = synth_tenant_cli(self.repo.baseline_slots, extra_body=body)
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            self._shape(text)
        message = str(ctx.exception)
        if sys.version_info >= (3, 12):
            self.assertIn("sets type_params=", message)
            self.assertIn("PEP 695", message)
        else:
            self.assertIn("does not parse", message)

    def test_generic_class_rejected(self):
        self._assert_generic_rejected("class Boxed[T]:\n    pass")

    def test_generic_function_rejected(self):
        self._assert_generic_rejected("def first[T](items: list[T]) -> T:\n    return items[0]")

    def test_generic_error_class_cannot_hide_behind_the_fingerprint(self):
        """The concrete bypass: re-declaring the loader's error class as generic."""

        base = self._shape(synth_tenant_cli(self.repo.baseline_slots)).ast_fingerprint
        generic = synth_tenant_cli(self.repo.baseline_slots).replace(
            "class ProviderLoadError(RuntimeError):",
            "class ProviderLoadError[T](RuntimeError):",
        )
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            self._shape(generic)
        self.assertIsInstance(base, str)
        message = str(ctx.exception)
        self.assertTrue(
            "sets type_params=" in message or "does not parse" in message, message
        )

    def _assert_verify_rejects_generic(self, body: str) -> None:
        """Smuggle the generic in behind a *legitimate* stage-3 receipt.

        Without the receipt the plain SHA gate would reject it first, which
        would prove nothing about the fingerprint.  Re-pointing the receipt's
        ``to_sha256`` at the tampered bytes is exactly the move the fingerprint
        exists to catch.
        """

        stage = self.repo.stage(3)
        self.repo.add_receipt(3)
        self.repo.write(
            self.repo.tenant_cli_path,
            synth_tenant_cli(
                list(self.repo.baseline_slots) + ["cwk_tenant_cmd_profile"], extra_body=body
            ),
        )
        receipt = json.loads(self.repo.read(stage["receipt_path"]).decode("utf-8"))
        receipt["to_sha256"] = self.repo.sha(self.repo.tenant_cli_path)
        self.repo.write(stage["receipt_path"], _dump(receipt))
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            self.repo.verify()
        message = str(ctx.exception)
        needle = "sets type_params=" if sys.version_info >= (3, 12) else "does not parse"
        self.assertIn(needle, message)

    def test_generic_class_drift_is_caught_end_to_end(self):
        self._assert_verify_rejects_generic("class Boxed[T]:\n    pass")

    def test_generic_function_drift_is_caught_end_to_end(self):
        self._assert_verify_rejects_generic("def ident[T](x: T) -> T:\n    return x")

    def test_every_skipped_field_has_a_declared_default(self):
        """A field may only be omitted from the fingerprint if it is checked."""

        self.assertEqual(
            sorted(EG._AST_DEFAULT_ONLY_FIELDS), ["type_comment", "type_params"]
        )
        self.assertIsNone(EG._AST_DEFAULT_ONLY_FIELDS["type_comment"])
        self.assertEqual(EG._AST_DEFAULT_ONLY_FIELDS["type_params"], [])

    def test_default_only_check_rejects_a_non_default_type_comment(self):
        node = ast.parse("x = 1").body[0]
        self.assertIn("type_comment", node._fields)
        node.type_comment = "int"
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG._assert_default_only_fields(node, "Assign")
        self.assertIn("sets type_comment='int'", str(ctx.exception))

    def test_default_only_check_rejects_a_non_default_type_params(self):
        node = ast.parse("class C: pass").body[0]
        if "type_params" not in node._fields:
            self.skipTest("this Python has no PEP 695 type_params field")
        node.type_params = [ast.TypeVar(name="T", bound=None)]
        with self.assertRaises(EG.ScriptEvolutionError) as ctx:
            EG._assert_default_only_fields(node, "ClassDef")
        self.assertIn("sets type_params=", str(ctx.exception))

    def test_default_only_check_passes_on_the_real_baseline(self):
        for node in ast.walk(
            ast.parse(EG.read_required_bytes(_REAL_ROOT, "scripts/cwk_tenant_cli.py").decode())
        ):
            EG._assert_default_only_fields(node, type(node).__name__)

    def test_type_comment_is_invisible_because_we_never_request_it(self):
        """We parse with ``type_comments=False``, so the field stays default."""

        tree = ast.parse("x = 1  # type: int")
        self.assertIsNone(getattr(tree.body[0], "type_comment", None))
        EG._assert_default_only_fields(tree.body[0], "Assign")

    def test_pinned_fingerprints_are_stable_across_python_versions(self):
        """B1 regression bar: the pins must not be a 3.11-only accident."""

        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_HERE)!r})\n"
            "import pr001_script_evolution_guard as EG\n"
            "from pathlib import Path\n"
            f"root = Path({str(_REAL_ROOT)!r})\n"
            "shape = EG.tenant_cli_shape(\n"
            "    EG.read_required_bytes(root, 'scripts/cwk_tenant_cli.py').decode('utf-8'),\n"
            "    slot_symbol='FROZEN_PROVIDER_SLOTS',\n"
            "    slot_name_pattern=r'\\Acwk_tenant_cmd_[a-z][a-z0-9_]{0,47}\\Z',\n"
            "    max_span_lines=16,\n"
            ")\n"
            "print(shape.ast_fingerprint, shape.comment_fingerprint)\n"
        )
        checked = 0
        for version in ("3.11", "3.12", "3.13", "3.14"):
            interpreter = shutil.which(f"python{version}")
            if interpreter is None:
                continue
            proc = subprocess.run(
                [interpreter, "-c", script],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(_HERE),
            )
            self.assertEqual(proc.returncode, 0, f"python{version}: {proc.stderr}")
            ast_fp, comment_fp = proc.stdout.split()
            self.assertEqual(
                ast_fp, EG.PINNED_TENANT_CLI_AST_FINGERPRINT, f"python{version} AST fingerprint"
            )
            self.assertEqual(
                comment_fp,
                EG.PINNED_TENANT_CLI_COMMENT_FINGERPRINT,
                f"python{version} comment fingerprint",
            )
            checked += 1
        self.assertGreaterEqual(checked, 1, "no supported interpreter found on PATH")


# ---------------------------------------------------------------------------
# Cross-RT ordering
# ---------------------------------------------------------------------------


class RT026OrderTests(GuardTestCase):
    def test_rt026_cannot_skip_rt019_tenant_cli_stage(self):
        """Stage 7 (RT-026) declares requires_stage_index 3 (RT-019)."""

        stage3, stage7 = self.repo.stage(3), self.repo.stage(7)
        self.repo.add_receipt(3)
        self.repo.add_receipt(7)
        self.repo.refresh_fingerprints()
        self.repo.verify()  # both present: legal

        (self.root / stage3["receipt_path"]).unlink()
        message = self.assertGuardFails("requires stage 3 (RT-019)", self.repo.verify)
        self.assertIn(stage7["target_path"], message)
        self.assertIn("has no receipt yet", message)
        self.assertNotIn("is gapped", message)
        self.assertEqual(stage3["owner_rt"], "RT-019")

    def test_requires_stage_index_branch_runs_inside_verify(self):
        """M4: the ``requires_stage_index`` branch must be *executed*, not just declared.

        Asserting on the policy field alone left the branch dead — per-path
        ordinal contiguity always tripped the gap rule first.  This drives it
        through ``verify()`` and pins the message that only that branch emits.
        """

        policy = self.repo.load()
        self.assertEqual(policy.stage_by_index(7)["requires_stage_index"], 3)

        # Stage 7's receipt alone: the requires-rule is the *first* thing to
        # reject it, and the error must identify RT-019 as the missing owner.
        self.repo.add_receipt(7)
        message = self.assertGuardFails("requires stage 3 (RT-019)", self.repo.verify)
        self.assertIn("scripts/cwk_tenant_cli.py", message)

    def test_requires_stage_index_null_falls_through_to_the_gap_rule(self):
        """The other side of the same branch stays live."""

        raw = copy.deepcopy(self.repo.policy_raw)
        for stage in raw["stages"]:
            if stage["stage_index"] == 7:
                self.assertEqual(stage["requires_stage_index"], 3)
                stage["requires_stage_index"] = None
        self.repo.write_policy(raw)
        policy = self.repo.load()
        self.assertIsNone(policy.stage_by_index(7)["requires_stage_index"])

        self.repo.add_receipt(7)
        self.assertGuardFails("is gapped", self.repo.verify)

    def test_ordered_tenant_cli_stages_pass(self):
        self.repo.add_receipt(3)
        self.repo.add_receipt(7)
        self.repo.refresh_fingerprints()
        report = self.repo.verify()
        self.assertEqual(
            report.tenant_cli_slots,
            self.repo.baseline_slots + ("cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"),
        )

    def test_independent_paths_may_land_out_of_stage_order(self):
        """Per-path prefix closure: RT-022's stage 6 may land before stage 1."""

        self.repo.add_receipt(6)
        report = self.repo.verify()
        self.assertEqual(report.receipt_count, 1)


# ---------------------------------------------------------------------------
# The real worktree
# ---------------------------------------------------------------------------


class Rt016ReaderIntegrationTests(GuardTestCase):
    """M3: the RT-016 zero-drift test must not re-open the hole it closed.

    ``assert_frozen_baseline`` reads the 26 genesis paths through the guard's
    dir-fd reader, but the *rest* of ``test_rt016_schemas.py`` was still using
    ``Path.exists`` / ``read_bytes`` / ``iterdir`` / ``is_file``, all of which
    follow symlinks and none of which look at ``st_nlink``.  A symlink or hard
    link with identical bytes therefore satisfied every SHA assertion.
    """

    _UNSAFE_ATTRS = frozenset({"read_bytes", "read_text", "exists", "iterdir", "is_file", "is_dir"})
    _HARDENED_CLASSES = ("FrozenFilesZeroDriftTests", "FrozenBaselineExactSetTests")

    def test_frozen_file_checks_never_use_symlink_following_apis(self):
        source = EG.read_required_bytes(_REAL_ROOT, "tests/test_rt016_schemas.py").decode("utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in self._HARDENED_CLASSES:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in self._UNSAFE_ATTRS:
                    offenders.append(f"{node.name}: .{sub.attr}() at line {sub.lineno}")
        self.assertEqual(
            offenders,
            [],
            "these APIs follow symlinks and ignore st_nlink; frozen-file checks "
            "must go through EG.read_checked_bytes / EG._list_dir instead",
        )

    def test_symlink_with_identical_bytes_is_rejected(self):
        """Same SHA, different file — the old reader could not tell."""

        real = self.root / "scripts" / "cwk_frozen_01.py"
        alias = self.root / "scripts" / "cwk_alias_01.py"
        alias.symlink_to(real)
        self.assertEqual(alias.read_bytes(), real.read_bytes(), "bytes really are identical")
        self.assertEqual(
            hashlib.sha256(alias.read_bytes()).hexdigest(),
            EG.file_sha256(self.root, "scripts/cwk_frozen_01.py"),
        )
        self.assertGuardFails(
            "is a symlink", EG.read_checked_bytes, self.root, "scripts/cwk_alias_01.py"
        )

    @unittest.skipUnless(hasattr(os, "link"), "platform cannot create hard links")
    def test_hard_link_with_identical_bytes_is_rejected(self):
        real = self.root / "scripts" / "cwk_frozen_01.py"
        try:
            os.link(real, self.root / "scripts" / "cwk_hard_01.py")
        except (OSError, NotImplementedError):
            self.skipTest("filesystem does not support hard links")
        self.assertGuardFails(
            "hard links", EG.read_checked_bytes, self.root, "scripts/cwk_hard_01.py"
        )
        # ...and the original now fails too: a pinned file gaining a second
        # link is itself the drift signal.
        self.assertGuardFails(
            "hard links", EG.read_checked_bytes, self.root, "scripts/cwk_frozen_01.py"
        )

    def test_list_dir_rejects_a_symlinked_directory(self):
        (self.root / "linkdir").symlink_to(self.root / "scripts", target_is_directory=True)
        self.assertGuardFails("is a symlink", EG._list_dir, self.root, ("linkdir",), label="probe")

    def test_list_dir_returns_lstat_so_symlinked_entries_are_visible(self):
        (self.root / "scripts" / "cwk_alias_01.py").symlink_to(
            self.root / "scripts" / "cwk_frozen_01.py"
        )
        entries = dict(EG._list_dir(self.root, ("scripts",), label="probe"))
        self.assertIn("cwk_alias_01.py", entries)
        self.assertTrue(stat_module.S_ISLNK(entries["cwk_alias_01.py"].st_mode))
        self.assertFalse(stat_module.S_ISREG(entries["cwk_alias_01.py"].st_mode))
        self.assertTrue(stat_module.S_ISREG(entries["cwk_frozen_01.py"].st_mode))

    def test_list_dir_of_a_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(EG._list_dir(self.root, ("nope",), label="probe"), [])

    def test_list_dir_of_a_file_is_rejected(self):
        self.assertGuardFails(
            "not a directory",
            EG._list_dir,
            self.root,
            ("scripts", "cwk_frozen_01.py"),
            label="probe",
        )


class LiveRepoInvariantTests(GuardTestCase):
    """Proof that ``assert_repo_invariants`` survives every legal future state.

    ``RealRepoTests`` runs the same function against the real worktree, which
    may hold any legal receipt prefix.  These synthetic runs land the receipts the
    owner RTs will actually append — one path, several independent paths, the
    ordered tenant-CLI pair, and finally all ten stages — and show the
    central assertions still hold without being edited.  That is what lets
    RT-017/019/021/022/026 append their declared receipts without touching
    this file or the guard helper.
    """

    def _check(self) -> EG.Report:
        policy = self.repo.load()
        report = self.repo.verify(policy=policy)
        assert_repo_invariants(self, self.root, self.repo.genesis, policy, report)
        return report

    def test_invariants_hold_at_wave_0_with_no_receipts(self):
        report = self._check()
        self.assertEqual(report.receipt_count, 0)
        self.assertEqual(report.tenant_cli_slots, self.repo.baseline_slots)

    def test_invariants_hold_after_a_single_receipt(self):
        self.repo.add_receipt(1)
        report = self._check()
        self.assertEqual(report.receipt_count, 1)

    def test_invariants_hold_after_receipts_on_several_independent_paths(self):
        """RT-017 (x2), RT-019 and RT-022 land; RT-021 and RT-026 have not."""

        for index in (1, 2, 3, 6):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        report = self._check()
        self.assertEqual(report.receipt_count, 4)
        self.assertEqual(
            report.tenant_cli_slots, self.repo.baseline_slots + ("cwk_tenant_cmd_profile",)
        )

    def test_invariants_hold_after_the_ordered_tenant_cli_pair(self):
        for index in (3, 7):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        report = self._check()
        self.assertEqual(
            report.tenant_cli_slots,
            self.repo.baseline_slots + ("cwk_tenant_cmd_profile", "cwk_tenant_cmd_release"),
        )

    def test_invariants_hold_after_every_declared_stage_lands(self):
        for index in range(1, 11):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        report = self._check()
        self.assertEqual(report.receipt_count, 10)
        self.assertEqual(report.immutable_count, 17)
        self.assertEqual(len(report.tips), 9)

    def test_invariants_reject_a_gapped_future_state(self):
        """The same helper must still fail when the chain is not a prefix."""

        for index in (3, 7):
            self.repo.add_receipt(index)
        self.repo.refresh_fingerprints()
        (self.root / self.repo.stage(3)["receipt_path"]).unlink()
        self.assertGuardFails("requires stage 3 (RT-019)", self.repo.verify)


class RealRepoTests(unittest.TestCase):
    def test_real_repo_passes_with_the_pinned_values(self):
        """State-independent: true at Wave-0's 0 receipts and at all 10."""

        policy = EG.load_policy(_REAL_ROOT)
        report = EG.assert_frozen_baseline(_REAL_ROOT, _REAL_GENESIS)
        assert_repo_invariants(self, _REAL_ROOT, _REAL_GENESIS, policy, report)

    def test_real_policy_declares_nine_paths_and_ten_stages(self):
        policy = EG.load_policy(_REAL_ROOT)
        self.assertEqual(len(policy.raw["evolvable_paths"]), 9)
        self.assertEqual(len(policy.stages), 10)
        self.assertEqual(
            sorted(policy.evolvable),
            [
                "scripts/cwk_access_ledger.py",
                "scripts/cwk_agent_binding.py",
                "scripts/cwk_collect_live.py",
                "scripts/cwk_entity_catalog.py",
                "scripts/cwk_instance.py",
                "scripts/cwk_nightly_pipeline.py",
                "scripts/cwk_tenant_cli.py",
                "scripts/cwk_wiki_query.py",
                "scripts/cwk_wiki_search_index.py",
            ],
        )
        self.assertEqual(
            [(s["stage_index"], s["owner_rt"], s["target_path"], s["ordinal"]) for s in policy.stages],
            [
                (1, "RT-017", "scripts/cwk_access_ledger.py", 1),
                (2, "RT-017", "scripts/cwk_collect_live.py", 1),
                (3, "RT-019", "scripts/cwk_tenant_cli.py", 1),
                (4, "RT-021", "scripts/cwk_entity_catalog.py", 1),
                (5, "RT-021", "scripts/cwk_wiki_search_index.py", 1),
                (6, "RT-022", "scripts/cwk_wiki_query.py", 1),
                (7, "RT-026", "scripts/cwk_tenant_cli.py", 2),
                (8, "RT-026", "scripts/cwk_nightly_pipeline.py", 1),
                (9, "RT-012", "scripts/cwk_instance.py", 1),
                (10, "RT-013", "scripts/cwk_agent_binding.py", 1),
            ],
        )
        self.assertEqual(policy.stage_by_index(7)["requires_stage_index"], 3)
        self.assertEqual(policy.stage_by_index(3)["adds_provider_slot"], "cwk_tenant_cmd_profile")
        self.assertEqual(policy.stage_by_index(7)["adds_provider_slot"], "cwk_tenant_cmd_release")

    def test_real_tenant_cli_fingerprints_match_the_pins(self):
        """The fingerprints are slot-invariant by construction, so they stay pinned.

        The slot *list*, by contrast, grows as RT-019 and RT-026 land, so it
        is checked against the policy + the receipts on disk rather than
        against a frozen Wave-0 tuple.
        """

        policy = EG.load_policy(_REAL_ROOT)
        spec = policy.tenant_cli
        shape = EG.tenant_cli_shape(
            EG.read_required_bytes(_REAL_ROOT, spec["target_path"]).decode("utf-8"),
            slot_symbol=spec["slot_symbol"],
            slot_name_pattern=spec["slot_name_pattern"],
            max_span_lines=spec["max_slot_span_lines"],
        )
        self.assertEqual(shape.ast_fingerprint, EG.PINNED_TENANT_CLI_AST_FINGERPRINT)
        self.assertEqual(shape.comment_fingerprint, EG.PINNED_TENANT_CLI_COMMENT_FINGERPRINT)
        self.assertEqual(
            shape.slots, expected_slots_for(policy, receipts_on_disk(_REAL_ROOT, policy))
        )
        self.assertEqual(shape.slots[: len(spec["baseline_slots"])], tuple(spec["baseline_slots"]))

    def test_guard_helper_sha_matches_its_pin(self):
        actual = EG.file_sha256(_REAL_ROOT, "tests/pr001_script_evolution_guard.py")
        self.assertEqual(
            actual,
            _GUARD_HELPER_SHA256,
            "tests/pr001_script_evolution_guard.py changed; a later RT must not refresh "
            "this pin — the helper is frozen at Wave-0.",
        )

    def test_central_contract_shas_match_their_pins(self):
        self.assertEqual(EG.file_sha256(_REAL_ROOT, EG.POLICY_REL), EG.PINNED_POLICY_SHA256)
        self.assertEqual(
            EG.file_sha256(_REAL_ROOT, EG.POLICY_SCHEMA_REL), EG.PINNED_POLICY_SCHEMA_SHA256
        )
        self.assertEqual(
            EG.file_sha256(_REAL_ROOT, EG.RECEIPT_SCHEMA_REL), EG.PINNED_RECEIPT_SCHEMA_SHA256
        )

    def test_real_repo_receipts_are_all_policy_declared(self):
        """Accepts 0..10 receipts; rejects any file the policy did not predeclare.

        The real tree may hold any policy-declared prefix.  When RT-012/013/
        017/019/021/022/026 append their declared receipts this test keeps passing
        unchanged, which is the point: a downstream RT must never need to edit
        the central guard or rename a central test in order to ship.
        """

        policy = EG.load_policy(_REAL_ROOT)
        declared = {stage["receipt_path"] for stage in policy.stages}
        self.assertEqual(len(declared), 10)

        found = {
            str(p.relative_to(_REAL_ROOT)).replace(os.sep, "/")
            for p in (_REAL_ROOT / "RT").glob("*/receipts/script-evolution/*")
        }
        undeclared = sorted(found - declared)
        self.assertEqual(undeclared, [], "receipt files outside the policy's declared set")
        # ``found`` is a set, so ``sorted`` yields alphabetical order, while
        # ``receipts_on_disk`` yields policy *stage* order.  Those two orders
        # agreed only by coincidence while stage-09 (RT-012) and stage-10
        # (RT-013) were the sole receipts on disk.  The first legitimate
        # stage 1..8 receipt breaks the coincidence -- RT-022's stage-06 sorts
        # first by stage but last by path -- which would contradict this test's
        # own docstring promise that it "keeps passing unchanged" when
        # RT-012/013/017/019/021/022/026 append their declared receipts.
        #
        # Compare the two declared sets in one deterministic order.  Set
        # equality is unchanged, the ``undeclared`` assertion above still
        # rejects any receipt the policy did not predeclare, and per-path stage
        # ordering remains enforced by ``replay_chain`` through its closed-prefix
        # gap rule and ``from_sha256`` chaining.
        self.assertEqual(sorted(found), sorted(receipts_on_disk(_REAL_ROOT, policy)))

        # The current state is derived, never frozen to a particular count.
        report = EG.verify_repo(_REAL_ROOT, _REAL_GENESIS)
        self.assertEqual(report.receipt_count, len(found))
        if not found:
            for rel in policy.evolvable:
                self.assertEqual(report.tips[rel], _REAL_GENESIS[rel], rel)

    def test_guard_disclaims_cryptographic_tamper_proofing(self):
        doc = EG.__doc__ or ""
        self.assertIn("not cryptographic", doc.replace("\n", " "))
        self.assertIn("independent", doc)
        self.assertIn("diff review", doc.replace("\n", " "))

    def test_attack_surface_matches_the_frozen_required_set(self):
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(sys.modules[__name__])
        self.assertEqual(getattr(loader, "errors", []), [])
        collected: set[str] = set()

        def walk(item: Any) -> None:
            if isinstance(item, unittest.TestSuite):
                for child in item:
                    walk(child)
            else:
                collected.add(f"{type(item).__name__}.{item._testMethodName}")

        walk(suite)
        missing = sorted(EG.REQUIRED_ATTACK_TEST_NAMES - collected)
        extra = sorted(collected - EG.REQUIRED_ATTACK_TEST_NAMES)
        self.assertEqual(
            (missing, extra),
            ([], []),
            "The negative-test surface is frozen in "
            "pr001_script_evolution_guard.REQUIRED_ATTACK_TEST_NAMES so that coverage "
            "cannot be quietly deleted.  Deleting a test requires a deliberate, "
            "reviewable edit to that set.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
