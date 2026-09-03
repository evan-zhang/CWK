"""RT-037: backfill source rows switch to the 3.1 inbox endpoint.

The inbox API speaks second-level timestamps and silently matches zero rows
when fed 13-digit milliseconds (live-verified 2026-09-03: same window,
ms = 0 rows, seconds = 38 rows).  These tests lock the conversion, the
client contract, pagination integrity, and the inbox-row fallbacks.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_backfill_range import (  # noqa: E402
    enrich_inbox_row,
    epoch_seconds,
    inbox_source_rows,
    report_time_from_row,
    source_rows,
    writer_from_row,
)


# 2026-09-03 00:00:00 +08:00 / 23:59:59 +08:00, cross-checked against the
# live inbox endpoint on 2026-09-03 (total > 0 only with these values).
DAY_START = 1788364800
DAY_END = 1788451199


class FakeClient:
    def __init__(self, pages, error_on_page=None):
        self.pages = pages
        self.error_on_page = error_on_page
        self.calls = []

    def get_inbox_list(self, *, page_size, page_index, begin_time, end_time, **_):
        self.calls.append(
            {"page_size": page_size, "page_index": page_index,
             "begin_time": begin_time, "end_time": end_time}
        )
        if self.error_on_page == page_index:
            raise RuntimeError("boom")
        rows, total = self.pages[page_index]
        return {"list": rows, "total": total}


def inbox_row(rid, time_text="2026-09-03T10:48:31.000+00:00"):
    return {"id": rid, "main": f"row {rid}", "reportEventVO": {"name": "许敏玲", "time": time_text}}


class EpochSecondsTests(unittest.TestCase):
    def test_golden_day_window(self):
        self.assertEqual(epoch_seconds("2026-09-03"), DAY_START)
        self.assertEqual(epoch_seconds("2026-09-03", end_of_day=True), DAY_END)
        self.assertEqual(DAY_END - DAY_START, 86399)

    def test_never_millisecond_scale(self):
        for date_text in ("2026-01-01", "2026-09-03", "2030-12-31"):
            for end_of_day in (False, True):
                value = epoch_seconds(date_text, end_of_day=end_of_day)
                self.assertTrue(10**9 <= value < 10**12, (date_text, end_of_day, value))

    def test_pins_utc8_regardless_of_host_timezone(self):
        # 2026-09-04 00:00 +08:00 is 2026-09-03 16:00 UTC.
        expected = int(datetime(2026, 9, 3, 16, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(epoch_seconds("2026-09-04"), expected)

    def test_rejects_non_iso_date(self):
        with self.assertRaises(ValueError):
            epoch_seconds("2026/09/03")


class InboxSourceRowsTests(unittest.TestCase):
    def test_client_receives_second_level_window(self):
        client = FakeClient({1: ([], 0)})
        inbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)
        call = client.calls[0]
        self.assertEqual(call["begin_time"], DAY_START)
        self.assertEqual(call["end_time"], DAY_END)
        for key in ("begin_time", "end_time"):
            self.assertTrue(10**9 <= call[key] < 10**12, (key, call[key]))

    def test_pagination_aggregates_until_short_page(self):
        client = FakeClient({1: ([inbox_row("1"), inbox_row("2")], 3), 2: ([inbox_row("3")], 3)})
        rows, total = inbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=2)
        self.assertEqual({row["id"] for row in rows}, {"1", "2", "3"})
        self.assertEqual(total, 3)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[-1]["page_index"], 2)

    def test_duplicate_rows_across_pages_are_deduped(self):
        client = FakeClient(
            {1: ([inbox_row("1"), inbox_row("2")], 3),
             2: ([inbox_row("1"), inbox_row("3")], 3),
             3: ([], 3)}
        )
        rows, total = inbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=2)
        self.assertEqual({row["id"] for row in rows}, {"1", "2", "3"})
        self.assertEqual(total, 3)

    def test_total_mismatch_raises(self):
        client = FakeClient({1: ([inbox_row("1")], 5)})
        with self.assertRaisesRegex(RuntimeError, "source pagination incomplete: expected 5, got 1"):
            inbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)

    def test_client_error_is_wrapped_with_page_context(self):
        client = FakeClient({}, error_on_page=1)
        with self.assertRaisesRegex(RuntimeError, r"inbox page 1 failed"):
            inbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)


class SourceRowsDispatchTests(unittest.TestCase):
    def test_default_source_is_inbox_via_factory(self):
        client = FakeClient({1: ([inbox_row("7")], 1)})
        seen = []

        def factory(app_key):
            seen.append(app_key)
            return client

        rows, total = source_rows("secret-key", "2026-09-03", "2026-09-03", client_factory=factory)
        self.assertEqual(seen, ["secret-key"])
        self.assertEqual([row["id"] for row in rows], ["7"])
        self.assertEqual(total, 1)

    def test_search_list_branch_routes_to_legacy_lane(self):
        with mock.patch("cwk_backfill_range.search_list_source_rows", return_value=([], 0)) as legacy:
            source_rows("key", "2026-09-03", "2026-09-03", source="search-list")
        legacy.assert_called_once_with("key", "2026-09-03", "2026-09-03", 100)

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            source_rows("key", "2026-09-03", "2026-09-03", source="outbox")


class InboxRowFallbackTests(unittest.TestCase):
    def test_enrich_derives_second_level_report_time(self):
        row = enrich_inbox_row(inbox_row("1"))
        expected = int(datetime.fromisoformat("2026-09-03T10:48:31.000+00:00").timestamp())
        self.assertEqual(row["reportTime"], expected)
        self.assertTrue(10**9 <= row["reportTime"] < 10**12)

    def test_enrich_never_overwrites_server_value_or_bad_rows(self):
        self.assertEqual(enrich_inbox_row({"id": "1", "reportTime": 123})["reportTime"], 123)
        self.assertNotIn("reportTime", enrich_inbox_row({"id": "2", "reportEventVO": {"time": "nonsense"}}))
        self.assertNotIn("reportTime", enrich_inbox_row({"id": "3"}))

    def test_writer_falls_back_to_event_name(self):
        self.assertEqual(writer_from_row(inbox_row("1")), "许敏玲")
        self.assertEqual(writer_from_row({"id": "2", "fromEmp": {"name": "张三"}}), "张三")
        self.assertEqual(writer_from_row({"id": "3"}), "")

    def test_report_time_scales_ms_and_seconds_identically(self):
        seconds = 1788451711
        from_ms = report_time_from_row({"reportTime": seconds * 1000})
        from_s = report_time_from_row({"reportTime": seconds})
        expected = datetime.fromtimestamp(seconds).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(from_ms, expected)
        self.assertEqual(from_s, expected)

    def test_report_time_parses_iso_event_time(self):
        row = {"reportEventVO": {"time": "2026-09-03T10:48:31.000+00:00"}}
        expected = datetime.fromisoformat("2026-09-03T10:48:31.000+00:00").astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(report_time_from_row(row), expected)


if __name__ == "__main__":
    unittest.main()
