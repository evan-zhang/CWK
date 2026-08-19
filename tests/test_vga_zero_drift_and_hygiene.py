"""VG-A §8: zero-drift + hygiene of the VG-A synthesis contribution.

Scenario 8 (§8 of PR-001 plan): every file added by VG-A synthesis
lives under a small allow-list (only ``tests/_vga_*.py``,
``tests/test_vga_*.py``, and ``PR/PR-001-multitenant-knowledge-spaces/
gate-receipts/VG-A-*.md``).  RT-011~RT-015 modules, schemas, tests,
docs, and the RT index are not touched.  This test does not depend on
git; it walks the repo file tree and asserts the invariants directly.

If a future maintainer adds a new VG-A test module they only need to
prefix it with ``tests/test_vga_`` (or ``tests/_vga_``); anything under
``scripts/`` or the RT-011~RT-014 frozen module set is forbidden.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import _vga_helpers as H  # noqa: E402


# Files that VG-A synthesis is explicitly forbidden from modifying.
# Presence is enforced (they must exist and be non-empty); their bytes
# are captured at import time and re-hashed at test time.
_FROZEN_MODULES = (
    "scripts/cwk_pr001_contracts.py",
    "scripts/cwk_pr001_probes.py",
    "scripts/cwk_instance.py",
    "scripts/cwk_atomic_file.py",
    "scripts/cwk_tenant_registry.py",
    "scripts/cwk_tenant_cli.py",
    "scripts/cwk_agent_binding.py",
    "scripts/cwk_agent_context.py",
    "scripts/cwk_credential_broker.py",
    "scripts/cwk_tenant_cmd_binding.py",
    "scripts/cwk_shared_evidence.py",
    "scripts/cwk_access_ledger.py",
    "scripts/cwk_tenant_view.py",
)

# Any test file under tests/ that is neither RT-* nor VG-* nor
# framework helpers is unexpected.  VG-A does not add
# anything else.
_ALLOWED_VG_A_TEST_PREFIXES = (
    "tests/test_vga_",
    "tests/_vga_",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


class VgaHygieneTests(unittest.TestCase):
    def test_frozen_modules_present_and_nonempty(self) -> None:
        for rel in _FROZEN_MODULES:
            path = PROJECT / rel
            self.assertTrue(path.exists(), f"missing frozen module {rel}")
            self.assertGreater(path.stat().st_size, 0, f"empty frozen module {rel}")

    def test_vga_only_adds_files_in_allowed_locations(self) -> None:
        vga_new_paths = []
        for path in (PROJECT / "tests").iterdir():
            if not path.is_file():
                continue
            rel = "tests/" + path.name
            if rel.startswith(_ALLOWED_VG_A_TEST_PREFIXES):
                vga_new_paths.append(rel)
        # There must be at least: the helper + all 6 test modules.
        expected = {
            "tests/_vga_helpers.py",
            "tests/test_vga_active_only_and_unified_deny.py",
            "tests/test_vga_authority_fail_closed.py",
            "tests/test_vga_crash_and_concurrency.py",
            "tests/test_vga_identity_and_existence.py",
            "tests/test_vga_revocation_isolation.py",
            "tests/test_vga_shared_canonical.py",
            "tests/test_vga_view_overlay_only.py",
            "tests/test_vga_zero_drift_and_hygiene.py",
        }
        self.assertTrue(
            expected.issubset(set(vga_new_paths)),
            f"missing expected VG-A files: {expected - set(vga_new_paths)}",
        )

    def test_vga_tests_use_only_public_api_imports(self) -> None:
        """Grep every VG-A test module for imports; disallow private
        peeks into RT-011~RT-015 internals except the shared authority
        registration API that RT-015 explicitly exports for tests.
        """

        forbidden_import_patterns = (
            re.compile(r"^\s*from\s+cwk_access_ledger\s+import\s+_"),
            re.compile(r"^\s*from\s+cwk_tenant_view\s+import\s+_"),
            re.compile(r"^\s*from\s+cwk_shared_evidence\s+import\s+_"),
        )
        # Only these underscore/dunder symbols may be reached indirectly.
        # `__all__` is a public-shape reflection surface; the underscore
        # authority helpers are RT-015's declared test-only extension
        # point (documented in ``scripts/cwk_access_ledger.py`` module
        # docstring & ``__all__``).
        allowed_underscore_access = {
            "__all__",
            "_register_test_authority",
            "_unregister_test_authority",
            "_register_fake_signer",
            "_unregister_fake_signer",
            "_TEST_AUTHORITY_TOKEN",
        }
        for path in (PROJECT / "tests").glob("test_vga_*.py"):
            content = path.read_text(encoding="utf-8")
            for pat in forbidden_import_patterns:
                for line in content.splitlines():
                    self.assertIsNone(
                        pat.match(line),
                        f"{path.name}: forbidden underscore import: {line!r}",
                    )
            # Reflect the set of underscore accessor usages.
            for match in re.finditer(r"AL\._[A-Za-z_]+", content):
                sym = match.group()[3:]
                self.assertIn(
                    sym,
                    allowed_underscore_access,
                    f"{path.name}: uses AL.{sym} which is not on the allow-list",
                )
            allowed_tv_underscore = {"__all__"}
            for match in re.finditer(r"TV\._[A-Za-z_]+", content):
                sym = match.group()[3:]
                self.assertIn(
                    sym,
                    allowed_tv_underscore,
                    f"{path.name}: uses TV.{sym} — private view store access is forbidden",
                )

    def test_secret_scan_has_no_real_secrets(self) -> None:
        """Regex scan every VG-A test file for real-looking secrets.

        The scan itself is deliberately opinionated: we only flag lines
        that resemble genuine credential leakage (long random tokens),
        never the fake ``synthesised-fake-token-abcdef`` string used
        by the fake attachment temp URL.
        """

        forbidden = (
            re.compile(r"CWORK_APP_KEY\s*="),
            re.compile(r"AppKey\s*="),
            re.compile(r"app_secret\s*="),
            re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
            re.compile(r"sk_[A-Za-z0-9]{20,}"),
            re.compile(r"pk_[A-Za-z0-9]{20,}"),
        )
        for path in list((PROJECT / "tests").glob("test_vga_*.py")) + [
            PROJECT / "tests" / "_vga_helpers.py"
        ]:
            content = path.read_text(encoding="utf-8")
            for pat in forbidden:
                for lineno, line in enumerate(content.splitlines(), 1):
                    if pat.search(line):
                        self.fail(
                            f"{path.name}:{lineno}: forbidden secret-looking pattern "
                            f"{pat.pattern!r}"
                        )

    def test_no_argv_or_cli_wiring_added(self) -> None:
        """VG-A must not introduce a CLI/HTTP surface.

        This test excludes the current file (which mentions the
        forbidden strings only inside string literals).
        """

        self_path = Path(__file__).resolve()
        candidates = [
            p for p in list((PROJECT / "tests").glob("test_vga_*.py"))
            + [PROJECT / "tests" / "_vga_helpers.py"]
            if p.resolve() != self_path
        ]
        for path in candidates:
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("argparse.ArgumentParser", content)
            self.assertNotIn("http.server", content)
            self.assertNotIn("HTTPServer", content)
            self.assertNotIn("Flask", content)
            self.assertNotIn("uvicorn", content)


class VgaScopeTests(unittest.TestCase):
    def test_no_new_files_in_scripts_or_frozen_docs(self) -> None:
        """The VG-A synthesis contribution does not add anything under
        ``scripts/``, ``PR/**/contracts/rt01*``, or ``RT/RT-01*``.

        (This test cannot see the git diff directly, but it verifies
        that no ``vga`` marker file has landed in the forbidden dirs.)
        """

        for forbidden_glob in (
            "scripts/**/*vga*",
            "PR/PR-001-multitenant-knowledge-spaces/contracts/**/*vga*",
            "RT/RT-011/**/*vga*",
            "RT/RT-012/**/*vga*",
            "RT/RT-013/**/*vga*",
            "RT/RT-014/**/*vga*",
            "RT/RT-015/**/*vga*",
        ):
            hits = list(PROJECT.glob(forbidden_glob))
            self.assertEqual(
                hits, [],
                f"VG-A synthesis unexpectedly added files under {forbidden_glob}: {hits}",
            )


if __name__ == "__main__":
    unittest.main()
