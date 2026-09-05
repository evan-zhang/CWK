"""RT-043 摄取管道判据：lineage 寻址、格式工厂、源适配器、计划与确认卡。

判据编号对应 RT/RT-043/rt-lite.md：

- J1 originals write-once：同源同件重复摄取幂等（零写入、零账变）
- J2 覆盖率对账：手工删掉一个 raw 产物 → reconcile 必红
- J3 raw-index 原子写：写中断后旧 index 完整可用
- J4 状态账与断点续跑：failed 件重跑只处理未完成
- J5 源故障必红：源目录消失 / 适配器 5xx → 批次红且已完成件保留
- J6 lineage 寻址：同 ID 第二版 → 同条目 versions+1、supersedes 链正确

全部离线：源用临时目录 fake，DocDB 用 fake subprocess，后端用 LocalFSBackend /
MemoryBackend。没有任何一条测试会连真 NAS 或真 DocDB。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_create as create  # noqa: E402
import kb_ingest as ingest  # noqa: E402
from kb_storage import LocalFSBackend  # noqa: E402

FIXED_NOW = datetime(2026, 9, 5, 3, 0, 0, tzinfo=timezone.utc)


# ── shared fixtures ─────────────────────────────────────────────────────────


def make_kb(root: Path, sources=("cwork",)) -> str:
    """Build a real RT-042 library so the tests exercise the actual floor."""
    spec = create.KbSpec(
        display_name="摄取测试库",
        kb_code="b" * 32,
        owner_ref="owner-43",
        created_at=FIXED_NOW,
        sources=tuple(create.SourceSpec(source_type=name) for name in sources),
    )
    create.create_kb(LocalFSBackend(root), spec)
    return spec.kb_code


def write_mirror(root: Path, rel: str, body: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def run_cli(argv) -> tuple:
    """Run the CLI and return ``(exit_code, parsed_stdout)``."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ingest.main(argv)
    text = buffer.getvalue()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic aid
        raise AssertionError(f"CLI 没有输出单个 JSON 对象：{text[:400]}（{exc}）")
    return code, payload


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeDocdb:
    """A stand-in for ``scripts/browse/browse.py`` and friends.

    ``folders`` maps a folder id to the rows browse.py would print.  Setting
    ``fail_with`` makes every call answer with that resultMsg, which is how
    the 5xx criterion is driven without a network.
    """

    def __init__(self, folders: dict, fail_with: str = "") -> None:
        self.folders = folders
        self.fail_with = fail_with
        self.calls: list = []

    def __call__(self, cmd, cwd=None, env=None, text=None, capture_output=None):
        self.calls.append(list(cmd))
        script = Path(cmd[1]).name
        if self.fail_with:
            return FakeProc(
                0,
                json.dumps({"resultCode": 0, "resultMsg": self.fail_with, "data": None}),
            )
        if script == "browse.py":
            rows = self.folders.get(str(cmd[2]), [])
            return FakeProc(0, json.dumps({"resultCode": 1, "resultMsg": "ok", "data": rows}))
        raise AssertionError(f"fake 没有实现 {script}")


# ── lineage identity ────────────────────────────────────────────────────────


