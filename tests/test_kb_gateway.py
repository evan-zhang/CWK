"""RT-044 网关判据：J2（写语义/未知路径）、J3（引文现场拉取）、J4（token）、J5（JSON）。

Everything runs offline: a local temp directory or an in-process
:class:`MemoryBackend` stands in for the NAS, and the HTTP surface is
exercised over a real loopback socket on an ephemeral port.  No credential,
no network, no device — the criteria are about *this* process's behaviour,
and a test that needed the NAS to prove them would not be runnable in CI.

The J-numbers are RT-044 rt-lite's:

J2  写语义（POST/PUT/PATCH/DELETE）与未知路径 → 405 / 404。
J3  ``/citation`` 现场从存储后端拉字节：后端内容变了，返回的 sha256 就跟着变。
J4  错 token → 401；token 能访问的面不含任何管理或写动作。
J5  全动词输出 JSON，Content-Type 为 application/json。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_gateway as gateway  # noqa: E402
from kb_ledger import dumps  # noqa: E402
from kb_storage import LocalFSBackend, MemoryBackend, StorageError  # noqa: E402

ADMIN_KEY = "rt044-admin-key-中文"
TOKEN = hashlib.sha256(ADMIN_KEY.encode("utf-8")).hexdigest()
FIXED_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

LINEAGE = "docdb:2087519593823322113"
RAW_PATH = "raw/合同/供货协议.md"
BODY_V1 = "# 供货协议\n\n第一版正文。" + "内容" * 400
BODY_V2 = "# 供货协议\n\n第二版正文，条款已改。" + "内容" * 400

#: Management / write verbs a token holder might probe for.  None of them
#: exists; all of them must answer 404 rather than doing anything.
MANAGEMENT_PROBES = (
    "/create",
    "/ingest",
    "/admin",
    "/token",
    "/token/issue",
    "/rebuild",
    "/archive",
    "/rotate-key",
    "/taxonomy/confirm",
    "/activate",
    "/member/add",
    "/kb",
)

_QUIET = mock.patch.object(gateway.GatewayHandler, "log_message", lambda self, fmt, *a: None)


def setUpModule() -> None:  # noqa: N802 - unittest hook
    _QUIET.start()


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    _QUIET.stop()


# ── fixtures ────────────────────────────────────────────────────────────────


def index_document(*, path: str = RAW_PATH, sha256: str = "", version: int = 2) -> dict:
    return {
        "schema": "cwk.kb.raw-index.v1",
        "kb_code": "f" * 32,
        "entries": {
            LINEAGE: {
                "path": path,
                "title": "供货协议",
                "version": version,
                "sha256": sha256 or hashlib.sha256(BODY_V1.encode("utf-8")).hexdigest(),
                "status": "ok",
                "artifact_kind": "document",
                "versions": [
                    {"version": 1, "sha256": "0" * 64, "path": "raw/合同/供货协议.v1.md"},
                    {
                        "version": 2,
                        "sha256": hashlib.sha256(BODY_V1.encode("utf-8")).hexdigest(),
                        "path": path,
                    },
                ],
            },
            "cwork:2095046023776104449": {
                "path": "raw/2026-08/周报.md",
                "title": "八月第三周汇报",
                "version": 1,
                "sha256": "1" * 64,
                "status": "ok",
                "artifact_kind": "document",
            },
            "docdb:9990001": {
                "path": "raw/_unrouted/扫描件.png",
                "title": "扫描件",
                "version": 1,
                "sha256": "2" * 64,
                "status": "placeholder",
                "artifact_kind": "placeholder",
            },
        },
    }


def seed_memory_kb() -> MemoryBackend:
    backend = MemoryBackend()
    backend.write(gateway.RAW_INDEX_REL, dumps(index_document()))
    backend.write(RAW_PATH, BODY_V1.encode("utf-8"))
    backend.write("raw/合同/供货协议.v1.md", "第一版存档".encode("utf-8"))
    backend.write("raw/2026-08/周报.md", "周报正文".encode("utf-8"))
    return backend


def seed_local_kb(root: Path) -> LocalFSBackend:
    backend = LocalFSBackend(root)
    backend.write(gateway.RAW_INDEX_REL, dumps(index_document()))
    backend.write(RAW_PATH, BODY_V1.encode("utf-8"))
    backend.write("raw/合同/供货协议.v1.md", "第一版存档".encode("utf-8"))
    return backend


def make_app(backend, *, token: str = TOKEN) -> gateway.GatewayApp:
    return gateway.GatewayApp(backend, token, backend_kind="local", clock=lambda: FIXED_NOW)


# ── test doubles ────────────────────────────────────────────────────────────


class WriteAttempted(AssertionError):
    """The read-only gateway called a mutating backend method."""


class WriteTrapBackend(MemoryBackend):
    """A backend that treats any write as a test failure.

    This is how "网关进程不含任何写动词" is *observed* rather than promised:
    every route is driven against this object, so a write introduced later
    fails the suite at the call that made it.  ``seed`` is the only way bytes
    get in, and it is not part of the storage protocol.
    """

    def seed(self, path: str, data: bytes) -> None:
        super().write(path, data)

    def write(self, path: str, data: bytes) -> str:
        raise WriteAttempted(f"只读网关试图写入 {path}")

    def mkdir(self, path: str) -> None:
        raise WriteAttempted(f"只读网关试图建目录 {path}")

    def remove(self, path: str) -> None:
        raise WriteAttempted(f"只读网关试图删除 {path}")

    def remove_dir(self, path: str) -> None:
        raise WriteAttempted(f"只读网关试图删除目录 {path}")


class UnreachableBackend:
    """Every operation fails the way an offline NAS does."""

    def _fail(self, *_args, **_kwargs):
        raise StorageError("NAS 不可达（测试桩）")

    mkdir = write = read = exists = list_dir = walk_files = sha256 = _fail
    remove = remove_dir = _fail


# ── HTTP harness ────────────────────────────────────────────────────────────


class RunningGateway:
    """A real single-threaded gateway on an ephemeral loopback port."""

    def __init__(self, app: gateway.GatewayApp) -> None:
        self.httpd = gateway.make_server(app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        # A short poll interval keeps ``shutdown()`` from costing half a
        # second per test case; the default 0.5s dominates a suite that
        # stands a server up per test.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        token: Optional[str] = TOKEN,
        body: Optional[bytes] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        headers: Dict[str, str] = {}
        if token is not None:
            headers[gateway.TOKEN_HEADER] = token
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, urllib.parse.quote(path, safe="/?&=:#"), body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            conn.close()

    def json(self, path: str, **kwargs) -> Tuple[int, Dict[str, str], dict]:
        status, headers, payload = self.call(path, **kwargs)
        return status, headers, json.loads(payload.decode("utf-8"))


class GatewayHTTPCase(unittest.TestCase):
    """Base class that stands a gateway up over a temp-dir library."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "kb"
        self.backend = seed_local_kb(self.root)
        self.server = RunningGateway(make_app(self.backend))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.close)


