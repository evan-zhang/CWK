"""RT-070 判据：从 NAS 同步到检索索引。

这个脚本决定"线上能查到什么"，所以它出错的方式有两种，判据要分别挡住：

- **少收**：把真实内容当异常丢掉。第一版实测就犯了——读了 originals（原件，可能是
  docx/png）而不是 raw_path（转换后的文本），投前资料库当场少 26 条；另外两份
  27MB / 14MB 的大文档也被上限直接跳过。
- **多收**：把不该进的收进来。placeholder 的 raw 文件只是占位，收进去等于把空壳当文档。

还有一条比"收多少"更要紧：**失败时不能上线半份数据**。宁可线上停在昨天。

全部用合成夹具，不碰 NAS、不连检索后端。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_snapshot as snap  # noqa: E402


def make_bank(nas: Path, bank: str, items: dict, *, files: dict | None = None) -> None:
    """造一个库：账本 + raw 下的文本文件。"""
    (nas / bank / "_system").mkdir(parents=True, exist_ok=True)
    (nas / bank / "_system" / "ingest-state.json").write_text(
        json.dumps({"kb_code": bank, "items": items}, ensure_ascii=False), encoding="utf-8")
    for relative, text in (files or {}).items():
        target = nas / bank / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def record(raw_path: str, *, status: str = "converted", originals: str = "", updated: str = "2026-09-20T00:00:00Z") -> dict:
    return {"status": status, "raw_path": raw_path, "updated_at": updated,
            "originals": originals or raw_path.replace("raw/", "originals/")}


class TextComesFromRawPathTests(unittest.TestCase):
    """第一版读错了文件——这一组就是为那次事故立的判据。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nas = Path(self.tmp.name)

    def test_text_is_read_from_raw_path_even_when_the_original_is_a_docx(self):
        make_bank(self.nas, "b", {
            "docdb:1": record("raw/c/1-标题甲.md", originals="originals/docdb/id-1/abc.docx"),
        }, files={"raw/c/1-标题甲.md": "正文甲"})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual(result.usable, 1, "原件是 docx 不代表没有文本")
        self.assertEqual(result.documents[0]["text"], "正文甲")

    def test_a_placeholder_is_not_a_document(self):
        make_bank(self.nas, "b", {
            "docdb:1": record("raw/c/1.md"),
            "docdb:2": record("raw/c/2.md", status="placeholder"),
        }, files={"raw/c/1.md": "正文", "raw/c/2.md": "（占位）"})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual([d["doc_id"] for d in result.documents], ["docdb:1"])
        self.assertEqual(result.skipped_unconverted, 1)

    def test_a_failed_refresh_does_not_hide_a_document_that_still_exists(self):
        """2027 规划库实测：155 条全标 failed，文本却都在。按 status 过滤会让整库消失。"""
        make_bank(self.nas, "b", {
            "docdb:1": record("raw/c/1.md", status="failed"),
            "docdb:2": record("raw/c/2.md", status="failed"),
        }, files={"raw/c/1.md": "正文一", "raw/c/2.md": "正文二"})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual(result.usable, 2, "更新失败 ≠ 文档不可用")

    def test_a_missing_file_is_counted_not_guessed(self):
        make_bank(self.nas, "b", {"docdb:1": record("raw/c/gone.md")})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual((result.usable, result.skipped_missing), (0, 1))

    def test_the_index_map_points_into_the_nas_not_into_ops(self):
        make_bank(self.nas, "b", {"docdb:1": record("raw/c/1.md")}, files={"raw/c/1.md": "正文"})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual(result.documents[0]["_relative"], "b/raw/c/1.md")

    def test_a_title_is_taken_from_the_human_part_of_the_filename(self):
        self.assertEqual(snap.title_from("raw/c/2091816583587504129-00-SPBP建设规则书.md", "x.md"),
                         "SPBP建设规则书")
        self.assertEqual(snap.title_from("", "abc.md"), "abc")


class LargeDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nas = Path(self.tmp.name)

    def test_a_very_long_document_is_kept_and_truncated_for_the_index_only(self):
        """27MB 的审批件是真内容，不能丢；但也不能整篇塞进索引。"""
        long_text = "甲" * (snap.MAX_INDEX_CHARS + 5000)
        make_bank(self.nas, "b", {"cwork:1": record("raw/c/1.md")}, files={"raw/c/1.md": long_text})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual(result.usable, 1, "大文档必须收")
        self.assertEqual(result.truncated, 1)
        self.assertEqual(len(result.documents[0]["text"]), snap.MAX_INDEX_CHARS)
        self.assertEqual(result.documents[0]["_relative"], "b/raw/c/1.md",
                         "映射仍指向完整原文，读原文拿得到全文")

    def test_something_absurdly_large_is_still_refused(self):
        make_bank(self.nas, "b", {"cwork:1": record("raw/c/1.md")}, files={"raw/c/1.md": "x"})
        original = snap.MAX_DOC_BYTES
        snap.MAX_DOC_BYTES = 0
        try:
            result = snap.collect_bank(self.nas, "b")
        finally:
            snap.MAX_DOC_BYTES = original
        self.assertEqual((result.usable, result.skipped_oversize), (0, 1))


