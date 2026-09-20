"""RT-070 判据：读原文可以同时认「本地副本」和「NAS 挂载点」两个根。

为什么需要：内容同步会把映射表从「指向 OPS 本地副本」换成「指向 NAS」。
如果读原文只能认一个根，那么换映射表和换容器配置之间必然有一段
「搜得到但读不到」的窗口。让它同时认两个根，切换就没有窗口。

安全性不能因此松动：解析后的路径仍必须落在某个配置根内部，
而且同一份相对路径在多个根下都存在时要拒绝，不能随便挑一个。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.rag_answer.resolver import DocResolver, ResolveError  # noqa: E402


class TwoRootsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.old_root = base / "local-copy"
        self.nas_root = base / "nas"
        (self.old_root / "bank-a").mkdir(parents=True)
        (self.old_root / "bank-a" / "old.md").write_text("旧副本里的正文", encoding="utf-8")
        (self.nas_root / "bank-a" / "raw" / "c").mkdir(parents=True)
        (self.nas_root / "bank-a" / "raw" / "c" / "new.md").write_text("NAS 上的正文", encoding="utf-8")
        index = base / "index.json"
        index.write_text(json.dumps({
            "doc-old": "bank-a/old.md",
            "doc-new": "bank-a/raw/c/new.md",
            "doc-gone": "bank-a/missing.md",
        }), encoding="utf-8")
        self.resolver = DocResolver(roots=[self.old_root, self.nas_root], index_path=index)

    def test_both_the_old_copy_and_the_nas_resolve(self):
        self.assertEqual(self.resolver.resolve("doc-old"), "旧副本里的正文")
        self.assertEqual(self.resolver.resolve("doc-new"), "NAS 上的正文")

    def test_the_bank_is_still_read_from_the_first_path_segment(self):
        """RT-062 的鉴权靠这个判断文档属于哪个库，不能被多根改坏。"""
        self.assertEqual(self.resolver.bank_of("doc-new", "default"), "bank-a")
        self.assertEqual(self.resolver.bank_of("doc-old", "default"), "bank-a")

    def test_a_document_that_exists_nowhere_is_a_plain_miss(self):
        with self.assertRaises(KeyError):
            self.resolver.resolve("doc-gone")

    def test_the_same_path_under_two_roots_is_refused_rather_than_guessed(self):
        (self.nas_root / "bank-a" / "old.md").write_text("另一份同名文件", encoding="utf-8")
        with self.assertRaises(ResolveError) as raised:
            self.resolver.resolve("doc-old")
        self.assertIn("more than one root", str(raised.exception))

    def test_escaping_the_roots_is_still_refused(self):
        base = Path(self.tmp.name)
        (base / "outside.md").write_text("不该被读到", encoding="utf-8")
        index = base / "evil.json"
        index.write_text(json.dumps({"evil": "../outside.md"}), encoding="utf-8")
        resolver = DocResolver(roots=[self.old_root, self.nas_root], index_path=index)
        with self.assertRaises(ResolveError):
            resolver.resolve("evil")

    def test_a_single_root_behaves_as_before(self):
        index = Path(self.tmp.name) / "single.json"
        index.write_text(json.dumps({"doc-new": "bank-a/raw/c/new.md"}), encoding="utf-8")
        resolver = DocResolver(roots=[self.nas_root], index_path=index)
        self.assertEqual(resolver.resolve("doc-new"), "NAS 上的正文")


class IndexReloadTests(unittest.TestCase):
    """映射表换掉之后，读原文必须跟着换，且不能靠重启服务。

    真实事故：第一次内容同步完成、索引和映射表都更新了，但问答服务仍按
    启动时那份旧表回答，新同步进来的 67 份文档一律 404。上线当天才发现。
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "root" / "bank-a").mkdir(parents=True)
        (self.base / "root" / "bank-a" / "old.md").write_text("旧正文", encoding="utf-8")
        (self.base / "root" / "bank-a" / "new.md").write_text("新正文", encoding="utf-8")
        self.index = self.base / "index.json"
        self._write_index({"doc-old": "bank-a/old.md"})
        self.resolver = DocResolver(roots=[self.base / "root"], index_path=self.index)

    def _write_index(self, mapping) -> None:
        """按同步流水线的写法落盘：先写临时文件再原子改名。"""
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping), encoding="utf-8")
        tmp.replace(self.index)

    def test_new_document_is_readable_without_restart(self):
        self.assertEqual(self.resolver.resolve("doc-old"), "旧正文")
        with self.assertRaises(KeyError):
            self.resolver.resolve("doc-new")
        self._write_index({"doc-old": "bank-a/old.md", "doc-new": "bank-a/new.md"})
        self.assertEqual(self.resolver.resolve("doc-new"), "新正文")

    def test_bank_of_also_sees_the_new_index(self):
        self.assertIsNone(self.resolver.bank_of("doc-new", "默认库"))
        self._write_index({"doc-new": "bank-a/new.md"})
        self.assertEqual(self.resolver.bank_of("doc-new", "默认库"), "bank-a")

    def test_broken_new_index_keeps_serving_the_previous_one(self):
        self.assertEqual(self.resolver.resolve("doc-old"), "旧正文")
        self.index.write_text('{"doc-old": "bank-a/old', encoding="utf-8")
        self.assertEqual(self.resolver.resolve("doc-old"), "旧正文")
        self._write_index({"doc-old": "bank-a/old.md", "doc-new": "bank-a/new.md"})
        self.assertEqual(self.resolver.resolve("doc-new"), "新正文")

    def test_unchanged_index_is_not_reread_from_disk(self):
        calls = []
        original = self.resolver._load
        self.resolver._load = lambda: (calls.append(1), original())[1]
        for _ in range(3):
            self.resolver.resolve("doc-old")
        self.assertEqual(calls, [])
        self._write_index({"doc-old": "bank-a/old.md"})
        self.resolver.resolve("doc-old")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
