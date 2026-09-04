#!/usr/bin/env python3
"""RT-042: hash ledger, collection cursor and wizard state machine for a KB.

Three data models, all persisted through a :mod:`kb_storage` backend so
they behave identically on a local disk and on the NAS.

1. **root-manifest ledger** (B 表 #19).  Every write goes through
   :func:`record_write`, which writes and then *reads back* and compares the
   digest — a backend that silently drops writes fails here rather than three
   steps later.  :func:`verify_manifest` re-hashes the whole tree and reports
   missing / extra / mismatched paths.  :func:`assert_existing_unchanged`
   encodes the "存量不变" rule: a path that was already in the ledger must
   still be there with the same digest, so an in-place edit of a raw file is
   a hard failure instead of a quietly re-signed ledger.

   Exempt from the "unchanged" rule (and from the J1 byte comparison) are the
   *timestamp-class* files listed in :data:`TIMESTAMP_CLASS_PATHS`: the
   append-only run log, the append-only audit chain, and the manifest itself.
   They carry wall-clock time by construction, so requiring them to be stable
   would make the criterion untestable rather than strict.

2. **collection_state cursor** (B 表 #24).  Per source, per lane: a
   high-water cursor, the set of ids already collected, and the in-flight
   remainder of the current batch.  State is saved after *each* item, so an
   interrupted run resumes with no duplicates and no losses.

3. **wizard state machine** (EXECUTION-PLAN RT-042 §5).
   ``draft → verified → sourced → previewed → ingesting → taxonomy → active``
   with a 30-minute TTL on ``draft``.  Once the TTL lapses every verb is
   refused — including the verb that would have advanced the draft.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from cwk_kb_storage import NotFound, StorageBackend, sha256_bytes

MANIFEST_SCHEMA = "cwk.kb.root-manifest.v1"
COLLECTION_SCHEMA = "cwk.kb.collection-state.v1"
CHANGED_PATHS_SCHEMA = "cwk.kb.changed-paths-manifest.v1"
WIZARD_SCHEMA = "cwk.kb.wizard-state.v1"

MANIFEST_REL = "root-manifest.json"
RAW_MANIFEST_REL = "raw/_system/raw-manifest.json"
COLLECTION_STATE_REL = "_system/collection_state.json"
CHANGED_PATHS_REL = "_system/changed_paths_manifest.json"
AUDIT_REL = "audit.jsonl"
LOG_REL = "wiki/log.md"

# Files that legitimately differ between two otherwise identical builds:
# append-only logs and the ledger's own header (generated_at / version).
# Everything else must be byte-identical, so this set is deliberately short
# and named — "排除时间戳类文件" is a rule, not a loophole.
TIMESTAMP_CLASS_PATHS = (LOG_REL, AUDIT_REL, MANIFEST_REL)

# The one authority on what the root-manifest does *not* cover.  It is a code
# constant, never read back from the manifest file: a ledger that takes its
# own exclusion list from the document it is checking can be told to stop
# looking at whatever the attacker is about to edit.  Each entry is excluded
# because another verifier owns it, and that verifier is named here:
#
#   root-manifest.json          the ledger's own header (generated_at, version)
#   _system/collection_state.json   运行态游标 → verify_collection_state
#   _system/changed_paths_manifest.json  增量记录 → verify_changed_paths
#   audit.jsonl / wiki/log.md   append-only chains, wall-clock by construction
#
# Nothing is exempt from *all* checking; the exemptions move a file from one
# verifier to another.
EXCLUDED_PATHS: Tuple[str, ...] = (
    MANIFEST_REL,
    COLLECTION_STATE_REL,
    CHANGED_PATHS_REL,
    AUDIT_REL,
    LOG_REL,
)


class LedgerError(Exception):
    """Base class for ledger failures."""


class WriteReconcileFailed(LedgerError):
    """A write did not survive the read-back comparison."""


class LedgerViolation(LedgerError):
    """An already-recorded file changed or disappeared."""


class WizardError(Exception):
    """Base class for wizard state machine failures."""


class IllegalTransition(WizardError):
    """The requested verb is not defined for the current state."""


class DraftExpired(WizardError):
    """The draft's 30-minute TTL has lapsed; every verb is refused."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def dumps(payload: dict) -> bytes:
    """Canonical JSON: sorted keys, stable indent, trailing newline.

    Canonical because two backends must produce byte-identical files for the
    J1 comparison to mean anything.
    """
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def loads(data: bytes) -> dict:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise LedgerError("期望 JSON 对象")
    return payload


