#!/usr/bin/env python3
"""RT-044: the build-wizard CLI verbs (create / ingest / status / query).

Usage::

    python3 scripts/kb_wizard.py create --name "我的工作库" \\
        --kb-root /path/to/kb --source cwork --yes
    python3 scripts/kb_wizard.py ingest --kb-root /path/to/kb --since 2026-06-01 --yes
    python3 scripts/kb_wizard.py status --kb-root /path/to/kb
    python3 scripts/kb_wizard.py query  --kb-root /path/to/kb --q 合同

This is the **factory face**: the half of RT-044 that is allowed to write.
It wraps the RT-042 base (``kb_create`` / ``kb_ledger`` / ``kb_doctor``) and
the RT-043 ingest pipeline; it implements no storage logic of its own.

Three properties the verbs share:

**Output is always JSON** (CLI-SPEC §一.2).  Success, refusal and crash all
print one JSON document to stdout, so the RT-045 Skill layer parses one
shape and never has to scrape prose.  Human-readable text goes to stderr.

**``--yes`` is the write gate.**  Without it, ``create`` and ``ingest``
print the confirmation card they *would* act on (``confirmed: false``) and
touch nothing.  The wizard is parameter-driven in v1, so ``--yes`` is where
the conversational "确认吗?" turn lands.

**A dirty destination is refused before the first byte** (RT-044 J1).
``create`` runs :func:`kb_create.assert_destination_is_clean` itself, ahead
of the ``--yes`` branch, so a rejection is a rejection in both modes; the
build then re-checks it inside ``create_kb``.  Overwriting a library is not
a create, and a create that half-overwrites one has already destroyed what
it was refusing to touch.

Exit codes.  ``0`` success, ``1`` the verb ran and the answer is "not
healthy" (``status`` with a failing check, matching ``kb_doctor.py``), ``2``
refused or failed.  CLI-SPEC §四's 4/5/6/7 belong to the management API
surface that RT-044 does not build; the semantic class travels in
``error.kind`` instead, which is what the Skill layer reads.

Route mode.  DOCDB-INGEST-DESIGN §三 declines to hard-code a platform
default, so the wizard proposes one per source — ``cwork`` → ``timeline``
(high-frequency short items), ``docdb`` → ``classify`` (project documents) —
and prints it on the confirmation card, where ``--route-mode`` can override
it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_create  # noqa: E402
import kb_doctor  # noqa: E402
from kb_gateway import RAW_INDEX_REL, parse_root, query_index  # noqa: E402
from kb_ledger import (  # noqa: E402
    MANIFEST_REL,
    LedgerError,
    dumps,
    iso,
    read_json,
    utc_now,
    verify_collection_state,
    verify_manifest,
)
from kb_storage import (  # noqa: E402
    NotFound,
    StorageError,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
)

WIZARD_VERSION = "1.0.0"
CARD_SCHEMA = "cwk.kb.wizard.card.v1"
ERROR_SCHEMA = "cwk.kb.wizard.error.v1"

INGEST_STATE_REL = "_system/ingest-state.json"

#: Where ``ingest`` looks for the RT-043 pipeline.  The env var wins so an
#: operator (and the offline tests) can point the wizard at a different
#: binary without editing code; RT-043 is being built in parallel, so the
#: default may legitimately not exist yet on this branch.
ENV_INGEST_BIN = "KB_INGEST_BIN"
DEFAULT_INGEST_BIN = PROJECT / "scripts" / "kb_ingest.py"
DEFAULT_INGEST_TIMEOUT = 3600

#: DOCDB-INGEST-DESIGN §三: proposed per source, confirmed by the user.
DEFAULT_ROUTE_BY_SOURCE: Dict[str, str] = {"cwork": "timeline", "docdb": "classify"}
ROUTE_MODES = ("timeline", "classify")

#: How much child output a card carries.  Enough to diagnose, bounded so a
#: chatty pipeline cannot turn one card into a log file.
CAPTURE_CHARS = 2000

VERBS = ("create", "ingest", "status", "query")


class WizardRefused(Exception):
    """The verb refused.  Nothing was written."""


# ── cards ───────────────────────────────────────────────────────────────────


def card(verb: str, **fields: object) -> dict:
    payload = {
        "schema": CARD_SCHEMA,
        "wizard_version": WIZARD_VERSION,
        "verb": verb,
        "at": iso(utc_now()),
    }
    payload.update(fields)
    return payload


def error_card(verb: str, kind: str, message: str, **fields: object) -> dict:
    payload = {
        "schema": ERROR_SCHEMA,
        "wizard_version": WIZARD_VERSION,
        "verb": verb,
        "ok": False,
        "error": {"kind": kind, "message": message},
        "at": iso(utc_now()),
    }
    payload.update(fields)
    return payload


def emit(payload: dict) -> None:
    sys.stdout.write(dumps(payload).decode("utf-8"))


def clip(text: str) -> str:
    text = text or ""
    if len(text) <= CAPTURE_CHARS:
        return text
    return text[:CAPTURE_CHARS] + f"…（截断，共 {len(text)} 字）"


# ── create ──────────────────────────────────────────────────────────────────


def route_for(source_type: str, override: Optional[str]) -> str:
    if override:
        return override
    return DEFAULT_ROUTE_BY_SOURCE.get(source_type, "classify")


def spec_from_args(args: argparse.Namespace) -> kb_create.KbSpec:
    """Translate the wizard's flags into an RT-042 :class:`KbSpec`."""
    source_types = [part.strip() for part in args.source.split(",") if part.strip()]
    if not source_types:
        raise WizardRefused("--source 不能为空（cwork|docdb）")
    if args.route_mode and args.route_mode not in ROUTE_MODES:
        raise WizardRefused(f"--route-mode 只接受 {'|'.join(ROUTE_MODES)}")
    sources: List[kb_create.SourceSpec] = []
    for source_type in source_types:
        if source_type not in kb_create.SOURCE_TYPES:
            raise WizardRefused(f"未知源类型：{source_type}（可选 cwork / docdb）")
        sources.append(
            kb_create.SourceSpec(
                source_type=source_type,
                route=route_for(source_type, args.route_mode),
                key_ref=args.key_ref,
                docdb_root=args.docdb_root if source_type == "docdb" else None,
            )
        )
    return kb_create.KbSpec(
        display_name=args.name,
        kb_type=args.kb_type,
        visibility=args.visibility,
        owner_ref=args.owner_ref,
        sources=tuple(sources),
        focus_note=args.focus_note,
    )


