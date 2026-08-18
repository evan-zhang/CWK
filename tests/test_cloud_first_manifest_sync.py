import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cwk_sync_mirror_to_docdb
import cwk_cloud_coverage_audit
from cwk_cloud_objects import merge
from cwk_wiki_manifest_reconcile import reconcile_manifest


class CloudFirstManifestTests(unittest.TestCase):
    def test_raw_cloud_publish_requires_second_explicit_opt_in(self):
        with self.assertRaisesRegex(SystemExit, "raw cloud publishing is paused"):
            cwk_sync_mirror_to_docdb.enforce_raw_cloud_pause(
                allow_raw=True,
                experimental_cloud_raw=False,
            )
        cwk_sync_mirror_to_docdb.enforce_raw_cloud_pause(
            allow_raw=True,
            experimental_cloud_raw=True,
        )

    def test_catalog_merge_rejects_empty_local_mirror_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = root / "mirror"
            system = mirror / "wiki" / "_system"
            (mirror / "raw").mkdir(parents=True)
            system.mkdir(parents=True)
            system.joinpath("cloud-objects.json").write_text(
                json.dumps(
                    {
                        "objects": {
                            "raw/2026-08/2026-08-04/1.md": {
                                "file_id": "1", "content_sha256": "a" * 64,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"results": []}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "destructive cloud catalog prune"):
                merge(mirror, [receipt])

    def test_live_coverage_records_private_target_and_blocks_retry_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = root / "mirror"
            raw = mirror / "raw" / "a.md"
            wiki = mirror / "wiki" / "b.md"
            system = mirror / "wiki" / "_system"
            raw.parent.mkdir(parents=True)
            wiki.parent.mkdir(parents=True)
            system.mkdir(parents=True)
            raw.write_text("raw", encoding="utf-8")
            wiki.write_text("wiki", encoding="utf-8")
            import hashlib
            blobs = {"raw-id": raw.read_bytes(), "wiki-id": wiki.read_bytes()}
            catalog = {
                "project_id": "personal", "root_file_id": "root",
                "objects": {
                    "raw/a.md": {"file_id": "raw-id", "content_sha256": hashlib.sha256(blobs["raw-id"]).hexdigest()},
                    "wiki/b.md": {"file_id": "wiki-id", "content_sha256": hashlib.sha256(blobs["wiki-id"]).hexdigest()},
                },
            }
            system.joinpath("cloud-objects.json").write_text(json.dumps(catalog), encoding="utf-8")
            retry = root / "retry.json"
            retry.write_text(json.dumps({"failed_relative_paths": ["wiki/b.md"]}), encoding="utf-8")

            class FakeRepo:
                def __init__(self, **kwargs):
                    self.project_id = kwargs["project_id"]
                    self.root_file_id = kwargs["root_file_id"]
                    self.env = {}

                def download_object(self, row, target, expected_sha, force=False):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blobs[row["file_id"]])
                    return target

                def list_tree(self, **kwargs):
                    return [
                        {"relative_path": "raw/a.md", "file_id": "raw-id"},
                        {"relative_path": "wiki/b.md", "file_id": "wiki-id"},
                        {"relative_path": "wiki/_system/cwk-cloud-objects.json", "file_id": "catalog-id"},
                    ]

            with patch.object(cwk_cloud_coverage_audit, "DocDBCloudRepository", FakeRepo), \
                 patch.object(cwk_cloud_coverage_audit, "get_personal_project_id", return_value="personal"):
                blocked = cwk_cloud_coverage_audit.audit(
                    mirror, prefixes=("raw/", "wiki/"), live=True,
                    sender_id="", account_id="default", live_workers=1, retry_queue=retry,
                )
                self.assertFalse(blocked["overall_pass"])
                self.assertTrue(blocked["private_target_verified"])
                self.assertEqual(blocked["retry_queue_pending"], ["wiki/b.md"])
                retry.write_text(json.dumps({"failed_relative_paths": []}), encoding="utf-8")
                passed = cwk_cloud_coverage_audit.audit(
                    mirror, prefixes=("raw/", "wiki/"), live=True,
                    sender_id="", account_id="default", live_workers=1, retry_queue=retry,
                )
                self.assertTrue(passed["overall_pass"])
                self.assertEqual(passed["project_id"], "personal")
                self.assertEqual(passed["root_file_id"], "root")

    def test_non_live_coverage_never_passes_hard_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            (mirror / "wiki" / "_system").mkdir(parents=True)
            (mirror / "wiki" / "_system" / "cloud-objects.json").write_text(
                json.dumps({"objects": {}}), encoding="utf-8"
            )
            result = cwk_cloud_coverage_audit.audit(
                mirror, prefixes=("wiki/",), live=False,
                sender_id="", account_id="default", live_workers=1,
                retry_queue=mirror / "retry.json",
            )
            self.assertFalse(result["overall_pass"])

    def test_reconcile_preserves_quality_and_repairs_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            raw = mirror / "raw" / "2026-08" / "2026-08-04"
            raw.mkdir(parents=True)
            source = raw / "123456789012345-测试.md"
            source.write_text(
                '---\nreport_id: "123456789012345"\nsource_lane: inbox\n---\n正文\n', encoding="utf-8"
            )
            system = mirror / "raw" / "_system"
            system.mkdir()
            system.joinpath("raw-manifest.json").write_text(
                json.dumps({"records": [{"report_id": "123456789012345", "sha256": "stale", "canonical_path": source.relative_to(mirror).as_posix()}]}),
                encoding="utf-8",
            )
            summaries = mirror / "wiki" / "summaries"
            summaries.mkdir(parents=True)
            summaries.joinpath("123456789012345.md").write_text("# 已精编\n", encoding="utf-8")
            before = {
                "source_count": 0,
                "compiled_report_ids": [],
                "ai_refined_report_ids": [],
                "fallback_report_ids": ["stale"],
                "withheld_report_ids": ["stale"],
                "failure_queue": [{"report_id": "stale", "attempts": 2}],
                "index_version": 7,
            }
            after = reconcile_manifest(mirror, before)
            self.assertEqual(after["source_count"], 1)
            self.assertEqual(len(after["source_hashes"]), 1)
            self.assertEqual(after["compiled_report_ids"], ["123456789012345"])
            self.assertEqual(after["ai_refined_report_ids"], ["123456789012345"])
            self.assertEqual(after["fallback_report_ids"], [])
            self.assertEqual(after["failure_queue"], [])
            self.assertEqual(after["index_version"], 7)

    def test_sync_denies_raw_unless_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            (mirror / "raw").mkdir()
            (mirror / "wiki").mkdir()
            (mirror / "raw" / "a.md").write_text("raw", encoding="utf-8")
            (mirror / "wiki" / "b.md").write_text("wiki", encoding="utf-8")
            with patch.object(cwk_sync_mirror_to_docdb, "MIRROR", mirror):
                default_items = cwk_sync_mirror_to_docdb.iter_items(None, None)
                allowed_items = cwk_sync_mirror_to_docdb.iter_items(None, "raw/", allow_raw=True)
            self.assertEqual([item.rel.as_posix() for item in default_items], ["wiki/b.md"])
            self.assertEqual([item.rel.as_posix() for item in allowed_items], ["raw/a.md"])

    def test_legacy_withheld_state_is_removed_by_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            raw = mirror / "raw" / "2026-08" / "2026-08-04"
            summaries = mirror / "wiki" / "summaries"
            raw.mkdir(parents=True)
            summaries.mkdir(parents=True)
            rid = "123456789012345"
            raw.joinpath(f"{rid}-技术.md").write_text(
                f'---\nreport_id: "{rid}"\n---\n技术正文\n', encoding="utf-8"
            )
            summaries.joinpath(f"{rid}.md").write_text("# 已生成\n", encoding="utf-8")
            after = reconcile_manifest(
                mirror,
                {
                    "compiled_report_ids": [rid],
                    "fallback_report_ids": [],
                    "ai_refined_report_ids": [rid],
                    "withheld_report_ids": [rid],
                },
            )
            self.assertNotIn("withheld_report_ids", after)
            self.assertEqual(after["ai_refined_report_ids"], [rid])

    def test_raw_physical_create_is_not_marked_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "raw" / "x.md"
            path.parent.mkdir(parents=True)
            path.write_text("raw", encoding="utf-8")
            item = cwk_sync_mirror_to_docdb.SyncItem(
                path=path, rel=Path("raw/x.md"), folder_name="工作协同镜像/raw",
                file_name="x.md", expected_ancestor="工作协同镜像/raw",
            )
            with patch.object(cwk_sync_mirror_to_docdb, "upload_resource", return_value="resource"), \
                 patch.object(cwk_sync_mirror_to_docdb, "run_json", return_value={"resultCode": 1, "data": {"fileId": "9"}}) as run:
                result = cwk_sync_mirror_to_docdb.physical_save_or_update(item, None, "1", {})
            self.assertEqual(result["action"], "physical_create")
            self.assertIn("--is-sensitive", run.call_args.args[0])
            marker = run.call_args.args[0].index("--is-sensitive")
            self.assertEqual(run.call_args.args[0][marker + 1], "0")


if __name__ == "__main__":
    unittest.main()
