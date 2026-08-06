# -*- coding: utf-8 -*-
"""MySQL 引擎与 DDL 引导。"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, text

from .config import settings

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_DDL_PATH = Path(__file__).resolve().parent / "schema" / "ddl.sql"


def get_engine() -> Engine:
    """惰性创建并复用 SQLAlchemy 引擎（连接池）。"""
    global _engine
    if _engine is None:
        _engine = create_engine(settings.db_url, pool_pre_ping=True, pool_recycle=1800)
        logger.info("MySQL engine created -> %s", settings.db_name)
    return _engine


def bootstrap_ddl() -> None:
    """执行 schema/ddl.sql，创建全部 V3 表（幂等，IF NOT EXISTS）。

    之后追加幂等迁移：DB 中可能已存在旧版 wiki_page（无 folder_id / page_key，
    “一文一页”唯一键 uk_page_source 等），IF NOT EXISTS 不会重建旧表，因此
    需要 ALTER 补齐缺失的列与约束、移除已废弃结构，保证演进中的旧库也能自愈。
    """
    sql = _DDL_PATH.read_text(encoding="utf-8")
    engine = get_engine()
    with engine.begin() as conn:  # 事务块，支持多条语句
        # 拆分执行（部分驱动不支持一次性多语句）
        for stmt in _split_sql(sql):
            if stmt.strip():
                conn.execute(text(stmt))
    # 旧表结构兼容迁移
    _migrate_schema()
    logger.info("bootstrap_ddl done for database=%s", settings.db_name)


def _col_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(text(
        "SELECT COUNT(*) AS c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).mappings().first()
    return bool(row and row["c"])


def _index_exists(conn, table: str, index: str) -> bool:
    row = conn.execute(text(
        "SELECT COUNT(*) AS c FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
    ), {"t": table, "i": index}).mappings().first()
    return bool(row and row["c"])


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(text(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :t"
    ), {"t": table}).mappings().first()
    return bool(row and row["c"])


def _rename_tenant_to_emp(conn) -> None:
    """兼容旧库：将 tenant_id 列重命名为 emp_id（DDL 新版已使用 emp_id）。

    仅当该表同时存在 tenant_id 列且不存在 emp_id 列时执行 CHANGE，避免重复或冲突。
    列类型统一为 BIGINT NOT NULL（与 DDL 一致）。
    """
    tables = (
        "wiki_page", "wiki_page_source",
        "wiki_compilation_task", "wiki_folders", "wiki_log_entries",
        "ingest_batch", "ingest_event_log", "event_log", "file_version_chain",
        "grant_audit", "topic", "topic_document",
    )
    for t in tables:
        if not _table_exists(conn, t):
            continue
        has_old = _col_exists(conn, t, "tenant_id")
        has_new = _col_exists(conn, t, "emp_id")
        if has_old and not has_new:
            conn.execute(text(f"ALTER TABLE {t} CHANGE tenant_id emp_id BIGINT NOT NULL"))


def _rename_kb_to_report(conn) -> None:
    """兼容旧库：将 kb_file_id -> report_id、kb_file_version_id -> report_version_id。

    旧库使用 kb_file（knowledge base file）命名；重构后统一为 report（工作汇报）。
    仅当旧列存在且新列不存在时执行 CHANGE，幂等且避免冲突。
    """
    # 所有曾使用 kb_file_id / kb_file_version_id 的表
    tables = (
        "file_version_chain", "ingest_event_log", "wiki_page_source",
        "wiki_compilation_task", "ingest_batch", "topic_document",
    )
    for t in tables:
        if not _table_exists(conn, t):
            continue
        if _col_exists(conn, t, "kb_file_id") and not _col_exists(conn, t, "report_id"):
            conn.execute(text(
                f"ALTER TABLE {t} CHANGE kb_file_id report_id BIGINT NOT NULL DEFAULT 0"
            ))
        if _col_exists(conn, t, "kb_file_version_id") and not _col_exists(conn, t, "report_version_id"):
            conn.execute(text(
                f"ALTER TABLE {t} CHANGE kb_file_version_id report_version_id BIGINT NOT NULL DEFAULT 0"
            ))


def _migrate_schema() -> None:
    """幂等迁移（兼容旧库）：
    - 将旧库中的 tenant_id 列统一重命名为 emp_id（若二者并存则跳过，避免冲突）；
    - 将旧库的 kb_file_id / kb_file_version_id 重命名为 report_id / report_version_id；
    - 清理已废弃的 grant_cache / acl_revision 权限缓存表；
    - wiki_page：补齐 folder_id / page_key，移除“一文一页”的 uk_page_source 与
      source_file_id 单源列，改为 page_key 幂等。
    """
    with get_engine().begin() as conn:
        # 兼容旧库：tenant_id -> emp_id 整列重命名（仅当 tenant_id 存在且 emp_id 不存在）
        _rename_tenant_to_emp(conn)
        # tenant->emp 之后，kb_file -> report 旧列重命名
        _rename_kb_to_report(conn)
        # 1) 清理已废弃的权限缓存表（DDL 已不再创建；旧库需删除）
        for t in ("grant_cache", "acl_revision"):
            if _table_exists(conn, t):
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        # 2) 彻底移除 project_id 列（每用户独立构建，不再有项目概念）
        for t in (
            "wiki_page", "wiki_page_source", "wiki_compilation_task",
            "wiki_folders", "wiki_log_entries", "file_version_chain",
            "ingest_batch", "topic",
        ):
            if _table_exists(conn, t) and _col_exists(conn, t, "project_id"):
                conn.execute(text(f"ALTER TABLE {t} DROP COLUMN project_id"))

        # wiki_page 演进字段
        # folder_id：导航用归属文件夹（非安全边界）
        if not _col_exists(conn, "wiki_page", "folder_id"):
            conn.execute(text(
                "ALTER TABLE wiki_page ADD COLUMN folder_id BIGINT NOT NULL DEFAULT 0 "
                "COMMENT '归属文件夹，0=根；仅导航用途，非安全边界' AFTER emp_id"
            ))
        # 移除旧的“一文一页”唯一键（先于删列，避免依赖）
        if _index_exists(conn, "wiki_page", "uk_page_source"):
            conn.execute(text("ALTER TABLE wiki_page DROP INDEX uk_page_source"))
        # page_key：多文档聚合后的稳定幂等键
        if not _col_exists(conn, "wiki_page", "page_key"):
            conn.execute(text(
            "ALTER TABLE wiki_page ADD COLUMN page_key VARCHAR(128) NULL "
            "COMMENT '页面稳定标识（topic slug）；与 emp_id 组成幂等键' AFTER folder_id"
            ))
        if not _index_exists(conn, "wiki_page", "uk_page_key"):
            conn.execute(text(
                "ALTER TABLE wiki_page ADD UNIQUE KEY uk_page_key (emp_id, page_key)"
            ))
        else:
            # 旧唯一键可能仍含 project_id，重建为 (emp_id, page_key)
            conn.execute(text("ALTER TABLE wiki_page DROP INDEX uk_page_key"))
            conn.execute(text(
                "ALTER TABLE wiki_page ADD UNIQUE KEY uk_page_key (emp_id, page_key)"
            ))
        # 目录索引
        if not _index_exists(conn, "wiki_page", "idx_page_folder"):
            conn.execute(text(
                "ALTER TABLE wiki_page ADD INDEX idx_page_folder (emp_id, folder_id)"
            ))
        # source_file_id 单源列已废弃：直接删除（新库不再需要回填）
        if _col_exists(conn, "wiki_page", "source_file_id"):
            conn.execute(text("ALTER TABLE wiki_page DROP COLUMN source_file_id"))
        # links：页面正文中的 [[slug]] 内部链接目标列表
        if not _col_exists(conn, "wiki_page", "links"):
            conn.execute(text(
                "ALTER TABLE wiki_page ADD COLUMN links JSON NULL "
                "COMMENT '页面正文中的 [[slug]] 内部链接目标列表' AFTER summary"
            ))

    logger.info("schema migration check done")


def _split_sql(sql: str):
    """按分号切分，去掉注释与空语句。"""
    out = []
    buf = ""
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf += line + "\n"
        if line.strip().endswith(";"):
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def execute(sql: str, params: Optional[dict] = None):
    engine = get_engine()
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})


@contextmanager
def transaction():
    """提供一个已开启的事务连接，供多语句原子写入（如整页落库）。"""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def query_all(sql: str, params: Optional[dict] = None) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(r) for r in result.mappings()]
