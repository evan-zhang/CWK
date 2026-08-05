import json
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
    row: dict | None = None,
) -> str:
    return f'''---
report_id: "{report_id}"
title: "事项 {report_id}"
writer: "甲"
create_time: "2026-07-18 09:00:00"
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

    def test_decision_todo_has_decision_preview_actions(self):
        self.add("1", scopes="todo_pending,inbox", lane="todo_backed", node={"role": "决策人"})
        card = cwk_action_center.build_cards(self.run)["cards"][0]
        self.assertEqual(card["primary_type"], "decision_todo")
        self.assertEqual(card["role"], "decision_maker")
        self.assertTrue(card["mandatory"])
        self.assertIn("approve", [item["code"] for item in card["allowed_actions"]])

    def test_advice_todo_has_advice_action(self):
        self.add("2", scopes="todo_pending", lane="todo_backed", node={"role": "建议人"})
        card = cwk_action_center.build_cards(self.run)["cards"][0]
        self.assertEqual(card["primary_type"], "advice_todo")
        self.assertEqual(card["role"], "advisor")
        self.assertIn("submit_advice", [item["code"] for item in card["allowed_actions"]])

    def test_updated_todo_is_reactivated_and_keeps_role(self):
        self.add("3", scopes="todo_pending,unread", lane="todo_backed", change_type="updated", node={"role": "决策人"})
        card = cwk_action_center.build_cards(self.run)["cards"][0]
        self.assertEqual(card["primary_type"], "reactivated_todo")
        self.assertEqual(card["underlying_role"], "decision_maker")
        self.assertTrue(card["mandatory"])

    def test_inbox_and_update_are_optional(self):
        self.add("4", scopes="inbox", lane="inbox_awareness")
        self.add("5", scopes="inbox,unread", lane="reply_chain", change_type="updated", row={"hasNewReply": True})
        cards = {card["report_id"]: card for card in cwk_action_center.build_cards(self.run)["cards"]}
        self.assertEqual(cards["4"]["primary_type"], "inbox_awareness")
        self.assertEqual(cards["5"]["primary_type"], "update_notice")
        self.assertFalse(cards["4"]["mandatory"])
        self.assertIn("express_opinion", [item["code"] for item in cards["5"]["allowed_actions"]])

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
        self.assertIn("submit_advice", actions)

    def test_same_report_is_rendered_once_with_highest_responsibility(self):
        self.add("8", scopes="inbox", lane="inbox_awareness")
        (self.run / "raw" / "8-todo.md").write_text(raw_record("8", scopes="todo_pending", lane="todo_backed", node={"role": "建议人"}), encoding="utf-8")
        cards = cwk_action_center.build_cards(self.run)["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["primary_type"], "advice_todo")
        self.assertIn("inbox", cards[0]["source_labels"])

    def test_html_is_interactive_but_has_no_write_transport(self):
        self.add("9", scopes="inbox", lane="inbox_awareness")
        payload = cwk_action_center.build_cards(self.run)
        page = cwk_action_center.render_html(payload)
        cwk_action_center.assert_shadow_safe(page)
        self.assertIn("Shadow Mode", page)
        self.assertIn("不会提交到 CWork", page)
        self.assertIn("textarea", page)
        self.assertNotIn("fetch(", page)
        self.assertNotIn("XMLHttpRequest", page)

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
