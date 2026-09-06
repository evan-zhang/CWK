#!/usr/bin/env python3
"""RT-051 P3a: lexical primitive tests (C07 contracts).

Every test here pins a sentence of rt-lite C07: n-gram coverage, identifier
whole-term semantics (AB-017 ≠ 017), chunk geometry with byte mapping,
BM25 math (max-not-sum per document, idf formula, no div-zero), span
deduplication, and RRF rank arithmetic.  Pure functions only — no backend,
no library, no gateway.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_lexical import (  # noqa: E402
    CHUNK_MAX_CP,
    CHUNK_OVERLAP_CP,
    CHUNK_TARGET_CP,
    MAX_SPANS_PER_DOC,
    best_spans,
    bm25_rank,
    build_index,
    chunk_body,
    chunk_score,
    idf,
    rrf,
    tokenize,
)


def make_index(*docs: tuple[str, str]):
    return build_index([(lineage, chunk_body(body)) for lineage, body in docs])


# ── tokenize ───────────────────────────────────────────────────────────────


class TokenizeTests(unittest.TestCase):
    def test_cjk_yields_1_2_3_grams(self) -> None:
        terms = set(tokenize("体外模拟"))
        for expected in ("体", "体外", "体外模", "模拟"):
            self.assertIn(expected, terms)
        # 4 字 run：4 unigram + 3 bigram + 2 trigram = 9 tokens
        self.assertEqual(len(tokenize("体外模拟")), 9)

    def test_two_char_short_word_is_a_term(self) -> None:
        self.assertIn("体外", tokenize("体外模拟节点"))

    def test_identifier_is_one_whole_term(self) -> None:
        terms = set(tokenize("问题单 AB-017 的处理"))
        self.assertIn("ab-017", terms)
        self.assertNotIn("017", terms)
        self.assertNotIn("ab", terms)

    def test_standalone_numbers_are_their_own_terms(self) -> None:
        terms = set(tokenize("订单 017 与 12345 对账"))
        self.assertIn("017", terms)
        self.assertIn("12345", terms)
        self.assertNotIn("2345", terms)

    def test_ascii_is_lowercased(self) -> None:
        self.assertIn("hello", tokenize("Hello WORLD"))

    def test_punctuation_only_separates(self) -> None:
        self.assertEqual(set(tokenize("v1.2/3")), {"v1", "2", "3"})
        self.assertEqual(tokenize("，。！ ？"), ())

    def test_cjk_and_ascii_break_at_the_boundary(self) -> None:
        terms = set(tokenize("体外模拟AB-017节点"))
        self.assertIn("ab-017", terms)
        self.assertIn("体外", terms)
        self.assertIn("节点", terms)
        # CJK 与 ASCII 之间不造跨种词项
        self.assertFalse(any("模拟a" in t or "017节" in t for t in terms))


# ── chunk ──────────────────────────────────────────────────────────────────


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        chunks = chunk_body("短文本")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "短文本")
        self.assertEqual(chunks[0].start_cp, 0)
        self.assertEqual(chunks[0].end_cp, 3)

    def test_byte_mapping_is_exact(self) -> None:
        text = ("段落内容若干字。\n" * 150)  # 多字节 + 混合长度
        raw = text.encode("utf-8")
        for chunk in chunk_body(text):
            self.assertEqual(
                chunk.text.encode("utf-8"), raw[chunk.start_byte : chunk.end_byte]
            )

    def test_geometry_bounds_unbroken_text(self) -> None:
        text = "字" * 5000  # 无换行：硬几何
        chunks = chunk_body(text)
        for chunk in chunks:
            self.assertLessEqual(chunk.length_cp, CHUNK_MAX_CP)
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(b.start_cp, a.end_cp - CHUNK_OVERLAP_CP)

    def test_coverage_tiles_the_whole_text(self) -> None:
        text = ("行内容\n" * 300)
        chunks = chunk_body(text)
        self.assertEqual(chunks[0].start_cp, 0)
        self.assertEqual(chunks[-1].end_cp, len(text))
        for a, b in zip(chunks, chunks[1:]):
            self.assertLessEqual(b.start_cp, a.end_cp)  # 重叠或相接
            self.assertGreater(b.end_cp, a.end_cp)  # 永远前进

    def test_line_boundary_is_preferred_when_in_window(self) -> None:
        text = "行一\n" * 400  # 换行每 3 cp
        chunks = chunk_body(text)
        for chunk in chunks[:-1]:
            self.assertEqual(text[chunk.end_cp - 1], "\n")

    def test_no_strip_no_rejoin(self) -> None:
        text = ("  保留空白  \n" * 90)
        for chunk in chunk_body(text):
            self.assertEqual(chunk.text, text[chunk.start_cp : chunk.end_cp])

    def test_chunking_is_deterministic(self) -> None:
        text = ("混合 mixed 行 123。\n" * 120)
        self.assertEqual(chunk_body(text), chunk_body(text))


# ── BM25 ───────────────────────────────────────────────────────────────────


class BM25Tests(unittest.TestCase):
    def test_idf_formula_value(self) -> None:
        self.assertAlmostEqual(idf(10, 3), math.log(1.0 + (10 - 3 + 0.5) / (3 + 0.5)))
        self.assertEqual(idf(0, 0), 0.0)

    def test_empty_corpus_is_empty_not_an_exception(self) -> None:
        self.assertEqual(bm25_rank(build_index([]), "查询"), ())

    def test_termless_query_is_empty(self) -> None:
        idx = make_index(("docdb:1", "正文内容" * 60))
        self.assertEqual(bm25_rank(idx, "，。！"), ())

    def test_relevant_document_ranks_first(self) -> None:
        idx = make_index(
            ("docdb:relevant", "体外模拟测试节点交付包的正文说明"),
            ("docdb:other", "项目管理部看板设计汇报"),
        )
        ranked = bm25_rank(idx, "体外模拟")
        self.assertEqual(ranked[0][0], "docdb:relevant")

    def test_document_score_is_max_chunk_not_sum(self) -> None:
        body = (
            "填充行内容甲。\n" * 100
            + "体外模拟关键行。\n" * 40
            + "填充行内容乙。\n" * 100
        )
        idx = make_index(("docdb:long", body))
        query = "体外模拟"
        chunk_scores = [
            chunk_score(idx, cid, query)
            for cid in idx.chunk_lengths
            if idx.lineages[cid] == "docdb:long"
        ]
        self.assertGreater(max(chunk_scores), 0.0)
        ranked = bm25_rank(idx, query)
        self.assertAlmostEqual(ranked[0][1], max(chunk_scores))

    def test_rare_term_outweighs_common_term(self) -> None:
        idx = make_index(
            *[("docdb:d%d" % i, "常见内容第%d段" % i) for i in range(8)],
            ("docdb:rare", "稀有词内容"),
        )
        rare_chunk = next(
            cid for cid in idx.lineages if idx.lineages[cid] == "docdb:rare"
        )
        common_chunk = next(
            cid for cid in idx.lineages if idx.lineages[cid] == "docdb:d0"
        )
        self.assertGreater(
            chunk_score(idx, rare_chunk, "稀有词"),
            chunk_score(idx, common_chunk, "常见"),
        )

    def test_ranking_ties_break_by_given_order(self) -> None:
        idx = make_index(("docdb:b", "同样内容"), ("docdb:a", "同样内容"))
        ranked = bm25_rank(idx, "同样", lineages_order=("docdb:a", "docdb:b"))
        self.assertEqual([lin for lin, _ in ranked], ["docdb:a", "docdb:b"])

    def test_identifier_semantics_survive_scoring(self) -> None:
        idx = make_index(
            ("docdb:plain-017", "订单编号 017 的正文"),
            ("docdb:ticket", "问题单 AB-017 的处理记录"),
        )
        ranked = bm25_rank(idx, "AB-017")
        self.assertEqual([lin for lin, _ in ranked], ["docdb:ticket"])
        ranked2 = bm25_rank(idx, "017")
        self.assertEqual([lin for lin, _ in ranked2], ["docdb:plain-017"])

    def test_term_in_document_tail_still_ranks(self) -> None:
        head = "开头填充内容。\n" * 120  # 8 cp/行 → head 960 cp
        tail = "结尾提及体外模拟节点。\n" * 12
        idx = make_index(("docdb:long", head + tail))
        ranked = bm25_rank(idx, "体外模拟")
        self.assertEqual(ranked[0][0], "docdb:long")
        spans = best_spans(idx, "docdb:long", "体外模拟")
        self.assertTrue(spans)
        # span 是块坐标：含词块的终点必须越过 head（真正覆盖尾部词区域），
        # 起点落在 head 内是正常的（重叠窗口 + 边界几何）。
        self.assertGreater(spans[0][2], len(head))


# ── spans ──────────────────────────────────────────────────────────────────


class SpanTests(unittest.TestCase):
    def test_at_most_three_non_overlapping_spans(self) -> None:
        # 词散布在长文多处 → 多个候选块
        body = "".join(
            f"第{i}段铺垫内容。\n体外模拟关键句第{i}号。\n尾部填充第{i}行。\n"
            for i in range(60)
        )
        idx = make_index(("docdb:scatter", body))
        spans = best_spans(idx, "docdb:scatter", "体外模拟")
        self.assertLessEqual(len(spans), MAX_SPANS_PER_DOC)
        ordered = sorted(spans, key=lambda s: s[1])
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a[2], b[1])  # [start,end) 互不重叠

    def test_span_order_is_score_desc_then_chunk_id(self) -> None:
        body = "".join(
            f"第{i}段铺垫内容。\n体外模拟关键句第{i}号。\n尾部填充第{i}行。\n"
            for i in range(40)
        )
        idx = make_index(("docdb:s", body))
        spans = best_spans(idx, "docdb:s", "体外模拟")
        scores = [s[3] for s in spans]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_unknown_lineage_yields_nothing(self) -> None:
        idx = make_index(("docdb:1", "体外模拟"))
        self.assertEqual(best_spans(idx, "docdb:none", "体外模拟"), ())


# ── RRF ────────────────────────────────────────────────────────────────────


class RRFTests(unittest.TestCase):
    def test_fusion_math(self) -> None:
        self.assertAlmostEqual(rrf(1, 2), 1 / 61 + 1 / 62)
        self.assertAlmostEqual(rrf(3, None), 1 / 63)
        self.assertEqual(rrf(0, None), 0.0)

    def test_missing_path_contributes_zero(self) -> None:
        self.assertAlmostEqual(rrf(1, None), 1 / 61)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