def create_card(spec: kb_create.KbSpec, args: argparse.Namespace, *, confirmed: bool) -> dict:
    """The 建库确认卡 (CLI-SPEC 向导步 1/4)."""
    return card(
        "create",
        ok=True,
        confirmed=confirmed,
        kb_code=spec.kb_code,
        display_name=spec.display_name,
        kb_type=spec.kb_type,
        visibility=spec.visibility,
        owner_ref=spec.owner_ref,
        kb_root=args.kb_root,
        backend=args.backend,
        sources=[
            {
                "source_type": source.source_type,
                "route_mode": source.route,
                "key_ref": source.key_ref,
                "docdb_root": source.docdb_root,
            }
            for source in spec.sources
        ],
        route_mode_default_by_source=dict(DEFAULT_ROUTE_BY_SOURCE),
        tree_items=[item.item for item in kb_create.tree_for(spec.source_types)],
    )


def verb_create(args: argparse.Namespace) -> int:
    spec = spec_from_args(args)
    spec.validate()
    backend = build_backend(args.backend, root=parse_root(args.kb_root), prefix=args.prefix)
    try:
        # J1: ahead of the --yes branch, so "目的地脏必拒" is true of a dry
        # run as well as of a build.  Zero writes have happened at this point.
        kb_create.assert_destination_is_clean(backend)
        if not args.yes:
            payload = create_card(spec, args, confirmed=False)
            payload["next"] = "确认无误后加 --yes 执行建库"
            emit(payload)
            return 0
        result = kb_create.create_kb(backend, spec)
    finally:
        close_backend(backend)

    payload = create_card(spec, args, confirmed=True)
    payload["created"] = {
        "dirs": result.created_dirs,
        "dir_count": len(result.created_dirs),
        "files": result.created_files,
        "file_count": len(result.created_files),
        "manifest_entry_count": result.manifest.get("entry_count"),
    }
    payload["next"] = "kb_wizard.py ingest --kb-root <path> --yes"
    emit(payload)
    return 0


