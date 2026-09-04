"""RT-042 账本 / 游标 / 状态机判据：J4（账本锁死）、J5（游标续采）、J8（状态机）。

J4 红法：人为改一个**已存在**文件的内容 → 全量校验必须红，且「存量不变」
断言必须把它认出来。J5 红法：写到一半抛异常 → 重跑必须从游标续，且既不重复
也不丢失。J8 红法：draft TTL 过期后所有动词拒绝，非法跃迁拒绝。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_kb_doctor as doctor  # noqa: E402
import cwk_kb_ledger as ledger  # noqa: E402
from cwk_kb_create import KbSpec, SourceSpec, create_kb  # noqa: E402
from cwk_kb_storage import LocalFSBackend, MemoryBackend, NotFound  # noqa: E402

FIXED_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def built_kb(backend) -> str:
    spec = KbSpec(
        display_name="账本库",
        kb_code="b" * 32,
        owner_ref="owner-42",
        created_at=FIXED_NOW,
        sources=(SourceSpec("cwork"),),
    )
    create_kb(backend, spec)
    return spec.kb_code


# ── J4: ledger lock-down ────────────────────────────────────────────────────


class ManifestTests(unittest.TestCase):
    """J4 — after a write the manifest verifies; tamper with a file and it goes red."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = LocalFSBackend(Path(self.tmp.name) / "kb")
        self.kb_code = built_kb(self.backend)

    def test_a_fresh_build_verifies_clean(self) -> None:
        report = ledger.verify_manifest(self.backend)
        self.assertTrue(report.ok, report.describe())
        self.assertEqual(report.describe(), "全量校验通过")

    def test_tampering_with_an_existing_file_turns_the_check_red(self) -> None:
        self.backend.write("wiki/index.md", "# 被人改过\n".encode("utf-8"))
        report = ledger.verify_manifest(self.backend)
        self.assertFalse(report.ok)
        self.assertEqual(report.mismatched, ["wiki/index.md"])
        self.assertIn("哈希不符", report.describe())

    def test_deleting_a_recorded_file_turns_the_check_red(self) -> None:
        self.backend.remove("kb.json")
        report = ledger.verify_manifest(self.backend)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing, ["kb.json"])

    def test_an_unrecorded_new_file_turns_the_check_red(self) -> None:
        self.backend.write("wiki/summaries/sneaked.md", b"x")
        report = ledger.verify_manifest(self.backend)
        self.assertFalse(report.ok)
        self.assertEqual(report.extra, ["wiki/summaries/sneaked.md"])

    def test_refresh_refuses_to_resign_a_tree_whose_history_changed(self) -> None:
        """The v2-in-place-edit failure mode: rewrite raw, re-sign the ledger."""
        self.backend.write("wiki/index.md", "# 原地改写\n".encode("utf-8"))
        with self.assertRaises(ledger.LedgerViolation) as ctx:
            ledger.refresh_manifest(self.backend, kb_code=self.kb_code)
        self.assertIn("存量文件被原地改写", str(ctx.exception))

    def test_refresh_accepts_declared_additions_and_bumps_the_version(self) -> None:
        before = ledger.load_manifest(self.backend)["manifest_version"]
        ledger.record_write(self.backend, "wiki/summaries/one.md", "# 摘要\n".encode("utf-8"))
        after = ledger.refresh_manifest(
            self.backend, kb_code=self.kb_code, allow_new=["wiki/summaries/one.md"]
        )
        self.assertEqual(after["manifest_version"], before + 1)
        self.assertTrue(ledger.verify_manifest(self.backend).ok)

    def test_refresh_refuses_an_undeclared_addition(self) -> None:
        ledger.record_write(self.backend, "wiki/summaries/two.md", "# 摘要\n".encode("utf-8"))
        with self.assertRaises(ledger.LedgerViolation) as ctx:
            ledger.refresh_manifest(self.backend, kb_code=self.kb_code)
        self.assertIn("未声明的新增文件", str(ctx.exception))

    def test_timestamp_class_files_are_exempt_from_the_unchanged_rule(self) -> None:
        before = ledger.load_manifest(self.backend)
        for path in ledger.TIMESTAMP_CLASS_PATHS:
            if path == ledger.MANIFEST_REL:
                continue
            self.backend.write(path, self.backend.read(path) + "- 追加一行\n".encode("utf-8"))
        after = ledger.build_manifest(self.backend, kb_code=self.kb_code, manifest_version=2)
        self.assertEqual(ledger.assert_existing_unchanged(before, after), [])

    def test_the_exemption_does_not_cover_anything_else(self) -> None:
        before = ledger.load_manifest(self.backend)
        self.backend.write("source.json", b"{}\n")
        after = ledger.build_manifest(self.backend, kb_code=self.kb_code, manifest_version=2)
        violations = ledger.assert_existing_unchanged(before, after)
        self.assertEqual(len(violations), 1)
        self.assertIn("source.json", violations[0])


