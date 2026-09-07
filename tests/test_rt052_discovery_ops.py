"""RT-052: discovery isolation and read-only OPS checks."""
from __future__ import annotations
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import kb_gateway as gateway
import kb_ops
import kb_token
from kb_ledger import dumps
from kb_storage import MemoryBackend
NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)
ADMIN = "admin-secret"
def seeded(title="库"):
    backend = MemoryBackend()
    backend.write(gateway.RAW_INDEX_REL, dumps({"schema": "cwk.kb.raw-index.v1", "entries": {"docdb:1": {"path": "raw/a.md", "title": title, "version": 1, "sha256": hashlib.sha256(b"x").hexdigest(), "status": "ok", "artifact_kind": "document"}, "docdb:2": {"path": "raw/b", "title": "占位", "version": 1, "sha256": "2" * 64, "status": "placeholder", "artifact_kind": "placeholder"}}}))
    backend.write("kb.json", dumps({"display_name": title}))
    return backend
class Trap(MemoryBackend):
    def read(self, path): raise AssertionError("hidden backend must not be read")
class DiscoveryTests(unittest.TestCase):
    def registry(self, scope):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); path = Path(tmp.name) / "tokens.json"
        data = kb_token.new_registry(now=NOW); ident = kb_token.VerifiedIdentity("owner-test", verified_at="2026-09-07T00:00:00Z")
        _, plain = kb_token.issue_token(data, identity=ident, raw_agent_id="agent", kb_ids=scope, ttl_days=30, now=NOW, actor="test", reason="test")
        kb_token.save_registry(path, data, now=NOW); return kb_token.TokenFile(path), plain
    def app(self, scope=("visible",)):
        tokens, plain = self.registry(scope)
        return gateway.GatewayApp(seeded("Visible"), hashlib.sha256(ADMIN.encode()).hexdigest(), tokens=tokens, kb_id="visible", kb_mounts={"visible": seeded("Visible"), "hidden": Trap()}), plain
    def test_binding_scope_filters_before_backend_io_and_empty_is_200(self):
        app, token = self.app(); response = app.dispatch("GET", "/v2/kb/libraries", {"X-KB-Token": token})
        self.assertEqual(response.status, 200); self.assertEqual([x["kb_id"] for x in response.payload["libraries"]], ["visible"]); self.assertEqual(response.headers["Cache-Control"], "no-store")
        empty, token = self.app(("unmounted",)); self.assertEqual(empty.dispatch("GET", "/v2/kb/libraries", {"X-KB-Token": token}).payload["libraries"], [])
    def test_admin_does_not_need_registry_and_readiness_unknown(self):
        app = gateway.GatewayApp(seeded(), hashlib.sha256(ADMIN.encode()).hexdigest(), kb_id="a", kb_mounts={"a": seeded()})
        response = app.dispatch("GET", "/v2/kb/libraries", {"X-KB-Token": hashlib.sha256(ADMIN.encode()).hexdigest()})
        self.assertEqual(response.status, 200); self.assertEqual(response.payload["libraries"][0]["lexical_status"], "unknown")
        self.assertEqual(app.dispatch("GET", "/v2/kb/libraries", {"X-KB-Token": "bad"}).status, 401)
class OpsTests(unittest.TestCase):
    def test_registry_failure_is_nonzero_and_output_never_echoes_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reg = Path(tmp) / "libraries", Path(tmp) / "tokens.json"; root.mkdir(); reg.write_text('{"token_sha256":"LEAK","owner_ref":"NO"}', "utf-8")
            payload, code = kb_ops.status(root, reg); rendered = json.dumps(payload)
            self.assertEqual(code, 2); self.assertNotIn("LEAK", rendered); self.assertNotIn("owner_ref", rendered); self.assertEqual(payload["registry_status"], "registry_unavailable")
    def test_static_ops_has_no_write_surface(self):
        source = (PROJECT / "scripts" / "kb_ops.py").read_text("utf-8")
        for forbidden in (".write(", ".mkdir(", ".remove(", "save_registry", "subprocess", "os.system"):
            self.assertNotIn(forbidden, source)