# ── ingest ──────────────────────────────────────────────────────────────────


def resolve_ingest_bin(
    override: Optional[str] = None, env: Optional[Dict[str, str]] = None
) -> Path:
    """Locate the RT-043 pipeline: ``--ingest-bin`` → env → repo default."""
    source = os.environ if env is None else env
    if override:
        return Path(override)
    from_env = source.get(ENV_INGEST_BIN)
    if from_env:
        return Path(from_env)
    return DEFAULT_INGEST_BIN


def ingest_argv(bin_path: Path, kb_root: str, since: Optional[str], extra: Sequence[str]) -> List[str]:
    """The child command line.

    Kept as one small function because RT-043's flag set is still landing:
    if ``kb_ingest.py`` ends up spelling these differently, this is the only
    place that changes, and the tests pin the current shape.  ``--yes`` is
    the *wizard's* confirmation gate and is deliberately not forwarded.
    """
    argv = [sys.executable, str(bin_path), "--kb-root", kb_root]
    if since:
        argv += ["--since", since]
    argv += list(extra)
    return argv


def verb_ingest(args: argparse.Namespace) -> int:
    bin_path = resolve_ingest_bin(args.ingest_bin)
    if not bin_path.is_file():
        emit(
            error_card(
                "ingest",
                "ingest_bin_missing",
                f"找不到摄取脚本：{bin_path}。RT-043 的 scripts/kb_ingest.py 尚未落到本分支时，"
                f"用环境变量 {ENV_INGEST_BIN} 或 --ingest-bin 指向可用的实现。",
                ingest_bin=str(bin_path),
                ingest_bin_env=ENV_INGEST_BIN,
            )
        )
        return 2

    command = ingest_argv(bin_path, args.kb_root, args.since, args.ingest_arg)
    if not args.yes:
        emit(
            card(
                "ingest",
                ok=True,
                confirmed=False,
                kb_root=args.kb_root,
                ingest_bin=str(bin_path),
                command=command,
                next="确认无误后加 --yes 执行摄取",
            )
        )
        return 0

    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            command,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            cwd=str(PROJECT),
        )
    except subprocess.TimeoutExpired:
        emit(
            error_card(
                "ingest",
                "timeout",
                f"摄取超过 {args.timeout} 秒未结束，已终止；本次结果未知，请查库内 ingest-state 账。",
                ingest_bin=str(bin_path),
                command=command,
            )
        )
        return 2
    except OSError as exc:
        emit(
            error_card(
                "ingest",
                "spawn_failed",
                f"无法启动摄取脚本：{exc}",
                ingest_bin=str(bin_path),
                command=command,
            )
        )
        return 2

    child: Optional[dict] = None
    parse_error: Optional[str] = None
    try:
        parsed = json.loads(completed.stdout)
        child = parsed if isinstance(parsed, dict) else {"payload": parsed}
    except (ValueError, TypeError) as exc:
        parse_error = str(exc)

    ok = completed.returncode == 0 and child is not None
    payload = card(
        "ingest",
        ok=ok,
        confirmed=True,
        kb_root=args.kb_root,
        ingest_bin=str(bin_path),
        command=command,
        exit_code=completed.returncode,
        ingest=child,
        stderr_text=clip(completed.stderr),
    )
    if child is None:
        payload["stdout_text"] = clip(completed.stdout)
        payload["error"] = {
            "kind": "ingest_output_not_json",
            "message": f"摄取脚本的 stdout 不是 JSON：{parse_error}",
        }
    elif completed.returncode != 0:
        payload["error"] = {
            "kind": "ingest_failed",
            "message": f"摄取脚本退出码 {completed.returncode}",
        }
    emit(payload)
    return 0 if ok else 2


# ── status ──────────────────────────────────────────────────────────────────


