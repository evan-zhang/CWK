#!/usr/bin/env python3
"""RT-051 P2: the thin client and the wizard read-side verbs.

Pins the C05 contract: connection config arrives via environment variables
only (never argv), success payloads pass through the gateway schema
untouched, failures keep the v2 error shape, and exit codes split 0/1/2
(success / remote-permission-budget / request-protocol).

The tests run a real gateway on a loopback socket — the point of P2 is the
whole chain (CLI argv → client → HTTP → GatewayApp), not the app alone.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import tempfile
import threading
import unittest
import contextlib
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
import kb_access_file as kb_access  # noqa: E402
import kb_gateway_client as gw_client  # noqa: E402
import kb_wizard  # noqa: E402
from test_kb_gateway import (  # noqa: E402
    FIXED_NOW,
    LINEAGE,
    TOKEN,
    seed_memory_kb,
)

KB = "cwork-3m"


class LiveGatewayCase(unittest.TestCase):
    """A real HTTP server on an ephemeral loopback port, one per class."""

    @classmethod
    def setUpClass(cls) -> None:
        backend = seed_memory_kb()
        app = gateway.GatewayApp(
            backend, TOKEN, backend_kind="memory", clock=lambda: FIXED_NOW, kb_id=KB
        )
        cls._log_patch = mock.patch.object(
            gateway.GatewayHandler, "log_message", lambda s, f, *a: None
        )
        cls._log_patch.start()
        cls.server = gateway.make_server(app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.env = {
            gw_client.ENV_URL: f"http://127.0.0.1:{cls.port}",
            gw_client.ENV_TOKEN: TOKEN,
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._log_patch.stop()

    def cli(self, argv: list[str], env: dict | None = None) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        merged = dict(self.env)
        merged.update(env or {})
        with mock.patch.dict(os.environ, merged, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = kb_wizard.main(argv)
        raw = out.getvalue()
        payload = json.loads(raw) if raw.strip() else {}
        return code, payload, err.getvalue()


class ClientUnitTests(unittest.TestCase):
    def test_explicit_env_token_keeps_highest_priority(self) -> None:
        base, token = gw_client.connection_from_env(
            {gw_client.ENV_URL: "https://explicit.example.test", gw_client.ENV_TOKEN: "explicit"},
            kb_id=KB,
        )
        self.assertEqual((base, token), ("https://explicit.example.test", "explicit"))

    def test_missing_env_token_selects_the_local_file_by_kb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            access_dir = Path(tmp) / "access"
            token = secrets.token_hex(32)
            payload = {
                "schema": kb_access.ACCESS_FILE_SCHEMA,
                "kb_id": KB,
                "gateway_url": "https://local.example.test",
                "token": token,
                "token_id": "tok-" + "a" * 16,
                "issued_at": "2026-09-07T08:00:00Z",
                "expires_at": "2026-10-07T08:00:00Z",
            }
            kb_access.write_access_file(
                kb_access.access_path_for_kb(KB, access_dir), payload
            )
            base, selected = gw_client.connection_from_env(
                {gw_client.ENV_ACCESS_DIR: str(access_dir)}, kb_id=KB
            )
            self.assertEqual(base, "https://local.example.test")
            self.assertEqual(selected, token)

    def test_local_store_does_not_fall_back_to_another_kb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            access_dir = Path(tmp) / "access"
            token = secrets.token_hex(32)
            kb_access.write_access_file(
                kb_access.access_path_for_kb(KB, access_dir),
                {
                    "schema": kb_access.ACCESS_FILE_SCHEMA,
                    "kb_id": KB,
                    "gateway_url": "https://local.example.test",
                    "token": token,
                    "token_id": "tok-" + "b" * 16,
                    "issued_at": "2026-09-07T08:00:00Z",
                    "expires_at": "2026-10-07T08:00:00Z",
                },
            )
            with self.assertRaises(gw_client.ClientError) as ctx:
                gw_client.connection_from_env(
                    {gw_client.ENV_ACCESS_DIR: str(access_dir)}, kb_id="another-kb"
                )
            self.assertEqual(ctx.exception.code, "missing_connection")

    def test_missing_connection_refuses_before_any_network(self) -> None:
        with self.assertRaises(gw_client.ClientError) as ctx:
            gw_client.call("capabilities", {"kb": KB}, env={})
        self.assertEqual(ctx.exception.code, "missing_connection")
        self.assertEqual(ctx.exception.exit_code(), 2)

    def test_unknown_operation_is_refused_locally(self) -> None:
        with self.assertRaises(gw_client.ClientError) as ctx:
            gw_client.call("teleport", {"kb": KB}, env={"CWK_KB_GW_URL": "http://x", "CWK_KB_GW_TOKEN": "t"})
        self.assertEqual(ctx.exception.exit_code(), 2)

    def test_unreachable_gateway_is_a_remote_failure(self) -> None:
        with self.assertRaises(gw_client.ClientError) as ctx:
            gw_client.call(
                "capabilities", {"kb": KB},
                env={"CWK_KB_GW_URL": "http://127.0.0.1:1", "CWK_KB_GW_TOKEN": "t"},
                timeout=0.5,
            )
        self.assertEqual(ctx.exception.code, "unreachable")
        self.assertEqual(ctx.exception.exit_code(), 1)

    def test_the_error_message_never_contains_the_url(self) -> None:
        # 游标/句柄在 URL 里，错误信息不得把它带进 stderr（C04）
        try:
            gw_client.call(
                "capabilities", {"kb": KB},
                env={"CWK_KB_GW_URL": "http://127.0.0.1:1", "CWK_KB_GW_TOKEN": "t"},
                timeout=0.5,
            )
        except gw_client.ClientError as exc:
            self.assertNotIn("CWK_KB_GW_TOKEN", str(exc))
            self.assertNotIn("127.0.0.1:1?token", str(exc))


class WizardVerbHappyPaths(LiveGatewayCase):
    def test_capabilities_reports_the_contract(self) -> None:
        code, p, _ = self.cli(["capabilities", "--kb", KB])
        self.assertEqual(code, 0)
        self.assertEqual(p["schema"], "cwk.kb.capabilities.v2")
        self.assertEqual(len(p["supported_operations"]), 9)

    def test_list_paginates_and_searches(self) -> None:
        code, p, _ = self.cli(["list", "--kb", KB, "--page-size", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(p["schema"], "cwk.kb.documents.v2")
        self.assertEqual(p["total"], 3)
        self.assertFalse(p["eof"])
        code2, p2, _ = self.cli(["search", "--kb", KB, "--q", "供货"])
        self.assertEqual(code2, 0)
        self.assertEqual(p2["total"], 1)
        self.assertEqual(p2["items"][0]["lineage_id"], LINEAGE)

    def test_open_read_continue_walks_a_document(self) -> None:
        _c, opened, _e = self.cli(["open", "--kb", KB, "--lineage", LINEAGE])
        ref = opened["document_ref"]
        code, page1, _ = self.cli(
            ["read", "--kb", KB, "--document-ref", ref, "--max-bytes", "10"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(page1["schema"], "cwk.kb.read.v2")
        self.assertTrue(page1["full_sha_verified"])
        cur = page1["next_cursor"]
        self.assertIsNotNone(cur)
        code2, page2, _ = self.cli(
            ["continue", "--kb", KB, "--document-ref", ref, "--cursor", cur]
        )
        self.assertEqual(code2, 0)
        self.assertEqual(page2["span_start_byte"], page1["span_end_byte"])

    def test_inspect_and_renew(self) -> None:
        _c, opened, _e = self.cli(["open", "--kb", KB, "--lineage", LINEAGE])
        ref = opened["document_ref"]
        code, insp, _ = self.cli(["inspect", "--kb", KB, "--document-ref", ref])
        self.assertEqual(code, 0)
        self.assertEqual(insp["read_state"], "ready")
        self.assertGreater(insp["size_bytes"], 0)
        code2, renewed, _ = self.cli(["renew", "--kb", KB, "--document-ref", ref])
        self.assertEqual(code2, 0)
        self.assertNotEqual(renewed["document_ref"], ref)

    def test_success_payloads_carry_no_wizard_envelope(self) -> None:
        # C05：成功响应原样透传，不包第二层信封
        _c, p, _ = self.cli(["capabilities", "--kb", KB])
        self.assertNotIn("wizard_version", p)
        self.assertNotIn("verb", p)


class WizardVerbErrorPaths(LiveGatewayCase):
    def test_missing_env_is_exit_2_with_v2_shape(self) -> None:
        code, p, _ = self.cli(["capabilities", "--kb", KB], env={gw_client.ENV_URL: "", gw_client.ENV_TOKEN: ""})
        self.assertEqual(code, 2)
        self.assertEqual(p["schema"], "cwk.kb.error.v2")
        self.assertEqual(p["error"]["code"], "missing_connection")

    def test_bad_handle_is_exit_2(self) -> None:
        code, p, _ = self.cli(["read", "--kb", KB, "--document-ref", "garbage"])
        self.assertEqual(code, 2)
        self.assertEqual(p["error"]["code"], "bad_request")

    def test_unknown_kb_is_exit_1(self) -> None:
        code, p, _ = self.cli(["list", "--kb", "no-such-library"])
        self.assertEqual(code, 1)
        self.assertEqual(p["error"]["code"], "unknown_kb")

    def test_read_side_verbs_accept_no_backend_flags(self) -> None:
        # C05：read-side 动词不暴露 --backend/--prefix/--kb-root
        parser = kb_wizard.build_parser()
        argv = ["read", "--kb", KB, "--document-ref", "x", "--backend", "nas"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_document_refs_never_appear_in_stderr(self) -> None:
        _c, opened, _e = self.cli(["open", "--kb", KB, "--lineage", LINEAGE])
        ref = opened["document_ref"]
        _code, p, err = self.cli(["read", "--kb", KB, "--document-ref", "bad-handle"])
        self.assertNotIn(ref, err)
        self.assertNotIn("bad-handle", err)
        self.assertIn("网关调用失败", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
