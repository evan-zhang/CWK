# -*- coding: utf-8 -*-
"""检索封装（简化版：无权限过滤）。

仅提供基于 allowed_file_ids 范围的混合检索。权限鉴权已随 grant 模块移除。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .. import es_store

logger = logging.getLogger(__name__)


@dataclass
class Retriever:
    emp_id: int
    request_id: str = "cli"
    allowed_file_ids: Optional[List[int]] = None

    def retrieve(self, query: str, top_k: int = 6):
        chunks = es_store.hybrid_search(
            query, top_k=top_k, emp_id=self.emp_id,
            allowed_file_ids=self.allowed_file_ids,
        )
        return RetrievalResult(chunks=chunks)


@dataclass
class RetrievalResult:
    chunks: List[dict] = field(default_factory=list)

    def as_context(self) -> str:
        return es_store.as_context(self.chunks)

    @property
    def allowed_hit_count(self) -> int:
        return len(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)
