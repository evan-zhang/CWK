import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


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
import cwk_sample_pilot  # noqa: E402
import cwk_nightly_pipeline  # noqa: E402


class IncrementalCollectionTests(unittest.TestCase):
    def test_default_runtime_profile_is_local_and_cloud_paused(self):
        profile = cwk_nightly_pipeline.build_runtime_profile(
            cloud_first=False,
            publish_cloud_query_catalog=False,
            sync_docdb=True,
            wiki_sync=True,
        )
        self.assertEqual(profile["production_mode"], "local")
        self.assertEqual(profile["run_mode"], "local")
        self.assertEqual(profile["cloud_first_status"], "paused")
        self.assertEqual(profile["cloud_query_status"], "paused")
        self.assertFalse(profile["raw_cloud_sync"])
        self.assertEqual(profile["docdb_role"], "derived_backup_and_html_publishing")

    def test_runtime_profile_does_not_claim_unpublished_catalog(self):
        profile = cwk_nightly_pipeline.build_runtime_profile(
            cloud_first=False,
            publish_cloud_query_catalog=True,
            sync_docdb=True,
            wiki_sync=True,
            object_catalog={"returncode": 1},
        )
        self.assertEqual(profile["cloud_query_status"], "experimental_catalog_not_published")

    def test_cloud_first_is_paused_without_second_opt_in(self):
        with self.assertRaisesRegex(SystemExit, "Cloud-First is paused"):
            cwk_nightly_pipeline.enforce_cloud_pause(
                cloud_first=True,
                experimental_cloud_first=False,
                publish_cloud_query_catalog=False,
                experimental_cloud_query_catalog=False,
            )

    def test_cloud_query_catalog_is_paused_without_second_opt_in(self):
        with self.assertRaisesRegex(SystemExit, "catalog publishing is paused"):
            cwk_nightly_pipeline.enforce_cloud_pause(
                cloud_first=False,
                experimental_cloud_first=False,
                publish_cloud_query_catalog=True,
                experimental_cloud_query_catalog=False,
            )

    def test_paused_cloud_paths_allow_controlled_double_opt_in(self):
        cwk_nightly_pipeline.enforce_cloud_pause(
            cloud_first=True,
            experimental_cloud_first=True,
            publish_cloud_query_catalog=True,
            experimental_cloud_query_catalog=True,
        )

    def test_source_completeness_window_includes_prior_business_dates(self):
        self.assertEqual(
            cwk_nightly_pipeline.source_completeness_start_date("2026-08-11", 2),
            "2026-08-09",
        )

    def test_source_completeness_window_rejects_negative_lookback(self):
        with self.assertRaises(ValueError):
            cwk_nightly_pipeline.source_completeness_start_date("2026-08-11", -1)

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

    def test_safe_materializer_builds_history_and_preserves_cwork_content(self):
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
            runtime_key = "runtime-app-key-value"
            with (
                patch.object(cwk_materialize_safe, "PROJECT", project),
                patch.object(cwk_materialize_safe, "MIRROR", mirror),
                patch.dict("os.environ", {"CWORK_APP_KEY": runtime_key}, clear=False),
            ):
                extracted = json.loads((run / "extracted" / "1.json").read_text(encoding="utf-8"))
                extracted["actions"].append(f"核对 {runtime_key}")
                (run / "extracted" / "1.json").write_text(json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
                first = cwk_materialize_safe.materialize(run)
                second = cwk_materialize_safe.materialize(run)

            history_files = list((mirror / "history").rglob("*.md"))
            self.assertEqual(first["counts"]["history_pages"], 1)
            self.assertEqual(second["counts"]["history_pages"], 0)
            self.assertEqual(len(history_files), 1)
            history_text = history_files[0].read_text()
            self.assertNotIn("<redacted>", history_text)
            self.assertIn("sk-example_12345678901234567890", history_text)
            self.assertIn(runtime_key, history_text)
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

    def test_docdb_sync_uses_unique_cloud_name_for_wiki_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "工作协同镜像"
            target = mirror / "wiki" / "_system" / "manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            with patch.object(cwk_sync_mirror_to_docdb, "MIRROR", mirror):
                items = cwk_sync_mirror_to_docdb.iter_items(None, "wiki/", None)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].rel.as_posix(), "wiki/_system/manifest.json")
        self.assertEqual(items[0].file_name, "cwk-wiki-manifest-v2.json")

    def test_incremental_a4_low_volume_is_warning_not_failure(self):
        item = cwk_sample_pilot.Item("1", "事项", "甲", "2026-07-17", "reply_chain", "incremental", "updated", "inbox", "x.md", "正文")
        extraction = {
            "item_nature": "decision_or_action",
            "attention_type": "requires_action",
            "event_anchor": "事项",
            "event_family": "事项",
            "entities": [], "actions": [], "risks": [], "decision_points": [], "open_loops": [],
            "reply_chain": [], "source_ids": ["1"],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = cwk_sample_pilot.build_acceptance(
                Path(directory), [item], {"1": extraction}, {"1": []}, [], [], 1, "incremental"
            )
        self.assertTrue(result["checks"]["A4_relationship_judgment"])
        self.assertEqual(result["A4_status"], "PASS_LOW_VOLUME")
        self.assertTrue(result["warnings"])

    def test_docdb_retry_queue_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "retry.json"
            cwk_sync_mirror_to_docdb.write_retry_paths(queue, {"runs/a.md", "history/b.md"})
            self.assertEqual(
                cwk_sync_mirror_to_docdb.load_retry_paths(queue),
                {"runs/a.md", "history/b.md"},
            )

    def test_docdb_sync_state_bootstraps_latest_non_dry_run_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = {
                "generated_at": "2026-08-01T01:00:00", "dry_run": False,
                "results": [{"relative_path": "daily/a.md", "action": "update_version", "file_id": "1", "content_sha256": "old"}],
            }
            newer = {
                "generated_at": "2026-08-02T01:00:00", "dry_run": False,
                "results": [{"relative_path": "daily/a.md", "action": "update_version", "file_id": "1", "content_sha256": "new"}],
            }
            dry_run = {
                "generated_at": "2026-08-03T01:00:00", "dry_run": True,
                "results": [{"relative_path": "daily/a.md", "action": "update_version", "file_id": "1", "content_sha256": "fake"}],
            }
            for name, payload in (("docdb-z.json", older), ("docdb-a.json", newer), ("docdb-dry.json", dry_run)):
                root.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")
            state = cwk_sync_mirror_to_docdb.bootstrap_sync_state(root / "state.json", root)
            self.assertEqual(state["objects"]["daily/a.md"]["content_sha256"], "new")

    def test_docdb_retry_partition_removes_only_missing_paths_in_scope(self):
        active, stale = cwk_sync_mirror_to_docdb.partition_retry_paths(
            {"wiki/a.md", "wiki/gone.md", "daily/keep.md"}, "wiki/", {"wiki/a.md"},
        )
        self.assertEqual(active, {"wiki/a.md"})
        self.assertEqual(stale, {"wiki/gone.md"})

    def test_docdb_commit_pointer_is_partitioned_last(self):
        root = Path("/tmp")
        data = cwk_sync_mirror_to_docdb.SyncItem(root / "a", Path("wiki/a.md"), "f", "a.md", "f")
        pointer = cwk_sync_mirror_to_docdb.SyncItem(root / "m", Path("wiki/_system/manifest.json"), "f", "m.json", "f")
        regular, commits = cwk_sync_mirror_to_docdb.partition_commit_items([pointer, data])
        self.assertEqual([item.rel.as_posix() for item in regular], ["wiki/a.md"])
        self.assertEqual([item.rel.as_posix() for item in commits], ["wiki/_system/manifest.json"])

    def test_docdb_commit_pointer_rechecks_exact_remote_path_instead_of_cached_id(self):
        item = cwk_sync_mirror_to_docdb.SyncItem(
            Path("/tmp/manifest.json"), Path("wiki/_system/manifest.json"),
            "工作协同镜像/wiki/_system", "cwk-wiki-manifest-v2.json",
            "工作协同镜像/wiki/_system",
        )
        with patch.object(cwk_sync_mirror_to_docdb, "find_existing", return_value={"id": "live-id"}) as search:
            existing = cwk_sync_mirror_to_docdb.resolve_existing_for_publish(
                item, {"file_id": "stale-id"}, "project", "root", {}, False,
            )
        self.assertEqual(existing, {"id": "live-id"})
        search.assert_called_once()

    def test_docdb_text_upload_retries_transient_service_busy(self):
        module = Mock()
        module.call_api.side_effect = [
            {"resultCode": 0, "resultMsg": "服务器繁忙，请稍后再试！"},
            {"resultCode": 1, "resultMsg": None, "data": {"fileId": "ok"}},
        ]
        module.process_result.side_effect = lambda payload: payload
        with (
            patch.object(cwk_sync_mirror_to_docdb, "_UPLOAD_CONTENT_MODULE", module),
            patch.object(cwk_sync_mirror_to_docdb.time, "sleep") as sleep,
        ):
            result = cwk_sync_mirror_to_docdb.upload_text_api(content="x")
        self.assertEqual(result["resultCode"], 1)
        self.assertEqual(module.call_api.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_docdb_stale_id_heal_name_is_stable_and_path_specific(self):
        root = Path("/tmp")
        first = cwk_sync_mirror_to_docdb.SyncItem(root / "a", Path("events/a.md"), "f", "a.md", "f")
        same = cwk_sync_mirror_to_docdb.SyncItem(root / "b", Path("events/a.md"), "f", "a.md", "f")
        other = cwk_sync_mirror_to_docdb.SyncItem(root / "c", Path("daily/a.md"), "f", "a.md", "f")
        self.assertEqual(
            cwk_sync_mirror_to_docdb.healed_cloud_name(first),
            cwk_sync_mirror_to_docdb.healed_cloud_name(same),
        )
        self.assertNotEqual(
            cwk_sync_mirror_to_docdb.healed_cloud_name(first),
            cwk_sync_mirror_to_docdb.healed_cloud_name(other),
        )
        self.assertRegex(cwk_sync_mirror_to_docdb.healed_cloud_name(first), r"^a\.cwk-heal-[0-9a-f]{10}\.md$")

    def test_docdb_publish_recovers_from_unreadable_cached_file_id(self):
        item = cwk_sync_mirror_to_docdb.SyncItem(
            Path("/tmp/a.md"), Path("events/a.md"), "工作协同镜像/events", "a.md", "工作协同镜像/events",
        )
        healed_result = {
            "relative_path": "events/a.md", "action": "create", "file_id": "new-id",
            "folder_name": "工作协同镜像/events", "content_sha256": "abc",
        }
        with (
            patch.object(cwk_sync_mirror_to_docdb, "upload_or_update", side_effect=[RuntimeError("文件信息查询失败"), healed_result]) as upload,
            patch.object(cwk_sync_mirror_to_docdb, "find_existing", return_value=None) as search,
        ):
            result = cwk_sync_mirror_to_docdb.publish_with_stale_id_recovery(
                item, {"id": "stale-id"}, "project", "root", {}, False, False, [], None,
            )
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(search.call_count, 1)
        self.assertTrue(result["self_healed"])
        self.assertEqual(result["stale_file_id"], "stale-id")
        self.assertRegex(result["file_name"], r"^a\.cwk-heal-[0-9a-f]{10}\.md$")

    def test_nightly_sync_manifest_contains_only_current_mirror_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mirror = root / "mirror"
            current = mirror / "daily" / "2026-08" / "2026-08-08.md"
            current.parent.mkdir(parents=True)
            current.write_text("today", encoding="utf-8")
            output = root / "changed.json"
            cwk_nightly_pipeline.write_mirror_outputs_manifest(
                output, mirror, {"daily_md": str(current), "outside": str(root / "outside.md")},
            )
            self.assertEqual(
                json.loads(output.read_text())["changed_relative_paths"],
                ["daily/2026-08/2026-08-08.md"],
            )


if __name__ == "__main__":
    unittest.main()
