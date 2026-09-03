import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_backfill_range  # noqa: E402
import cwk_raw_store  # noqa: E402
import cwk_source_coverage_audit  # noqa: E402
import cwk_thread_timeline  # noqa: E402


def staged_report(report_id: str, report_time: int = 1785823836000) -> str:
    return f'''---
report_id: "{report_id}"
title: "测试汇报"
writer: "测试人"
create_time: ""
---

# 测试汇报

<meta>
- **时间**: 2026-08-04 13:57:34
</meta>

## List Row Metadata

```json
{{"id":"{report_id}","reportTime":{report_time},"main":"测试汇报"}}
```
'''


def threaded_staged_report(report_id: str, reply_id: str = "reply-1", reply_text: str = "同意按方案执行") -> str:
    return staged_report(report_id) + f'''\n## Record Simple Info

```json
{{
  "reportRecordId": "{report_id}",
  "replyList": [{{
    "id": "{reply_id}",
    "name": "审批人甲",
    "content": "{reply_text}",
    "createTime": "2026-08-04 14:00:00"
  }}]
}}
```

## Node / Opinion Chain

```json
{{
  "nodeList": [{{
    "nodeName": "负责人审批",
    "type": "审批",
    "status": "已完成",
    "level": 1,
    "userList": [{{
      "id": "node-user-1",
      "name": "审批人甲",
      "status": "已同意",
      "content": "{reply_text}",
      "finishTime": "2026-08-04 14:01:00"
    }}]
  }}]
}}
```
'''


class RawPromotionTests(unittest.TestCase):
    def test_date_backfill_preserves_cloud_first_manifest_semantics_when_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mirror = root / "mirror"
            promotion = {"counts": {"created": 0}, "changed_relative_paths": []}
            with (
                patch.object(cwk_backfill_range, "PROJECT", root),
                patch.object(cwk_backfill_range, "source_rows", return_value=([], 0)),
                patch.object(cwk_backfill_range, "raw_index", return_value={}),
                patch.object(cwk_backfill_range, "promote", return_value=promotion) as promote_mock,
            ):
                result = cwk_backfill_range.run_backfill(
                    app_key="key",
                    start_date="2026-08-01",
                    end_date="2026-08-01",
                    run_name="test-cloud-first",
                    mirror_root=mirror,
                    max_parallel=1,
                    page_size=100,
                    cloud_first=True,
                )
            self.assertEqual(result["remaining_missing"], 0)
            promote_mock.assert_called_once_with(
                [root / "runs" / "test-cloud-first" / "collected-raw"],
                mirror,
                cloud_first=True,
            )

    def test_business_date_prefers_report_time_and_promotes_by_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "1-test.md").write_text(staged_report("1"), encoding="utf-8")
            mirror = root / "mirror"
            result = cwk_raw_store.promote([staging], mirror)
            expected = mirror / "raw" / "2026-08" / "2026-08-04" / "1-test.md"
            self.assertTrue(expected.exists())
            self.assertEqual(result["counts"]["created"], 1)
            manifest = json.loads((mirror / "raw" / "_system" / "raw-manifest.json").read_text())
            self.assertEqual(manifest["record_count"], 1)

    def test_promotion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "1-test.md").write_text(staged_report("1"), encoding="utf-8")
            mirror = root / "mirror"
            cwk_raw_store.promote([staging], mirror)
            second = cwk_raw_store.promote([staging], mirror)
            self.assertEqual(second["counts"]["unchanged"], 1)
            self.assertEqual(len(cwk_raw_store.raw_index(mirror / "raw")), 1)

    def test_timeline_preserves_reply_and_approval_history_across_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "1-thread.md"
            mirror = root / "mirror"
            source.write_text(threaded_staged_report("1"), encoding="utf-8")

            first = cwk_raw_store.promote([staging], mirror)
            timeline = mirror / "raw" / "_system" / "timelines" / "1"
            self.assertEqual(first["thread_timeline"]["snapshots_created"], 1)
            self.assertEqual(first["thread_timeline"]["events_created"], 2)
            self.assertTrue(any(path.startswith("raw/_system/timelines/1/") for path in first["changed_relative_paths"]))
            self.assertEqual(len(list((timeline / "snapshots").glob("*.md"))), 1)
            self.assertEqual(len(list((timeline / "events").glob("*.json"))), 2)

            repeat = cwk_raw_store.promote([staging], mirror)
            self.assertEqual(repeat["counts"]["unchanged"], 1)
            self.assertEqual(repeat["thread_timeline"]["snapshots_created"], 0)
            self.assertEqual(repeat["thread_timeline"]["events_created"], 0)
            self.assertEqual(len(list((timeline / "snapshots").glob("*.md"))), 1)
            self.assertEqual(len(list((timeline / "events").glob("*.json"))), 2)

            source.write_text(threaded_staged_report("1", "reply-2", "不同意，需补充预算"), encoding="utf-8")
            updated = cwk_raw_store.promote([staging], mirror)
            self.assertEqual(updated["counts"]["updated"], 1)
            self.assertEqual(len(list((timeline / "snapshots").glob("*.md"))), 2)
            self.assertEqual(len(list((timeline / "events").glob("*.json"))), 4)

            raw_path = cwk_raw_store.raw_index(mirror / "raw")["1"]
            complete = cwk_thread_timeline.audit(mirror, [raw_path])
            self.assertTrue(complete["complete"])

            current_events = cwk_thread_timeline.events_from_raw("1", raw_path.read_text(encoding="utf-8"))
            (timeline / "events" / f"{current_events[0]['event_id']}.json").unlink()
            incomplete = cwk_thread_timeline.audit(mirror, [raw_path])
            self.assertFalse(incomplete["complete"])
            self.assertEqual(incomplete["missing_event_count"], 1)

    def test_source_pagination_requires_exact_total(self):
        pages = {
            "1": {"success": True, "data": {"list": [{"id": "1"}, {"id": "2"}], "total": 3}},
            "2": {"success": True, "data": {"list": [{"id": "3"}], "total": 3}},
        }

        def fake_run(_script, args, _key):
            return pages[args[args.index("--page-index") + 1]]

        with patch.object(cwk_backfill_range, "run_tool", side_effect=fake_run):
            rows, total = cwk_backfill_range.search_list_source_rows("key", "2026-08-01", "2026-08-04", page_size=2)
        self.assertEqual(total, 3)
        self.assertEqual({row["id"] for row in rows}, {"1", "2", "3"})

    def test_coverage_requires_raw_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "mirror"
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "1-test.md").write_text(staged_report("1"), encoding="utf-8")
            cwk_raw_store.promote([staging], mirror)
            (mirror / "wiki" / "summaries").mkdir(parents=True)
            rows = [{"id": "1", "reportTime": 1785823836000}, {"id": "2", "reportTime": 1785823836000}]
            with patch.object(cwk_source_coverage_audit, "source_rows", return_value=(rows, 2)):
                result = cwk_source_coverage_audit.audit("key", "2026-08-04", "2026-08-04", mirror)
            self.assertEqual(result["raw_covered"], 1)
            self.assertEqual(result["summary_covered"], 0)
            self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