# ── token derivation ────────────────────────────────────────────────────────


class TokenTests(unittest.TestCase):
    def test_token_is_sha256_of_the_admin_key(self) -> None:
        self.assertEqual(gateway.derive_token(ADMIN_KEY), TOKEN)
        self.assertEqual(len(gateway.derive_token("x")), 64)

    def test_an_empty_admin_key_cannot_produce_a_token(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            gateway.derive_token("")

    def test_token_from_env_reads_the_named_variable(self) -> None:
        self.assertEqual(
            gateway.token_from_env("CWK_KB_ADMIN_KEY", {"CWK_KB_ADMIN_KEY": ADMIN_KEY}), TOKEN
        )

    def test_an_unset_or_empty_variable_refuses_to_start(self) -> None:
        for env in ({}, {"CWK_KB_ADMIN_KEY": ""}):
            with self.subTest(env=env):
                with self.assertRaises(gateway.GatewayError):
                    gateway.token_from_env("CWK_KB_ADMIN_KEY", env)
        with self.assertRaises(gateway.GatewayError):
            gateway.token_from_env("", {"": ADMIN_KEY})

    def test_comparison_survives_junk_without_raising(self) -> None:
        self.assertTrue(gateway.tokens_match(TOKEN, TOKEN))
        for junk in ("", "nope", TOKEN[:-1], TOKEN + "0", "中文 token", "\x00"):
            with self.subTest(junk=junk):
                self.assertFalse(gateway.tokens_match(junk, TOKEN))


# ── J4: authentication ──────────────────────────────────────────────────────


class J4AuthTests(GatewayHTTPCase):
    """J4 — 错 token → 401；token 面不含任何管理或写动作。"""

    def test_J4_a_wrong_token_is_401_on_every_protected_route(self) -> None:
        for path in ("/query?q=合同", f"/citation?lineage={LINEAGE}"):
            with self.subTest(path=path):
                status, headers, payload = self.server.json(path, token="wrong-token")
                self.assertEqual(status, 401)
                self.assertEqual(payload["error"]["kind"], "unauthorized")
                self.assertTrue(headers["Content-Type"].startswith("application/json"))

    def test_J4_a_missing_token_is_401_too(self) -> None:
        status, _headers, payload = self.server.json("/query?q=合同", token=None)
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_J4_a_token_one_character_off_is_still_401(self) -> None:
        near_miss = TOKEN[:-1] + ("0" if TOKEN[-1] != "0" else "1")
        status, _headers, _payload = self.server.json("/query?q=合同", token=near_miss)
        self.assertEqual(status, 401)

    def test_the_right_token_gets_through(self) -> None:
        status, _headers, payload = self.server.json("/query?q=合同")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_J4_the_token_face_has_no_management_verb(self) -> None:
        """A valid token buys exactly three read routes and nothing else."""
        self.assertEqual(gateway.ROUTES, ("/health", "/query", "/citation"))
        for path in MANAGEMENT_PROBES:
            with self.subTest(path=path):
                status, _headers, payload = self.server.json(path)
                self.assertEqual(status, 404, f"{path} 不该存在")
                self.assertEqual(payload["error"]["kind"], "not_found")
                self.assertEqual(payload["routes"], list(gateway.ROUTES))

    def test_J4_a_management_probe_without_a_token_never_reveals_the_route_table(self) -> None:
        """401 before 404: probing must not be cheaper than authenticating."""
        status, _headers, payload = self.server.json("/admin", token=None)
        self.assertEqual(status, 401)
        self.assertNotIn("routes", payload)

    def test_health_needs_no_token_and_leaks_no_identity(self) -> None:
        status, _headers, payload = self.server.json("/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], gateway.GATEWAY_VERSION)
        self.assertTrue(payload["backend"]["reachable"])
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["write_verbs"], [])
        blob = json.dumps(payload, ensure_ascii=False)
        for secret in ("kb_code", "f" * 32, str(self.root), RAW_PATH, ADMIN_KEY, TOKEN):
            self.assertNotIn(secret, blob, "未鉴权的 /health 泄漏了不该说的东西")

    def test_health_reports_an_unreachable_backend_without_crashing(self) -> None:
        app = make_app(UnreachableBackend())
        response = app.dispatch("GET", "/health", {})
        self.assertEqual(response.status, 200)
        self.assertFalse(response.payload["ok"])
        self.assertFalse(response.payload["backend"]["reachable"])


# ── J2: method and route refusal ────────────────────────────────────────────


class J2WriteAndUnknownRouteTests(GatewayHTTPCase):
    """J2 — 写语义 → 405；未知路径 → 404。"""

    def test_J2_write_methods_are_405_on_a_real_route(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, payload = self.server.json(
                    "/query?q=合同", method=method, body=b'{"q":"x"}'
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), "GET")
                self.assertEqual(payload["error"]["kind"], "method_not_allowed")
                self.assertEqual(payload["allow"], ["GET"])

    def test_J2_write_methods_are_405_even_without_a_token(self) -> None:
        """A write attempt is refused as a write, authenticated or not."""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, _headers, _payload = self.server.json(
                    "/citation", method=method, token=None, body=b"{}"
                )
                self.assertEqual(status, 405)

    def test_J2_write_methods_are_405_on_paths_that_do_not_exist(self) -> None:
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                status, _headers, _payload = self.server.json(
                    "/ingest", method=method, body=b"{}"
                )
                self.assertEqual(status, 405)

    def test_J2_head_and_options_are_refused_as_well(self) -> None:
        for method in ("HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = self.server.call("/health", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), "GET")
        # HEAD must not carry a body even when it is being refused.
        _status, _headers, body = self.server.call("/health", method="HEAD")
        self.assertEqual(body, b"")

    def test_J2_unknown_paths_are_404(self) -> None:
        for path in ("/", "/nope", "/query/extra", "/citation/raw", "/.env"):
            with self.subTest(path=path):
                status, _headers, payload = self.server.json(path)
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"]["kind"], "not_found")

    def test_J2_a_trailing_slash_still_reaches_the_real_route(self) -> None:
        status, _headers, payload = self.server.json("/query/?q=合同")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_J2_red_no_route_ever_reaches_a_write(self) -> None:
        """Drive every route against a backend that raises on any mutation."""
        trap = WriteTrapBackend()
        trap.seed(gateway.RAW_INDEX_REL, dumps(index_document()))
        trap.seed(RAW_PATH, BODY_V1.encode("utf-8"))
        app = make_app(trap)
        probes = [
            ("GET", "/health"),
            ("GET", "/query?q=合同"),
            ("GET", "/query?q=&limit=9999"),
            ("GET", f"/citation?lineage={LINEAGE}"),
            ("GET", f"/citation?lineage={LINEAGE}&version=1"),
            ("GET", f"/citation?lineage={LINEAGE}&version=99"),
            ("GET", "/citation"),
            ("GET", "/unknown"),
            ("POST", "/query"),
            ("PUT", "/citation"),
            ("DELETE", "/health"),
        ]
        for method, target in probes:
            with self.subTest(method=method, target=target):
                response = app.dispatch(method, target, {gateway.TOKEN_HEADER: TOKEN})
                self.assertIsInstance(response.status, int)
        # The red method: prove the trap can actually see a write.
        with self.assertRaises(WriteAttempted):
            trap.write("raw/证明陷阱有效.md", b"x")


class BindTests(unittest.TestCase):
    def test_binding_does_not_do_a_reverse_dns_lookup(self) -> None:
        """``server_name`` is CGI-only; resolving it can stall startup for
        tens of seconds on a network where the reverse lookup times out."""
        app = make_app(MemoryBackend())
        with mock.patch("socket.getfqdn", side_effect=AssertionError("绑定时不该反查 DNS")):
            httpd = gateway.make_server(app, "127.0.0.1", 0)
        try:
            self.assertEqual(httpd.server_name, "127.0.0.1")
            self.assertGreater(httpd.server_address[1], 0)
        finally:
            httpd.server_close()


class TwoProcessConstitutionTests(unittest.TestCase):
    """The write face may depend on the read face; never the other way."""

    @staticmethod
    def imported_modules(path: Path) -> List[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        return names

    def test_the_gateway_never_imports_a_write_verb(self) -> None:
        imported = self.imported_modules(PROJECT / "scripts" / "kb_gateway.py")
        for forbidden in ("kb_create", "kb_wizard", "kb_migrate", "kb_doctor"):
            self.assertNotIn(
                forbidden,
                imported,
                f"网关进程不得引入 {forbidden}——两进程宪法写在 import 图上",
            )

    def test_the_dependency_direction_is_the_one_documented(self) -> None:
        wizard = self.imported_modules(PROJECT / "scripts" / "kb_wizard.py")
        self.assertIn("kb_create", wizard, "向导是写面，应当直接包装 kb_create")
        self.assertIn("kb_gateway", wizard, "查询语义应当复用网关的读函数，而不是抄一份")


# ── J3: live citation ───────────────────────────────────────────────────────


class J3LiveCitationTests(GatewayHTTPCase):
    """J3 — citation 现场从后端拉字节；后端换了内容，sha256 就跟着变。"""

    def citation(self, *, version: Optional[int] = None) -> dict:
        path = f"/citation?lineage={LINEAGE}"
        if version is not None:
            path += f"&version={version}"
        status, headers, payload = self.server.json(path)
        self.assertEqual(status, 200, payload)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        return payload

    def test_J3_changing_the_bytes_changes_the_returned_sha256(self) -> None:
        first = self.citation()
        self.assertEqual(
            first["sha256"], hashlib.sha256(BODY_V1.encode("utf-8")).hexdigest()
        )
        self.assertTrue(first["matches_index"])

        # The NAS-side file is replaced behind the gateway's back.
        (self.root / RAW_PATH).write_bytes(BODY_V2.encode("utf-8"))

        second = self.citation()
        self.assertNotEqual(
            first["sha256"], second["sha256"], "citation 读了缓存——sha256 没有跟着后端变"
        )
        self.assertEqual(
            second["sha256"], hashlib.sha256(BODY_V2.encode("utf-8")).hexdigest()
        )
        self.assertNotEqual(first["excerpt"], second["excerpt"])
        self.assertIn("第二版正文", second["excerpt"])
        # The index still records the old digest, so the drift is reported.
        self.assertFalse(second["matches_index"])
        self.assertEqual(second["index_sha256"], first["sha256"])

    def test_J3_the_index_itself_is_re_read_每次(self) -> None:
        """Re-point the lineage at another file; the next call must follow."""
        self.citation()
        moved = "raw/合同/供货协议-归档.md"
        self.backend.write(moved, BODY_V2.encode("utf-8"))
        self.backend.write(
            gateway.RAW_INDEX_REL,
            dumps(index_document(path=moved)),
        )
        after = self.citation()
        self.assertEqual(
            after["sha256"], hashlib.sha256(BODY_V2.encode("utf-8")).hexdigest()
        )

    def test_the_excerpt_is_the_first_500_characters(self) -> None:
        payload = self.citation()
        self.assertEqual(payload["excerpt"], BODY_V1[: gateway.EXCERPT_CHARS])
        self.assertEqual(payload["excerpt_chars"], gateway.EXCERPT_CHARS)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["bytes"], len(BODY_V1.encode("utf-8")))

    def test_a_short_document_is_not_marked_truncated(self) -> None:
        self.backend.write(RAW_PATH, "短文".encode("utf-8"))
        payload = self.citation()
        self.assertEqual(payload["excerpt"], "短文")
        self.assertFalse(payload["truncated"])

    def test_the_citation_carries_no_path(self) -> None:
        """CLI-SPEC §三: 引文钉 (lineage_id, version)，路径由 locate 即时解析。"""
        payload = self.citation()
        self.assertNotIn("path", payload)
        self.assertNotIn(RAW_PATH, json.dumps(payload, ensure_ascii=False))

    def test_a_pinned_version_reads_that_versions_file(self) -> None:
        payload = self.citation(version=1)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["excerpt"], "第一版存档")

    def test_an_unknown_lineage_or_version_is_404_not_a_wrong_answer(self) -> None:
        status, _headers, payload = self.server.json("/citation?lineage=docdb:nope")
        self.assertEqual(status, 404)
        status, _headers, payload = self.server.json(f"/citation?lineage={LINEAGE}&version=99")
        self.assertEqual(status, 404)
        self.assertIn("99", payload["error"]["message"])

    def test_a_missing_lineage_parameter_is_400(self) -> None:
        status, _headers, payload = self.server.json("/citation")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["kind"], "bad_request")

    def test_a_non_numeric_version_is_400(self) -> None:
        status, _headers, payload = self.server.json(f"/citation?lineage={LINEAGE}&version=abc")
        self.assertEqual(status, 400)

    def test_an_index_entry_pointing_at_a_missing_file_is_503_not_a_crash(self) -> None:
        (self.root / RAW_PATH).unlink()
        status, _headers, payload = self.server.json(f"/citation?lineage={LINEAGE}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["kind"], "backend_unavailable")


# ── query ───────────────────────────────────────────────────────────────────


class QueryRouteTests(GatewayHTTPCase):
    def test_substring_matching_covers_id_title_and_path(self) -> None:
        for needle, expected in (
            ("供货", LINEAGE),
            ("2087519593823322113", LINEAGE),
            ("raw/2026-08", "cwork:2095046023776104449"),
        ):
            with self.subTest(needle=needle):
                _status, _headers, payload = self.server.json(f"/query?q={needle}")
                self.assertEqual([hit["lineage_id"] for hit in payload["results"]], [expected])

    def test_a_hit_carries_lineage_title_version_and_path(self) -> None:
        _status, _headers, payload = self.server.json("/query?q=供货")
        hit = payload["results"][0]
        self.assertEqual(hit["lineage_id"], LINEAGE)
        self.assertEqual(hit["title"], "供货协议")
        self.assertEqual(hit["version"], 2)
        self.assertEqual(hit["path"], RAW_PATH)
        self.assertEqual(hit["status"], "ok")

    def test_matching_is_case_insensitive(self) -> None:
        _status, _headers, payload = self.server.json("/query?q=DOCDB:9990001")
        self.assertEqual(payload["matched"], 1)

    def test_no_hit_is_an_empty_success_not_an_error(self) -> None:
        status, _headers, payload = self.server.json("/query?q=不存在的词")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["matched"], 0)

    def test_an_empty_q_is_400(self) -> None:
        for path in ("/query", "/query?q="):
            with self.subTest(path=path):
                status, _headers, payload = self.server.json(path)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["kind"], "bad_request")

    def test_a_whitespace_only_q_is_400(self) -> None:
        # Dispatched directly: the socket harness percent-encodes what it is
        # given, so an already-encoded ``%20`` would arrive as a literal.
        app = make_app(self.backend)
        response = app.dispatch("GET", "/query?q=%20", {gateway.TOKEN_HEADER: TOKEN})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["error"]["kind"], "bad_request")

    def test_matched_counts_everything_while_results_are_paged(self) -> None:
        _status, _headers, payload = self.server.json("/query?q=docdb&limit=1")
        self.assertEqual(payload["matched"], 2)
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(len(payload["results"]), 1)

    def test_results_are_ordered_by_lineage_id(self) -> None:
        _status, _headers, payload = self.server.json("/query?q=raw")
        ids = [hit["lineage_id"] for hit in payload["results"]]
        self.assertEqual(ids, sorted(ids))

    def test_a_library_without_an_index_is_503(self) -> None:
        (self.root / gateway.RAW_INDEX_REL).unlink()
        status, _headers, payload = self.server.json("/query?q=合同")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["kind"], "backend_unavailable")


