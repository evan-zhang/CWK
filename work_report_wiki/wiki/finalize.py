# -*- coding: utf-8 -*-
"""Wiki 收尾阶段

- rebuild_index_page：基于本 emp_id 全部页面（按文件夹分组）重建「索引页」
  （page_key='index'），作为跨汇报总入口；页面正文含 [[slug]] 链接。
- prune_empty_folders：删除没有任何页面的末级文件夹（保留 Wiki Root）。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import text

from .. import db
from .compile import CompiledPage
from .folders import WikiFolderStore
from .persist import persist_page

logger = logging.getLogger(__name__)


def _collect_pages(emp_id: int) -> List[dict]:
    rows = db.execute(
        """
        SELECT wp.id, wp.page_key, wp.page_type, wp.title, wp.folder_id,
               f.name AS folder_name
        FROM wiki_page wp
        LEFT JOIN wiki_folders f ON f.id = wp.folder_id
        WHERE wp.emp_id = :eid AND wp.status = 1 AND wp.page_type <> 'index'
        ORDER BY wp.folder_id, wp.title
        """,
        {"eid": emp_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def rebuild_index_page(emp_id: int) -> Optional[int]:
    """重建索引页，返回 page_id。索引页按文件夹分组列出全部页面（含 [[slug]] 链接）。"""
    pages = _collect_pages(emp_id)
    if not pages:
        return None

    by_folder: dict = {}
    for p in pages:
        key = p.get("folder_name") or "未分类"
        by_folder.setdefault(key, []).append(p)

    lines = [
        "# Wiki 索引",
        "",
        "本索引汇总全部 Wiki 页面（按文件夹分组）。每个页面均可通过 [[slug]] 互相跳转。",
        "",
    ]
    for folder in sorted(by_folder):
        lines.append(f"## {folder}")
        for p in by_folder[folder]:
            lines.append(f"- [[{p['page_key']}]] {p['title']}")
        lines.append("")

    # 汇总全部来源（report_id -> version_id），与 persist 幂等键一致
    src_rows = db.execute(
        """
        SELECT DISTINCT report_id, report_version_id FROM wiki_page_source
        WHERE emp_id = :eid
        """,
        {"eid": emp_id},
    ).mappings().all()
    source_files = {int(r["report_id"]): int(r["report_version_id"]) for r in src_rows}

    idx = CompiledPage(
        page_id=0, emp_id=emp_id,
        title="Wiki 索引", page_type="index",
        markdown="\n".join(lines),
        summary="Wiki 索引页（全部页面入口）",
        page_key="index",
        source_files=source_files,
    )
    pid, _, _ = persist_page(emp_id, idx, folder_id=0)
    return pid


def clean_dead_links(emp_id: int) -> int:
    """收尾阶段：清理本 emp_id 全部已发布页面的死链（指向不存在 slug 的 [[slug|..]]）。

    职责边界（只负责清理，不负责注入）：
    - FINALIZE 只做零 LLM 的正则死链清理，绝不调 LLM 注入链接（注入由落库阶段
      的 linkifier / linkify_content 负责）。
    - 存活 slug 池只查一次（提到循环外），不再每页重查，避免 DB 放大。

    返回实际被更新的页面数。
    """
    from .linkify import clean_dead_links as regex_clean, alive_slug_set

    # 一次性取出存活 slug 池，后续所有页复用（避免每页一次 DB 查询）
    alive = alive_slug_set(emp_id)

    rows = db.execute(
        """
        SELECT id, markdown FROM wiki_page
        WHERE emp_id = :eid AND status = 1 AND markdown IS NOT NULL AND markdown <> ''
        ORDER BY id
        """,
        {"eid": emp_id},
    ).mappings().all()

    updated = 0
    for r in rows:
        pid = int(r["id"])
        md = r["markdown"] or ""
        if not md:
            continue

        # 正则清理死链（零 LLM，纯剪枝指向不存在 slug 的 [[slug|..]]）
        new_md = regex_clean(md, alive)

        if new_md != md:
            db.execute(
                "UPDATE wiki_page SET markdown = :md WHERE id = :pid AND emp_id = :eid",
                {"md": new_md, "pid": pid, "eid": emp_id},
            )
            updated += 1
    return updated


def prune_empty_folders(emp_id: int) -> int:
    """删除没有任何页面的末级文件夹（保留 Wiki Root 与带子文件夹的节点）。返回删除数。"""
    store = WikiFolderStore(emp_id)
    children = store.list_children(0)
    root_ids = {c.id for c in children}
    # 找出所有含页面的文件夹
    rows = db.execute(
        """
        SELECT DISTINCT folder_id FROM wiki_page
        WHERE emp_id = :eid AND status = 1 AND folder_id > 0
        """,
        {"eid": emp_id},
    ).mappings().all()
    used = {int(r["folder_id"]) for r in rows}
    # 含子文件夹的也算"被使用"，避免误删父节点
    sub = db.execute(
        "SELECT DISTINCT parent_id FROM wiki_folders WHERE emp_id = :eid AND parent_id > 0",
        {"eid": emp_id},
    ).mappings().all()
    used |= {int(r["parent_id"]) for r in sub}
    used |= root_ids  # 保留根

    all_folders = db.execute(
        "SELECT id FROM wiki_folders WHERE emp_id = :eid",
        {"eid": emp_id},
    ).mappings().all()
    deleted = 0
    for r in all_folders:
        fid = int(r["id"])
        if fid not in used:
            db.execute(
                "DELETE FROM wiki_folders WHERE id = :fid AND emp_id = :eid",
                {"fid": fid, "eid": emp_id},
            )
            deleted += 1
    return deleted
