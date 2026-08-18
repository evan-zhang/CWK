import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_daily_html  # noqa: E402
import cwk_human_digest  # noqa: E402


def raw(
    report_id: str,
    title: str,
    create_time: str,
    change_type: str,
    collection_mode: str = "live-incremental",
    simple: dict | None = None,
    node: dict | None = None,
) -> str:
    return f'''---
report_id: "{report_id}"
title: "{title}"
writer: "甲"
create_time: "{create_time}"
source_lane: inbox_awareness
collection_mode: {collection_mode}
change_type: {change_type}
source_scopes: "inbox"
---

# {title}

## Record Simple Info

```json
{json.dumps(simple or {}, ensure_ascii=False)}
```

## Node / Opinion Chain

```json
{json.dumps(node or {}, ensure_ascii=False)}
```
'''


def extracted(report_id: str, title: str, change_type: str, attention: str, collection_mode: str = "live-incremental") -> dict:
    return {
        "source_ids": [report_id],
        "title": title,
        "change_type": change_type,
        "collection_mode": collection_mode,
        "attention_type": attention,
        "event_anchor": title,
        "event_family": "general",
        "item_nature": "general",
        "risks": [],
        "open_loops": [],
        "decision_points": [],
        "actions": [],
    }


class DailyViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.run = self.project / "runs" / "nightly-test"
        (self.run / "raw").mkdir(parents=True)
        (self.run / "extracted").mkdir()
        (self.run / "events").mkdir()
        self.project_patch = patch("cwk_human_digest.PROJECT", self.project)
        self.project_patch.start()

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def add(self, report_id: str, title: str, create_time: str, change_type: str, attention: str, collection_mode: str = "live-incremental"):
        (self.run / "raw" / f"{report_id}.md").write_text(
            raw(report_id, title, create_time, change_type, collection_mode), encoding="utf-8"
        )
        (self.run / "extracted" / f"{report_id}.json").write_text(
            json.dumps(extracted(report_id, title, change_type, attention, collection_mode), ensure_ascii=False), encoding="utf-8"
        )

    def test_digest_separates_today_changes_and_ongoing(self):
        self.add("1", "需要我处理的待办", "2026-08-01 09:00:00", "continuation", "requires_action")
        self.add("2", "当天补采的新汇报", "2026-08-11 10:00:00", "historical_backfill", "awareness_only", "historical-backfill")
        self.add("3", "历史汇报今天有变化", "2026-07-20 10:00:00", "updated", "optional_review")
        self.add("4", "普通持续事项", "2026-07-18 10:00:00", "continuation", "awareness_only")
        (self.run / "run.json").write_text(json.dumps({"processed_count": 4}), encoding="utf-8")

        markdown = cwk_human_digest.render(self.run, "2026-08-11")
        today = markdown.split("## 今日处理", 1)[1].split("## 近期变更", 1)[0]
        changes = markdown.split("## 近期变更", 1)[1].split("## 持续未闭环", 1)[0]
        ongoing = markdown.split("## 持续未闭环", 1)[1].split("## 本版质量边界", 1)[0]
        self.assertLess(today.index("需要我处理的待办"), today.index("当天补采的新汇报"))
        self.assertIn("历史汇报今天有变化", changes)
        self.assertNotIn("普通持续事项", changes)
        self.assertIn("普通持续事项", ongoing)

    def test_daily_html_builds_two_tabs_and_folded_ongoing(self):
        markdown = """# 工作协同每日简报\n\n## 今日处理\n\n- 今日事项\n\n## 近期变更\n\n- 变更事项\n\n## 持续未闭环\n\n- 延续事项\n\n## 质量边界\n\n- 只读\n"""
        page = cwk_daily_html.render_html(markdown, "daily.md")
        self.assertIn('data-daily-tab="today"', page)
        self.assertIn('data-daily-tab="recent_changes"', page)
        self.assertIn("持续未闭环 1", page)
        self.assertIn("<details>", page)

    def test_digest_marks_visible_only_only_from_backend_manifest(self):
        report_id = "5"
        title = "仅权限可见事项"
        (self.run / "raw" / f"{report_id}.md").write_text(
            raw(
                report_id,
                title,
                "2026-08-11 12:00:00",
                "new",
                simple={"writeEmpId": "writer-1", "writeEmpName": "甲"},
                node={
                    "writeEmpId": "writer-1",
                    "writeEmpName": "甲",
                    "nodeList": [{"nodeName": "接收人", "type": "传阅", "userList": [{"empId": "other-1", "name": "乙"}]}],
                },
            ),
            encoding="utf-8",
        )
        (self.run / "extracted" / f"{report_id}.json").write_text(
            json.dumps(extracted(report_id, title, "new", "awareness_only"), ensure_ascii=False),
            encoding="utf-8",
        )
        relationship_manifest = self.run / "report-relationships.json"
        relationship_manifest.write_text(
            json.dumps({
                "schema_version": "cwk.report_relationships.v1",
                "provider_status": "ok",
                "items": [{"reportId": report_id, "status": "resolved", "visibility": "visible_only", "primaryRole": "observer", "roles": [], "actionRequired": False, "pendingActions": [], "reasonCode": "PERMISSION_ONLY"}],
            }),
            encoding="utf-8",
        )
        markdown = cwk_human_digest.render(self.run, "2026-08-11", "owner-1", "本人", relationship_manifest)
        self.assertIn("仅权限可见 1 条", markdown)
        self.assertIn("【仅权限可见 · 与我无关】", markdown)

    def test_digest_does_not_infer_visible_only_from_people_lists(self):
        self.add("6", "本地人员清单不完整", "2026-08-11 12:00:00", "new", "awareness_only")
        markdown = cwk_human_digest.render(self.run, "2026-08-11", "owner-1", "本人")
        self.assertIn("仅权限可见 0 条", markdown)
        self.assertIn("关系待确认 1 条", markdown)


if __name__ == "__main__":
    unittest.main()
