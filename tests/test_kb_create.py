"""RT-042 建库判据：B 表 26 项目录树、128 位 kb_code、子配置唯一权威、v1 拒收参数。

These are the create-side halves of the acceptance contract:
KB-PARAMETERS §A1 (identity), §A2 (sources, including the v1-rejected
parameters) and §B (the 30-item v1.3 tree with its 适用源 column).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_create as create  # noqa: E402
from kb_ledger import (  # noqa: E402
    EXCLUDED_PATHS,
    LedgerViolation,
    loads,
    verify_manifest,
)
from kb_storage import LocalFSBackend, MemoryBackend  # noqa: E402

FIXED_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def spec(**kwargs) -> create.KbSpec:
    params = {
        "display_name": "工作库",
        "kb_code": "a" * 32,
        "owner_ref": "owner-42",
        "created_at": FIXED_NOW,
        "sources": (create.SourceSpec(source_type="cwork"),),
    }
    params.update(kwargs)
    return create.KbSpec(**params)


DOCDB_SOURCE = create.SourceSpec(source_type="docdb", docdb_root="/玄关/合同")


class TreeTableTests(unittest.TestCase):
    """The B table itself, before anything is built."""

    def test_the_table_has_exactly_30_rows_with_unique_numbers_and_paths(self) -> None:
        self.assertEqual(len(create.KB_TREE), 30)
        numbers = [item.item for item in create.KB_TREE]
        paths = [item.path for item in create.KB_TREE]
        self.assertEqual(len(set(numbers)), 30, "B 表编号有重复")
        self.assertEqual(len(set(paths)), 30, "B 表路径有重复")
        self.assertIn("2c", numbers, "v1.3 新增的 #2c originals 必须在表内")
        self.assertIn("28", numbers, "v1.3 raw-index 必须在表内")
        self.assertIn("24", numbers)
        self.assertIn("25", numbers)

    def test_source_applicability_matches_the_contract_column(self) -> None:
        by_number = {item.item: item for item in create.KB_TREE}
        # v1.3: #2 raw 对两源通用（统一分类路由）；cwork-only 仅 #4 timelines、#18 reply-state.
        self.assertEqual(by_number["2"].sources, ())
        self.assertEqual(by_number["2c"].sources, ())
        self.assertEqual(by_number["4"].sources, ("cwork",))
        self.assertEqual(by_number["18"].sources, ("cwork",))
        # v1.3: 无 docdb-only 行（路由与源无关后 docdb 专用目录不存在）
        self.assertFalse(any(item.sources == ("docdb",) for item in create.KB_TREE))
        # 其余 28 行通用.
        common = [item.item for item in create.KB_TREE if not item.sources]
        self.assertEqual(len(common), 28)

    def test_tree_is_trimmed_per_configured_source(self) -> None:
        self.assertEqual(len(create.tree_for(("cwork",))), 30)
        self.assertEqual(len(create.tree_for(("docdb",))), 28)
        self.assertEqual(len(create.tree_for(("cwork", "docdb"))), 30)


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, source_spec=None, **kwargs):
        backend = LocalFSBackend(Path(self.tmp.name) / kwargs.pop("dirname", "kb"))
        built = create.create_kb(
            backend, spec(sources=source_spec or (create.SourceSpec("cwork"),), **kwargs)
        )
        return backend, built

    def test_every_applicable_item_exists_and_is_non_empty(self) -> None:
        for sources, label in (
            ((create.SourceSpec("cwork"),), "cwork"),
            ((DOCDB_SOURCE,), "docdb"),
            ((create.SourceSpec("cwork"), DOCDB_SOURCE), "both"),
        ):
            with self.subTest(sources=label):
                backend, built = self.build(sources, dirname=f"kb-{label}")
                self.assertEqual(create.audit_tree(backend, [s.source_type for s in sources]), [])
                self.assertEqual(len(built.tree_items), len(create.tree_for([s.source_type for s in sources])))

    def test_cwork_only_items_are_absent_from_a_docdb_library(self) -> None:
        backend, _ = self.build((DOCDB_SOURCE,), dirname="kb-docdb-only")
        for path in ("timelines", "_system/reply-state.json"):
            with self.subTest(path=path):
                self.assertFalse(backend.exists(path), f"纯 docdb 库不该有 {path}")
        self.assertTrue(backend.exists("raw"), "v1.3: raw 对 docdb 库同样存在（统一分类路由）")
        self.assertTrue(backend.exists("originals"), "v1.3: 存档层两源通用")

    def test_docdb_only_item_is_absent_from_a_cwork_library(self) -> None:
        backend, _ = self.build(dirname="kb-cwork-only")
        self.assertTrue(backend.exists("raw"))
        self.assertTrue(backend.exists("originals"), "v1.3: cwork 库同样有存档层")
        self.assertTrue(backend.exists("timelines"))

    def test_manifest_covers_the_build_and_verifies_clean(self) -> None:
        backend, built = self.build()
        self.assertTrue(verify_manifest(backend).ok)
        ledgered = [p for p in built.created_files if p not in EXCLUDED_PATHS]
        self.assertEqual(built.manifest["entry_count"], len(ledgered))
        self.assertEqual(sorted(built.manifest["entries"]), sorted(ledgered))
        self.assertEqual(built.manifest["kb_code"], "a" * 32)
        # The exclusions are the runtime-state files, and nothing else.
        self.assertEqual(
            sorted(set(built.created_files) - set(ledgered)),
            sorted(p for p in EXCLUDED_PATHS if p in built.created_files),
        )

    def test_build_is_deterministic_for_a_fixed_spec(self) -> None:
        left = MemoryBackend()
        right = MemoryBackend()
        create.create_kb(left, spec())
        create.create_kb(right, spec())
        stable = [p for p in left.walk_files(".") if p not in ("wiki/log.md", "audit.jsonl", "root-manifest.json")]
        for path in stable:
            with self.subTest(path=path):
                self.assertEqual(left.sha256(path), right.sha256(path))


class DestinationPreflightTests(unittest.TestCase):
    """建库必须先预检目标根，不许「先破坏后报错」。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "kb"
        self.backend = LocalFSBackend(self.root)
        create.create_kb(self.backend, spec())
        self.before = {
            path: self.backend.sha256(path) for path in self.backend.walk_files(".")
        }

    def snapshot(self) -> dict:
        return {path: self.backend.sha256(path) for path in self.backend.walk_files(".")}

    def test_building_into_an_existing_library_is_refused(self) -> None:
        """红法：指向已建库 → 拒绝，且库内容一个字节都没变。"""
        second = spec(display_name="另一个库", kb_code="e" * 32)
        with self.assertRaises(LedgerViolation) as ctx:
            create.create_kb(self.backend, second)
        self.assertIn("零写入", str(ctx.exception))
        self.assertEqual(self.snapshot(), self.before, "被拒绝的建库改动了既有库")

    def test_the_refusal_happens_before_the_first_write(self) -> None:
        """A single stray file is enough; the check is not about kb.json only."""
        empty = LocalFSBackend(Path(self.tmp.name) / "half")
        empty.write("wiki/index.md", "# 我先写的\n".encode("utf-8"))
        before = {path: empty.sha256(path) for path in empty.walk_files(".")}
        with self.assertRaises(LedgerViolation):
            create.create_kb(empty, spec())
        self.assertEqual(
            {path: empty.sha256(path) for path in empty.walk_files(".")},
            before,
            "预检失败却已经落了盘",
        )
        self.assertFalse(empty.exists("kb.json"))

    def test_an_empty_or_absent_root_is_still_accepted(self) -> None:
        fresh = LocalFSBackend(Path(self.tmp.name) / "fresh")
        result = create.create_kb(fresh, spec())
        self.assertTrue(fresh.exists("kb.json"))
        self.assertGreater(len(result.created_files), 0)


