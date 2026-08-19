"""RT-012: unit tests for the atomic file primitives.

Every test targets a specific attack surface named in the RT-012 plan:
short writes, EINTR, TOCTOU, symlink defense, CAS conflict, lock
release-on-crash, orphan recovery, permission drift.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_atomic_file as A  # noqa: E402


class LeafValidationTests(unittest.TestCase):
    def test_reject_dot_and_dotdot(self):
        for name in (".", ".."):
            with self.assertRaises(A.ContainmentError):
                A._validate_leaf(name)

    def test_reject_path_separators(self):
        for name in ("a/b", "..\\c", "a\x00b"):
            with self.assertRaises(A.ContainmentError):
                A._validate_leaf(name)

    def test_reject_uppercase(self):
        with self.assertRaises(A.ContainmentError):
            A._validate_leaf("Foo.json")

    def test_reject_leading_hyphen(self):
        with self.assertRaises(A.ContainmentError):
            A._validate_leaf("-flag")

    def test_reject_overlong(self):
        with self.assertRaises(A.ContainmentError):
            A._validate_leaf("a" * 200)


class OpenDirNofollowTests(unittest.TestCase):
    def test_refuses_symlink_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            with self.assertRaises(A.ContainmentError):
                A.open_dir_nofollow(str(link))

    def test_refuses_missing(self):
        with self.assertRaises(A.ContainmentError):
            A.open_dir_nofollow("/nonexistent/definitely-not-here")

    def test_refuses_regular_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            with self.assertRaises(A.ContainmentError):
                A.open_dir_nofollow(path)
        finally:
            os.unlink(path)


class WriteAtomicTests(unittest.TestCase):
    def test_basic_roundtrip_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                r = A.write_atomic(fd, "foo.json", b"hello")
                self.assertEqual(r.size, 5)
                self.assertEqual(A.read_file(fd, "foo.json"), b"hello")
                st = os.stat("foo.json", dir_fd=fd, follow_symlinks=False)
                self.assertEqual(stat.S_IMODE(st.st_mode), A.FILE_MODE)
            finally:
                os.close(fd)

    def test_exclusive_write_raises_when_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                A.write_atomic(fd, "foo.json", b"x")
                with self.assertRaises(A.AtomicFileError) as cm:
                    A.write_atomic(fd, "foo.json", b"y", exclusive=True)
                self.assertEqual(cm.exception.code, "exists")
            finally:
                os.close(fd)

    def test_symlink_write_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                # Pre-create a symlink named 'foo.json' pointing to an
                # OUTSIDE canary; write_atomic (exclusive=True) must reject
                # because the child already exists (even as a symlink), and
                # the canary must be unchanged.
                outside_dir = tempfile.mkdtemp(prefix="rt012-canary-")
                outside = Path(outside_dir) / "outside.txt"
                outside.write_bytes(b"canary")
                os.symlink(str(outside), "foo.json", dir_fd=fd)
                try:
                    with self.assertRaises(A.AtomicFileError) as cm:
                        A.write_atomic(fd, "foo.json", b"tampered", exclusive=True)
                    self.assertEqual(cm.exception.code, "exists")
                    self.assertEqual(outside.read_bytes(), b"canary")
                finally:
                    outside.unlink(missing_ok=True)
                    os.rmdir(outside_dir)
            finally:
                os.close(fd)

    def test_read_file_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                # A symlink to a file inside the same dir.
                A.write_atomic(fd, "real.json", b"payload")
                os.symlink("real.json", "linky.json", dir_fd=fd)
                with self.assertRaises(A.ContainmentError):
                    # read_file rejects symlink via O_NOFOLLOW ELOOP.
                    A.read_file(fd, "linky.json")
            finally:
                os.close(fd)


class HardLinkTests(unittest.TestCase):
    def test_read_refuses_hard_linked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                A.write_atomic(fd, "foo.json", b"hi")
                # Create a hard link with a *fresh grammar-valid* name.
                os.link("foo.json", "foo2.json", src_dir_fd=fd, dst_dir_fd=fd)
                # Now nlink for both is 2; the reader must refuse.
                with self.assertRaises(A.ContainmentError):
                    A.read_file(fd, "foo.json")
            finally:
                os.close(fd)


class CasWriteTests(unittest.TestCase):
    def test_cas_matches_previous_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                r1 = A.write_atomic(fd, "state.json", b"v1")
                r2 = A.cas_write(
                    fd,
                    "state.json",
                    b"v2",
                    expected_previous_sha256=r1.sha256,
                )
                self.assertNotEqual(r2.sha256, r1.sha256)
            finally:
                os.close(fd)

    def test_cas_conflict_when_content_diverged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                r1 = A.write_atomic(fd, "state.json", b"v1")
                # Attacker sneaks in a new write.
                A.write_atomic(fd, "state.json", b"attacker")
                with self.assertRaises(A.RevisionConflict):
                    A.cas_write(
                        fd, "state.json", b"v2", expected_previous_sha256=r1.sha256
                    )
            finally:
                os.close(fd)

    def test_cas_missing_when_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                with self.assertRaises(A.RevisionConflict):
                    A.cas_write(
                        fd, "state.json", b"v", expected_previous_sha256="0" * 64
                    )
            finally:
                os.close(fd)


class LockTests(unittest.TestCase):
    def test_nonblocking_lock_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                # Spawn a subprocess that holds the lock for 2 seconds.
                child_code = (
                    "import os, sys, time, fcntl;"
                    f"import sys; sys.path.insert(0, {str(PROJECT / 'scripts')!r});"
                    "import cwk_atomic_file as A;"
                    f"fd=A.open_dir_nofollow({tmp!r});"
                    "cm=A.exclusive_lock(fd, 'proc.lock', blocking=True);"
                    "cm.__enter__();"
                    "print('holding', flush=True);"
                    "time.sleep(2);"
                    "cm.__exit__(None,None,None);"
                    "os.close(fd);"
                )
                proc = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-c", child_code],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(proc.stdout.close)
                try:
                    # Wait for child to acquire lock.
                    line = proc.stdout.readline()
                    self.assertIn("holding", line)
                    with self.assertRaises(A.LockUnavailable):
                        with A.exclusive_lock(fd, "proc.lock", blocking=False):
                            self.fail("should not have obtained the lock")
                finally:
                    proc.wait(timeout=5)
            finally:
                os.close(fd)

    def test_lock_released_on_process_death(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                child_code = (
                    "import os, sys, fcntl;"
                    f"import sys; sys.path.insert(0, {str(PROJECT / 'scripts')!r});"
                    "import cwk_atomic_file as A;"
                    f"fd=A.open_dir_nofollow({tmp!r});"
                    "cm=A.exclusive_lock(fd, 'proc.lock', blocking=True);"
                    "cm.__enter__();"
                    "print('holding', flush=True);"
                    "os._exit(0)"
                )
                proc = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-c", child_code],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(proc.stdout.close)
                line = proc.stdout.readline()
                self.assertIn("holding", line)
                proc.wait(timeout=5)
                # After child dies the lock should be reclaimable.
                with A.exclusive_lock(fd, "proc.lock", blocking=False) as lock_fd:
                    self.assertGreater(lock_fd, 0)
            finally:
                os.close(fd)


class OrphanRecoveryTests(unittest.TestCase):
    def test_recover_orphans_removes_prefixed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                # Simulate a crash mid-atomic-write.
                orphan = f"{A.TEMP_PREFIX}foo.deadbeef"
                os.open(orphan, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
                # Also a real file that must NOT be removed.
                A.write_atomic(fd, "foo.json", b"keep")
                removed = A.recover_orphans(fd)
                self.assertIn(orphan, removed)
                self.assertTrue(A.child_exists(fd, "foo.json"))
                self.assertFalse(A.child_exists(fd, orphan))
            finally:
                os.close(fd)

    def test_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = A.open_dir_nofollow(tmp)
            try:
                self.assertEqual(A.recover_orphans(fd), [])
                # Second call also no-op.
                self.assertEqual(A.recover_orphans(fd), [])
            finally:
                os.close(fd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
