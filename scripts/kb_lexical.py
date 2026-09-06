#!/usr/bin/env python3
"""RT-051 P3a: pure lexical retrieval primitives (C07 contracts).

No storage, no backend, no ingest knowledge: everything here is a pure
function over text, unit-testable without any library.  The lexical builder
and the gateway fusion route consume this layer without moving files.

Body-extraction rules (CWork envelope, Markdown frontmatter) belong to the
builder, not here — this layer receives already-extracted body text and
answers three questions:

1. which terms does a piece of text yield (:func:`tokenize`),
2. how does a body split into code-point chunks with byte mapping
   (:func:`chunk_body`),
3. given chunked documents and a query, which ranks come out
   (:func:`bm25_rank`, :func:`best_spans`, :func:`rrf`).

Design anchors (RT/RT-051/rt-lite.md C07):

- 中文 1/2/3-gram（短词 1/2 字不漏）；ASCII lowercase、完整标识符整词，
  「AB-017 不等于 017」——带连字符/下划线的字母数字串是单一词项。
- chunk 目标 800 code points、硬 1200、重叠 ≤120，优先行边界；单块连续
  span，不 strip/rejoin 伪造坐标；字节映射到原文 raw。
- BM25 k1=1.5、b=0.75；idf=ln(1+(N-df+0.5)/(df+0.5))；标准 tf 长度归一化；
  每文档取最高块分（不相加奖励长文）；空集显式 0，不除零。
- RRF=1/(60+rank)，缺路为 0。

Span SHA / chunker_version / tokenizer_version / domain-separated chunk_id
belong to the builder layer (C07 chunk fields); this module only fixes the
geometry and the math so both sides share one implementation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ── C07 constants ──────────────────────────────────────────────────────────

K1 = 1.5
B = 0.75
RRF_K = 60.0
CHUNK_TARGET_CP = 800
CHUNK_MAX_CP = 1200
CHUNK_OVERLAP_CP = 120
MAX_SPANS_PER_DOC = 3

CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df]")
#: ASCII 词项：小写、保连字符/下划线连接的完整编号（AB-017 是单词项）。
#: 大小写通配后统一 lower；词项必须字母/数字开头，孤立的 -/_ 只是分隔。
ASCII_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class Chunk:
    """A single continuous span of body text, byte-mapped to the raw."""

    start_cp: int
    end_cp: int
    start_byte: int
    end_byte: int
    text: str

    @property
    def length_cp(self) -> int:
        return self.end_cp - self.start_cp


# ── tokenize ───────────────────────────────────────────────────────────────


def tokenize(text: str) -> Tuple[str, ...]:
    """Term tuple for *text* — corpus and query use the same rules.

    - CJK runs: unigram + bigram + trigram over consecutive code points.
    - ASCII runs: lowercase whole identifier terms.  ``AB-017`` yields the
      single term ``ab-017``; a document whose only term is ``017`` never
      matches it (C07: 编号整词命中).
    - CJK 与 ASCII 自然断开；标点/空白/控制符只作分隔，原文不归一化。
    """
    terms: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if CJK_RE.match(ch):
            j = i
            while j < n and CJK_RE.match(text[j]):
                j += 1
            run = text[i:j]
            run_len = len(run)
            for start in range(run_len):
                terms.append(run[start])
                if start + 2 <= run_len:
                    terms.append(run[start : start + 2])
                if start + 3 <= run_len:
                    terms.append(run[start : start + 3])
            i = j
            continue
        if ch.isascii() and ch.isalnum():
            m = ASCII_TERM_RE.match(text, i)
            assert m is not None  # ch 已保证命中
            terms.append(m.group(0).lower())
            i = m.end()
            continue
        i += 1
    return tuple(terms)


# ── chunk ──────────────────────────────────────────────────────────────────


def _forward_byte_offsets(text: str, points: Sequence[int]) -> List[int]:
    """Byte offsets for strictly ascending code-point *points*, one pass.

    Starts (and ends) of successive chunks are each monotonic, so two calls
    with sorted point lists map the whole document in O(n) — no per-chunk
    prefix re-encode, which matters when the builder meets 128MiB raws.
    """
    out: List[int] = []
    cp = 0
    byte = 0
    for p in points:
        byte += len(text[cp:p].encode("utf-8"))
        cp = p
        out.append(byte)
    return out


def chunk_body(
    text: str,
    *,
    target_cp: int = CHUNK_TARGET_CP,
    max_cp: int = CHUNK_MAX_CP,
    overlap_cp: int = CHUNK_OVERLAP_CP,
) -> Tuple[Chunk, ...]:
    """Split *text* into continuous code-point chunks with byte offsets.

    Chunks tile ``[0, len(text))`` with neighbour overlap ≤ ``overlap_cp``;
    no chunk exceeds ``max_cp``; when a line end falls inside the look-back
    window before the target point the boundary snaps to it (优先行切分).
    """
    n = len(text)
    if n <= target_cp:
        return (Chunk(0, n, 0, len(text.encode("utf-8")), text),)

    spans: List[Tuple[int, int]] = []
    start = 0
    while start < n:
        remaining = n - start
        if remaining <= target_cp:
            end = n
        else:
            desired = start + target_cp
            window_start = max(start + 1, desired - overlap_cp)
            line_at = text.rfind("\n", window_start, desired)
            end = line_at + 1 if line_at != -1 else desired
            end = min(end, start + max_cp)
            if end <= start:  # 防御：参数退化时也要前进
                end = min(n, start + 1)
        spans.append((start, end))
        if end >= n:
            break
        next_start = end - overlap_cp
        if next_start <= start:
            next_start = min(n, end)
        start = next_start

    starts = [s for s, _ in spans]
    ends = [e for _, e in spans]
    start_bytes = _forward_byte_offsets(text, starts)
    end_bytes = _forward_byte_offsets(text, ends)
    return tuple(
        Chunk(s, e, sb, eb, text[s:e])
        for (s, e), sb, eb in zip(spans, start_bytes, end_bytes)
    )


# ── index & BM25 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Posting:
    """One chunk's occurrence of a term (df 统计单位 = chunk)。"""

    chunk_id: str
    lineage: str
    span_start_cp: int
    span_end_cp: int
    tf: int


