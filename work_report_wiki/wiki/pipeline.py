# -*- coding: utf-8 -*-
"""Wiki 全链路生成编排

阶段：
  0. ingest   ：调用方提供 WikiSourceDoc（report_id / version_id / file_name / chunks）。
  1. MAP      ：每篇文档编译出 summary 页（直接成稿，兼作主题视图），并合并抽取
                entity/concept 候选（SlugUpdate，不生成正文）。MAP 按批并发。
  2. TAXONOMY：整批页面一次性规划分类（plan_batch_taxonomy），写 wiki_folders，
               产出 page_key -> folder_id（串行，reduce 前必须先收敛文件夹）。
  3. REDUCE   ：按 slug 聚合 SlugUpdate，每个 slug 一次 reduce_slug 调用生成/合并
                正文并幂等落库；summary 页直接落库。REDUCE 并发（reduceParallel）。
                落库后立即发布（publishDraftPages）。
  4. FINALIZE ：linkify 注入 [[slug]] 内部链接 + 清理死链 + 重建索引页 + 裁剪空文件夹
                （本实现在每批落库后统一收敛一次）。
  5. lint     ：校验每页来源完整性（至少 1 个源文件）。

说明：已移除 claim 抽取与证据组（无权限场景，claim 细粒度证据归属无意义）；
页面内容即 LLM 生成的 markdown。emp_id 即知识库 id，report_id 即文件 id。
"""
from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .build import WikiSourceDoc, build_pages_for_doc
from .compile import CompiledPage, WikiCompiler, SlugUpdate
from .persist import persist_page, publish_draft_pages
from . import taxonomy as taxonomy_mod
from . import finalize as finalize_mod
from .linkify import linkify_content

logger = logging.getLogger(__name__)

# 并发度
MAP_PARALLEL = 10
REDUCE_PARALLEL = 10
# 落库批次大小：每批落库后即发布，尽早可见。
# 调大到 50
# 当前 linkify 已零 LLM、落库为幂等单事务，逐页串行发布收益低、开销大，
# 加大批次可显著降低 publishDraftPages 调用次数）。
WIKI_MAX_DOCS_PER_BATCH = 50
# REDUCE slug 总数硬上限：超出部分降级为 stub（零 LLM），避免 2000 篇场景下
# 长尾 slug 失控导致 REDUCE 阶段无限膨胀、LLM 调用数爆炸。
MAX_REDUCE_SLUGS = 5000


@dataclass
class LintReport:
    total_pages: int = 0
    pages_without_source: List[str] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict:
        return {
            "total_pages": self.total_pages,
            "pages_without_source": self.pages_without_source,
            "ok": self.ok,
        }


@dataclass
class PipelineResult:
    emp_id: int
    pages: List[dict] = field(default_factory=list)      # {page_id, page_key, page_type, title, source_files}
    index_page_id: Optional[int] = None
    lint: dict = field(default_factory=dict)
    summary_page_count: int = 0
    entity_page_count: int = 0
    concept_page_count: int = 0
    failed: int = 0
    timings: dict = field(default_factory=dict)  # 各阶段耗时（秒）：MAP/TAXONOMY/REDUCE/FINALIZE/total


def _make_compiler(emp_id: int, compiler) -> WikiCompiler:
    return compiler or WikiCompiler(emp_id=emp_id)


def _lint(compiled_pages: List[CompiledPage]) -> LintReport:
    """校验来源完整性（每页至少 1 个源文件）。"""
    report = LintReport(total_pages=len(compiled_pages))
    for pg in compiled_pages:
        if not pg.source_files:
            report.pages_without_source.append(pg.page_key or pg.title)
    report.ok = not report.pages_without_source
    return report


def _count_page(result: PipelineResult, page_type: str) -> None:
    if page_type == "summary":
        result.summary_page_count += 1
    elif page_type == "entity":
        result.entity_page_count += 1
    elif page_type == "concept":
        result.concept_page_count += 1


