# -*- coding: utf-8 -*-
"""Embedding 封装：langchain OpenAIEmbeddings（text-embedding-v4，OpenAI 兼容）。

- 模型输出 1024 维，与 ES wiki_chunks.embedding 维度一致（遵循“维度不要动”约束）。
- 查询与文档共用同一模型，保证向量空间一致。
"""
from __future__ import annotations

from typing import List

from langchain_openai import OpenAIEmbeddings

from .config import settings


def get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        dimensions=settings.es_embed_dims,
        check_embedding_ctx_length=False,
        # 嵌入接口限单批条数（实测上限约 10），分批避免 400 错误
        chunk_size=10,
        max_retries=3,
    )


def embed_query(text: str) -> List[float]:
    return get_embeddings().embed_query(text)


def embed_documents(texts: List[str]) -> List[List[float]]:
    return get_embeddings().embed_documents(texts)