@dataclass
class LexicalIndex:
    """In-memory BM25 index: term→postings + per-chunk scoring state."""

    terms: Dict[str, Tuple[Posting, ...]]
    chunk_terms: Dict[str, Dict[str, int]]  # chunk_id -> term -> tf
    chunk_lengths: Dict[str, int]  # chunk_id -> term count (dl)
    chunk_spans: Dict[str, Tuple[int, int]]  # chunk_id -> (start_cp, end_cp)
    lineages: Dict[str, str]  # chunk_id -> lineage

    @property
    def n_chunks(self) -> int:
        return len(self.chunk_lengths)

    def df(self, term: str) -> int:
        return len(self.terms.get(term, ()))

    @property
    def avgdl(self) -> float:
        n = self.n_chunks
        if n == 0:
            return 0.0
        return sum(self.chunk_lengths.values()) / n


def build_index(
    docs: Sequence[Tuple[str, Sequence[Chunk]]],
    *,
    chunk_id_of: Optional[Callable[[str, Chunk, int], str]] = None,
) -> LexicalIndex:
    """Build a BM25 index over ``(lineage, chunks)`` pairs.

    ``chunk_id_of(lineage, chunk, index)`` supplies the C07 domain-separated
    id; the default ``lineage#start-end`` is stable enough for tests.
    """
    terms: Dict[str, List[Posting]] = {}
    chunk_terms: Dict[str, Dict[str, int]] = {}
    lengths: Dict[str, int] = {}
    spans: Dict[str, Tuple[int, int]] = {}
    lineages: Dict[str, str] = {}
    for lineage, chunks in docs:
        for idx, chunk in enumerate(chunks):
            cid = (
                chunk_id_of(lineage, chunk, idx)
                if chunk_id_of is not None
                else f"{lineage}#{chunk.start_byte}-{chunk.end_byte}"
            )
            local: Dict[str, int] = {}
            for term in tokenize(chunk.text):
                local[term] = local.get(term, 0) + 1
            chunk_terms[cid] = local
            lengths[cid] = sum(local.values())
            spans[cid] = (chunk.start_cp, chunk.end_cp)
            lineages[cid] = lineage
            for term, tf in local.items():
                terms.setdefault(term, []).append(
                    Posting(cid, lineage, chunk.start_cp, chunk.end_cp, tf)
                )
    return LexicalIndex(
        terms={t: tuple(ps) for t, ps in terms.items()},
        chunk_terms=chunk_terms,
        chunk_lengths=lengths,
        chunk_spans=spans,
        lineages=lineages,
    )