# ── write-through reconciliation ────────────────────────────────────────────


def record_write(backend: StorageBackend, path: str, data: bytes) -> str:
    """Write ``data`` then read it back and compare digests.

    This is the single write primitive for the KB.  It is what makes the
    anti-idle criterion (J2) bite: a backend that accepts writes and stores
    nothing fails on the read-back, at the exact call that lied.
    """
    expected = sha256_bytes(data)
    backend.write(path, data)
    try:
        actual = sha256_bytes(backend.read(path))
    except NotFound as exc:
        raise WriteReconcileFailed(
            f"写后对账失败：{path} 写入后读不回来（后端声称成功但没有落盘）"
        ) from exc
    if actual != expected:
        raise WriteReconcileFailed(
            f"写后对账失败：{path} 期望 {expected}，读回 {actual}"
        )
    return expected


def write_json(backend: StorageBackend, path: str, payload: dict) -> str:
    return record_write(backend, path, dumps(payload))


def read_json(backend: StorageBackend, path: str) -> dict:
    return loads(backend.read(path))


# ── root-manifest ───────────────────────────────────────────────────────────


@dataclass
class VerifyReport:
    """Result of re-hashing a tree against a manifest."""

    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    mismatched: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.mismatched)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "missing": sorted(self.missing),
            "extra": sorted(self.extra),
            "mismatched": sorted(self.mismatched),
        }

    def describe(self) -> str:
        if self.ok:
            return "全量校验通过"
        parts = []
        if self.missing:
            parts.append(f"缺失 {len(self.missing)}：{', '.join(sorted(self.missing)[:5])}")
        if self.extra:
            parts.append(f"未登记 {len(self.extra)}：{', '.join(sorted(self.extra)[:5])}")
        if self.mismatched:
            parts.append(f"哈希不符 {len(self.mismatched)}：{', '.join(sorted(self.mismatched)[:5])}")
        return "校验未通过——" + "；".join(parts)


def scan_tree(backend: StorageBackend, *, exclude: Sequence[str] = ()) -> Dict[str, dict]:
    """Return ``{path: {sha256, size}}`` for every file the backend holds."""
    skip = set(exclude)
    entries: Dict[str, dict] = {}
    for path in backend.walk_files("."):
        if path in skip:
            continue
        data = backend.read(path)
        entries[path] = {"sha256": sha256_bytes(data), "size": len(data)}
    return entries


def build_manifest(
    backend: StorageBackend,
    *,
    kb_code: str,
    manifest_version: int = 1,
    generated_at: Optional[datetime] = None,
) -> dict:
    """Hash every file the ledger owns.  The exclusion set is not negotiable.

    ``excluded_paths`` is written into the document for the reader's benefit;
    :func:`verify_manifest` treats it as a claim to check against
    :data:`EXCLUDED_PATHS`, never as an instruction.
    """
    entries = scan_tree(backend, exclude=EXCLUDED_PATHS)
    return {
        "schema": MANIFEST_SCHEMA,
        "kb_code": kb_code,
        "manifest_version": manifest_version,
        "generated_at": iso(generated_at or utc_now()),
        "entry_count": len(entries),
        "excluded_paths": sorted(EXCLUDED_PATHS),
        "entries": entries,
    }


def load_manifest(backend: StorageBackend) -> dict:
    return read_json(backend, MANIFEST_REL)


def write_manifest(backend: StorageBackend, manifest: dict) -> str:
    """Persist the manifest with read-back reconciliation."""
    return write_json(backend, MANIFEST_REL, manifest)


def refresh_manifest(
    backend: StorageBackend,
    *,
    kb_code: str,
    generated_at: Optional[datetime] = None,
    allow_new: Sequence[str] = (),
    allow_replaced: Sequence[str] = (),
) -> dict:
    """Rebuild the manifest after a write, asserting the existing set is intact.

    ``allow_new`` names paths this write is permitted to add and
    ``allow_replaced`` names paths it is permitted to overwrite (the
    migration filling in a build's placeholder is the case that needs it).
    Anything else that appeared, changed or vanished raises
    :class:`LedgerViolation` — the ledger refuses to re-sign a tree it cannot
    explain, and both allowances are per-call data a reviewer can read.
    """
    try:
        previous = load_manifest(backend)
    except NotFound:
        previous = {}
    version = int(previous.get("manifest_version", 0)) + 1
    current = build_manifest(
        backend, kb_code=kb_code, manifest_version=version, generated_at=generated_at
    )
    if previous:
        violations = assert_existing_unchanged(
            previous, current, allow_new=allow_new, allow_replaced=allow_replaced
        )
        if violations:
            raise LedgerViolation(
                "存量不变断言失败：\n  " + "\n  ".join(violations)
            )
    write_manifest(backend, current)
    return current


