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


class RawPromotionTests(unittest.TestCase):
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

    def test_source_pagination_requires_exact_total(self):
        pages = {
            "1": {"success": True, "data": {"list": [{"id": "1"}, {"id": "2"}], "total": 3}},
            "2": {"success": True, "data": {"list": [{"id": "3"}], "total": 3}},
        }

        def fake_run(_script, args, _key):
            return pages[args[args.index("--page-index") + 1]]

        with patch.object(cwk_backfill_range, "run_tool", side_effect=fake_run):
            rows, total = cwk_backfill_range.source_rows("key", "2026-08-01", "2026-08-04", page_size=2)
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