def idf(n_chunks: int, df: int) -> float:
    """C07 idf：``ln(1+(N-df+0.5)/(df+0.5))``；空语料显式 0，不除零。"""
    if n_chunks <= 0:
        return 0.0
    return math.log(1.0 + (n_chunks - df + 0.5) / (df + 0.5))


def chunk_score(index: LexicalIndex, chunk_id: str, query: str) -> float:
    """BM25 score of one chunk against *query*（tf 长度归一化）。"""
    local = index.chunk_terms.get(chunk_id)
    if not local:
        return 0.0
    dl = index.chunk_lengths.get(chunk_id, 0)
    if dl <= 0:
        return 0.0
    avgdl = index.avgdl or 1.0
    n = index.n_chunks
    total = 0.0
    for term in sorted(set(tokenize(query))):
        tf = local.get(term)
        if not tf:
            continue
        w = idf(n, index.df(term))
        total += w * (tf * (K1 + 1.0)) / (tf + K1 * (1.0 - B + B * dl / avgdl))
    return total


def bm25_rank(
    index: LexicalIndex,
    query: str,
    *,
    lineages_order: Optional[Sequence[str]] = None,
) -> Tuple[Tuple[str, float], ...]:
    """Rank lineages by best chunk score（每文档取最高块分，C07）。

    Sorted by score desc; ties break by ``lineages_order`` (stable), else by
    lineage id.  Empty corpus / no terms → empty tuple, never an exception.
    """
    if not tokenize(query) or index.n_chunks == 0:
        return ()
    best: Dict[str, float] = {}
    for chunk_id, lineage in index.lineages.items():
        s = chunk_score(index, chunk_id, query)
        if s > best.get(lineage, 0.0):
            best[lineage] = s
    if not best:
        return ()
    if lineages_order is None:
        lineages_order = ()
    order = {lineage: i for i, lineage in enumerate(lineages_order)}
    return tuple(
        sorted(best.items(), key=lambda kv: (-kv[1], order.get(kv[0], 10**9), kv[0]))
    )


def best_spans(
    index: LexicalIndex,
    lineage: str,
    query: str,
    *,
    max_spans: int = MAX_SPANS_PER_DOC,
) -> Tuple[Tuple[str, int, int, float], ...]:
    """Up to ``max_spans`` non-overlapping best chunks of *lineage*.

    Each tuple is ``(chunk_id, start_cp, end_cp, score)``; a chunk that
    overlaps an already-accepted span is skipped (C07: 每文最多 3 个去重
    span)，order is score desc then chunk_id — deterministic.
    """
    candidates: List[Tuple[float, str]] = []
    for chunk_id, owner in index.lineages.items():
        if owner != lineage:
            continue
        s = chunk_score(index, chunk_id, query)
        if s > 0.0:
            candidates.append((s, chunk_id))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    accepted: List[Tuple[str, int, int, float]] = []
    for s, cid in candidates:
        if len(accepted) >= max_spans:
            break
        span = index.chunk_spans[cid]
        if any(
            not (span[1] <= a_start or a_end <= span[0])
            for _, a_start, a_end, _ in accepted
        ):
            continue
        accepted.append((cid, span[0], span[1], s))
    return tuple(accepted)


def rrf(rank_1: int, rank_2: Optional[int], *, k: float = RRF_K) -> float:
    """Reciprocal-rank fusion of two 1-based ranks; a missing path is 0."""
    total = 0.0
    if rank_1 and rank_1 > 0:
        total += 1.0 / (k + rank_1)
    if rank_2 is not None and rank_2 > 0:
        total += 1.0 / (k + rank_2)
    return total
