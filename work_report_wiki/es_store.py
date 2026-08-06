# -*- coding: utf-8 -*-
"""Elasticsearch 存储与检索：wiki_chunks 索引 + 权限感知混合检索。

- embedding 维度取自 config（默认 1024，cosine / HNSW）。
- 混合检索复用 LangChain 的 EnsembleRetriever（BM25 + kNN，权重 [0.3, 0.7]）；
  EnsembleRetriever 在 Python 端做 Reciprocal Rank Fusion，不依赖 ES 的
  rank.rrf 插件许可，与 touqian_rag.search_doc 的写法一致。
- G1 检索前预过滤通过 ES terms 过滤实现（metadata.report_id），缩小攻击面。
- 文档结构对齐既有约定：page_content 存于 text，ACL/展示字段统一置于 metadata 对象。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .config import settings
from .embeddings import get_embeddings, embed_query

logger = logging.getLogger(__name__)

_client = None
_embeddings: Optional[OpenAIEmbeddings] = None


def get_client():
    global _client
    if _client is None:
        from elasticsearch import Elasticsearch

        kwargs: Dict[str, Any] = {
            "hosts": [{"scheme": settings.es_scheme, "host": settings.es_host, "port": settings.es_port}],
        }
        if settings.es_user:
            kwargs["basic_auth"] = (settings.es_user, settings.es_password)
        _client = Elasticsearch(**kwargs)
    return _client


def get_embeddings_client() -> object:
    global _embeddings
    if _embeddings is None:
        _embeddings = get_embeddings()
    return _embeddings


_RRF_K = 60


def _es_hit_to_dict(hit: Dict[str, Any]) -> Dict[str, Any]:
    """把 ES 原始命中还原为与检索层契约一致的 dict。"""
    src = hit.get("_source", {}) or {}
    m = src.get("metadata", {}) or {}
    return {
        "_id": hit.get("_id"),
        "report_id": m.get("report_id"),
        "report_version_id": m.get("report_version_id"),
        "chunk_id": m.get("chunk_id"),
        "chunk_type": m.get("chunk_type"),
        "title": m.get("title"),
        "text": src.get("text"),
        "authority_level": m.get("authority_level"),
        "status": m.get("status"),
        "_score": hit.get("_score", 1.0),
    }


def _rrf_fuse(bm25_hits, knn_hits, top_k: int) -> List[Dict[str, Any]]:
    """Python 端 Reciprocal Rank Fusion（basic 许可证无 rank.rrf 插件）。"""
    scores: Dict[str, float] = {}
    hits_by_id: Dict[str, Any] = {}
    for rank, h in enumerate(bm25_hits):
        cid = h.get("_id")
        scores[cid] = scores.get(cid, 0.0) + settings.bm25_weight / (rank + 1 + _RRF_K)
        hits_by_id[cid] = h
    for rank, h in enumerate(knn_hits):
        cid = h.get("_id")
        scores[cid] = scores.get(cid, 0.0) + settings.vector_weight / (rank + 1 + _RRF_K)
        hits_by_id[cid] = h
    ranked = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)[:top_k]
    return [_es_hit_to_dict(hits_by_id[c]) for c in ranked]


def _mapping() -> Dict[str, Any]:
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "analyzer": {
                    "default": {"type": "custom", "tokenizer": "standard",
                                "filter": ["lowercase"]}
                }
            },
        },
        "mappings": {
            "properties": {
                # page_content：chunk 正文，BM25 检索字段
                "text": {"type": "text", "analyzer": settings.es_analyzer},
                # 向量字段名固定为 embedding（与 ElasticsearchStore(embedding_field=) 对应）
                "embedding": {
                    "type": "dense_vector",
                    "dims": settings.es_embed_dims,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "hnsw", "m": 16, "ef_construction": 200},
                },
                # ACL / 展示字段统一放在 metadata 对象，便于权限预过滤与回读
                "metadata": {
                    "properties": {
                        "emp_id": {"type": "long"},
                        "report_id": {"type": "long"},
                        "report_version_id": {"type": "long"},
                        "chunk_id": {"type": "keyword"},
                        "chunk_type": {"type": "keyword"},
                        "title": {"type": "text", "analyzer": settings.es_analyzer},
                        "authority_level": {"type": "integer"},
                        "status": {"type": "integer"},
                    }
                },
            }
        },
    }


def ensure_index(force_recreate: bool = False) -> None:
    client = get_client()
    exists = client.indices.exists(index=settings.es_index)
    if exists and not force_recreate:
        logger.info("ES index %s already exists", settings.es_index)
        return
    if exists and force_recreate:
        client.indices.delete(index=settings.es_index)
    client.indices.create(index=settings.es_index, body=_mapping())
    logger.info("ES index %s created", settings.es_index)


def index_document(doc: Dict[str, Any]) -> None:
    """写入单条 chunk 文档（_id 由调用方保证幂等）。"""
    client = get_client()
    client.index(index=settings.es_index, id=doc["_id"], document=doc["_source"])


def bulk_index(docs: Sequence[Dict[str, Any]]) -> int:
    """批量写入，返回成功条数。"""
    from elasticsearch.helpers import bulk

    client = get_client()
    actions = [
        {"_index": settings.es_index, "_id": d["_id"], "_source": d["_source"]} for d in docs
    ]
    success, _ = bulk(client, actions, raise_on_error=False)
    return int(success)


def indexed_file_ids() -> set:
    """返回 ES 中已索引的去重文件 id 集合（来自 metadata.report_id）。"""
    client = get_client()
    resp = client.search(
        index=settings.es_index,
        body={"size": 0, "aggs": {"files": {"terms": {"field": "metadata.report_id", "size": 10000}}}},
    )
    buckets = resp.get("aggregations", {}).get("files", {}).get("buckets", [])
    return {int(b["key"]) for b in buckets}


def _g1_filter(emp_id: int, allowed_file_ids: Optional[Sequence[int]],
               extra_filters: Optional[Dict[str, Any]]):
    """G1 检索前预过滤：租户隔离 + 可读文件集合（权限感知）。"""
    must: List[Dict[str, Any]] = [{"term": {"metadata.emp_id": emp_id}}]
    if allowed_file_ids is not None:
        must.append({"terms": {"metadata.report_id": list(allowed_file_ids)}})
    if extra_filters:
        for k, v in extra_filters.items():
            must.append({"term": {f"metadata.{k}": v}})
    return {"bool": {"must": must}}


def _doc_to_hit(doc) -> Dict[str, Any]:
    """把 LangChain Document 还原为与检索层契约一致的 dict。"""
    m = doc.metadata or {}
    _id = getattr(doc, "id", None) or m.get("chunk_id")
    return {
        "_id": _id,
        "report_id": m.get("report_id"),
        "report_version_id": m.get("report_version_id"),
        "chunk_id": m.get("chunk_id"),
        "chunk_type": m.get("chunk_type"),
        "title": m.get("title"),
        "text": doc.page_content,
        "authority_level": m.get("authority_level"),
        "status": m.get("status"),
        "_score": 1.0,
    }


def hybrid_search(
    query: str,
    top_k: int = 6,
    emp_id: int = 0,
    allowed_file_ids: Optional[Sequence[int]] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """权限感知混合检索：BM25 + kNN 经 LangChain EnsembleRetriever 融合。

    - G1 检索前预过滤：仅在 allowed_file_ids 指定的可读文件内检索。
    - 融合在 Python 端完成（EnsembleRetriever 的内置 RRF），无需 ES rank.rrf 许可。

    :return: 命中列表，每项含 report_id / report_version_id / chunk_id / title /
             text / authority_level / status（契约见 retrieval.retriever）。
    """
    client = get_client()
    g1 = _g1_filter(emp_id, allowed_file_ids, extra_filters)
    _src = ["text", "metadata"]

    # 空查询（如 Wiki 全量编译）：直接按 G1 过滤返回该文件全部分片
    if not (query and query.strip()):
        body = {
            "size": top_k,
            "query": g1,
            "_source": _src,
            "sort": [{"metadata.chunk_id": {"order": "asc"}}],
        }
        hits = client.search(index=settings.es_index, body=body).get("hits", {}).get("hits", [])
        return [_es_hit_to_dict(h) for h in hits]

    # BM25
    bm25_body = {
        "size": top_k,
        "query": {"bool": {"must": {"match": {"text": query}}, "filter": g1}},
        "_source": _src,
    }
    # kNN
    num_candidates = max(200, top_k * 5)
    knn_body = {
        "size": top_k,
        "knn": {
            "field": "embedding",
            "query_vector": embed_query(query),
            "k": top_k,
            "num_candidates": num_candidates,
            "filter": g1,
        },
        "_source": _src,
    }
    bm25_hits = client.search(index=settings.es_index, body=bm25_body).get("hits", {}).get("hits", [])
    knn_hits = client.search(index=settings.es_index, body=knn_body).get("hits", {}).get("hits", [])
    return _rrf_fuse(bm25_hits, knn_hits, top_k=top_k)


def as_context(
    chunks: Sequence[Dict[str, Any]],
    wiki_pages: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """把检索结果拼装为问答上下文列表（供 prompts.build_user_content 渲染）。

    Progressive RAG 上下文顺序：
      1. Wiki 综述页（page_id 标记）——归纳为优先，跨汇报聚合；
      2. L0 chunk 细节证据（chunk_id 标记）——用于佐证具体出处。

    :param chunks: hybrid_search 返回的 chunk 列表
    :param wiki_pages: 可选，来自 wiki.retrieval 的 Wiki 页面列表
    :return: 上下文 dict 列表，每项含 report_id / report_version_id / chunk_id /
             page_id / title / text（page_id 仅在 Wiki 页非零）
    """
    ctx: List[Dict[str, Any]] = []
    for p in (wiki_pages or []):
        ctx.append({
            "report_id": 0,
            "report_version_id": 0,
            "chunk_id": f"wiki:{p.get('page_id')}",
            "page_id": p.get("page_id"),
            "title": f"[Wiki综述] {p.get('title', '')}",
            "text": p.get("summary") or p.get("markdown") or "",
        })
    for c in (chunks or []):
        ctx.append({
            "report_id": c.get("report_id"),
            "report_version_id": c.get("report_version_id"),
            "chunk_id": c.get("chunk_id"),
            "page_id": 0,
            "title": c.get("title") or "",
            "text": c.get("text") or "",
        })
    return ctx