class WriteReconcileTests(unittest.TestCase):
    def test_record_write_returns_the_digest_it_verified(self) -> None:
        backend = MemoryBackend()
        digest = ledger.record_write(backend, "kb.json", b"{}\n")
        self.assertEqual(digest, backend.sha256("kb.json"))

    def test_a_backend_that_stores_nothing_fails_at_the_write(self) -> None:
        class Sink(MemoryBackend):
            def write(self, path, data):
                return ledger.sha256_bytes(data)

        with self.assertRaises(ledger.WriteReconcileFailed):
            ledger.record_write(Sink(), "kb.json", b"{}\n")

    def test_a_backend_that_corrupts_bytes_fails_at_the_write(self) -> None:
        class Corrupt(MemoryBackend):
            def write(self, path, data):
                return super().write(path, data + b"extra")

        with self.assertRaises(ledger.WriteReconcileFailed) as ctx:
            ledger.record_write(Corrupt(), "kb.json", b"{}\n")
        self.assertIn("写后对账失败", str(ctx.exception))


class ChangedPathsTests(unittest.TestCase):
    def test_batches_accumulate(self) -> None:
        backend = MemoryBackend()
        built_kb(backend)
        ledger.record_changed_paths(backend, ["wiki/index.md"], reason="rebuild --index", at=FIXED_NOW)
        payload = ledger.record_changed_paths(
            backend, ["wiki/daily/2026-09-04.md"], reason="nightly", at=FIXED_NOW
        )
        self.assertEqual(len(payload["batches"]), 2)
        self.assertEqual(payload["batches"][0]["reason"], "rebuild --index")
        self.assertEqual(payload["batches"][1]["paths"], ["wiki/daily/2026-09-04.md"])


# ── J5: cursor resume ───────────────────────────────────────────────────────


