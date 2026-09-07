"""RT-052: discovery isolation and read-only OPS checks."""
from __future__ import annotations
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
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

    def test_authorized_scope_is_request_local_under_interleaving(self):
        """A later request must not overwrite an earlier request's auth set."""
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "tokens.json"
        data = kb_token.new_registry(now=NOW)
        ident = kb_token.VerifiedIdentity("owner-test", verified_at="2026-09-07T00:00:00Z")
        _, token_a = kb_token.issue_token(data, identity=ident, raw_agent_id="agent-a", kb_ids=["a"], ttl_days=30, now=NOW, actor="test", reason="test")
        _, token_b = kb_token.issue_token(data, identity=ident, raw_agent_id="agent-b", kb_ids=["b"], ttl_days=30, now=NOW, actor="test", reason="test")
        kb_token.save_registry(path, data, now=NOW)
        app = gateway.GatewayApp(
            seeded("A"), hashlib.sha256(ADMIN.encode()).hexdigest(),
            tokens=kb_token.TokenFile(path), kb_id="a",
            kb_mounts={"a": seeded("A"), "b": seeded("B")},
        )
        refusal_a, scope_a = app.authorize_libraries({"X-KB-Token": token_a})
        refusal_b, scope_b = app.authorize_libraries({"X-KB-Token": token_b})
        self.assertIsNone(refusal_a); self.assertIsNone(refusal_b)
        response_a = app.v2_dispatch("libraries", {}, libraries_scope=scope_a)
        response_b = app.v2_dispatch("libraries", {}, libraries_scope=scope_b)
        self.assertEqual([row["kb_id"] for row in response_a.payload["libraries"]], ["a"])
        self.assertEqual([row["kb_id"] for row in response_b.payload["libraries"]], ["b"])
class OpsTests(unittest.TestCase):
    def test_registry_failure_is_nonzero_and_output_never_echoes_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "tokens.json"; reg.write_text('{"token_sha256":"LEAK","owner_ref":"NO"}', "utf-8")
            payload, code = kb_ops.status({}, reg); rendered = json.dumps(payload)
            self.assertEqual(code, 2); self.assertNotIn("LEAK", rendered); self.assertNotIn("owner_ref", rendered); self.assertEqual(payload["registry_status"], "registry_unavailable")

    def test_injected_survey_reads_readiness_not_lexical_index_and_never_mutates(self):
        backend = seeded()
        before = dict(backend.files)
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "tokens.json"
            reg.write_text('{"records": []}', "utf-8")
            payload, code = kb_ops.status({"a": backend}, reg)
        self.assertEqual(code, 0)
        self.assertEqual(payload["libraries"][0]["lexical_status"], "unknown")
        self.assertEqual(backend.files, before)
        self.assertNotIn("lexical-index", "\n".join(backend.files))

    def test_nas_builds_each_explicit_mount_once_and_subset_reports_difference(self):
        class Closable:
            def __init__(self, backend): self.backend, self.closed = backend, 0
            def read(self, path): return self.backend.read(path)
            def logout(self): self.closed += 1
        built = {name: Closable(seeded(name)) for name in ("a", "b", "c")}
        calls = []
        def factory(kind, *, prefix="", **_kwargs):
            calls.append((kind, prefix)); return built[prefix]
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "tokens.json"; reg.write_text('{"records": []}', "utf-8")
            out = io.StringIO()
            with mock.patch.object(kb_ops, "build_backend", side_effect=factory), redirect_stdout(out):
                code = kb_ops.main(["status", "--backend", "nas", "--mounts", "a,b,c", "--kb", "a", "--registry", str(reg), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("nas", "a"), ("nas", "b"), ("nas", "c")])
        self.assertEqual([row["kb_id"] for row in payload["libraries"]], ["a"])
        self.assertEqual(payload["mount_differences"]["mounted_not_selected"], ["b", "c"])
        self.assertEqual([backend.closed for backend in built.values()], [1, 1, 1])

    def test_nas_requires_explicit_mounts(self):
        with self.assertRaises(SystemExit) as raised:
            kb_ops.main(["status", "--backend", "nas"])
        self.assertEqual(raised.exception.code, 2)

    def test_local_discovers_mounts_from_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reg = Path(tmp) / "libraries", Path(tmp) / "tokens.json"
            (root / "a").mkdir(parents=True); (root / "b").mkdir()
            reg.write_text('{"records": []}', "utf-8")
            calls, out = [], io.StringIO()
            def factory(kind, *, root=None, **_kwargs):
                calls.append((kind, Path(root).name)); return seeded(Path(root).name)
            with mock.patch.object(kb_ops, "build_backend", side_effect=factory), redirect_stdout(out):
                code = kb_ops.main(["status", "--root", str(root), "--registry", str(reg), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("local", "a"), ("local", "b")])
        self.assertEqual([row["kb_id"] for row in json.loads(out.getvalue())["libraries"]], ["a", "b"])

    def test_static_ops_has_no_write_surface(self):
        source = (PROJECT / "scripts" / "kb_ops.py").read_text("utf-8")
        for forbidden in (".write(", ".mkdir(", ".remove(", "save_registry", "subprocess", "os.system", "lexical-index"):
            self.assertNotIn(forbidden, source)
