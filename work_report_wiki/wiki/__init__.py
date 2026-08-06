# -*- coding: utf-8 -*-
"""Wiki 编译与页面投影。"""
from .compile import CompiledPage, WikiCompiler
from .project import ProjectedPage, project_wiki_page
from .retrieval import (
    fetch_wiki_pages_for_reports,
    route_wiki_pages_by_topic,
)

__all__ = [
    "CompiledPage", "WikiCompiler",
    "ProjectedPage", "project_wiki_page",
    "fetch_wiki_pages_for_reports", "route_wiki_pages_by_topic",
]