class CollectionCursorTests(unittest.TestCase):
    """J5 — an interrupted run resumes from the cursor: no duplicates, no losses."""

    ITEMS = [f"report-{n:02d}" for n in range(1, 11)]

    def setUp(self) -> None:
        self.backend = MemoryBackend()
        built_kb(self.backend)

    def collect(self, state: ledger.CollectionState, *, fail_after: int | None = None):
        """Collect the batch, optionally dying after ``fail_after`` items."""
        processed = []
        pending = state.begin_batch("cwork", "inbox", self.ITEMS, at=FIXED_NOW)
        for index, item in enumerate(pending):
            if fail_after is not None and index == fail_after:
                raise RuntimeError("模拟采集中断")
            state.mark_done("cwork", "inbox", item, at=FIXED_NOW)
            processed.append(item)
        state.finish_batch("cwork", "inbox", at=FIXED_NOW)
        return processed

    def test_a_clean_run_collects_every_item_once(self) -> None:
        state = ledger.CollectionState.load(self.backend)
        self.assertEqual(self.collect(state), self.ITEMS)
        self.assertEqual(state.collected("cwork", "inbox"), self.ITEMS)
        self.assertEqual(state.pending("cwork", "inbox"), [])
        self.assertFalse(state.is_interrupted("cwork", "inbox"))

    def test_an_interrupted_run_resumes_with_no_duplicates_and_no_losses(self) -> None:
        first = ledger.CollectionState.load(self.backend)
        with self.assertRaises(RuntimeError):
            self.collect(first, fail_after=4)

        # A fresh process: state comes from storage, not from memory.
        resumed = ledger.CollectionState.load(self.backend)
        self.assertEqual(resumed.collected("cwork", "inbox"), self.ITEMS[:4])
        self.assertEqual(resumed.cursor("cwork", "inbox"), "report-04")
        self.assertTrue(resumed.is_interrupted("cwork", "inbox"))

        second_pass = self.collect(resumed)
        self.assertEqual(second_pass, self.ITEMS[4:], "续采只该处理剩下的 6 条")

        final = ledger.CollectionState.load(self.backend)
        collected = final.collected("cwork", "inbox")
        self.assertEqual(collected, self.ITEMS, "无丢失")
        self.assertEqual(len(collected), len(set(collected)), "无重复")
        self.assertFalse(final.is_interrupted("cwork", "inbox"))

    def test_rerunning_a_completed_batch_collects_nothing_again(self) -> None:
        state = ledger.CollectionState.load(self.backend)
        self.collect(state)
        again = ledger.CollectionState.load(self.backend)
        self.assertEqual(again.begin_batch("cwork", "inbox", self.ITEMS, at=FIXED_NOW), [])
        self.assertEqual(again.collected("cwork", "inbox"), self.ITEMS)

    def test_a_later_window_only_hands_out_the_new_items(self) -> None:
        state = ledger.CollectionState.load(self.backend)
        self.collect(state)
        grown = self.ITEMS + ["report-11", "report-12"]
        self.assertEqual(
            state.begin_batch("cwork", "inbox", grown, at=FIXED_NOW),
            ["report-11", "report-12"],
        )

    def test_lanes_and_sources_do_not_bleed_into_each_other(self) -> None:
        state = ledger.CollectionState.load(self.backend)
        state.begin_batch("cwork", "inbox", ["a", "b"], at=FIXED_NOW)
        state.mark_done("cwork", "inbox", "a", at=FIXED_NOW)
        self.assertEqual(state.begin_batch("cwork", "outbox", ["a", "b"], at=FIXED_NOW), ["a", "b"])
        self.assertEqual(state.begin_batch("docdb", "files", ["a"], at=FIXED_NOW), ["a"])
        self.assertEqual(state.collected("cwork", "inbox"), ["a"])

    def test_finishing_a_batch_with_work_left_is_refused(self) -> None:
        state = ledger.CollectionState.load(self.backend)
        state.begin_batch("cwork", "inbox", self.ITEMS, at=FIXED_NOW)
        with self.assertRaises(ledger.LedgerError):
            state.finish_batch("cwork", "inbox", at=FIXED_NOW)

    def test_the_structural_verifier_catches_a_corrupted_cursor_file(self) -> None:
        self.assertTrue(ledger.verify_collection_state(self.backend).ok)
        payload = ledger.read_json(self.backend, ledger.COLLECTION_STATE_REL)
        payload["sources"]["cwork"]["inbox"] = {
            "cursor": "report-01",
            "collected": ["report-01", "report-01"],
            "pending": ["report-01"],
        }
        ledger.write_json(self.backend, ledger.COLLECTION_STATE_REL, payload)
        report = ledger.verify_collection_state(self.backend)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatched), 2)

    def test_a_missing_cursor_file_is_reported_not_invented(self) -> None:
        backend = MemoryBackend()
        report = ledger.verify_collection_state(backend)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing, [ledger.COLLECTION_STATE_REL])


# ── J8: wizard state machine ────────────────────────────────────────────────


