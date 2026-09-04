#!/usr/bin/env python3
"""RT-042: migrate the existing mirror tree into a NAS-backed KB.

Usage::

    python3 scripts/kb_migrate.py plan  --source-root knowledge/工作协同镜像 \\
        --dest-root /tmp/kb --kb-code <code>
    python3 scripts/kb_migrate.py apply --source-root ... --dest-root ...
    python3 scripts/kb_migrate.py reconcile --source-root ... --dest-root ...

The migration is checked, not trusted.  Three mechanisms:

1. **Path map** — :data:`PATH_RULES` states, per prefix, where a mirror path
   lands in the KB tree.  A source path that matches no rule is reported, not
   guessed at; a path under a retired directory
   (:data:`RETIRED_PREFIXES` — ``entities/`` ``events/`` ``history/``
   ``_index/``) is dropped on purpose and recorded as such.
2. **Bidirectional content pairing** — every source file is paired with a
   destination file by sha256 *and* by mapped path, in both directions.  A
   file that exists on one side with no partner on the other lands in
   ``unpaired_source`` / ``unpaired_dest``.  Dropping one file mid-migration
   therefore cannot come out clean (J6).
3. **Allow lists** — ``allowed_new`` (things the destination is expected to
   have that the mirror never had, e.g. the freshly built B-table skeleton)
   and ``allowed_renames`` (explicit ``source → dest`` pairs whose paths
   differ by design).  Anything unpaired that is *not* on a list is a
   finding; the lists are data, so a reviewer can read exactly what was
   forgiven and why.

Content is never rewritten: bytes move as-is, so the sha256 recorded in the
mirror's raw manifest still identifies the same file after the move.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_kb_ledger import (  # noqa: E402
    MANIFEST_REL,
    dumps,
    read_json,
    record_write,
    refresh_manifest,
)
from cwk_kb_storage import (  # noqa: E402
    NotFound,
    StorageBackend,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
    normalize_path,
)

MIGRATION_SCHEMA = "cwk.kb.migration-plan.v1"

# Retired in the platform v1 layout: superseded by taxonomy/categories, the
# audit chain and the search index respectively.  They are deliberately not
# migrated, and the plan says so rather than silently omitting them.
RETIRED_PREFIXES: Tuple[str, ...] = ("entities/", "events/", "history/", "_index/")

# (mirror prefix, kb prefix).  Longest prefix wins, so a more specific rule
# can be added above a general one without reordering the general one.
PATH_RULES: Tuple[Tuple[str, str], ...] = (
    ("raw/_system/raw-manifest.json", "raw/_system/raw-manifest.json"),
    ("raw/", "raw/"),
    ("timelines/", "timelines/"),
    ("wiki/_system/reply-state.json", "_system/reply-state.json"),
    ("wiki/_system/taxonomy.json", "_system/taxonomy.json"),
    ("wiki/_system/entity-catalog.json", "_system/entity-catalog.json"),
    ("wiki/_system/search-index.json", "_system/search-index.json"),
    ("wiki/_system/", "_system/"),
    ("wiki/", "wiki/"),
)


class MigrationError(Exception):
    """A migration input was rejected."""


@dataclass
class MigrationPlan:
    """What the migration intends to do, before it does any of it."""

    mapping: Dict[str, str] = field(default_factory=dict)
    retired: List[str] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)
    source_digests: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "schema": MIGRATION_SCHEMA,
            "mapped_count": len(self.mapping),
            "retired_count": len(self.retired),
            "unmapped_count": len(self.unmapped),
            "mapping": dict(sorted(self.mapping.items())),
            "retired": sorted(self.retired),
            "unmapped": sorted(self.unmapped),
            "source_digests": dict(sorted(self.source_digests.items())),
        }


@dataclass
class ReconcileReport:
    """The bidirectional pairing result.  Empty lists = clean migration."""

    paired: List[Tuple[str, str]] = field(default_factory=list)
    unpaired_source: List[str] = field(default_factory=list)
    unpaired_dest: List[str] = field(default_factory=list)
    digest_mismatch: List[str] = field(default_factory=list)
    retired_skipped: List[str] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.unpaired_source
            or self.unpaired_dest
            or self.digest_mismatch
            or self.unmapped
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "paired_count": len(self.paired),
            "unpaired_source": sorted(self.unpaired_source),
            "unpaired_dest": sorted(self.unpaired_dest),
            "digest_mismatch": sorted(self.digest_mismatch),
            "retired_skipped": sorted(self.retired_skipped),
            "unmapped": sorted(self.unmapped),
        }

    def describe(self) -> str:
        if self.ok:
            return f"迁移对账通过：{len(self.paired)} 个文件双向配对，零未配对"
        parts = []
        if self.unpaired_source:
            parts.append(f"源侧未配对 {len(self.unpaired_source)}：{', '.join(self.unpaired_source[:5])}")
        if self.unpaired_dest:
            parts.append(f"目标侧未配对 {len(self.unpaired_dest)}：{', '.join(self.unpaired_dest[:5])}")
        if self.digest_mismatch:
            parts.append(f"内容哈希不符 {len(self.digest_mismatch)}：{', '.join(self.digest_mismatch[:5])}")
        if self.unmapped:
            parts.append(f"无映射规则 {len(self.unmapped)}：{', '.join(self.unmapped[:5])}")
        return "迁移对账未通过——" + "；".join(parts)


def is_retired(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in RETIRED_PREFIXES)


def map_path(path: str) -> Optional[str]:
    """Return the KB path for a mirror ``path``, or ``None`` when unmapped."""
    safe = normalize_path(path)
    best: Optional[Tuple[str, str]] = None
    for prefix, target in PATH_RULES:
        if safe == prefix or safe.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, target)
    if best is None:
        return None
    prefix, target = best
    if safe == prefix:
        return normalize_path(target)
    return normalize_path(target + safe[len(prefix) :])


def build_plan(source: StorageBackend, *, scope: str = ".") -> MigrationPlan:
    plan = MigrationPlan()
    for path in source.walk_files(scope):
        if is_retired(path):
            plan.retired.append(path)
            continue
        target = map_path(path)
        if target is None:
            plan.unmapped.append(path)
            continue
        plan.mapping[path] = target
        plan.source_digests[path] = source.sha256(path)
    return plan


def ledger_digests(dest: StorageBackend) -> Dict[str, str]:
    """Return ``{path: sha256}`` as the destination's root-manifest records it."""
    try:
        manifest = read_json(dest, MANIFEST_REL)
    except (NotFound, ValueError):
        return {}
    return {
        path: str(row.get("sha256"))
        for path, row in (manifest.get("entries") or {}).items()
        if isinstance(row, dict)
    }


