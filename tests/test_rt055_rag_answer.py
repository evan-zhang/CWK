"""RT-055 synthetic RAG answer and HTTP contract tests.

These tests create all source files in a temporary directory.  They never read,
index, or copy repository knowledge-base material.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from adapters.rag_answer import server as rag_server
from adapters.rag_answer.pipeline import LexicalRetriever, RAGError, RAGPipeline
from adapters.rag_answer.resolver import DocResolver


class FakeLLM:
    model = "synthetic-test-llm"

    def generate(self, query: str, contexts: list[str]) -> str:
        self.query = query
        self.contexts = contexts
        return "答案：" + " ".join(contexts)


class FailingLLM:
    model = "synthetic-test-llm"

    def generate(self, query: str, contexts: list[str]) -> str:
        raise RAGError("synthetic model failure")


class SyntheticRAGFixture(unittest.TestCase):
    """A known tiny corpus proves answer grounding and citation identity."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "docs" / "alpha.txt").write_text(
            "synthetic-fact-alpha-42 owns the synthetic-blue-queue.",
            encoding="utf-8",
        )
        (root / "docs" / "beta.txt").write_text(
            "synthetic-fact-beta-07 owns the synthetic-green-queue.",
            encoding="utf-8",
        )
        index = root / "index.json"
        index.write_text(
            json.dumps(
                {
                    "synthetic-doc-alpha": "docs/alpha.txt",
                    "synthetic-doc-beta": "docs/beta.txt",
                }
            ),
            encoding="utf-8",
        )
        resolver = DocResolver(roots=[root], index_path=index)
        corpus = {
            "synthetic-doc-alpha": "synthetic-fact-alpha-42 owns the synthetic-blue-queue.",
            "synthetic-doc-beta": "synthetic-fact-beta-07 owns the synthetic-green-queue.",
        }
        self.llm = FakeLLM()
        self.pipeline = RAGPipeline(LexicalRetriever(corpus), resolver, self.llm)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_known_question_returns_expected_fact_and_doc_id(self) -> None:
        result = self.pipeline.answer("Which owner handles alpha-42")

        self.assertIn("synthetic-fact-alpha-42", result["answer"])
        self.assertEqual(
            [citation["doc_id"] for citation in result["citations"]],
            ["synthetic-doc-alpha"],
        )
        self.assertNotIn("synthetic-doc-beta", [c["doc_id"] for c in result["citations"]])
        self.assertEqual(self.llm.contexts, [
            "synthetic-fact-alpha-42 owns the synthetic-blue-queue."
        ])

    def test_top_k_lower_and_upper_bound_are_honored(self) -> None:
        retriever = LexicalRetriever({
            "synthetic-doc-1": "shared synthetic token",
            "synthetic-doc-2": "shared synthetic token",
            "synthetic-doc-3": "shared synthetic token",
        })

        self.assertEqual(len(retriever.search("shared synthetic", top_k=1)), 1)
        self.assertEqual(len(retriever.search("shared synthetic", top_k=100)), 3)

    def test_empty_query_is_a_client_error(self) -> None:
        with self.assertRaises(RAGError) as raised:
            self.pipeline.answer("   ")
        self.assertEqual(raised.exception.status, 400)


class AnswerHTTPContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        source = root / "answer.txt"
        source.write_text("synthetic HTTP answer fact 99.", encoding="utf-8")
        second_source = root / "answer-2.txt"
        second_source.write_text("synthetic HTTP answer fact 88.", encoding="utf-8")
        index = root / "index.json"
        index.write_text(json.dumps({
            "synthetic-http-doc": source.name,
            "synthetic-http-doc-2": second_source.name,
        }), encoding="utf-8")
        pipeline = RAGPipeline(
            LexicalRetriever({
                "synthetic-http-doc": "synthetic HTTP answer fact 99.",
                "synthetic-http-doc-2": "synthetic HTTP answer fact 88.",
            }),
            DocResolver(roots=[root], index_path=index),
            FakeLLM(),
        )
        rag_server.Handler.pipeline = pipeline
        self.httpd = rag_server.ThreadingHTTPServer(("127.0.0.1", 0), rag_server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, payload: object = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_answer_success_has_contract_and_citation(self) -> None:
        status, payload = self.request("POST", "/answer", {"query": "synthetic HTTP fact"})

        self.assertEqual(status, 200)
        self.assertIn("synthetic HTTP answer fact 99", payload["answer"])
        self.assertEqual(payload["citations"][0]["doc_id"], "synthetic-http-doc")
        self.assertEqual(payload["model"], "synthetic-test-llm")
        self.assertIsInstance(payload["took_ms"], int)

    def test_top_k_boundaries_are_honored_and_invalid_values_are_400(self) -> None:
        status, payload = self.request(
            "POST", "/answer", {"query": "synthetic HTTP fact", "top_k": 1}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["citations"]), 1)

        for invalid in (0, 101, True, "1"):
            status, payload = self.request(
                "POST", "/answer", {"query": "synthetic HTTP fact", "top_k": invalid}
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload, {"error": "top_k is outside the supported range"})

    def test_malformed_or_empty_answer_request_is_400(self) -> None:
        status, payload = self.request("POST", "/answer", {"query": ""})
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "query is required"})

        request = urllib.request.Request(
            self.base + "/answer",
            data=b"not-json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_unknown_answer_route_is_404(self) -> None:
        status, payload = self.request("POST", "/unknown", {"query": "synthetic"})
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "not found"})

    def test_no_retrieval_result_is_503(self) -> None:
        rag_server.Handler.pipeline = RAGPipeline(
            LexicalRetriever({}),
            self.httpd.RequestHandlerClass.pipeline.resolver,
            FakeLLM(),
        )
        status, payload = self.request("POST", "/answer", {"query": "missing synthetic fact"})
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": "no retrieval result"})

    def test_llm_failure_is_503(self) -> None:
        current = rag_server.Handler.pipeline
        rag_server.Handler.pipeline = RAGPipeline(
            current.retriever, current.resolver, FailingLLM()
        )
        status, payload = self.request("POST", "/answer", {"query": "synthetic HTTP fact"})
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": "synthetic model failure"})


if __name__ == "__main__":
    unittest.main()
