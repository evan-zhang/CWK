#!/usr/bin/env python3
"""RT-042: create a knowledge base — the B-table 26-item directory tree.

Usage::

    python3 scripts/kb_create.py --name "我的工作库" --root /path/to/kb-root
    python3 scripts/kb_create.py --name "文档库" --sources docdb \\
        --docdb-root /玄关/合同 --backend nas

What it guarantees:

- ``kb_code`` is **128 bits** of ``secrets`` randomness (32 hex chars).  The
  display name never appears in a path, so guessing a library id is not a
  matter of guessing a name.
- Exactly the B-table items that apply to the configured sources are
  created.  ``KB_TREE`` below is the single declaration of that table; the
  cwork-only items (raw month tree, timelines, reply-state) are skipped for a
  docdb-only library and the docdb-only item is skipped for a cwork-only one.
- ``kb.json`` carries **identity and references only**.  Source and schedule
  settings live in ``source.json`` / ``schedule.json``, which are the sole
  authority (KB-PARAMETERS B #1, #20-21); duplicating them into kb.json is
  what makes two files disagree later.
- v1-rejected source parameters (``keywords`` / ``senders`` / ``recipients``
  / ``project_tags`` / ``min_relevance``) are refused before anything is
  written — the contract says "收到即 400，不落盘", so the CLI exits without
  creating the library at all.

Every write goes through :func:`kb_ledger.record_write`, so the build
fails loudly against a backend that does not actually store bytes.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_kb_ledger import (  # noqa: E402
    AUDIT_REL,
    CHANGED_PATHS_REL,
    CHANGED_PATHS_SCHEMA,
    COLLECTION_SCHEMA,
    COLLECTION_STATE_REL,
    LOG_REL,
    MANIFEST_REL,
    RAW_MANIFEST_REL,
    dumps,
    iso,
    record_write,
    refresh_manifest,
    utc_now,
)
from cwk_kb_storage import (  # noqa: E402
    StorageBackend,
    assert_no_plaintext_credential_flags,
    build_backend,
)

KB_SCHEMA = "cwk.kb.identity.v1"
SOURCE_SCHEMA = "cwk.kb.source.v1"
SCHEDULE_SCHEMA = "cwk.kb.schedule.v1"
MEMBERS_SCHEMA = "cwk.kb.members.v1"
TAXONOMY_SCHEMA = "cwk.kb.taxonomy.v1"

KB_CODE_BITS = 128

SOURCE_TYPES = ("cwork", "docdb")

# KB-PARAMETERS A2: accepted in a later version, refused today.  Accepting
# them silently would create libraries whose stored config claims a filter
# that nothing implements.
REJECTED_SOURCE_PARAMS = (
    "keywords",
    "senders",
    "recipients",
    "project_tags",
    "min_relevance",
)

DEFAULT_LANES = ("inbox", "outbox")
DEFAULT_WINDOW_MODE = "auto-3m"
DEFAULT_FREQUENCY = "daily@22:00"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TAXONOMY_DIMENSIONS = ("内容类型", "项目")


class CreateError(Exception):
    """A build refused before touching storage."""


# ── the B table ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreeItem:
    """One row of KB-PARAMETERS B (库目录树规范).

    ``item`` is the B-table number so the acceptance check can be read next
    to the contract.  ``sources`` is the "适用源" column: ``()`` means the
    item applies to every library.
    """

    item: str
    path: str
    kind: str  # "dir" | "file"
    sources: Tuple[str, ...] = ()
    note: str = ""

    def applies_to(self, configured: Sequence[str]) -> bool:
        if not self.sources:
            return True
        return any(source in configured for source in self.sources)


# 30 items: v1.0 的 23 项 + v1.1 的 #24/#25 + v1.3 的 #2c/#26-#29（#2b 已被统一路由取代删除）。
# Placement note: the B table names files without always naming a directory.
# The convention used here — root for identity/authority files, ``_system/``
# for machine-maintained indexes — follows the two rows that *are* explicit
# (#14 ``_system/taxonomy.json`` and #24/#25 ``_system/...``), and #3, which
# puts the raw manifest in ``raw/_system/``.
KB_TREE: Tuple[TreeItem, ...] = (
    TreeItem("1", "kb.json", "file", note="A1 身份 + 子配置引用（子配置为唯一权威）"),
    TreeItem("2", "raw", "dir", note="raw/ 用户分类树（route=classify）或月分卷（timeline）；两源通用"),
    TreeItem("2c", "originals", "dir", note="存档层：原样字节/write-once；cwork=月分区，docdb=云端镜像路径"),
    TreeItem("3", RAW_MANIFEST_REL, "file", note="raw 账本，每次写 raw 后更新"),
    TreeItem("4", "timelines", "dir", ("cwork",), note="timelines/{id}/snapshots/"),
    TreeItem("5", "wiki/AGENTS.md", "file", note="taxonomy 或 focus_note 变更时重生成"),
    TreeItem("6", "wiki/index.md", "file", note="每页增改同步"),
    TreeItem("7", LOG_REL, "file", note="只追加运行日志（幂等断言豁免项）"),
    TreeItem("8", "wiki/summaries", "dir", note="wiki/summaries/{id}.md"),
    TreeItem("9", "wiki/entities", "dir", note="实体页，复利更新"),
    TreeItem("10", "wiki/topics", "dir", note="主题页，复利更新"),
    TreeItem("11", "wiki/categories", "dir", note="taxonomy 定稿后生成"),
    TreeItem("12", "wiki/daily", "dir", note="每晚增长"),
    TreeItem("13", "wiki/sources", "dir", note="分卷增长"),
    TreeItem("14", "_system/taxonomy.json", "file", note="默认版，confirm/patch 时 version+1"),
    TreeItem("15", "_system/entity-catalog.json", "file"),
    TreeItem("16", "_system/search-index.json", "file"),
    TreeItem("17", "_system/query-contract.json", "file", note="含 contract_version，平台可升级"),
    TreeItem("18", "_system/reply-state.json", "file", ("cwork",), note="指向当前权威 snapshot"),
    TreeItem("19", MANIFEST_REL, "file", note="每写操作后对账"),
    TreeItem("20", "source.json", "file", note="源配置唯一权威"),
    TreeItem("21", "schedule.json", "file", note="调度配置唯一权威"),
    TreeItem("22", AUDIT_REL, "file", note="哈希链，由工厂审计接收器唯一写入"),
    TreeItem("23", "kb_members.json", "file", note="OPS 集中索引为权威，此为审计副本"),
    TreeItem("24", COLLECTION_STATE_REL, "file", note="每源每 lane 游标 + 断点"),
    TreeItem("25", CHANGED_PATHS_REL, "file", note="增量变更记录"),
    TreeItem("26", "_system/classify_rules.json", "file", note="分类路由规则（静态层+AI 层），版本化"),
    TreeItem("27", "_system/provenance.json", "file", note="派生/挪动/拆分溯源账"),
    TreeItem("28", "_system/raw-index.json", "file", note="lineage 定位账：ID→path（路径只是缓存，对账主键=ID+sha256）"),
    TreeItem("29", "_system/ingest-state.json", "file", note="摄取处理状态账：防静默丢件（originals↔index 覆盖率对账）"),
)

assert len(KB_TREE) == 30, "B 表 v1.3 是 30 项——改这张表就是改验收合同"


def tree_for(sources: Sequence[str]) -> Tuple[TreeItem, ...]:
    """Return the B-table rows that apply to a library with ``sources``."""
    return tuple(item for item in KB_TREE if item.applies_to(sources))


# ── spec ────────────────────────────────────────────────────────────────────


def new_kb_code() -> str:
    """128 bits of randomness, hex-encoded (KB-PARAMETERS A1)."""
    return secrets.token_hex(KB_CODE_BITS // 8)


@dataclass
class SourceSpec:
    """One entry of the A2 source array."""

    source_type: str
    route: str = "classify"
    key_ref: str = "env:CWK_CWORK_KEY"
    lanes: Tuple[str, ...] = DEFAULT_LANES
    window_mode: str = DEFAULT_WINDOW_MODE
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    docdb_root: Optional[str] = None
    recursive: bool = True
    include_types: Optional[Tuple[str, ...]] = None

    def as_dict(self) -> dict:
        if self.source_type == "cwork":
            payload = {
                "source_type": "cwork",
                "key_ref": self.key_ref,
                "lanes": list(self.lanes),
                "window": {"mode": self.window_mode},
            }
            if self.window_start:
                payload["window"]["start"] = self.window_start
            if self.window_end:
                payload["window"]["end"] = self.window_end
            payload["route"] = self.route
            return payload
        payload = {
            "source_type": "docdb",
            "key_ref": self.key_ref,
            "root_folder": self.docdb_root,
            "recursive": self.recursive,
        }
        if self.include_types:
            payload["include_types"] = list(self.include_types)
        payload["route"] = self.route
        return payload


@dataclass
class KbSpec:
    """Everything a build needs.  Deterministic: no clock, no RNG inside build.

    ``kb_code`` and ``created_at`` are fields rather than being drawn during
    the build, so the same spec replayed against two backends produces
    byte-identical trees.  That is what makes the J1 comparison meaningful.
    """

    display_name: str
    kb_code: str = field(default_factory=new_kb_code)
    kb_type: str = "personal"
    visibility: str = "private"
    owner_ref: str = "owner-ref-pending"
    created_at: datetime = field(default_factory=utc_now)
    sources: Tuple[SourceSpec, ...] = ()
    frequency: str = DEFAULT_FREQUENCY
    timezone: str = DEFAULT_TIMEZONE
    reply_refresh: bool = True
    refine_batch: Optional[int] = None
    focus_note: str = ""

    @property
    def source_types(self) -> Tuple[str, ...]:
        return tuple(source.source_type for source in self.sources)

    def validate(self) -> None:
        if not self.display_name.strip():
            raise CreateError("--name 不能为空")
        if len(self.kb_code) != KB_CODE_BITS // 4:
            raise CreateError(
                f"kb_code 必须是 {KB_CODE_BITS} 位随机（{KB_CODE_BITS // 4} 个十六进制字符）"
            )
        if self.kb_type not in ("personal", "team"):
            raise CreateError(f"未知 kb_type：{self.kb_type}")
        if self.visibility not in ("private", "shared"):
            raise CreateError(f"未知 visibility：{self.visibility}")
        if not self.sources:
            raise CreateError("至少要配置一个源（--sources cwork|docdb）")
        seen = set()
        for source in self.sources:
            if source.source_type not in SOURCE_TYPES:
                raise CreateError(f"未知源类型：{source.source_type}")
            if source.source_type in seen:
                raise CreateError(f"源类型重复：{source.source_type}")
            seen.add(source.source_type)
            if source.source_type == "docdb" and not source.docdb_root:
                raise CreateError("docdb 源必须给 --docdb-root")


def reject_v1_unsupported(params: Dict[str, object]) -> None:
    """Refuse the A2 parameters reserved for v2.  Nothing is written."""
    present = sorted(name for name in REJECTED_SOURCE_PARAMS if params.get(name))
    if present:
        raise CreateError(
            "v1 拒收以下预留参数（收到即拒绝，不落盘）：" + ", ".join(present)
        )


# ── initial file contents ───────────────────────────────────────────────────


def kb_identity(spec: KbSpec) -> dict:
    """B #1 — identity plus references.  No source or schedule values here."""
    return {
        "schema": KB_SCHEMA,
        "kb_code": spec.kb_code,
        "display_name": spec.display_name,
        "kb_type": spec.kb_type,
        "visibility": spec.visibility,
        "owner_ref": spec.owner_ref,
        "created_at": iso(spec.created_at),
        "refs": {
            "source": "source.json",
            "schedule": "schedule.json",
            "taxonomy": "_system/taxonomy.json",
            "members": "kb_members.json",
            "manifest": MANIFEST_REL,
            "collection_state": COLLECTION_STATE_REL,
        },
        "authority_note": (
            "子配置为唯一权威：source.json / schedule.json / taxonomy.json 的字段"
            "不在本文件重复，本文件只存 A1 身份与引用。"
        ),
    }


