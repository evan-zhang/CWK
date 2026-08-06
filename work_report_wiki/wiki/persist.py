# -*- coding: utf-8 -*-
"""Wiki 页面落库（将编译结果持久化到 MySQL）。

落库内容（单事务内）：
  wiki_page            ->  一页一行（含 folder_id / page_key）
  wiki_page_source     -> 页面与源文件的关联（多源），用于按 report_id 聚合/过滤
  wiki_compilation_task-> 每源一行编译历史（status=2 done）
  wiki_folders         -> 自动建/复用默认文件夹（按 page_type），维护 page_count
  wiki_log_entries     -> 编译事件日志（ACTION_COMPILE）

幂等（多文档聚合）：以 (emp_id, page_key) 为唯一键，重复编译同一
页面时 UPDATE 已有页面（revision+1）、刷新 wiki_page_source，不会新增页面行；
文件夹 page_count 仅在新建页面时 +1。

简化版：不再抽取/落库 Claim（无权限场景，claim 细粒度证据归属无意义）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy import text

from .. import db
from ..db import get_engine
from ..ai_client import AIClient
from .compile import CompiledPage
from .prompts import (
    PAGE_MODIFY_SYSTEM_PROMPT, PAGE_MODIFY_USER_TEMPLATE, format_chunks,
)

logger = logging.getLogger(__name__)

_DEFAULT_FOLDER = {
    "summary": "Summaries",
    "glossary": "Glossary",
    "guide": "Guides",
    "index": "Index",
}


def _json(obj) -> Optional[str]:
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _default_folder_name(page_type: str) -> str:
    return _DEFAULT_FOLDER.get(page_type, "Wiki")


def build_wiki_page_params(
    emp_id: int, folder_id: int, page: CompiledPage,
) -> dict:
    """整理 wiki_page 插入参数（纯函数，便于单测）。folder_id / page_key 直接落入。"""
    return {
        "eid": emp_id,
        "fid": folder_id,
        "pkey": page.page_key,
        "ptype": page.page_type or "summary",
        "title": page.title,
        "rev": 1,
        "md": page.markdown,
        "summary": page.summary,
        "status": 1,
    }


def _resolve_folder(conn, emp_id: int, page_type: str, folder_name: str = None) -> int:
    """按 page_type（或 taxonomy 指定的 folder_name）自动建/复用默认文件夹，保证根目录行存在。"""
    from .folders import WikiFolderStore

    store = WikiFolderStore(emp_id)
    root_id = store.get_or_create(name="Wiki Root", parent_id=0)
    name = folder_name or _default_folder_name(page_type)
    return store.get_or_create(name=name, parent_id=root_id.id).id


_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def _extract_links(markdown: str) -> List[str]:
    """从 markdown 中提取 [[slug]] 链接目标（去重、保序）。"""
    if not markdown:
        return []
    seen = set()
    out = []
    for m in _LINK_RE.finditer(markdown):
        slug = m.group(1).strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _json(obj) -> str:
    """对象 -> JSON 字符串（None/空 -> '[]'），用于 links 列存储。"""
    if not obj:
        return "[]"
    return json.dumps(obj, ensure_ascii=False)


def read_page_body(emp_id: int, page_key: str) -> Optional[str]:
    """读取 DB 已有同 page_key 页面正文，供 REDUCE 增量合并（无则返回 None）。"""
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT markdown FROM wiki_page WHERE emp_id=:eid AND page_key=:pkey LIMIT 1"
        ), {"eid": emp_id, "pkey": page_key}).mappings().first()
    if row:
        return row["markdown"]
    return None


def _get_out_links(conn, emp_id: int, page_key: str) -> List[str]:
    """读取某页的 out_links（[[slug]] 目标列表），用于反链清理。"""
    row = conn.execute(text(
        "SELECT links FROM wiki_page WHERE emp_id=:eid AND page_key=:pkey LIMIT 1"
    ), {"eid": emp_id, "pkey": page_key}).mappings().first()
    if not row or not row["links"]:
        return []
    try:
        return row["links"] if isinstance(row["links"], list) else json.loads(row["links"])
    except (ValueError, TypeError):
        return []


def remove_in_links(conn, emp_id: int, source_slug: str, target_slugs: List[str]) -> None:
    """反链清理

    source_slug 即将被删除/解绑，从它 out_links 指向的每個目标页的 in_links 中摘掉
    source_slug。当前 wiki_page 仅显式存 links（out_links），in_links 未单独持久化，
    因此这里改为：把目标页正文中指向 source_slug 的 [[source_slug|...]] 链接改写为
    纯文本（去除链接壳），避免死链。这是为了在不引入 in_links 列的前提下保持链接有效。
    """
    for target in target_slugs:
        if target == source_slug:
            continue
        trow = conn.execute(text(
            "SELECT id, markdown, links FROM wiki_page "
            "WHERE emp_id=:eid AND page_key=:pkey LIMIT 1"
        ), {"eid": emp_id, "pkey": target}).mappings().first()
        if not trow:
            continue
        md = trow["markdown"] or ""
        new_md = _unlink_slug(md, source_slug)
        if new_md != md:
            new_links = [s for s in (trow["links"] or []) if s != source_slug]
            conn.execute(text(
                "UPDATE wiki_page SET markdown=:md, links=:links, updated_at=CURRENT_TIMESTAMP(3) "
                "WHERE id=:pid"
            ), {
                "md": new_md,
                "links": json.dumps(new_links, ensure_ascii=False),
                "pid": int(trow["id"]),
            })


_LINK_SHELL_RE = re.compile(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]")


def _unlink_slug(markdown: str, slug: str) -> str:
    """把正文中指向 slug 的 [[slug|display]] 改写为 display（去链接壳，保留可读文本）。"""
    def _repl(m):
        target = m.group(1).strip()
        if target == slug:
            return m.group(2)[1:] if m.group(2) else target  # 去掉 "|display" 前缀
        return m.group(0)
    return _LINK_SHELL_RE.sub(_repl, markdown)


def reconcile_sources(
    emp_id: int,
    report_ids: List[int],
    soft_delete_empty: bool = True,
) -> dict:
    """按 report_id 解绑源

    对 emp_id 下所有引用了这些 report_id 的 wiki_page：
      1) 从 wiki_page_source 中删除这些 report 的关联行；
      2) 若解绑后该页仍引用其他 report -> 保留页面，仅刷新源集合；
      3) 若解绑后该页不再引用任何 report（source_refs 归零）-> 软删（status=2）
         + 反链清理（从其他页摘除指向它的死链）。

    index / log 系统页不受源解绑影响（它们不是由 report 编译而来）。

    :return: {"pages_kept", "pages_deleted", "deleted_slugs"}
    """
    report_ids = [int(r) for r in report_ids]
    if not report_ids:
        return {"pages_kept": 0, "pages_deleted": 0, "deleted_slugs": []}

    pages_kept = 0
    pages_deleted = 0
    deleted_slugs: List[str] = []

    with db.transaction() as conn:
        # 找出引用了待解绑 report 的页面
        rows = conn.execute(text(
            """
            SELECT DISTINCT wp.id, wp.page_key, wp.page_type
            FROM wiki_page wp
            JOIN wiki_page_source wps ON wps.emp_id = wp.emp_id AND wps.page_id = wp.id
            WHERE wp.emp_id = :eid AND wps.report_id IN :rids
              AND wp.status = 1
            """
        ), {"eid": emp_id, "rids": tuple(report_ids)}).fetchall()

        for r in rows:
            page_id = int(r[0])
            page_key = r[1]
            page_type = r[2]
            if page_type in ("index", "log"):
                continue

            # 先解绑待删除的关联，再查剩余源（避免把即将删除的源算进 remaining）
            conn.execute(text(
                "DELETE FROM wiki_page_source "
                "WHERE emp_id=:eid AND page_id=:pid AND report_id IN :rids"
            ), {"eid": emp_id, "pid": page_id, "rids": tuple(report_ids)})
            # 剩余源（解绑后）
            src_rows = conn.execute(text(
                "SELECT report_id, report_version_id FROM wiki_page_source "
                "WHERE emp_id=:eid AND page_id=:pid"
            ), {"eid": emp_id, "pid": page_id}).fetchall()
            remaining = {int(s[0]): int(s[1]) for s in src_rows}

            if remaining:
                # 仍引用其他 report -> 保留
                pages_kept += 1
                logger.info("reconcile: 页面 %s 保留（剩余 %d 源）", page_key, len(remaining))
            else:
                # 源归零 -> 软删 + 反链清理
                out_links = _get_out_links(conn, emp_id, page_key)
                if soft_delete_empty:
                    remove_in_links(conn, emp_id, page_key, out_links)
                    conn.execute(text(
                        "UPDATE wiki_page SET status=2, updated_at=CURRENT_TIMESTAMP(3) "
                        "WHERE id=:pid"
                    ), {"pid": page_id})
                    pages_deleted += 1
                    deleted_slugs.append(page_key)
                    logger.info("reconcile: 页面 %s 源归零，软删 + 反链清理", page_key)
                else:
                    pages_kept += 1

    # 文件夹计数修正：软删的页从所属文件夹 -1
    if deleted_slugs:
        try:
            from .folders import WikiFolderStore
            with get_engine().connect() as conn:
                frows = conn.execute(text(
                    "SELECT DISTINCT folder_id FROM wiki_page "
                    "WHERE emp_id=:eid AND page_key IN :pks AND folder_id IS NOT NULL"
                ), {"eid": emp_id, "pks": tuple(deleted_slugs)}).fetchall()
            store = WikiFolderStore(emp_id)
            for fr in frows:
                store.increment_page_count(int(fr[0]), delta=-1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile: 文件夹计数修正失败: %s", exc)

    return {"pages_kept": pages_kept, "pages_deleted": pages_deleted,
            "deleted_slugs": deleted_slugs}


def publish_draft_pages(emp_id: int, page_ids: List[int]) -> None:
    """发布草稿页（把 status=1 的页置为可见）。

    当前简化为：对已落库页（status=1）记录一次发布日志。无独立草稿态时本函数为空操作。
    """
    if not page_ids:
        return
    logger.info("publish draft pages emp_id=%s count=%d", emp_id, len(page_ids))


def _reduce_markdown(
    slug: str, page_type: str, old_markdown: str, agg: str,
) -> Optional[dict]:
    """增量 reduce：把新源内容合并进旧页 markdown。

    复用与 WikiCompiler._ai_reduce_slug 相同的 PAGE_MODIFY_USER_TEMPLATE 占位符
    （slug/page_type/prev_md/agg），避免 KeyEr ror。返回 {"title","summary","markdown"}
    或 None（失败）。
    """
    try:
        user = PAGE_MODIFY_USER_TEMPLATE.format(
            slug=slug, page_type=page_type,
            prev_md=old_markdown or "（无旧版，请基于下方内容全新生成）",
            agg=agg or "（无新增候选内容）",
        )
        return AIClient.chat_json(PAGE_MODIFY_SYSTEM_PROMPT, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("增量 reduce 失败，回退覆盖旧页: %s", exc)
        return None


def persist_page(
    emp_id: int,
    page: CompiledPage,
    folder_id: int = 0,
    folder_name: str = None,
) -> Tuple[int, int, int]:
    """将整页落库并返回 (page_id, claim_count, emp_id)。

    幂等键为 (emp_id, page_key)：多文档聚合后同一页面复用 page_id、
    revision+1、刷新源文件集合。folder_id=0 时按 page_type 自动建默认文件夹。

    增量 reduce：当 DB 已存在同 page_key 页且本次提供了新的 chunks
    内容时，调用 PAGE_MODIFY 把新源增量合并进旧页（而非整页覆盖）；合并失败则
    回退为覆盖更新。落库同时写入 folder_id 与从正文解析的 links（[[slug]] 目标）。

    claim_count 恒为 0（已不再抽取 Claim）。
    """
    with db.transaction() as conn:
        # folder：folder_id=0 时自动按 page_type（或 taxonomy folder_name）解析（保证 wiki_folders 有数据）
        if folder_id == 0:
            folder_id = _resolve_folder(conn, emp_id, page.page_type or "summary", folder_name)

        existing = conn.execute(text(
            """
            SELECT id, revision, markdown, summary FROM wiki_page
            WHERE emp_id = :eid AND page_key <=> :pkey
            LIMIT 1
            """
        ), {"eid": emp_id, "pkey": page.page_key}).mappings().first()

        # 增量 reduce：REDUCE 阶段已通过 reduce_slug 把正文合并好并写入 page.markdown。
        # 此处仅当 page.markdown 为空（未预合并）时回退做一次合并；已有正文则直接落库，
        # 避免重复 LLM 调用
        final_markdown = page.markdown
        final_summary = page.summary
        if existing and not final_markdown and page.source_files:
            old_md = existing["markdown"] or ""
            agg = page.summary or page.title or "（无新增候选内容）"
            reduced = _reduce_markdown(
                slug=page.page_key, page_type=page.page_type or "summary",
                old_markdown=old_md, agg=agg,
            )
            if reduced and reduced.get("markdown"):
                final_markdown = reduced["markdown"]
                final_summary = reduced.get("summary") or final_summary
                logger.info("增量 reduce（回退）成功 page_key=%s", page.page_key)

        links_json = _json(_extract_links(final_markdown))

        if existing:
            page_id = int(existing["id"])
            new_rev = int(existing["revision"]) + 1
            conn.execute(text(
                """
                UPDATE wiki_page SET
                    folder_id = :fid, page_type = :ptype, markdown = :md, summary = :summary,
                    links = :links, revision = :rev, status = 1, updated_at = CURRENT_TIMESTAMP(3)
                WHERE id = :pid_
                """
            ), {
                "fid": folder_id, "ptype": page.page_type or "summary",
                "md": final_markdown, "summary": final_summary,
                "links": links_json, "rev": new_rev, "pid_": page_id,
            })
            is_new = False
        else:
            res = conn.execute(text(
                """
                INSERT INTO wiki_page
                    (emp_id, folder_id, page_key, page_type, title, revision, markdown, summary, links, status, is_official, official_grant)
                VALUES (:eid, :fid, :pkey, :ptype, :title, :rev, :md, :summary, :links, :status, 0, NULL)
                """
            ), {
                **build_wiki_page_params(emp_id, folder_id, page),
                "links": links_json,
            })
            page_id = int(res.lastrowid)
            is_new = True

        page.page_id = page_id  # 回填真实 id，供后续投影/日志使用

        # 刷新页面源文件集合（多文档：合并既有来源，支持多篇文档聚合为一页）
        existing_src: dict = {}
        if not is_new:
            rows = conn.execute(text(
                "SELECT report_id, report_version_id FROM wiki_page_source "
                "WHERE emp_id=:eid AND page_id=:pid"
            ), {"eid": emp_id, "pid": page_id}).fetchall()
            for r in rows:
                existing_src[int(r[0])] = int(r[1])
        merged_sources = dict(existing_src)
        merged_sources.update(page.source_files or {})

        conn.execute(
            text("DELETE FROM wiki_page_source WHERE emp_id=:eid AND page_id=:pid"),
            {"eid": emp_id, "pid": page_id},
        )
        for fid, vid in merged_sources.items():
            conn.execute(text(
                """
                INSERT INTO wiki_page_source
                    (emp_id, page_id, report_id, report_version_id)
                VALUES (:eid, :pid, :fid, :vid)
                """
            ), {"eid": emp_id, "pid": page_id, "fid": fid, "vid": vid})

        # 编译历史：每源一行
        for fid, vid in (page.source_files or {}).items():
            conn.execute(text(
                """
                INSERT INTO wiki_compilation_task
                    (emp_id, report_id, version_id, status)
                VALUES (:eid, :fid, :vid, 2)
                """
            ), {
                "eid": emp_id, "fid": fid, "vid": vid,
            })

    # 维护文件夹计数：仅新建页面时 +1
    if is_new:
        from .folders import WikiFolderStore
        WikiFolderStore(emp_id).increment_page_count(folder_id, delta=1)

    # 编译事件日志（始终写入 wiki_log_entries）
    from .log import WikiLogStore
    WikiLogStore(emp_id).record_compile(
        page_id=page_id,
        user_id=0,
        source_files=list((page.source_files or {}).keys()),
        folder_id=folder_id,
    )

    return page_id, 0, emp_id
