"""RT-058: portal / console contract tests.

Two properties carry most of the weight here:

1. the portal cannot reach management data — asserted against the module's
   imports, not against its routing table, because a route can be added by
   accident while an import cannot be added by accident;
2. a rejected authentication attempt is recorded — the gap RT-056 shipped
   with, where somebody guessing at the admin key left no trace.

Everything runs on synthetic fixtures and loopback sockets: no OPS, no real
registry, no real corpus.
"""
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
import kb_portal
from kb_ledger import dumps
from kb_storage import LocalFSBackend


class PortalIsolationTests(unittest.TestCase):
    """The portal is the LAN-facing face; it must have no way to read anything."""

    FORBIDDEN = ("kb_ops", "kb_storage", "kb_token", "kb_ledger", "kb_gateway", "kb_admin", "kb_ingest")

    def test_portal_imports_no_data_module(self):
        source = (PROJECT / "scripts" / "kb_portal.py").read_text(encoding="utf-8")
        for name in self.FORBIDDEN:
            self.assertNotIn(
                f"import {name}", source,
                f"门户导入了 {name}——它一旦能读管理数据，绑到局域网就等于把管理面也暴露了",
            )
        for name in self.FORBIDDEN:
            self.assertFalse(
                hasattr(kb_portal, name),
                f"kb_portal 暴露了 {name} 属性，隔离被绕开了",
            )

    def test_portal_has_no_registry_or_audit_configuration(self):
        """No config knob may point the portal at a registry or an audit file."""
        source = (PROJECT / "scripts" / "kb_portal.py").read_text(encoding="utf-8")
        for needle in ("KB_REGISTRY_PATH", "KB_ADMIN_AUDIT_PATH", "KB_LOCAL_LIBRARY_ROOT", "KB_ADMIN_KEY"):
            self.assertNotIn(needle, source)