def source_config(spec: KbSpec) -> dict:
    return {
        "schema": SOURCE_SCHEMA,
        "kb_code": spec.kb_code,
        "sources": [source.as_dict() for source in spec.sources],
        "rejected_params_v1": list(REJECTED_SOURCE_PARAMS),
    }


def schedule_config(spec: KbSpec) -> dict:
    fetch: dict = {"frequency": spec.frequency, "timezone": spec.timezone}
    if "cwork" in spec.source_types:
        fetch["reply_refresh"] = spec.reply_refresh
    refine: dict = {}
    if spec.refine_batch is not None:
        refine["batch_size"] = spec.refine_batch
    return {
        "schema": SCHEDULE_SCHEMA,
        "kb_code": spec.kb_code,
        "fetch": fetch,
        "refine": refine,
        "heartbeat_note": (
            "单 launchd 心跳（30 分钟）+ 工厂按本文件判到期；不按库生成 launchd。"
        ),
    }


def members_copy(spec: KbSpec) -> dict:
    """B #23 — the audit copy.  OPS members-index stays the authority."""
    return {
        "schema": MEMBERS_SCHEMA,
        "kb_code": spec.kb_code,
        "authority": "ops:members-index",
        "authority_note": (
            "OPS 集中索引 members-index.json 是权威，本文件是库内审计副本；"
            "成员变更以 OPS 为准，RT-043 接入集中索引后由工厂单写者同步。"
        ),
        "members": [
            {
                "owner_ref": spec.owner_ref,
                "role": "owner",
                "added_at": iso(spec.created_at),
            }
        ],
    }