class LineageAddressingTests(unittest.TestCase):
    """J6 前置：键的形状。键错了，版本链根本不会形成。"""

    def test_j6_lineage_key_is_source_colon_stable_id(self) -> None:
        self.assertEqual(ingest.lineage_id("cwork", "2095046023776104449"),
                         "cwork:2095046023776104449")
        self.assertEqual(ingest.lineage_id("docdb", "2087519593823322113"),
                         "docdb:2087519593823322113")

    def test_j6_lineage_key_refuses_rev_and_seq_suffixes(self) -> None:
        # 负例：把快照号写进键。这些串在第一次摄取时看起来完全正常，
        # 第二版到达时才暴露——那时版本链已经错过了。
        for bad in ("docdb:2087@7", "cwork:2095~2", "docdb:2087.v2", "cwork:a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ingest.IngestError):
                    ingest.assert_lineage_key(bad)

    def test_j6_split_item_anchor_is_still_an_identity(self) -> None:
        # DOCDB-INGEST-DESIGN §II：拆分件 <fileId>#<内容锚点slug> 是身份的一部分，
        # 不是版本，必须放行——否则 docdb 拆分件永远进不了索引。
        self.assertEqual(
            ingest.assert_lineage_key("docdb:2087519593823322113#第三章-验收"),
            "docdb:2087519593823322113#第三章-验收",
        )

    def test_lineage_key_refuses_unknown_source_and_empty_id(self) -> None:
        for bad in ("mystery:123", "cwork:", "no-colon", "a:b:c"):
            with self.subTest(bad=bad):
                with self.assertRaises(ingest.IngestError):
                    ingest.assert_lineage_key(bad)


class StableIdTests(unittest.TestCase):
    def test_report_id_is_the_leading_digit_run(self) -> None:
        self.assertEqual(
            ingest.stable_id_from_name("2095046023776104449-周报.md"), "2095046023776104449"
        )

    def test_a_date_prefixed_name_is_not_mistaken_for_an_id(self) -> None:
        # 坏情形：把 2026-08-14-周报.md 认成 report 2026。那样同一年的
        # 每一件都会挤进同一个 lineage，第二件直接覆盖第一件。
        self.assertIsNone(ingest.stable_id_from_name("2026-08-14-周报.md"))
        self.assertIsNone(ingest.stable_id_from_name("README.md"))


class DateDerivationTests(unittest.TestCase):
    def test_written_date_beats_file_mtime(self) -> None:
        # mtime 指向 2020 年，路径写着 2026-08-14。复制一份镜像会重写
        # 每个 mtime；照 mtime 落位会把整个档案重新归到复制那天。
        mtime = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            ingest.derive_date("2026-08/2026-08-14/2095046023776104449-周报.md", mtime),
            "2026-08-14",
        )

    def test_month_directory_falls_back_to_the_first_of_the_month(self) -> None:
        self.assertEqual(
            ingest.derive_date("2026-08/2095046023776104449-周报.md", None), "2026-08-01"
        )

    def test_mtime_is_the_last_resort(self) -> None:
        mtime = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ingest.derive_date("flat/2095046023776104449-周报.md", mtime),
                         "2026-07-09")

    def test_no_date_anywhere_yields_none(self) -> None:
        self.assertIsNone(ingest.derive_date("flat/2095046023776104449-周报.md", None))


class SlugTests(unittest.TestCase):
    def test_slug_keeps_cjk_and_drops_path_characters(self) -> None:
        self.assertEqual(ingest.slugify("八月/周报 v2"), "八月-周报-v2")
        self.assertEqual(ingest.slugify("../../etc/passwd"), "etc-passwd")

    def test_slug_never_returns_empty(self) -> None:
        self.assertEqual(ingest.slugify("///"), "untitled")
        self.assertEqual(ingest.slugify(""), "untitled")


# ── format factory (decision half) ──────────────────────────────────────────