class WizardStateTests(unittest.TestCase):
    """J8 — the happy path, the TTL, and every illegal transition."""

    HAPPY_PATH = ("verify-key", "set-sources", "preview", "ingest", "taxonomy", "activate")

    def fresh(self) -> ledger.WizardState:
        return ledger.WizardState(draft_id="draft-1", created_at=FIXED_NOW, updated_at=FIXED_NOW)

    def test_the_happy_path_reaches_active(self) -> None:
        state = self.fresh()
        moment = FIXED_NOW
        for verb in self.HAPPY_PATH:
            state.apply(verb, now=moment)
            moment += timedelta(minutes=1)
        self.assertEqual(state.state, "active")
        self.assertEqual([row["verb"] for row in state.history], list(self.HAPPY_PATH))

    def test_the_state_list_matches_the_contract(self) -> None:
        self.assertEqual(
            ledger.STATES,
            ("draft", "verified", "sourced", "previewed", "ingesting", "taxonomy", "active", "expired"),
        )

    def test_every_out_of_order_verb_is_refused(self) -> None:
        for skipped in range(1, len(self.HAPPY_PATH)):
            with self.subTest(verb=self.HAPPY_PATH[skipped]):
                state = self.fresh()
                with self.assertRaises(ledger.IllegalTransition):
                    state.apply(self.HAPPY_PATH[skipped], now=FIXED_NOW)

    def test_a_verb_cannot_be_replayed(self) -> None:
        state = self.fresh()
        state.apply("verify-key", now=FIXED_NOW)
        with self.assertRaises(ledger.IllegalTransition):
            state.apply("verify-key", now=FIXED_NOW)

    def test_active_is_terminal(self) -> None:
        state = self.fresh()
        for verb in self.HAPPY_PATH:
            state.apply(verb, now=FIXED_NOW)
        for verb in ledger.VERBS:
            with self.subTest(verb=verb):
                with self.assertRaises(ledger.IllegalTransition):
                    state.apply(verb, now=FIXED_NOW)

    def test_an_unknown_verb_is_refused(self) -> None:
        with self.assertRaises(ledger.IllegalTransition):
            self.fresh().apply("rebuild --raw", now=FIXED_NOW)

    def test_the_draft_ttl_is_thirty_minutes(self) -> None:
        self.assertEqual(ledger.DRAFT_TTL, timedelta(minutes=30))
        state = self.fresh()
        self.assertEqual(state.expires_at(), FIXED_NOW + timedelta(minutes=30))

    def test_a_draft_survives_right_up_to_the_ttl(self) -> None:
        state = self.fresh()
        state.apply("verify-key", now=FIXED_NOW + timedelta(minutes=29, seconds=59))
        self.assertEqual(state.state, "verified")

    def test_every_verb_is_refused_once_the_ttl_lapses(self) -> None:
        late = FIXED_NOW + timedelta(minutes=30, seconds=1)
        for verb in ledger.VERBS:
            with self.subTest(verb=verb):
                state = self.fresh()
                with self.assertRaises(ledger.DraftExpired):
                    state.apply(verb, now=late)
                self.assertEqual(state.state, "expired", "过期后状态必须落到 expired")

    def test_an_expired_draft_stays_expired(self) -> None:
        state = self.fresh()
        with self.assertRaises(ledger.DraftExpired):
            state.apply("verify-key", now=FIXED_NOW + timedelta(hours=1))
        with self.assertRaises(ledger.DraftExpired):
            state.apply("verify-key", now=FIXED_NOW)

    def test_a_verified_draft_no_longer_expires(self) -> None:
        state = self.fresh()
        state.apply("verify-key", now=FIXED_NOW)
        self.assertIsNone(state.expires_at())
        self.assertFalse(state.is_expired(FIXED_NOW + timedelta(days=7)))
        state.apply("set-sources", now=FIXED_NOW + timedelta(days=7))
        self.assertEqual(state.state, "sourced")

    def test_round_trips_through_json(self) -> None:
        state = self.fresh()
        state.apply("verify-key", now=FIXED_NOW)
        restored = ledger.WizardState.from_dict(state.as_dict())
        self.assertEqual(restored.state, "verified")
        self.assertEqual(restored.created_at, FIXED_NOW)
        self.assertEqual(restored.history, state.history)
        self.assertEqual(restored.ttl, ledger.DRAFT_TTL)