class IndexParsingTests(unittest.TestCase):
    """The index reader is deliberately tolerant: RT-043 owns the writer."""

    def test_the_list_shape_is_accepted_too(self) -> None:
        backend = MemoryBackend()
        backend.write(
            gateway.RAW_INDEX_REL,
            dumps({"entries": [{"lineage_id": LINEAGE, "path": RAW_PATH, "title": "供货协议"}]}),
        )
        index = gateway.load_index(backend)
        self.assertEqual(list(index), [LINEAGE])
        self.assertEqual(index[LINEAGE].path, RAW_PATH)

    def test_a_row_missing_every_optional_field_still_parses(self) -> None:
        backend = MemoryBackend()
        backend.write(gateway.RAW_INDEX_REL, dumps({"entries": {LINEAGE: {}}}))
        entry = gateway.load_index(backend)[LINEAGE]
        self.assertEqual(entry.version, 1)
        self.assertEqual(entry.status, "unknown")
        self.assertEqual(entry.title, "")

    def test_a_freshly_built_library_has_an_empty_index_not_an_error(self) -> None:
        backend = MemoryBackend()
        backend.write(gateway.RAW_INDEX_REL, dumps({"entries": {}}))
        self.assertEqual(gateway.load_index(backend), {})

    def test_a_missing_index_file_is_reported_as_backend_unavailable(self) -> None:
        with self.assertRaises(gateway.BackendUnavailable):
            gateway.load_index(MemoryBackend())

    def test_a_corrupt_index_is_reported_not_swallowed(self) -> None:
        backend = MemoryBackend()
        backend.write(gateway.RAW_INDEX_REL, b"{ this is not json")
        with self.assertRaises(gateway.BackendUnavailable):
            gateway.load_index(backend)

    def test_limits_are_clamped_to_the_ceiling(self) -> None:
        self.assertEqual(gateway.clamp_limit("5"), 5)
        self.assertEqual(gateway.clamp_limit(10**9), gateway.MAX_LIMIT)
        for junk in ("", "abc", None, 0, -3):
            with self.subTest(junk=junk):
                self.assertEqual(gateway.clamp_limit(junk), gateway.DEFAULT_LIMIT)

    def test_an_entry_without_a_path_cannot_be_cited(self) -> None:
        entry = gateway.IndexEntry(lineage_id=LINEAGE)
        with self.assertRaises(gateway.GatewayError):
            gateway.resolve_version(entry, None)


