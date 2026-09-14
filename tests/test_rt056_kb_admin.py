from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_admin
from kb_ledger import dumps
from kb_storage import LocalFSBackend


class AdminHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.library_root = root / "libraries"
        (self.library_root / "demo").mkdir(parents=True)
        backend = LocalFSBackend(self.library_root / "demo")
        backend.write("_system/raw-index.json", dumps({"schema": "cwk.kb.raw-index.v1", "entries": {"doc:1": {"path": "logical-only", "title": "Synthetic", "version": 1, "sha256": hashlib.sha256(b"x").hexdigest(), "status": "ok", "artifact_kind": "document"}}}))
        self.registry = root / "registry.json"
        self.registry.write_text(json.dumps({"tokens": [{"token_id": "kbtk_safe_12345678", "token_sha256": "SECRET-DIGEST", "owner_ref_salt": "SECRET-SALT", "owner_ref": "SECRET-OWNER", "kb_ids": ["demo"], "created_at": "2026-09-01T00:00:00Z", "expires_at": "2099-10-01T00:00:00Z", "revoked_at": None}]}), encoding="utf-8")
        self.audit = root / "audit.jsonl"
        self.env = {
            "KB_ADMIN_ENABLED": "true", "KB_ADMIN_KEY_ENV": "TEST_ADMIN_KEY", "TEST_ADMIN_KEY": "unit-secret",
            "KB_LOCAL_LIBRARY_ROOT": str(self.library_root), "KB_REGISTRY_PATH": str(self.registry),
            "KB_ADMIN_AUDIT_PATH": str(self.audit), "KB_ADMIN_WRITE_ENABLED": "false",
            "KB_GATEWAY_URL": "http://127.0.0.1:1/health", "KB_OPS_URL": "http://127.0.0.1:1/health",
        }
        self.app = kb_admin.AdminApp(self.env)
        self.server = kb_admin.AdminHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path: str, method="GET", key=None):
        headers = {} if key is None else {"X-KB-Admin-Key": key}
        req = urllib.request.Request(self.base + path, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_auth_disabled_and_health(self):
        self.assertEqual(self.request("/api/overview")[0], 401)
        self.assertEqual(self.request("/api/overview", key="wrong")[0], 401)
        self.assertEqual(self.request("/healthz")[0], 200)
        disabled = kb_admin.AdminApp({"KB_ADMIN_ENABLED": "false"})
        self.assertEqual(disabled.handle("GET", "/api/overview", {})[0], 503)
        self.assertEqual(disabled.handle("GET", "/healthz", {})[0], 503)

    def test_overview_redacts_registry_and_reads_real_index(self):
        status, payload = self.request("/api/overview", key="unit-secret")
        self.assertEqual(status, 200)
        self.assertEqual(payload["libraries"][0]["total"], 1)
        self.assertEqual(payload["tokens"][0]["scope"], ["demo"])
        rendered = json.dumps(payload)
        for secret in ("SECRET-DIGEST", "SECRET-SALT", "SECRET-OWNER", "token_sha256", "owner_ref_salt", str(self.library_root), "logical-only"):
            self.assertNotIn(secret, rendered)

    def test_services_health_timeout_is_bounded_and_audit_is_redacted(self):
        status, payload = self.request("/api/services", key="unit-secret")
        self.assertEqual(status, 200)
        self.assertEqual({row["status"] for row in payload["services"]}, {"unhealthy"})
        self.assertLessEqual(payload["timeout_seconds"], 3)
        self.assertEqual(self.request("/api/audit", key="unit-secret")[0], 200)
        audit = json.dumps(self.request("/api/audit", key="unit-secret")[1])
        self.assertNotIn("unit-secret", audit)
        self.assertNotIn(str(self.audit), audit)

    def test_jobs_are_safe_placeholders_and_do_not_change_kb(self):
        before = sorted(str(path.relative_to(self.library_root)) for path in self.library_root.rglob("*"))
        self.assertEqual(self.request("/api/jobs/create", method="POST", key="unit-secret")[0], 403)
        self.assertEqual(self.request("/api/jobs/ingest", method="POST", key="unit-secret")[0], 403)
        self.app.write_enabled = True
        self.assertEqual(self.request("/api/jobs/ingest", method="POST", key="unit-secret")[0], 501)
        after = sorted(str(path.relative_to(self.library_root)) for path in self.library_root.rglob("*"))
        self.assertEqual(before, after)

    def test_ui_is_native_html_and_unknown_errors_are_generic(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req) as response:
            body = response.read().decode("utf-8")
        self.assertIn("知识库管理台", body)
        self.assertIn("/api/overview", body)
        self.assertIn("fetch(p", body)
        self.assertEqual(self.request("/not-a-route", key="unit-secret")[0], 404)


if __name__ == "__main__":
    unittest.main()
