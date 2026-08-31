"""RT-012: unit tests for the InstanceLayout / TenantLayout resolver."""

from __future__ import annotations

import copy
import os
import pickle
import stat
import sys
import tempfile
import threading
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
            os.environ[I.ENV_VAR] = str(Path(path).resolve())
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()
        finally:
            os.unlink(path)

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = str(Path(tmp).resolve())
            os.environ[I.ENV_VAR] = resolved
            self.assertEqual(I.resolve_instance_root(), resolved)


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
        self.tmp = str(Path(self._tmp.name).resolve())
        os.environ[I.ENV_VAR] = self.tmp
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()

    def tearDown(self):
        self.layout.close()
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


class RootAnchorTests(unittest.TestCase):
    """The textual root may never retarget an existing layout handle."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _open_root(self, path: Path) -> I.InstanceLayout:
        path.mkdir(parents=True)
        return I.InstanceLayout.open(root=str(path))

    def test_ancestor_symlink_component_is_rejected(self):
        real_parent = self.base / "real-parent"
        (real_parent / "instance").mkdir(parents=True)
        link_parent = self.base / "link-parent"
        os.symlink(real_parent, link_parent)
        with self.assertRaises(I.InstanceRootError):
            I.InstanceLayout.open(root=str(link_parent / "instance"))

    def test_root_replacement_after_open_is_rejected(self):
        root = self.base / "instance"
        layout = self._open_root(root)
        try:
            root.rename(self.base / "original-instance")
            root.mkdir()
            with self.assertRaises(I.InstanceRootError):
                with layout.root_fd():
                    pass
        finally:
            layout.close()

    def test_ancestor_replacement_after_open_is_rejected(self):
        parent = self.base / "parent"
        root = parent / "instance"
        layout = self._open_root(root)
        try:
            parent.rename(self.base / "original-parent")
            (parent / "instance").mkdir(parents=True)
            with self.assertRaises(I.InstanceRootError):
                with layout.root_fd():
                    pass
        finally:
            layout.close()

    def test_racing_replacement_never_yields_replacement_root(self):
        pivot = self.base / "pivot"
        original_root = pivot / "instance"
        original_root.mkdir(parents=True)
        (original_root / "origin-marker").write_text("original", encoding="utf-8")
        replacement_parent = self.base / "replacement"
        replacement_root = replacement_parent / "instance"
        replacement_root.mkdir(parents=True)
        (replacement_root / "origin-marker").write_text("replacement", encoding="utf-8")
        parked = self.base / "parked-original"
        layout = I.InstanceLayout.open(root=str(original_root))
        stop = threading.Event()

        def swap() -> None:
            while not stop.is_set():
                try:
                    pivot.rename(parked)
                    os.symlink(replacement_parent, pivot)
                    os.unlink(pivot)
                    parked.rename(pivot)
                except FileNotFoundError:
                    continue

        worker = threading.Thread(target=swap, daemon=True)
        worker.start()
        observed: set[str] = set()
        rejected = 0
        try:
            for _ in range(200):
                try:
                    with layout.root_fd() as root_fd:
                        fd = os.open("origin-marker", os.O_RDONLY, dir_fd=root_fd)
                        try:
                            observed.add(os.read(fd, 32).decode("ascii"))
                        finally:
                            os.close(fd)
                except I.InstanceRootError:
                    rejected += 1
        finally:
            stop.set()
            worker.join(timeout=2)
            layout.close()
            if pivot.is_symlink():
                pivot.unlink()
            if parked.exists() and not pivot.exists():
                parked.rename(pivot)
        self.assertNotIn("replacement", observed)
        self.assertTrue(observed == {"original"} or rejected > 0)

    def test_repeated_root_fd_keeps_component_identity(self):
        layout = self._open_root(self.base / "instance")
        try:
            with layout.root_fd() as first:
                first_identity = (os.fstat(first).st_dev, os.fstat(first).st_ino)
            with layout.root_fd() as second:
                second_identity = (os.fstat(second).st_dev, os.fstat(second).st_ino)
            self.assertEqual(first_identity, second_identity)
        finally:
            layout.close()

    def test_close_is_idempotent_and_root_fd_fails_closed(self):
        layout = self._open_root(self.base / "instance")
        layout.close()
        layout.close()
        with self.assertRaisesRegex(I.InstanceRootError, "instance layout is closed"):
            with layout.root_fd():
                pass

    def test_context_manager_closes_anchor_fd(self):
        root = self.base / "instance"
        root.mkdir()
        with I.InstanceLayout.open(root=str(root)) as layout:
            with layout.root_fd() as fd:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        with self.assertRaises(I.InstanceRootError):
            with layout.root_fd():
                pass

    def test_root_fd_yield_does_not_hold_the_lifecycle_lock(self):
        layout = self._open_root(self.base / "instance")
        entered = threading.Event()
        release = threading.Event()
        close_done = threading.Event()
        identities: dict[str, tuple[int, int]] = {}
        errors: list[BaseException] = []

        def hold_root_fd() -> None:
            try:
                with layout.root_fd() as fd:
                    st = os.fstat(fd)
                    identities["before"] = (st.st_dev, st.st_ino)
                    entered.set()
                    release.wait(timeout=3)
                    st = os.fstat(fd)
                    identities["after"] = (st.st_dev, st.st_ino)
            except BaseException as exc:  # surfaced in the owning test thread
                errors.append(exc)

        def close_layout() -> None:
            try:
                layout.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_done.set()

        holder = threading.Thread(target=hold_root_fd, daemon=True)
        holder.start()
        self.assertTrue(entered.wait(timeout=2), "root_fd holder never entered")
        closer = threading.Thread(target=close_layout, daemon=True)
        closer.start()
        closed_while_yielded = close_done.wait(timeout=2)
        if closed_while_yielded:
            with self.assertRaisesRegex(I.InstanceRootError, "instance layout is closed"):
                with layout.root_fd():
                    pass
        release.set()
        holder.join(timeout=2)
        closer.join(timeout=2)
        self.assertTrue(closed_while_yielded, "close blocked behind a yielded root_fd")
        self.assertFalse(holder.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(identities["before"], identities["after"])

    def test_unrelated_sibling_change_preserves_anchor(self):
        layout = self._open_root(self.base / "instance")
        try:
            sibling = self.base / "sibling"
            sibling.mkdir()
            sibling.rename(self.base / "renamed-sibling")
            with layout.root_fd() as fd:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            layout.close()

    def test_copy_shares_the_same_explicit_lifecycle(self):
        layout = self._open_root(self.base / "instance")
        shallow = copy.copy(layout)
        deep = copy.deepcopy(layout)
        self.assertIs(shallow, layout)
        self.assertIs(deep, layout)
        layout.close()
        with self.assertRaises(I.InstanceRootError):
            with shallow.root_fd():
                pass

    def test_pickle_is_rejected_instead_of_duplicating_the_anchor(self):
        layout = self._open_root(self.base / "instance")
        try:
            with self.assertRaisesRegex(TypeError, "cannot be pickled"):
                pickle.dumps(layout)
        finally:
            layout.close()

    def test_dot_and_dotdot_root_components_are_rejected(self):
        root = self.base / "instance"
        root.mkdir()
        for candidate in (f"{self.base}/./instance", f"{self.base}/x/../instance"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(I.InstanceRootError):
                    I.InstanceLayout.open(root=candidate)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
