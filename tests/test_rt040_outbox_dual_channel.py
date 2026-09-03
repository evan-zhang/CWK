"""RT-040 ord1: backfill source rows dual channel (inbox + outbox).

The outbox endpoint returns reports this account sent itself — rows the
inbox lane can never see.  These tests lock the merge/dedupe contract, the
shared pagination integrity check, the second-level window contract on the
outbox call, error wrapping, scopes stamping, and the CLI default flip.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_backfill_range import (  # noqa: E402
    _paged_list,
    outbox_source_rows,
    run_backfill,
    source_rows,
)

# 2026-09-03 00:00:00 / 23:59:59 +08:00 (cross-checked live 2026-09-03).
DAY_START = 1788364800
DAY_END = 1788451199


def row(rid, name="发送人", time_text="2026-09-03T03:00:00.000+00:00"):
    return {"id": rid, "main": f"row {rid}", "reportEventVO": {"name": name, "time": time_text}}


class FakeDualClient:
    def __init__(self, inbox_pages, outbox_pages, outbox_error_on_page=None):
        self.inbox_pages = inbox_pages
        self.outbox_pages = outbox_pages
        self.outbox_error_on_page = outbox_error_on_page
        self.calls = []

    def get_inbox_list(self, *, page_size, page_index, begin_time, end_time, **_):
        self.calls.append(("inbox", page_index, begin_time, end_time))
        rows, total = self.inbox_pages[page_index]
        return {"list": rows, "total": total}

    def get_outbox_list(self, *, page_size, page_index, begin_time, end_time, **_):
        self.calls.append(("outbox", page_index, begin_time, end_time))
        if self.outbox_error_on_page == page_index:
            raise RuntimeError("boom")
        rows, total = self.outbox_pages[page_index]
        return {"list": rows, "total": total}


class OutboxSourceRowsTests(unittest.TestCase):
    def test_outbox_receives_second_level_window(self):
        client = FakeDualClient({}, {1: ([], 0)})
        outbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)
        call = client.calls[0]
        self.assertEqual((call[0], call[2], call[3]), ("outbox", DAY_START, DAY_END))
        for value in (call[2], call[3]):
            self.assertTrue(10**9 <= value < 10**12, value)

    def test_pagination_aggregates_and_dedupes(self):
        client = FakeDualClient({}, {1: ([row("1"), row("2")], 3), 2: ([row("1"), row("3")], 3), 3: ([], 3)})
        rows, total = outbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=2)
        self.assertEqual({r["id"] for r in rows}, {"1", "2", "3"})
        self.assertEqual(total, 3)
        self.assertEqual(client.calls[-1][1], 3)

    def test_total_mismatch_raises(self):
        client = FakeDualClient({}, {1: ([row("1")], 5)})
        with self.assertRaisesRegex(RuntimeError, "expected 5, got 1"):
            outbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)

    def test_client_error_is_wrapped_with_outbox_context(self):
        client = FakeDualClient({}, {}, outbox_error_on_page=1)
        with self.assertRaisesRegex(RuntimeError, r"outbox page 1 failed"):
            outbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)

    def test_rows_get_second_level_report_time(self):
        client = FakeDualClient({}, {1: ([row("9")], 1)})
        rows, _ = outbox_source_rows(client, "2026-09-03", "2026-09-03", page_size=100)
        self.assertEqual(rows[0]["reportTime"], 1788404400)  # 2026-09-03T03:00:00Z
        self.assertTrue(10**9 <= rows[0]["reportTime"] < 10**12)


class DualMergeTests(unittest.TestCase):
    def test_outbox_only_rows_are_added(self):
        client = FakeDualClient(
            {1: ([row("1", "同事甲")], 1)},
            {1: ([row("2", "我自己"), row("1", "同事甲")], 2)},
        )
        rows, total = source_rows("k", "2026-09-03", "2026-09-03", client_factory=lambda k: client, source="dual")
        self.assertEqual({r["id"] for r in rows}, {"1", "2"})
        self.assertEqual(total, 2)  # inbox_total(1) + outbox_only(1)

    def test_no_outbox_rows_keeps_inbox_total(self):
        client = FakeDualClient({1: ([row("1"), row("2")], 2)}, {1: ([], 0)})
        rows, total = source_rows("k", "2026-09-03", "2026-09-03", client_factory=lambda k: client, source="dual")
        self.assertEqual(len(rows), 2)
        self.assertEqual(total, 2)

    def test_inbox_row_wins_on_overlap(self):
        client = FakeDualClient(
            {1: ([row("1", "收件箱视角")], 1)},
            {1: ([row("1", "发件箱视角")], 1)},
        )
        rows, total = source_rows("k", "2026-09-03", "2026-09-03", client_factory=lambda k: client, source="dual")
        self.assertEqual(rows[0]["reportEventVO"]["name"], "收件箱视角")
        self.assertEqual(total, 1)

    def test_inbox_source_is_unchanged_without_dual(self):
        client = FakeDualClient({1: ([row("1")], 1)}, {1: ([row("2")], 1)})
        source_rows("k", "2026-09-03", "2026-09-03", client_factory=lambda k: client, source="inbox")
        self.assertTrue(all(call[0] == "inbox" for call in client.calls))


class PagedListHelperTests(unittest.TestCase):
    def test_short_page_stops_walk(self):
        pages = {1: ({"list": [row("1")], "total": 1},)}
        calls = []

        def fetch(page):
            calls.append(page)
            return pages[page][0]

        rows, total = _paged_list(fetch, label="x", page_size=10)
        self.assertEqual(calls, [1])
        self.assertEqual(total, 1)
        self.assertEqual(len(rows), 1)


class ScopesAndCLITests(unittest.TestCase):
    def test_dual_scopes_stamp_both_channels(self):
        staging_written = {}

        def fake_write_markdown(raw_dir, rid, r, lane, full, simple, node, **kwargs):
            from pathlib import Path as P
            path = P(f"{rid}-x.md")
            staging_written[(rid, tuple(sorted(kwargs.get("source_scopes") or set())))] = True
            return raw_dir / path

        client = FakeDualClient(
            {1: ([row("1", "同事甲")], 1)},
            {1: ([row("2", "我自己")], 1)},
        )
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp) / "mirror"
            with mock.patch("cwk_backfill_range.run_tool") as tool, \
                 mock.patch("cwk_backfill_range.write_markdown", side_effect=fake_write_markdown), \
                 mock.patch("cwk_backfill_range._inbox_client", lambda key: client):
                tool.return_value = {"success": True, "data": {"fullContent": "正文"}}
                manifest = run_backfill(
                    app_key="k", start_date="2026-09-03", end_date="2026-09-03",
                    run_name="rt040-test", mirror_root=mirror, max_parallel=2,
                    page_size=100, source="dual",
                )
        # dual run stamps both channels on every fetched row; the per-row
        # origin is preserved in the row's List Row Metadata, not in scopes.
        expected = {("1", ("inbox_range", "outbox_range")), ("2", ("inbox_range", "outbox_range"))}
        self.assertEqual(set(staging_written), expected)
        self.assertEqual(manifest["source_mode"], "dual")
        self.assertEqual(manifest["source_total"], 2)
        self.assertEqual(manifest["remaining_missing"], 0)

    def test_cli_default_is_dual(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "cwk_backfill_range.py"), "--help"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("--source {dual,inbox,search-list}", result.stdout)
        self.assertIn("closes the self-sent", result.stdout)
        self.assertNotIn("(default; live-verified 2026-09-03). search-list", result.stdout.replace("closes the self-sent gap, live-verified", "x") if False else result.stdout)


if __name__ == "__main__":
    unittest.main()