def identity_summary(backend) -> dict:
    """kb.json + source.json, best effort — a broken library still reports."""
    summary: dict = {"kb_code": None, "display_name": None, "sources": []}
    try:
        identity = read_json(backend, "kb.json")
        summary["kb_code"] = identity.get("kb_code")
        summary["display_name"] = identity.get("display_name")
    except (NotFound, StorageError, ValueError):
        summary["kb_json"] = "缺失或不可读"
    try:
        sources = read_json(backend, "source.json").get("sources") or []
        summary["sources"] = [
            {
                "source_type": entry.get("source_type"),
                "route_mode": entry.get("route"),
            }
            for entry in sources
            if isinstance(entry, dict)
        ]
    except (NotFound, StorageError, ValueError):
        summary["source_json"] = "缺失或不可读"
    return summary


def ledger_summary(backend) -> dict:
    """The account books' own metadata plus the two structural verdicts."""
    summary: dict = {}
    try:
        manifest = read_json(backend, MANIFEST_REL)
        summary["manifest_version"] = manifest.get("manifest_version")
        summary["entry_count"] = manifest.get("entry_count")
        summary["generated_at"] = manifest.get("generated_at")
        summary["manifest"] = verify_manifest(backend, manifest).as_dict()
    except (NotFound, StorageError, ValueError, LedgerError) as exc:
        summary["manifest"] = {"ok": False, "error": str(exc)}
    try:
        summary["collection_state"] = verify_collection_state(backend).as_dict()
    except (NotFound, StorageError, ValueError, LedgerError) as exc:
        summary["collection_state"] = {"ok": False, "error": str(exc)}
    return summary


def ingest_state_summary(backend) -> dict:
    """B #29 处理状态账 — reported only when the library actually has one."""
    try:
        payload = read_json(backend, INGEST_STATE_REL)
    except NotFound:
        return {"present": False}
    except (StorageError, ValueError) as exc:
        return {"present": True, "ok": False, "error": str(exc)}

    items = payload.get("items")
    rows: List[dict] = []
    if isinstance(items, dict):
        rows = [row for row in items.values() if isinstance(row, dict)]
    elif isinstance(items, list):
        rows = [row for row in items if isinstance(row, dict)]
    by_status: Dict[str, int] = {}
    failed: List[str] = []
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if status.startswith("failed"):
            lineage = row.get("lineage_id")
            if lineage:
                failed.append(str(lineage))
    return {
        "present": True,
        "ok": not failed,
        "items": len(rows),
        "by_status": by_status,
        "failed": sorted(failed)[:10],
        "failed_count": len(failed),
    }


def raw_index_summary(backend) -> dict:
    try:
        payload = read_json(backend, RAW_INDEX_REL)
    except NotFound:
        return {"present": False, "entries": 0}
    except (StorageError, ValueError) as exc:
        return {"present": True, "ok": False, "error": str(exc)}
    entries = payload.get("entries")
    if isinstance(entries, dict):
        count = len(entries)
    elif isinstance(entries, list):
        count = len(entries)
    else:
        count = 0
    return {"present": True, "ok": True, "entries": count}


def doctor_summary(backend) -> dict:
    """Every ``kb_doctor`` check, run here rather than re-implemented."""
    detail: Dict[str, dict] = {}
    failed: List[str] = []
    for name in kb_doctor.CHECKS:
        try:
            report = kb_doctor.CHECK_FUNCS[name](backend).as_dict()
        except (NotFound, StorageError, ValueError, LedgerError) as exc:
            report = {"ok": False, "error": str(exc)}
        detail[name] = report
        if not report.get("ok"):
            failed.append(name)
    return {
        "checks": list(kb_doctor.CHECKS),
        "ok": not failed,
        "failed": failed,
        "detail": detail,
    }