def members_index_record(spec: KbSpec) -> dict:
    """The record RT-043 will push to the OPS central members index.

    Kept here (and covered by tests) so the library-side copy and the future
    central index are generated from one definition rather than two.
    """
    return {
        "kb_code": spec.kb_code,
        "owner_ref": spec.owner_ref,
        "kb_type": spec.kb_type,
        "visibility": spec.visibility,
        "members": [member["owner_ref"] for member in members_copy(spec)["members"]],
        "updated_at": iso(spec.created_at),
    }


def taxonomy_default(spec: KbSpec) -> dict:
    return {
        "schema": TAXONOMY_SCHEMA,
        "kb_code": spec.kb_code,
        "version": 1,
        "dimensions": [
            {"name": name, "values": [], "dynamic": True}
            for name in DEFAULT_TAXONOMY_DIMENSIONS
        ],
        "entity_candidates": [],
        "topic_candidates": [],
        "focus_note": spec.focus_note,
        "version_note": "focus_note 变更也 bump version。",
    }


def query_contract(spec: KbSpec) -> dict:
    return {
        "schema": "cwk.kb.query-contract.v1",
        "kb_code": spec.kb_code,
        "contract_version": 1,
        "template": "fixed",
        "upgrade_policy": "平台可统一升级 contract_version；不接受单库定制。",
    }


