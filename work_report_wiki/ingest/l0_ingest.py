# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from ..db import execute
from ..embeddings import embed_documents
from ..es_store import bulk_index, ensure_index
from .chunker import chunk_text

logger = logging.getLogger(__name__)


@dataclass
class IngestDoc:
    emp_id: int
    report_id: int
    version_id: int
    file_name: str
    content: str
    authority_level: int = 1


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_version(doc: IngestDoc) -> int:
    """版本号「更新时间/内容驱动」：

    汇报本身无显式版本号。重新入库同一 report_id 时：
      * 若 file_version_chain 无该 report 记录 -> version_id = 1（首次）
      * 若有记录且最新 content_hash 与本次一致 -> 沿用当前最新 version（内容未变，幂等）
      * 若有记录但 content_hash 不同 -> 升一版（version_id = max+1），旧版本标记 superseded
    返回最终落库的 version_id，并写回 doc.version_id 供 ES / 事件日志使用。
    """
    row = execute(
        """
        SELECT version_id, content_hash, status FROM file_version_chain
        WHERE emp_id = :e AND report_id = :f
        ORDER BY version_id DESC LIMIT 1
        """,
        {"e": doc.emp_id, "f": doc.report_id},
    ).fetchone()
    new_hash = _content_hash(doc.content)
    if row is None:
        doc.version_id = 1
    else:
        cur_v, cur_h, _ = row[0], row[1], row[2]
        if cur_h == new_hash:
            doc.version_id = int(cur_v)  # 内容未变，幂等沿用
        else:
            doc.version_id = int(cur_v) + 1
            # 旧版本标记失效
            execute(
                "UPDATE file_version_chain SET status=2 WHERE emp_id=:e AND report_id=:f AND version_id=:v",
                {"e": doc.emp_id, "f": doc.report_id, "v": int(cur_v)},
            )
    return doc.version_id


def _write_version_chain(doc: IngestDoc) -> None:
    execute(
        """
        INSERT INTO file_version_chain
            (emp_id, report_id, version_id, file_name, content_hash, status, authority_level)
        VALUES
            (:e, :f, :v, :n, :h, 1, :a)
        ON DUPLICATE KEY UPDATE
            file_name=VALUES(file_name), content_hash=VALUES(content_hash),
            status=1, authority_level=VALUES(authority_level)
        """,
        {
            "e": doc.emp_id, "f": doc.report_id,
            "v": doc.version_id, "n": doc.file_name,
            "h": _content_hash(doc.content), "a": doc.authority_level,
        },
    )


def _write_event(doc: IngestDoc, event_type: str, status: int = 1, detail: Optional[dict] = None) -> None:
    execute(
        """
        INSERT INTO ingest_event_log
            (emp_id, report_id, event_type, status, detail)
        VALUES (:e, :f, :e2, :s, :d)
        """,
        {"e": doc.emp_id, "f": doc.report_id, "e2": event_type,
         "s": status, "d": __import__("json").dumps(detail or {})},
    )


def ingest_documents(docs: List[IngestDoc]) -> dict:
    """批量入库。返回统计。

    每篇文档在入库前先经 _resolve_version 决定版本号（内容驱动），
    重新拉取内容变化的汇报会升版，未变的幂等沿用。
    """
    ensure_index()
    stats = {"files": 0, "chunks": 0, "failed": 0}
    for doc in docs:
        t0 = time.time()
        try:
            _resolve_version(doc)
            texts = chunk_text(doc.content)
            if not texts:
                stats["failed"] += 1
                continue
            vectors = embed_documents(texts)
            es_docs = []
            for i, (txt, vec) in enumerate(zip(texts, vectors)):
                cid = f"{doc.report_id}_{i}"
                es_docs.append({
                    "_id": cid,
                    "_source": {
                        # page_content：chunk 正文
                        "text": txt,
                        "embedding": vec,
                        # ACL / 展示字段统一置于 metadata（与检索预过滤、回读契约一致）
                        "metadata": {
                            "emp_id": doc.emp_id,
                            "report_id": doc.report_id,
                            "report_version_id": doc.version_id,
                            "chunk_id": cid,
                            "chunk_type": "text",
                            "title": doc.file_name,
                            "authority_level": doc.authority_level,
                            "status": 1,
                        },
                    },
                })
            n = bulk_index(es_docs)
            _write_version_chain(doc)
            _write_event(doc, "index", 1, {"chunks": n})
            stats["files"] += 1
            stats["chunks"] += n
            logger.info("ingested report_id=%s chunks=%s cost=%.1fs", doc.report_id, n, time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            logger.exception("ingest failed report_id=%s: %s", doc.report_id, exc)
    return stats