def verify_manifest(backend: StorageBackend, manifest: Optional[dict] = None) -> VerifyReport:
    """Re-hash the whole tree and compare it with the recorded manifest.

    The scan is driven by :data:`EXCLUDED_PATHS`, not by the manifest's own
    ``excluded_paths`` field.  Honouring that field would hand the tamperer
    the checker's blind spot: write the victim path into the list, edit the
    file, and the ledger would agree with itself.  The field is verified
    instead — a manifest that claims a different exclusion set is itself a
    finding.
    """
    recorded = manifest if manifest is not None else load_manifest(backend)
    entries = recorded.get("entries") or {}
    disk = scan_tree(backend, exclude=EXCLUDED_PATHS)
    report = VerifyReport()
    claimed = recorded.get("excluded_paths")
    if claimed is None or sorted(str(path) for path in claimed) != sorted(EXCLUDED_PATHS):
        report.mismatched.append(
            f"{MANIFEST_REL}: excluded_paths 与代码常量不符"
            f"（账本声明 {sorted(claimed) if claimed else claimed}，"
            f"代码常量 {sorted(EXCLUDED_PATHS)}）"
        )
    for path, row in entries.items():
        if path not in disk:
            report.missing.append(path)
        elif disk[path]["sha256"] != row.get("sha256"):
            report.mismatched.append(path)
    for path in disk:
        if path not in entries:
            report.extra.append(path)
    return report


def assert_existing_unchanged(
    previous: dict,
    current: dict,
    *,
    allow_new: Sequence[str] = (),
    allow_replaced: Sequence[str] = (),
    exempt: Sequence[str] = TIMESTAMP_CLASS_PATHS,
) -> List[str]:
    """Return the list of "存量不变" violations between two manifests."""
    exempt_set = set(exempt)
    allowed = set(allow_new)
    replaced = set(allow_replaced)
    old = previous.get("entries") or {}
    new = current.get("entries") or {}
    violations: List[str] = []
    for path, row in old.items():
        if path in exempt_set:
            continue
        if path not in new:
            violations.append(f"存量文件被删除：{path}")
        elif new[path].get("sha256") != row.get("sha256") and path not in replaced:
            violations.append(
                f"存量文件被原地改写：{path}"
                f"（{row.get('sha256')} → {new[path].get('sha256')}）"
            )
    for path in new:
        if path in old or path in exempt_set or path in allowed:
            continue
        violations.append(f"出现未声明的新增文件：{path}")
    return sorted(violations)


# ── changed-paths manifest (B 表 #25) ───────────────────────────────────────


def record_changed_paths(
    backend: StorageBackend,
    changed: Iterable[str],
    *,
    reason: str,
    at: Optional[datetime] = None,
) -> dict:
    """Append an incremental-change record to ``_system/changed_paths_manifest.json``."""
    try:
        payload = read_json(backend, CHANGED_PATHS_REL)
    except NotFound:
        payload = {"schema": CHANGED_PATHS_SCHEMA, "batches": []}
    payload.setdefault("batches", []).append(
        {
            "at": iso(at or utc_now()),
            "reason": reason,
            "paths": sorted(set(changed)),
        }
    )
    write_json(backend, CHANGED_PATHS_REL, payload)
    return payload


# ── collection_state cursor (B 表 #24) ──────────────────────────────────────


def batch_id_for(items: Sequence[str]) -> str:
    """Stable id for an ordered batch, so a resumed run recognises itself."""
    joined = "\n".join(items).encode("utf-8")
    return sha256_bytes(joined)[:16]


