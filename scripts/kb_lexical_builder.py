#!/usr/bin/env python3
"""RT-051 P3b: the lexical index builder (写面，独立于网关进程).

Usage::

    python3 scripts/kb_lexical_builder.py build --backend local --kb-root /kb
    python3 scripts/kb_lexical_builder.py build --backend nas --prefix cwork-3m --yes

One verb.  Default is a dry report (generation, counts, whether a write is
needed); ``--yes`` publishes through the ledger.  The gateway never calls
this — two-process constitution: this process writes, the gateway reads
(C06 只读红线).

Contract anchors (RT/RT-051/rt-lite.md C07):

- 资格域 = raw-index 里 status ∈ {ok, converted} 且 sha 非空的当前主 raw；
  placeholder/failed/unknown 明确 excluded 并计数，不悄悄少件。
- 每件合格 raw：读回字节并复核 SHA——复核不过 = 硬失败（build_refused），
  不是「跳过」；UTF-8 解不开 / 正文为空是显式 excluded，进清单。
- 正文 = frontmatter 之后（保守形状），否则全文——cwork 镜像件是摄取时
  已剥 envelope 的纯 markdown，边界歧义不猜。
- generation = corpus 投影 + 引擎串的确定性哈希，不含时间戳；同代已发布
  → 零写入（unchanged 逻辑投影相同无需重建）。
- 发布走账本：record_write（写后对账）→ record_changed_paths 留痕 →
  refresh_manifest（allow_new/allow_replaced 圈定本文件）。publish 前重走
  _collect 并断言 generation 与报告一致——同进程两读之间源漂移也会被拒。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_gateway import load_index  # noqa: E402  读函数复用网关的解析器
from kb_ledger import (  # noqa: E402
    CHANGED_PATHS_REL,
    NotFound,
    dumps,
    read_json,
    record_changed_paths,
    record_write,
    refresh_manifest,
    utc_now,
)
from kb_lexical import (  # noqa: E402
    ELIGIBLE_STATUSES,
    LEXICAL_INDEX_REL,
    LEXICAL_INDEX_SCHEMA,
    Chunk,
    build_index,
    chunk_body,
    chunk_id_of,
    corpus_digest,
    eligible_rows,
    engine_string,
    extract_body,
    generation_of,
    to_json_payload,
)
from kb_storage import (  # noqa: E402
    assert_no_plaintext_credential_flags,
    build_backend,
    sha256_bytes,
)

class BuildError(Exception):
    """A structured refusal; the CLI answers in JSON with kind=build_refused."""


def _collect(backend) -> Tuple[List[Tuple[str, Tuple[Chunk, ...]]], dict, list]:
    """Read + verify + extract the whole eligible corpus.

    Returns ``(docs, excluded_counts, rows)`` where rows is the eligibility
    projection (lineage, version, sha256) used for the generation hash.
    """
    entries = load_index(backend)
    rows = eligible_rows(
        (lineage, entry.version, entry.sha256, entry.status)
        for lineage, entry in entries.items()
    )
    excluded: dict = {}
    docs: List[Tuple[str, Tuple[Chunk, ...]]] = []
    for lineage, entry in sorted(entries.items()):
        if entry.status not in ELIGIBLE_STATUSES or not entry.sha256:
            key = entry.status or "unknown"
            excluded[key] = excluded.get(key, 0) + 1
            continue
        if not entry.path:
            # 合格但无 path = 索引行损坏，不能悄悄少件
            raise BuildError(f"{lineage} 状态合格但缺 path——raw-index 行损坏，拒绝建代")
        try:
            data = backend.read(entry.path)
        except NotFound:
            raise BuildError(
                f"{lineage} 的原件读不到（{entry.path}）——证据不可用，拒绝建代"
            ) from None
        if sha256_bytes(data) != entry.sha256:
            raise BuildError(
                f"{lineage} 字节与索引 SHA 不符——源在漂移，拒绝建代（先跑 refresh）"
            ) from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            excluded["invalid_utf8"] = excluded.get("invalid_utf8", 0) + 1
            continue
        body, body_start = extract_body(text)
        if not body.strip():
            excluded["empty_body"] = excluded.get("empty_body", 0) + 1
            continue
        chunks = tuple(
            Chunk(
                start_cp=c.start_cp,
                end_cp=c.end_cp,
                start_byte=c.start_byte + body_start,  # 平移到 raw 绝对坐标
                end_byte=c.end_byte + body_start,
                text=c.text,
            )
            for c in chunk_body(body)
        )
        docs.append((lineage, chunks))
    return docs, excluded, rows


def published_generation(backend) -> Optional[str]:
    try:
        return str(read_json(backend, LEXICAL_INDEX_REL).get("generation") or "")
    except NotFound:
        return None
    except (ValueError, KeyError):  # 损坏的代文件视同未发布（重建覆盖）
        return None


def build_lexical_index(backend, *, kb_code: str) -> dict:
    """Dry report: what generation the corpus hashes to, and is it published."""
    docs, excluded, rows = _collect(backend)
    gen = generation_of(rows)
    return {
        "schema": "cwk.kb.lexical.build-report.v1",
        "ok": True,
        "kb_code": kb_code,
        "generation": gen,
        "engine": engine_string(),
        "corpus_digest": corpus_digest(rows),
        "eligible_docs": len(rows),
        "indexed_docs": len(docs),
        "excluded_counts": excluded,
        "chunks": sum(len(chunks) for _, chunks in docs),
        "published_generation": published_generation(backend),
        "up_to_date": published_generation(backend) == gen,
        "at": utc_now().isoformat(),
    }


def publish(backend, *, kb_code: str, report: dict) -> dict:
    """Write the lexical index through the ledger (idempotent by generation)."""
    if report.get("up_to_date") and published_generation(backend) is not None:
        return {
            "schema": "cwk.kb.lexical.publish.v1",
            "ok": True,
            "wrote": False,
            "generation": report["generation"],
            "reason": "generation 未变，零写入",
        }
    docs, excluded, rows = _collect(backend)
    gen = generation_of(rows)
    if gen != report.get("generation"):
        # 报告与发布两读之间语料变了——同进程也一样拒，不发布半新半旧
        raise BuildError(
            f"语料在报告后已变化（{report.get('generation')[:12]}… → {gen[:12]}…），拒绝发布"
        )
    index = build_index(docs, chunk_id_of=chunk_id_of(kb_code))
    payload = {
        "schema": LEXICAL_INDEX_SCHEMA,
        "kb_code": kb_code,
        "generation": gen,
        "engine": engine_string(),
        "corpus_digest": corpus_digest(rows),
        "eligible_docs": report["eligible_docs"],
        "indexed_docs": report["indexed_docs"],
        "excluded_counts": report["excluded_counts"],
        "chunks": index.n_chunks,
        # coverage_complete：资格域内无 invalid_utf8/empty_body 排除才算完整；
        # 状态性排除（placeholder 等）本就在资格域外，不影响该口径
        "coverage_complete": len(docs) == len(rows),
        "built_at": utc_now().isoformat(),
        "index": to_json_payload(index),
    }
    record_write(backend, LEXICAL_INDEX_REL, dumps(payload))
    record_changed_paths(backend, [LEXICAL_INDEX_REL], reason="lexical-build")
    refresh_manifest(
        backend,
        kb_code=kb_code,
        allow_new=[LEXICAL_INDEX_REL],
        allow_replaced=[LEXICAL_INDEX_REL, CHANGED_PATHS_REL],
    )
    return {"schema": "cwk.kb.lexical.publish.v1", "ok": True, "wrote": True,
            "generation": gen}


def refresh_hook(backend, *, kb_code: str) -> dict:
    """RT-051 P3c: refresh 收尾钩子——增量落账后重建词法代。

    unchanged 快路径：generation 只依赖 raw-index 资格投影（lineage/
    version/sha），语料没变就不读正文、零写入。变了才走完整建代+发布
    （发布内部仍逐件复核 SHA）。失败抛 :class:`BuildError`，由调用方
    （kb_ingest.refresh_library）隔离成报告字段，不拖垮 refresh 本身。
    """
    entries = load_index(backend)
    rows = eligible_rows(
        (lineage, e.version, e.sha256, e.status)
        for lineage, e in entries.items()
    )
    gen = generation_of(rows)
    pub = published_generation(backend)
    if pub is None:
        # opt-in：从未发布过词法代的库不因 refresh 自动创建（首次走显式 build --yes）
        return {"status": "missing"}
    if pub == gen:
        return {"status": "unchanged", "generation": gen}
    report = build_lexical_index(backend, kb_code=kb_code)
    result = publish(backend, kb_code=kb_code, report=report)
    return {
        "status": "rebuilt" if result.get("wrote") else "unchanged",
        "generation": gen,
        "chunks": report["chunks"],
        "excluded_counts": report["excluded_counts"],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        parser = argparse.ArgumentParser(description="RT-051 词法索引 builder（写面）")
        parser.add_argument("verb", choices=("build",))
        parser.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
        parser.add_argument("--kb-root", help="local 后端的库根目录")
        parser.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
        parser.add_argument("--yes", action="store_true", help="确认发布；不给只出干跑报告")
        args = parser.parse_args(argv)

        backend = build_backend(args.backend, root=args.kb_root, prefix=args.prefix)
        try:
            try:
                kb_code = str(read_json(backend, "kb.json").get("kb_code") or "")
            except NotFound:
                raise BuildError("库根下没有 kb.json——不是建好的库") from None
            if not kb_code:
                raise BuildError("kb.json 缺 kb_code——不是建好的库") from None
            report = build_lexical_index(backend, kb_code=kb_code)
            result = dict(report)
            if args.yes:
                result["publish"] = publish(backend, kb_code=kb_code, report=report)
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()
        sys.stdout.write(dumps(result).decode("utf-8"))
        return 0
    except BuildError as exc:
        sys.stdout.write(dumps({
            "schema": "cwk.kb.lexical.build-report.v1", "ok": False,
            "error": {"kind": "build_refused", "message": str(exc)},
        }).decode("utf-8"))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        sys.stdout.write(dumps({
            "schema": "cwk.kb.lexical.build-report.v1", "ok": False,
            "error": {"kind": type(exc).__name__, "message": str(exc)},
        }).decode("utf-8"))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
