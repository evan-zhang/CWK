"""RT-042 迁移判据 J6：小样本（≥20 文件）双向配对零未配对；人为漏迁一个 →
未配对清单非空。

The fixture mirrors the real mirror layout closely enough to exercise every
rule: dated raw volumes, a raw manifest, timelines snapshots, wiki pages and
``wiki/_system`` indexes, plus the four retired directories that must not be
migrated.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_migrate as migrate  # noqa: E402
from kb_create import KbSpec, SourceSpec, create_kb  # noqa: E402
from kb_ledger import load_manifest, verify_manifest  # noqa: E402
from kb_storage import LocalFSBackend, MemoryBackend, UnsafePath  # noqa: E402

FIXED_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

# 24 migratable files + 4 retired ones.
MIRROR_FILES = {
    **{f"raw/2026-07/report-{n:02d}.md": f"# 七月 {n}\n" for n in range(1, 9)},
    **{f"raw/2026-08/report-{n:02d}.md": f"# 八月 {n}\n" for n in range(1, 9)},
    "raw/_system/raw-manifest.json": '{"schema": "cwk.raw-truth-source.v2", "entries": {}}\n',
    "timelines/report-01/snapshots/001.md": "# 快照 1\n",
    "timelines/report-01/snapshots/002.md": "# 快照 2\n",
    "wiki/index.md": "# 索引\n",
    "wiki/summaries/report-01.md": "# 摘要 1\n",
    "wiki/daily/2026-08-31.md": "# 日报\n",
    "wiki/_system/reply-state.json": '{"reports": {}}\n',
    "wiki/_system/search-index.json": '{"documents": []}\n',
}

RETIRED_FILES = {
    "entities/张三.md": "# 张三\n",
    "events/2026-08-01.md": "# 事件\n",
    "history/2026-07.md": "# 历史\n",
    "_index/all.json": "{}\n",
}


def build_mirror(root: Path) -> LocalFSBackend:
    backend = LocalFSBackend(root)
    for path, body in {**MIRROR_FILES, **RETIRED_FILES}.items():
        backend.write(path, body.encode("utf-8"))
    return backend


def fresh_kb(backend) -> None:
    create_kb(
        backend,
        KbSpec(
            display_name="迁移库",
            kb_code="c" * 32,
            owner_ref="owner-42",
            created_at=FIXED_NOW,
            sources=(SourceSpec("cwork"),),
        ),
    )


class PathMapTests(unittest.TestCase):
    def test_the_sample_is_at_least_twenty_files(self) -> None:
        self.assertGreaterEqual(len(MIRROR_FILES), 20)

    def test_longest_prefix_wins(self) -> None:
        self.assertEqual(
            migrate.map_path("wiki/_system/reply-state.json"), "_system/reply-state.json"
        )
        self.assertEqual(migrate.map_path("wiki/_system/other.json"), "_system/other.json")
        self.assertEqual(migrate.map_path("wiki/index.md"), "wiki/index.md")

    def test_raw_and_timelines_keep_their_shape(self) -> None:
        self.assertEqual(migrate.map_path("raw/2026-07/a.md"), "raw/2026-07/a.md")
        self.assertEqual(
            migrate.map_path("timelines/report-01/snapshots/001.md"),
            "timelines/report-01/snapshots/001.md",
        )

    def test_retired_directories_are_recognised(self) -> None:
        for path in RETIRED_FILES:
            with self.subTest(path=path):
                self.assertTrue(migrate.is_retired(path))
        self.assertFalse(migrate.is_retired("wiki/entities/张三.md"))

    def test_an_unmapped_path_is_reported_not_guessed(self) -> None:
        self.assertIsNone(migrate.map_path("scratch/notes.md"))

    def test_mapping_refuses_an_unsafe_source_path(self) -> None:
        with self.assertRaises(UnsafePath):
            migrate.map_path("../outside.md")


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = build_mirror(Path(self.tmp.name) / "mirror")
        self.plan = migrate.build_plan(self.source)

    def test_every_live_file_is_mapped_and_every_retired_one_is_dropped(self) -> None:
        self.assertEqual(len(self.plan.mapping), len(MIRROR_FILES))
        self.assertEqual(sorted(self.plan.retired), sorted(RETIRED_FILES))
        self.assertEqual(self.plan.unmapped, [])

    def test_the_plan_records_the_source_digest_for_every_mapped_file(self) -> None:
        self.assertEqual(sorted(self.plan.source_digests), sorted(self.plan.mapping))
        for path, digest in self.plan.source_digests.items():
            self.assertEqual(digest, self.source.sha256(path))

    def test_an_unmapped_file_shows_up_in_the_plan(self) -> None:
        self.source.write("scratch/notes.md", b"x")
        plan = migrate.build_plan(self.source)
        self.assertEqual(plan.unmapped, ["scratch/notes.md"])


class ReconcileTests(unittest.TestCase):
    """J6 — bidirectional pairing.  Zero unpaired when clean, non-empty when not."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = build_mirror(Path(self.tmp.name) / "mirror")
        self.dest = MemoryBackend()
        fresh_kb(self.dest)
        self.plan = migrate.build_plan(self.source)
        migrate.apply_plan(self.source, self.dest, self.plan)
        self.allow_new = migrate.skeleton_allow_list(self.dest, self.plan)

    def reconcile(self, **kwargs):
        params = {"allowed_new": self.allow_new}
        params.update(kwargs)
        return migrate.reconcile(self.source, self.dest, self.plan, **params)

    def test_a_complete_migration_pairs_in_both_directions(self) -> None:
        report = self.reconcile()
        self.assertTrue(report.ok, report.describe())
        self.assertEqual(len(report.paired), len(MIRROR_FILES))
        self.assertEqual(report.unpaired_source, [])
        self.assertEqual(report.unpaired_dest, [])
        self.assertEqual(sorted(report.retired_skipped), sorted(RETIRED_FILES))

    def test_dropping_one_file_leaves_the_unpaired_list_non_empty(self) -> None:
        """J6 红法：人为漏迁一个 → 未配对清单必须非空。"""
        self.dest.remove("raw/2026-07/report-03.md")
        report = self.reconcile()
        self.assertFalse(report.ok)
        self.assertEqual(report.unpaired_source, ["raw/2026-07/report-03.md"])
        self.assertIn("源侧未配对", report.describe())

    def test_a_corrupted_copy_is_caught_by_the_content_hash(self) -> None:
        self.dest.write("wiki/index.md", "# 索引（被改）\n".encode("utf-8"))
        report = self.reconcile()
        self.assertFalse(report.ok)
        self.assertEqual(report.digest_mismatch, ["wiki/index.md → wiki/index.md"])

    def test_an_unexplained_destination_file_is_caught_in_the_reverse_direction(self) -> None:
        self.dest.write("wiki/summaries/invented.md", b"x")
        report = self.reconcile()
        self.assertFalse(report.ok)
        self.assertEqual(report.unpaired_dest, ["wiki/summaries/invented.md"])

    def test_the_allow_new_list_forgives_only_what_it_names(self) -> None:
        self.dest.write("wiki/summaries/invented.md", b"x")
        report = self.reconcile(allowed_new=self.allow_new + ["wiki/summaries/invented.md"])
        self.assertTrue(report.ok, report.describe())

    def test_allow_new_accepts_a_whole_subtree_with_a_trailing_slash(self) -> None:
        self.dest.write("wiki/generated/a.md", b"a")
        self.dest.write("wiki/generated/b.md", b"b")
        report = self.reconcile(allowed_new=self.allow_new + ["wiki/generated/"])
        self.assertTrue(report.ok, report.describe())

    def test_a_declared_rename_pairs_across_the_new_path(self) -> None:
        self.dest.remove("wiki/index.md")
        self.dest.write("wiki/README.md", MIRROR_FILES["wiki/index.md"].encode("utf-8"))
        report = self.reconcile(
            allowed_new=self.allow_new,
            allowed_renames=[("wiki/index.md", "wiki/README.md")],
        )
        self.assertTrue(report.ok, report.describe())
        self.assertIn(("wiki/index.md", "wiki/README.md"), report.paired)

    def test_an_undeclared_rename_is_unpaired_on_both_sides(self) -> None:
        self.dest.remove("wiki/index.md")
        self.dest.write("wiki/README.md", MIRROR_FILES["wiki/index.md"].encode("utf-8"))
        report = self.reconcile()
        self.assertFalse(report.ok)
        self.assertEqual(report.unpaired_source, ["wiki/index.md"])
        self.assertEqual(report.unpaired_dest, ["wiki/README.md"])

    def test_retired_directories_never_reach_the_destination(self) -> None:
        for path in RETIRED_FILES:
            with self.subTest(path=path):
                self.assertFalse(self.dest.exists(path))

    def test_applying_the_plan_twice_converges(self) -> None:
        before = {path: self.dest.sha256(path) for path in self.dest.walk_files(".")}
        migrate.apply_plan(self.source, self.dest, self.plan)
        after = {path: self.dest.sha256(path) for path in self.dest.walk_files(".")}
        self.assertEqual(before, after)
        self.assertTrue(self.reconcile().ok)


