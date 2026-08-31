"""RT-022 acceptance tests for author/date report-listing evolution."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_wiki_query  # noqa: E402


def _raw_page(report_id: str, writer: str, date: str, body: str) -> str:
    return f'''---
report_id: "{report_id}"
title: "工作汇报"
writer: "{writer}"
create_time: "{date} 10:00:00"
source_lane: inbox_awareness
---

# 工作汇报

<meta>
- **汇报人**: {writer}
- **时间**: {date} 10:00:00
</meta>
<content>
{body}
</content>
'''


def _summary_page(report_id: str, writer: str, date: str, raw_rel: str) -> str:
    return f'''---
type: SourceSummary
report_id: "{report_id}"
source: "{raw_rel}"
---

# 工作汇报

- 原文：[`{report_id}`]({raw_rel})
- 发送人：{writer}
- 时间：{date} 10:00:00
- 来源类型：`inbox_awareness`

## 摘要

例行事项。

## 证据边界

事实以原文为准。
'''


class AuthorListingEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.mirror = Path(self.temp.name) / "工作协同镜像"
        self.raw = self.mirror / "raw" / "2026-07" / "2026-07-16"
        self.summaries = self.mirror / "wiki" / "summaries"
        self.system = self.mirror / "wiki" / "_system"
        for path in (self.raw, self.summaries, self.system):
            path.mkdir(parents=True)
        (self.system / "manifest.json").write_text(
            json.dumps({"ai_refined_report_ids": [], "fallback_report_ids": []}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_raw(self, report_id: str, writer: str, date: str) -> Path:
        path = self.raw / f"{report_id}-工作汇报.md"
        path.write_text(
            _raw_page(report_id, writer, date, "正文不包含通用列表标签。"),
            encoding="utf-8",
        )
        return path

    def test_generic_author_listing_is_metadata_scoped_and_raw_verified(self) -> None:
        report_id = "2077642842540343298"
        raw = self._write_raw(report_id, "杨晶晶", "2026-07-16")
        raw_rel = f"../../raw/2026-07/2026-07-16/{raw.name}"
        (self.summaries / f"{report_id}.md").write_text(
            _summary_page(report_id, "杨晶晶", "2026-07-16", raw_rel),
            encoding="utf-8",
        )

        result = cwk_wiki_query.query_mirror(
            self.mirror,
            "工作汇报",
            writer="杨晶晶",
            from_date="2026-07-15",
            to_date="2026-07-20",
            top_k=5,
        )

        self.assertEqual([row["report_id"] for row in result["results"]], [report_id])
        self.assertEqual(result["entity_resolution"]["reason"], "filter_listing")
        self.assertEqual(result["results"][0]["evidence_status"], "verified")
        self.assertEqual(
            result["results"][0]["evidence"][0]["kind"],
            "raw_listing_metadata",
        )

    def test_raw_only_author_listing_preserves_incremental_coverage(self) -> None:
        report_id = "2077999999999999999"
        self._write_raw(report_id, "杨晶晶", "2026-07-17")

        result = cwk_wiki_query.query_mirror(
            self.mirror,
            "工作汇报",
            writer="杨晶晶",
            from_date="2026-07-15",
            to_date="2026-07-20",
            top_k=5,
        )

        self.assertEqual([row["report_id"] for row in result["results"]], [report_id])
        self.assertEqual(result["indexed"]["raw_listing_fallback_count"], 1)
        self.assertEqual(result["results"][0]["evidence_status"], "verified")
