"""RT-044 向导判据：J1（目的地脏必拒、零写入）与 J5（全动词 JSON）。

Offline throughout.  The RT-043 ingest pipeline is not on this branch yet, so
``ingest`` is exercised against generated stub scripts injected through
``KB_INGEST_BIN`` — which is also how an operator points the wizard at a
different implementation, so the seam under test is the real one.

The J-numbers are RT-044 rt-lite's:

J1  向导目的地脏必拒（复用 kb_create 预检），零写入。
J5  向导输出一律 JSON（CLI-SPEC 合同）——成功、拒绝、崩溃都一样。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_wizard as wizard  # noqa: E402
from kb_gateway import RAW_INDEX_REL  # noqa: E402
from kb_ledger import dumps, record_write, refresh_manifest  # noqa: E402
from kb_storage import LocalFSBackend  # noqa: E402

LINEAGE = "docdb:2087519593823322113"


def run_wizard(argv: Sequence[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, dict, str]:
    """Run a verb in-process and return ``(exit_code, parsed_stdout, stderr)``.

    ``json.loads`` is not defensive here: it is the J5 assertion.  A verb that
    printed anything unparseable fails every test that touches it.
    """
    out, err = io.StringIO(), io.StringIO()
    patched = dict(os.environ)
    patched.update(env or {})
    with mock.patch.dict(os.environ, patched, clear=True):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wizard.main(list(argv))
    return code, json.loads(out.getvalue()), err.getvalue()


def tree_digest(root: Path) -> Dict[str, str]:
    """``{relative path: sha256}`` for everything under ``root``.

    The J1 zero-write assertion compares two of these.  A comparison that
    cannot see a single changed byte proves nothing, so
    :meth:`J1DirtyDestinationTests.test_J1_red_the_snapshot_sees_one_flipped_byte`
    flips one and checks that it does.
    """
    if not root.exists():
        return {}
    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def seed_json(root: Path, rel: str, payload: dict) -> None:
    """Write a ledger-managed file the way the pipeline would.

    Dropping bytes in behind ``root-manifest.json``'s back would turn every
    later ``status`` red for a reason the test never intended, so the seed
    updates the manifest too.
    """
    backend = LocalFSBackend(root)
    kb_code = json.loads((root / "kb.json").read_text(encoding="utf-8"))["kb_code"]
    record_write(backend, rel, dumps(payload))
    refresh_manifest(backend, kb_code=kb_code, allow_new=[rel], allow_replaced=[rel])


def write_stub(directory: Path, body: str, name: str = "fake_ingest.py") -> Path:
    """Materialise a stand-in for RT-043's ``scripts/kb_ingest.py``."""
    stub = directory / name
    stub.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


class WizardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.root = self.work / "kb"
        self.addCleanup(self.tmp.cleanup)

    def build_library(self, *, name: str = "工作库", source: str = "cwork") -> dict:
        code, payload, _err = run_wizard(
            ["create", "--name", name, "--kb-root", str(self.root), "--source", source, "--yes"]
        )
        self.assertEqual(code, 0, payload)
        return payload


# ── create ──────────────────────────────────────────────────────────────────


