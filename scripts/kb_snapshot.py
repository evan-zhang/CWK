#!/usr/bin/env python3
"""RT-070: 把 NAS 上的库同步成对外服务的检索索引与原文映射。

背景：夜间刷新更新的是 NAS 上的库，而对外服务读的是 2026-09-13 从一份实验语料
一次性导出的本地副本，两者从未打通——线上内容因此冻结了一周，没人看得出来。

这个脚本是两者之间缺的那一段。它只做四件事：

1. **列**：以 NAS 上每个库的摄取账本（``_system/ingest-state.json``）为清单，
   逐条确认 ``originals`` 指向的文本文件在磁盘上真的存在、真的读得到。
2. **建**：把读到的内容灌进一个**新的**索引（名字带时间戳），老索引照常服务。
3. **验**：条数对得上、抽样查得到、抽样读得到原文、总量没有异常暴跌。
4. **切**：全部通过才把别名指向新索引，并写下一份映射表与元数据；否则什么都不动。

两条硬规则，都是踩过的坑换来的：

- **以磁盘上真实存在的文本为准，不看账本的最后状态。** 账本里的 ``status`` 说的是
  「最后一次更新成功没有」，不是「这个文档还能不能用」。2026-09-20 实测：
  spbp-2027 的 155 条全部标着 failed（玄关那边权限不足），但磁盘上 110 份文件都在、
  都读得到。按 status 过滤的话，这个库会在同步后整个消失。
- **失败不切。** 任何一步没过，线上保持昨天的样子并报错——宁可旧，不可半。

OPS 上不留知识库内容：映射表只是 doc_id 到 NAS 路径的指针（几百 KB，可随时重建），
正文一律回 NAS 读。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT))

SNAPSHOT_SCHEMA = "cwk.kb.snapshot-index.v1"
META_SCHEMA = "cwk.kb.snapshot-meta.v1"
PLAN_SCHEMA = "cwk.kb.snapshot-plan.v1"
BUILD_SCHEMA = "cwk.kb.snapshot-build.v1"
STATUS_SCHEMA = "cwk.kb.snapshot-status.v1"
ERROR_SCHEMA = "cwk.kb.snapshot-error.v1"

INGEST_STATE = "_system/ingest-state.json"
#: 文本取自账本的这个字段——规范化后的 markdown，不是原件。
TEXT_FIELD = "raw_path"
DEFAULT_BANKS = ("cwork-3m", "docdb-touqian", "spbp-2027")
#: 读取上限。实测最大一份 27.4MB（内嵌大量表格的审批件），所以不能卡在几 MB——
#: 那会把真实内容当异常丢掉。这个上限只用来挡住"某天谁传了个 1GB 的东西"。
MAX_DOC_BYTES = 64 * 1024 * 1024
#: 进索引的文本上限。超过就截断**只影响检索用的那份投影**，原文在 NAS 上一字不少，
#: 读原文照样翻得到全文。宁可让一份 27MB 的文档"搜得到开头"，也不能让它不进索引。
MAX_INDEX_CHARS = 400_000
#: 相对上一次的最小保留比例。低于它说明这次读取多半出了问题，不是内容真的没了。
MIN_KEEP_RATIO = 0.8
_BANK_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_INDEX_SUFFIX = re.compile(r"\A[a-z0-9-]{1,40}\Z")


class SnapshotError(Exception):
    kind = "snapshot_error"


class UsageError(SnapshotError):
    kind = "usage"


class SourceError(SnapshotError):
    """NAS 侧的问题：挂载没了、账本读不了、文件消失。"""

    kind = "source"


class VerifyError(SnapshotError):
    """新索引没通过校验——这时候绝不切换。"""

    kind = "verify"


@dataclass
class BankResult:
    bank: str
    documents: List[dict] = field(default_factory=list)
    skipped_missing: int = 0
    skipped_unconverted: int = 0
    skipped_oversize: int = 0
    truncated: int = 0
    unreadable: List[str] = field(default_factory=list)
    newest_update: str = ""

    @property
    def usable(self) -> int:
        return len(self.documents)

    def as_dict(self) -> dict:
        return {
            "bank": self.bank,
            "usable": self.usable,
            "skipped_missing": self.skipped_missing,
            "skipped_unconverted": self.skipped_unconverted,
            "skipped_oversize": self.skipped_oversize,
            "truncated": self.truncated,
            "unreadable": len(self.unreadable),
            "newest_update": self.newest_update,
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_bank(bank: str) -> str:
    if not _BANK_RE.match(bank or ""):
        raise UsageError(f"库名非法：{bank!r}")
    return bank


def load_ledger(nas: Path, bank: str) -> Mapping[str, Any]:
    """读一个库的摄取账本。它是文档清单的唯一来源。"""
    path = nas / bank / INGEST_STATE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"{bank}: 找不到账本 {INGEST_STATE}——库不存在或 NAS 没挂上") from exc
    except OSError as exc:
        raise SourceError(f"{bank}: 账本读取失败（{type(exc).__name__}）——NAS 可能掉线了") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"{bank}: 账本不是合法 JSON（{type(exc).__name__}）") from exc
    items = data.get("items")
    if not isinstance(items, dict):
        raise SourceError(f"{bank}: 账本里没有 items")
    return items


def title_from(raw_path: object, filename: str) -> str:
    """从 raw 路径里取一个像样的标题；取不到就退回文件名。

    raw 路径形如 ``raw/classify/c-00-.../2091816583587504129-00-SPBP阶段….md``，
    去掉前缀数字和扩展名剩下的就是人写的标题。
    """
    name = Path(str(raw_path or "")).stem if raw_path else ""
    if name:
        trimmed = re.sub(r"\A\d{6,}[-_]*", "", name)
        trimmed = re.sub(r"\A\d{1,3}[-_]", "", trimmed).strip(" -_")
        if trimmed:
            return trimmed[:200]
    return Path(filename).stem[:200] or filename[:200]


def collect_bank(nas: Path, bank: str, *, read_text: bool = True) -> BankResult:
    """列出一个库当前可用的文档。

    可用 = 账本里有这条，且 ``raw_path`` 指向的规范化文本在磁盘上存在、读得到、不超限。

    **文本来自 ``raw_path``，不是 ``originals``。** originals 指的是原件——它可能已经是
    markdown（工作协同全是），也可能是 docx / png / xlsx（玄关文档库里很常见）；
    raw_path 指向的才是转换后的规范化文本，实测三个库的 converted 记录全部是 .md 且文件都在。
    2026-09-20 第一版按 originals 收，投前资料库当场少了 26 条，被防暴跌闸拦下。

    **只收 converted。** placeholder 表示原件还没转成文本（它的 raw 文件只是占位），
    收进来等于把空壳当文档。至于 failed——那说的是"最后一次更新失败"，
    不是"这个文档不能用"，所以不按它过滤：只要文本还在磁盘上就照收。
    """
    result = BankResult(bank=bank)
    items = load_ledger(nas, bank)
    for doc_id, record in sorted(items.items()):
        if not isinstance(record, Mapping):
            continue
        relative = record.get("raw_path")
        updated = str(record.get("updated_at") or "")
        if updated > result.newest_update:
            result.newest_update = updated
        if str(record.get("status")) == "placeholder":
            result.skipped_unconverted += 1
            continue
        if not isinstance(relative, str) or not relative.lower().endswith(".md"):
            result.skipped_unconverted += 1
            continue
        path = nas / bank / relative
        try:
            size = path.stat().st_size
        except OSError:
            result.skipped_missing += 1
            continue
        if size > MAX_DOC_BYTES:
            result.skipped_oversize += 1
            continue
        text = ""
        if read_text:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                result.unreadable.append(doc_id)
                continue
            if len(text) > MAX_INDEX_CHARS:
                text = text[:MAX_INDEX_CHARS]
                result.truncated += 1
        result.documents.append({
            "bank": bank,
            "doc_id": doc_id,
            "title": title_from(relative, path.name),
            "filename": path.name,
            "text": text,
            "_relative": f"{bank}/{relative}",
        })
    return result


def collect(nas: Path, banks: Sequence[str], *, read_text: bool = True) -> List[BankResult]:
    if not nas.is_dir():
        raise SourceError(f"NAS 挂载点不可用：{nas}")
    return [collect_bank(nas, validate_bank(b), read_text=read_text) for b in banks]


def previous_counts(index_path: Path) -> Dict[str, int]:
    """上一次同步各库多少条——用来判断这次是不是异常暴跌。"""
    try:
        mapping = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    counts: Dict[str, int] = {}
    for value in mapping.values() if isinstance(mapping, dict) else []:
        if isinstance(value, str) and "/" in value:
            counts[value.split("/", 1)[0]] = counts.get(value.split("/", 1)[0], 0) + 1
    return counts


def check_no_collapse(results: Sequence[BankResult], before: Mapping[str, int]) -> List[str]:
    """内容大幅减少时拦住：多半是这次读取出了问题，而不是内容真没了。"""
    problems = []
    for result in results:
        was = before.get(result.bank, 0)
        if was and result.usable < was * MIN_KEEP_RATIO:
            problems.append(
                f"{result.bank}: 只读到 {result.usable} 条，上次有 {was} 条"
                f"（低于 {int(MIN_KEEP_RATIO * 100)}%），拒绝切换"
            )
        if was and result.usable == 0:
            problems.append(f"{result.bank}: 一条都没读到，拒绝切换")
    return problems


def write_index_map(path: Path, results: Sequence[BankResult]) -> int:
    """写 doc_id → NAS 相对路径 的映射。这是指针，不是内容。"""
    mapping = {doc["doc_id"]: doc["_relative"] for result in results for doc in result.documents}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    os.replace(tmp, path)
    return len(mapping)


def write_meta(path: Path, results: Sequence[BankResult], *, index_name: str, started: str) -> dict:
    meta = {
        "schema": META_SCHEMA,
        "generated_at": _now(),
        "started_at": started,
        "index_name": index_name,
        "total": sum(r.usable for r in results),
        "banks": {r.bank: r.as_dict() for r in results},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return meta


# ── 索引侧 ──────────────────────────────────────────────────────────────────


def _client_and_settings():
    from adapters.opensearch_retrieval.cli import _client, _settings  # noqa: PLC0415

    namespace = argparse.Namespace(host=None, index=None, tenant=None, banks=None, timeout=None,
                                   username=None, password=None, top_k=None)
    settings = _settings(namespace)
    return _client(settings), settings


def build_index(documents: Sequence[Mapping[str, Any]], *, index_name: str, tenant: str,
                banks: Sequence[str]) -> dict:
    from adapters.opensearch_retrieval.indexing.builder import IndexBuilder  # noqa: PLC0415

    client, _ = _client_and_settings()
    stats = IndexBuilder(client, index_name=index_name, tenant_id=tenant, banks=list(banks)).build(
        [{k: v for k, v in doc.items() if not k.startswith("_")} for doc in documents]
    )
    return stats.as_dict()


def switch_alias(alias: str, new_index: str) -> dict:
    """把别名原子地指向新索引；顺带列出它原来指向谁，便于回滚。"""
    client, _ = _client_and_settings()
    try:
        current = client.request("GET", f"/_alias/{alias}")
    except Exception:  # noqa: BLE001 - 别名还不存在是正常的
        current = {}
    previous = sorted(current) if isinstance(current, dict) else []
    actions = [{"remove": {"index": name, "alias": alias}} for name in previous]
    actions.append({"add": {"index": new_index, "alias": alias}})
    client.request("POST", "/_aliases", {"actions": actions})
    return {"alias": alias, "now": new_index, "was": previous}


def verify_index(index_name: str, results: Sequence[BankResult], *, tenant: str,
                 banks: Sequence[str]) -> dict:
    """切换前的自查：条数对得上，而且真的查得到。"""
    from adapters.opensearch_retrieval.query.engine import RetrievalQuery  # noqa: PLC0415

    client, settings = _client_and_settings()
    client.request("POST", f"/{index_name}/_refresh")
    counted = client.request("GET", f"/{index_name}/_count")
    indexed = int((counted or {}).get("count", 0))
    if indexed <= 0:
        raise VerifyError(f"新索引 {index_name} 里一条都没有")

    engine = RetrievalQuery(client, index_name=index_name, tenant_id=tenant, banks=list(banks))
    probes = {}
    for result in results:
        if not result.documents:
            continue
        sample = result.documents[len(result.documents) // 2]
        words = [w for w in re.split(r"[\s，。、；：,.;:]+", sample["title"]) if len(w) >= 2]
        query = words[0] if words else sample["title"][:6]
        hits = engine.query(result.bank, query, top_k=3)
        probes[result.bank] = {"query_len": len(query), "hits": len(hits)}
        if not hits:
            raise VerifyError(f"{result.bank}: 用本库文档的标题去查，一条都没命中——索引有问题")
    return {"indexed_chunks": indexed, "probes": probes}


# ── 命令 ────────────────────────────────────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> Tuple[dict, int]:
    results = collect(Path(args.nas), args.banks, read_text=False)
    before = previous_counts(Path(args.index_map)) if args.index_map else {}
    payload = {
        "schema": PLAN_SCHEMA,
        "at": _now(),
        "nas": str(args.nas),
        "banks": [dict(r.as_dict(), previous=before.get(r.bank, 0)) for r in results],
        "total": sum(r.usable for r in results),
        "previous_total": sum(before.values()) or None,
        "blockers": check_no_collapse(results, before),
    }
    return payload, 0 if not payload["blockers"] else 3


def cmd_build(args: argparse.Namespace) -> Tuple[dict, int]:
    started = _now()
    index_map = Path(args.index_map)
    results = collect(Path(args.nas), args.banks)
    before = previous_counts(index_map)
    blockers = check_no_collapse(results, before)
    if blockers:
        raise VerifyError("；".join(blockers))
    documents = [doc for result in results for doc in result.documents]
    if not documents:
        raise VerifyError("一条可用文档都没有，拒绝继续")

    suffix = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    index_name = f"{args.index_prefix}-{suffix}"
    stats = build_index(documents, index_name=index_name, tenant=args.tenant, banks=args.banks)
    verified = verify_index(index_name, results, tenant=args.tenant, banks=args.banks)
    if args.dry_run:
        return {"schema": BUILD_SCHEMA, "dry_run": True, "index_name": index_name,
                "build": stats, "verify": verified,
                "banks": [r.as_dict() for r in results], "switched": False}, 0

    written = write_index_map(index_map, results)
    alias = switch_alias(args.alias, index_name)
    meta = write_meta(Path(args.meta), results, index_name=index_name, started=started)
    return {
        "schema": BUILD_SCHEMA,
        "dry_run": False,
        "index_name": index_name,
        "build": stats,
        "verify": verified,
        "alias": alias,
        "index_map_entries": written,
        "banks": [r.as_dict() for r in results],
        "total": meta["total"],
        "switched": True,
    }, 0


def cmd_status(args: argparse.Namespace) -> Tuple[dict, int]:
    meta_path = Path(args.meta)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        meta = {}
    payload = {"schema": STATUS_SCHEMA, "at": _now(), "meta": meta or None}
    try:
        client, _ = _client_and_settings()
        payload["alias"] = sorted(client.request("GET", f"/_alias/{args.alias}") or {})
    except Exception as exc:  # noqa: BLE001 - 状态查询不该因为后端不可用而失败
        payload["alias_error"] = type(exc).__name__
    return payload, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 NAS 上的库同步成对外服务的索引（输出一律 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--nas", default=os.environ.get("KB_NAS_MOUNT", str(Path.home() / "NAS" / "ai-knowledge")),
                       help="NAS 上知识库共享的挂载点")
        p.add_argument("--banks", default=",".join(DEFAULT_BANKS), help="逗号分隔的库名")
        p.add_argument("--index-map", default=os.environ.get("KB_SNAPSHOT_INDEX", ""),
                       help="doc_id → NAS 路径 的映射表输出路径")
        return p

    plan = common(sub.add_parser("plan", help="只算不写：看这次会同步多少条"))

    build = common(sub.add_parser("build", help="建新索引 → 校验 → 切换"))
    build.add_argument("--meta", default=os.environ.get("KB_SNAPSHOT_META", ""), help="元数据输出路径")
    build.add_argument("--index-prefix", default="cwk-retrieval")
    build.add_argument("--alias", default=os.environ.get("CWK_OPENSEARCH_INDEX", "cwk-retrieval"))
    build.add_argument("--tenant", default=os.environ.get("CWK_RETRIEVAL_TENANT", "default"))
    build.add_argument("--dry-run", action="store_true", help="建索引并校验，但不切换、不写映射表")

    status = sub.add_parser("status", help="现在线上是哪一份、什么时候生成的")
    status.add_argument("--meta", default=os.environ.get("KB_SNAPSHOT_META", ""))
    status.add_argument("--alias", default=os.environ.get("CWK_OPENSEARCH_INDEX", "cwk-retrieval"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
        if getattr(args, "banks", None):
            args.banks = [validate_bank(b.strip()) for b in str(args.banks).split(",") if b.strip()]
        if getattr(args, "index_map", None) == "" and args.command in ("plan", "build"):
            raise UsageError("--index-map 必填（或设 KB_SNAPSHOT_INDEX）")
        if getattr(args, "meta", None) == "" and args.command in ("build", "status"):
            raise UsageError("--meta 必填（或设 KB_SNAPSHOT_META）")
        payload, code = {"plan": cmd_plan, "build": cmd_build, "status": cmd_status}[args.command](args)
    except SnapshotError as exc:
        payload, code = {"schema": ERROR_SCHEMA, "ok": False,
                         "error": {"kind": exc.kind, "message": str(exc)}}, 2
        print(f"kb_snapshot 失败：{exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - CLI 边界：任何意外也要吐 JSON
        payload, code = {"schema": ERROR_SCHEMA, "ok": False,
                         "error": {"kind": type(exc).__name__, "message": str(exc)[:200]}}, 2
        print(f"kb_snapshot 失败：{type(exc).__name__}", file=sys.stderr)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
