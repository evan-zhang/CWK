from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..kb_auth import TOKEN_HEADER, authorize, header_value

from .pipeline import (
    LexicalRetriever,
    OllamaLLM,
    RAGError,
    RAGPipeline,
    RetrievalHTTPRetriever,
    validate_top_k,
)
from .resolver import DocResolver

MAX_BODY_BYTES = 100_000
DEFAULT_TOP_K = 5


def _float_env(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def build_pipeline() -> RAGPipeline:
    retrieval_url = os.getenv("RAG_RETRIEVAL_URL", "")
    if retrieval_url:
        retriever = RetrievalHTTPRetriever(
            retrieval_url,
            os.getenv("RAG_BANK", "cwork-3m"),
            _float_env("RAG_RETRIEVAL_TIMEOUT_SECONDS", "10"),
        )
    else:
        # No source text is bundled in the image. Without the retrieval API,
        # requests fail closed instead of silently answering from stale data.
        retriever = LexicalRetriever({})
    return RAGPipeline(retriever, DocResolver(), OllamaLLM())


class Handler(BaseHTTPRequestHandler):
    pipeline: RAGPipeline | None = None

    def _send(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - stdlib HTTP handler contract
        if self.path in ("/healthz", "/readyz"):
            ready = self.pipeline is not None and bool(getattr(self.pipeline.llm, "model", ""))
            self._send(200 if self.path == "/healthz" or ready else 503, {"status": "ok" if ready else "unready"})
            return
        if urllib.parse.urlsplit(self.path).path == "/read":
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            doc_id = params.get("doc_id", [""])[0]
            bank = params.get("bank", [os.getenv("RAG_BANK", "cwork-3m")])[0]
            refusal = authorize(self.headers, bank)
            if refusal:
                self._send(*refusal)
                return
            try:
                if self.pipeline is None or not doc_id:
                    raise ValueError
                self._send(200, {"doc_id": doc_id, "text": self.pipeline.resolver.resolve(doc_id)})
            except KeyError:
                self._send(404, {"error": "document not found"})
            except Exception:
                self._send(503, {"error": "source document unavailable"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib HTTP handler contract
        if self.path != "/answer":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "invalid JSON request"})
            return
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON request"})
            return
        if not isinstance(data, dict) or set(data) - {"query", "top_k", "bank"}:
            self._send(400, {"error": "invalid JSON request"})
            return
        query = data.get("query")
        top_k = data.get("top_k", DEFAULT_TOP_K)
        bank = data.get("bank")
        if bank is not None and (not isinstance(bank, str) or not bank.strip()):
            self._send(400, {"error": "invalid JSON request"})
            return
        bank_for_auth = bank or os.getenv("RAG_BANK", "cwork-3m")
        refusal = authorize(self.headers, bank_for_auth)
        if refusal:
            self._send(*refusal)
            return
        try:
            validate_top_k(top_k)
            if self.pipeline is None:
                raise RAGError("RAG pipeline is not configured")
            self._send(200, self.pipeline.answer(query, top_k=top_k, bank=bank, token=header_value(self.headers, TOKEN_HEADER)))
        except RAGError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid JSON request"})
        except Exception:
            # Never turn a backend/programming failure into a misleading 400.
            self._send(503, {"error": "RAG service unavailable"})

    def log_message(self, *args):
        # Query paths and arguments may contain protected source text.
        pass


def main():
    Handler.pipeline = build_pipeline()
    ThreadingHTTPServer(
        (os.getenv("RAG_HOST", "127.0.0.1"), int(os.getenv("RAG_PORT", "8790"))),
        Handler,
    ).serve_forever()


if __name__ == "__main__":
    main()
