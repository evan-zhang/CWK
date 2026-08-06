# -*- coding: utf-8 -*-
"""命令行入口。

子命令：
  bootstrap                  创建 MySQL 表 + ES 索引（幂等）
  ingest --docs FILE         入库（FILE 为 JSON 数组，含 emp_id/report_id/version_id/file_name/content）
  query --q --file-ids       问答检索（无权限过滤；--file-ids 仅作检索范围白名单）
  wiki --file --file-ids     编译单文档 Wiki 并投影（全量返回 claim）
  build-wiki --file-ids      多文档聚合构建 Wiki（参考完整链路）
  build-report-wiki --emp-id 从工作汇报接口构建个人 Wiki（按 emp_id）
  generate-report-summary --emp-id  独立预生成 report_summary（不触发 Wiki 构建，供 --consume-only 消费）

注意：已简化，不再做用户级权限过滤，也不再区分 project。--file-ids 仅作为检索范围约束。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wiki_v3.cli")


def _parse_file_ids(raw: str):
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip()]


def cmd_bootstrap(_args):  # noqa: ANN001
    from . import db, es_store
    db.bootstrap_ddl()
    es_store.ensure_index()
    print("bootstrap done")


def cmd_ingest(args):  # noqa: ANN001
    from .ingest import ingest_documents, IngestDoc
    docs = json.load(open(args.docs, "r", encoding="utf-8"))
    ingest_docs = [IngestDoc(**d) for d in docs]
    stats = ingest_documents(ingest_docs)
    print("ingest:", stats)


def cmd_query(args):  # noqa: ANN001
    from . import es_store
    from .qa.engine import answer
    es_store.ensure_index()
    allowed = _parse_file_ids(args.file_ids) or None
    res = answer(args.q, args.emp_id, user_id=args.user, request_id="cli",
                 top_k=args.top_k, allowed_file_ids=allowed,
                 use_wiki=not args.no_wiki)
    print(json.dumps({
        "answer": res.answer,
        "citations": res.citations,
        "confidence": res.confidence,
        "needs_clarification": res.needs_clarification,
        "notes": res.notes,
    }, ensure_ascii=False, indent=2))


def cmd_wiki(args):  # noqa: ANN001
    from . import es_store
    from .ai_client import AIClient
    from .wiki.compile import WikiCompiler
    from .wiki.project import project_wiki_page
    from .wiki import persist
    es_store.ensure_index()
    allowed = _parse_file_ids(args.file_ids) or None
    chunks = es_store.hybrid_search(
        "", top_k=200, emp_id=args.emp_id,
        allowed_file_ids=[args.file], extra_filters={"report_id": args.file},
    )
    compiler = WikiCompiler(emp_id=args.emp_id)
    page = compiler.compile(
        file_name=f"file_{args.file}", version_id=args.version,
        chunks=chunks, report_id=args.file,
    )
    page_id, _, _ = persist.persist_page(
        emp_id=args.emp_id, folder_id=args.folder, page=page,
    )
    proj = project_wiki_page(
        page, AIClient(), allowed_file_ids=[args.file],
        emp_id=args.emp_id,
    )
    print(json.dumps({
        "page_id": page_id,
        "projected_markdown": proj.markdown,
    }, ensure_ascii=False, indent=2))


def cmd_build_wiki(args):  # noqa: ANN001
    from . import es_store
    from .wiki.build import WikiSourceDoc
    from .wiki.pipeline import run_wiki_pipeline
    es_store.ensure_index()
    file_ids = _parse_file_ids(args.file_ids)
    if not file_ids:
        print("build-wiki 需要 --file-ids（逗号分隔的源文件白名单）")
        return
    docs = []
    for fid in file_ids:
        chunks = es_store.hybrid_search(
            "", top_k=args.top_k, emp_id=args.emp_id,
            allowed_file_ids=[fid], extra_filters={"report_id": fid},
        )
        docs.append(WikiSourceDoc(
            report_id=fid, version_id=args.version,
            file_name=f"file_{fid}", chunks=chunks,
        ))
    res = run_wiki_pipeline(
        emp_id=args.emp_id, docs=docs,
        allowed_file_ids=set(file_ids),
    )
    print(json.dumps({
        "emp_id": res.emp_id,
        "summary_pages": res.summary_page_count,
        "index_page_id": res.index_page_id,
        "pages": res.pages,
        "lint": res.lint,
    }, ensure_ascii=False, indent=2))


def cmd_build_report_wiki(args):  # noqa: ANN001
    from .wiki.report import build_report_wiki
    result = build_report_wiki(
        emp_id=args.emp_id, begin_time=args.begin, end_time=args.end,
        folder_id=args.folder, consume_only=getattr(args, "consume_only", False),
        page_size=getattr(args, "page_size", 0) or 0,
        max_pages=getattr(args, "max_pages", 0) or 0,
    )
    timings = result.get("timings", {}) if isinstance(result, dict) else getattr(result, "timings", {})
    if timings:
        total_all = round(sum(v for k, v in timings.items() if k != "total"), 3)
        print("\n=== Wiki 构建各阶段耗时（跨批次累计）===", flush=True)
        for phase in ("MAP", "TAXONOMY", "REDUCE", "FINALIZE"):
            if phase in timings:
                print(f"  {phase:10s}: {timings[phase]:.3f}s", flush=True)
        print(f"  {'TOTAL':10s}: {total_all:.3f}s", flush=True)
        print("========================================\n", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_generate_report_summary(args):  # noqa: ANN001
    """独立预生成 report_summary：遍历某 emp 的汇报，逐篇调 ensure_report_summary。

    不触发 Wiki 构建——只把汇报级提炼（markdown+entities+concepts）写入
    report_summary 表，供后续 build-report-wiki --consume-only 纯消费。

    ensure_report_summary 已内置幂等：status=1 且 content_hash 匹配的篇目
    直接跳过（不重复调 LLM）；--force 可强制全部重新生成。
    """
    from .wiki.report import (
        fetch_report_ids, fetch_report_content, _resolve_report_version,
        ensure_report_summary, read_report_summary,
    )
    emp_id = args.emp_id
    report_ids = fetch_report_ids(emp_id, args.begin, args.end)
    logger.info("generate-report-summary: emp_id=%s got %d report ids", emp_id, len(report_ids))

    generated = 0
    skipped = 0
    failed = 0
    for rid in report_ids:
        try:
            text = fetch_report_content(rid)
            if not text:
                logger.warning("report %s 无内容，跳过", rid)
                failed += 1
                continue
            version = _resolve_report_version(emp_id, rid, text)
            # force=False 时，已 done 且 hash 匹配的篇目跳过，不调 LLM
            if not args.force and read_report_summary(rid) is not None:
                skipped += 1
                continue
            md = ensure_report_summary(rid, text, version, emp_id=emp_id)
            if md is None:
                failed += 1
            else:
                generated += 1
        except Exception as e:  # noqa: BLE001
            logger.error("report %s generate failed: %s", rid, e)
            failed += 1
    result = {
        "emp_id": emp_id,
        "report_count": len(report_ids),
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_verify(_args):  # noqa: ANN001
    import pytest
    rc = pytest.main(["-q", str(__import__("pathlib").Path(__file__).resolve().parent / "tests")])
    sys.exit(rc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiki_v3", description="RAG+Wiki")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bootstrap", help="创建 MySQL 表 + ES 索引")

    pi = sub.add_parser("ingest", help="入库文档")
    pi.add_argument("--docs", required=True, help="JSON 数组文件路径")

    pq = sub.add_parser("query", help="问答检索（无权限过滤）")
    pq.add_argument("--emp-id", type=int, required=True)
    pq.add_argument("--user", type=int, default=0, help="仅作标识，不参与过滤")
    pq.add_argument("--q", required=True)
    pq.add_argument("--file-ids", help="检索范围白名单（逗号分隔）；缺省全量")
    pq.add_argument("--top-k", type=int, default=settings.top_k)
    pq.add_argument("--no-wiki", action="store_true", help="禁用 Wiki 综述层（仅 chunk 检索）")

    pw = sub.add_parser("wiki", help="编译并投影单文档 Wiki")
    pw.add_argument("--emp-id", type=int, required=True)
    pw.add_argument("--user", type=int, default=0)
    pw.add_argument("--file", type=int, required=True)
    pw.add_argument("--version", type=int, default=1)
    pw.add_argument("--folder", type=int, default=0, help="归属文件夹 id（0=根）")
    pw.add_argument("--file-ids", help="检索范围白名单；缺省仅含 --file")

    pb = sub.add_parser("build-wiki", help="多文档聚合构建 Wiki（完整链路）")
    pb.add_argument("--emp-id", type=int, required=True)
    pb.add_argument("--user", type=int, default=0)
    pb.add_argument("--file-ids", required=True, help="源文件白名单（逗号分隔）")
    pb.add_argument("--version", type=int, default=1)
    pb.add_argument("--top-k", type=int, default=200)

    pr = sub.add_parser("build-report-wiki", help="从工作汇报接口构建个人 Wiki（按 emp_id）")
    pr.add_argument("--emp-id", type=int, required=True)
    pr.add_argument("--begin", default="2026-08-01 00:00:00", help="时间范围起点")
    pr.add_argument("--end", default="2026-08-23 23:59:59", help="时间范围终点")
    pr.add_argument("--folder", type=int, default=0, help="归属文件夹 id（0=根）")
    pr.add_argument("--consume-only", action="store_true",
                   help="纯消费模式：仅用 report_summary 已生成(done)的提炼，缺失则跳过该篇（不调用 LLM）。"
                        "生产环境由工作协同系统负责提炼生成。")
    pr.add_argument("--page-size", type=int, default=0,
                   help="分页拉取汇报的每批大小（0=不分页，一次拉全部）；用于近一年等大规模构建")
    pr.add_argument("--max-pages", type=int, default=0,
                   help="分页拉取最多批次数（0=不限）；配合 --page-size 做断点续跑/限流")

    pg = sub.add_parser("generate-report-summary",
                        help="独立预生成 report_summary（不触发 Wiki 构建，供 --consume-only 消费）")
    pg.add_argument("--emp-id", type=int, required=True)
    pg.add_argument("--begin", default="2026-08-01 00:00:00", help="时间范围起点")
    pg.add_argument("--end", default="2026-08-23 23:59:59", help="时间范围终点")
    pg.add_argument("--force", action="store_true",
                   help="强制全部重新生成（忽略已有 done 的提炼，每篇都调一次 LLM）")

    sub.add_parser("verify", help="运行测试")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {
        "bootstrap": cmd_bootstrap,
        "ingest": cmd_ingest,
        "query": cmd_query,
        "wiki": cmd_wiki,
        "build-wiki": cmd_build_wiki,
        "build-report-wiki": cmd_build_report_wiki,
        "generate-report-summary": cmd_generate_report_summary,
        "verify": cmd_verify,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
