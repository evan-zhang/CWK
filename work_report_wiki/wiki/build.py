# -*- coding: utf-8 -*-
"""Wiki 多文档聚合构建编排

本模块负责把多篇源文档（每篇即一个 report_id / 文件）编译为待落库的页面列表：
  - 每篇文档：1 个 summary 页（page_type=summary，兼作该篇的主题视图）
  - 每篇文档：1 次合并抽取 entity/concept 候选，产出轻量 SlugUpdate（不生成正文）

注：原 topic 页（page_type=topic）已移除，主题视图统一由 summary 页承载。

关键对齐：MAP 阶段**不为每个 entity/concept 生成正文**（那样是逐 slug 串行 N 次
LLM）。entity/concept 正文在 REDUCE 阶段按 slug 聚合后用一次
WikiPageModifyUserPrompt 调用生成（多文档同 slug 复用一次）。

落库（TAXONOMY 分类 + REDUCE 增量 + FINALIZE）由 pipeline.run_wiki_pipeline 统一编排；
本模块也保留 build_and_persist 作为一次性兼容入口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .compile import CompiledPage, WikiCompiler, ExtractedConcepts, SlugUpdate
from .persist import persist_page

logger = logging.getLogger(__name__)

# 单篇汇报抽取的 entity / concept 候选数量上限，避免长汇报候选爆炸（REDUCE 次数随之受限）。
MAX_ENTITY_CANDIDATES = 10
MAX_CONCEPT_CANDIDATES = 10


@dataclass
class WikiSourceDoc:
    report_id: int
    version_id: int
    file_name: str
    chunks: List[Dict]
    # 可选：显式归属 topic；同一 topic 的多篇文档会聚合为一个 Wiki 页面。
    topic: Optional[str] = None
    # 完整汇报原文（20000 字符内），供摘要/抽取基于全文而非切片生成。
    full_text: Optional[str] = None
    # 已由外部系统/异步任务写入 report_summary 的「汇报级提炼」。
    # 非空时 Wiki 直接消费（summary/entities/concepts），不再调 LLM；为空时由 Wiki fallback 生成并回写。
    summary_markdown: Optional[str] = None
    # 该篇汇报的实体/概念候选（来自 report_summary.entities/concepts），Wiki 纯消费、跨篇聚合到 REDUCE。
    entities: Optional[List[Dict]] = None
    concepts: Optional[List[Dict]] = None


@dataclass
class MapResult:
    """单篇文档 MAP 结果：直接成稿页 + 待 REDUCE 的 slug 候选。"""
    doc: WikiSourceDoc
    pages: List[CompiledPage] = field(default_factory=list)
    slug_updates: List[SlugUpdate] = field(default_factory=list)


def build_pages_for_doc(
    doc: WikiSourceDoc,
    compiler: WikiCompiler,
    do_extract: bool = True,
) -> MapResult:
    """MAP：把单篇文档编译为候选（summary 直接成稿，entity/concept 仅出候选）。

    返回的 MapResult.pages 为已生成正文的页面（仅 summary；topic 页已移除，
    每篇文档的主题视图由 summary 页承载），
    MapResult.slug_updates 为 entity/concept 候选（由 REDUCE 阶段聚合后生成正文）。
    """
    result = MapResult(doc=doc)

    # 1) 摘要页（源 = 该文档；纯消费 report_summary.markdown，缺失才 fallback）
    #    同时作为该篇文档的主题视图（原 topic 页职责合并进 summary 页）。
    try:
        result.pages.append(compiler.compile_summary(doc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("summary 生成失败 report=%s: %s", doc.report_id, exc)

    # 2) entity / concept 候选：纯消费 report_summary.entities/concepts（跨篇聚合到 REDUCE）。
    #    不再自行从原文切片抽取；缺失候选时为空（该篇不贡献实体/概念页）。
    if do_extract:
        try:
            ex: ExtractedConcepts = compiler.extract_entities_concepts(
                file_name=doc.file_name, version_id=doc.version_id,
                entities=doc.entities, concepts=doc.concepts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("entity/concept 抽取失败 report=%s: %s", doc.report_id, exc)
            ex = ExtractedConcepts()

        if len(ex.entities) > MAX_ENTITY_CANDIDATES:
            logger.info("entity 候选 %d 个，截留前 %d 个（report=%s）",
                        len(ex.entities), MAX_ENTITY_CANDIDATES, doc.report_id)
        if len(ex.concepts) > MAX_CONCEPT_CANDIDATES:
            logger.info("concept 候选 %d 个，截留前 %d 个（report=%s）",
                        len(ex.concepts), MAX_CONCEPT_CANDIDATES, doc.report_id)

        for e in ex.entities[:MAX_ENTITY_CANDIDATES]:
            result.slug_updates.append(SlugUpdate(
                slug=e["slug"], page_type="entity", name=e.get("name", ""),
                description=e.get("description", ""),
                source_files={doc.report_id: doc.version_id},
            ))
        for c in ex.concepts[:MAX_CONCEPT_CANDIDATES]:
            result.slug_updates.append(SlugUpdate(
                slug=c["slug"], page_type="concept", name=c.get("name", ""),
                description=c.get("description", ""),
                source_files={doc.report_id: doc.version_id},
            ))

    return result


def build_wiki(
    emp_id: int,
    docs: List[WikiSourceDoc],
    compiler: WikiCompiler,
    do_extract: bool = True,
) -> List[CompiledPage]:
    """编译多篇文档，逐篇产出候选页面（不做聚合，聚合交给 REDUCE 阶段）。

    注意：此函数仅返回 summary 页；entity/concept 候选由调用方单独处理
    （见 run_wiki_pipeline 的 REDUCE 阶段）。保留该签名以兼容测试。
    """
    out: List[CompiledPage] = []
    for d in docs:
        mr = build_pages_for_doc(d, compiler, do_extract=do_extract)
        out.extend(mr.pages)
    for i, p in enumerate(out, start=1):
        p.page_id = i
    return out


def build_and_persist(
    emp_id: int,
    docs: List[WikiSourceDoc],
    compiler: WikiCompiler,
    folder_id: int = 0,
    do_extract: bool = True,
) -> List[Tuple[int, int, int]]:
    """兼容入口：编译并直接逐页落库（幂等 + 增量合并）。

    注：分类在落库时按 page_type 默认文件夹；如需整批 taxonomy 规划，
    请使用 pipeline.run_wiki_pipeline。
    """
    pages = build_wiki(emp_id, docs, compiler, do_extract=do_extract)
    results: List[Tuple[int, int, int]] = []
    for page in pages:
        pid, n, proj = persist_page(emp_id=emp_id, page=page, folder_id=folder_id)
        results.append((pid, n, proj))
    return results
