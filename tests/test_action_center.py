import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_action_center  # noqa: E402


def raw_record(
    report_id: str,
    *,
    scopes: str = "inbox",
    lane: str = "inbox_awareness",
    change_type: str = "new",
    collection_mode: str = "live-incremental",
    node: dict | None = None,
    simple: dict | None = None,
    row: dict | None = None,
    create_time: str = "2026-07-18 09:00:00",
) -> str:
    return f'''---
report_id: "{report_id}"
title: "事项 {report_id}"
writer: "甲"
create_time: "{create_time}"
source_lane: {lane}
collection_mode: {collection_mode}
change_type: {change_type}
source_scopes: "{scopes}"
---

# 事项 {report_id}

## Original Full Content For AI

请处理该事项。

## List Row Metadata

```json
{json.dumps(row or {}, ensure_ascii=False)}
```

## Record Simple Info

```json
{json.dumps(simple or {}, ensure_ascii=False)}
```

## Node / Opinion Chain

```json
{json.dumps(node or {}, ensure_ascii=False)}
```
'''


class ActionCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.run = self.project / "runs" / "sample"
        (self.run / "raw").mkdir(parents=True)
        (self.run / "extracted").mkdir()
        (self.run / "ai-understanding").mkdir()
        self.project_patch = patch("cwk_action_center.PROJECT", self.project)
        self.project_patch.start()

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def add(self, report_id: str, **kwargs):
        (self.run / "raw" / f"{report_id}-item.md").write_text(raw_record(report_id, **kwargs), encoding="utf-8")

    def relations(self, *items: dict) -> Path:
        path = self.run / "report-relationships.json"
        path.write_text(
            json.dumps({"schema_version": "cwk.report_relationships.v1", "provider_status": "ok", "items": list(items)}),
            encoding="utf-8",
        )
        return path

    def test_decision_todo_has_decision_preview_actions(self):
        self.add("1", scopes="todo_pending,inbox", lane="todo_backed", node={"role": "决策人"})
        relation = self.relations({"reportId": "1", "status": "resolved", "visibility": "related", "primaryRole": "decision_maker", "roles": ["decision_maker"], "actionRequired": True, "pendingActions": ["approve", "reject"]})
        card = cwk_action_center.build_cards(self.run, relationship_manifest=relation)["cards"][0]
        self.assertEqual(card["primary_type"], "decision_todo")
        self.assertEqual(card["role"], "decision_maker")
        self.assertTrue(card["mandatory"])
        self.assertIn("approve", [item["code"] for item in card["allowed_actions"]])

    def test_advice_todo_has_advice_action(self):
        self.add("2", scopes="todo_pending", lane="todo_backed", node={"role": "建议人"})
        relation = self.relations({"reportId": "2", "status": "resolved", "visibility": "related", "primaryRole": "advisor", "roles": ["advisor"], "actionRequired": True, "pendingActions": ["submit_advice"]})
        card = cwk_action_center.build_cards(self.run, relationship_manifest=relation)["cards"][0]
        self.assertEqual(card["primary_type"], "advice_todo")
        self.assertEqual(card["role"], "advisor")
        self.assertIn("submit_advice", [item["code"] for item in card["allowed_actions"]])

    def test_updated_todo_is_reactivated_and_keeps_role(self):
        self.add("3", scopes="todo_pending,unread", lane="todo_backed", change_type="updated", node={"role": "决策人"})
        relation = self.relations({"reportId": "3", "status": "resolved", "visibility": "related", "primaryRole": "decision_maker", "roles": ["decision_maker"], "actionRequired": True, "pendingActions": ["approve"]})
        card = cwk_action_center.build_cards(self.run, relationship_manifest=relation)["cards"][0]
        self.assertEqual(card["primary_type"], "reactivated_todo")
        self.assertEqual(card["underlying_role"], "decision_maker")
        self.assertTrue(card["mandatory"])
        self.assertEqual(set(card["views"]), {"today", "recent_changes"})

    def test_inbox_and_update_are_optional(self):
        self.add("4", scopes="inbox", lane="inbox_awareness")
        self.add("5", scopes="inbox,unread", lane="reply_chain", change_type="updated", row={"hasNewReply": True})
        cards = {card["report_id"]: card for card in cwk_action_center.build_cards(self.run)["cards"]}
        self.assertEqual(cards["4"]["primary_type"], "inbox_awareness")
        self.assertEqual(cards["5"]["primary_type"], "update_notice")
        self.assertFalse(cards["4"]["mandatory"])
        self.assertNotIn("express_opinion", [item["code"] for item in cards["5"]["allowed_actions"]])
        self.assertIn("open_source", [item["code"] for item in cards["5"]["allowed_actions"]])

    def test_reply_chain_without_current_change_is_not_reactivated(self):
        self.add("5b", scopes="inbox,unread", lane="reply_chain", change_type="continuation", row={"hasNewReply": True})
        card = cwk_action_center.build_cards(self.run, "2026-07-18")["cards"][0]
        self.assertEqual(card["primary_type"], "inbox_awareness")
        self.assertFalse(card["has_current_change"])
        self.assertEqual(card["views"], ["ongoing"])

    def test_generic_pending_scope_is_not_a_structured_todo(self):
        self.add("5p", scopes="inbox,pending,unread", lane="reply_chain", change_type="continuation")
        card = cwk_action_center.build_cards(self.run, "2026-07-18")["cards"][0]
        self.assertFalse(card["mandatory"])
        self.assertEqual(card["views"], ["ongoing"])

    def test_current_date_backfill_is_today_new_not_historical(self):
        self.add(
            "5c",
            scopes="date_range_search",
            lane="inbox_awareness",
            change_type="historical_backfill",
            collection_mode="historical-backfill",
            create_time="2026-08-11 14:30:00",
        )
        payload = cwk_action_center.build_cards(self.run, "2026-08-11")
        card = payload["cards"][0]
        self.assertEqual(card["effective_change_type"], "new")
        self.assertEqual(card["primary_type"], "inbox_awareness")
        self.assertEqual(card["views"], ["today"])
        self.assertEqual(payload["view_counts"]["today"], 1)

    def test_historical_never_becomes_mandatory(self):
        self.add("6", scopes="history_todo_pending", lane="todo_backed", change_type="historical_backfill", collection_mode="historical-backfill", node={"role": "决策人"})
        card = cwk_action_center.build_cards(self.run)["cards"][0]
        self.assertEqual(card["primary_type"], "historical")
        self.assertFalse(card["mandatory"])

    def test_unknown_todo_role_never_shows_approval(self):
        self.add("7", scopes="todo_pending", lane="todo_backed")
        card = cwk_action_center.build_cards(self.run)["cards"][0]
        actions = [item["code"] for item in card["allowed_actions"]]
        self.assertTrue(card["requires_role_review"])
        self.assertNotIn("approve", actions)
        self.assertNotIn("submit_advice", actions)
        self.assertEqual(card["primary_type"], "action_todo")

    def test_complete_people_list_without_owner_does_not_make_negative_claim(self):
        self.add(
            "7v",
            scopes="inbox,unread",
            simple={"writeEmpId": "writer-1", "writeEmpName": "甲"},
            node={
                "writeEmpId": "writer-1",
                "writeEmpName": "甲",
                "nodeList": [{"nodeName": "接收人", "type": "传阅", "userList": [{"empId": "other-1", "name": "乙"}]}],
            },
        )
        card = cwk_action_center.build_cards(self.run, owner_emp_id="owner-1", owner_name="本人")["cards"][0]
        self.assertFalse(card["visible_only"])
        self.assertEqual(card["relationship_status"], "unknown")
        self.assertEqual(card["role"], "unknown")
        self.assertFalse(card["mandatory"])
        actions = [item["code"] for item in card["allowed_actions"]]
        self.assertNotIn("express_opinion", actions)
        self.assertIn("open_source", actions)

    def test_owner_role_comes_from_backend_not_people_list(self):
        self.add(
            "7r",
            scopes="inbox,todo_pending",
            lane="todo_backed",
            simple={"writeEmpId": "writer-1", "writeEmpName": "甲"},
            node={
                "writeEmpId": "writer-1",
                "writeEmpName": "甲",
                "nodeList": [
                    {"nodeName": "决策人", "type": "决策", "userList": [{"empId": "other-1", "name": "乙"}]},
                    {"nodeName": "建议人", "type": "建议", "userList": [{"empId": "owner-1", "name": "本人"}]},
                ],
            },
        )
        relation = self.relations({"reportId": "7r", "status": "resolved", "visibility": "related", "primaryRole": "advisor", "roles": ["recipient", "advisor"], "actionRequired": True, "pendingActions": ["submit_advice"]})
        card = cwk_action_center.build_cards(self.run, owner_emp_id="owner-1", owner_name="本人", relationship_manifest=relation)["cards"][0]
        self.assertFalse(card["visible_only"])
        self.assertEqual(card["relationship_role"], "advisor")
        self.assertEqual(card["role"], "advisor")
        self.assertTrue(card["mandatory"])
        self.assertIn("submit_advice", [item["code"] for item in card["allowed_actions"]])

    def test_incomplete_node_data_does_not_make_negative_relation_claim(self):
        self.add("7u", scopes="inbox", simple={"writeEmpId": "writer-1"}, node={})
        card = cwk_action_center.build_cards(self.run, owner_emp_id="owner-1")["cards"][0]
        self.assertEqual(card["relationship_status"], "unknown")
        self.assertFalse(card["visible_only"])

    def test_same_report_is_rendered_once_with_highest_responsibility(self):
        self.add("8", scopes="inbox", lane="inbox_awareness")
        (self.run / "raw" / "8-todo.md").write_text(raw_record("8", scopes="todo_pending", lane="todo_backed", node={"role": "建议人"}), encoding="utf-8")
        cards = cwk_action_center.build_cards(self.run)["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["primary_type"], "action_todo")
        self.assertIn("inbox", cards[0]["source_labels"])

    def test_html_is_interactive_but_has_no_write_transport(self):
        self.add("9", scopes="inbox", lane="inbox_awareness")
        payload = cwk_action_center.build_cards(self.run)
        page = cwk_action_center.render_html(payload)
        cwk_action_center.assert_shadow_safe(page)
        self.assertIn("Shadow Mode", page)
        self.assertIn("不会提交到 CWork", page)
        self.assertIn("textarea", page)
        self.assertIn("今日处理 1", page)
        self.assertIn("近期变更 0", page)
        self.assertIn("持续未闭环", page)
        self.assertIn("项仅权限可见", page)
        self.assertNotIn("fetch(", page)
        self.assertNotIn("XMLHttpRequest", page)
        self.assertIn("textContent+='\\n\\n已确认预览", page)
        self.assertNotIn("textContent+='\n\n已确认预览", page)
        if shutil.which("node"):
            scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", page, re.S)
            javascript = "\n".join(script for script in scripts if not script.lstrip().startswith("{"))
            checked = subprocess.run(
                ["node", "--check"], input=javascript, text=True, capture_output=True, check=False
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_cwork_key_like_text_is_preserved_in_all_action_center_views(self):
        source_value = "example-app-key-value"
        self.add("10", scopes="inbox", lane="inbox_awareness")
        (self.run / "ai-understanding" / "10.json").write_text(
            json.dumps({"summary": f"工作协同原文中的 AppKey：{source_value}"}, ensure_ascii=False),
            encoding="utf-8",
        )
        payload = cwk_action_center.build_cards(self.run)
        outputs = [
            json.dumps(payload, ensure_ascii=False),
            cwk_action_center.render_markdown(payload),
            cwk_action_center.render_html(payload),
        ]
        for output in outputs:
            self.assertIn(source_value, output)
            self.assertNotIn("<redacted>", output)


if __name__ == "__main__":
    unittest.main()
