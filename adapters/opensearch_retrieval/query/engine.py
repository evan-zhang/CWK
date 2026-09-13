"""Fail-closed dual-channel OpenSearch query engine."""

from __future__ import annotations

import math
import urllib.parse
from typing import Any, Sequence

from ..client import BackendError, BackendProtocolError
from .exact import ExactResolution, resolve_exact
from .merge import merge_channel_hits
from .types import RetrievalHit


MAX_QUERY_CHARS = 4096


class BackendQueryError(BackendError):
    """The search response is unavailable or violates the query contract."""


class UnknownBank(KeyError):
    """The requested bank is not registered for this service."""


class RetrievalQuery:
    """OpenSearch query engine with exact-first routing and lexical BM25."""

    def __init__(self, client: Any, *, index_name: str, tenant_id: str, banks: Sequence[str]) -> None:
        self.client = client
        self.index_name = index_name
        self.tenant_id = tenant_id
        self.banks = frozenset(banks)

    def _path(self, suffix: str) -> str:
        return "/" + urllib.parse.quote(self.index_name, safe="") + suffix

    def _scope(self, bank: str) -> list[dict[str, Any]]:
        return [{"term": {"tenant_id": self.tenant_id}}, {"term": {"bank": bank}}, {"term": {"kind": "chunk"}}]

    def exact_body(self, bank: str, resolution: ExactResolution, top_k: int) -> dict[str, Any]:
        return {
            "size": top_k,
            "track_total_hits": False,
            "collapse": {"field": "doc_id"},
            "_source": ["tenant_id", "bank", "doc_id", "parent_id", "chunk_id", "kind"],
            "query": {"bool": {"filter": [*self._scope(bank), *resolution.as_filters()], "must": []}},
            "sort": [{"_score": "desc"}, {"doc_id": "asc"}, {"chunk_id": "asc"}],
        }

    def lexical_body(self, bank: str, query: str, top_k: int) -> dict[str, Any]:
        return {
            "size": top_k,
            "track_total_hits": False,
            "collapse": {"field": "doc_id"},
            "_source": ["tenant_id", "bank", "doc_id", "parent_id", "chunk_id", "kind"],
            "query": {
                "bool": {
                    "filter": self._scope(bank),
                    "must": [{
                        "multi_match": {
                            "query": query,
                            "fields": ["title^3", "section_path^2", "body"],
                            "type": "best_fields",
                        }
                    }],
                }
            },
            "sort": [{"_score": "desc"}, {"doc_id": "asc"}, {"chunk_id": "asc"}],
        }

    @staticmethod
    def _response_hits(response: Any, *, bank: str, tenant_id: str, channel: str, top_k: int) -> list[RetrievalHit]:
        if not isinstance(response, dict) or response.get("timed_out") is not False:
            raise BackendQueryError("incomplete search response")
        shards = response.get("_shards")
        if not isinstance(shards, dict) or shards.get("failed") != 0:
            raise BackendQueryError("search shard failure")
        payload = response.get("hits")
        raw_hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(raw_hits, list) or len(raw_hits) > top_k:
            raise BackendQueryError("invalid search hits")
        result: list[RetrievalHit] = []
        for raw in raw_hits:
            if not isinstance(raw, dict):
                raise BackendQueryError("invalid search hit")
            source = raw.get("_source")
            if not isinstance(source, dict) or source.get("tenant_id") != tenant_id or source.get("bank") != bank or source.get("kind") != "chunk":
                raise BackendQueryError("search scope mismatch")
            doc_id = source.get("doc_id")
            parent_id = source.get("parent_id")
            chunk_id = source.get("chunk_id")
            if not all(isinstance(value, str) and value for value in (doc_id, parent_id, chunk_id)):
                raise BackendQueryError("search identity is invalid")
            score = raw.get("_score", 0.0)
            if score is None:
                score = 0.0
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise BackendQueryError("search score is invalid")
            result.append(RetrievalHit(doc_id, float(score), channel, parent_id, chunk_id))
        return result

    def _search(self, body: dict[str, Any], *, bank: str, channel: str, top_k: int) -> list[RetrievalHit]:
        try:
            response = self.client.request("POST", self._path("/_search"), body)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendQueryError("search backend failed") from exc
        return self._response_hits(response, bank=bank, tenant_id=self.tenant_id, channel=channel, top_k=top_k)

    def _expand_parents(self, hits: Sequence[RetrievalHit], *, bank: str) -> list[RetrievalHit]:
        if not hits:
            return []
        response = self.client.request(
            "POST",
            self._path("/_mget"),
            {"ids": [hit.parent_id for hit in hits], "_source": ["tenant_id", "bank", "doc_id", "parent_id", "kind"]},
        )
        docs = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(docs, list) or len(docs) != len(hits):
            raise BackendQueryError("parent expansion is incomplete")
        for hit, parent in zip(hits, docs):
            if not isinstance(parent, dict) or parent.get("found") is not True or parent.get("_id") != hit.parent_id:
                raise BackendQueryError("parent expansion identity is invalid")
            source = parent.get("_source")
            if not isinstance(source, dict) or source.get("tenant_id") != self.tenant_id or source.get("bank") != bank:
                raise BackendQueryError("parent expansion scope mismatch")
            if source.get("kind") != "parent" or source.get("parent_id") != hit.parent_id or source.get("doc_id") != hit.doc_id:
                raise BackendQueryError("parent expansion document mismatch")
        return list(hits)

    def query(self, bank: str, query: str, *, top_k: int = 10) -> list[RetrievalHit]:
        if bank not in self.banks:
            raise UnknownBank(bank)
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be non-empty text")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k is outside the supported range")
        resolution = resolve_exact(query)
        if resolution.has_terms:
            exact_hits = self._search(self.exact_body(bank, resolution, top_k), bank=bank, channel="exact", top_k=top_k)
            # Exact-bearing requests never fall back to lexical on zero exact
            # matches. If exact succeeds, lexical contributes to the merge.
            if not exact_hits:
                return []
            lexical_hits = self._search(self.lexical_body(bank, query, top_k), bank=bank, channel="lexical", top_k=top_k)
        else:
            exact_hits = []
            lexical_hits = self._search(self.lexical_body(bank, query, top_k), bank=bank, channel="lexical", top_k=top_k)
        merged = merge_channel_hits(exact_hits, lexical_hits, top_k=top_k)
        try:
            return self._expand_parents(merged, bank=bank)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendQueryError("parent expansion failed") from exc

    def ready(self) -> bool:
        checker = getattr(self.client, "is_ready", None)
        if not callable(checker):
            return False
        return bool(checker(self.index_name))


def is_no_answer(hits: Sequence[RetrievalHit]) -> bool:
    return not hits