class PortalAppTests(unittest.TestCase):
    def app(self, **env):
        return kb_portal.PortalApp(env)

    def test_page_lists_banks_and_console_entry(self):
        status, content_type, body, _ = self.app().handle("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        page = body.decode("utf-8")
        for bank_id, _ in kb_portal.DEFAULT_BANKS:
            self.assertIn(bank_id, page)
        # RT-059 改了按钮文案；这条判据守的是「站内有控制台入口」，不是某个字面串。
        self.assertIn("管理控制台", page)
        self.assertIn(kb_portal.DEFAULT_CONSOLE_URL, page)

    def test_console_link_rejects_non_http_scheme(self):
        """Operator configuration must not become script execution in a browser."""
        page = self.app(KB_PORTAL_CONSOLE_URL="javascript:alert(1)").page()
        self.assertNotIn("javascript:", page)
        self.assertIn("管理控制台未配置", page)
        page = self.app(KB_PORTAL_CONSOLE_URL="https://kb.example.internal/console").page()
        self.assertIn("https://kb.example.internal/console", page)

    def test_bank_names_are_escaped(self):
        page = self.app(KB_PORTAL_BANKS="<script>evil</script>:说明").page()
        self.assertNotIn("<script>evil", page)
        self.assertIn("&lt;script&gt;evil", page)

    def test_bank_parsing(self):
        self.assertEqual(kb_portal.parse_banks(None), list(kb_portal.DEFAULT_BANKS))
        self.assertEqual(kb_portal.parse_banks("   "), list(kb_portal.DEFAULT_BANKS))
        self.assertEqual(kb_portal.parse_banks(",,"), list(kb_portal.DEFAULT_BANKS))
        self.assertEqual(kb_portal.parse_banks("a:甲, b"), [("a", "甲"), ("b", "")])

    def test_unknown_and_api_routes_are_plain_404(self):
        for route in ("/api/overview", "/api/audit", "/console", "/nope"):
            status, _, body, _ = self.app().handle("GET", route)
            self.assertEqual(status, 404, route)
            self.assertEqual(json.loads(body), {"error": "not_found"})

    def test_write_methods_are_refused(self):
        status, _, _, headers = self.app().handle("POST", "/")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD")


class PortalHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = kb_portal.PortalApp({})
        self.server = kb_portal.PortalHTTPServer(("127.0.0.1", 0), self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_real_http_serves_page_with_hardening_headers(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("CWK 知识库服务", response.read().decode("utf-8"))

    def test_real_http_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["status"], "ok")


class ConsoleAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        library_root = root / "libraries"
        (library_root / "demo").mkdir(parents=True)
        backend = LocalFSBackend(library_root / "demo")
        backend.write("_system/raw-index.json", dumps({"schema": "cwk.kb.raw-index.v1", "entries": {
            "doc:1": {"path": "logical-only", "title": "Synthetic", "version": 1,
                      "sha256": hashlib.sha256(b"x").hexdigest(), "status": "ok", "artifact_kind": "document"}}}))
        registry = root / "registry.json"
        registry.write_text(json.dumps({"tokens": []}), encoding="utf-8")
        self.audit = root / "audit.jsonl"
        self.app = kb_admin.AdminApp({
            "KB_ADMIN_ENABLED": "true", "KB_ADMIN_KEY_ENV": "TEST_ADMIN_KEY", "TEST_ADMIN_KEY": "unit-secret",
            "KB_LOCAL_LIBRARY_ROOT": str(library_root), "KB_REGISTRY_PATH": str(registry),
            "KB_ADMIN_AUDIT_PATH": str(self.audit),
        })

    def events(self) -> list[dict]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text("utf-8").splitlines() if line.strip()]

    def test_rejected_attempt_is_recorded_without_the_supplied_key(self):
        self.app.handle("GET", "/api/overview", {})
        self.app.handle("GET", "/api/overview", {"X-KB-Admin-Key": "guess-me-9999"})
        events = self.events()
        self.assertEqual([e["outcome"] for e in events], ["unauthorized", "unauthorized"])
        self.assertEqual([e["action"] for e in events], ["overview", "overview"])
        self.assertEqual({e["status"] for e in events}, {401})
        self.assertNotIn("guess-me-9999", self.audit.read_text("utf-8"))

    def test_reading_the_audit_log_is_itself_audited(self):
        self.app.handle("GET", "/api/audit", {"X-KB-Admin-Key": "unit-secret"})
        self.assertEqual([e["action"] for e in self.events()], ["audit"])

    def test_disabled_console_still_records_the_attempt(self):
        """Disabled is the state most likely to be probed; it must not go dark."""
        audit = Path(self.tmp.name) / "disabled-audit.jsonl"
        app = kb_admin.AdminApp({"KB_ADMIN_ENABLED": "false", "KB_ADMIN_AUDIT_PATH": str(audit)})
        self.assertEqual(app.handle("GET", "/api/overview", {})[0], 401)
        rows = [json.loads(line) for line in audit.read_text("utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["outcome"], "unauthorized")

    def test_audit_action_name_is_bounded(self):
        self.assertEqual(kb_admin._action_for("/api/jobs/create"), "jobs.create")
        self.assertEqual(kb_admin._action_for("/api/"), "api")
        self.assertEqual(kb_admin._action_for("/api/x\ny"), "xy")
        self.assertLessEqual(len(kb_admin._action_for("/api/" + "z" * 500)), 64)

    def test_successful_reads_are_still_audited(self):
        self.app.handle("GET", "/api/overview", {"X-KB-Admin-Key": "unit-secret"})
        self.app.handle("GET", "/api/services", {"X-KB-Admin-Key": "unit-secret"})
        self.assertEqual([e["outcome"] for e in self.events()], ["ok", "ok"])


class SnapshotLibraryTests(unittest.TestCase):
    """The shape production actually stores: a doc_id index plus one dir per bank."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / "rag-sources"
        for bank, count in (("cwork-3m", 3), ("spbp-2027", 2)):
            (self.sources / bank).mkdir(parents=True)
            for i in range(count):
                (self.sources / bank / f"{i}.txt").write_text("x", encoding="utf-8")
        self.index = self.root / "rag-index.json"
        self.write_index({
            "cwork:1": "cwork-3m/0.txt", "cwork:2": "cwork-3m/1.txt", "cwork:3": "cwork-3m/2.txt",
            "sp:1": "spbp-2027/0.txt", "sp:2": "spbp-2027/1.txt",
        })

    def write_index(self, mapping) -> None:
        self.index.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    def app(self, **extra):
        env = {"KB_ADMIN_ENABLED": "true", "KB_SNAPSHOT_INDEX": str(self.index),
               "KB_SNAPSHOT_ROOT": str(self.sources)}
        env.update(extra)
        return kb_admin.AdminApp(env)

    def libraries(self, **extra):
        return {row["kb_id"]: row for row in self.app(**extra)._snapshot_libraries()}

    def test_banks_are_grouped_with_counts(self):
        rows = self.libraries()
        self.assertEqual(sorted(rows), ["cwork-3m", "spbp-2027"])
        self.assertEqual((rows["cwork-3m"]["total"], rows["cwork-3m"]["readable_total"]), (3, 3))
        self.assertEqual((rows["spbp-2027"]["total"], rows["spbp-2027"]["readable_total"]), (2, 2))
        self.assertIsNone(rows["cwork-3m"]["error"])
        self.assertEqual(rows["cwork-3m"]["source"], "snapshot")

    def test_a_missing_file_is_reported_not_silently_counted(self):
        """An index that outlived its files must not look healthy."""
        (self.sources / "cwork-3m" / "1.txt").unlink()
        rows = self.libraries()
        self.assertEqual((rows["cwork-3m"]["total"], rows["cwork-3m"]["readable_total"]), (3, 2))
        self.assertIn("缺失", rows["cwork-3m"]["error"])

    def test_index_entries_cannot_point_outside_the_snapshot_root(self):
        outside = self.root / "secret.txt"
        outside.write_text("no", encoding="utf-8")
        self.write_index({"a": "cwork-3m/../../secret.txt", "b": "cwork-3m/0.txt"})
        rows = self.libraries()
        # The traversal entry is counted under its declared bank but must never
        # be confirmed readable — otherwise the index could probe the filesystem.
        self.assertEqual(rows["cwork-3m"]["total"], 2)
        self.assertEqual(rows["cwork-3m"]["readable_total"], 1)

    def test_unreadable_index_is_named_not_rendered_as_empty(self):
        self.index.write_text("{not json", encoding="utf-8")
        rows = self.app()._snapshot_libraries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "索引不可读")

    def test_missing_index_configuration_yields_nothing(self):
        self.assertEqual(kb_admin.AdminApp({"KB_ADMIN_ENABLED": "true"})._snapshot_libraries(), [])

    def test_malformed_entries_are_skipped(self):
        self.write_index({"a": "", "b": "no-slash", "c": "/absolute/x", "d": "cwork-3m/0.txt"})
        rows = self.libraries()
        self.assertEqual(list(rows), ["cwork-3m"])
        self.assertEqual(rows["cwork-3m"]["total"], 1)

    def test_existence_check_is_bounded(self):
        """A far larger bank must not turn one request into unbounded stat calls."""
        original = kb_admin.MAX_SNAPSHOT_STATS
        kb_admin.MAX_SNAPSHOT_STATS = 2
        self.addCleanup(setattr, kb_admin, "MAX_SNAPSHOT_STATS", original)
        rows = self.libraries()
        checked = sum(r["readable_total"] or 0 for r in rows.values())
        self.assertLessEqual(checked, 2)
        self.assertTrue(any(r["error"] == "未逐一核对" for r in rows.values()))

    def test_overview_merges_snapshot_with_tree_and_survives_registry_failure(self):
        app = self.app(KB_REGISTRY_PATH=str(self.root / "nope.json"),
                       KB_LOCAL_LIBRARY_ROOT=str(self.root / "no-tree"))
        payload = app._overview()
        banks = [row["kb_id"] for row in payload["libraries"]]
        self.assertIn("cwork-3m", banks)
        self.assertIn("spbp-2027", banks)


class ConsolePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = kb_admin.AdminApp({"KB_ADMIN_ENABLED": "true"})

    def page(self) -> str:
        status, payload, headers = self.app.handle("GET", "/console", {})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        return payload["html"]

    def test_console_is_served_at_both_paths(self):
        self.assertEqual(self.app.handle("GET", "/", {})[1], self.app.handle("GET", "/console", {})[1])

    def test_key_is_never_persisted_by_the_page(self):
        page = self.page()
        for storage in ("localStorage", "sessionStorage", "document.cookie"):
            self.assertNotIn(storage, page, f"控制台把密钥交给了 {storage}——刷新即失效是它唯一的保管策略")

    def test_key_is_asked_once_not_per_click(self):
        page = self.page()
        self.assertNotIn("prompt(", page)
        self.assertIn("id='key'", page)
        self.assertIn("id='relock'", page)

    def test_page_distinguishes_failure_modes(self):
        page = self.page()
        self.assertIn("连不上管理服务", page)
        self.assertIn("密钥不对", page)
        self.assertIn("不是鉴权问题", page)

    def test_page_shows_which_host_is_being_viewed(self):
        self.assertIn("location.host", self.page())

    def test_page_renders_tables_not_raw_json(self):
        page = self.page()
        self.assertIn("<table>", page)
        self.assertNotIn("JSON.stringify", page)


if __name__ == "__main__":
    unittest.main()