def assert_destination_is_clean(
    dest: StorageBackend,
    plan: MigrationPlan,
    *,
    allowed_overwrite: Sequence[str] = (),
    known: Optional[Dict[str, str]] = None,
) -> None:
    """Prove, before the first copy, that no target holds unaccounted content.

    A target may be written when it does not exist, when it already carries
    the source bytes (the idempotent re-run), or when it carries exactly what
    the destination's root-manifest says the build put there — the B-table
    skeleton the migration is meant to fill in.  Anything else (a file the
    ledger never recorded, a file whose content drifted from the ledger, the
    debris of an earlier interrupted run) is a collision, and the migration
    refuses with zero writes instead of clobbering half of it and then
    reporting the problem.
    """
    known = ledger_digests(dest) if known is None else known
    allowed = set(allowed_overwrite)
    collisions: List[str] = []
    for src_path in sorted(plan.mapping):
        target = plan.mapping[src_path]
        if not dest.exists(target) or target in allowed:
            continue
        try:
            existing = dest.sha256(target)
        except NotFound:  # a directory is standing where the file should go
            collisions.append(f"{src_path} → {target}（目的地同名对象不是文件）")
            continue
        if existing == plan.source_digests[src_path]:
            continue
        if known.get(target) == existing:
            continue
        reason = "账本未登记" if target not in known else "内容已偏离账本"
        collisions.append(f"{src_path} → {target}（{reason}）")
    if collisions:
        raise MigrationError(
            f"迁移预检失败：{len(collisions)} 个目标路径的既有内容不受目的地账本认领，"
            "本次零写入。冲突项：\n  " + "\n  ".join(collisions[:10])
        )


def apply_plan(
    source: StorageBackend,
    dest: StorageBackend,
    plan: MigrationPlan,
    *,
    kb_code: Optional[str] = None,
    allowed_overwrite: Sequence[str] = (),
) -> List[str]:
    """Copy every mapped file, then re-sign the destination ledger.

    Idempotent: a target that already carries the planned bytes is left
    alone, so a re-run writes nothing and the manifest is not re-issued.
    """
    known = ledger_digests(dest)
    assert_destination_is_clean(
        dest, plan, allowed_overwrite=allowed_overwrite, known=known
    )
    created: List[str] = []
    replaced: List[str] = []
    for src_path in sorted(plan.mapping):
        target = plan.mapping[src_path]
        if dest.exists(target) and dest.sha256(target) == plan.source_digests[src_path]:
            continue
        data = source.read(src_path)
        # record_write, not dest.write: a backend that swallows the copy is
        # caught here instead of at the reconcile step.
        record_write(dest, target, data)
        # Classified against the *ledger*, not the disk: that is the set the
        # manifest refresh compares against, and a file the ledger never knew
        # about is an addition however long it has been lying there.
        (replaced if target in known else created).append(target)
    written = sorted(created + replaced)
    if written:
        # Same function, same call: leaving the ledger stale until some later
        # command happens to refresh it is how a tree and its manifest drift
        # apart without anybody deciding that they should.  The migration
        # declares what it added and what it overwrote; the ledger forgives
        # exactly that and nothing else.
        refresh_manifest(
            dest,
            kb_code=resolve_kb_code(dest, kb_code),
            allow_new=created,
            allow_replaced=replaced,
        )
    return written