# ── J5: JSON everywhere ─────────────────────────────────────────────────────


class J5JsonContractTests(GatewayHTTPCase):
    """J5 — 每个响应都是 application/json，且 body 一定 json.loads 得动。"""

    def test_J5_every_status_class_answers_parseable_json(self) -> None:
        probes = [
            ("GET", "/health", None, 200),
            ("GET", "/query?q=合同", TOKEN, 200),
            ("GET", f"/citation?lineage={LINEAGE}", TOKEN, 200),
            ("GET", "/query", TOKEN, 400),
            ("GET", "/query?q=合同", "wrong", 401),
            ("GET", "/admin", TOKEN, 404),
            ("POST", "/query", TOKEN, 405),
            ("PUT", "/citation", None, 405),
        ]
        for method, path, token, expected in probes:
            with self.subTest(method=method, path=path):
                status, headers, body = self.server.call(path, method=method, token=token)
                self.assertEqual(status, expected)
                self.assertTrue(
                    headers["Content-Type"].startswith("application/json"),
                    headers["Content-Type"],
                )
                payload = json.loads(body.decode("utf-8"))
                self.assertIsInstance(payload, dict)
                self.assertIn("ok", payload)
                self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_J5_responses_are_marked_uncacheable(self) -> None:
        _status, headers, _body = self.server.call(f"/citation?lineage={LINEAGE}")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_J5_the_token_never_appears_in_any_response(self) -> None:
        for path in ("/health", "/query?q=合同", f"/citation?lineage={LINEAGE}", "/admin"):
            with self.subTest(path=path):
                _status, _headers, body = self.server.call(path)
                text = body.decode("utf-8")
                self.assertNotIn(TOKEN, text)
                self.assertNotIn(ADMIN_KEY, text)


