"""RT-062: 读原文接口按文档真实归属的库鉴权。

线上曾出现两个洞，这里各用一组判据锁住：

1. **POST /read 完全不鉴权**——不带令牌、带假令牌都能读原文。
2. **GET /read 校验的是调用方自报的库**——而 doc_id 在索引里是全局的，
   报一个自己有权的库名，就能读走别的库的文档。

全部夹具在临时目录里合成，不碰真实语料、不连 OPS、不含任何真实凭据。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.rag_answer import server as rag_server
from adapters.rag_answer.pipeline import LexicalRetriever, RAGPipeline
from adapters.rag_answer.resolver import DocResolver

# 令牌 → 授权范围。默认库是 bank-a。
TOKENS = {
    "synthetic-token-a": ["bank-a"],
    "synthetic-token-b": ["bank-b"],
    "synthetic-token-ab": ["bank-a", "bank-b"],
}
DOC_A = "doc-in-bank-a"
DOC_B = "doc-in-bank-b"
DOC_FLAT = "doc-with-flat-path"
TEXT_B = "synthetic bank-b secret sentence."


class NullLLM:
    model = "synthetic-test-llm"

    def generate(self, query, contexts):
        return ""


class ReadAuthzHarness(unittest.TestCase):
    auth_enabled = "true"

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        sources = root / "sources"
        for rel, text in (("bank-a/a.txt", "synthetic bank-a sentence."),
                          ("bank-b/b.txt", TEXT_B),
                          ("flat.txt", "synthetic flat sentence.")):
            (sources / rel).parent.mkdir(parents=True, exist_ok=True)
            (sources / rel).write_text(text, encoding="utf-8")
        index = root / "index.json"
        index.write_text(json.dumps({
            DOC_A: "bank-a/a.txt", DOC_B: "bank-b/b.txt", DOC_FLAT: "flat.txt",
        }), encoding="utf-8")
        registry = root / "tokens.json"
        registry.write_text(json.dumps({
            "schema": "cwk.kb.token-registry.v1",
            "tokens": [{
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "token_id": f"tok-{token}", "kb_ids": scope,
                "expires_at": "2099-01-01T00:00:00Z", "revoked": False,
            } for token, scope in TOKENS.items()],
        }), encoding="utf-8")

        env = patch.dict(os.environ, {
            "RAG_AUTH_ENABLED": self.auth_enabled,
            "RAG_AUTH_REGISTRY": str(registry),
            "RAG_BANK": "bank-a",
        })
        env.start()
        self.addCleanup(env.stop)

        rag_server.Handler.pipeline = RAGPipeline(
            LexicalRetriever({}), DocResolver(roots=[sources], index_path=index), NullLLM(),
        )
        self.httpd = rag_server.ThreadingHTTPServer(("127.0.0.1", 0), rag_server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload=None, token: str | None = None):
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["X-KB-Token"] = token
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def post_read(self, doc_id: str, token: str | None = None):
        return self.call("POST", "/read", {"doc_id": doc_id}, token)

    def get_read(self, doc_id: str, token: str | None = None, bank: str | None = None):
        query = {"doc_id": doc_id}
        if bank is not None:
            query["bank"] = bank
        return self.call("GET", "/read?" + urllib.parse.urlencode(query), token=token)


class PostReadRequiresTokenTests(ReadAuthzHarness):
    def test_missing_or_fake_token_is_401(self):
        """这就是线上那个洞：修复前这两条都是 200。"""
        for token in (None, "synthetic-not-registered"):
            status, body = self.post_read(DOC_A, token)
            self.assertEqual(status, 401, f"token={token!r}")
            self.assertNotIn(b"sentence", body)

    def test_token_scoped_to_the_documents_bank_reads_it(self):
        status, body = self.post_read(DOC_A, "synthetic-token-a")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "synthetic bank-a sentence.")

    def test_token_for_another_bank_is_403_and_carries_no_text(self):
        status, body = self.post_read(DOC_B, "synthetic-token-a")
        self.assertEqual(status, 403)
        self.assertNotIn(TEXT_B.encode(), body)

    def test_multi_bank_token_reads_both(self):
        for doc_id in (DOC_A, DOC_B):
            self.assertEqual(self.post_read(doc_id, "synthetic-token-ab")[0], 200, doc_id)


class NoExistenceOracleTests(ReadAuthzHarness):
    def test_unauthenticated_caller_cannot_tell_known_from_unknown_ids(self):
        """没令牌的人拿到的回应完全一样，猜不出哪个编号存在。"""
        known = self.post_read(DOC_B)
        unknown = self.post_read("doc-that-does-not-exist")
        self.assertEqual(known[0], 401)
        self.assertEqual(known, unknown)
        self.assertEqual(self.get_read(DOC_B), self.get_read("doc-that-does-not-exist"))

    def test_unknown_id_with_a_valid_token_is_404(self):
        self.assertEqual(self.post_read("doc-that-does-not-exist", "synthetic-token-a")[0], 404)

    def test_unknown_id_is_404_even_when_the_default_bank_is_out_of_scope(self):
        """未知编号没有内容可泄露，所以不必给有效令牌一个误导性的 403。"""
        self.assertEqual(self.post_read("doc-that-does-not-exist", "synthetic-token-b")[0], 404)

    def test_unsafe_ids_are_refused_before_the_resolver_for_unauthenticated_callers(self):
        self.assertEqual(self.post_read("../bank-b/b.txt")[0], 401)
        self.assertEqual(self.post_read("../bank-b/b.txt", "synthetic-token-a")[0], 400)


class GetReadIgnoresClaimedBankTests(ReadAuthzHarness):
    def test_claiming_an_authorized_bank_does_not_unlock_another_banks_document(self):
        """这是 GET 那个洞：报自己有权的 bank-a，去读 bank-b 的文档。"""
        status, body = self.get_read(DOC_B, "synthetic-token-a", bank="bank-a")
        self.assertEqual(status, 403)
        self.assertNotIn(TEXT_B.encode(), body)

    def test_get_read_requires_a_token(self):
        self.assertEqual(self.get_read(DOC_A)[0], 401)

    def test_get_read_serves_the_documents_own_bank(self):
        status, body = self.get_read(DOC_B, "synthetic-token-b", bank="bank-a")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], TEXT_B)


class FlatIndexPathTests(ReadAuthzHarness):
    def test_single_segment_path_belongs_to_the_default_bank(self):
        """单库部署的索引没有库名前缀——它属于默认库，而不是「谁都能读」。"""
        self.assertEqual(self.post_read(DOC_FLAT, "synthetic-token-a")[0], 200)
        self.assertEqual(self.post_read(DOC_FLAT, "synthetic-token-b")[0], 403)
        self.assertEqual(self.post_read(DOC_FLAT)[0], 401)


class AuthDisabledStillServesReadsTests(ReadAuthzHarness):
    """本地开发与合成测试默认关闭鉴权，这条路径不能被修复顺手堵死。"""

    auth_enabled = "false"

    def test_reads_work_without_a_token(self):
        for doc_id in (DOC_A, DOC_B, DOC_FLAT):
            self.assertEqual(self.post_read(doc_id)[0], 200, doc_id)
            self.assertEqual(self.get_read(doc_id)[0], 200, doc_id)
        self.assertEqual(self.post_read("doc-that-does-not-exist")[0], 404)


class ApiReferenceTests(unittest.TestCase):
    def test_read_section_documents_both_auth_refusals(self):
        """文档只写 400/404 时，调用方会把 401/403 当成服务故障去排查。"""
        sys.path.insert(0, str(ROOT / "scripts"))
        import kb_portal

        _, _, body, _ = kb_portal.PortalApp({}).handle("GET", "/docs/api")
        api = body.decode("utf-8")
        read_section = api[api.index("/read</span>"):]
        for code in ("401", "403"):
            self.assertIn(f"<code>{code}</code>", read_section, f"读原文一节缺状态码 {code}")
        self.assertIn("这篇文档所属的库", read_section)


class BankOfTests(unittest.TestCase):
    def resolver(self, index: dict) -> DocResolver:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        return DocResolver(roots=[tmp.name], index_path=path)

    def test_first_path_segment_is_the_bank(self):
        r = self.resolver({"x": "bank-a/sub/dir/file.md", "y": "file.md", "z": 7})
        self.assertEqual(r.bank_of("x", "default"), "bank-a")
        self.assertEqual(r.bank_of("y", "default"), "default")
        self.assertIsNone(r.bank_of("z", "default"), "非字符串路径视为未索引")
        self.assertIsNone(r.bank_of("missing", "default"))
        self.assertIsNone(r.bank_of(["not", "hashable"], "default"))


if __name__ == "__main__":
    unittest.main()
