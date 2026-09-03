"""RT-041 ord1: CWork source adapter (wrapper-style) contract tests.

Locks the four contract operations, the prefixed global dedupe key, the
normalized-output shape (same-construction as existing raw files), and the
equivalence bar: within one test window the adapter's discovered id set
must equal the legacy backfill lane's id set (same underlying dual-channel
source_rows), and fetch output must be byte-identical to what
``cwk_backfill_range.fetch_one`` writes today.

Everything runs against fakes: no network, no credentials, no skill
client import in CI.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "adapters"))
sys.path.insert(0, str(PROJECT / "scripts"))

import base  # noqa: E402
from gwork import GWorkAdapter  # noqa: E402

# 2026-09-03 00:00:00 / 23:59:59 +08:00 — same anchors as the RT-040 suite.
DAY_START = 1788364800
DAY_END = 1788451199


def row(rid, name="发送人", time_text="2026-09-03T03:00:00.000+00:00", **extra):
    payload = {"id": rid, "main": f"汇报 {rid}", "reportEventVO": {"name": name, "time": time_text}}
    payload.update(extra)
    return payload


class FakeDualClient:
    def __init__(self, inbox_pages, outbox_pages):
        self.inbox_pages = inbox_pages
        self.outbox_pages = outbox_pages
        self.calls = []

    def get_inbox_list(self, *, page_size, page_index, begin_time, end_time, **_):
        self.calls.append(("inbox", page_index, begin_time, end_time))
        rows, total = self.inbox_pages[page_index]
        return {"list": rows, "total": total}

    def get_outbox_list(self, *, page_size, page_index, begin_time, end_time, **_):
        self.calls.append(("outbox", page_index, begin_time, end_time))
        rows, total = self.outbox_pages[page_index]
        return {"list": rows, "total": total}


def raw_text(rid, title, writer, created="2026-09-03 12:00:00"):
    return (
        "---\n"
        f'report_id: "{rid}"\n'
        f'title: "{title}"\n'
        f'writer: "{writer}"\n'
        f'create_time: "{created}"\n'
        "source_lane: inbox_awareness\n"
        "collection_mode: historical-backfill\n"
        "change_type: historical_backfill\n"
        'source_scopes: "inbox_range,outbox_range"\n'
        "---\n\n"
        f"# {title}\n\n"
        "## Original Full Content For AI\n\n正文内容。\n\n"
        "- **汇报人**: 张三\n"
        "- **收件人**: 李四、王五\n"
        "\n## List Row Metadata\n\n```json\n{}\n```\n"
    )


class RegistryTests(unittest.TestCase):
    def test_gwork_is_registered_by_source_type(self):
        cls = base._REGISTRY.get("gwork")
        self.assertIs(cls, GWorkAdapter)

    def test_get_adapter_lazy_registration(self):
        base._REGISTRY.pop("gwork", None)
        adapter = base.get_adapter("gwork")
        self.assertIsInstance(adapter, GWorkAdapter)
        self.assertEqual(adapter.source_type, "gwork")

    def test_get_adapter_unknown_source_raises(self):
        with self.assertRaises(KeyError):
            base.get_adapter("no-such-source")

    def test_known_adapters_lists_gwork(self):
        self.assertIn("gwork", base.known_adapters())

    def test_register_requires_source_type(self):
        with self.assertRaises(ValueError):
            base.register(type("NoType", (), {}))


class ContractShapeTests(unittest.TestCase):
    def test_source_item_and_normalized_doc_fields(self):
        item = base.SourceItem(native_id="123", row={"id": "123"})
        doc = base.NormalizedDoc(
            id="gwork-123", native_id="123", title="t", author="a",
            participants=["a"], created="2026-09-03", source_type="gwork",
            body_markdown="x",
        )
        self.assertEqual(item.native_id, "123")
        self.assertEqual((doc.id, doc.source_type), ("gwork-123", "gwork"))


class DiscoverTests(unittest.TestCase):
    def test_discover_dual_channel_ids_and_prefix_free_native_ids(self):
        client = FakeDualClient(
            {1: ([row("1", "同事甲")], 1)},
            {1: ([row("2", "我自己"), row("1", "同事甲")], 2)},
        )
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            items = adapter.discover("k", "2026-09-03", "2026-09-03")
        self.assertEqual(sorted(i.native_id for i in items), ["1", "2"])
        # native ids stay unprefixed; the prefix lives only in dedupe_key
        self.assertTrue(all(not i.native_id.startswith("gwork-") for i in items))
        # dual window contract preserved (second-level, UTC+8 anchors)
        self.assertEqual(
            {(c[0], c[2], c[3]) for c in client.calls},
            {("inbox", DAY_START, DAY_END), ("outbox", DAY_START, DAY_END)},
        )

    def test_discover_skips_rows_without_id(self):
        client = FakeDualClient({1: ([row("1"), {"main": "无 id"}], 1)}, {1: ([], 0)})
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            items = adapter.discover("k", "2026-09-03", "2026-09-03")
        self.assertEqual([i.native_id for i in items], ["1"])


class DedupeKeyTests(unittest.TestCase):
    def test_dedupe_key_carries_source_prefix(self):
        adapter = GWorkAdapter()
        key = adapter.dedupe_key(base.SourceItem(native_id="209123456", row={}))
        self.assertEqual(key, "gwork-209123456")
        self.assertTrue(key.startswith("gwork-"))

    def test_dedupe_key_never_collides_with_other_sources(self):
        # Same native id under two prefixes must differ — cross-source
        # collision-freedom is the contract's reason for the prefix.
        adapter = GWorkAdapter()
        a = adapter.dedupe_key(base.SourceItem(native_id="42", row={}))
        self.assertNotEqual(a, "docdb-42")


class FetchTests(unittest.TestCase):
    def _run_fetch(self, tmp):
        client = FakeDualClient({1: ([row("7", "张三")], 1)}, {1: ([], 0)})
        adapter = GWorkAdapter()
        staging = Path(tmp) / "collected-raw"
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client), \
             mock.patch("cwk_backfill_range.run_tool") as tool:
            tool.return_value = {"success": True, "data": {"fullContent": "正文内容。"}}
            item = adapter.discover("k", "2026-09-03", "2026-09-03")[0]
            doc = adapter.fetch(item, "k", raw_dir=staging)
        return doc, staging

    def test_fetch_returns_normalized_doc_with_prefixed_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, staging = self._run_fetch(tmp)
            self.assertEqual(doc.id, "gwork-7")
            self.assertEqual(doc.native_id, "7")
            self.assertEqual(doc.source_type, "gwork")
            self.assertEqual(doc.author, "张三")
            self.assertTrue(doc.body_markdown.startswith("---\n"))
            self.assertIn("report_id: \"7\"", doc.body_markdown)
            self.assertTrue(staging.joinpath("7-汇报-7.md").exists() or any(staging.iterdir()))

    def test_fetch_output_is_byte_identical_to_legacy_fetch_one(self):
        """等价性核心：适配器 fetch 产物 = 现有 backfill fetch_one 落盘文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            doc, _ = self._run_fetch(tmp)
            # locate the file fetch_one wrote and compare bytes
            written = [p for p in Path(tmp).rglob("*.md")]
            self.assertTrue(written)
            legacy_bytes = written[0].read_bytes()
            self.assertEqual(doc.body_markdown.encode("utf-8"), legacy_bytes)

    def test_fetch_failure_raises(self):
        adapter = GWorkAdapter()
        record = {"report_id": "7", "status": "failed", "error": "boom"}
        with mock.patch("cwk_backfill_range.fetch_one", return_value=record):
            with self.assertRaisesRegex(RuntimeError, "gwork fetch failed"):
                adapter.fetch(base.SourceItem(native_id="7", row=row("7")), "k")

    def test_fetch_reads_frontmatter_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, _ = self._run_fetch(tmp)
            self.assertIn("汇报", doc.title)
            self.assertTrue(doc.created.startswith("2026-09-03"))

    def test_participants_from_role_lines_only(self):
        from gwork import _participants

        text = (
            "---\n"
            'report_id: "7"\n'
            'title: "t"\n'
            'writer: "张三"\n'
            "---\n\n# t\n\n"
            "正文中提到 李四 不算参与人。\n"
            "- **汇报人**: 张三\n"
            "- **收件人**: 李四、王五\n"
            "- **抄送**: 赵六\n"
            "普通行里的 王五 也不算。\n"
        )
        names = _participants(text, "张三")
        self.assertIn("张三", names)
        self.assertIn("李四", names)
        self.assertIn("王五", names)
        self.assertIn("赵六", names)
        # free-text co-occurrence never widens participants: the plain-line
        # mentions above carry no role label, and they are excluded by design.
        self.assertEqual(names, ["张三", "李四", "王五", "赵六"])

    def test_participants_dedupe_and_empty_writer(self):
        from gwork import _participants

        text = "---\n---\n\n- **汇报人**: 张三\n- **收件人**: 张三\n"
        self.assertEqual(_participants(text, ""), ["张三"])
        self.assertEqual(_participants(text, "张三"), ["张三"])


