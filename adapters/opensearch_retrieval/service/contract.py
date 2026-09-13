"""Pure validation and serialization for the HTTP contract."""

from __future__ import annotations

import math
from typing import Any, Collection


MAX_QUERY_CHARS = 4096
MAX_TOP_K = 100


class RequestError(ValueError):
    """The client request does not satisfy the public contract."""


def parse_query_request(
    payload: Any, *, registered_banks: Collection[str], default_top_k: int
) -> tuple[str, str, int]:
    if not isinstance(payload, dict):
        raise RequestError("request must be an object")
    if set(payload) - {"bank", "query", "top_k"}:
        raise RequestError("request contains unsupported fields")
    bank = payload.get("bank")
    query = payload.get("query")
    if not isinstance(bank, str) or not bank.strip() or not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise RequestError("bank and query are required text")
    if bank not in registered_banks:
        # The application translates this sentinel to the public 404.
        raise LookupError("bank is not registered")
    top_k = payload.get("top_k", default_top_k)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise RequestError("top_k is outside the supported range")
    return bank, query, top_k


def query_response(hits: Any, took_ms: float) -> dict[str, Any]:
    if not isinstance(hits, (list, tuple)):
        raise TypeError("hits must be a sequence")
    if isinstance(took_ms, bool) or not isinstance(took_ms, (int, float)) or not math.isfinite(took_ms) or took_ms < 0:
        raise TypeError("took_ms must be a finite non-negative number")
    rendered = []
    for hit in hits:
        doc_id = getattr(hit, "doc_id", None)
        score = getattr(hit, "score", None)
        channel = getattr(hit, "channel", None)
        if not isinstance(doc_id, str) or not doc_id or not isinstance(channel, str) or not channel:
            raise TypeError("hit identity is invalid")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise TypeError("hit score is invalid")
        rendered.append({"doc_id": doc_id, "score": float(score), "channel": channel})
    return {"hits": rendered, "no_answer": not rendered, "took_ms": round(float(took_ms), 3)}


def error_response(code: str, message: str) -> dict[str, Any]:
    # Messages are fixed service vocabulary; backend response text never crosses the API.
    return {"error": {"code": code, "message": message}}
