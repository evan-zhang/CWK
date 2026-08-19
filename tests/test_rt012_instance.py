"""RT-012: unit tests for the InstanceLayout / TenantLayout resolver."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_atomic_file as A  # noqa: E402
import cwk_instance as I  # noqa: E402


class EnvResolverTests(unittest.TestCase):
    """CWK_INSTANCE_ROOT must be explicit, absolute, non-empty, and safe.

    Every rejection path is documented in PR-001 plan §RT-012 and must
    fail closed — never fall back to repo runs/state/.env.
    """

    def setUp(self) -> None:
        self._saved = os.environ.pop(I.ENV_VAR, None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ[I.ENV_VAR] = self._saved
        else:
            os.environ.pop(I.ENV_VAR, None)

    def test_unset_fails_closed(self):
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_empty_fails_closed(self):
        os.environ[I.ENV_VAR] = ""
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_relative_fails_closed(self):
        os.environ[I.ENV_VAR] = "relative/path"
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_whitespace_fails_closed(self):
        os.environ[I.ENV_VAR] = "  /tmp/foo  "
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_nul_fails_closed(self):
        # Python's os.environ refuses to set NUL-containing values, which
        # itself closes this attack at the OS boundary.  We also verify the
        # resolver defends when a caller bypasses that (via ``os.environb``
        # or a C-level setenv from a native module).
        with self.assertRaises(ValueError):
            os.environ[I.ENV_VAR] = "/tmp/\x00evil"
        # Direct resolver-level test using monkey-patched env dict.
        import unittest.mock as m

        with m.patch.dict(os.environ, {I.ENV_VAR: "/tmp/foo/bar"}, clear=False):
            # Simulate the bypass by monkey-patching os.environ.get to return
            # a NUL-tainted value.
            original = os.environ.get

            def fake_get(name, default=None):
                if name == I.ENV_VAR:
                    return "/tmp/\x00evil"
                return original(name, default)

            with m.patch.object(os.environ, "get", side_effect=fake_get):
                with self.assertRaises(I.InstanceRootError):
                    I.resolve_instance_root()

    def test_crlf_fails_closed(self):
        os.environ[I.ENV_VAR] = "/tmp/foo\n"
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_backslash_fails_closed(self):
        os.environ[I.ENV_VAR] = r"C:\Windows\Temp"
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_encoded_traversal_fails_closed(self):
        os.environ[I.ENV_VAR] = "/tmp/%2e%2e/root"
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_symlink_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            os.environ[I.ENV_VAR] = str(link)
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()

    def test_missing_root_fails_closed(self):
        os.environ[I.ENV_VAR] = "/definitely/does/not/exist/rt012"
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_non_dir_root_fails_closed(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            os.environ[I.ENV_VAR] = path
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()
        finally:
            os.unlink(path)

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[I.ENV_VAR] = tmp
            self.assertEqual(I.resolve_instance_root(), tmp)


class TenantIdTests(unittest.TestCase):
    def test_valid_id(self):
        self.assertEqual(I.validate_tenant_id("t_" + "a" * 26), "t_" + "a" * 26)

    def test_traversal_variants(self):
        for bad in ("../../../etc/passwd", "t_../foo", "t_%2e%2e", "T_" + "a" * 26):
            with self.assertRaises(I.TenantIdError):
                I.validate_tenant_id(bad)

    def test_trailing_newline_rejected(self):
        with self.assertRaises(I.TenantIdError):
            I.validate_tenant_id("t_" + "a" * 26 + "\n")

    def test_uppercase_rejected(self):
        with self.assertRaises(I.TenantIdError):
            I.validate_tenant_id("t_" + "A" * 26)

    def test_wrong_prefix_rejected(self):
        with self.assertRaises(I.TenantIdError):
            I.validate_tenant_id("sp_" + "a" * 26)


class InstanceLayoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        os.environ[I.ENV_VAR] = self.tmp
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)

    def test_initialize_creates_all_children(self):
        with self.layout.root_fd() as rfd:
            names = {e.name for e in os.scandir(rfd)}
        for expected in I.INSTANCE_ROOT_CHILDREN:
            self.assertIn(expected, names)

    def test_initialize_is_idempotent(self):
        self.layout.initialize()  # again
        with self.layout.root_fd() as rfd:
            names = {e.name for e in os.scandir(rfd)}
        for expected in I.INSTANCE_ROOT_CHILDREN:
            self.assertIn(expected, names)

    def test_top_level_perms_are_0o700(self):
        with self.layout.root_fd() as rfd:
            for name in I.INSTANCE_ROOT_CHILDREN:
                st = os.stat(name, dir_fd=rfd, follow_symlinks=False)
                self.assertEqual(stat.S_IMODE(st.st_mode), 0o700, name)

    def test_child_fd_rejects_unknown_leaf(self):
        with self.assertRaises(I.LayoutError):
            with self.layout.child_fd("banana"):
                pass

    def test_child_fd_rejects_symlink(self):
        # Replace `shared` with a symlink and confirm access is refused.
        with self.layout.root_fd() as rfd:
            os.rmdir("shared", dir_fd=rfd)
            os.symlink("../outside", "shared", dir_fd=rfd)
        with self.assertRaises(I.LayoutError):
            with self.layout.child_fd("shared"):
                pass

    def test_tenant_initialize_creates_all_children(self):
        tid = "t_" + "b" * 26
        tenant = self.layout.tenant(tid)
        tenant.initialize()
        with tenant.tenant_fd() as tfd:
            names = {e.name for e in os.scandir(tfd)}
        for expected in I.TENANT_CHILDREN:
            self.assertIn(expected, names)

    def test_tenant_child_perms_are_0o700(self):
        tid = "t_" + "c" * 26
        tenant = self.layout.tenant(tid)
        tenant.initialize()
        with tenant.tenant_fd() as tfd:
            for name in I.TENANT_CHILDREN:
                st = os.stat(name, dir_fd=tfd, follow_symlinks=False)
                self.assertEqual(stat.S_IMODE(st.st_mode), 0o700, name)

    def test_tenant_rejects_slug_leaf(self):
        tenant = self.layout.tenant("t_" + "d" * 26)
        tenant.initialize()
        with self.assertRaises(I.LayoutError):
            with tenant.child_fd("badname"):
                pass

    def test_layout_descriptor_matches_frozen(self):
        d = I.frozen_layout_descriptor()
        self.assertEqual(d["schema"], "cwk.rt012.instance_layout.v1")
        self.assertEqual(tuple(d["instance_root_children"]), I.INSTANCE_ROOT_CHILDREN)
        self.assertEqual(tuple(d["tenant_children"]), I.TENANT_CHILDREN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