class FormatFactoryDecisionTests(unittest.TestCase):
    """DOCDB-INGEST-DESIGN §V 的 v1 范围表，逐行。"""

    def test_markdown_and_text_pass_through(self) -> None:
        for name in ("a.md", "b.markdown", "c.txt"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual((decision.handling, decision.expected_status),
                             ("passthrough", "converted"), name)

    def test_docx_converts_when_the_host_binary_exists_and_degrades_when_it_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            converter = Path(tmp) / "md2md"
            converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            converter.chmod(0o755)
            good = ingest.decide_format("x.docx", env={ingest.ENV_DOCX_CONVERTER: str(converter)})
            self.assertEqual((good.handling, good.expected_status), ("docx-convert", "converted"))
        missing = ingest.decide_format(
            "x.docx", env={ingest.ENV_DOCX_CONVERTER: str(Path(tmp) / "gone")}
        )
        self.assertEqual((missing.handling, missing.expected_status),
                         ("placeholder", "placeholder"))

    def test_xlsx_follows_openpyxl_availability(self) -> None:
        with_lib = ingest.decide_format("s.xlsx", env={}, has_openpyxl=True)
        without = ingest.decide_format("s.xlsx", env={}, has_openpyxl=False)
        self.assertEqual(with_lib.handling, "xlsx-csv")
        self.assertEqual(without.handling, "placeholder")

    def test_pptx_images_and_zip_are_placeholders(self) -> None:
        for name in ("deck.pptx", "photo.png", "scan.JPG", "bundle.zip"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual(decision.expected_status, "placeholder", name)

    def test_rar_and_7z_are_skipped_not_placeheld(self) -> None:
        for name in ("archive.rar", "archive.7z"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual((decision.handling, decision.expected_status), ("skip", "skipped"),
                             name)

    def test_unknown_extension_takes_the_generic_placeholder_path(self) -> None:
        decision = ingest.decide_format("mystery.qqq", env={})
        self.assertEqual((decision.format, decision.expected_status), ("unknown", "placeholder"))


# ── routing ─────────────────────────────────────────────────────────────────


class RoutePlacementTests(unittest.TestCase):
    def test_timeline_places_by_month_and_day(self) -> None:
        self.assertEqual(
            ingest.raw_path_for(
                route_mode="timeline", lineage="cwork:2095046023776104449",
                stable_id="2095046023776104449", name="2095046023776104449-八月周报.md",
                group="2026-08-14", date="2026-08-14",
            ),
            "raw/2026-08/2026-08-14/2095046023776104449-八月周报.md",
        )

    def test_classify_places_under_the_source_directory_name(self) -> None:
        self.assertEqual(
            ingest.raw_path_for(
                route_mode="classify", lineage="docdb:2087",
                stable_id="20875195938", name="20875195938-合同.docx",
                group="玄关合同", date="2026-08-14",
            ),
            "raw/classify/玄关合同/20875195938-合同.md",
        )

    def test_timeline_without_a_date_uses_a_named_bucket_not_today(self) -> None:
        # 坏情形：无日期件落到"今天"。那样同一件重跑两次会落到两个路径，
        # 幂等判据全绿而 raw 里多出一份孤儿。
        path = ingest.raw_path_for(
            route_mode="timeline", lineage="cwork:2095046023776104449",
            stable_id="2095046023776104449", name="2095046023776104449-无日期.md",
            group="flat", date=None,
        )
        self.assertEqual(path, f"raw/{ingest.UNDATED_BUCKET}/2095046023776104449-无日期.md")

    def test_originals_path_depends_only_on_identity_and_bytes(self) -> None:
        # J1 的前置条件：档案路径不含日期、不含计数器。含了就会
        # "写一次"变成"每次 mtime 漂移写一次"。
        first = ingest.originals_path_for("cwork", "2095046023776104449", "a" * 64, "x.md")
        second = ingest.originals_path_for("cwork", "2095046023776104449", "a" * 64, "x.md")
        self.assertEqual(first, second)
        self.assertEqual(first, "originals/cwork/2095046023776104449/" + "a" * 64 + ".md")
        self.assertNotIn("2026", first)


# ── source adapter: cwork-mirror ────────────────────────────────────────────


class CworkMirrorAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mirror"
        write_mirror(self.root, "2026-08/2026-08-14/2095046023776104449-八月周报.md", b"# 8\n")
        write_mirror(self.root, "2026-06/2026-06-02/2095046023776104450-六月周报.md", b"# 6\n")
        write_mirror(self.root, "2026-08/2026-08-14/README.md", b"readme\n")

    def test_scan_returns_identified_items_sorted_by_date(self) -> None:
        items, unidentified = ingest.scan_cwork_mirror(self.root)
        self.assertEqual([item.stable_id for item in items],
                         ["2095046023776104450", "2095046023776104449"])
        self.assertEqual([item.date for item in items], ["2026-06-02", "2026-08-14"])

    def test_files_without_a_stable_id_are_reported_never_dropped(self) -> None:
        # 静默丢件是 §VI 存在的理由：扫描器不认识的东西必须留下痕迹。
        _, unidentified = ingest.scan_cwork_mirror(self.root)
        self.assertEqual(unidentified, ["2026-08/2026-08-14/README.md"])

    def test_since_filters_on_the_derived_date(self) -> None:
        items, _ = ingest.scan_cwork_mirror(self.root, since="2026-07-01")
        self.assertEqual([item.stable_id for item in items], ["2095046023776104449"])

    def test_j5_missing_source_directory_raises_instead_of_returning_empty(self) -> None:
        # 负例：源目录消失时返回空列表。那样"零件可摄取"和"源没了"
        # 长得一模一样，批次会全绿收工。
        with self.assertRaises(ingest.SourceError):
            ingest.scan_cwork_mirror(self.root.parent / "gone")


# ── source adapter: docdb ───────────────────────────────────────────────────


class DocdbAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        skill = Path(self.tmp.name) / "cms-docdb"
        (skill / "scripts" / "browse").mkdir(parents=True)
        (skill / "scripts" / "browse" / "browse.py").write_text("", encoding="utf-8")
        self.env = {
            ingest.ENV_DOCDB_SKILL_DIR: str(skill),
            "XG_BIZ_API_KEY": "fake-key-not-a-real-secret",
        }
        self.folders = {
            "100": [
                {"id": "200", "name": "合同", "type": "1"},
                {"fileId": "301", "name": "2026 年度计划.docx", "type": "2",
                 "updateTime": "2026-08-14 09:30:00", "size": "2048"},
            ],
            "200": [
                {"fileId": "302", "name": "供应商合同.pdf", "type": "2",
                 "updateTime": "1754000000000"},
            ],
        }

    def test_scan_walks_folders_and_uses_file_id_as_the_stable_id(self) -> None:
        fake = FakeDocdb(self.folders)
        items, _ = ingest.scan_docdb("100", env=self.env, retries=1,
                                     sleep=lambda _s: None, runner=fake)
        self.assertEqual(sorted(item.stable_id for item in items), ["301", "302"])
        by_id = {item.stable_id: item for item in items}
        self.assertEqual(by_id["301"].date, "2026-08-14")
        self.assertEqual(by_id["301"].group, "100")
        self.assertEqual(by_id["302"].group, "合同")

    def test_j5_adapter_5xx_raises_after_retries_instead_of_yielding_no_items(self) -> None:
        # 负例：把 500 当成"这个目录是空的"。那样源故障会伪装成
        # "没有新件"，批次全绿而整批数据没进来。
        fake = FakeDocdb(self.folders, fail_with="500 Internal Server Error")
        with self.assertRaises(ingest.SourceError) as ctx:
            ingest.scan_docdb("100", env=self.env, retries=2, sleep=lambda _s: None, runner=fake)
        self.assertIn("500", str(ctx.exception))
        self.assertEqual(len(fake.calls), 2, "瞬时错误应当按 retries 重试")

    def test_permanent_error_is_not_retried(self) -> None:
        fake = FakeDocdb(self.folders, fail_with="403 forbidden: no access to this folder")
        with self.assertRaises(ingest.SourceError):
            ingest.scan_docdb("100", env=self.env, retries=3, sleep=lambda _s: None, runner=fake)
        self.assertEqual(len(fake.calls), 1, "永久错误重试只会把清楚的失败拖慢")

    def test_credentials_come_from_the_environment_only(self) -> None:
        with self.assertRaises(ingest.SourceError) as ctx:
            ingest.docdb_env({ingest.ENV_DOCDB_SKILL_DIR: "/tmp"})
        self.assertIn("XG_BIZ_API_KEY", str(ctx.exception))


class CredentialFlagTests(unittest.TestCase):
    def test_a_secret_on_the_command_line_is_refused_before_anything_runs(self) -> None:
        code, payload = run_cli(["plan", "--source", "cwork-mirror", "--root", "/tmp",
                                 "--kb-root", "/tmp", "--token", "s3cret"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])


# ── plan + confirmation cards ───────────────────────────────────────────────


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.kb = self.base / "kb"
        self.kb_code = make_kb(self.kb)
        self.mirror = self.base / "mirror"
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104449-八月周报.md", b"# 8\n")
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104451-附件.rar", b"RAR\n")
        write_mirror(self.mirror, "2026-08/2026-08-14/notes.md", b"stray\n")

    def plan(self, *extra) -> dict:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(self.kb), *extra]
        )
        self.assertEqual(code, 0, payload)
        return payload

    def test_plan_defaults_route_by_source(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["route"]["mode"], "timeline")
        self.assertEqual(payload["route"]["default_for_source"], "timeline")
        self.assertEqual(payload["kb_code"], self.kb_code)

    def test_docdb_defaults_to_classify(self) -> None:
        self.assertEqual(ingest.DEFAULT_ROUTE["docdb"], "classify")

    def test_route_override_changes_every_proposed_path(self) -> None:
        payload = self.plan("--route", "classify")
        for row in payload["items"]:
            self.assertTrue(row["confirmation"]["proposed_raw_path"].startswith("raw/classify/"))

    def test_each_item_carries_a_confirmation_card(self) -> None:
        payload = self.plan()
        card = next(
            row["confirmation"] for row in payload["items"]
            if row["stable_id"] == "2095046023776104449"
        )
        self.assertEqual(card["lineage_id"], "cwork:2095046023776104449")
        self.assertEqual(
            card["proposed_raw_path"],
            "raw/2026-08/2026-08-14/2095046023776104449-八月周报.md",
        )
        self.assertEqual(card["format"]["handling"], "passthrough")
        self.assertEqual(sorted(card["editable"]), ["proposed_raw_path", "route_mode"])

    def test_plan_reports_unidentified_files(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["unidentified"], ["2026-08/2026-08-14/notes.md"])

    def test_plan_counts_expected_statuses(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["expected_status_counts"],
                         {"converted": 1, "skipped": 1})

    def test_since_is_validated_before_the_source_is_touched(self) -> None:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(self.kb), "--since", "2026/08/01"]
        )
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", payload["error"])

    def test_plan_can_be_written_to_a_file_and_read_back(self) -> None:
        out = self.base / "plan.json"
        self.plan("--out", str(out))
        loaded = ingest.load_plan(out)
        self.assertEqual(loaded["schema"], ingest.PLAN_SCHEMA)

    def test_j5_plan_against_a_missing_source_is_red_and_says_so(self) -> None:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.base / "gone"),
             "--kb-root", str(self.kb)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "SourceError")

    def test_plan_refuses_a_kb_root_that_is_not_a_library(self) -> None:
        empty = self.base / "not-a-kb"
        empty.mkdir()
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(empty)]
        )
        self.assertEqual(code, 2)
        self.assertIn("kb.json", payload["error"])


