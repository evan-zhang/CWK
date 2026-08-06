# -*- coding: utf-8 -*-
"""Wiki 内部链接

内部链接注入是**纯规则、零 LLM** 的确定性算法（非 AI 生成），
避免每页调一次模型导致大规模构建时 LLM 调用爆炸。

- linkify_content：基于本 emp_id 全部可用 slug 列表，把正文里与某目标展示名
  一致的内容包裹为 [[slug|展示名]]（按匹配长度降序、跳过代码块/已有链接/
  Markdown 链接、词边界检查），使页面间可互相跳转（progressive 引用）。
- clean_dead_links：清理指向不存在 slug 的 [[slug|..]] 死链（保留展示名文本）。
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from .. import db

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# 代码块（围栏 / 缩进）与行内代码：linkify 不应触碰
_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`")
# 已有 Markdown 链接 [text](url) 与图片 ![alt](url)，避免在其内重复包裹
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def _available_slugs(emp_id: int) -> List[Dict[str, str]]:
    """取本 emp_id 全部页面的 page_key 与 title，作为链接目标池。"""
    rows = db.execute(
        """
        SELECT page_key, title FROM wiki_page
        WHERE emp_id = :eid AND status = 1 AND page_key IS NOT NULL
        """,
        {"eid": emp_id},
    ).mappings().all()
    return [{"slug": r["page_key"], "name": r["title"]} for r in rows]


def alive_slug_set(emp_id: int) -> set:
    """取本 emp_id 全部存活 slug（page_key）集合，供死链清理一次性使用（仅查一次 DB）。"""
    rows = db.execute(
        """
        SELECT page_key FROM wiki_page
        WHERE emp_id = :eid AND status = 1 AND page_key IS NOT NULL
        """,
        {"eid": emp_id},
    ).mappings().all()
    return {str(r["page_key"]) for r in rows}


def _build_targets(targets: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """把 (slug, name) 按展示名长度降序排列（优先匹配更长、更具体的名称）。"""
    pairs = [(str(t["slug"]), str(t.get("name") or t["slug"])) for t in targets]
    # 按名称长度降序；同长度按 slug 降序，保证确定性
    pairs.sort(key=lambda kv: (len(kv[1]), kv[0]), reverse=True)
    return pairs


def _word_boundary_ok(text: str, start: int, end: int) -> bool:
    """检查命中区间前后是否为词边界（避免把 '云' 嵌进 '云平台' 里再包一次）。"""
    def _is_word(ch: Optional[str]) -> bool:
        if ch is None:
            return False
        return ch.isalnum() or ch in "_-"
    before = text[start - 1] if start > 0 else None
    after = text[end] if end < len(text) else None
    return not _is_word(before) and not _is_word(after)


def linkify_content(emp_id: int, markdown: str) -> str:
    """给正文注入 [[slug|展示名]] 内部链接（纯规则，零 LLM）。

    算法：
    1. 跳过代码块 / 行内代码 / 已有 Markdown 链接 / 已有 [[..]]。
    2. 目标按展示名长度降序，逐段扫描，首个词边界命中的展示名包裹为
       [[slug|name]]（不重复包裹已链接文本）。
    3. 不改变事实与句意。
    """
    if not markdown:
        return markdown
    targets = _available_slugs(emp_id)
    if not targets:
        return markdown
    pairs = _build_targets(targets)
    # 先收集需保护的区间（代码块/已有链接/已有 wiki 链接）
    protected = [(m.start(), m.end()) for m in
                 list(_FENCE_RE.finditer(markdown)) +
                 list(_MD_LINK_RE.finditer(markdown)) +
                 list(_LINK_RE.finditer(markdown))]

    def _in_protected(i: int) -> bool:
        return any(s <= i < e for s, e in protected)

    out = []
    last = 0
    # 逐字符扫描，尝试在每个起始位置匹配任一目标名
    i = 0
    n = len(markdown)
    while i < n:
        if _in_protected(i):
            i += 1
            continue
        matched = False
        for slug, name in pairs:
            ln = len(name)
            if ln == 0:
                continue
            if markdown[i:i + ln] == name:
                end = i + ln
                # 不包进已有链接/代码区间
                if _in_protected(end - 1):
                    continue
                if _word_boundary_ok(markdown, i, end):
                    out.append(markdown[last:i])
                    out.append(f"[[{slug}|{name}]]")
                    last = end
                    i = end
                    matched = True
                    break
        if not matched:
            i += 1
    out.append(markdown[last:])
    return "".join(out)


def clean_dead_links(markdown: str, alive_slugs: Optional[set] = None) -> str:
    """清理死链：若 slug 不在 alive_slugs 中，去掉 [[..|name]] 的链接壳，保留 name。"""
    if alive_slugs is None:
        return markdown
    def _repl(m: "re.Match") -> str:
        slug = m.group(1).strip()
        name = m.group(2) or m.group(1)
        return name if slug not in alive_slugs else m.group(0)
    return _LINK_RE.sub(_repl, markdown)
