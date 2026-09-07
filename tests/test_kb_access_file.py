"""RT-053: one-KB shared access-file export/import/revoke/rotate criteria.

All bearer values are generated per test. Nothing talks to OPS, IM, a real
registry, or a real Gateway.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import secrets
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_access_file as access  # noqa: E402
import kb_token  # noqa: E402
from kb_ledger import parse_iso  # noqa: E402

NOW = parse_iso("2026-09-07T08:00:00Z")
KB_ONE = "libraries/共享测试库"
KB_TWO = "libraries/另一个测试库"
GATEWAY = "https://gateway.example.test"


class AccessCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "ops" / "tokens.json"
        self.output = self.root / "delivery" / "one.cwk-access.json"
        self.install_dir = self.root / "receiver" / "access"
        kb_token.init_registry(self.registry, now=NOW)

    def export(self, kb_id: str = KB_ONE, **kwargs) -> dict:
        return access.export_access(
            self.registry,
            kb_id=kb_id,
            gateway_url=GATEWAY,
            output=kwargs.pop("output", self.output),
            now=NOW,
            **kwargs,
        )

    def payload(self, path: Path | None = None) -> dict:
        return json.loads((path or self.output).read_text("utf-8"))


class ExportContractTests(AccessCase):
    def test_export_is_one_kb_private_and_registry_stores_only_digest(self) -> None:
        receipt = self.export()
        payload = self.payload()
        self.assertEqual(set(payload), set(access.REQUIRED_FIELDS))
        self.assertEqual(payload["schema"], access.ACCESS_FILE_SCHEMA)
        self.assertEqual(payload["kb_id"], KB_ONE)
        self.assertEqual(receipt["token_id"], payload["token_id"])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.parent.stat().st_mode), 0o700)

        registry_text = self.registry.read_text("utf-8")
        self.assertNotIn(payload["token"], registry_text)
        row = kb_token.load_registry(self.registry)["tokens"][0]
        self.assertEqual(row["token_kind"], kb_token.TOKEN_KIND_SHARED_KB)
        self.assertEqual(row["agent_binding_id"], f"share:{KB_ONE}")
        self.assertEqual(row["kb_ids"], [KB_ONE])
        self.assertEqual(row["owner_ref"], "")
        self.assertEqual(row["owner_ref_basis"], "ops-token-registry-write-access")

    def test_success_receipt_and_cli_output_never_echo_the_token(self) -> None:
        receipt = self.export()
        token = self.payload()["token"]
        self.assertNotIn(token, json.dumps(receipt, ensure_ascii=False))

        other = self.root / "delivery2" / "two.json"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = access.main(
                [
                    "export", "--registry", str(self.registry), "--kb-id", KB_TWO,
                    "--gateway-url", GATEWAY, "--out", str(other),
                ],
                now=NOW,
            )
        self.assertEqual(code, 0, err.getvalue())
        second_token = json.loads(other.read_text("utf-8"))["token"]
        self.assertNotIn(second_token, out.getvalue() + err.getvalue())

    def test_active_share_requires_explicit_rotate_and_does_not_mutate(self) -> None:
        self.export()
        before = self.registry.read_bytes()
        with self.assertRaises(kb_token.ConflictError):
            self.export(output=self.root / "delivery" / "duplicate.json")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_http_is_limited_to_the_current_internal_gateway(self) -> None:
        accepted = copy.deepcopy(
            access.make_payload(
                {
                    "kb_ids": [KB_ONE], "token_id": "tok-" + "a" * 16,
                    "created_at": "2026-09-07T08:00:00Z",
                    "expires_at": "2026-10-07T08:00:00Z",
                },
                "b" * 64,
                "http://192.168.91.72:8787",
            )
        )
        self.assertEqual(accepted["gateway_url"], "http://192.168.91.72:8787")
        for bad in ("http://example.test", "ftp://example.test", "https://u:p@example.test"):
            candidate = copy.deepcopy(accepted)
            candidate["gateway_url"] = bad
            with self.subTest(bad=bad), self.assertRaises(access.InvalidAccessFile):
                access.validate_access_payload(candidate)

    def test_schema_unknown_fields_and_permission_fields_are_refused(self) -> None:
        self.export()
        payload = self.payload()
        variants = []
        wrong = copy.deepcopy(payload); wrong["schema"] = "cwk.kb.access-file.v999"; variants.append(wrong)
        extra = copy.deepcopy(payload); extra["kb_ids"] = [KB_ONE, KB_TWO]; variants.append(extra)
        wildcard = copy.deepcopy(payload); wildcard["permissions"] = ["*"]; variants.append(wildcard)
        empty = copy.deepcopy(payload); empty["kb_id"] = ""; variants.append(empty)
        control = copy.deepcopy(payload); control["kb_id"] = "bad\nkb"; variants.append(control)
        for value in variants:
            with self.subTest(keys=sorted(value)), self.assertRaises(kb_token.TokenError):
                access.validate_access_payload(value)

    def test_output_symlink_and_implicit_overwrite_are_refused(self) -> None:
        self.output.parent.mkdir(mode=0o700)
        victim = self.root / "victim"
        victim.write_text("untouched", encoding="utf-8")
        self.output.symlink_to(victim)
        with self.assertRaises(access.UnsafePath):
            self.export()
        self.assertEqual(victim.read_text("utf-8"), "untouched")

        self.output.unlink()
        self.export()
        with self.assertRaises(access.AccessConflict):
            access.write_access_file(self.output, self.payload())


class RotationAndRevocationTests(AccessCase):
    def test_rotate_advances_generation_and_invalidates_every_old_copy(self) -> None:
        first = self.export()
        old_token = self.payload()["token"]
        second = self.export(rotate=True, replace=True)
        new_token = self.payload()["token"]
        data = kb_token.load_registry(self.registry)
        self.assertEqual(first["generation"], 1)
        self.assertEqual(second["generation"], 2)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(kb_token.decide(data, old_token, kb_id=KB_ONE, now=NOW).reason, "revoked")
        self.assertTrue(kb_token.decide(data, new_token, kb_id=KB_ONE, now=NOW).ok)
        self.assertEqual(second["superseded_token_ids"], [first["token_id"]])

    def test_revoke_by_token_id_is_immediate(self) -> None:
        receipt = self.export()
        token = self.payload()["token"]
        args = access.build_parser().parse_args(
            ["revoke", "--registry", str(self.registry), "--token-id", receipt["token_id"]]
        )
        revoked = access.run(args, now=NOW)
        self.assertEqual(revoked["effective"], "immediate")
        decision = kb_token.TokenFile(self.registry).decide(token, kb_id=KB_ONE, now=NOW)
        self.assertEqual(decision.reason, "revoked")

    def test_rotate_without_a_prior_export_is_refused(self) -> None:
        with self.assertRaises(kb_token.NotFound):
            self.export(rotate=True)


class ImportAndSelectionStoreTests(AccessCase):
    def test_import_uses_hashed_filename_and_private_permissions(self) -> None:
        self.export(kb_id="../../not/a/path")
        receipt = access.import_access(self.output, access_dir=self.install_dir)
        installed = Path(receipt["installed_file"])
        self.assertTrue(os.path.samefile(installed.parent, self.install_dir))
        self.assertNotIn("..", installed.name)
        self.assertNotIn("/", installed.name)
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(installed.parent.stat().st_mode), 0o700)
        loaded = access.load_installed_access("../../not/a/path", self.install_dir)
        self.assertEqual(loaded["token_id"], receipt["token_id"])

    def test_different_token_for_same_kb_requires_replace(self) -> None:
        self.export()
        access.import_access(self.output, access_dir=self.install_dir)
        self.export(rotate=True, replace=True)
        with self.assertRaises(access.AccessConflict):
            access.import_access(self.output, access_dir=self.install_dir)
        access.import_access(self.output, access_dir=self.install_dir, replace=True)
        self.assertEqual(
            access.load_installed_access(KB_ONE, self.install_dir)["token_id"],
            self.payload()["token_id"],
        )

    def test_multiple_kbs_coexist_and_are_selected_by_kb_id(self) -> None:
        first = self.root / "delivery" / "one.json"
        second = self.root / "delivery" / "two.json"
        self.export(KB_ONE, output=first)
        self.export(KB_TWO, output=second)
        access.import_access(first, access_dir=self.install_dir)
        access.import_access(second, access_dir=self.install_dir)
        one = access.load_installed_access(KB_ONE, self.install_dir)
        two = access.load_installed_access(KB_TWO, self.install_dir)
        self.assertNotEqual(one["token"], two["token"])
        self.assertEqual(one["kb_id"], KB_ONE)
        self.assertEqual(two["kb_id"], KB_TWO)

    def test_tampered_installed_file_cannot_be_selected_for_another_kb(self) -> None:
        self.export()
        access.import_access(self.output, access_dir=self.install_dir)
        original = access.access_path_for_kb(KB_ONE, self.install_dir)
        wrong = access.access_path_for_kb(KB_TWO, self.install_dir)
        wrong.write_bytes(original.read_bytes())
        os.chmod(wrong, 0o600)
        with self.assertRaises(access.InvalidAccessFile):
            access.load_installed_access(KB_TWO, self.install_dir)

    def test_invalid_token_error_never_echoes_the_supplied_value(self) -> None:
        self.export()
        payload = self.payload()
        supplied = "do-not-echo-" + secrets.token_hex(12)
        payload["token"] = supplied
        self.output.write_text(json.dumps(payload), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = access.main(
                ["import", "--file", str(self.output), "--access-dir", str(self.install_dir)]
            )
        self.assertEqual(code, 2)
        self.assertNotIn(supplied, out.getvalue() + err.getvalue())


class LegacyCompatibilityTests(AccessCase):
    def test_pre_rt053_records_are_still_agent_tokens(self) -> None:
        record = {"schema": kb_token.RECORD_SCHEMA}
        self.assertEqual(kb_token.token_kind(record), kb_token.TOKEN_KIND_AGENT)


if __name__ == "__main__":
    unittest.main()
