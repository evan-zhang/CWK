# -*- coding: utf-8 -*-
"""Wiki 页面投影（无权限，直接返回页面内容）。

简化版：页面内容即 LLM 生成的 markdown/summary，不再做 claim 级投影。
唯一过滤是 report_id 白名单（allowed_file_ids）——仅用作"检索范围"入参。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProjectedPage:
    """投影后的 Wiki 页面（无 claim，无权限过滤）。"""
    page_id: int
    page_key: str          # 同时作为内部链接 slug 目标（[[page_key]]）
    title: str
    page_type: str         # entity / concept / summary / index
    revision: int
    language: str
    summary: str
    markdown: str
    links: List[str]       # 正文中的 [[slug]] 链接目标
    source_files: Dict[str, int]

    def __init__(self, **kwargs):
        self.page_id = kwargs.get("page_id")
        self.page_key = kwargs.get("page_key")
        self.title = kwargs.get("title")
        self.page_type = kwargs.get("page_type") or "summary"
        self.revision = kwargs.get("revision") or 0
        self.language = kwargs.get("language") or "zh"
        self.summary = kwargs.get("summary") or ""
        self.markdown = kwargs.get("markdown") or ""
        self.links = kwargs.get("links") or []
        self.source_files = kwargs.get("source_files") or {}


def project_wiki_page(
    page,
    ai_client=None,
    allowed_file_ids: Optional[List[int]] = None,
    emp_id: Optional[int] = None,
) -> ProjectedPage:
    """渲染 Wiki 页面为投影页。无权限过滤，直接返回页面 markdown/summary。

    Args:
        page: wiki_page ORM 行或字典。
        ai_client: 保留入参（无权限投影已不需要，可传 None）。
        allowed_file_ids: 仅作为检索范围入参（可选），不影响内容。
        emp_id: 上下文（可选）。
    """
    links = getattr(page, "links", None)
    if links is None:
        links = page.get("links")
    if isinstance(links, str):
        import json as _json
        try:
            links = _json.loads(links)
        except Exception:
            links = []
    return ProjectedPage(
        page_id=getattr(page, "id", None) or page.get("id"),
        page_key=getattr(page, "page_key", None) or page.get("page_key"),
        title=getattr(page, "title", None) or page.get("title"),
        page_type=getattr(page, "page_type", None) or page.get("page_type") or "summary",
        revision=getattr(page, "revision", 0) or page.get("revision") or 0,
        language=getattr(page, "language", None) or page.get("language") or "zh",
        summary=getattr(page, "summary", None) or page.get("summary") or "",
        markdown=getattr(page, "markdown", None) or page.get("markdown") or "",
        links=links or [],
        source_files={},
    )