class IdentityTests(unittest.TestCase):
    def test_kb_code_is_128_bits_of_randomness(self) -> None:
        codes = {create.new_kb_code() for _ in range(200)}
        self.assertEqual(len(codes), 200, "kb_code 出现碰撞")
        for code in codes:
            self.assertEqual(len(code), 32, "128 位 = 32 个十六进制字符")
            self.assertEqual(len(bytes.fromhex(code)) * 8, 128)

    def test_a_short_kb_code_is_refused(self) -> None:
        with self.assertRaises(create.CreateError):
            spec(kb_code="abc").validate()

    def test_kb_json_holds_identity_and_references_only(self) -> None:
        payload = create.kb_identity(spec())
        self.assertEqual(
            sorted(payload),
            [
                "authority_note",
                "created_at",
                "display_name",
                "kb_code",
                "kb_type",
                "owner_ref",
                "refs",
                "schema",
                "visibility",
            ],
        )
        # The failure this guards against: source/schedule values copied into
        # kb.json, which then disagree with the files that actually drive the
        # factory.
        blob = json.dumps(payload, ensure_ascii=False)
        for leaked in ("lanes", "frequency", "timezone", "window", "root_folder", "key_ref"):
            with self.subTest(field=leaked):
                self.assertNotIn(f'"{leaked}"', blob)

    def test_sub_configs_are_the_sole_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalFSBackend(Path(tmp) / "kb")
            create.create_kb(backend, spec(sources=(create.SourceSpec("cwork"), DOCDB_SOURCE)))
            kb = loads(backend.read("kb.json"))
            self.assertEqual(kb["refs"]["source"], "source.json")
            self.assertEqual(kb["refs"]["schedule"], "schedule.json")
            source = loads(backend.read("source.json"))
            schedule = loads(backend.read("schedule.json"))
            self.assertEqual([s["source_type"] for s in source["sources"]], ["cwork", "docdb"])
            self.assertEqual(schedule["fetch"]["frequency"], create.DEFAULT_FREQUENCY)
            self.assertEqual(schedule["fetch"]["timezone"], create.DEFAULT_TIMEZONE)
            self.assertTrue(schedule["fetch"]["reply_refresh"])

    def test_reply_refresh_is_omitted_for_a_docdb_only_library(self) -> None:
        schedule = create.schedule_config(spec(sources=(DOCDB_SOURCE,)))
        self.assertNotIn("reply_refresh", schedule["fetch"])

    def test_members_copy_defers_to_the_ops_index(self) -> None:
        payload = create.members_copy(spec())
        self.assertEqual(payload["authority"], "ops:members-index")
        self.assertEqual(payload["members"][0]["owner_ref"], "owner-42")
        self.assertEqual(payload["members"][0]["role"], "owner")

    def test_members_index_record_is_generated_from_the_same_definition(self) -> None:
        record = create.members_index_record(spec())
        self.assertEqual(record["kb_code"], "a" * 32)
        self.assertEqual(record["members"], ["owner-42"])


