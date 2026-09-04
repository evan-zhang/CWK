#!/usr/bin/env python3
"""RT-042: ``verify`` subcommand family for a KB (C 表维护动词 ``verify``).

Usage::

    python3 scripts/kb_doctor.py verify --manifest --root /path/to/kb
    python3 scripts/kb_doctor.py verify --raw --collection-state --root ...
    python3 scripts/kb_doctor.py verify --all --backend nas --json

Checks, one flag each:

``--raw``               ``raw/_system/raw-manifest.json`` describes the raw
                        tree: every recorded entry present with a matching
                        digest, and no raw file missing from the ledger.
``--manifest``          ``root-manifest.json`` re-hashed against the whole
                        tree (B #19).
``--collection-state``  the cursor file is structurally sound: no id both
                        pending and collected, no duplicate collected id, no
                        cursor without a collected item (B #24).
``--changed-paths``     the incremental-change record is well-formed and
                        every path it lists still exists (B #25).
``--tree``              the B-table items that apply to this library's
                        configured sources all exist and are non-empty.

Exit code 0 when every requested check passes, 1 when any fails, 2 on a
usage or I/O error.  The failing check names the paths, so the output is
usable as evidence rather than a bare boolean.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_kb_create import audit_tree  # noqa: E402
from cwk_kb_ledger import (  # noqa: E402
    CHANGED_PATHS_REL,
    CHANGED_PATHS_SCHEMA,
    RAW_MANIFEST_REL,
    VerifyReport,
    dumps,
    read_json,
    sha256_bytes,
    verify_collection_state,
    verify_manifest,
)
from cwk_kb_storage import (  # noqa: E402
    NotFound,
    StorageBackend,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
)

CHECKS = ("raw", "manifest", "collection-state", "changed-paths", "tree")


def verify_raw(backend: StorageBackend) -> VerifyReport:
    """Compare ``raw/_system/raw-manifest.json`` with the raw tree."""
    report = VerifyReport()
    try:
        manifest = read_json(backend, RAW_MANIFEST_REL)
    except NotFound:
        report.missing.append(RAW_MANIFEST_REL)
        return report
    entries = manifest.get("entries") or {}
    on_disk = {
        path: sha256_bytes(backend.read(path))
        for path in backend.walk_files("raw")
        if path != RAW_MANIFEST_REL
    }
    for path, row in entries.items():
        digest = row.get("sha256") if isinstance(row, dict) else row
        if path not in on_disk:
            report.missing.append(path)
        elif on_disk[path] != digest:
            report.mismatched.append(path)
    for path in on_disk:
        if path not in entries:
            report.extra.append(path)
    return report


def verify_changed_paths(backend: StorageBackend) -> VerifyReport:
    report = VerifyReport()
    try:
        payload = read_json(backend, CHANGED_PATHS_REL)
    except NotFound:
        report.missing.append(CHANGED_PATHS_REL)
        return report
    if payload.get("schema") != CHANGED_PATHS_SCHEMA:
        report.mismatched.append(f"{CHANGED_PATHS_REL}: schema 非法")
        return report
    batches = payload.get("batches")
    if not isinstance(batches, list):
        report.mismatched.append(f"{CHANGED_PATHS_REL}: batches 不是数组")
        return report
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict) or not batch.get("at"):
            report.mismatched.append(f"{CHANGED_PATHS_REL}#{index}: 缺 at")
            continue
        for path in batch.get("paths") or []:
            if not backend.exists(path):
                report.missing.append(f"{CHANGED_PATHS_REL}#{index} → {path}")
    return report


def verify_tree(backend: StorageBackend) -> VerifyReport:
    """B-table existence + non-emptiness for this library's applicable rows."""
    report = VerifyReport()
    try:
        source_config = read_json(backend, "source.json")
    except NotFound:
        report.missing.append("source.json")
        return report
    sources = [
        entry.get("source_type")
        for entry in source_config.get("sources") or []
        if isinstance(entry, dict)
    ]
    report.missing.extend(audit_tree(backend, sources))
    return report


CHECK_FUNCS: Dict[str, Callable[[StorageBackend], VerifyReport]] = {
    "raw": verify_raw,
    "manifest": verify_manifest,
    "collection-state": verify_collection_state,
    "changed-paths": verify_changed_paths,
    "tree": verify_tree,
}


def run_checks(backend: StorageBackend, checks: Sequence[str]) -> Dict[str, VerifyReport]:
    return {name: CHECK_FUNCS[name](backend) for name in checks}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KB 体检：verify 子命令族")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="各账本体检")
    verify.add_argument("--raw", action="store_true")
    verify.add_argument("--manifest", action="store_true")
    verify.add_argument("--collection-state", action="store_true")
    verify.add_argument("--changed-paths", action="store_true")
    verify.add_argument("--tree", action="store_true")
    verify.add_argument("--all", action="store_true", help="跑全部检查")
    verify.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
    verify.add_argument("--root", help="local 后端的库根目录")
    verify.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
    verify.add_argument("--json", action="store_true")
    return parser


def selected_checks(args: argparse.Namespace) -> List[str]:
    if args.all:
        return list(CHECKS)
    chosen = [name for name in CHECKS if getattr(args, name.replace("-", "_"))]
    return chosen


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    backend = None
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        checks = selected_checks(args)
        if not checks:
            print(
                "至少要选一项检查：--raw / --manifest / --collection-state / "
                "--changed-paths / --tree / --all",
                file=sys.stderr,
            )
            return 2
        backend = build_backend(args.backend, root=args.root, prefix=args.prefix)
        reports = run_checks(backend, checks)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"体检失败：{exc}", file=sys.stderr)
        return 2
    finally:
        close_backend(backend)

    if args.json:
        sys.stdout.write(
            dumps({name: report.as_dict() for name, report in reports.items()}).decode("utf-8")
        )
    else:
        for name, report in reports.items():
            mark = "ok  " if report.ok else "FAIL"
            print(f"[{mark}] verify --{name}: {report.describe()}")
    return 0 if all(report.ok for report in reports.values()) else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