class CreateVerbTests(WizardCase):
    def test_create_builds_the_b_table_tree_and_returns_the_card(self) -> None:
        payload = self.build_library()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["confirmed"])
        self.assertEqual(len(payload["kb_code"]), 32)
        self.assertEqual(payload["display_name"], "工作库")
        self.assertEqual(len(payload["tree_items"]), 30)
        self.assertEqual(payload["created"]["file_count"], 20)
        self.assertGreater(payload["created"]["manifest_entry_count"], 0)
        self.assertTrue((self.root / "kb.json").is_file())

    def test_the_card_carries_every_id_the_skill_needs_to_continue(self) -> None:
        """CLI-SPEC §一.2: 所有 ID 必须回显，Skill 靠它们续话。"""
        payload = self.build_library()
        for field in ("kb_code", "display_name", "kb_root", "backend", "sources", "next"):
            self.assertIn(field, payload)
        self.assertEqual(json.loads(
            (self.root / "kb.json").read_text(encoding="utf-8")
        )["kb_code"], payload["kb_code"])

    def test_without_yes_it_only_shows_the_card_and_writes_nothing(self) -> None:
        code, payload, _err = run_wizard(
            ["create", "--name", "工作库", "--kb-root", str(self.root)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["confirmed"])
        self.assertNotIn("created", payload)
        self.assertEqual(tree_digest(self.root), {}, "未确认的 create 不该落盘")

    def test_route_mode_is_proposed_per_source(self) -> None:
        """DOCDB-INGEST-DESIGN §三: cwork→timeline、docdb→classify，卡上显式展示。"""
        for source, expected in (("cwork", "timeline"), ("docdb", "classify")):
            with self.subTest(source=source):
                argv = ["create", "--name", "库", "--kb-root", str(self.work / source),
                        "--source", source]
                if source == "docdb":
                    argv += ["--docdb-root", "/玄关/合同"]
                _code, payload, _err = run_wizard(argv)
                self.assertEqual(payload["sources"][0]["route_mode"], expected)
                self.assertEqual(
                    payload["route_mode_default_by_source"], {"cwork": "timeline", "docdb": "classify"}
                )

    def test_route_mode_can_be_overridden(self) -> None:
        _code, payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root),
             "--source", "cwork", "--route-mode", "classify"]
        )
        self.assertEqual(payload["sources"][0]["route_mode"], "classify")

    def test_the_route_mode_lands_in_source_json(self) -> None:
        self.build_library()
        stored = json.loads((self.root / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sources"][0]["route"], "timeline")

    def test_a_docdb_library_trims_the_cwork_only_rows(self) -> None:
        _code, payload, _err = run_wizard(
            ["create", "--name", "文档库", "--kb-root", str(self.root),
             "--source", "docdb", "--docdb-root", "/玄关/合同", "--yes"]
        )
        self.assertEqual(len(payload["tree_items"]), 28)
        self.assertFalse((self.root / "timelines").exists())

    def test_an_unknown_source_is_refused_in_json(self) -> None:
        code, payload, err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root), "--source", "email", "--yes"]
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "WizardRefused")
        self.assertIn("email", payload["error"]["message"])
        self.assertIn("向导失败", err)
        self.assertEqual(tree_digest(self.root), {})

    def test_a_plaintext_key_ref_is_refused_before_anything_is_built(self) -> None:
        code, payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root),
             "--key-ref", "sk-live-1234567890", "--yes"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "CreateError")
        self.assertEqual(tree_digest(self.root), {})

    def test_a_plaintext_credential_flag_is_refused(self) -> None:
        code, payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root), "--token", "leaked", "--yes"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "PlaintextCredential")

    def test_a_file_url_root_is_accepted(self) -> None:
        code, payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", f"file://{self.root}", "--yes"]
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "kb.json").is_file())
        self.assertEqual(payload["kb_root"], f"file://{self.root}")


# ── J1 ──────────────────────────────────────────────────────────────────────


