# -*- coding: utf-8 -*-
"""答案引擎：检索 -> 上下文拼装 -> 生成。

简化版：无权限过滤，直接检索全部可见 chunk 并生成答案。
Progressive RAG：先召回 L0 chunk，再用命中的 report_id 反查 L2 Wiki 页面
（topic 聚合页 + index 索引页，综述优先），可选叠加 LLM 主题路由，
解决「跨数月项目、几十篇汇报」这类复杂归纳问题。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .. import ai_client, es_store
from ..wiki.retrieval import fetch_wiki_pages_for_reports, route_wiki_pages_by_topic
from .prompts import ANSWER_SYSTEM_PROMPT, build_user_content

logger = logging.getLogger(__name__)


def _extract_topics(query: str, ai_client_inst) -> List[str]:
    """用 LLM 从 query 抽取主题关键词（供 Wiki 主题路由）。失败则返回空。"""
    try:
        raw = ai_client_inst.chat(
            "你是查询理解助手。从用户问题中提取 1~3 个用于检索知识库的主题关键词"
            "（如项目名、事项名、风险类型），用逗号分隔，不要解释。",
            query,
        )
        if not raw:
            return []
        return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()][:3]
    except Exception as exc:  # noqa: BLE001
        logger.warning("主题抽取失败，跳过路由: %s", exc)
        return []


def _parse_confidence(raw) -> float:
    """鲁棒解析模型给出的 confidence（可能是数值、数值字符串，或 high/low 等词）。"""
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        s = raw.strip().lower()
        word_map = {
            "high": 0.8, "medium": 0.5, "mid": 0.5, "low": 0.3,
            "very low": 0.1, "insufficient": 0.0, "none": 0.0,
            "no": 0.0, "weak": 0.2, "strong": 0.9,
        }
        if s in word_map:
            return word_map[s]
        try:
            val = float(s)
        except ValueError:
            logger.warning("无法解析 confidence=%r，回退 0.0", raw)
            return 0.0
    else:
        return 0.0
    # 钳制到 [0, 1]
    return max(0.0, min(1.0, val))


def _normalize_citations(raw_citations) -> List[Dict]:
    """把 LLM 返回的 citations 归一为干净 schema（wiki / chunk 两类）。

    丢弃遗留字段（claim_id / page_revision / version_id / file_id），
    仅保留：wiki -> {type, page_id, title}；chunk -> {type, report_id, chunk_id, title}。
    """
    out: List[Dict] = []
    if not isinstance(raw_citations, list):
        return out
    for c in raw_citations:
        if not isinstance(c, dict):
            continue
        title = c.get("title") or ""
        if c.get("page_id"):
            out.append({"type": "wiki", "page_id": int(c["page_id"]), "title": title})
        elif c.get("report_id") or c.get("chunk_id"):
            out.append({
                "type": "chunk",
                "report_id": int(c["report_id"]) if c.get("report_id") else 0,
                "chunk_id": str(c.get("chunk_id") or ""),
                "title": title,
            })
    return out


@dataclass
class AnswerResult:
    answer: str
    citations: List[Dict] = field(default_factory=list)
    confidence: float = 0.0
    needs_clarification: bool = False
    hit_count: int = 0
    notes: List[str] = field(default_factory=list)


def answer(
    query: str,
    emp_id: int,
    user_id: int = 0,
    request_id: str = "cli",
    top_k: int = 6,
    allowed_file_ids: Optional[List[int]] = None,
    use_wiki: bool = True,
) -> AnswerResult:
    """检索（Progressive RAG）+ 生成答案。

    Progressive RAG 流程：
      1. 召回 L0 chunk（BM25 + kNN + RRF），收集命中的 report_id 集合；
      2. 用 report_id 反查关联的 Wiki 页面（topic 聚合页 + index 索引页），
         并可选叠加 LLM 主题路由按 page_key/title 定向取页；
      3. 上下文拼装：Wiki 综述优先，chunk 细节证据补充；
      4. 调用 LLM 生成带 [n] 引用的答案。

    Args:
        query: 用户问题。
        emp_id: 员工 ID。
        user_id: 用户 ID（保留入参，不参与过滤）。
        request_id: 请求标识。
        top_k: 检索条数。
        allowed_file_ids: 可选的检索范围白名单（report_id），仅作范围约束。
        use_wiki: 是否启用 Wiki 综述层（默认 True；无 Wiki 时自动降级到 chunk-only）。
    """
    notes: List[str] = []
    chunks = es_store.hybrid_search(
        query, top_k=top_k, emp_id=emp_id, allowed_file_ids=allowed_file_ids
    )

    # 阶段 1：从 chunk 命中收集 report_id（用于反查 Wiki）
    report_ids = []
    for c in chunks:
        rid = c.get("report_id")
        if rid is not None and rid not in report_ids:
            report_ids.append(rid)

    wiki_pages: List[Dict] = []
    if use_wiki:
        try:
            # 阶段 2a：report_id 反查关联 Wiki 页面（综述优先）
            wiki_pages = fetch_wiki_pages_for_reports(emp_id, report_ids)
            # 阶段 2b：LLM 主题路由，补充按主题定向的 Wiki 页面
            if len(wiki_pages) < 12:
                topics = _extract_topics(query, ai_client.AIClient())
                if topics:
                    routed = route_wiki_pages_by_topic(emp_id, topics, report_ids)
                    seen = {p["page_id"] for p in wiki_pages}
                    for p in routed:
                        if p["page_id"] not in seen:
                            wiki_pages.append(p)
                            seen.add(p["page_id"])
            if wiki_pages:
                notes.append(f"已接入 Wiki 综述层：{len(wiki_pages)} 个页面")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wiki 检索失败，降级为 chunk-only: %s", exc)
            notes.append("Wiki 检索失败，已降级为仅 chunk")
            wiki_pages = []

    # 阶段 3：上下文拼装（Wiki 综述优先 + chunk 细节证据）
    context = es_store.as_context(chunks, wiki_pages=wiki_pages)

    if not context:
        return AnswerResult(
            answer="根据现有资料无法回答（未检索到相关内容）。",
            confidence=0.0, needs_clarification=True, hit_count=0, notes=notes,
        )

    user_content = build_user_content(query, context)
    try:
        parsed = ai_client.AIClient.chat_json(ANSWER_SYSTEM_PROMPT, user_content)
        assert isinstance(parsed, dict), "AI 返回非 JSON 对象"
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI JSON 解析失败，回退无引用答案: %s", exc)
        return AnswerResult(
            answer="根据现有资料无法回答（生成失败）。",
            confidence=0.0, needs_clarification=True, hit_count=len(context), notes=notes,
        )

    return AnswerResult(
        answer=parsed.get("answer", ""),
        citations=_normalize_citations(parsed.get("citations", [])),
        confidence=_parse_confidence(parsed.get("confidence", 0.0)),
        needs_clarification=bool(parsed.get("needs_clarification", False)),
        hit_count=len(context),
        notes=notes,
    )
