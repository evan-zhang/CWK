import sys
import tempfile
import unittest
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_wiki_query  # noqa: E402
import cwk_cloud_wiki_compile  # noqa: E402
import cwk_cloud_wiki_compile  # noqa: E402


def raw_page(report_id: str, title: str, writer: str, date: str, content: str) -> str:
    return f'''---
report_id: "{report_id}"
title: "{title}"
writer: "{writer}"
create_time: "{date} 10:00:00"
source_lane: inbox_awareness
---

# {title}

<content>
{content}
</content>
'''


def summary_page(report_id: str, title: str, writer: str, date: str, raw_rel: str, summary: str, quote: str = "") -> str:
    evidence = f"\n## 关键事实\n\n- {summary}  \n  证据：> {quote}\n" if quote else ""
    return f'''---
type: SourceSummary
report_id: "{report_id}"
source: "{raw_rel}"
---

# {title}

- 原文：[`{report_id}`]({raw_rel})
- 发送人：{writer}
- 时间：{date} 10:00:00
- 来源类型：`inbox_awareness`

## 摘要

{summary}
{evidence}
## 证据边界

事实以原文为准。
'''


class WikiQueryTests(unittest.TestCase):
    def test_fallback_summary_is_navigable_without_claiming_source_facts(self):
        metadata = {
            "report_id": "1", "title": "测试汇报", "writer": "甲",
            "created_at": "2026-08-04", "source_lane": "inbox_awareness",
        }
        page = cwk_cloud_wiki_compile.render_fallback(metadata, "raw/2026-08/2026-08-04/1.md", "模型失败")
        self.assertIn(cwk_cloud_wiki_compile.FALLBACK_MARKER, page)
        self.assertIn("必须回读原文", page)
        self.assertNotIn("关键事实", page)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mirror = Path(self.temp.name) / "工作协同镜像"
        self.raw = self.mirror / "raw" / "2026-07" / "2026-07-16"
        self.summaries = self.mirror / "wiki" / "summaries"
        self.topics = self.mirror / "wiki" / "topics"
        self.entities = self.mirror / "wiki" / "entities" / "people"
        for path in (self.raw, self.summaries, self.topics, self.entities):
            path.mkdir(parents=True)

        self.finance_id = "2077642842540343298"
        self.other_id = "2076646836936179713"
        finance_raw = self.raw / f"{self.finance_id}-AI财务单据审核token消耗汇报.md"
        other_raw = self.raw / f"{self.other_id}-OpenClaw异常分析.md"
        finance_quote = "两周调用次数 37551 次，两周 Token 10.91 亿。"
        other_quote = "7 月 10 日三位用户合计 7.79 亿 token。"
        finance_raw.write_text(raw_page(self.finance_id, "AI财务单据审核token消耗汇报", "杨晶晶", "2026-07-16", finance_quote), encoding="utf-8")
        other_raw.write_text(raw_page(self.other_id, "OpenClaw异常分析", "屈军利", "2026-07-13", other_quote), encoding="utf-8")
        finance_rel = f"../../raw/2026-07/2026-07-16/{finance_raw.name}"
        other_rel = f"../../raw/2026-07/2026-07-16/{other_raw.name}"
        (self.summaries / f"{self.finance_id}.md").write_text(
            summary_page(self.finance_id, "AI财务单据审核token消耗汇报", "杨晶晶", "2026-07-16", finance_rel, "财务审核两周消耗 10.91 亿 Token", finance_quote),
            encoding="utf-8",
        )
        (self.summaries / f"{self.other_id}.md").write_text(
            summary_page(self.other_id, "OpenClaw异常分析", "屈军利", "2026-07-13", other_rel, "OpenClaw Token 异常", other_quote),
            encoding="utf-8",
        )
        (self.topics / "财务审核.md").write_text(
            f"# 财务审核\n\n- [`{self.finance_id}`](../summaries/{self.finance_id}.md) 财务审核\n",
            encoding="utf-8",
        )
        (self.entities / "杨晶晶.md").write_text(
            f"# 杨晶晶\n\n- [`{self.finance_id}`](../../summaries/{self.finance_id}.md) 财务审核\n",
            encoding="utf-8",
        )
        system = self.mirror / "wiki" / "_system"
        system.mkdir(parents=True)
        (system / "manifest.json").write_text(
            json.dumps(
                {
                    "ai_refined_report_ids": [self.finance_id],
                    "fallback_report_ids": [self.other_id],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_fact_query_ranks_expected_report_and_verifies_raw_quote(self):
        result = cwk_wiki_query.query_mirror(self.mirror, "财务单据审核两周 Token 10.91亿", top_k=2)
        self.assertEqual(result["results"][0]["report_id"], self.finance_id)
        self.assertEqual(result["results"][0]["evidence_status"], "verified")
        self.assertIn("10.91 亿", result["results"][0]["evidence"][0]["quote"])
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["results"][0]["summary_quality"], "ai_refined")
        self.assertEqual(result["indexed"]["summary_quality"]["fallback_pending"], 1)

    def test_writer_and_date_filters_are_applied(self):
        result = cwk_wiki_query.query_mirror(
            self.mirror,
            "Token",
            writer="杨晶晶",
            from_date="2026-07-15",
            to_date="2026-07-20",
        )
        self.assertEqual([item["report_id"] for item in result["results"]], [self.finance_id])

    def test_navigation_page_adds_graph_recall(self):
        result = cwk_wiki_query.query_mirror(self.mirror, "杨晶晶最近参与什么", top_k=2)
        self.assertEqual(result["results"][0]["report_id"], self.finance_id)
        self.assertTrue(result["results"][0]["navigation_hits"])

    def test_unknown_query_abstains(self):
        result = cwk_wiki_query.query_mirror(self.mirror, "霜蓝鲸鱼量子披萨", top_k=2)
        self.assertEqual(result["confidence"], "none")
        self.assertEqual(result["results"], [])

    def test_lint_passes_and_ignores_non_report_long_ids_in_evidence(self):
        topic = self.topics / "财务审核.md"
        topic.write_text(topic.read_text(encoding="utf-8") + '\n证据："fileId":"1514822141906423809"\n', encoding="utf-8")
        result = cwk_wiki_query.lint_mirror(self.mirror)
        self.assertTrue(result["overall_pass"])
        self.assertEqual(result["checks"]["dangling_navigation_refs"], [])

    def test_lint_detects_missing_raw(self):
        next(self.raw.glob(f"{self.finance_id}-*.md")).unlink()
        result = cwk_wiki_query.lint_mirror(self.mirror)
        self.assertFalse(result["overall_pass"])
        self.assertIn(self.finance_id, result["checks"]["missing_raw"])

    def test_compile_reconcile_separates_coverage_from_ai_quality(self):
        fallback = self.summaries / f"{self.finance_id}.md"
        fallback.write_text(
            fallback.read_text(encoding="utf-8")
            + "\n本页为本次重组阶段生成的本地兜底摘要。\n",
            encoding="utf-8",
        )
        manifest = {"compiled_report_ids": []}
        result = cwk_cloud_wiki_compile.reconcile_disk_to_manifest(self.mirror / "wiki", manifest)
        self.assertEqual(result["disk_summaries"], 2)
        self.assertEqual(result["fallback_summaries"], 1)
        self.assertEqual(result["ai_refined_summaries"], 1)
        self.assertEqual(manifest["fallback_report_ids"], [self.finance_id])
        self.assertEqual(manifest["ai_refined_report_ids"], [self.other_id])

    def test_compile_prompt_requires_valid_json_and_quote_safe_evidence(self):
        metadata = {
            "report_id": "1",
            "title": "示例",
            "writer": "同事",
            "created_at": "2026-08-04 10:00:00",
            "source_lane": "inbox_awareness",
        }
        value = cwk_cloud_wiki_compile.prompt(metadata, "启动“周数据总结”报告。")
        self.assertIn("valid JSON", value)
        self.assertIn("Never place an unescaped ASCII double quote", value)
        self.assertIn("choose a shorter exact contiguous source span", value)

    def test_compile_failure_queue_counts_attempts(self):
        manifest = {"failure_queue": []}
        cwk_cloud_wiki_compile.update_manifest_failure("42", "timeout", manifest)
        cwk_cloud_wiki_compile.update_manifest_failure("42", "invalid json", manifest)
        self.assertEqual(len(manifest["failure_queue"]), 1)
        self.assertEqual(manifest["failure_queue"][0]["attempts"], 2)
        self.assertEqual(manifest["failure_queue"][0]["error"], "invalid json")

    def test_query_exposes_terminal_fallback_quality(self):
        manifest_path = self.mirror / "wiki" / "_system" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["failure_queue"] = [{"report_id": self.other_id, "attempts": 3, "error": "timeout"}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = cwk_wiki_query.query_mirror(self.mirror, "OpenClaw Token 异常", top_k=2)
        self.assertEqual(result["results"][0]["summary_quality"], "fallback_terminal_error")


if __name__ == "__main__":
    unittest.main()
