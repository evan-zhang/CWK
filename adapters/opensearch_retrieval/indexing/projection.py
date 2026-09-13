"""Deterministic source-to-parent/child projection.

The input is an explicit JSON-like source projection, not a filesystem scan.
The projector never imports experiment code and never emits source text in
build statistics or service logs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..query.exact import extract_exact_fields


MAX_SOURCE_BYTES = 8 * 1024 * 1024
PARENT_MAX_TOKENS = 2000
CHILD_MAX_TOKENS = 700
CHILD_OVERLAP_TOKENS = 40
GENERATION_SCHEMA = "cwk-opensearch-retrieval-v1"
_ID_RE = re.compile(r"^[^\x00\r\n\t ]{1,256}$")
_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9][A-Za-z0-9_.-]*|[^\s]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_ALLOWED_LOCATOR = {
    "page_start", "page_end", "sheet_name", "row_start", "row_end",
    "paragraph_start", "paragraph_end", "section_path",
}


class ProjectionError(ValueError):
    """The source projection cannot be safely indexed."""


@dataclass(frozen=True)
class SourceDocument:
    bank: str
    doc_id: str
    title: str
    filename: str
    text: str
    source_version: int = 1
    locator: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceDocument":
        if not isinstance(value, Mapping):
            raise ProjectionError("source document must be an object")
        bank = value.get("bank", value.get("kb_id"))
        doc_id = value.get("doc_id", value.get("id"))
        title = value.get("title", "")
        filename = value.get("filename", "")
        text = value.get("text", value.get("body", value.get("content", "")))
        version = value.get("source_version", 1)
        locator = value.get("locator", {})
        if not all(isinstance(item, str) for item in (bank, doc_id, title, filename, text)):
            raise ProjectionError("source document fields have invalid types")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ProjectionError("source_version must be a positive integer")
        if not isinstance(locator, Mapping):
            raise ProjectionError("locator must be an object")
        unknown = set(locator) - _ALLOWED_LOCATOR
        if unknown:
            raise ProjectionError("locator contains unsupported fields")
        return cls(bank, doc_id, title, filename, text, version, dict(locator))

    @property
    def canonical_text(self) -> str:
        return f"{self.title}\n{self.filename}\n\n{self.text}"


def coerce_documents(values: Iterable[SourceDocument | Mapping[str, Any]]) -> tuple[SourceDocument, ...]:
    documents = tuple(value if isinstance(value, SourceDocument) else SourceDocument.from_mapping(value) for value in values)
    if not documents:
        raise ProjectionError("source projection is empty")
    seen: set[tuple[str, str]] = set()
    for document in documents:
        if not _ID_RE.fullmatch(document.bank) or not _ID_RE.fullmatch(document.doc_id):
            raise ProjectionError("source identity is invalid")
        if not document.text.strip() or len(document.canonical_text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ProjectionError("source document content is invalid")
        key = (document.bank, document.doc_id)
        if key in seen:
            raise ProjectionError("duplicate source document")
        seen.add(key)
    return documents


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _TOKEN_RE.finditer(text)]


def _chunks(text: str, limit: int, overlap: int = 0) -> list[str]:
    spans = _token_spans(text)
    if not spans:
        return []
    result: list[str] = []
    start = 0
    while start < len(spans):
        end = min(len(spans), start + limit)
        result.append(text[spans[start][0]:spans[end - 1][1]])
        if end == len(spans):
            break
        start = max(start + 1, end - overlap)
    return result


def _section_paths(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _HEADING_RE.match(line)
        if match:
            result.append((offset, match.group(1)))
        offset += len(line)
    return result


def _section_for(text: str, chunk: str) -> list[str]:
    headings = _section_paths(text)
    if not headings:
        return []
    position = text.find(chunk)
    if position < 0:
        return [headings[-1][1]]
    return [title for offset, title in headings if offset <= position][-1:]


def _parent_row(document: SourceDocument, tenant_id: str, parent_id: str, ordinal: int, text: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "bank": document.bank,
        "kb_id": document.bank,
        "doc_id": document.doc_id,
        "parent_id": parent_id,
        "chunk_id": parent_id,
        "kind": "parent",
        "source_version": document.source_version,
        "generation_schema": GENERATION_SCHEMA,
        "title": document.title,
        "section_path": [],
        "body": text,
        "identifiers": [],
        "date_values": [],
        "filenames": [],
        "entity_names": [],
        "acronyms": [],
        "locator": dict(document.locator),
        "join": "document",
    }


def _child_row(document: SourceDocument, tenant_id: str, parent_id: str, ordinal: int, body: str) -> dict[str, Any]:
    exact = extract_exact_fields(f"{document.title}\n{document.filename}\n{body}")
    child_id = _stable_id("chunk", document.bank, document.doc_id, parent_id, str(ordinal))
    return {
        "tenant_id": tenant_id,
        "bank": document.bank,
        "kb_id": document.bank,
        "doc_id": document.doc_id,
        "parent_id": parent_id,
        "chunk_id": child_id,
        "kind": "chunk",
        "source_version": document.source_version,
        "generation_schema": GENERATION_SCHEMA,
        "title": document.title,
        "section_path": _section_for(document.canonical_text, body),
        "body": body,
        "identifiers": exact["identifiers"],
        "date_values": exact["date_values"],
        "filenames": exact["filenames"],
        "entity_names": [],
        "acronyms": [],
        "locator": dict(document.locator),
        "join": {"name": "chunk", "parent": parent_id},
    }


@dataclass(frozen=True)
class ProjectedIndex:
    parents: tuple[dict[str, Any], ...]
    children: tuple[dict[str, Any], ...]

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return self.parents + self.children

    @property
    def document_count(self) -> int:
        return len({(row["bank"], row["doc_id"]) for row in self.parents})


def project_documents(
    values: Sequence[SourceDocument | Mapping[str, Any]], *, tenant_id: str
) -> ProjectedIndex:
    if not isinstance(tenant_id, str) or not _ID_RE.fullmatch(tenant_id):
        raise ProjectionError("tenant identity is invalid")
    documents = coerce_documents(values)
    parents: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    for document in documents:
        parent_parts = _chunks(document.canonical_text, PARENT_MAX_TOKENS)
        for parent_ordinal, parent_text in enumerate(parent_parts):
            parent_id = _stable_id("parent", document.bank, document.doc_id, str(parent_ordinal))
            parents.append(_parent_row(document, tenant_id, parent_id, parent_ordinal, parent_text))
            for child_ordinal, child_text in enumerate(_chunks(parent_text, CHILD_MAX_TOKENS, CHILD_OVERLAP_TOKENS)):
                children.append(_child_row(document, tenant_id, parent_id, child_ordinal, child_text))
    if not children:
        raise ProjectionError("source projection produced no searchable chunks")
    return ProjectedIndex(tuple(parents), tuple(children))
