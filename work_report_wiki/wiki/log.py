# -*- coding: utf-8 -*-
"""Wiki 事件日志

设计原则：
  - 只追加（append-only）：每次 Wiki 生命周期事件 = 一条 INSERT，读全量也
    无需解析单 TEXT 列（避免 O(n^2)），读取按 (emp_id, id DESC) 分页。
  - 与通用 event_log / grant_audit 区分：本表聚焦 Wiki 业务事件
    （compile / publish / reproject / move / grant / revoke），便于审计某次操作
    影响了哪些页面。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from .. import db

# 受支持的 action 取值
ACTION_COMPILE = "wiki.compile"
ACTION_PUBLISH = "wiki.publish"
ACTION_REPROJECT = "wiki.reproject"
ACTION_MOVE = "wiki.move"


@dataclass
class WikiLogEntry:
    id: int
    emp_id: int
    action: str
    knowledge_id: str
    doc_title: Optional[str]
    summary: Optional[str]
    pages_affected: Optional[list]
    created_at: Optional[str]


class WikiLogStore:
    """wiki_log_entries 表的读写辅助（依赖 db.py）。"""

    def __init__(self, emp_id: int):
        self.emp_id = emp_id

    def append(
        self,
        action: str,
        knowledge_id: str = "",
        doc_title: Optional[str] = None,
        summary: Optional[str] = None,
        pages_affected: Optional[list] = None,
    ) -> int:
        pa_json = json.dumps(pages_affected, ensure_ascii=False) if pages_affected is not None else None
        res = db.execute(
            """
            INSERT INTO wiki_log_entries
                (emp_id, action, knowledge_id, doc_title, summary, pages_affected)
            VALUES (:eid, :action, :kid, :title, :summary, :pa)
            """,
            {
                "eid": self.emp_id, "action": action,
                "kid": str(knowledge_id or ""), "title": doc_title,
                "summary": summary, "pa": pa_json,
            },
        )
        return int(res.lastrowid)

    def record_compile(
        self,
        page_id: int,
        source_files: List[int],
        user_id: int = 0,
        folder_id: int = 0,
    ) -> int:
        """编译完成后写一条 wiki.compile 日志（多源：source_files 为 report_id 列表）。"""
        sf = source_files or []
        summary = (
            f"编译页面 page={page_id} 来源文件数={len(sf)}"
        )
        return self.append(
            action=ACTION_COMPILE,
            knowledge_id=str(page_id),
            doc_title=f"page_{page_id}",
            summary=summary,
            pages_affected=[{
                "page_id": page_id, "source_file_ids": sf,
                "folder_id": folder_id,
            }],
        )

    def recent(self, limit: int = 50) -> List[WikiLogEntry]:
        rows = db.query_all(
            """
            SELECT * FROM wiki_log_entries
            WHERE emp_id = :eid
            ORDER BY id DESC LIMIT :lim
            """,
            {"eid": self.emp_id, "lim": limit},
        )
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: dict) -> WikiLogEntry:
    pa = row.get("pages_affected")
    if isinstance(pa, str):
        try:
            pa = json.loads(pa)
        except Exception:
            pa = None
    return WikiLogEntry(
        id=row["id"], emp_id=row["emp_id"],
        action=row["action"], knowledge_id=row.get("knowledge_id", ""),
        doc_title=row.get("doc_title"), summary=row.get("summary"),
        pages_affected=pa, created_at=row.get("created_at"),
    )