def run_wiki_pipeline(
    emp_id: int,
    docs: List[WikiSourceDoc],
    allowed_file_ids: Optional[Set[int]] = None,
    user_id: int = 0,
    compiler=None,
    do_extract: bool = True,
    make_index: bool = True,
    persister: Callable = None,
    classifier: Optional[Callable] = None,
    folder_id: int = 0,
    linkifier: Callable = None,
) -> PipelineResult:
    """运行 Wiki 全链路生成（MAP -> TAXONOMY -> REDUCE -> FINALIZE）。

    Args:
        docs：源文档（每篇含 report_id/version/chunks）。
        allowed_file_ids：可选白名单；仅聚合其中的文档（scoped build）。
        compiler：可注入的 WikiCompiler（测试用桩）。
        do_extract：是否抽取 entity/concept 候选。
        persister：可注入的落库函数；默认 persist.persist_page。
        classifier：可选整批 taxonomy 分类器（不传则用 LLM 逐页规划）。
        链接注入：由落库阶段的 linkifier（默认 linkify_content）负责，
            FINALIZE 只做零 LLM 正则死链清理，绝不注入。
    """
    result = PipelineResult(emp_id=emp_id)
    pipeline_t0 = time.time()
    if allowed_file_ids is not None:
        docs = [d for d in docs if d.report_id in allowed_file_ids]
    if not docs:
        logger.warning("run_wiki_pipeline：无可用文档（可能为空的 allowed_file_ids）")
        return result

    comp = _make_compiler(emp_id, compiler)
    persist = persister or persist_page
    _linkify = linkifier or linkify_content

    # ---- 阶段 1：MAP（按批并发）----
    # 聚合所有文档的成稿页 + slug 候选
    all_pages: List[CompiledPage] = []
    slug_updates: List[SlugUpdate] = []
    map_results: List = []

    def _map_one(doc):
        return build_pages_for_doc(doc, comp, do_extract=do_extract)

    print(f"[MAP] 开始：{len(docs)} 篇文档，并发={MAP_PARALLEL}", flush=True)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=MAP_PARALLEL) as ex:
        futs = {ex.submit(_map_one, d): d for d in docs}
        for fut in as_completed(futs):
            doc = futs[fut]
            done += 1
            try:
                mr = fut.result()
                map_results.append(mr)
                all_pages.extend(mr.pages)
                slug_updates.extend(mr.slug_updates)
                print(f"[MAP] {done}/{len(docs)} 完成 report={doc.report_id} "
                      f"(+{len(mr.pages)}页 +{len(mr.slug_updates)}候选, 累计 {time.time()-t0:.1f}s)",
                      flush=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MAP 失败 report=%s: %s", doc.report_id, exc)
                result.failed += 1
                print(f"[MAP] {done}/{len(docs)} 失败 report={doc.report_id}: {exc}", flush=True)
    print(f"[MAP] 完成：{len(all_pages)} 成稿页 + {len(slug_updates)} slug 候选  ({time.time()-t0:.1f}s)", flush=True)
    result.timings["MAP"] = round(time.time() - t0, 3)

    # ---- 阶段 2：TAXONOMY（整批分类，先于 REDUCE）----
    t0_tax = time.time()
    if classifier is not None:
        folder_map = {p.page_key: (classifier(p) or 0) for p in all_pages}
    else:
        folder_map = taxonomy_mod.plan_batch_taxonomy(
            emp_id, [{"page_key": p.page_key, "title": p.title, "summary": p.summary,
                      "slug": p.page_key}
                     for p in all_pages],
        )
    result.timings["TAXONOMY"] = round(time.time() - t0_tax, 3)
    print(f"[TAXONOMY] 完成：{len(folder_map)} 页分类  ({result.timings['TAXONOMY']:.1f}s)", flush=True)

    # ---- 阶段 3：REDUCE（按 slug 聚合，每个 slug 一次生成/合并）----
    # 3a) summary 页：直接落库（linkify + 发布）
    # 3b) slug 候选：按 slug 聚合 -> 一次 reduce_slug -> 落库
    # 先构建 slug -> 聚合更新 映射
    agg: Dict[str, SlugUpdate] = {}
    for u in slug_updates:
        if u.slug in agg:
            base = agg[u.slug]
            base.source_files.update(u.source_files)
            if u.name and not base.name:
                base.name = u.name
            if u.description:
                # 多文档描述拼接（去重）
                if u.description not in base.description:
                    base.description = (base.description + " | " + u.description).strip(" |")
        else:
            agg[u.slug] = SlugUpdate(
                slug=u.slug, page_type=u.page_type, name=u.name,
                description=u.description, source_files=dict(u.source_files),
            )

    # 准备 REDUCE 任务列表：summary 页 + 每个 slug 的待生成页
    reduce_slug_tasks = list(agg.items())  # (slug, SlugUpdate)

    # ---- P2-1：REDUCE slug 总数硬上限，超出降级为 stub（零 LLM）----
    # 5000 篇汇报场景下，entity/concept 长尾可能产出远超正文承载能力的 slug。
    # 超出 MAX_REDUCE_SLUGS 的 slug 不再调 LLM 生成正文，直接落库一个 stub 占位页，
    # 保证页面可追溯（带源文件引用），但不消耗 LLM 配额。
    stub_pages: List[CompiledPage] = []
    if len(reduce_slug_tasks) > MAX_REDUCE_SLUGS:
        overflow = reduce_slug_tasks[MAX_REDUCE_SLUGS:]
        reduce_slug_tasks = reduce_slug_tasks[:MAX_REDUCE_SLUGS]
        for slug, upd in overflow:
            page_key = f"{upd.page_type}/{slug}"
            # 若该页在 DB 已有正文（历史/往期已生成），复用已有正文，避免被占位页覆盖
            prev_md = persist_read_body(emp_id, slug, upd.page_type)
            if prev_md:
                stub_pages.append(CompiledPage(
                    page_id=0, emp_id=emp_id,
                    title=upd.name or slug,
                    page_type=upd.page_type,
                    markdown=prev_md,
                    summary="",
                    page_key=page_key,
                    source_files=dict(upd.source_files),
                    is_stub=False,
                ))
                continue
            stub_pages.append(CompiledPage(
                page_id=0, emp_id=emp_id,
                title=upd.name or slug,
                page_type=upd.page_type,
                markdown=f"# {upd.name or slug}\n\n"
                         f"> 本页为自动生成的占位页（stub）。源文件："
                         f"{', '.join(str(s) for s in sorted(upd.source_files)) or '未知'}。\n",
                summary="",
                page_key=page_key,
                source_files=dict(upd.source_files),
                is_stub=True,
            ))
        print(f"[REDUCE] slug 总数 {len(agg)} 超出上限 {MAX_REDUCE_SLUGS}，"
              f"降级 {len(overflow)} 个为 stub（零 LLM）", flush=True)

    print(f"[REDUCE] 开始：{len(all_pages)} 直接页 + {len(reduce_slug_tasks)} slug 页，并发={REDUCE_PARALLEL}", flush=True)

    def _reduce_slug_task(item):
        slug, upd = item
        # 读 DB 已有正文做增量合并
        prev = persist_read_body(emp_id, slug, upd.page_type)
        raw = comp.reduce_slug(slug, upd.page_type, prev, [upd])
        pg = CompiledPage(
            page_id=0, emp_id=emp_id,
            title=raw.get("title") or upd.name or slug,
            page_type=upd.page_type,
            markdown=raw.get("markdown", ""),
            summary=raw.get("summary", ""),
            page_key=f"{upd.page_type}/{slug}",
            source_files=dict(upd.source_files),
            is_stub=False,
        )
        return pg

    produced: List[CompiledPage] = list(all_pages)  # summary 页先放入
    produced.extend(stub_pages)  # 超出上限的降级 stub 一并落库
    t_reduce_start = time.time()
    with ThreadPoolExecutor(max_workers=REDUCE_PARALLEL) as ex:
        futs = {ex.submit(_reduce_slug_task, it): it for it in reduce_slug_tasks}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                produced.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                slug = it[0]
                logger.warning("REDUCE slug 失败 %s: %s", slug, exc)
                result.failed += 1
    print(f"[REDUCE] 生成 {len(produced)} 页  ({time.time()-t_reduce_start:.1f}s)", flush=True)

    # ---- 阶段 3.5：落库 + 发布（幂等 persist + publishDraftPages）----
    # 注意：内部链接注入（LLM linkify）不在落库循环里逐页做（那是 N 次 LLM，极慢），
    # 也不在 FINALIZE 默认执行（同理由）。FINALIZE 只做零 LLM 的正则死链清理；
    # 如需全量重新注入内部链接，对 run_wiki_pipeline 传 do_linkify=True。
    total_pages = len(produced)
    committed = 0
    for pidx, page in enumerate(produced, 1):
        try:
            fid = folder_map.get(page.page_key, folder_id)
            pid, cc, _ = persist(emp_id, page, folder_id=fid)
            # 每页独立事务已在 persist_page 内部提交（engine.begin autocommit），
            # 此处打印进度，便于实时确认逐页落库。
            committed += 1
            _count_page(result, page.page_type)
            result.pages.append({
                "page_id": pid, "page_key": page.page_key, "page_type": page.page_type,
                "title": page.title, "source_files": dict(page.source_files),
            })
            print(f"[REDUCE] 落库 {committed}/{total_pages} 已提交 page_key={page.page_key} "
                  f"page_id={pid} type={page.page_type} (本批累计 {len(result.pages)})",
                  flush=True)
            if pidx % WIKI_MAX_DOCS_PER_BATCH == 0 or pidx == total_pages:
                try:
                    publish_draft_pages(emp_id, [p["page_id"] for p in result.pages[-WIKI_MAX_DOCS_PER_BATCH:]])
                    print(f"[REDUCE] 已发布草稿页批次 (共 {len(result.pages)} 页已落库)", flush=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("发布草稿页失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("REDUCE 落库失败 page_key=%s: %s", page.page_key, exc)
            result.failed += 1
            continue
    result.timings["REDUCE"] = round(time.time() - t_reduce_start, 3)
    print(f"[REDUCE] 落库+发布完成：{committed}/{total_pages} 页  (REDUCE 总 {result.timings['REDUCE']:.1f}s)", flush=True)

    # ---- 阶段 4：FINALIZE（死链清理 + 索引重建 + 裁剪空文件夹）----
    # 注意：FINALIZE 只做零 LLM 的正则死链清理（clean_dead_links 不调 LLM），
    # 内部链接注入由落库阶段的 linkifier 负责，FINALIZE 绝不注入。
    if make_index and result.pages:
        t0_fin = time.time()
        try:
            print(f"[FINALIZE] 清理死链 emp_id={emp_id} ...", flush=True)
            finalize_mod.clean_dead_links(emp_id)
            result.index_page_id = finalize_mod.rebuild_index_page(emp_id)
            finalize_mod.prune_empty_folders(emp_id)
            result.timings["FINALIZE"] = round(time.time() - t0_fin, 3)
            print(f"[FINALIZE] 完成：索引页={result.index_page_id}  ({result.timings['FINALIZE']:.1f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("FINALIZE 失败: %s", exc)

    report = _lint(produced)
    result.lint = report.as_dict()
    result.timings["total"] = round(time.time() - pipeline_t0, 3)
    phases = " | ".join(
        f"{k}={result.timings[k]:.1f}s"
        for k in ("MAP", "TAXONOMY", "REDUCE", "FINALIZE")
        if k in result.timings
    )
    print(f"[WIKI] 全链路完成：{phases} | total={result.timings['total']:.1f}s", flush=True)
    return result


def persist_read_body(emp_id: int, slug: str, page_type: str) -> Optional[str]:
    """读取 DB 已有同 slug 页面正文，供 REDUCE 增量合并（无则返回 None）。"""
    from .persist import read_page_body
    return read_page_body(emp_id, f"{page_type}/{slug}")


def build_wiki_incremental(
    emp_id: int,
    file_ids: List[int],
    top_k: int = 200,
    user_id: int = 0,
    compiler=None,
    do_extract: bool = True,
    make_index: bool = True,
) -> PipelineResult:
    """增量构建：逐文件从 ES 取 chunk 编译并幂等落库。

    契合「摄入一篇、构建一篇」的增量模型；重跑安全（revision+1、合并源文件）。
    缺失 chunk 的 report_id（未摄入）会被跳过并计入 failed。
    """
    from .. import es_store
    es_store.ensure_index()
    comp = compiler if isinstance(compiler, WikiCompiler) else WikiCompiler(emp_id=emp_id)
    result = PipelineResult(emp_id=emp_id)
    docs = []
    for fid in file_ids:
        try:
            chunks = es_store.hybrid_search(
                "", top_k=top_k, emp_id=emp_id,
                allowed_file_ids=[fid], extra_filters={"report_id": fid},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("incremental 检索失败 report=%s: %s", fid, exc)
            result.failed += 1
            continue
        if not chunks:
            logger.warning("incremental: report=%s 无 chunk（可能未摄入），跳过", fid)
            result.failed += 1
            continue
        docs.append(WikiSourceDoc(report_id=fid, version_id=1, file_name=f"file_{fid}", chunks=chunks))
    if docs:
        result = run_wiki_pipeline(
            emp_id, docs, compiler=comp, do_extract=do_extract, make_index=make_index,
        )
    return result


# ---------------------------------------------------------------------------
# 汇报删除 / 重解析
# ---------------------------------------------------------------------------

def delete_report_wiki(emp_id: int, report_ids: List[int]) -> dict:
    """删除/解绑指定汇报的 wiki 贡献（**不是**删库重建）。

    行为：
      - 仅把 report_ids 从这些页面的源集合中摘除；
      - 解绑后若某页仍引用其他 report -> 保留该页（保留跨汇报聚合知识）；
      - 解绑后若某页 source_refs 归零 -> 软删（status=2）+ 反链清理；
      - 随后清理死链、重建索引、裁剪空文件夹。

    语义优势：删除一篇汇报不会破坏由其他汇报贡献的 entity/concept 聚合页。
    """
    from . import persist as persist_mod
    logger.info("[DELETE] emp_id=%s report_ids=%s", emp_id, report_ids)
    result = persist_mod.reconcile_sources(emp_id, report_ids, soft_delete_empty=True)
    try:
        finalize_mod.clean_dead_links(emp_id)
        finalize_mod.rebuild_index_page(emp_id)
        finalize_mod.prune_empty_folders(emp_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DELETE] FINALIZE 失败: %s", exc)
    logger.info("[DELETE] 完成: %s", result)
    return result


def reparse_report_wiki(
    emp_id: int,
    report_ids: List[int],
    compiler=None,
    do_extract: bool = True,
    make_index: bool = True,
) -> PipelineResult:
    """重解析指定汇报。

    流程：
      1) 先按 report_ids 解绑旧贡献（源归零则软删）；
      2) 用最新 chunk 重新对这些 report 做 MAP -> REDUCE -> 落库（增量合并到已有页）；
      3) FINALIZE（死链清理 + 索引重建 + 裁剪空文件夹）。

    避免整库删除重建，保留其他汇报贡献的聚合结果。
    """
    from .. import es_store
    from . import persist as persist_mod
    es_store.ensure_index()
    comp = compiler if isinstance(compiler, WikiCompiler) else WikiCompiler(emp_id=emp_id)

    # 1) 解绑旧贡献
    persist_mod.reconcile_sources(emp_id, report_ids, soft_delete_empty=True)

    # 2) 重新拉取最新 chunk 并编译
    docs: List[WikiSourceDoc] = []
    for fid in report_ids:
        try:
            chunks = es_store.hybrid_search(
                "", top_k=200, emp_id=emp_id,
                allowed_file_ids=[fid], extra_filters={"report_id": fid},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[REPARSE] 检索失败 report=%s: %s", fid, exc)
            continue
        if chunks:
            docs.append(WikiSourceDoc(report_id=fid, version_id=1,
                                      file_name=f"file_{fid}", chunks=chunks))
    result = PipelineResult(emp_id=emp_id)
    if docs:
        result = run_wiki_pipeline(
            emp_id, docs, compiler=comp, do_extract=do_extract,
            make_index=make_index, allowed_file_ids=set(report_ids),
        )
    return result