def initial_files(spec: KbSpec) -> Dict[str, bytes]:
    """Return ``{path: bytes}`` for every *file* row that applies."""
    created = iso(spec.created_at)
    payloads: Dict[str, dict] = {
        "kb.json": kb_identity(spec),
        RAW_MANIFEST_REL: {
            "schema": "cwk.kb.raw-manifest.v1",
            "kb_code": spec.kb_code,
            "entries": {},
            "entry_count": 0,
        },
        "_system/taxonomy.json": taxonomy_default(spec),
        "_system/entity-catalog.json": {
            "schema": "cwk.kb.entity-catalog.v1",
            "kb_code": spec.kb_code,
            "entities": [],
        },
        "_system/search-index.json": {
            "schema": "cwk.kb.search-index.v1",
            "kb_code": spec.kb_code,
            "documents": [],
        },
        "_system/query-contract.json": query_contract(spec),
        "_system/reply-state.json": {
            "schema": "cwk.kb.reply-state.v1",
            "kb_code": spec.kb_code,
            "reports": {},
        },
        "source.json": source_config(spec),
        "schedule.json": schedule_config(spec),
        "kb_members.json": members_copy(spec),
        "_system/classify_rules.json": {
            "schema": "cwk.kb.classify-rules.v1",
            "kb_code": spec.kb_code,
            "version": 0,
            "static_rules": [],
            "ai_layer": {"enabled": True},
            "note": "向导第 5 步定稿后 version=1",
        },
        "_system/provenance.json": {
            "schema": "cwk.kb.provenance.v1",
            "kb_code": spec.kb_code,
            "records": {},
        },
        "_system/raw-index.json": {
            "schema": "cwk.kb.raw-index.v1",
            "kb_code": spec.kb_code,
            "entries": {},
        },
        "_system/ingest-state.json": {
            "schema": "cwk.kb.ingest-state.v1",
            "kb_code": spec.kb_code,
            "items": {},
        },
        COLLECTION_STATE_REL: {
            "schema": COLLECTION_SCHEMA,
            "kb_code": spec.kb_code,
            "sources": {
                source.source_type: {
                    lane: {
                        "cursor": None,
                        "collected": [],
                        "pending": [],
                        "batch_id": None,
                        "last_run_at": None,
                        "interrupted": False,
                    }
                    for lane in (source.lanes if source.source_type == "cwork" else ("files",))
                }
                for source in spec.sources
            },
        },
        CHANGED_PATHS_REL: {
            "schema": CHANGED_PATHS_SCHEMA,
            "kb_code": spec.kb_code,
            "batches": [],
        },
    }

    text: Dict[str, str] = {
        "wiki/AGENTS.md": (
            f"# {spec.display_name} — 库内 Agent 说明\n\n"
            f"- kb_code: `{spec.kb_code}`\n"
            f"- 分类维度：{'、'.join(DEFAULT_TAXONOMY_DIMENSIONS)}（默认版，"
            "taxonomy confirm 后重生成）\n"
            "- 引文纪律：答案必须引权威 snapshot 的字节子串；引不到就拒答。\n"
        ),
        "wiki/index.md": (
            f"# {spec.display_name}\n\n"
            "建库完成，尚未初灌。索引页在每次页面增改后同步。\n"
        ),
        LOG_REL: f"# 运行日志\n\n- {created} 建库：kb_code={spec.kb_code}\n",
        AUDIT_REL: dumps_jsonl_first_line(spec, created),
    }

    applicable = {item.path for item in tree_for(spec.source_types) if item.kind == "file"}
    out: Dict[str, bytes] = {}
    for path, payload in payloads.items():
        if path in applicable:
            out[path] = dumps(payload)
    for path, body in text.items():
        if path in applicable:
            out[path] = body.encode("utf-8")
    return out


