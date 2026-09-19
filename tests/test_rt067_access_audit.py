"""RT-067 判据：访问审计只记"谁碰了哪个库、结果如何"，绝不记内容。

为什么要有这条审计：8787/8790 为防正文泄露完全不记请求，代价在 RT-062 兑现了——
读原文的洞开着好几天，事后无法确认有没有被利用。

为什么它本身也危险：审计是最容易慢慢长出内容字段的地方（"就加个 query 方便排查"）。
所以这里既锁"记了该记的"，也锁"没记不该记的"：字段白名单 + 真实请求穿透检查。

默认关闭：没配路径就一个字节都不写。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import kb_auth  # noqa: E402
from adapters.rag_answer import server as rag_server  # noqa: E402
from adapters.rag_answer.pipeline import LexicalRetriever, RAGPipeline  # noqa: E402
from adapters.rag_answer.resolver import DocResolver  # noqa: E402

TOKEN = "synthetic-audit-token"
SECRET_QUERY = "绝密查询词-不该出现在审计里"
SECRET_TEXT = "绝密正文-不该出现在审计里"
DOC_ID = "doc-with-a-telling-name"


def registry(path: Path, *, banks=("bank-a",), principal="person:16:17", revoked=False):
    path.write_text(json.dumps({
        "schema": kb_auth.REGISTRY_SCHEMA,
        "tokens": [{
            "token_sha256": sha256(TOKEN.encode()).hexdigest(),
            "token_id": "tok-audit", "principal": principal, "kb_ids": list(banks),
            "expires_at": "2099-01-01T00:00:00Z", "revoked": revoked,
        }],
    }), encoding="utf-8")


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class AuditRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "tokens.json"
        self.audit = self.root / "nested" / "access.jsonl"
        registry(self.registry)

    def authorize(self, token, bank, *, endpoint="query", audit=True, **env):
        base = {"RAG_AUTH_ENABLED": "true", "RAG_AUTH_REGISTRY": str(self.registry)}
        if audit:
            base[kb_auth.ENV_AUDIT_PATH] = str(self.audit)
        base.update(env)
        with patch.dict(os.environ, base, clear=False):
            if not audit:
                os.environ.pop(kb_auth.ENV_AUDIT_PATH, None)
            return kb_auth.authorize({"X-KB-Token": token} if token else {}, bank,
                                     endpoint=endpoint, client="10.0.0.7")

    def test_an_allowed_request_is_recorded(self):
        self.assertIsNone(self.authorize(TOKEN, "bank-a"))
        row = lines(self.audit)[-1]
        self.assertEqual(row["status"], 200)
        self.assertEqual((row["bank"], row["endpoint"], row["reason"]), ("bank-a", "query", "authorized"))
        self.assertEqual((row["token_id"], row["principal"]), ("tok-audit", "person:16:17"))
        self.assertEqual(row["client"], "10.0.0.7")
        self.assertTrue(row["ts"].endswith("Z"))

    def test_every_refusal_is_recorded_with_its_reason(self):
        cases = [
            (None, "bank-a", 401, "unknown_token"),
            ("wrong-token", "bank-a", 401, "unknown_token"),
            (TOKEN, "bank-b", 403, "kb_not_in_scope"),
        ]
        for token, bank, status, reason in cases:
            with self.subTest(reason=reason):
                self.authorize(token, bank)
                row = lines(self.audit)[-1]
                self.assertEqual((row["status"], row["reason"]), (status, reason))

    def test_a_revoked_token_is_named_in_the_record(self):
        registry(self.registry, revoked=True)
        self.authorize(TOKEN, "bank-a")
        row = lines(self.audit)[-1]
        self.assertEqual((row["status"], row["reason"], row["token_id"]), (401, "revoked", "tok-audit"))

    def test_nothing_is_written_unless_a_path_is_configured(self):
        self.assertIsNone(self.authorize(TOKEN, "bank-a", audit=False))
        self.assertFalse(self.audit.exists())

    def test_disabled_auth_writes_nothing(self):
        with patch.dict(os.environ, {"RAG_AUTH_ENABLED": "false", kb_auth.ENV_AUDIT_PATH: str(self.audit)}, clear=False):
            self.assertIsNone(kb_auth.authorize({}, "bank-a", endpoint="query"))
        self.assertEqual(lines(self.audit), [])

    def test_the_file_is_owner_only_and_created_with_its_directory(self):
        self.authorize(TOKEN, "bank-a")
        self.assertTrue(self.audit.exists())
        self.assertEqual(oct(self.audit.stat().st_mode & 0o777), "0o600")

    def test_a_broken_audit_path_never_breaks_the_request(self):
        blocked = self.root / "afile"
        blocked.write_text("not a directory", encoding="utf-8")
        result = self.authorize(TOKEN, "bank-a", **{kb_auth.ENV_AUDIT_PATH: str(blocked / "nope.jsonl")})
        self.assertIsNone(result, "审计写不进去时，请求仍应正常放行")


class NeverRecordsContentTests(unittest.TestCase):
    """字段白名单 + 真实请求穿透：审计里不能出现查询词、正文或文档编号。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "tokens.json"
        self.audit = self.root / "access.jsonl"
        registry(self.registry)
        sources = self.root / "sources" / "bank-a"
        sources.mkdir(parents=True)
        (sources / "doc.txt").write_text(SECRET_TEXT, encoding="utf-8")
        index = self.root / "index.json"
        index.write_text(json.dumps({DOC_ID: "bank-a/doc.txt"}), encoding="utf-8")
        rag_server.Handler.pipeline = RAGPipeline(
            LexicalRetriever({DOC_ID: SECRET_TEXT}),
            DocResolver(roots=[self.root / "sources"], index_path=index),
            type("LLM", (), {"model": "x", "generate": lambda self, q, c: ""})(),
        )
        env = patch.dict(os.environ, {
            "RAG_AUTH_ENABLED": "true", "RAG_AUTH_REGISTRY": str(self.registry),
            "RAG_BANK": "bank-a", kb_auth.ENV_AUDIT_PATH: str(self.audit),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.httpd = rag_server.ThreadingHTTPServer(("127.0.0.1", 0), rag_server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def call(self, path, payload, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-KB-Token"] = token
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                         method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_a_real_read_is_audited_without_the_document_id_or_text(self):
        status, _ = self.call("/read", {"doc_id": DOC_ID, "length": 10})
        self.assertEqual(status, 200)
        raw = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(DOC_ID, raw, "审计不该记文档编号")
        self.assertNotIn(SECRET_TEXT, raw, "审计不该记正文")
        row = lines(self.audit)[-1]
        self.assertEqual((row["endpoint"], row["bank"], row["status"]), ("read", "bank-a", 200))

    def test_a_refused_read_is_audited_too(self):
        status, _ = self.call("/read", {"doc_id": DOC_ID}, token=None)
        self.assertEqual(status, 401)
        row = lines(self.audit)[-1]
        self.assertEqual((row["endpoint"], row["status"], row["token_id"]), ("read", 401, ""))

    def test_the_query_text_never_reaches_the_audit(self):
        self.call("/answer", {"query": SECRET_QUERY, "top_k": 1})
        raw = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_QUERY, raw)
        self.assertEqual(lines(self.audit)[-1]["endpoint"], "answer")

    def test_a_line_may_only_carry_the_declared_fields(self):
        """白名单判据：以后想'顺手加个 query 方便排查'，这里会红。"""
        self.call("/answer", {"query": SECRET_QUERY, "top_k": 1})
        self.call("/read", {"doc_id": DOC_ID})
        for row in lines(self.audit):
            self.assertEqual(set(row), set(kb_auth.AUDIT_FIELDS), "审计字段超出白名单")

    def test_the_token_itself_is_never_written(self):
        self.call("/read", {"doc_id": DOC_ID})
        raw = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(sha256(TOKEN.encode()).hexdigest(), raw, "连摘要也不记")


class RotationTests(unittest.TestCase):
    def write(self, audit: Path, count: int = 1) -> None:
        for index in range(count):
            kb_auth.audit_access(endpoint="query", bank=f"bank-{index}", status=200,
                                 reason="authorized", path=str(audit))

    def test_the_file_rolls_over_once_instead_of_growing_without_end(self):
        with TemporaryDirectory() as tmp:
            audit = Path(tmp) / "access.jsonl"
            previous = audit.with_name(audit.name + ".1")
            with patch.dict(os.environ, {kb_auth.ENV_AUDIT_MAX_BYTES: "400"}, clear=False):
                self.write(audit, 2)
                self.assertFalse(previous.exists(), "没到上限就不该轮转")
                grew = audit.stat().st_size
                self.write(audit, 10)
                self.assertTrue(previous.exists(), "超过上限应留下一代旧文件")
                self.assertLessEqual(audit.stat().st_size, 400, "新文件从头开始写")
                self.assertGreater(grew, 0)
            self.write(audit, 1)
            self.assertTrue(lines(audit), "轮转后照常可写")

    def test_only_one_previous_generation_is_kept(self):
        with TemporaryDirectory() as tmp:
            audit = Path(tmp) / "access.jsonl"
            with patch.dict(os.environ, {kb_auth.ENV_AUDIT_MAX_BYTES: "300"}, clear=False):
                self.write(audit, 30)
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ["access.jsonl", "access.jsonl.1"], "只留一代，不无限堆积")

    def test_the_audit_directory_is_owner_only(self):
        with TemporaryDirectory() as tmp:
            audit = Path(tmp) / "fresh" / "access.jsonl"
            self.write(audit, 1)
            self.assertEqual(oct(audit.parent.stat().st_mode & 0o777), "0o700")


if __name__ == "__main__":
    unittest.main()