class J1DirtyDestinationTests(WizardCase):
    """J1 — 目的地脏必拒，且本次零写入。"""

    def test_J1_creating_into_an_existing_library_is_refused_with_zero_writes(self) -> None:
        first = self.build_library(name="原有库")
        before = tree_digest(self.root)
        self.assertGreater(len(before), 20)

        code, payload, err = run_wizard(
            ["create", "--name", "覆盖库", "--kb-root", str(self.root), "--yes"]
        )

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "LedgerViolation")
        self.assertIn("零写入", payload["error"]["message"])
        self.assertIn("向导失败", err)
        self.assertEqual(tree_digest(self.root), before, "被拒的建库动了目的地的内容")
        # The original library is still itself, not a half-overwritten one.
        identity = json.loads((self.root / "kb.json").read_text(encoding="utf-8"))
        self.assertEqual(identity["kb_code"], first["kb_code"])
        self.assertEqual(identity["display_name"], "原有库")

    def test_J1_the_refusal_also_holds_without_yes(self) -> None:
        self.build_library()
        before = tree_digest(self.root)
        code, payload, _err = run_wizard(
            ["create", "--name", "覆盖库", "--kb-root", str(self.root)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "LedgerViolation")
        self.assertEqual(tree_digest(self.root), before)

    def test_J1_a_root_holding_one_unrelated_file_is_dirty_too(self) -> None:
        """Not "is this a KB?" but "is this empty?" — a stricter question."""
        self.root.mkdir(parents=True)
        (self.root / "别人的笔记.md").write_text("不要动我", encoding="utf-8")
        before = tree_digest(self.root)

        code, payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root), "--yes"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "LedgerViolation")
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual((self.root / "别人的笔记.md").read_text(encoding="utf-8"), "不要动我")

    def test_an_empty_directory_is_not_dirty(self) -> None:
        self.root.mkdir(parents=True)
        code, _payload, _err = run_wizard(
            ["create", "--name", "库", "--kb-root", str(self.root), "--yes"]
        )
        self.assertEqual(code, 0)

    def test_J1_red_the_snapshot_sees_one_flipped_byte(self) -> None:
        """The zero-write comparison's own red method."""
        self.build_library()
        before = tree_digest(self.root)
        victim = self.root / "kb.json"
        data = bytearray(victim.read_bytes())
        data[-2] ^= 0x01
        victim.write_bytes(bytes(data))
        self.assertNotEqual(tree_digest(self.root), before, "快照比对看不见改动，判据无效")


# ── ingest ──────────────────────────────────────────────────────────────────

STUB_OK = """
    import argparse, json, sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-root", required=True)
    parser.add_argument("--since")
    parser.add_argument("--extra")
    args = parser.parse_args()
    print(json.dumps({
        "schema": "cwk.kb.ingest.v1", "ok": True,
        "kb_root": args.kb_root, "since": args.since, "extra": args.extra,
        "ingested": 3, "argv": sys.argv[1:],
    }, ensure_ascii=False))
"""

STUB_FAILS = """
    import json, sys
    print(json.dumps({"schema": "cwk.kb.ingest.v1", "ok": False, "failed": ["docdb:1"]}))
    print("源 5xx：批次红", file=sys.stderr)
    sys.exit(1)
"""

STUB_NOT_JSON = """
    print("摄取完成，共 3 件")
"""

STUB_MARKER = """
    import pathlib, json, os
    pathlib.Path(os.environ["STUB_MARKER"]).write_text("ran", encoding="utf-8")
    print(json.dumps({"ok": True}))
"""

STUB_SLOW = """
    import time
    time.sleep(30)
"""