class NeverShipHalfTests(unittest.TestCase):
    """失败时线上保持原样——这条比"同步多少条"重要。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nas = Path(self.tmp.name)
        self.root = Path(self.tmp.name)

    def test_a_big_drop_is_refused(self):
        # 实测那次：应有 114 条，只读到 88 条（读错了字段）。这道闸必须拦住它。
        results = [snap.BankResult(bank="b", documents=[{"doc_id": str(i)} for i in range(88)])]
        problems = snap.check_no_collapse(results, {"b": 114})
        self.assertTrue(problems)
        self.assertIn("拒绝切换", problems[0])

    def test_an_empty_bank_is_refused_even_if_it_was_small(self):
        results = [snap.BankResult(bank="b", documents=[])]
        self.assertTrue(snap.check_no_collapse(results, {"b": 3}))

    def test_normal_growth_is_fine(self):
        results = [snap.BankResult(bank="b", documents=[{"doc_id": str(i)} for i in range(564)])]
        self.assertEqual(snap.check_no_collapse(results, {"b": 502}), [])

    def test_a_brand_new_bank_is_not_treated_as_a_drop(self):
        results = [snap.BankResult(bank="new", documents=[{"doc_id": "1"}])]
        self.assertEqual(snap.check_no_collapse(results, {}), [])

    def test_a_missing_nas_is_a_clear_refusal_not_an_empty_sync(self):
        with self.assertRaises(snap.SourceError) as raised:
            snap.collect(self.nas / "not-mounted", ["b"])
        self.assertIn("挂载点不可用", str(raised.exception))

    def test_a_missing_ledger_is_a_refusal(self):
        (self.nas / "b").mkdir(parents=True)
        with self.assertRaises(snap.SourceError):
            snap.collect_bank(self.nas, "b")

    def test_a_corrupt_ledger_is_a_refusal(self):
        (self.nas / "b" / "_system").mkdir(parents=True)
        (self.nas / "b" / "_system" / "ingest-state.json").write_text("{ broken", encoding="utf-8")
        with self.assertRaises(snap.SourceError):
            snap.collect_bank(self.nas, "b")


class BuildRefusesToSwitchTests(unittest.TestCase):
    """build 在任一步失败时，都不能写映射表、不能切别名。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.nas = self.root / "nas"
        make_bank(self.nas, "b", {
            "cwork:1": record("raw/c/1.md"), "cwork:2": record("raw/c/2.md"),
        }, files={"raw/c/1.md": "正文一", "raw/c/2.md": "正文二"})
        self.index_map = self.root / "rag-index.json"
        self.index_map.write_text(json.dumps({"cwork:1": "b/raw/c/1.md", "cwork:2": "b/raw/c/2.md"}),
                                  encoding="utf-8")
        self.meta = self.root / "meta.json"
        self.calls = []
        self._real = (snap.build_index, snap.verify_index, snap.switch_alias)
        snap.build_index = lambda documents, **kw: self.calls.append("build") or {"document_count": len(documents)}
        snap.verify_index = lambda index_name, results, **kw: self.calls.append("verify") or {"indexed_chunks": 9}
        snap.switch_alias = lambda alias, new: self.calls.append("switch") or {"alias": alias, "now": new, "was": []}

    def tearDown(self) -> None:
        snap.build_index, snap.verify_index, snap.switch_alias = self._real

    def args(self, **over):
        import argparse
        base = dict(nas=str(self.nas), banks=["b"], index_map=str(self.index_map), meta=str(self.meta),
                    index_prefix="cwk-retrieval", alias="cwk-retrieval", tenant="default", dry_run=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_good_run_builds_verifies_then_switches_in_that_order(self):
        payload, code = snap.cmd_build(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["build", "verify", "switch"], "先验后切，顺序不能反")
        self.assertTrue(payload["switched"])
        self.assertEqual(json.loads(self.meta.read_text(encoding="utf-8"))["total"], 2)

    def test_a_failed_verification_leaves_the_old_map_and_alias_alone(self):
        before = self.index_map.read_bytes()
        snap.verify_index = lambda *a, **kw: (_ for _ in ()).throw(snap.VerifyError("抽样查不到"))
        with self.assertRaises(snap.VerifyError):
            snap.cmd_build(self.args())
        self.assertNotIn("switch", self.calls, "校验没过就不许切")
        self.assertEqual(self.index_map.read_bytes(), before)
        self.assertFalse(self.meta.exists())

    def test_a_collapse_is_refused_before_anything_is_built(self):
        self.index_map.write_text(json.dumps({f"cwork:{i}": "b/raw/c/x.md" for i in range(50)}),
                                  encoding="utf-8")
        with self.assertRaises(snap.VerifyError):
            snap.cmd_build(self.args())
        self.assertEqual(self.calls, [], "内容暴跌时连索引都不该建")

    def test_dry_run_builds_and_verifies_but_changes_nothing(self):
        before = self.index_map.read_bytes()
        payload, code = snap.cmd_build(self.args(dry_run=True))
        self.assertEqual((code, payload["switched"]), (0, False))
        self.assertEqual(self.calls, ["build", "verify"])
        self.assertEqual(self.index_map.read_bytes(), before)
        self.assertFalse(self.meta.exists())

    def test_the_map_is_replaced_atomically(self):
        snap.cmd_build(self.args())
        mapping = json.loads(self.index_map.read_text(encoding="utf-8"))
        self.assertEqual(sorted(mapping), ["cwork:1", "cwork:2"])
        self.assertFalse(self.index_map.with_name(self.index_map.name + ".new").exists(),
                         "临时文件不该留下")


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nas = Path(self.tmp.name)

    def test_each_bank_reports_how_fresh_it_is(self):
        """"数据截止到哪天"必须能答上来——否则再旧的回答看起来都一样自信。"""
        make_bank(self.nas, "b", {
            "cwork:1": record("raw/c/1.md", updated="2026-09-10T00:00:00Z"),
            "cwork:2": record("raw/c/2.md", updated="2026-09-19T15:30:06Z"),
        }, files={"raw/c/1.md": "一", "raw/c/2.md": "二"})
        result = snap.collect_bank(self.nas, "b")
        self.assertEqual(result.newest_update, "2026-09-19T15:30:06Z")
        self.assertIn("newest_update", result.as_dict())


if __name__ == "__main__":
    unittest.main()
