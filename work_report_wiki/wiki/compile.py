# -*- coding: utf-8 -*-
"""L2 Wiki 编译：页面生成（markdown + summary）。

  MAP  阶段：每篇文档只做 1 次「合并抽取」（entity/concept 候选）+ 1 次 summary
            生成。**不在 MAP 阶段逐个生成 entity/concept 正文**，而是产出轻量
            的 SlugUpdate 候选（name/description/details/why），重定向到 REDUCE。
  REDUCE 阶段：按 slug 聚合所有文档对同一 slug 的 SlugUpdate，对每个 slug 用一次
            WikiPageModifyUserPrompt 调用生成/合并页面正文（多文档同 slug 复用一次）。
            summary 页在 MAP 即直接成稿；entity/concept 页在 REDUCE 生成。

简化版：不再抽取 Claim / 证据组（无权限场景，claim 细粒度证据归属无意义）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..ai_client import AIClient
from .prompts import (
    SUMMARY_PROMPT, COMPILE_SYSTEM_PROMPT, COMPILE_USER_TEMPLATE,
    build_summary_user_content, format_chunks,
    EXTRACT_SYSTEM_PROMPT, EXTRACT_USER_TEMPLATE,
    REFINE_PROMPT, build_refine_user_content,
    PAGE_MODIFY_SYSTEM_PROMPT, PAGE_MODIFY_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

# Wiki 编译/抽取的 max_tokens 策略：
# - 页面编译（topic/summary 完整 markdown）与 entity/concept 合并抽取可能很长，
#   必须用全局上限（None -> settings.ai_max_output_tokens=16384），否则截断会破坏
#   JSON 导致整页丢失（致命）。
# - REDUCE 单 slug 正文生成较短，可用较小上限以加速。
WIKI_MAX_TOKENS = None
# 实体/概念页（REDUCE slug 正文）输出上限；2026-04 起由 4096 提升至 8192
WIKI_REDUCE_MAX_TOKENS = 8192


@dataclass
class CompiledPage:
    page_id: int
    emp_id: int
    title: str
    page_type: str = "summary"
    markdown: str = ""
    summary: str = ""
    # 页面稳定标识（slug），与 emp_id 组成幂等键（支持多篇文档聚合后复用）。
    # 带命名空间：summary-<report_id> / entity/<slug> / concept/<slug> / index
    page_key: Optional[str] = None
    # 来源文件集合：report_id -> report_version_id（一篇页面可来自多篇汇报）
    source_files: Dict[int, int] = field(default_factory=dict)
    # 分类路径（taxonomy 阶段填充）：如 ["概念", "指标"]
    folder_path: List[str] = field(default_factory=list)
    # 页面内 [[slug]] 链接目标列表（linkify 阶段填充）
    links: List[str] = field(default_factory=list)
    # 页面正文引用的 chunk_id 列表（用于溯源/交叉链接）
    chunk_ids: List[str] = field(default_factory=list)
    # 是否尚未生成正文（仅 REDUCE 前的占位 SlugUpdate 会置 True）
    is_stub: bool = False


@dataclass
class SlugUpdate:
    """REDUCE 阶段按 slug 聚合的更新意图

    仅携带「该 slug 在某文档中的名称/描述/细节候选」，真实页面正文在
    REDUCE 阶段用一次 WikiPageModifyUserPrompt 调用生成（多文档同 slug 复用一次）。
    """
    slug: str
    page_type: str                       # "entity" | "concept"
    name: str = ""
    description: str = ""
    details: str = ""
    source_files: Dict[int, int] = field(default_factory=dict)
    chunk_ids: List[str] = field(default_factory=list)


@dataclass
class ExtractedConcepts:
    """单篇文档抽取的 entity/concept 候选（每个含 slug + 描述候选）。"""
    entities: List[Dict] = field(default_factory=list)
    concepts: List[Dict] = field(default_factory=list)


def _ai_compile(file_name: str, version_id, chunks: List[Dict]) -> Dict:
    user_content = COMPILE_USER_TEMPLATE.format(
        file_name=file_name, version_id=version_id, chunks=format_chunks(chunks),
    )
    return AIClient.chat_json(COMPILE_SYSTEM_PROMPT, user_content, max_tokens=WIKI_MAX_TOKENS)


def _ai_extract(file_name: str, version_id, chunks: List[Dict]) -> Dict:
    user_content = EXTRACT_USER_TEMPLATE.format(
        file_name=file_name, version_id=version_id, chunks=format_chunks(chunks),
    )
    return AIClient.chat_json(EXTRACT_SYSTEM_PROMPT, user_content, max_tokens=WIKI_MAX_TOKENS)


def _ai_reduce_slug(slug: str, page_type: str, prev_md: Optional[str],
                    updates: List[SlugUpdate], max_tokens: int = WIKI_REDUCE_MAX_TOKENS) -> Dict:
    """REDUCE：对一个 slug 聚合所有文档的 SlugUpdate，生成/合并页面正文。
    updates 为各文档提供的 name/description/details。一次调用产出最终页面。
    """
    # 聚合各文档对该 slug 的描述/细节（去重拼接）
    lines: List[str] = []
    for u in updates:
        if u.name:
            lines.append(f"- 名称：{u.name}")
        if u.description:
            lines.append(f"- 描述：{u.description}")
        if u.details:
            lines.append(f"- 细节：{u.details}")
    agg = "\n".join(lines) or "（无新增候选内容）"
    user_content = PAGE_MODIFY_USER_TEMPLATE.format(
        slug=slug, page_type=page_type,
        prev_md=prev_md or "（无旧版，请基于下方内容全新生成）",
        agg=agg,
    )
    return AIClient.chat_json(PAGE_MODIFY_SYSTEM_PROMPT, user_content, max_tokens=max_tokens)


def _ai_summary_extractor(file_name: str, version_id, chunks: Optional[List[Dict]] = None, full_text: Optional[str] = None) -> Dict:
    # 设计取舍：单篇摘要基于「完整汇报」生成，而非切片拼接。
    # full_text 优先（截断 20000 字符，覆盖绝大多数汇报全文）；切片仅作 fallback。
    if full_text and full_text.strip():
        text = full_text[:20000]
    else:
        text = "\n\n".join(
            (c.get("text") if isinstance(c, dict) else str(c)) for c in (chunks or [])
        )[:6000]
    user_content = build_summary_user_content(file_name, text)
    try:
        return AIClient.chat_json(SUMMARY_PROMPT, user_content, max_tokens=WIKI_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("summary 生成失败，回退模板: %s", exc)
        return {
            "title": f"摘要：{file_name}",
            "summary": text[:200],
            "markdown": f"# 摘要：{file_name}\n\n{text[:800]}",
        }


def _slug(title: str) -> str:
    """标题 -> 稳定 slug（命名空间外）。多文档聚合后按 entity/<slug> 或 concept/<slug> 复用 page_id。"""
    s = (title or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:100] or "untitled"


class WikiCompiler:
    def __init__(self, emp_id: int, compiler: Optional[Callable] = None):
        self.emp_id = emp_id
        self._compile = compiler or _ai_compile
        self._summarize = _ai_summary_extractor
        self._extract = compiler or _ai_extract
        self._reduce = _ai_reduce_slug
        self._page_seq = 0

    def _next_page_id(self) -> int:
        self._page_seq += 1
        return self._page_seq

    def compile_summary(self, doc) -> CompiledPage:
        """MAP：为单篇文档生成「摘要页」（page_type=summary），源文件 = 该文档。

        摘要来源优先级：
          1. doc.summary_markdown —— 已由外部系统/异步任务写入 report_summary 的汇报级摘要，直接消费；
          2. fallback：以 full_text[:20000]（优先）或切片拼接调 LLM 生成，并应由调用方回写 report_summary。
        """
        if doc.summary_markdown and doc.summary_markdown.strip():
            title = f"摘要：{doc.file_name}"[:120]
            markdown = doc.summary_markdown
            summary = (markdown.split("\n", 1)[1] if "\n" in markdown else markdown)[:200]
            return CompiledPage(
                page_id=self._next_page_id(),
                emp_id=self.emp_id,
                title=title,
                page_type="summary",
                markdown=markdown,
                summary=summary,
                page_key=f"summary-{doc.report_id}",
                source_files={doc.report_id: doc.version_id},
            )
        raw = self._summarize(doc.file_name, doc.version_id, doc.chunks, getattr(doc, "full_text", None))
        title = (raw.get("title") or f"摘要：{doc.file_name}")[:120]
        markdown = raw.get("markdown") or raw.get("summary") or f"# {title}\n\n（该文档暂无摘要）"
        summary = raw.get("summary") or markdown[:200]
        return CompiledPage(
            page_id=self._next_page_id(),
            emp_id=self.emp_id,
            title=title,
            page_type="summary",
            markdown=markdown,
            summary=summary,
            page_key=f"summary-{doc.report_id}",
            source_files={doc.report_id: doc.version_id},
        )

    def refine_report(
        self, file_name: str, version_id, full_text: Optional[str],
    ) -> Dict:
        """一次 LLM 调用产出单篇汇报的「汇报级提炼」：摘要 + 实体候选 + 概念候选。

        设计取舍：单篇汇报的摘要/实体/概念是同一粒度产物，应由工作协同系统等外部生产者
        生成并写入 report_summary，Wiki 纯消费。验证期无外部系统时，Wiki 才用本方法兜底，
        且仅此一次 LLM 调用（而非 摘要/实体/概念 三次）。基于完整汇报原文 full_text[:20000]。

        返回 {title, summary, markdown, entities, concepts}。
        """
        text = (full_text or "")[:20000]
        user_content = build_refine_user_content(file_name, text)
        try:
            raw = AIClient.chat_json(REFINE_PROMPT, user_content, max_tokens=WIKI_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("汇报级提炼失败，回退模板: %s", exc)
            raw = {
                "title": f"摘要：{file_name}",
                "summary": text[:200],
                "markdown": f"# 摘要：{file_name}\n\n{text[:800]}",
                "entities": [], "concepts": [],
            }
        # 规范化实体/概念候选（补 slug）
        entities = []
        concepts = []
        for e in (raw.get("entities") or []):
            slug = e.get("slug") or _slug(e.get("name", ""))
            entities.append({
                "name": e.get("name", ""), "slug": slug,
                "description": e.get("description") or "",
            })
        for c in (raw.get("concepts") or []):
            slug = c.get("slug") or _slug(c.get("name", ""))
            concepts.append({
                "name": c.get("name", ""), "slug": slug,
                "description": c.get("description") or "",
            })
        raw["entities"] = entities
        raw["concepts"] = concepts
        return raw

    def extract_entities_concepts(
        self, file_name: str, version_id, chunks: Optional[List[Dict]] = None,
        entities: Optional[List[Dict]] = None, concepts: Optional[List[Dict]] = None,
    ) -> ExtractedConcepts:
        """MAP：产出 entity/concept 候选（slug 跨文档一致）。

        **纯消费**：候选直接来自 report_summary 中该篇汇报的 entities/concepts 字段
        （由工作协同系统或 Wiki 兜底 refine_report 一次生成），不再自行调 LLM 从原文抽。
        chunks 参数保留仅为向后兼容/测试，不再参与抽取。
        """
        out_entities = []
        out_concepts = []
        for e in (entities or []):
            slug = e.get("slug") or _slug(e.get("name", ""))
            out_entities.append({
                "name": e.get("name", ""), "slug": slug,
                "description": e.get("description") or "",
            })
        for c in (concepts or []):
            slug = c.get("slug") or _slug(c.get("name", ""))
            out_concepts.append({
                "name": c.get("name", ""), "slug": slug,
                "description": c.get("description") or "",
            })
        return ExtractedConcepts(entities=out_entities, concepts=out_concepts)

    def reduce_slug(
        self, slug: str, page_type: str,
        prev_md: Optional[str], updates: List[SlugUpdate],
    ) -> Dict:
        """REDUCE：对一个 slug 生成/合并页面正文（一次 LLM 调用）。"""
        return self._reduce(slug, page_type, prev_md, updates)

    def compile_entity(self, *args, **kwargs) -> CompiledPage:
        """已弃用：entity 正文改由 REDUCE 的 reduce_slug 生成。保留签名以兼容测试。"""
        raise NotImplementedError("entity 正文请在 REDUCE 阶段用 reduce_slug 生成")

    def compile_concept(self, *args, **kwargs) -> CompiledPage:
        """已弃用：concept 正文改由 REDUCE 的 reduce_slug 生成。保留签名以兼容测试。"""
        raise NotImplementedError("concept 正文请在 REDUCE 阶段用 reduce_slug 生成")