@dataclass
class CollectionState:
    """Per-source, per-lane collection cursor with crash-resume semantics."""

    backend: StorageBackend
    payload: dict

    @classmethod
    def load(cls, backend: StorageBackend) -> "CollectionState":
        try:
            payload = read_json(backend, COLLECTION_STATE_REL)
        except NotFound:
            payload = {"schema": COLLECTION_SCHEMA, "sources": {}}
        payload.setdefault("sources", {})
        return cls(backend=backend, payload=payload)

    def save(self, *, at: Optional[datetime] = None) -> None:
        self.payload["updated_at"] = iso(at or utc_now())
        write_json(self.backend, COLLECTION_STATE_REL, self.payload)

    def lane(self, source: str, lane: str) -> dict:
        sources = self.payload.setdefault("sources", {})
        lanes = sources.setdefault(source, {})
        return lanes.setdefault(
            lane,
            {
                "cursor": None,
                "collected": [],
                "pending": [],
                "batch_id": None,
                "last_run_at": None,
                "interrupted": False,
            },
        )

    def begin_batch(
        self, source: str, lane: str, items: Sequence[str], *, at: Optional[datetime] = None
    ) -> List[str]:
        """Return the items still to process, resuming an interrupted batch.

        Items already in ``collected`` are never handed out again, so a
        re-run after a crash produces no duplicates.  Items that were pending
        when the process died are handed out again, so nothing is lost.
        """
        state = self.lane(source, lane)
        collected = set(state["collected"])
        remaining = [item for item in items if item not in collected]
        state["pending"] = remaining
        state["batch_id"] = batch_id_for(list(items))
        state["last_run_at"] = iso(at or utc_now())
        state["interrupted"] = bool(remaining)
        self.save(at=at)
        return list(remaining)

    def mark_done(
        self,
        source: str,
        lane: str,
        item: str,
        *,
        cursor: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> None:
        """Record one item as collected and persist immediately.

        Persisting per item is the whole point: the window in which a crash
        can lose work is one item wide, and the ledger says which one.
        """
        state = self.lane(source, lane)
        if item not in state["collected"]:
            state["collected"].append(item)
        state["pending"] = [pending for pending in state["pending"] if pending != item]
        state["cursor"] = cursor if cursor is not None else item
        state["interrupted"] = bool(state["pending"])
        self.save(at=at)

    def finish_batch(
        self, source: str, lane: str, *, at: Optional[datetime] = None
    ) -> None:
        state = self.lane(source, lane)
        if state["pending"]:
            raise LedgerError(
                f"{source}/{lane} 还有 {len(state['pending'])} 项未完成，不能收批"
            )
        state["batch_id"] = None
        state["interrupted"] = False
        self.save(at=at)

    def collected(self, source: str, lane: str) -> List[str]:
        return list(self.lane(source, lane)["collected"])

    def pending(self, source: str, lane: str) -> List[str]:
        return list(self.lane(source, lane)["pending"])

    def cursor(self, source: str, lane: str) -> Optional[str]:
        return self.lane(source, lane)["cursor"]

    def is_interrupted(self, source: str, lane: str) -> bool:
        return bool(self.lane(source, lane)["interrupted"])


def verify_collection_state(backend: StorageBackend) -> VerifyReport:
    """Structural check for ``collection_state.json``.

    A lane whose ``pending`` and ``collected`` overlap has double-counted an
    item; a lane with a cursor but no collected item never actually ran.
    """
    report = VerifyReport()
    try:
        payload = read_json(backend, COLLECTION_STATE_REL)
    except NotFound:
        report.missing.append(COLLECTION_STATE_REL)
        return report
    if payload.get("schema") != COLLECTION_SCHEMA:
        report.mismatched.append(COLLECTION_STATE_REL)
        return report
    for source, lanes in (payload.get("sources") or {}).items():
        for lane, state in (lanes or {}).items():
            label = f"{COLLECTION_STATE_REL}#{source}/{lane}"
            collected = state.get("collected") or []
            pending = state.get("pending") or []
            if set(collected) & set(pending):
                report.mismatched.append(f"{label}: pending 与 collected 重叠")
            if len(set(collected)) != len(collected):
                report.mismatched.append(f"{label}: collected 有重复项")
            if state.get("cursor") and not collected:
                report.mismatched.append(f"{label}: 有游标却没有已采集项")
    return report


# ── wizard state machine ────────────────────────────────────────────────────

DRAFT_TTL = timedelta(minutes=30)

STATES: Tuple[str, ...] = (
    "draft",
    "verified",
    "sourced",
    "previewed",
    "ingesting",
    "taxonomy",
    "active",
    "expired",
)

# verb → (required state, resulting state).  A verb that is not defined for
# the current state is refused; there is no "force" path.
VERBS: Dict[str, Tuple[str, str]] = {
    "verify-key": ("draft", "verified"),
    "set-sources": ("verified", "sourced"),
    "preview": ("sourced", "previewed"),
    "ingest": ("previewed", "ingesting"),
    "taxonomy": ("ingesting", "taxonomy"),
    "activate": ("taxonomy", "active"),
}

TRANSITIONS: Dict[str, frozenset] = {
    "draft": frozenset({"verified", "expired"}),
    "verified": frozenset({"sourced"}),
    "sourced": frozenset({"previewed"}),
    "previewed": frozenset({"ingesting"}),
    "ingesting": frozenset({"taxonomy"}),
    "taxonomy": frozenset({"active"}),
    "active": frozenset(),
    "expired": frozenset(),
}


@dataclass
class WizardState:
    """The build wizard's persisted state, with a TTL on ``draft``.

    Only ``draft`` expires: once the key has been verified the library is a
    real object with a persisted record, and abandoning it is a separate
    (audited) cleanup concern rather than a timeout.
    """

    draft_id: str
    state: str = "draft"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    history: List[dict] = field(default_factory=list)
    ttl: timedelta = DRAFT_TTL

    def expires_at(self) -> Optional[datetime]:
        return self.created_at + self.ttl if self.state == "draft" else None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.state == "expired":
            return True
        if self.state != "draft":
            return False
        return (now or utc_now()) >= self.created_at + self.ttl

    def apply(self, verb: str, *, now: Optional[datetime] = None) -> "WizardState":
        """Advance the machine, or refuse.

        Order matters: expiry is checked *before* the verb table, so an
        expired draft refuses even its own legal next verb.
        """
        moment = now or utc_now()
        if self.is_expired(moment):
            self.state = "expired"
            self.updated_at = moment
            raise DraftExpired(
                f"草稿 {self.draft_id} 已过 {int(self.ttl.total_seconds() // 60)} 分钟 TTL，"
                f"动词 {verb!r} 拒绝执行；请重新发起建库向导。"
            )
        if verb not in VERBS:
            raise IllegalTransition(f"未知动词：{verb!r}")
        required, target = VERBS[verb]
        if self.state != required:
            raise IllegalTransition(
                f"非法跃迁：动词 {verb!r} 要求状态 {required}，当前 {self.state}"
            )
        if target not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"非法跃迁：{self.state} → {target}")
        self.history.append({"verb": verb, "from": self.state, "to": target, "at": iso(moment)})
        self.state = target
        self.updated_at = moment
        return self

    def as_dict(self) -> dict:
        return {
            "schema": WIZARD_SCHEMA,
            "draft_id": self.draft_id,
            "state": self.state,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "ttl_seconds": int(self.ttl.total_seconds()),
            "expires_at": iso(self.expires_at()) if self.expires_at() else None,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WizardState":
        return cls(
            draft_id=payload["draft_id"],
            state=payload.get("state", "draft"),
            created_at=parse_iso(payload["created_at"]),
            updated_at=parse_iso(payload.get("updated_at", payload["created_at"])),
            history=list(payload.get("history") or []),
            ttl=timedelta(seconds=int(payload.get("ttl_seconds", DRAFT_TTL.total_seconds()))),
        )


__all__ = [
    "AUDIT_REL",
    "EXCLUDED_PATHS",
    "CHANGED_PATHS_REL",
    "CHANGED_PATHS_SCHEMA",
    "COLLECTION_SCHEMA",
    "COLLECTION_STATE_REL",
    "DRAFT_TTL",
    "LOG_REL",
    "MANIFEST_REL",
    "MANIFEST_SCHEMA",
    "RAW_MANIFEST_REL",
    "STATES",
    "TIMESTAMP_CLASS_PATHS",
    "TRANSITIONS",
    "VERBS",
    "CollectionState",
    "DraftExpired",
    "IllegalTransition",
    "LedgerError",
    "LedgerViolation",
    "VerifyReport",
    "WizardState",
    "WriteReconcileFailed",
    "assert_existing_unchanged",
    "batch_id_for",
    "build_manifest",
    "dumps",
    "iso",
    "load_manifest",
    "loads",
    "parse_iso",
    "read_json",
    "record_changed_paths",
    "record_write",
    "refresh_manifest",
    "scan_tree",
    "utc_now",
    "verify_collection_state",
    "verify_manifest",
    "write_json",
    "write_manifest",
]
