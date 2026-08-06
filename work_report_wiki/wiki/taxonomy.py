# -*- coding: utf-8 -*-
"""Wiki 分类（taxonomy）。

设计：
- 分类是**串行、整批**完成的（reduce 并行前必须先收敛文件夹）。
- 给定一批页面（title + summary），让 LLM 规划每个页面应归入的文件夹路径
  （如 ["概念", "指标"]），复用既有 wiki_folders，产出 page_key -> folder_id 映射。
- 文件夹仅用于导航，**不是安全边界**（安全性由 emp_id 隔离保证）。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from ..ai_client import AIClient
from .folders import WikiFolderStore
from .prompts import (
    TAXONOMY_PROMPT,
    build_taxonomy_user_content,
    TAXONOMY_PLAN_SYSTEM_PROMPT,
    build_taxonomy_plan_user_content,
)

logger = logging.getLogger(__name__)

# 每个分块一次性规划，调用 1 次 LLM。
# 5000 篇 -> 5000/60 ≈ 84 次。
TAXONOMY_PLAN_CHUNK_SIZE = 60


def plan_batch_taxonomy(
    emp_id: int,
    pages: List[dict],
    classifier=None,
) -> Dict[str, int]:
    """为一批页面规划分类，写 wiki_folders 树，返回 {page_key: folder_id}。

    批量一次规划，避免逐页发散：
    - 默认按 chunk（TAXONOMY_PLAN_CHUNK_SIZE）分组，每组只调 1 次 LLM 规划，
      返回 {slug: path[]}；分块间把已规划的文件夹 feed-forward 到下一个分块，
      使整批收敛到同一棵树。
    - classifier 传入时（测试桩）退化为逐页单页分类，保持兼容。

    :param emp_id: 员工隔离维度
    :param pages: 每项 {"page_key":.., "title":.., "summary":.., "slug":..}
    :param classifier: 可选 (title, summary) -> folder_name 的单页分类器（测试用桩）
    :return: page_key -> 末级文件夹 folder_id
    """
    store = WikiFolderStore(emp_id)
    root = store.get_or_create(name="Wiki Root", parent_id=0)
    mapping: Dict[str, int] = {}

    # 测试桩 / 单页分类器：逐页处理（保持旧行为）
    if classifier is not None:
        for p in pages:
            pkey = p.get("page_key")
            if not pkey:
                continue
            try:
                folder_name = classifier(p.get("title", ""), p.get("summary", ""))
            except Exception as exc:  # noqa: BLE001
                logger.warning("classifier 失败，回退默认: %s", exc)
                folder_name = None
            if folder_name:
                leaf = store.get_or_create(name=folder_name, parent_id=root.id)
                mapping[pkey] = leaf.id
        return mapping

    # 生产路径：按 chunk 批量规划（feed-forward）
    items: List[Tuple[str, str, str, str]] = []  # (page_key, slug, title, summary)
    for p in pages:
        pkey = p.get("page_key")
        if not pkey:
            continue
        slug = p.get("slug") or pkey
        items.append((pkey, slug, p.get("title", ""), p.get("summary", "")))

    existing = ""  # 分块间 feed-forward 的已有文件夹树文本
    for start in range(0, len(items), TAXONOMY_PLAN_CHUNK_SIZE):
        chunk = items[start:start + TAXONOMY_PLAN_CHUNK_SIZE]
        assignments = _plan_chunk(chunk, existing)
        for pkey, slug, title, summary in chunk:
            path = assignments.get(slug)
            if not path:
                continue
            # 按 path 逐层建文件夹，末级 folder_id 作为该 page 的归属
            parent_id = root.id
            leaf_id = root.id
            for label in path[:2]:  # 最多 2 级
                leaf = store.get_or_create(name=label, parent_id=parent_id)
                parent_id = leaf.id
                leaf_id = leaf.id
            mapping[pkey] = leaf_id
        # feed-forward：把本轮已用文件夹拼回 existing，供下一分块锚定收敛
        existing = _render_existing(store, root.id)

    return mapping


def _plan_chunk(chunk: List[Tuple[str, str, str, str]], existing: str) -> Dict[str, List[str]]:
    """对一个分块调 1 次 LLM，返回 {slug: path[]}。失败时退化为空（落默认文件夹）。"""
    try:
        items_arg = [(slug, title, summary) for (_pk, slug, title, summary) in chunk]
        raw = AIClient.chat_json(
            TAXONOMY_PLAN_SYSTEM_PROMPT,
            build_taxonomy_plan_user_content(items_arg, existing=existing),
        )
        out: Dict[str, List[str]] = {}
        for a in raw.get("assignments") or []:
            slug = a.get("slug")
            path = a.get("path") or []
            if slug and isinstance(path, list) and path:
                # 仅保留合法字符串标签，去空白
                clean = [str(x).strip() for x in path if str(x).strip()]
                if clean:
                    out[slug] = clean
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("taxonomy 批量规划失败（本分块 %d 项退回默认）: %s", len(chunk), exc)
        return {}


def _render_existing(store: "WikiFolderStore", root_id: int) -> str:
    """把当前文件夹树渲染成紧凑文本，供下一个分块 feed-forward（对齐 existing_folders）。"""
    try:
        tree = store.render_tree(root_id)
        if isinstance(tree, str):
            return tree
        return str(tree)
    except Exception:  # noqa: BLE001
        return ""


def _llm_classify(title: str, summary: str) -> Optional[str]:
    """单页分类（仅备用/测试，生产路径已走 _plan_chunk）。"""
    try:
        raw = AIClient.chat_json(
            TAXONOMY_PROMPT,
            build_taxonomy_user_content(title, summary),
        )
        name = (raw.get("folder") or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("taxonomy LLM 分类失败，回退默认文件夹: %s", exc)
        return None
