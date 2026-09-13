"""Idempotent bulk index builder for the production projection."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..client import BackendError, BackendProtocolError
from .projection import ProjectedIndex, SourceDocument, coerce_documents, project_documents
from .template import build_index_template


_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,180}$")
BULK_SIZE = 100


@dataclass(frozen=True)
class BuildStats:
    document_count: int
    parent_count: int
    child_count: int
    batch_count: int
    index_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_count": self.document_count,
            "parent_count": self.parent_count,
            "child_count": self.child_count,
            "batch_count": self.batch_count,
            "index_name": self.index_name,
        }


class IndexBuilder:
    def __init__(self, client: Any, *, index_name: str, tenant_id: str, banks: Sequence[str]) -> None:
        if not _INDEX_RE.fullmatch(index_name):
            raise ValueError("invalid index name")
        self.client = client
        self.index_name = index_name
        self.tenant_id = tenant_id
        self.banks = frozenset(banks)

    def _path(self, suffix: str = "") -> str:
        return "/" + urllib.parse.quote(self.index_name, safe="") + suffix

    def ensure_index(self) -> None:
        try:
            exists = self.client.index_exists(self.index_name)
        except BackendError:
            raise
        if exists:
            return
        response = self.client.request("PUT", self._path(), build_index_template())
        if response.get("acknowledged") is not True:
            raise BackendProtocolError("index creation was not acknowledged")

    def _bulk(self, rows: Sequence[dict[str, Any]]) -> None:
        lines: list[str] = []
        for row in rows:
            row = dict(row)
            row.pop("_parent_ordinal", None)
            metadata: dict[str, Any] = {"_index": self.index_name, "_id": row["chunk_id"]}
            if row["kind"] == "chunk":
                metadata["routing"] = row["parent_id"]
            lines.append(json.dumps({"index": metadata}, separators=(",", ":")))
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        response = self.client.request(
            "POST", self._path("/_bulk"), ("\n".join(lines) + "\n").encode("utf-8")
        )
        items = response.get("items")
        if response.get("errors") is not False or not isinstance(items, list) or len(items) != len(rows):
            raise BackendProtocolError("bulk index response was incomplete")
        for item in items:
            operation = item.get("index") if isinstance(item, dict) else None
            if not isinstance(operation, dict) or not isinstance(operation.get("status"), int) or not 200 <= operation["status"] < 300:
                raise BackendProtocolError("bulk index item failed")

    def build(self, values: Iterable[SourceDocument | Mapping[str, Any]]) -> BuildStats:
        documents = coerce_documents(values)
        if any(document.bank not in self.banks for document in documents):
            raise ValueError("source document belongs to an unregistered bank")
        projected: ProjectedIndex = project_documents(documents, tenant_id=self.tenant_id)
        self.ensure_index()
        rows = projected.rows
        batch_count = 0
        for offset in range(0, len(rows), BULK_SIZE):
            self._bulk(rows[offset:offset + BULK_SIZE])
            batch_count += 1
        refreshed = self.client.request("POST", self._path("/_refresh"))
        shards = refreshed.get("_shards", {})
        if not isinstance(shards, dict) or shards.get("failed") != 0:
            raise BackendProtocolError("index refresh failed")
        return BuildStats(
            document_count=projected.document_count,
            parent_count=len(projected.parents),
            child_count=len(projected.children),
            batch_count=batch_count,
            index_name=self.index_name,
        )