def dumps_jsonl_first_line(spec: KbSpec, created: str) -> str:
    """Open the audit chain with a genesis record.

    The chain is written by the factory's audit receiver from here on; this
    single line only establishes ``prev_hash`` for the first real event.
    """
    import json

    genesis = {
        "schema": "cwk.kb.audit.v1",
        "event": "kb.created",
        "kb_code": spec.kb_code,
        "owner_ref": spec.owner_ref,
        "at": created,
        "prev_hash": None,
    }
    return json.dumps(genesis, ensure_ascii=False, sort_keys=True) + "\n"


# ── build ───────────────────────────────────────────────────────────────────


@dataclass
class BuildResult:
    kb_code: str
    created_dirs: List[str]
    created_files: List[str]
    manifest: dict
    tree_items: List[str]

    def as_dict(self) -> dict:
        return {
            "kb_code": self.kb_code,
            "tree_items": self.tree_items,
            "dirs": self.created_dirs,
            "files": self.created_files,
            "manifest_entry_count": self.manifest.get("entry_count"),
        }


def create_kb(backend: StorageBackend, spec: KbSpec) -> BuildResult:
    """Materialise the B-table tree for ``spec`` on ``backend``."""
    spec.validate()
    items = tree_for(spec.source_types)
    files = initial_files(spec)

    created_dirs: List[str] = []
    for item in items:
        if item.kind == "dir":
            backend.mkdir(item.path)
            created_dirs.append(item.path)

    created_files: List[str] = []
    for path in sorted(files):
        # record_write, not backend.write: every build is reconciled as it
        # goes, so an inert backend fails here instead of at acceptance.
        record_write(backend, path, files[path])
        created_files.append(path)

    manifest = refresh_manifest(
        backend, kb_code=spec.kb_code, generated_at=spec.created_at, allow_new=created_files
    )
    return BuildResult(
        kb_code=spec.kb_code,
        created_dirs=created_dirs,
        created_files=created_files,
        manifest=manifest,
        tree_items=[item.item for item in items],
    )