def verb_status(args: argparse.Namespace) -> int:
    backend = build_backend(args.backend, root=parse_root(args.kb_root), prefix=args.prefix)
    try:
        identity = identity_summary(backend)
        ledger = ledger_summary(backend)
        doctor = doctor_summary(backend)
        ingest_state = ingest_state_summary(backend)
        raw_index = raw_index_summary(backend)
    finally:
        close_backend(backend)

    ok = bool(
        doctor["ok"]
        and ledger.get("manifest", {}).get("ok")
        and ledger.get("collection_state", {}).get("ok")
        and ingest_state.get("ok", True)
    )
    emit(
        card(
            "status",
            ok=ok,
            kb_root=args.kb_root,
            backend=args.backend,
            identity=identity,
            ledger=ledger,
            doctor=doctor,
            ingest_state=ingest_state,
            raw_index=raw_index,
        )
    )
    return 0 if ok else 1


# ── query ───────────────────────────────────────────────────────────────────


def verb_query(args: argparse.Namespace) -> int:
    backend = build_backend(args.backend, root=parse_root(args.kb_root), prefix=args.prefix)
    try:
        # Same function the gateway's /query serves, so "同 wizard query 语义"
        # holds by construction rather than by two implementations agreeing.
        result = query_index(backend, args.q, limit=args.limit)
    finally:
        close_backend(backend)
    emit(card("query", ok=True, kb_root=args.kb_root, backend=args.backend, **result))
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kb-root", required=True, help="库根路径，支持 file:// 前缀")
    parser.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
    parser.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KB 建库向导：create / ingest / status / query（输出一律 JSON）"
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    create = sub.add_parser("create", help="建库（包装 kb_create，目的地脏必拒）")
    add_backend_args(create)
    create.add_argument("--name", required=True, help="display_name（A1，必填）")
    create.add_argument("--source", default="cwork", help="源类型，逗号分隔：cwork,docdb")
    create.add_argument(
        "--route-mode",
        choices=ROUTE_MODES,
        help="落位路由；不给则按源提议（cwork=timeline、docdb=classify）",
    )
    create.add_argument("--docdb-root", help="docdb 源的根目录")
    create.add_argument("--type", dest="kb_type", default="personal", choices=("personal", "team"))
    create.add_argument("--visibility", default="private", choices=("private", "shared"))
    create.add_argument("--owner-ref", default="owner-ref-pending")
    create.add_argument("--key-ref", default=kb_create.DEFAULT_KEY_REF, help="env:<变量名>；禁明文")
    create.add_argument("--focus-note", default="")
    create.add_argument("--yes", action="store_true", help="确认执行；不给则只出确认卡")

    ingest = sub.add_parser("ingest", help="摄取（子进程调 RT-043 的 kb_ingest.py）")
    add_backend_args(ingest)
    ingest.add_argument("--since", help="增量起点，透传给摄取脚本")
    ingest.add_argument("--ingest-bin", help=f"摄取脚本路径；默认取 ${ENV_INGEST_BIN} 或仓库内脚本")
    ingest.add_argument(
        "--ingest-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="额外透传给摄取脚本的参数，可重复；值以 -- 开头时写成 --ingest-arg=--flag",
    )
    ingest.add_argument("--timeout", type=int, default=DEFAULT_INGEST_TIMEOUT)
    ingest.add_argument("--yes", action="store_true", help="确认执行；不给则只出确认卡")

    status = sub.add_parser("status", help="汇总账本 verify + ingest-state + doctor 摘要")
    add_backend_args(status)

    query = sub.add_parser("query", help="本地查 raw-index 的 lineage 条目")
    add_backend_args(query)
    query.add_argument("--q", required=True, help="查询词（子串匹配）")
    query.add_argument("--limit", type=int, default=20)

    return parser


HANDLERS = {
    "create": verb_create,
    "ingest": verb_ingest,
    "status": verb_status,
    "query": verb_query,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verb = argv[0] if argv and not argv[0].startswith("-") else "unknown"
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        return HANDLERS[args.verb](args)
    except SystemExit:
        # argparse already wrote its usage message; let it through so
        # ``--help`` and a bad flag keep the exit codes argparse defines.
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        # Refusals and crashes are JSON too (CLI-SPEC §一.2): the Skill layer
        # parses one shape, and a failure it cannot parse is a failure it
        # cannot explain to the user.
        emit(error_card(verb, type(exc).__name__, str(exc)))
        print(f"向导失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
