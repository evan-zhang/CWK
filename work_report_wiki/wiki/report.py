# -*- coding: utf-8 -*-
"""工作汇报 Wiki 构建。

链路：
  listAllReportIds (按 emp_id + 时间范围取汇报 id 列表)
    -> getReportSimpleInfo (逐篇取 content/file/reply，拼接为单篇文档)
    -> split_text (定长切片为 chunks)
    -> WikiSourceDoc(report_id, version_id) 聚合
    -> run_wiki_pipeline 编译 + 摘要 + 幂等落库 (emp_id 过滤)

版本号（无显式版本，更新时间驱动）：
  重新构建时计算内容 content_hash，与 file_version_chain 最新版本比较：
    内容未变 -> 沿用当前版本（幂等）；内容变化 -> 升一版（旧版本标记 superseded）。
  该版本号随 WikiSourceDoc.version_id 传入，最终落在 wiki_page_source.report_version_id。

落库时 tenant 维度统一使用 emp_id（与全库改名后的 emp_id 列一致）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

import requests

from ..db import execute, query_all
from .build import WikiSourceDoc
from .compile import WikiCompiler
from .pipeline import run_wiki_pipeline

logger = logging.getLogger(__name__)

# 汇报接口基础地址与鉴权头。测试阶段固定；生产应下沉到配置。
REPORT_BASE_URL = "http://test-report.internal.com"
REPORT_AUTH = "6c911fa0-c816-3abc-81cf-852a77d762d6"

_DEFAULT_BEGIN = "2026-08-01 00:00:00"
_DEFAULT_END = "2026-08-23 23:59:59"
_CHUNK_SIZE = 2000
_REQUEST_TIMEOUT = 10
_MAX_RETRIES = 2


def _auth_headers() -> Dict[str, Any]:
    # 注意不要将 REPORT_AUTH 打印到日志
    return {
        "Content-Type": "application/json",
        "Authorization": REPORT_AUTH,
    }


def _post(path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """带超时与重试的内部 POST；失败返回 None（由调用方决定是否跳过）。"""
    url = f"{REPORT_BASE_URL}{path}"
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - 网络层异常统一兜底
            last_err = e
            logger.warning("report api %s attempt %d failed: %s", path, attempt, e)
    logger.error("report api %s finally failed: %s", path, last_err)
    return None


def fetch_report_ids(emp_id: int, begin_time: str, end_time: str) -> List[int]:  # noqa: D103
    """获取指定员工在 [begin_time, end_time] 内的全部汇报 id 列表。"""
    data = _post(
        "/inner/report/record/listAllReportIds",
        {"targetEmpId": emp_id, "beginTime": begin_time, "endTime": end_time},
    )
    if not data:
        return []
    # 兼容多种返回结构：data 可能是 {recordIds:[...]} / {data:[...]} / [...] 等
    if isinstance(data, list):
        return [int(x) for x in data]
    if isinstance(data, dict):
        for key in ("recordIds", "reportRecordIds", "ids", "data", "list"):
            val = data.get(key)
            if isinstance(val, list):
                return [int(x) for x in val]
    logger.warning("fetch_report_ids: unexpected response shape: %r", data)
    return []


def _extract_file_names(*objs: Any) -> List[str]:
    """从若干（可能嵌套的）对象中尽量抽取附件文件名/标题。"""
    names: List[str] = []
    for obj in objs:
        if not obj:
            continue
        if isinstance(obj, str):
            names.append(obj)
        elif isinstance(obj, list):
            for it in obj:
                names.extend(_extract_file_names(it))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ("filename", "name", "title", "file_name", "originalname", "url"):
                    if isinstance(v, str) and v:
                        names.append(v)
                else:
                    names.extend(_extract_file_names(v))
    # 去重保序
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def fetch_report_content(report_record_id: int) -> str:
    """拉取单篇汇报并拼接为纯文本文档。

    真实返回结构（已验证）：
      {resultCode, data: {reportRecord: {main, content, leadContent, ...},
                          replyList: [{replyEmpName, title, content, ...}],
                          atDatabaseFileList, ...}}

    拼接顺序：标题(main) -> 正文(content) -> 附件 -> 回复(replyList)。
    接口失败或结构异常时返回空串（调用方跳过该篇）。
    优化：只取正文及回复["content", "file", "reply"]->["content", "reply"]
    """
    data = _post(
        "/inner/report/record/getReportSimpleInfo",
        {"reportRecordId": report_record_id, "typeList": ["content", "reply"]},
    )
    if not data:
        return ""
    # 兼容两种外层：{data: {...}} 或直接为负载
    payload = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(payload, dict):
        return ""

    parts: List[str] = []

    rr = payload.get("reportRecord") or {}
    if isinstance(rr, dict):
        main = rr.get("main") or rr.get("title")
        if isinstance(main, str) and main.strip():
            parts.append(f"# {main.strip()}")
        content = rr.get("content") or rr.get("text") or rr.get("leadContent")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
        # reportRecord 内可能携带附件字段
        file_names = _extract_file_names(
            rr.get("fileList"), rr.get("atDatabaseFileList"), rr.get("attachments"),
        )
        for fn in file_names:
            parts.append(f"[附件] {fn}")

    # 顶层附件
    for fn in _extract_file_names(payload.get("atDatabaseFileList")):
        parts.append(f"[附件] {fn}")

    # 回复
    for r in (payload.get("replyList") or []):
        if not isinstance(r, dict):
            continue
        who = r.get("replyEmpName") or r.get("empName") or ""
        txt = r.get("content") or r.get("text") or ""
        if txt:
            prefix = f"[回复]{who}：" if who else "[回复]："
            parts.append(prefix + txt)
        for fn in _extract_file_names(r.get("fileList")):
            parts.append(f"[附件] {fn}")

    return "\n\n".join(p for p in parts if p).strip()


def split_text(text: str, size: int = _CHUNK_SIZE) -> List[Dict[str, Any]]:
    """按字符长度定长切分为 chunks（WikiCompiler 消费的 {"text": ...} 列表）。"""
    text = text or ""
    if not text:
        return []
    chunks: List[Dict[str, Any]] = []
    for i in range(0, len(text), size):
        chunk = text[i:i + size].strip()
        if chunk:
            chunks.append({"text": chunk})
    return chunks


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_report_version(emp_id: int, report_id: int, content: str) -> int:
    """版本号「更新时间/内容驱动」（与 l0_ingest 共用 file_version_chain 语义）。

    - 无记录 -> version_id = 1（首次构建）
    - 最新版本 content_hash 与本次一致 -> 沿用当前版本（幂等）
    - hash 不同（汇报被更新）-> 升一版，旧版本标记 superseded，并写入新版本链
    """
    new_hash = _content_hash(content)
    row = execute(
        """
        SELECT version_id, content_hash FROM file_version_chain
        WHERE emp_id = :e AND report_id = :f
        ORDER BY version_id DESC LIMIT 1
        """,
        {"e": emp_id, "f": report_id},
    ).fetchone()
    if row is None:
        version = 1
    else:
        cur_v, cur_h = int(row[0]), row[1]
        if cur_h == new_hash:
            version = cur_v  # 内容未变，幂等沿用
        else:
            version = cur_v + 1
            execute(
                "UPDATE file_version_chain SET status=2 WHERE emp_id=:e AND report_id=:f AND version_id=:v",
                {"e": emp_id, "f": report_id, "v": cur_v},
            )
    # 写入/更新版本链
    execute(
        """
        INSERT INTO file_version_chain
            (emp_id, report_id, version_id, file_name, content_hash, status, authority_level)
        VALUES (:e, :f, :v, :n, :h, 1, 1)
        ON DUPLICATE KEY UPDATE
            file_name=VALUES(file_name), content_hash=VALUES(content_hash),
            status=1, authority_level=VALUES(authority_level)
        """,
        {
            "e": emp_id, "f": report_id, "v": version,
            "n": f"report_{report_id}", "h": new_hash,
        },
    )
    return version


def build_report_wiki(
    emp_id: int,
    begin_time: str = _DEFAULT_BEGIN,
    end_time: str = _DEFAULT_END,
    folder_id: int = 0,
    consume_only: bool = False,
    page_size: int = 0,
    max_pages: int = 0,
) -> Dict[str, Any]:
    """编排「拉列表 -> 逐篇拉内容 -> 切片 -> 版本解析 -> 编译 -> 幂等落库」。

    consume_only=True：仅消费 report_summary 中已生成(done)的汇报级提炼，缺失则跳过该篇
        （不调用 LLM）。用于生产环境——提炼由工作协同系统等外部生产者负责。
    consume_only=False（默认，验证期）：缺失提炼时由 Wiki 兜底 refine_report 一次生成并回写。

    返回 {emp_id, report_count, success, failed, skipped, summary_page_count,
          index_page_id, lint, error}。
    """
    report_ids = fetch_report_ids(emp_id, begin_time, end_time)
    logger.info("build_report_wiki: emp_id=%s got %d report ids (consume_only=%s)",
                emp_id, len(report_ids), consume_only)

    docs: List[WikiSourceDoc] = []
    success = 0
    failed = 0
    skipped = 0
    for rid in report_ids:
        try:
            text = fetch_report_content(rid)
            chunks = split_text(text)
            if not chunks:
                logger.warning("report %s has no content, skipped", rid)
                failed += 1
                continue
            # 版本号更新时间驱动：内容变化才升版
            version = _resolve_report_version(emp_id, rid, text)
            # 消费「汇报级提炼」：仅读取 report_summary 表（由外部系统/异步任务写入）。
            refine = read_report_summary(rid)
            if refine is None:
                if consume_only:
                    # 生产态：缺失提炼直接跳过，由外部系统补齐后重跑。
                    logger.info("report %s 无提炼且 consume_only，跳过", rid)
                    skipped += 1
                    continue
                # 验证态：Wiki 兜底一次 LLM 产出完整提炼并回写 report_summary。
                refine_md = ensure_report_summary(rid, text, version, emp_id=emp_id)
                refine = read_report_summary(rid) if refine_md else None
            docs.append(WikiSourceDoc(
                report_id=rid, version_id=version,
                file_name=f"report_{rid}", chunks=chunks,
                full_text=text,
                summary_markdown=refine.get("markdown") if refine else None,
                entities=refine.get("entities") if refine else None,
                concepts=refine.get("concepts") if refine else None,
            ))
            success += 1
        except Exception as e:  # noqa: BLE001
            logger.error("report %s build failed: %s", rid, e)
            failed += 1

    if not docs:
        return {
            "emp_id": emp_id,
            "report_count": len(report_ids), "success": success, "failed": failed,
            "skipped": skipped,
            "summary_page_count": 0,
            "index_page_id": None, "lint": {"ok": True, "total_pages": 0},
            "error": "no report docs to build",
        }

    compiler = WikiCompiler(emp_id=emp_id)
    # 分页构建：近一年等大规模时按 page_size 切片，每批独立跑流水线——天然支持限流与断点续跑
    # （单批失败不影响其他批；重跑时整批幂等）。
    if page_size and page_size > 0:
        batches = [docs[i:i + page_size] for i in range(0, len(docs), page_size)]
    else:
        batches = [docs]
    if max_pages and max_pages > 0:
        batches = batches[:max_pages]

    agg_summary = 0
    agg_lint_ok = True
    last_lint = {"ok": True, "total_pages": 0}
    last_index = None
    agg_timings = {}        # 各阶段累计耗时（跨批次累加）
    batch_timings = []      # 每批次耗时明细
    for bidx, bdocs in enumerate(batches, 1):
        print(f"[BATCH] {bidx}/{len(batches)} 开始：{len(bdocs)} 篇", flush=True)
        try:
            res = run_wiki_pipeline(
                emp_id=emp_id, docs=bdocs,
                compiler=compiler, folder_id=folder_id,
            )
            agg_summary += res.summary_page_count
            if res.lint:
                agg_lint_ok = agg_lint_ok and bool(res.lint.get("ok"))
                last_lint = res.lint
            last_index = res.index_page_id
            # 累加各阶段耗时（total 不跨批累加，单批 total 见 batch_timings）
            for ph, sec in (res.timings or {}).items():
                if ph == "total":
                    continue
                agg_timings[ph] = round(agg_timings.get(ph, 0.0) + sec, 3)
            batch_timings.append({"batch": bidx, "docs": len(bdocs), "timings": res.timings})
        except Exception as e:  # noqa: BLE001
            logger.exception("run_wiki_pipeline 第 %s 批失败 for emp_id=%s", bidx, emp_id)
            agg_lint_ok = False
            failed += len(bdocs)

    return {
        "emp_id": emp_id,
        "report_count": len(report_ids), "success": success, "failed": failed,
        "skipped": skipped, "batches": len(batches),
        "summary_page_count": agg_summary,
        "index_page_id": last_index,
        "lint": last_lint,
        "timings": agg_timings,
        "batch_timings": batch_timings,
        "error": None,
    }


def read_report_summary(report_id: int) -> Optional[Dict]:
    """只读消费：返回 report_summary 表中已生成(done)的汇报级提炼字典；缺失/未生成则返回 None。

    返回结构（来自表列）：
        {"title":..., "summary":..., "markdown":..., "entities":[...], "concepts":[...]}

    本函数不触发 LLM 调用，供 Wiki 构建在 MAP 前快速消费外部系统已写好的汇报级提炼。
    若返回 None，Wiki 在 --consume-only 模式下跳过该篇；否则由 refine_report 兜底生成（并回写）。
    """
    try:
        row = query_all(
            "SELECT title, summary, markdown, entities, concepts, summary_status "
            "FROM report_summary WHERE report_id=:rid",
            {"rid": report_id},
        )
        if row and row[0].get("summary_status") == 1:
            r = row[0]
            return {
                "title": r.get("title"),
                "summary": r.get("summary"),
                "markdown": r.get("markdown"),
                # MySQL JSON 列经 SQLAlchemy 可能已是 list，或仍是 str，统一兜底解析
                "entities": _coerce_json(r.get("entities"), default=[]),
                "concepts": _coerce_json(r.get("concepts"), default=[]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 report_summary 失败 report=%s: %s", report_id, exc)
    return None


def _coerce_json(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        import json as _json
        return _json.loads(value)
    except Exception:  # noqa: BLE001
        return default


def ensure_report_summary(
    report_id: int,
    text: str,
    version_id: int,
    emp_id: Optional[int] = None,
) -> Optional[str]:
    """确保单篇汇报摘要存在并返回其 markdown。

    设计取舍（汇报摘要独立存储，一篇汇报一条，无需 emp_id）：
      * 一篇汇报(report_id)全局唯一，摘要按 report_id 只存一条，由 Wiki 与各消费方共享，
        不应「每人为同一篇汇报各生成一份」。
      * 摘要可不由 Wiki 负责：工作协同系统等外部生产者写入 report_summary 表，Wiki 仅消费。
      * summary_status 驱动异步重算：汇报更新后由上游/异步任务置为 0(pending)；
        此处若 status!=1 或 content_hash 不匹配，则以 full_text[:20000] 兜底生成并回写。
      * Wiki 是 fallback 生产者——常态下应已由外部系统生成，缺失才补。

    emp_id 仅用于构造 LLM 调用的编译器实例，不再作为 report_summary 表的维度。

    返回 report_summary.markdown（已生成/已消费），无内容时返回 None。
    """
    new_hash = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    # 1) 读取已有摘要：status=1(done) 且 content_hash 匹配则直接消费，不重复调 LLM。
    try:
        row = query_all(
            "SELECT markdown, summary, title, content_hash, summary_status "
            "FROM report_summary WHERE report_id=:rid",
            {"rid": report_id},
        )
        if row and row[0].get("summary_status") == 1 and row[0].get("content_hash") == new_hash:
            return row[0].get("markdown") or row[0].get("summary")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 report_summary 失败(将 fallback 生成): %s", exc)

    # 2) fallback：基于完整汇报前 20000 字符，**一次 LLM 调用**产出汇报级提炼
    #    （摘要 + 实体候选 + 概念候选），避免对单篇原文多次串行调用。
    try:
        compiler = WikiCompiler(emp_id=emp_id) if emp_id is not None else WikiCompiler()
        raw = compiler.refine_report(f"report_{report_id}", version_id, text)
        markdown = raw.get("markdown") or raw.get("summary")
        title = raw.get("title") or f"摘要：report_{report_id}"
        summary = raw.get("summary") or (markdown or "")[:200]
        entities = raw.get("entities") or []
        concepts = raw.get("concepts") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("fallback 生成 report_summary 失败 report=%s: %s", report_id, exc)
        return None

    if not markdown:
        return None

    # 3) UPSERT 回写 report_summary（status=1 done）；外部系统写入的提炼也应保持同样结构。
    try:
        execute(
            """
            INSERT INTO report_summary
                (report_id, version_id, title, summary, markdown, entities, concepts,
                 content_hash, summary_status, generated_at, created_at, updated_at)
            VALUES (:rid, :vid, :title, :summary, :markdown, :entities, :concepts,
                    :chash, 1, NOW(), NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                version_id=VALUES(version_id), title=VALUES(title), summary=VALUES(summary),
                markdown=VALUES(markdown), entities=VALUES(entities), concepts=VALUES(concepts),
                content_hash=VALUES(content_hash),
                summary_status=1, generated_at=NOW(), updated_at=NOW()
            """,
            {
                "rid": report_id, "vid": version_id, "title": title,
                "summary": summary, "markdown": markdown,
                "entities": json.dumps(entities, ensure_ascii=False),
                "concepts": json.dumps(concepts, ensure_ascii=False),
                "chash": new_hash,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("回写 report_summary 失败 report=%s: %s", report_id, exc)
    return markdown
