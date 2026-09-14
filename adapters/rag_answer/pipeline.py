from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

from .resolver import DocResolver, ResolveError

MAX_TOP_K = 100
MAX_RETRIEVAL_RESPONSE_BYTES = 2_000_000


class RAGError(Exception):
    def __init__(self, message: str, status: int = 503):
        self.status = status
        super().__init__(message)


def validate_top_k(top_k: object) -> int:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise RAGError("top_k is outside the supported range", 400)
    return top_k


class LexicalRetriever:
    """Small deterministic retriever used by synthetic tests and local fixtures."""

    def __init__(self, index: dict[str, str]):
        if not isinstance(index, dict) or any(
            not isinstance(doc_id, str) or not isinstance(text, str)
            for doc_id, text in index.items()
        ):
            raise ValueError("retrieval index must map text doc_ids to strings")
        self.index = dict(index)

    def search(self, query: str, top_k: int = 5, bank: str | None = None, token: str = "") -> list[dict[str, object]]:
        validate_top_k(top_k)
        words = set(query.lower().split())
        out = []
        for doc_id, text in self.index.items():
            score = sum(word in text.lower() for word in words)
            if score:
                out.append({"doc_id": doc_id, "score": float(score)})
        return sorted(out, key=lambda item: (-item["score"], item["doc_id"]))[:top_k]


class RetrievalHTTPRetriever:
    """Client for the loopback retrieval API; response bodies never enter errors."""

    def __init__(self, url: str, bank: str, timeout: float = 10.0):
        if not isinstance(url, str) or not url:
            raise ValueError("retrieval URL is required")
        if not isinstance(bank, str) or not bank.strip():
            raise ValueError("retrieval bank is required")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("retrieval timeout must be positive")
        self.url = url
        self.bank = bank
        self.timeout = timeout
        # The loopback service token is deliberately separate from the caller's
        # token: it is injected only when the deployment explicitly configures it.
        self.auth_token = os.getenv("RAG_AUTH_TOKEN", "")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def search(self, query: str, top_k: int = 5, bank: str | None = None, token: str = "") -> list[dict[str, object]]:
        validate_top_k(top_k)
        payload = {"bank": bank or self.bank, "query": query, "top_k": top_k}
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.auth_token:
            headers["X-KB-Token"] = self.auth_token
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RETRIEVAL_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            raise RAGError("retrieval backend unavailable", 503) from None
        if len(raw) > MAX_RETRIEVAL_RESPONSE_BYTES:
            raise RAGError("retrieval backend unavailable", 503)
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RAGError("retrieval backend unavailable", 503) from None
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list) or len(hits) > top_k:
            raise RAGError("retrieval backend unavailable", 503)
        result = []
        for hit in hits:
            if not isinstance(hit, dict):
                raise RAGError("retrieval backend unavailable", 503)
            doc_id = hit.get("doc_id")
            score = hit.get("score")
            if (
                not isinstance(doc_id, str)
                or not doc_id
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise RAGError("retrieval backend unavailable", 503)
            result.append({"doc_id": doc_id, "score": float(score)})
        return result


class OllamaLLM:
    def __init__(self):
        self.base = os.getenv("RAG_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
        self.model = os.getenv("RAG_LLM_MODEL", "")
        self.timeout = float(os.getenv("RAG_LLM_TIMEOUT_SECONDS", "10"))
        self.api_key = os.getenv("RAG_LLM_API_KEY", "")

    def generate(self, query: str, contexts: list[str]) -> str:
        if not self.model:
            raise RAGError("LLM model is not configured")
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": query + "\n\nContext:\n" + "\n".join(contexts),
            }],
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(
            self.base,
            json.dumps(body).encode(),
            headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read(2_000_000))
            answer = data.get("choices", [{}])[0].get("message", {}).get("content") or data.get("response")
            if not isinstance(answer, str) or not answer:
                raise ValueError()
            return answer
        except Exception as exc:
            raise RAGError("LLM failed or timed out") from exc


class RAGPipeline:
    def __init__(self, retriever, resolver: DocResolver, llm):
        self.retriever, self.resolver, self.llm = retriever, resolver, llm

    def answer(self, query: str, *, top_k: int = 5, bank: str | None = None, token: str = "") -> dict[str, object]:
        if not isinstance(query, str) or not query.strip():
            raise RAGError("query is required", 400)
        top_k = validate_top_k(top_k)
        started = time.monotonic()
        try:
            search_kwargs = {"top_k": top_k, "bank": bank}
            if token:
                search_kwargs["token"] = token
            hits = self.retriever.search(query, **search_kwargs)
        except RAGError:
            raise
        except Exception as exc:
            raise RAGError("retrieval backend unavailable") from exc
        if not hits:
            return {
                "answer": "知识库中未找到相关内容。",
                "citations": [],
                "model": getattr(self.llm, "model", "local"),
                "took_ms": int((time.monotonic() - started) * 1000),
            }
        texts = []
        for hit in hits:
            try:
                texts.append(self.resolver.resolve(hit["doc_id"]))
            except KeyError as exc:
                raise RAGError("source document missing", 503) from exc
            except ResolveError as exc:
                raise RAGError("source document unavailable", 503) from exc
        answer = self.llm.generate(query, texts)
        return {
            "answer": answer,
            "citations": [
                {"doc_id": hit["doc_id"], "score": hit["score"]} for hit in hits
            ],
            "model": getattr(self.llm, "model", "local"),
            "took_ms": int((time.monotonic() - started) * 1000),
        }