def audit_tree(backend: StorageBackend, sources: Sequence[str]) -> List[str]:
    """Return the B-table items that are missing for a library.

    Used by the acceptance check ("26 项按适用源逐项存在性"); directories are
    checked for existence, files for existence *and* non-emptiness.
    """
    missing: List[str] = []
    for item in tree_for(sources):
        if not backend.exists(item.path):
            missing.append(f"#{item.item} {item.path}（缺失）")
        elif item.kind == "file" and not backend.read(item.path).strip():
            missing.append(f"#{item.item} {item.path}（为空）")
    return missing


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="建库：生成 KB-PARAMETERS B 表 26 项目录树（按适用源裁剪）"
    )
    parser.add_argument("--name", required=True, help="display_name（A1，必填）")
    parser.add_argument("--type", dest="kb_type", default="personal", choices=("personal", "team"))
    parser.add_argument("--visibility", default="private", choices=("private", "shared"))
    parser.add_argument("--owner-ref", default="owner-ref-pending", help="稳定工号标识（Key 派生）")
    parser.add_argument(
        "--sources",
        default="cwork",
        help="逗号分隔的源类型：cwork,docdb（默认 cwork）",
    )
    parser.add_argument("--key-ref", default="env:CWK_CWORK_KEY", help="Key 的 .env 引用，禁明文")
    parser.add_argument("--lanes", default=",".join(DEFAULT_LANES))
    parser.add_argument("--window", dest="window_mode", default=DEFAULT_WINDOW_MODE)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--docdb-root")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.add_argument("--include-types")
    parser.add_argument("--schedule", dest="frequency", default=DEFAULT_FREQUENCY)
    parser.add_argument("--tz", dest="timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--no-reply-refresh", dest="reply_refresh", action="store_false")
    parser.add_argument("--refine-batch", type=int)
    parser.add_argument("--focus-note", default="")
    parser.add_argument(
        "--backend",
        default="local",
        choices=("local", "memory", "nas"),
        help="local 需要 --root；nas 从 CWK_NAS_KB_* 环境变量读凭据",
    )
    parser.add_argument("--root", help="local 后端的库根目录")
    parser.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
    # v1-rejected parameters are accepted by the parser only so the CLI can
    # answer with the contract's error instead of argparse's "unknown option".
    for name in REJECTED_SOURCE_PARAMS:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, default=None)
    return parser


def spec_from_args(args: argparse.Namespace) -> KbSpec:
    reject_v1_unsupported({name: getattr(args, name, None) for name in REJECTED_SOURCE_PARAMS})
    requested = [part.strip() for part in args.sources.split(",") if part.strip()]
    sources: List[SourceSpec] = []
    for source_type in requested:
        if source_type == "cwork":
            sources.append(
                SourceSpec(
                    source_type="cwork",
                    key_ref=args.key_ref,
                    lanes=tuple(part.strip() for part in args.lanes.split(",") if part.strip()),
                    window_mode=args.window_mode,
                    window_start=args.start_date,
                    window_end=args.end_date,
                )
            )
        elif source_type == "docdb":
            sources.append(
                SourceSpec(
                    source_type="docdb",
                    key_ref=args.key_ref,
                    docdb_root=args.docdb_root,
                    recursive=args.recursive,
                    include_types=(
                        tuple(part.strip() for part in args.include_types.split(","))
                        if args.include_types
                        else None
                    ),
                )
            )
        else:
            raise CreateError(f"未知源类型：{source_type}")
    return KbSpec(
        display_name=args.name,
        kb_type=args.kb_type,
        visibility=args.visibility,
        owner_ref=args.owner_ref,
        sources=tuple(sources),
        frequency=args.frequency,
        timezone=args.timezone,
        reply_refresh=args.reply_refresh,
        refine_batch=args.refine_batch,
        focus_note=args.focus_note,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        spec = spec_from_args(args)
        backend = build_backend(
            "local" if args.backend == "local" else args.backend,
            root=args.root,
            prefix=args.prefix,
        )
        result = create_kb(backend, spec)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"建库失败：{exc}", file=sys.stderr)
        return 2
    print(f"kb_code: {result.kb_code}")
    print(f"B 表项：{len(result.tree_items)} 项 → {', '.join(result.tree_items)}")
    print(f"目录 {len(result.created_dirs)} 个 / 文件 {len(result.created_files)} 个")
    print(f"root-manifest 登记 {result.manifest['entry_count']} 条")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