class ApplyPreflightTests(unittest.TestCase):
    """迁移必须先预检目的地，不许「先破坏后报错」。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = build_mirror(Path(self.tmp.name) / "mirror")
        self.dest = MemoryBackend()
        fresh_kb(self.dest)
        self.plan = migrate.build_plan(self.source)

    def snapshot(self) -> dict:
        return {path: self.dest.sha256(path) for path in self.dest.walk_files(".")}

    def test_a_target_the_ledger_never_recorded_is_refused_with_zero_writes(self) -> None:
        """红法：目的地已有账本不认的同名文件 → 拒绝，且零写入。"""
        self.dest.write("raw/2026-07/report-03.md", "# 别人先放的\n".encode("utf-8"))
        before = self.snapshot()
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.apply_plan(self.source, self.dest, self.plan)
        self.assertIn("零写入", str(ctx.exception))
        self.assertIn("账本未登记", str(ctx.exception))
        self.assertEqual(self.snapshot(), before, "预检失败却已经写了一部分")
        self.assertFalse(self.dest.exists("raw/2026-07/report-01.md"))

    def test_a_destination_that_drifted_from_its_ledger_is_refused(self) -> None:
        self.dest.write("wiki/index.md", "# 手改过的骨架\n".encode("utf-8"))
        before = self.snapshot()
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.apply_plan(self.source, self.dest, self.plan)
        self.assertIn("内容已偏离账本", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def test_an_explicitly_allowed_overwrite_goes_through(self) -> None:
        self.dest.write("raw/2026-07/report-03.md", "# 别人先放的\n".encode("utf-8"))
        written = migrate.apply_plan(
            self.source,
            self.dest,
            self.plan,
            allowed_overwrite=["raw/2026-07/report-03.md"],
        )
        self.assertIn("raw/2026-07/report-03.md", written)
        self.assertEqual(
            self.dest.read("raw/2026-07/report-03.md"),
            MIRROR_FILES["raw/2026-07/report-03.md"].encode("utf-8"),
        )

    def test_apply_refreshes_the_destination_ledger_before_returning(self) -> None:
        """迁移后账本必须在同一函数内刷新，不留给下一条命令。"""
        written = migrate.apply_plan(self.source, self.dest, self.plan)
        report = verify_manifest(self.dest)
        self.assertTrue(report.ok, report.describe())
        manifest = load_manifest(self.dest)
        self.assertEqual(manifest["kb_code"], "c" * 32)
        for path in written:
            with self.subTest(path=path):
                self.assertIn(path, manifest["entries"])

    def test_a_second_apply_writes_nothing_and_leaves_the_ledger_alone(self) -> None:
        migrate.apply_plan(self.source, self.dest, self.plan)
        before = self.snapshot()
        self.assertEqual(migrate.apply_plan(self.source, self.dest, self.plan), [])
        self.assertEqual(self.snapshot(), before)

    def test_a_destination_without_a_library_is_refused_by_name(self) -> None:
        bare = MemoryBackend()
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.apply_plan(self.source, bare, self.plan)
        self.assertIn("kb.json", str(ctx.exception))


class MigrateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mirror = Path(self.tmp.name) / "mirror"
        build_mirror(self.mirror)
        self.dest = Path(self.tmp.name) / "kb"
        fresh_kb(LocalFSBackend(self.dest))

    def run_cli(self, *args) -> int:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = migrate.main(list(args))
        self.output = buffer.getvalue()
        return code

    def test_plan_apply_reconcile_round_trip(self) -> None:
        self.assertEqual(
            self.run_cli("plan", "--source-root", str(self.mirror), "--dest-root", str(self.dest)),
            0,
        )
        self.assertIn('"mapped_count": 24', self.output)
        self.assertEqual(
            self.run_cli("apply", "--source-root", str(self.mirror), "--dest-root", str(self.dest)),
            0,
        )
        backend = LocalFSBackend(self.dest)
        plan = migrate.build_plan(LocalFSBackend(self.mirror))
        allow = ",".join(migrate.skeleton_allow_list(backend, plan))
        self.assertEqual(
            self.run_cli(
                "reconcile",
                "--source-root",
                str(self.mirror),
                "--dest-root",
                str(self.dest),
                "--allow-new",
                allow,
            ),
            0,
        )
        self.assertIn("迁移对账通过", self.output)

    def test_reconcile_exits_non_zero_when_a_file_is_missing(self) -> None:
        self.run_cli("apply", "--source-root", str(self.mirror), "--dest-root", str(self.dest))
        backend = LocalFSBackend(self.dest)
        plan = migrate.build_plan(LocalFSBackend(self.mirror))
        allow = ",".join(migrate.skeleton_allow_list(backend, plan))
        backend.remove("raw/2026-08/report-05.md")
        code = self.run_cli(
            "reconcile",
            "--source-root",
            str(self.mirror),
            "--dest-root",
            str(self.dest),
            "--allow-new",
            allow,
        )
        self.assertEqual(code, 1)
        self.assertIn("源侧未配对", self.output)

    def test_a_malformed_rename_pair_is_refused(self) -> None:
        with self.assertRaises(migrate.MigrationError):
            migrate.parse_renames(["not-a-pair"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
