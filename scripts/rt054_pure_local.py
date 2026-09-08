#!/usr/bin/env python3
"""The fixed, isolated RT-054 pure-local acceptance entry point."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

PROJECT = Path(__file__).resolve().parents[1]
TESTS = ("test_rt054_bounded_read", "test_kb_storage", "test_rt054_p0", "test_rt054_pure_local")
BOOTSTRAP = r"""
import atexit, builtins, io, os, socket, sys
from pathlib import Path
if not sys.flags.isolated or not sys.flags.no_site:
    raise RuntimeError("RT054_GUARD interpreter")
if any("sitecustomize" in item for item in sys.modules):
    raise RuntimeError("RT054_GUARD sitecustomize")
counts = {"socket": 0, "filestation_from_env": 0, "filestation_write": 0, "dotenv": 0}
def dotenv_path(value):
    try:
        return Path(os.fspath(value)).name == ".env"
    except TypeError:
        return False
def block_dotenv(value):
    if dotenv_path(value):
        counts["dotenv"] += 1
        raise AssertionError("RT054_GUARD dotenv")
def audit(event, args):
    if event == "open" and args and dotenv_path(args[0]):
        block_dotenv(args[0])
sys.addaudithook(audit)
real_builtin_open = builtins.open
real_io_open = io.open
real_os_open = os.open
def guarded_builtin_open(file, *args, **kwargs):
    block_dotenv(file)
    return real_builtin_open(file, *args, **kwargs)
def guarded_io_open(file, *args, **kwargs):
    block_dotenv(file)
    return real_io_open(file, *args, **kwargs)
def guarded_path_open(self, *args, **kwargs):
    block_dotenv(self)
    return real_io_open(self, *args, **kwargs)
def guarded_os_open(file, *args, **kwargs):
    block_dotenv(file)
    return real_os_open(file, *args, **kwargs)
builtins.open = guarded_builtin_open
io.open = guarded_io_open
Path.open = guarded_path_open
os.open = guarded_os_open
builtins._rt054_pure_local_guard = {"counts": counts, "block_dotenv": block_dotenv}
project = Path(sys.argv[1]).resolve()
temp_root = Path(sys.argv[2]).resolve()
tests = tuple(sys.argv[3:])
os.environ.pop("__CF_USER_TEXT_ENCODING", None)  # macOS inserts this after execve.
expected = ("test_rt054_bounded_read", "test_kb_storage", "test_rt054_p0", "test_rt054_pure_local")
if tests != expected:
    raise RuntimeError("RT054_GUARD argv")
if Path.cwd() != temp_root or not temp_root.is_dir():
    raise RuntimeError("RT054_GUARD cwd")
expected_env = {"CWK_RT054_PURE_LOCAL": "1", "HOME": str(temp_root), "LANG": "C", "LC_ALL": "C", "TMPDIR": str(temp_root)}
if os.environ != expected_env:
    raise RuntimeError("RT054_GUARD environment")
sys.path[:] = [str(project / "scripts"), str(project / "tests")] + [item for item in sys.path if item]
def blocked_socket(*args, **kwargs):
    counts["socket"] += 1
    raise AssertionError("RT054_GUARD external socket")
socket.socket.connect = blocked_socket
socket.create_connection = blocked_socket
import kb_storage
def blocked_from_env(*args, **kwargs):
    counts["filestation_from_env"] += 1
    raise AssertionError("RT054_GUARD FileStationBackend.from_env")
def block_default_transport_write(method):
    def guarded(self, *args, **kwargs):
        if self._uses_default_transport:
            counts["filestation_write"] += 1
            raise AssertionError("RT054_GUARD FileStationBackend write")
        return method(self, *args, **kwargs)
    return guarded
kb_storage.FileStationBackend.from_env = classmethod(blocked_from_env)
kb_storage.FileStationBackend.write = block_default_transport_write(kb_storage.FileStationBackend.write)
kb_storage.FileStationBackend.mkdir = block_default_transport_write(kb_storage.FileStationBackend.mkdir)
kb_storage.FileStationBackend.remove = block_default_transport_write(kb_storage.FileStationBackend.remove)
kb_storage.FileStationBackend.remove_dir = block_default_transport_write(kb_storage.FileStationBackend.remove_dir)
def report():
    print("RT054_PURE_LOCAL_BOOTSTRAP=1 RT054_GUARD socket={socket} filestation_from_env={filestation_from_env} filestation_write={filestation_write} dotenv={dotenv}".format(**counts))
atexit.register(report)
import unittest
result = unittest.main(module=None, argv=["rt054-pure-local", "-v", *tests], exit=False).result
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def controlled_temp_root() -> Path:
    """Create a mode-0700 local root without consulting parent TMP variables."""
    for parent in (Path("/private/tmp"), Path("/tmp")):
        if parent.is_dir():
            root = Path(tempfile.mkdtemp(prefix="cwk-rt054-", dir=parent))
            root.chmod(0o700)
            (root / ".env").write_text("CANARY_DOTENV_MUST_NOT_BE_READ=1\n", encoding="utf-8")
            return root
    raise RuntimeError("RT-054 needs a local temporary directory")


def pure_local_env(temp_root: Path) -> dict[str, str]:
    """Return every child variable; parent values are intentionally ignored."""
    return {"CWK_RT054_PURE_LOCAL": "1", "HOME": str(temp_root), "LANG": "C", "LC_ALL": "C", "TMPDIR": str(temp_root)}


def reject_arguments(argv: Sequence[str]) -> None:
    if argv:
        raise SystemExit("RT-054 pure-local accepts no test selection or extra arguments")


def main(argv: Sequence[str]) -> int:
    reject_arguments(argv)
    temp_root = controlled_temp_root()
    try:
        if not sys.flags.isolated or not sys.flags.no_site:
            raise RuntimeError("RT-054 outer runner must be isolated without site")
        command = [sys.executable, "-I", "-S", "-c", BOOTSTRAP, str(PROJECT), str(temp_root), *TESTS]
        return subprocess.run(command, cwd=temp_root, env=pure_local_env(temp_root)).returncode
    finally:
        shutil.rmtree(temp_root)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