# ── doctor: the verify subcommand family ────────────────────────────────────


class DoctorTests(unittest.TestCase):
    """C 表 ``verify`` 动词：--raw / --manifest / --collection-state /
    --changed-paths / --tree each report independently."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "kb"
        self.backend = LocalFSBackend(self.root)
        built_kb(self.backend)

    def run_cli(self, *args) -> int:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = doctor.main(list(args))
        self.output = buffer.getvalue()
        return code

    def test_every_check_passes_on_a_fresh_library(self) -> None:
        self.assertEqual(self.run_cli("verify", "--all", "--root", str(self.root)), 0)
        for name in doctor.CHECKS:
            self.assertIn(f"verify --{name}", self.output)
        self.assertNotIn("FAIL", self.output)

    def test_manifest_check_fails_on_a_tampered_file(self) -> None:
        self.backend.write("wiki/index.md", "# 改过\n".encode("utf-8"))
        self.assertEqual(self.run_cli("verify", "--manifest", "--root", str(self.root)), 1)
        self.assertIn("FAIL", self.output)
        self.assertIn("wiki/index.md", self.output)

    def test_raw_check_fails_when_a_raw_file_is_not_in_the_raw_manifest(self) -> None:
        self.backend.write("raw/2026-09/report-01.md", b"# raw\n")
        self.assertEqual(self.run_cli("verify", "--raw", "--root", str(self.root)), 1)
        self.assertIn("raw/2026-09/report-01.md", self.output)

    def test_tree_check_fails_when_a_b_table_item_is_missing(self) -> None:
        self.backend.remove("kb_members.json")
        self.assertEqual(self.run_cli("verify", "--tree", "--root", str(self.root)), 1)
        self.assertIn("#23 kb_members.json", self.output)

    def test_changed_paths_check_fails_when_a_listed_path_vanished(self) -> None:
        ledger.record_changed_paths(
            self.backend, ["wiki/gone.md"], reason="nightly", at=FIXED_NOW
        )
        self.assertEqual(self.run_cli("verify", "--changed-paths", "--root", str(self.root)), 1)
        self.assertIn("wiki/gone.md", self.output)

    def test_collection_state_check_fails_on_an_inconsistent_cursor(self) -> None:
        payload = ledger.read_json(self.backend, ledger.COLLECTION_STATE_REL)
        payload["sources"]["cwork"]["inbox"]["collected"] = ["a", "a"]
        ledger.write_json(self.backend, ledger.COLLECTION_STATE_REL, payload)
        self.assertEqual(
            self.run_cli("verify", "--collection-state", "--root", str(self.root)), 1
        )
        self.assertIn("collected 有重复项", self.output)

    def test_selecting_no_check_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_cli("verify", "--root", str(self.root)), 2)
        self.assertIn("至少要选一项检查", self.output)

    def test_json_output_is_machine_readable(self) -> None:
        self.assertEqual(
            self.run_cli("verify", "--all", "--root", str(self.root), "--json"), 0
        )
        payload = ledger.loads(self.output.encode("utf-8"))
        self.assertEqual(sorted(payload), sorted(doctor.CHECKS))
        self.assertTrue(all(row["ok"] for row in payload.values()))

    def test_doctor_refuses_a_plaintext_credential_flag(self) -> None:
        self.assertEqual(
            self.run_cli("verify", "--all", "--root", str(self.root), "--token", "abc"), 2
        )
        self.assertIn("拒绝命令行明文凭据", self.output)


class MissingLedgerTests(unittest.TestCase):
    def test_loading_a_manifest_that_was_never_written_raises_not_found(self) -> None:
        with self.assertRaises(NotFound):
            ledger.load_manifest(MemoryBackend())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