def run_cli(*args) -> int:
    """Run the create CLI with its chatter captured, so the suite stays readable."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        return create.main(list(args))


class RejectedParameterTests(unittest.TestCase):
    """KB-PARAMETERS A2: v1 拒收，收到即拒绝且不落盘。"""

    def test_each_reserved_parameter_is_refused(self) -> None:
        for name in create.REJECTED_SOURCE_PARAMS:
            with self.subTest(param=name):
                with self.assertRaises(create.CreateError) as ctx:
                    create.reject_v1_unsupported({name: "value"})
                self.assertIn(name, str(ctx.exception))

    def test_empty_values_are_not_treated_as_present(self) -> None:
        create.reject_v1_unsupported({name: None for name in create.REJECTED_SOURCE_PARAMS})

    def test_cli_refuses_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "kb"
            code = run_cli("--name", "x", "--keywords", "预算", "--root", str(root))
            self.assertEqual(code, 2)
            self.assertFalse(root.exists(), "拒收的请求不得在磁盘上留下任何东西")


class ValidationTests(unittest.TestCase):
    def test_a_library_needs_a_name_and_a_source(self) -> None:
        with self.assertRaises(create.CreateError):
            spec(display_name="   ").validate()
        with self.assertRaises(create.CreateError):
            spec(sources=()).validate()

    def test_docdb_source_requires_a_root_folder(self) -> None:
        with self.assertRaises(create.CreateError):
            spec(sources=(create.SourceSpec("docdb"),)).validate()

    def test_duplicate_source_types_are_refused(self) -> None:
        with self.assertRaises(create.CreateError):
            spec(sources=(create.SourceSpec("cwork"), create.SourceSpec("cwork"))).validate()

    def test_unknown_enums_are_refused(self) -> None:
        with self.assertRaises(create.CreateError):
            spec(kb_type="global").validate()
        with self.assertRaises(create.CreateError):
            spec(visibility="public").validate()


class KeyRefTests(unittest.TestCase):
    """--key-ref 是环境变量引用，不是放明文 Key 的地方。"""

    def test_only_an_env_reference_is_accepted(self) -> None:
        for good in ("env:CWK_CWORK_KEY", "env:A", "env:_X9", "env:KB_KEY_2"):
            with self.subTest(key_ref=good):
                self.assertEqual(create.validate_key_ref(good), good)

    def test_everything_else_is_refused(self) -> None:
        """红法：明文 Key、文件路径、小写变量名、注入片段 → 全部拒绝。"""
        for bad in (
            "sk-live-0123456789",          # 明文 Key
            "CWK_CWORK_KEY",               # 少了 env: 前缀
            "env:",                        # 空变量名
            "env:cwk_cwork_key",           # 小写
            "env:9KEY",                    # 数字开头
            "env:KEY WITH SPACE",
            "env:KEY;rm -rf /",
            "env:KEY$(id)",
            "file:/etc/passwd",
            "env:KEY\nenv:OTHER",
            "",
            None,
        ):
            with self.subTest(key_ref=bad):
                with self.assertRaises(create.CreateError):
                    create.validate_key_ref(bad)

    def test_a_build_with_a_bad_key_ref_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalFSBackend(Path(tmp) / "kb")
            bad = spec(sources=(create.SourceSpec("cwork", key_ref="sk-live-abc"),))
            with self.assertRaises(create.CreateError):
                create.create_kb(backend, bad)
            self.assertEqual(backend.walk_files("."), [])

    def test_the_cli_refuses_it_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "kb"
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = create.main(
                    ["--name", "库", "--root", str(root), "--key-ref", "sk-live-abc"]
                )
            self.assertEqual(code, 2)
            self.assertIn("--key-ref", buffer.getvalue())
            self.assertEqual(LocalFSBackend(root).walk_files("."), [])

    def test_the_default_is_itself_a_valid_reference(self) -> None:
        self.assertEqual(
            create.validate_key_ref(create.DEFAULT_KEY_REF), create.DEFAULT_KEY_REF
        )


class AuditTreeTests(unittest.TestCase):
    def test_a_missing_or_empty_item_is_reported(self) -> None:
        backend = MemoryBackend()
        create.create_kb(backend, spec())
        self.assertEqual(create.audit_tree(backend, ["cwork"]), [])
        backend.remove("kb.json")
        findings = create.audit_tree(backend, ["cwork"])
        self.assertEqual(len(findings), 1)
        self.assertIn("#1 kb.json（缺失）", findings[0])
        backend.write("kb.json", b"   \n")
        self.assertIn("（为空）", create.audit_tree(backend, ["cwork"])[0])


class CliTests(unittest.TestCase):
    def test_cli_builds_a_library_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "kb"
            self.assertEqual(run_cli("--name", "端到端库", "--root", str(root)), 0)
            backend = LocalFSBackend(root)
            self.assertEqual(create.audit_tree(backend, ["cwork"]), [])
            self.assertTrue(verify_manifest(backend).ok)

    def test_cli_refuses_a_plaintext_credential_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = run_cli(
                "--name", "x", "--root", str(Path(tmp) / "kb"), "--password", "hunter2"
            )
            self.assertEqual(code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