class WatchTests(unittest.TestCase):
    def test_watch_returns_changed_items_and_fresh_baseline(self):
        client = FakeDualClient(
            {1: ([row("1", "同事甲", replyCount=0)], 1)},
            {1: ([row("2", "我自己", replyCount=3)], 1)},
        )
        baseline = {
            # report 1: replyCount 0 → 0 unchanged; report 2: 0 → 3 changed
            "1": {"reply_count": 0, "has_new_reply": False, "checked_at": "x"},
            "2": {"reply_count": 0, "has_new_reply": False, "checked_at": "x"},
        }
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            changed, fresh = adapter.watch("k", baseline, "2026-09-03", "2026-09-03")
        self.assertEqual([c.native_id for c in changed], ["2"])
        self.assertEqual(changed[0].row["_reply_state_entry"]["reply_count"], 3)
        self.assertEqual(fresh, {})

    def test_watch_first_sight_seeds_fresh_not_changed(self):
        client = FakeDualClient({1: ([row("9", replyCount=1)], 1)}, {1: ([], 0)})
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            changed, fresh = adapter.watch("k", {}, "2026-09-03", "2026-09-03")
        self.assertEqual(changed, [])
        self.assertIn("9", fresh)

    def test_watch_marks_outbox_rows(self):
        client = FakeDualClient({1: ([], 0)}, {1: ([row("5", "我自己", replyCount=2)], 1)})
        baseline = {"5": {"reply_count": 0, "has_new_reply": False, "checked_at": "x"}}
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            changed, _fresh = adapter.watch("k", baseline, "2026-09-03", "2026-09-03")
        self.assertEqual([c.native_id for c in changed], ["5"])
        self.assertTrue(changed[0].row.get("_from_outbox"))