def resolve_kb_code(dest: StorageBackend, kb_code: Optional[str] = None) -> str:
    """Return the destination library's kb_code (kb.json first, ledger next)."""
    if kb_code:
        return kb_code
    for path, key in (("kb.json", "kb_code"), (MANIFEST_REL, "kb_code")):
        try:
            value = read_json(dest, path).get(key)
        except (NotFound, ValueError):
            continue
        if isinstance(value, str) and value:
            return value
    raise MigrationError(
        "目的地既没有 kb.json 也没有 root-manifest.json，无法确定 kb_code："
        "迁移只写进已建好的库，请先 cwk_kb_create.py 建库。"
    )


def reconcile(
    source: StorageBackend,
    dest: StorageBackend,
    plan: MigrationPlan,
    *,
    allowed_new: Sequence[str] = (),
    allowed_renames: Iterable[Tuple[str, str]] = (),
) -> ReconcileReport:
    """Pair source and destination in both directions.

    Forward: every mapped source file must exist at its mapped destination
    with the same sha256.  Backward: every destination file must be claimed
    by exactly one source file, an ``allowed_new`` entry, or an
    ``allowed_renames`` pair.
    """
    report = ReconcileReport(
        retired_skipped=list(plan.retired), unmapped=list(plan.unmapped)
    )
    renames = {src: dst for src, dst in allowed_renames}
    allowed_new_set = set(allowed_new)

    claimed: Dict[str, str] = {}
    for src_path, mapped in sorted(plan.mapping.items()):
        target = renames.get(src_path, mapped)
        if not dest.exists(target):
            report.unpaired_source.append(src_path)
            continue
        if dest.sha256(target) != plan.source_digests[src_path]:
            report.digest_mismatch.append(f"{src_path} → {target}")
            continue
        claimed[target] = src_path
        report.paired.append((src_path, target))

    for dest_path in dest.walk_files("."):
        if dest_path in claimed or dest_path in allowed_new_set:
            continue
        if any(dest_path.startswith(prefix) for prefix in allowed_new_set if prefix.endswith("/")):
            continue
        report.unpaired_dest.append(dest_path)

    return report


def skeleton_allow_list(dest: StorageBackend, plan: MigrationPlan) -> List[str]:
    """Destination files the freshly built KB skeleton owns.

    Anything on the destination that the plan does not map, but that the
    build step created (kb.json, the ledgers, the empty indexes), belongs
    here.  Computed rather than hand-written so it cannot quietly grow.
    """
    mapped = set(plan.mapping.values())
    return sorted(path for path in dest.walk_files(".") if path not in mapped)


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="镜像 → NAS 迁移（路径映射 + 双向配对）")
    parser.add_argument("command", choices=("plan", "apply", "reconcile"))
    parser.add_argument("--source-root", required=True, help="镜像根目录（本地 FS）")
    parser.add_argument("--dest-root", help="目标库根目录；--dest-backend nas 时可省")
    parser.add_argument("--dest-backend", default="local", choices=("local", "memory", "nas"))
    parser.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
    parser.add_argument(
        "--allow-new",
        default="",
        help="逗号分隔的目标侧允许新增路径（目录以 / 结尾表示整棵子树）",
    )
    parser.add_argument(
        "--allow-rename",
        action="append",
        default=[],
        metavar="SRC=DST",
        help="允许的重命名对，可重复",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def parse_renames(pairs: Sequence[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for pair in pairs:
        if "=" not in pair:
            raise MigrationError(f"--allow-rename 需要 SRC=DST 形式：{pair!r}")
        src, dst = pair.split("=", 1)
        out.append((normalize_path(src), normalize_path(dst)))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    source = dest = None
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        source = build_backend("local", root=args.source_root)
        dest = build_backend(args.dest_backend, root=args.dest_root, prefix=args.prefix)
        plan = build_plan(source)
        if args.command == "plan":
            payload = plan.as_dict()
        elif args.command == "apply":
            written = apply_plan(source, dest, plan)
            payload = {"written": written, "written_count": len(written)}
        else:
            allow_new = [part for part in args.allow_new.split(",") if part]
            report = reconcile(
                source,
                dest,
                plan,
                allowed_new=allow_new,
                allowed_renames=parse_renames(args.allow_rename),
            )
            payload = report.as_dict()
            if not args.json:
                print(report.describe())
                return 0 if report.ok else 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 2
    finally:
        for backend in (source, dest):
            close_backend(backend)
    sys.stdout.write(dumps(payload).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
