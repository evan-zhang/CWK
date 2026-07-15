import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_collection_state import (  # noqa: E402
    choose_backfill,
    choose_incremental,
    default_state,
    load_state,
    pending_entry,
    row_fingerprint,
    save_state,
)
import cwk_collect_live  # noqa: E402
import cwk_materialize_safe  # noqa: E402
import cwk_sync_mirror_to_docdb  # noqa: E402


class IncrementalCollectionTests(unittest.TestCase):
    def test_fingerprint_tracks_change_fields_but_ignores_share_link(self):
        original = {"reportId": "1", "main": "标题", "replyCount": 0, "content": "正文一", "shareLink": "old"}
        same = {**original, "shareLink": "new"}
        changed = {**original, "replyCount": 1}
        content_changed = {**original, "content": "正文二"}
        self.assertEqual(row_fingerprint(original), row_fingerprint(same))
        self.assertNotEqual(row_fingerprint(original), row_fingerprint(changed))
        self.assertNotEqual(row_fingerprint(original), row_fingerprint(content_changed))
        self.assertNotIn("content", pending_entry("1", original, {"inbox"}, "new", "daily")["row"])

    def test_incremental_selection_excludes_unchanged_and_keeps_carryover(self):
        rows = {
            "same": {"reportId": "same", "main": "未变化", "replyCount": 0},
            "changed": {"reportId": "changed", "main": "已更新", "replyCount": 2},
            "new": {"reportId": "new", "main": "新增", "replyCount": 0},
            "todo": {"reportId": "todo", "main": "延续待办", "replyCount": 0},
        }
        state = default_state()
        for rid in ("same", "changed", "todo"):
            prior = dict(rows[rid])
            if rid == "changed":
                prior["replyCount"] = 1
            state["records"][rid] = {"fingerprint": row_fingerprint(prior)}
        scopes = {rid: {"inbox"} for rid in rows}
        scopes["todo"] = {"todo_pending"}

        selected, pending, counts = choose_incremental(rows, scopes, state, detail_cap=10, continuation_cap=2)

        self.assertEqual([item["report_id"] for item in selected], ["changed", "new", "todo"])
        self.assertEqual([item["change_type"] for item in selected], ["updated", "new", "continuation"])
        self.assertEqual(pending, [])
        self.assertEqual(counts, {"new": 1, "updated": 1, "continuation": 1, "unchanged": 1})

    def test_overflow_is_persisted_for_next_run(self):
        rows = {str(i): {"reportId": str(i), "main": f"新增{i}"} for i in range(4)}
        scopes = {rid: {"inbox"} for rid in rows}
        selected, pending, _ = choose_incremental(rows, scopes, default_state(), detail_cap=2, continuation_cap=0)
        self.assertEqual([item["report_id"] for item in selected], ["0", "1"])
        self.assertEqual([item["report_id"] for item in pending], ["2", "3"])

        next_state = default_state()
        next_state["pending"] = pending
        selected_next, pending_next, _ = choose_incremental({}, {}, next_state, detail_cap=2, continuation_cap=0)
        self.assertEqual([item["report_id"] for item in selected_next], ["2", "3"])
        self.assertEqual(pending_next, [])

    def test_migration_cutoff_does_not_mislabel_old_history_as_new(self):
        state = default_state()
        state["incremental_cutoff"] = "2026-07-15T22:30:00+08:00"
        rows = {
            "old": {"reportId": "old", "main": "旧收件箱", "createTime": "2026-07-01 10:00:00"},
            "old-todo": {"reportId": "old-todo", "main": "旧待办", "createTime": "2026-07-01 10:00:00"},
            "new": {"reportId": "new", "main": "新协同", "createTime": "2026-07-16 01:00:00"},
        }
        scopes = {"old": {"inbox"}, "old-todo": {"todo_pending"}, "new": {"inbox"}}
        selected, _, counts = choose_incremental(rows, scopes, state, detail_cap=10, continuation_cap=5)
        self.assertEqual([item["report_id"] for item in selected], ["new", "old-todo"])
        self.assertEqual(counts, {"new": 1, "updated": 0, "continuation": 1, "unchanged": 1})

    def test_continuations_rotate_unprocessed_then_oldest(self):
        state = default_state()
        state["incremental_cutoff"] = "2026-07-15T22:30:00+08:00"
        rows = {
            "never": {"reportId": "never", "main": "未处理", "createTime": "2026-07-01 10:00:00"},
            "oldest": {"reportId": "oldest", "main": "较早处理", "createTime": "2026-07-01 10:00:00"},
            "recent": {"reportId": "recent", "main": "最近处理", "createTime": "2026-07-01 10:00:00"},
        }
        state["records"]["oldest"] = {"fingerprint": row_fingerprint(rows["oldest"]), "last_processed_at": "2026-07-10T10:00:00+08:00"}
        state["records"]["recent"] = {"fingerprint": row_fingerprint(rows["recent"]), "last_processed_at": "2026-07-15T10:00:00+08:00"}
        scopes = {rid: {"todo_pending"} for rid in rows}

        selected, _, _ = choose_incremental(rows, scopes, state, detail_cap=2, continuation_cap=2)
        self.assertEqual([entry["report_id"] for entry in selected], ["never", "oldest"])

    def test_backfill_skips_processed_records_and_preserves_overflow(self):
        state = default_state()
        state["records"]["old"] = {"fingerprint": "x"}
        entries = [
            pending_entry("old", {"reportId": "old"}, {"history_inbox"}, "historical_backfill", "backfill"),
            pending_entry("a", {"reportId": "a"}, {"history_inbox"}, "historical_backfill", "backfill"),
            pending_entry("b", {"reportId": "b"}, {"history_inbox"}, "historical_backfill", "backfill"),
        ]
        selected, pending = choose_backfill(state, entries, backfill_cap=1)
        self.assertEqual([item["report_id"] for item in selected], ["a"])
        self.assertEqual([item["report_id"] for item in pending], ["b"])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "collection-state.json"
            state = default_state()
            state["records"]["1"] = {"fingerprint": "abc"}
            save_state(path, state)
            loaded = load_state(path)
            self.assertEqual(loaded["records"]["1"]["fingerprint"], "abc")
            self.assertTrue(loaded["updated_at"])
            self.assertEqual(json.loads(path.read_text())["schema_version"], "cwk.collection_state.v1")

    def test_live_collector_processes_unchanged_record_only_once(self):
        row = {"reportId": "1", "main": "增量测试", "createTime": "2026-07-16 03:00:00", "replyCount": 0}

        def fake_run_tool(_script, args, _app_key):
            if "full-content-for-ai" in args:
                return {"success": True, "data": {"fullContent": "增量正文"}}
            if "record-simple-info" in args:
                return {"success": True, "data": {"reportRecordId": "1"}}
            if "node-detail" in args:
                return {"success": True, "data": {"id": "1"}}
            if args and args[0] == "list":
                return {"success": True, "items": []}
            page = args[args.index("--page-index") + 1]
            mode = args[args.index("--mode") + 1]
            return {"success": True, "data": {"list": [row] if mode == "inbox" and page == "1" else []}}

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            state_path = project / "state.json"
            with patch.object(cwk_collect_live, "PROJECT", project), patch.object(cwk_collect_live, "run_tool", side_effect=fake_run_tool):
                for run_name in ("first", "second"):
                    argv = [
                        "cwk_collect_live.py",
                        "--app-key",
                        "test-key",
                        "--run-name",
                        run_name,
                        "--state-file",
                        str(state_path),
                        "--no-backfill-enabled",
                    ]
                    with patch("sys.argv", argv):
                        with redirect_stdout(io.StringIO()):
                            cwk_collect_live.main()

            first = json.loads((project / "runs" / "first" / "collect-manifest.json").read_text())
            second = json.loads((project / "runs" / "second" / "collect-manifest.json").read_text())
            self.assertEqual(first["selected_change_counts"], {"new": 1})
            self.assertEqual(first["written_count"], 1)
            self.assertEqual(second["selected_change_counts"], {})
            self.assertEqual(second["written_count"], 0)

    def test_safe_materializer_builds_history_without_raw_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            run = project / "runs" / "test-run"
            for name in ("raw", "extracted", "events", "entities"):
                (run / name).mkdir(parents=True, exist_ok=True)
            (run / "raw" / "1.md").write_text(
                '---\nreport_id: "1"\ntitle: "历史汇报"\nwriter: "同事"\ncreate_time: "2026-07-01 10:00:00"\n---\n\ncredential sk-example_12345678901234567890',
                encoding="utf-8",
            )
            (run / "extracted" / "1.json").write_text(
                json.dumps({
                    "source_ids": ["1"], "title": "历史汇报", "event_anchor": "历史项目",
                    "collection_mode": "historical-backfill", "change_type": "historical_backfill",
                    "source_scopes": "history_inbox", "actions": ["核对 sk-example_12345678901234567890"],
                    "risks": [], "open_loops": [], "decision_points": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (run / "events" / "e.json").write_text(json.dumps({"event": "历史项目", "related_raw_ids": ["1"], "current_state": "已归档", "open_loops": [], "decisions_and_opinions": []}, ensure_ascii=False), encoding="utf-8")
            (run / "entities" / "e.json").write_text(json.dumps({"entity_name": "历史系统", "entity_type": "systems", "recent_activity": [{"source_id": "1", "title": "历史汇报"}]}, ensure_ascii=False), encoding="utf-8")
            mirror = project / "knowledge" / "工作协同镜像"
            with patch.object(cwk_materialize_safe, "PROJECT", project), patch.object(cwk_materialize_safe, "MIRROR", mirror):
                first = cwk_materialize_safe.materialize(run)
                second = cwk_materialize_safe.materialize(run)

            history_files = list((mirror / "history").rglob("*.md"))
            self.assertEqual(first["counts"]["history_pages"], 1)
            self.assertEqual(second["counts"]["history_pages"], 0)
            self.assertEqual(len(history_files), 1)
            self.assertIn("<redacted>", history_files[0].read_text())
            self.assertNotIn("sk-example", history_files[0].read_text())
            self.assertFalse((mirror / "raw").exists())

    def test_docdb_sync_filters_to_materializer_changed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mirror = root / "knowledge" / "工作协同镜像"
            (mirror / "history").mkdir(parents=True)
            (mirror / "events").mkdir(parents=True)
            (mirror / "history" / "changed.md").write_text("changed", encoding="utf-8")
            (mirror / "history" / "unchanged.md").write_text("unchanged", encoding="utf-8")
            (mirror / "events" / "changed.md").write_text("event", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"changed_relative_paths": ["history/changed.md", "events/changed.md"]}), encoding="utf-8")
            with patch.object(cwk_sync_mirror_to_docdb, "MIRROR", mirror):
                history = cwk_sync_mirror_to_docdb.iter_items(None, "history/", str(manifest))
                events = cwk_sync_mirror_to_docdb.iter_items(None, "events/", str(manifest))
            self.assertEqual([item.rel.as_posix() for item in history], ["history/changed.md"])
            self.assertEqual([item.rel.as_posix() for item in events], ["events/changed.md"])


if __name__ == "__main__":
    unittest.main()