class PlanValidationTests(unittest.TestCase):
    """一份被编辑过的计划是输入，不是权威——落盘前必须再验一次。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plan.json"

    def write(self, payload: dict) -> Path:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.path

    def base_plan(self, **card) -> dict:
        confirmation = {
            "lineage_id": "cwork:2095046023776104449",
            "route_mode": "timeline",
            "proposed_raw_path": "raw/2026-08/2026-08-14/2095046023776104449-x.md",
        }
        confirmation.update(card)
        return {
            "schema": ingest.PLAN_SCHEMA,
            "adapter": "cwork-mirror",
            "source": "cwork",
            "kb_root": "/tmp/kb",
            "items": [{"stable_id": "2095046023776104449", "confirmation": confirmation}],
        }

    def test_a_good_plan_loads(self) -> None:
        self.assertEqual(len(ingest.load_plan(self.write(self.base_plan()))["items"]), 1)

    def test_an_edited_card_cannot_place_an_artefact_outside_the_library(self) -> None:
        # 负例：确认卡可改 → 改成 ../../etc/cron.d/x。卡是操作员编辑过的输入，
        # 不能当权威直接落盘。
        with self.assertRaises(Exception):
            ingest.load_plan(self.write(self.base_plan(proposed_raw_path="../../etc/x.md")))

    def test_an_unknown_route_mode_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(self.base_plan(route_mode="magic")))

    def test_a_card_with_a_rev_in_its_key_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(self.base_plan(lineage_id="docdb:2087@7")))

    def test_a_foreign_schema_is_refused(self) -> None:
        payload = self.base_plan()
        payload["schema"] = "something.else.v1"
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(payload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