# ── CLI ─────────────────────────────────────────────────────────────────────


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "kb"
        seed_local_kb(self.root)
        self.addCleanup(self.tmp.cleanup)

    def run_cli(self, argv, env=None) -> Tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        patched = dict(os.environ)
        patched.update(env or {})
        with mock.patch.dict(os.environ, patched, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = gateway.main(argv)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_check_prints_the_startup_card_without_binding(self) -> None:
        code, payload, _err = self.run_cli(
            ["--admin-key-env", "KB_ADMIN", "--root", str(self.root), "--check"],
            env={"KB_ADMIN": ADMIN_KEY},
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["routes"], list(gateway.ROUTES))
        self.assertEqual(payload["write_verbs"], [])
        self.assertEqual(payload["auth"]["key_env"], "KB_ADMIN")
        self.assertTrue(payload["backend"]["reachable"])

    def test_the_startup_card_names_the_variable_never_the_key(self) -> None:
        _code, payload, _err = self.run_cli(
            ["--admin-key-env", "KB_ADMIN", "--root", str(self.root), "--check"],
            env={"KB_ADMIN": ADMIN_KEY},
        )
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(ADMIN_KEY, blob)
        self.assertNotIn(TOKEN, blob)

    def test_a_missing_admin_key_variable_refuses_to_start_in_json(self) -> None:
        code, payload, err = self.run_cli(
            ["--admin-key-env", "KB_ADMIN_ABSENT", "--root", str(self.root), "--check"]
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("KB_ADMIN_ABSENT", payload["error"]["message"])
        self.assertIn("网关启动失败", err)

    def test_a_plaintext_credential_flag_is_refused(self) -> None:
        code, payload, _err = self.run_cli(
            ["--admin-key-env", "KB_ADMIN", "--token", "leaked", "--check"],
            env={"KB_ADMIN": ADMIN_KEY},
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "PlaintextCredential")

    def test_file_urls_resolve_to_a_local_root(self) -> None:
        self.assertEqual(gateway.parse_root(None), None)
        self.assertEqual(gateway.parse_root("/tmp/kb"), "/tmp/kb")
        self.assertEqual(gateway.parse_root("file:///tmp/kb"), "/tmp/kb")
        self.assertEqual(gateway.parse_root("file://localhost/tmp/kb"), "/tmp/kb")
        self.assertEqual(gateway.parse_root("file:///tmp/%E5%BA%93"), "/tmp/库")
        with self.assertRaises(gateway.GatewayError):
            gateway.parse_root("file://nas.example.lan/kb")

    def test_a_file_url_root_serves_the_same_library(self) -> None:
        code, payload, _err = self.run_cli(
            ["--admin-key-env", "KB_ADMIN", "--root", f"file://{self.root}", "--check"],
            env={"KB_ADMIN": ADMIN_KEY},
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["backend"]["reachable"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
