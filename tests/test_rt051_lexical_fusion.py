#!/usr/bin/env python3
"""RT-051 P3b: lexical builder + gateway fusion route tests (C07).

The chain under test is the whole write→read pair on real backends: the
builder publishes a deterministic generation through the ledger, and the
gateway's ``lexical_fusion_v1`` answers from it — with stale generations
refused, degraded fallback explicit, candidate spans byte-addressable into
the same reader, and RRF semantics visible in the response.
"""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
import kb_lexical_builder as builder  # noqa: E402
from kb_ledger import dumps, read_json  # noqa: E402
from kb_storage import LocalFSBackend  # noqa: E402
from test_kb_gateway import FIXED_NOW, TOKEN  # noqa: E402
from test_kb_ingest import RefreshFixture  # noqa: E402

KB = "lexlib"
H = {gateway.TOKEN_HEADER: TOKEN}

# 语料：两件正文可区分的合格件 + 一件 placeholder（资格域外）
DOC_A = "# 看板设计\n\n" + "项目管理部看板设计说明。\n" * 90
DOC_B = "# 体外模拟\n\n" + "体外模拟测试节点交付包说明。\n" * 90
BODY_A = DOC_A.encode("utf-8")
BODY_B = DOC_B.encode("utf-8")


def seed_kb(root: Path) -> LocalFSBackend:
    backend = LocalFSBackend(root)
    entries = {
        "docdb:a": {
            "path": "raw/看板.md", "title": "看板设计", "version": 1,
            "sha256": hashlib.sha256(BODY_A).hexdigest(),
            "status": "ok", "artifact_kind": "document",
        },
        "docdb:b": {
            "path": "raw/体外模拟.md", "title": "体外模拟", "version": 1,
            "sha256": hashlib.sha256(BODY_B).hexdigest(),
            "status": "ok", "artifact_kind": "document",
        },
        "docdb:ph": {
            "path": "raw/占位.png", "title": "占位", "version": 1,
            "sha256": "2" * 64, "status": "placeholder", "artifact_kind": "placeholder",
        },
    }
    backend.write("_system/raw-index.json", dumps({
        "schema": "cwk.kb.raw-index.v1", "kb_code": "ab" * 16, "entries": entries,
    }))
    backend.write("kb.json", dumps({"schema": "cwk.kb.kb.v1", "kb_code": "ab" * 16}))
    backend.write("raw/看板.md", BODY_A)
    backend.write("raw/体外模拟.md", BODY_B)
    return backend


class BuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = seed_kb(Path(self.tmp.name) / "kb")
        self.kb_code = "ab" * 16

    def build(self, apply: bool = False) -> dict:
        report = builder.build_lexical_index(self.backend, kb_code=self.kb_code)
        if apply:
            report["publish"] = builder.publish(
                self.backend, kb_code=self.kb_code, report=report
            )
        return report

    def test_dry_report_counts_and_excludes_placeholder(self) -> None:
        r = self.build()
        self.assertTrue(r["ok"])
        self.assertEqual(r["eligible_docs"], 2)
        self.assertEqual(r["indexed_docs"], 2)
        self.assertEqual(r["excluded_counts"], {"placeholder": 1})
        self.assertFalse(r["up_to_date"])  # 尚未发布
        # 干跑零写入
        self.assertFalse(self.backend.exists("_system/lexical-index.json"))

    def test_publish_then_idempotent_zero_write(self) -> None:
        r1 = self.build(apply=True)
        self.assertTrue(r1["publish"]["wrote"])
        r2 = self.build(apply=True)
        self.assertFalse(r2["publish"]["wrote"])  # 同代幂等
        self.assertEqual(r1["generation"], r2["generation"])
        published = read_json(self.backend, "_system/lexical-index.json")
        self.assertEqual(published["generation"], r1["generation"])
        self.assertTrue(published["coverage_complete"])
        self.assertEqual(published["excluded_counts"], {"placeholder": 1})

    def test_generation_is_deterministic_and_content_addressed(self) -> None:
        g1 = self.build()["generation"]
        g2 = self.build()["generation"]
        self.assertEqual(g1, g2)  # 无时间戳：同语料同引擎 → 同代

    def test_source_drift_between_report_and_publish_is_refused(self) -> None:
        report = self.build()
        # 报告后源漂移：改字节不升索引（SHA 对不上 → _collect 硬失败）
        self.backend.write("raw/看板.md", ("篡改" * 300).encode("utf-8"))
        with self.assertRaises(builder.BuildError):
            builder.publish(self.backend, kb_code=self.kb_code, report=report)

    def test_corrupt_index_row_is_refused_not_skipped(self) -> None:
        # 合格状态但 path 指向不存在的件 → 拒绝建代（不悄悄少件）
        idx = read_json(self.backend, "_system/raw-index.json")
        idx["entries"]["docdb:a"]["path"] = "raw/不存在.md"
        self.backend.write("_system/raw-index.json", dumps(idx))
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_utf8_boundary_skip_is_explicitly_counted(self) -> None:
        self.backend.write("raw/看板.md", b"\xff\xfe not utf8 " + BODY_A)
        # 索引 sha 也要同步改，否则先撞 SHA 漂移
        idx = read_json(self.backend, "_system/raw-index.json")
        idx["entries"]["docdb:a"]["sha256"] = hashlib.sha256(
            b"\xff\xfe not utf8 " + BODY_A
        ).hexdigest()
        self.backend.write("_system/raw-index.json", dumps(idx))
        r = self.build()
        self.assertEqual(r["excluded_counts"].get("invalid_utf8"), 1)
        self.assertEqual(r["indexed_docs"], 1)
        r2 = self.build(apply=True)
        published = read_json(self.backend, "_system/lexical-index.json")
        self.assertFalse(published["coverage_complete"])  # 有排除就不冒充完整


class FusionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = seed_kb(Path(self.tmp.name) / "kb")
        self.app = gateway.GatewayApp(
            self.backend, TOKEN, backend_kind="local",
            clock=lambda: FIXED_NOW, kb_id=KB,
        )
        report = builder.build_lexical_index(self.backend, kb_code="ab" * 16)
        builder.publish(self.backend, kb_code="ab" * 16, report=report)

    def get(self, path: str):
        return self.app.dispatch("GET", path, H)

    def test_body_only_term_is_found_through_fusion(self) -> None:
        # 「交付包」在标题/haystack 里没有、只在 docdb:b 正文里
        r = self.get(f"/v2/kb/search?kb={KB}&q=交付包说明&retrieval_mode=lexical_fusion_v1")
        self.assertEqual(r.status, 200, r.payload)
        p = r.payload
        self.assertEqual(p["schema"], "cwk.kb.search.v2")
        self.assertEqual(p["effective_mode"], "lexical_fusion_v1")
        self.assertFalse(p["degraded"])
        self.assertEqual(p["score_kind"], "rrf_rank_v1")
        self.assertEqual(p["items"][0]["lineage_id"], "docdb:b")
        hit = p["items"][0]
        self.assertTrue(hit["document_ref"])
        self.assertTrue(hit["candidate_spans"], "命中件必须带候选 span")

    def test_rrf_fusion_beats_single_path(self) -> None:
        # 「体外模拟」两路都命中 docdb:b：body 命中 + metadata(title) 命中
        r = self.get(f"/v2/kb/search?kb={KB}&q=体外模拟&retrieval_mode=lexical_fusion_v1")
        p = r.payload
        top = p["items"][0]
        self.assertEqual(top["lineage_id"], "docdb:b")
        self.assertIsNotNone(top["body_rank"])
        self.assertIsNotNone(top["metadata_rank"])
        # 融合分应高于任一单路 1/(60+1)
        self.assertGreater(top["rrf_score"], 1.0 / 61.0)

    def test_metadata_only_hit_still_fused_with_rank_nulls(self) -> None:
        # 「看板设计」在 a 的标题与正文都命中；占位件不在资格域内
        r = self.get(f"/v2/kb/search?kb={KB}&q=看板&retrieval_mode=lexical_fusion_v1")
        p = r.payload
        lineages = [i["lineage_id"] for i in p["items"]]
        self.assertIn("docdb:a", lineages)
        self.assertNotIn("docdb:ph", lineages)  # 同资格域：placeholder 不参与

    def test_candidate_spans_point_into_the_same_reader(self) -> None:
        r = self.get(f"/v2/kb/search?kb={KB}&q=交付包说明&retrieval_mode=lexical_fusion_v1")
        hit = r.payload["items"][0]
        span = hit["candidate_spans"][0]
        # span 是 raw 绝对字节坐标：用同一条 read 路径按 span 读回正文
        rr = self.get(
            f"/v2/kb/read?kb={KB}&document_ref={hit['document_ref']}"
            f"&start_byte={span['start_byte']}&end_byte={span['end_byte']}"
        )
        self.assertEqual(rr.status, 200)
        self.assertIn("交付包", rr.payload["text"])
        self.assertTrue(rr.payload["full_sha_verified"])

    def test_stale_generation_is_refused_not_served(self) -> None:
        # 源升版后 lexical 代过时：融合必须 503，不拿旧候选冒充
        new_body = ("# 体外模拟\n\n全新第二版正文，内容全变。\n" * 80).encode("utf-8")
        self.backend.write("raw/体外模拟.md", new_body)
        idx = read_json(self.backend, "_system/raw-index.json")
        idx["entries"]["docdb:b"]["version"] = 2
        idx["entries"]["docdb:b"]["sha256"] = hashlib.sha256(new_body).hexdigest()
        self.backend.write("_system/raw-index.json", dumps(idx))
        r = self.get(f"/v2/kb/search?kb={KB}&q=交付包&retrieval_mode=lexical_fusion_v1")
        self.assertEqual(r.status, 503)
        self.assertEqual(r.payload["error"]["code"], "lexical_unavailable")
        # 显式降级可用且如实标注
        r2 = self.get(
            f"/v2/kb/search?kb={KB}&q=交付包&retrieval_mode=lexical_fusion_v1"
            "&allow_degraded=metadata"
        )
        self.assertEqual(r2.status, 200)
        self.assertTrue(r2.payload["degraded"])
        self.assertEqual(r2.payload["effective_mode"], "metadata")

    def test_rebuilt_generation_recovers_fusion(self) -> None:
        report = builder.build_lexical_index(self.backend, kb_code="ab" * 16)
        builder.publish(self.backend, kb_code="ab" * 16, report=report)
        r = self.get(f"/v2/kb/search?kb={KB}&q=交付包&retrieval_mode=lexical_fusion_v1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.payload["generation"], report["generation"])

    def test_lexical_mode_still_refuses_cursors(self) -> None:
        r = self.get(
            f"/v2/kb/search?kb={KB}&q=交付包&retrieval_mode=lexical_fusion_v1&cursor=x"
        )
        self.assertEqual(r.status, 400)

    def test_query_kind_is_fusion_and_topk_is_honest(self) -> None:
        r = self.get(f"/v2/kb/search?kb={KB}&q=看板&retrieval_mode=lexical_fusion_v1")
        p = r.payload
        self.assertEqual(p["query_kind"], "fusion")
        self.assertIn(p["matched_relation"], ("exact", "lower_bound"))
        # Top-K 不冒充全量：eof=True 但语义上只是「本轮候选返回完」
        self.assertTrue(p["candidate_truncated"] is False or p["total"] > len(p["items"]))


class RefreshHookTests(RefreshFixture):
    """P3c：refresh 收尾自动重建词法代（真 ingest 链，FakeDocdb 源）。

    四条合同：未发布不自动创建（opt-in）；dry 零副作用；语料未变零写入；
    builder 故障只进报告不拖垮 refresh 本身。
    """

    def publish_lexical(self) -> str:
        report = builder.build_lexical_index(self.backend, kb_code=self.kb_code)
        builder.publish(self.backend, kb_code=self.kb_code, report=report)
        return report["generation"]

    def test_a_library_without_lexical_stays_missing(self) -> None:
        r = self.refresh(blobs={"301": b"# plan\n"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["lexical"], {"status": "missing"})  # opt-in：不自动创建
        self.assertFalse((self.kb / "_system" / "lexical-index.json").exists())

    def test_a_dry_refresh_has_no_lexical_side_effect(self) -> None:
        self.refresh(blobs={"301": b"# plan\n"})
        self.publish_lexical()
        r = self.refresh(apply=False, blobs={"301": b"# plan\n"})
        self.assertFalse(r["applied"])
        self.assertIsNone(r["lexical"])  # dry：钩子不跑

    def test_unchanged_corpus_rebuilds_nothing(self) -> None:
        self.refresh(blobs={"301": b"# plan\n"})
        gen = self.publish_lexical()
        lexical_before = (self.kb / "_system" / "lexical-index.json").read_bytes()
        r = self.refresh(blobs={"301": b"# plan\n"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["lexical"], {"status": "unchanged", "generation": gen})
        # 零写入指词法代文件逐字节不变（refresh 自身的 state/账本重签不在口径内）
        self.assertEqual(
            (self.kb / "_system" / "lexical-index.json").read_bytes(), lexical_before
        )

    def test_a_new_item_triggers_a_rebuild(self) -> None:
        self.refresh(blobs={"301": b"# plan\n"})
        gen = self.publish_lexical()
        self.folders["/玄关/合同"].append(
            {"fileId": "302", "name": "302-风险清单.md", "type": "2",
             "updateTime": "2026-08-20 10:00:00"}
        )
        r = self.refresh(blobs={"301": b"# plan\n", "302": "# 风险：体外模拟节点延期\n".encode("utf-8") * 30})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["lexical"]["status"], "rebuilt")
        self.assertNotEqual(r["lexical"]["generation"], gen)
        published = read_json(self.backend, "_system/lexical-index.json")
        self.assertEqual(published["eligible_docs"], 2)

    def test_builder_failure_is_isolated_from_refresh(self) -> None:
        self.refresh(blobs={"301": b"# plan\n"})
        self.publish_lexical()
        self.folders["/玄关/合同"].append(
            {"fileId": "302", "name": "302-新增.md", "type": "2",
             "updateTime": "2026-08-20 10:00:00"}
        )
        with mock.patch.object(
            builder, "build_lexical_index", side_effect=RuntimeError("boom")
        ):
            r = self.refresh(blobs={"301": b"# plan\n", "302": "# 新件\n".encode("utf-8")})
        self.assertTrue(r["ok"], r)  # refresh 本身没被拖垮
        self.assertEqual(r["lexical"]["status"], "failed")
        self.assertIn("boom", r["lexical"]["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