class IngestVerbTests(WizardCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_library()

    def ingest(self, stub_body: Optional[str], *extra: str, env=None) -> Tuple[int, dict, str]:
        environ = dict(env or {})
        if stub_body is not None:
            environ[wizard.ENV_INGEST_BIN] = str(write_stub(self.work, stub_body))
        return run_wizard(
            ["ingest", "--kb-root", str(self.root), *extra], env=environ
        )

    def test_a_missing_ingest_script_is_a_clean_json_refusal(self) -> None:
        """RT-043 may not have landed on this branch yet; say so, don't crash."""
        absent = self.work / "没有这个文件.py"
        code, payload, _err = self.ingest(
            None, "--yes", env={wizard.ENV_INGEST_BIN: str(absent)}
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "ingest_bin_missing")
        self.assertEqual(payload["ingest_bin"], str(absent))
        self.assertIn(wizard.ENV_INGEST_BIN, payload["error"]["message"])

    def test_the_binary_is_located_by_flag_then_env_then_repo_default(self) -> None:
        self.assertEqual(
            wizard.resolve_ingest_bin("/tmp/a.py", {wizard.ENV_INGEST_BIN: "/tmp/b.py"}),
            Path("/tmp/a.py"),
        )
        self.assertEqual(
            wizard.resolve_ingest_bin(None, {wizard.ENV_INGEST_BIN: "/tmp/b.py"}),
            Path("/tmp/b.py"),
        )
        self.assertEqual(wizard.resolve_ingest_bin(None, {}), wizard.DEFAULT_INGEST_BIN)
        self.assertEqual(wizard.DEFAULT_INGEST_BIN.name, "kb_ingest.py")

    def test_a_successful_run_embeds_the_child_json(self) -> None:
        code, payload, _err = self.ingest(STUB_OK, "--since", "2026-06-01", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["ingest"]["ingested"], 3)
        self.assertEqual(payload["ingest"]["kb_root"], str(self.root))
        self.assertEqual(payload["ingest"]["since"], "2026-06-01")

    def test_the_child_command_line_is_the_documented_one(self) -> None:
        bin_path = Path("/tmp/kb_ingest.py")
        self.assertEqual(
            wizard.ingest_argv(bin_path, "/tmp/kb", None, []),
            [sys.executable, "/tmp/kb_ingest.py", "--kb-root", "/tmp/kb"],
        )
        self.assertEqual(
            wizard.ingest_argv(bin_path, "/tmp/kb", "2026-06-01", ["--dry-run"]),
            [sys.executable, "/tmp/kb_ingest.py", "--kb-root", "/tmp/kb",
             "--since", "2026-06-01", "--dry-run"],
        )

    def test_yes_is_not_forwarded_to_the_child(self) -> None:
        """``--yes`` is the wizard's own gate, not part of RT-043's contract."""
        _code, payload, _err = self.ingest(STUB_OK, "--yes")
        self.assertNotIn("--yes", payload["ingest"]["argv"])
        self.assertNotIn("--yes", payload["command"])

    def test_extra_arguments_pass_through(self) -> None:
        # ``--ingest-arg=--extra=x``: an argparse value that itself starts
        # with ``--`` has to be attached with ``=``.
        _code, payload, _err = self.ingest(STUB_OK, "--yes", "--ingest-arg=--extra=x")
        self.assertEqual(payload["ingest"]["extra"], "x")

    def test_without_yes_the_child_is_never_started(self) -> None:
        marker = self.work / "marker.txt"
        code, payload, _err = self.ingest(
            STUB_MARKER, env={"STUB_MARKER": str(marker)}
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["confirmed"])
        self.assertIn("command", payload)
        self.assertFalse(marker.exists(), "未确认的 ingest 启动了子进程")

    def test_a_failing_child_surfaces_as_a_failure_with_its_output_kept(self) -> None:
        code, payload, _err = self.ingest(STUB_FAILS, "--yes")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertEqual(payload["error"]["kind"], "ingest_failed")
        self.assertEqual(payload["ingest"]["failed"], ["docdb:1"])
        self.assertIn("源 5xx", payload["stderr_text"])

    def test_child_output_that_is_not_json_is_reported_not_swallowed(self) -> None:
        code, payload, _err = self.ingest(STUB_NOT_JSON, "--yes")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "ingest_output_not_json")
        self.assertIn("摄取完成", payload["stdout_text"])

    def test_a_hanging_child_is_killed_and_reported(self) -> None:
        code, payload, _err = self.ingest(STUB_SLOW, "--yes", "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "timeout")

    def test_a_child_that_cannot_be_started_is_reported_as_json(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("Exec format error")):
            code, payload, _err = self.ingest(STUB_OK, "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "spawn_failed")

    def test_long_child_output_is_clipped(self) -> None:
        self.assertEqual(len(wizard.clip("a" * 10)), 10)
        clipped = wizard.clip("a" * (wizard.CAPTURE_CHARS + 500))
        self.assertTrue(clipped.startswith("a" * wizard.CAPTURE_CHARS))
        self.assertIn("截断", clipped)


# ── status ──────────────────────────────────────────────────────────────────


class StatusVerbTests(WizardCase):
    def setUp(self) -> None:
        super().setUp()
        self.created = self.build_library()

    def test_a_freshly_built_library_is_green(self) -> None:
        code, payload, _err = run_wizard(["status", "--kb-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["identity"]["kb_code"], self.created["kb_code"])
        self.assertEqual(payload["identity"]["display_name"], "工作库")
        self.assertEqual(payload["identity"]["sources"], [
            {"source_type": "cwork", "route_mode": "timeline"}
        ])

    def test_status_aggregates_ledger_doctor_ingest_state_and_index(self) -> None:
        _code, payload, _err = run_wizard(["status", "--kb-root", str(self.root)])
        self.assertTrue(payload["ledger"]["manifest"]["ok"])
        self.assertTrue(payload["ledger"]["collection_state"]["ok"])
        self.assertGreater(payload["ledger"]["entry_count"], 0)
        self.assertEqual(payload["doctor"]["checks"], list(__import__("kb_doctor").CHECKS))
        self.assertEqual(payload["doctor"]["failed"], [])
        self.assertTrue(payload["ingest_state"]["present"])
        self.assertEqual(payload["ingest_state"]["items"], 0)
        self.assertTrue(payload["raw_index"]["present"])
        self.assertEqual(payload["raw_index"]["entries"], 0)

    def test_a_hand_edited_file_turns_status_red_with_exit_1(self) -> None:
        target = self.root / "wiki" / "index.md"
        target.write_text("有人手工改了这一页", encoding="utf-8")

        code, payload, _err = run_wizard(["status", "--kb-root", str(self.root)])
        self.assertEqual(code, 1, "账本对不上时 status 不该报 0")
        self.assertFalse(payload["ok"])
        self.assertIn("manifest", payload["doctor"]["failed"])
        self.assertIn("wiki/index.md", payload["ledger"]["manifest"]["mismatched"])

    def test_a_deleted_file_is_reported_as_missing(self) -> None:
        (self.root / "source.json").unlink()
        code, payload, _err = run_wizard(["status", "--kb-root", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("source.json", payload["ledger"]["manifest"]["missing"])
        self.assertIn("tree", payload["doctor"]["failed"])

    def test_failed_ingest_items_turn_status_red(self) -> None:
        seed_json(self.root, wizard.INGEST_STATE_REL, {
            "schema": "cwk.kb.ingest-state.v1",
            "items": {
                LINEAGE: {"lineage_id": LINEAGE, "status": "failed:convert", "ts": "x"},
                "docdb:2": {"lineage_id": "docdb:2", "status": "ok", "ts": "x"},
            },
        })
        _code, payload, _err = run_wizard(["status", "--kb-root", str(self.root)])
        self.assertFalse(payload["ingest_state"]["ok"])
        self.assertEqual(payload["ingest_state"]["items"], 2)
        self.assertEqual(payload["ingest_state"]["by_status"]["failed:convert"], 1)
        self.assertEqual(payload["ingest_state"]["failed"], [LINEAGE])
        self.assertEqual(payload["doctor"]["failed"], [], "红点应该只来自状态账，不是账本")
        self.assertFalse(payload["ok"])

    def test_a_library_that_is_not_there_reports_in_json_instead_of_crashing(self) -> None:
        code, payload, _err = run_wizard(["status", "--kb-root", str(self.work / "空的")])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["identity"]["kb_json"], "缺失或不可读")
        self.assertFalse(payload["ingest_state"]["present"])
        self.assertFalse(payload["raw_index"]["present"])


# ── query ───────────────────────────────────────────────────────────────────


def seed_index(root: Path) -> None:
    seed_json(
        root,
        RAW_INDEX_REL,
        {
            "schema": "cwk.kb.raw-index.v1",
            "entries": {
                LINEAGE: {
                    "path": "raw/合同/供货协议.md",
                    "title": "供货协议",
                    "version": 3,
                    "sha256": "a" * 64,
                    "status": "ok",
                },
                "cwork:2095046023776104449": {
                    "path": "raw/2026-08/周报.md",
                    "title": "八月第三周汇报",
                    "version": 1,
                    "sha256": "b" * 64,
                    "status": "ok",
                },
            },
        },
    )


class QueryVerbTests(WizardCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_library()
        seed_index(self.root)

    def test_a_substring_hit_returns_lineage_title_version_and_path(self) -> None:
        code, payload, _err = run_wizard(["query", "--kb-root", str(self.root), "--q", "供货"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["matched"], 1)
        hit = payload["results"][0]
        self.assertEqual(hit["lineage_id"], LINEAGE)
        self.assertEqual(hit["title"], "供货协议")
        self.assertEqual(hit["version"], 3)
        self.assertEqual(hit["path"], "raw/合同/供货协议.md")

    def test_the_lineage_id_itself_is_searchable(self) -> None:
        _code, payload, _err = run_wizard(
            ["query", "--kb-root", str(self.root), "--q", "2087519593823322113"]
        )
        self.assertEqual([hit["lineage_id"] for hit in payload["results"]], [LINEAGE])

    def test_no_hit_is_a_successful_empty_answer(self) -> None:
        code, payload, _err = run_wizard(
            ["query", "--kb-root", str(self.root), "--q", "不存在的东西"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"], [])

    def test_limit_pages_the_results_while_matched_stays_honest(self) -> None:
        _code, payload, _err = run_wizard(
            ["query", "--kb-root", str(self.root), "--q", "raw", "--limit", "1"]
        )
        self.assertEqual(payload["matched"], 2)
        self.assertEqual(payload["returned"], 1)

    def test_an_empty_query_word_is_refused(self) -> None:
        code, payload, _err = run_wizard(["query", "--kb-root", str(self.root), "--q", "  "])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "GatewayError")

    def test_a_library_without_an_index_says_so(self) -> None:
        (self.root / RAW_INDEX_REL).unlink()
        code, payload, _err = run_wizard(["query", "--kb-root", str(self.root), "--q", "供货"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "BackendUnavailable")

    def test_the_wizard_and_the_gateway_run_the_same_search(self) -> None:
        """One implementation, two faces — imported, not copied."""
        import kb_gateway

        self.assertIs(wizard.query_index, kb_gateway.query_index)
        _code, payload, _err = run_wizard(["query", "--kb-root", str(self.root), "--q", "供货"])
        direct = kb_gateway.query_index(LocalFSBackend(self.root), "供货")
        self.assertEqual(payload["results"], direct["results"])


# ── J5 ──────────────────────────────────────────────────────────────────────


class J5JsonOutputTests(WizardCase):
    """J5 — 四个动词、成功与失败路径，stdout 一律 json.loads 得动。"""

    def test_J5_every_verb_prints_one_json_object_on_success(self) -> None:
        self.build_library()
        seed_index(self.root)
        stub = write_stub(self.work, STUB_OK)
        cases = [
            (["create", "--name", "第二个库", "--kb-root", str(self.work / "kb2"), "--yes"], 0),
            (["ingest", "--kb-root", str(self.root), "--yes"], 0),
            (["status", "--kb-root", str(self.root)], 0),
            (["query", "--kb-root", str(self.root), "--q", "供货"], 0),
        ]
        for argv, expected in cases:
            with self.subTest(verb=argv[0]):
                code, payload, _err = run_wizard(
                    argv, env={wizard.ENV_INGEST_BIN: str(stub)}
                )
                self.assertEqual(code, expected)
                self.assertEqual(payload["schema"], wizard.CARD_SCHEMA)
                self.assertEqual(payload["verb"], argv[0])
                self.assertIn("ok", payload)
                self.assertIn("at", payload)
                self.assertEqual(payload["wizard_version"], wizard.WIZARD_VERSION)

    def test_J5_every_verb_prints_one_json_object_on_refusal(self) -> None:
        self.build_library()
        cases = [
            ["create", "--name", "库", "--kb-root", str(self.root), "--yes"],
            ["ingest", "--kb-root", str(self.root), "--yes"],
            ["query", "--kb-root", str(self.root), "--q", ""],
        ]
        for argv in cases:
            with self.subTest(verb=argv[0]):
                code, payload, err = run_wizard(
                    argv, env={wizard.ENV_INGEST_BIN: str(self.work / "缺席.py")}
                )
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["verb"], argv[0])
                self.assertIn("kind", payload["error"])
                self.assertIn("message", payload["error"])

    def test_J5_the_verb_list_matches_the_parser(self) -> None:
        self.assertEqual(tuple(sorted(wizard.HANDLERS)), tuple(sorted(wizard.VERBS)))

    def test_J5_output_is_canonical_json_bytes(self) -> None:
        """Sorted keys, UTF-8, trailing newline — diffable between runs."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wizard.emit({"b": 1, "a": "中文"})
        self.assertEqual(out.getvalue(), '{\n  "a": "中文",\n  "b": 1\n}\n')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