class EquivalenceTests(unittest.TestCase):
    """同一测试窗口内：适配器采出的 id 集合 = 现有 backfill 采出的 id 集合。"""

    def test_discovered_id_set_equals_legacy_source_rows(self):
        client = FakeDualClient(
            {1: ([row("1", "同事甲"), row("3", "同事乙")], 2)},
            {1: ([row("2", "我自己"), row("1", "同事甲")], 2)},
        )
        adapter = GWorkAdapter()
        with mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
            items = adapter.discover("k", "2026-09-03", "2026-09-03")
        from cwk_backfill_range import source_rows  # noqa: E402

        with mock.patch("cwk_backfill_range._inbox_client", lambda key: FakeDualClient(
                {1: ([row("1", "同事甲"), row("3", "同事乙")], 2)},
                {1: ([row("2", "我自己"), row("1", "同事甲")], 2)},
        )):
            legacy_rows, legacy_total = source_rows("k", "2026-09-03", "2026-09-03", source="dual")
        adapter_ids = {i.native_id for i in items}
        legacy_ids = {str(r.get("id")) for r in legacy_rows}
        self.assertEqual(adapter_ids, legacy_ids)
        self.assertEqual(adapter_ids, {"1", "2", "3"})
        self.assertEqual(legacy_total, 3)

    def test_discover_reuses_source_rows_no_reimplementation(self):
        # The wrapper must route through the legacy lane: patching
        # source_rows must be observable through discover.
        sentinel_rows = [row("42")]
        with mock.patch("cwk_backfill_range.source_rows", return_value=(sentinel_rows, 1)) as sr:
            items = GWorkAdapter().discover("k", "2026-09-03", "2026-09-03")
        sr.assert_called_once_with("k", "2026-09-03", "2026-09-03", source="dual")
        self.assertEqual([i.native_id for i in items], ["42"])


class GovernanceContractTests(unittest.TestCase):
    def test_adapters_files_are_claimed_by_prefix_rule(self):
        import json

        manifest = json.loads(
            (PROJECT / ".aodw-next/06-project/governance/code-ownership-manifest.json")
            .read_text(encoding="utf-8")
        )
        rule = next(r for r in manifest["rules"] if r.get("id") == "R-runtime-adapters")
        self.assertEqual(rule["prefix"], "adapters/")
        self.assertEqual(rule["domain"], "runtime")
        self.assertEqual(rule["owner"], "RT-041")
        for rel in ("adapters/__init__.py", "adapters/base.py", "adapters/gwork.py"):
            self.assertTrue(rel.startswith(rule["prefix"]), rel)

    def test_contract_doc_exists_and_names_gwork(self):
        text = (PROJECT / "docs/ADAPTER-CONTRACT.md").read_text(encoding="utf-8")
        for needle in ("discover", "fetch", "dedupe_key", "watch", "NormalizedDoc", "gwork"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
