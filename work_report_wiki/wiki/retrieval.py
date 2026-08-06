# -*- coding: utf-8 -*-
"""Wiki 检索层

设计：
- 问答先召回 L0 chunk，拿到命中的 report_id 集合；
- 再用这些 report_id 反查「与之关联的 Wiki 页面」（topic 聚合页 / index 索引页），
  作为综述/结论优先进入上下文，chunk 仅作细节证据补充。
- 同时提供 topic 路由：让 LLM 判断 query 属于哪个主题，按 page_key/title 定向取页，
  解决「跨数月 IT 项目、几十篇汇报」这类复杂问题的归纳需求。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text

from ..db import get_engine

logger = logging.getLogger(__name__)


def fetch_wiki_pages_for_reports(
    emp_id: int,
    report_ids: Sequence[int],
    include_index: bool = True,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """按 report_id 集合反查关联的 Wiki 页面（综述优先）。

    :param emp_id: 员工隔离维度
    :param report_ids: 来自 chunk 命中的源汇报 id 集合（去重）
    :param include_index: 是否额外纳入该员工的 index 索引页（总入口兜底）
    :param limit: 返回页面上限
    :return: Wiki 页面字典列表，按 status=1 优先、revision 降序，每页含
             page_id/page_key/page_type/title/revision/summary/markdown/source_files
    """
    if not report_ids and not include_index:
        return []
    engine = get_engine()
    with engine.connect() as conn:
        pages: List[Dict[str, Any]] = []
        # 1) 通过这些 report 关联的页面（wiki_page_source 多源关联）
        #    注意：report_id 为空时不能写 IN ()（非法 SQL），直接跳过该反查。
        if report_ids:
            pages = _select_pages(
                conn, emp_id,
                where_extra="""
                    wp.id IN (
                        SELECT DISTINCT page_id FROM wiki_page_source
                        WHERE emp_id = :eid AND report_id IN :rids
                    )
                """,
                params={"eid": emp_id, "rids": tuple(report_ids)},
            )
        # 2) 兜底纳入 index 索引页（按 emp_id 聚合全部页面，作为跨汇报总入口）
        if include_index and len(pages) < limit:
            index_pages = _select_pages(
                conn, emp_id,
                where_extra="wp.page_type = 'index'",
                params={"eid": emp_id},
            )
            seen = {p["page_id"] for p in pages}
            for p in index_pages:
                if p["page_id"] not in seen:
                    pages.append(p)
        return pages[:limit]


def route_wiki_pages_by_topic(
    emp_id: int,
    topics: Sequence[str],
    report_ids: Optional[Sequence[int]] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """主题路由：按 LLM 判定的主题关键词，定向取 Wiki 页面。

    适用于复杂问题（如跨月 IT 项目）。topics 由 engine 调用 AIClient 从 query 提取，
    这里做模糊匹配 page_key/title/summary，并可选叠加 report_id 反查。

    :param emp_id: 员工隔离维度
    :param topics: 主题关键词列表（如 ["项目延期", "线上故障"]）
    :param report_ids: 可选，叠加 chunk 命中的 report_id 反查
    :param limit: 返回页面上限
    :return: 匹配的 Wiki 页面字典列表
    """
    if not topics and not report_ids:
        return []
    engine = get_engine()
    with engine.connect() as conn:
        clauses = []
        params: Dict[str, Any] = {"eid": emp_id}
        if topics:
            like = []
            for i, t in enumerate(topics):
                key = f"t{i}"
                like.append(f"(wp.page_key LIKE :{key} OR wp.title LIKE :{key} OR wp.summary LIKE :{key})")
                params[key] = f"%{t}%"
            clauses.append("(" + " OR ".join(like) + ")")
        if report_ids:
            clauses.append(
                "wp.id IN (SELECT DISTINCT page_id FROM wiki_page_source "
                "WHERE emp_id = :eid AND report_id IN :rids)"
            )
            params["rids"] = tuple(report_ids)
        where_extra = " AND ".join(clauses) if clauses else "1=0"
        pages = _select_pages(
            conn, emp_id,
            where_extra=where_extra,
            params=params,
        )
        return pages[:limit]


def _select_pages(conn, emp_id: int, where_extra: str, params: Dict[str, Any],
                  limit: int = 12) -> List[Dict[str, Any]]:
    """统一查询 wiki_page + 关联 source_files。"""
    rows = conn.execute(text(f"""
        SELECT wp.id, wp.page_key, wp.page_type, wp.title, wp.revision,
               wp.summary, wp.markdown, wp.status, wp.links
        FROM wiki_page wp
        WHERE wp.emp_id = :eid AND wp.status = 1
          AND ({where_extra})
        ORDER BY wp.revision DESC
        LIMIT {int(limit)}
    """), params).mappings().all()

    pages: List[Dict[str, Any]] = []
    for r in rows:
        pid = int(r["id"])
        src_rows = conn.execute(text(
            "SELECT report_id, report_version_id FROM wiki_page_source "
            "WHERE emp_id = :eid AND page_id = :pid"
        ), {"eid": emp_id, "pid": pid}).mappings().all()
        source_files = {str(int(s["report_id"])): int(s["report_version_id"]) for s in src_rows}
        links = r.get("links")
        if isinstance(links, str):
            try:
                links = __import__("json").loads(links)
            except Exception:
                links = []
        elif links is None:
            links = []
        pages.append({
            "page_id": pid,
            "page_key": r["page_key"],
            "page_type": r["page_type"],
            "title": r["title"],
            "revision": int(r["revision"]),
            "summary": r["summary"] or "",
            "markdown": r["markdown"] or "",
            "links": links,
            "source_files": source_files,
        })
    return pages
