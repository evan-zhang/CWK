"""RT-040 ord2: reply-dynamics refresh keeps originals immutable.

Baseline detection (replyCount/hasNewReply vs wiki/_system/reply-state.json),
v2 sibling writes, original-file immutability, manifest registration,
recompile triggering, and dry-run behaviour.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from reply_refresh import (  # noqa: E402
    detect_changes,
    load_baseline,
    next_version_path,
    refresh,
    save_baseline,
    write_v2_raw,
)


def row(rid, *, reply_count=0, has_new=False, name="罗毓娴", main=None):
    return {"id": rid, "main": main or f"汇报 {rid}", "replyCount": reply_count,
            "hasNewReply": has_new, "reportEventVO": {"name": name, "time": "2026-09-03T03:00:00.000+00:00"}}


class DetectChangesTests(unittest.TestCase):
    def test_first_sight_seeds_baseline_without_flagging(self):
        changed, fresh = detect_changes({}, [row("1", reply_count=3)])
        self.assertEqual(changed, [])
        self.assertIn("1", fresh)
        self.assertEqual(fresh["1"]["reply_count"], 3)

    def test_count_change_is_detected(self):
        baseline = {"1": {"reply_count": 2, "has_new_reply": False, "checked_at": "x"}}
        changed, fresh = detect_changes(baseline, [row("1", reply_count=3)])
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["id"], "1")
        self.assertEqual(fresh, {})

    def test_has_new_reply_change_is_detected(self):
        baseline = {"1": {"reply_count": 1, "has_new_reply": False, "checked_at": "x"}}
        changed, _ = detect_changes(baseline, [row("1", reply_count=1, has_new=True)])
        self.assertEqual(len(changed), 1)

    def test_unchanged_rows_are_ignored(self):
        baseline = {"1": {"reply_count": 1, "has_new_reply": False, "checked_at": "x"}}
        changed, fresh = detect_changes(baseline, [row("1", reply_count=1)])
        self.assertEqual((changed, fresh), ([], {}))


class NextVersionPathTests(unittest.TestCase):
    def test_plain_name_gets_v2(self):
        original = Path("/mirror/raw/2026-09/2026-09-01/209123-标题甲.md")
        target = next_version_path(original, "标题甲")
        self.assertEqual(target.name, "209123-v2-标题甲.md")

    def test_v2_name_gets_v3(self):
        original = Path("/mirror/raw/2026-09/2026-09-01/209123-v2-标题甲.md")
        target = next_version_path(original, "标题甲")
        self.assertEqual(target.name, "209123-v3-标题甲.md")

    def test_existing_v2_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            (parent / "209123-v2-标题甲.md").write_text("x", encoding="utf-8")
            original = parent / "209123-标题甲.md"
            target = next_version_path(original, "标题甲")
            self.assertEqual(target.name, "209123-v3-标题甲.md")


class WriteV2RawTests(unittest.TestCase):
    def _original(self, root: Path) -> Path:
        original = root / "raw" / "2026-09" / "2026-09-01" / "209777-原始汇报.md"
        original.parent.mkdir(parents=True)
        original.write_text(
            "---\nreport_id: \"209777\"\ntitle: \"原始汇报\"\nwriter: \"甲\"\ncreate_time: \"2026-09-01 10:00:00\"\nsource_lane: inbox_awareness\ncollection_mode: live-incremental\n---\n\n# 原始汇报\n\n旧正文\n",
            encoding="utf-8",
        )
        return original

    def test_v2_written_and_original_bytes_untouched(self):
        import tempfile

        detail = {
            "full": {"success": True, "data": {"fullContent": "新正文"}},
            "simple": {"success": True, "data": {"replyList": [{"replyContent": "回复1"}]}},
            "node": {"success": True, "data": {"nodeList": []}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self._original(root)
            before = original.read_bytes()
            target = write_v2_raw(root, original, row("209777", reply_count=2, main="原始汇报"), detail)
            self.assertTrue(target.exists())
            self.assertEqual(target.name, "209777-v2-原始汇报.md")
            self.assertEqual(original.read_bytes(), before, "原 raw 一字不动")
            text = target.read_text(encoding="utf-8")
            self.assertIn('change_type: reply_refresh', text)
            self.assertIn('supersedes: "209777-原始汇报.md"', text)
            self.assertIn("reply_count: 2", text)
            self.assertIn("新正文", text)
            self.assertIn("回复1", text)

    def test_baseline_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_baseline(root, {"1": {"reply_count": 2, "has_new_reply": False, "checked_at": "x"}}, established=True)
            loaded = load_baseline(root)
            self.assertEqual(loaded["1"]["reply_count"], 2)


class RefreshIntegrationTests(unittest.TestCase):
    def _mirror(self, root: Path) -> Path:
        mirror = root / "mirror"
        raw = mirror / "raw" / "2026-09" / "2026-09-03"
        raw.mkdir(parents=True)
        (raw / "209888-今日汇报.md").write_text(
            "---\nreport_id: \"209888\"\ntitle: \"今日汇报\"\nwriter: \"乙\"\ncreate_time: \"2026-09-03 10:00:00\"\nsource_lane: inbox_awareness\n---\n\n# 今日汇报\n\n正文\n",
            encoding="utf-8",
        )
        return mirror

    def test_dry_run_writes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mirror = self._mirror(Path(tmp))
            fake = mock.MagicMock()
            with mock.patch("reply_refresh._inbox_client", return_value=fake), \
                 mock.patch("cwk_backfill_range.inbox_source_rows", return_value=([row("209888", reply_count=1, main="今日汇报")], 1)), \
                 mock.patch("reply_refresh.outbox_source_rows", return_value=([], 0)):
                result = refresh(app_key="k", start_date="2026-09-03", end_date="2026-09-03",
                                 mirror_root=mirror, recompile=False, dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["changed_count"], 0)  # first sight → baseline seed
            self.assertFalse((mirror / "wiki" / "_system" / "reply-state.json").exists())

    def test_first_run_establishes_baseline_second_run_refreshes(self):
        import tempfile

        detail = {
            "full": {"success": True, "data": {"fullContent": "正文 v2"}},
            "simple": {"success": True, "data": {"replyList": [{"replyContent": "新回复"}]}},
            "node": {"success": True, "data": {}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            mirror = self._mirror(Path(tmp))
            fake = mock.MagicMock()
            original = mirror / "raw" / "2026-09" / "2026-09-03" / "209888-今日汇报.md"
            before = original.read_bytes()
            with mock.patch("reply_refresh._inbox_client", return_value=fake), \
                 mock.patch("reply_refresh.outbox_source_rows", return_value=([], 0)), \
                 mock.patch("cwk_backfill_range.inbox_source_rows", side_effect=[
                     ([row("209888", reply_count=0, main="今日汇报")], 1),   # first run: seed
                     ([row("209888", reply_count=1, main="今日汇报")], 1),   # second run: changed
                 ]), mock.patch("reply_refresh.fetch_detail", return_value=detail), \
                 mock.patch("reply_refresh.trigger_recompile", return_value={"returncode": 0}) as recompile:
                first = refresh(app_key="k", start_date="2026-09-03", end_date="2026-09-03",
                                mirror_root=mirror, recompile=True, dry_run=False)
                self.assertTrue(first["baseline_established"])
                self.assertEqual(first["changed_count"], 0)
                recompile.assert_not_called()

                second = refresh(app_key="k", start_date="2026-09-03", end_date="2026-09-03",
                                 mirror_root=mirror, recompile=True, dry_run=False)
                self.assertEqual(second["changed_count"], 1)
                self.assertEqual(second["changed_ids"], ["209888"])
                self.assertEqual(len(second["refreshed"]), 1)
                entry = second["refreshed"][0]
                self.assertEqual(entry["status"], "written")
                self.assertTrue(entry["original_untouched"])
                v2 = Path(entry["path"])
                self.assertTrue(v2.exists())
                self.assertEqual(v2.name, "209888-v2-今日汇报.md")
                self.assertEqual(original.read_bytes(), before, "原 raw 一字不动")
                recompile.assert_called_once()
                self.assertEqual(recompile.call_args[0][0], "209888")
                # manifest registered the v2 file
                manifest = json.loads((mirror / "raw" / "_system" / "raw-manifest.json").read_text(encoding="utf-8"))
                self.assertTrue(any(r["canonical_path"].endswith("209888-v2-今日汇报.md") for r in manifest["records"]))
                # baseline updated so a third identical run is a no-op
                third_rows = [row("209888", reply_count=1, main="今日汇报")]
                with mock.patch("reply_refresh._inbox_client", return_value=fake), \
                     mock.patch("cwk_backfill_range.inbox_source_rows", return_value=(third_rows, 1)), \
                     mock.patch("reply_refresh.outbox_source_rows", return_value=([], 0)):
                    third = refresh(app_key="k", start_date="2026-09-03", end_date="2026-09-03",
                                    mirror_root=mirror, recompile=True, dry_run=False)
                self.assertEqual(third["changed_count"], 0)


if __name__ == "__main__":
    unittest.main()


class CompilerTieBreakTests(unittest.TestCase):
    """RT-040 ord2: recompiles must pick the highest -vN snapshot."""

    def test_version_of(self):
        from cwk_cloud_wiki_compile import _version_of

        self.assertEqual(_version_of(Path("2095046023776104449-统计局月报.md")), 0)
        self.assertEqual(_version_of(Path("2095046023776104449-v2-统计局月报.md")), 2)
        self.assertEqual(_version_of(Path("2095046023776104449-v3-统计局月报.md")), 3)

    def test_by_id_prefers_highest_version(self):
        from cwk_cloud_wiki_compile import _version_of

        candidates = [
            Path("raw/2095046023776104449-v2-统计局月报.md"),
            Path("raw/2095046023776104449-统计局月报.md"),
        ]
        by_id: dict = {}
        for raw in candidates:  # same construction as the compiler
            rid = raw.name.split("-", 1)[0]
            if rid not in by_id or _version_of(raw) > _version_of(by_id[rid]):
                by_id[rid] = raw
        self.assertTrue(by_id["2095046023776104449"].name.startswith("2095046023776104449-v2-"))

    def test_plain_only_names_behave_identically(self):
        from cwk_cloud_wiki_compile import _version_of

        self.assertEqual(_version_of(Path("2095046023776104449-标题.md")), 0)


if __name__ == "__main__":
    unittest.main()
