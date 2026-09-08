#!/usr/bin/env python3
"""Run RT-054 bounded-read acceptance without inheriting operator secrets.

This is intentionally a whitelist rebuild, not a blacklist.  The only child
variables are enough to find Python and create local test fixtures; all NAS,
credential, token, password, secret, and key variables from the parent are
therefore absent.  The explicit flag also makes the NAS smoke decorator skip
even if a caller later broadens this whitelist by mistake.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROJECT = Path(__file__).resolve().parents[1]
ALLOWED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP")
DEFAULT_TESTS = (
    "test_rt054_bounded_read",
    "test_kb_storage",
    "test_rt054_p0",
    "test_rt054_pure_local",
)


def pure_local_env(parent: dict[str, str]) -> dict[str, str]:
    """Return the complete, non-secret environment for the child process."""
    env = {name: parent[name] for name in ALLOWED_ENV if parent.get(name)}
    env.setdefault("LANG", "C")
    env.setdefault("LC_ALL", "C")
    env.setdefault("TMPDIR", "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp")
    env["CWK_RT054_PURE_LOCAL"] = "1"
    return env


def main(argv: Sequence[str]) -> int:
    tests = tuple(argv) or DEFAULT_TESTS
    unknown = [name for name in tests if name not in DEFAULT_TESTS]
    if unknown:
        raise SystemExit("RT-054 pure-local accepts only: " + ", ".join(DEFAULT_TESTS))
    # Verbose unittest output retains the forced skip reason in RT evidence.
    command = [sys.executable, "-m", "unittest", "-v", *tests]
    completed = subprocess.run(command, cwd=PROJECT / "tests", env=pure_local_env(dict(os.environ)))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
