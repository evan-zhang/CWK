"""OpenSearch mapping for ICU BM25 and explicit parent/child documents."""

from __future__ import annotations

from typing import Any


INDEX_TEMPLATE_VERSION = "cwk-opensearch-retrieval-v1"


def build_index_template() -> dict[str, Any]:
    """Return a fresh strict index definition; callers may safely mutate it."""
    icu_text = {
        "type": "text",
        "analyzer": "cwk_icu",
        "similarity": "cwk_bm25",
    }
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "-1",
            "analysis": {
                "analyzer": {
                    "cwk_icu": {
                        "type": "custom",
                        "tokenizer": "icu_tokenizer",
                        "filter": ["icu_normalizer", "lowercase"],
                    }
                }
            },
            "similarity": {
                "cwk_bm25": {"type": "BM25", "k1": 1.2, "b": 0.75}
            },
        },
        "mappings": {
            "dynamic": "strict",
            "_source": {"enabled": True, "excludes": ["body"]},
            "properties": {
                "tenant_id": {"type": "keyword"},
                "bank": {"type": "keyword"},
                "kb_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "parent_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "source_version": {"type": "long"},
                "generation_schema": {"type": "keyword"},
                "title": icu_text,
                "section_path": icu_text,
                "body": icu_text,
                "identifiers": {"type": "keyword"},
                "date_values": {"type": "keyword"},
                "filenames": {"type": "keyword"},
                "entity_names": {"type": "keyword"},
                "acronyms": {"type": "keyword"},
                "locator": {
                    "type": "object",
                    "dynamic": "strict",
                    "properties": {
                        "page_start": {"type": "integer"},
                        "page_end": {"type": "integer"},
                        "sheet_name": {"type": "keyword"},
                        "row_start": {"type": "integer"},
                        "row_end": {"type": "integer"},
                        "paragraph_start": {"type": "integer"},
                        "paragraph_end": {"type": "integer"},
                        "section_path": {"type": "keyword"},
                    },
                },
                "join": {
                    "type": "join",
                    "relations": {"document": "chunk"},
                },
            },
        },
    }
