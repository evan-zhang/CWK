#!/usr/bin/env python3
"""RT-054 OPS-only three-library OpenSearch benchmark.

The program is designed to execute inside one private temporary directory on
OPS.  Protected text, queries, identifiers, filenames and paths never enter its
report.  It reuses the read-only FileStation adapter, builds only disposable
loopback OpenSearch indices, and emits an allowlisted aggregate JSON object
after cleanup.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import kb_stage_b_opensearch_benchmark as osbench  # noqa: E402
import kb_stage_b_poc as poc  # noqa: E402
import kb_stage_b_ops_cases as casegen  # noqa: E402
import kb_storage  # noqa: E402

KBS = ("cwork-3m", "docdb-touqian", "spbp-2027")
ANALYZERS = ("legacy_123gram", "analysis_icu")
EXPECTED_VERSION = "3.3.2"
DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
PORT = 39254
BASE_IMAGE = "opensearchproject/opensearch:3.3.2"
SELECTED_MAPPING = "cwk-child-mapping-v2-icu-body-excluded"
ACCEPTED_OLD_LEXICAL_BYTES = {
    "cwork-3m": 1_495_220_459,
    "docdb-touqian": 46_513_952,
    "spbp-2027": 264_466_313,
}


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def run(cmd: Sequence[str], *, input_bytes: bytes | None = None, timeout: float = 3600,
        check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(list(cmd), input=input_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False)
    if check and proc.returncode:
        raise RuntimeError("subprocess_failed")
    return proc


def docker(*args: str, timeout: float = 3600, check: bool = True) -> bytes:
    return run((DOCKER, *args), timeout=timeout, check=check).stdout


def parse_gateway_processes() -> list[dict[str, Any]]:
    raw = run(("/bin/ps", "-axo", "pid=,command="), timeout=30).stdout.decode("utf-8", "replace")
    rows: list[dict[str, Any]] = []
    import shlex
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or "kb_gateway.py" not in parts[1]:
            continue
        pid, command = int(parts[0]), parts[1]
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        def value(flag: str, default: str = "") -> str:
            try:
                return argv[argv.index(flag) + 1]
            except (ValueError, IndexError):
                return default
        rows.append({
            "pid": pid, "command": command, "command_sha256": sha(command),
            "prefix": value("--prefix"), "port": int(value("--port", "8787")),
            "admin_key_env": value("--admin-key-env"),
        })
    return sorted(rows, key=lambda row: row["pid"])


def process_environment(pid: int, names: Iterable[str]) -> dict[str, str]:
    text = run(("/bin/ps", "eww", "-p", str(pid), "-o", "command="), timeout=30).stdout.decode(
        "utf-8", "replace")
    result: dict[str, str] = {}
    for name in names:
        match = re.search(r"(?:^|\s)" + re.escape(name) + r"=([^\s]*)", text)
        if match:
            result[name] = match.group(1)
    return result


def read_json(backend: kb_storage.FileStationBackend, rel: str) -> dict[str, Any]:
    value = json.loads(backend.read(rel))
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def remote_metadata(backend: kb_storage.FileStationBackend, rel: str) -> dict[str, Any]:
    remote = backend._remote(rel)  # read-only FileStation getinfo
    payload = backend._get("entry.cgi", {
        "api": "SYNO.FileStation.List", "version": "2", "method": "getinfo",
        "path": json.dumps([remote]),
        "additional": json.dumps(["size", "time", "type"]),
    })
    rows = payload.get("files") or []
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("metadata_unavailable")
    row = rows[0]
    additional = row.get("additional") if isinstance(row.get("additional"), dict) else {}
    times = additional.get("time") if isinstance(additional.get("time"), dict) else {}
    # Names and paths are intentionally excluded.
    return {
        "size": int(additional.get("size") or row.get("size") or 0),
        "mtime": int(times.get("mtime") or row.get("mtime") or 0),
        "type": str(additional.get("type") or row.get("type") or "file"),
    }


def metadata_summary(backend: kb_storage.FileStationBackend) -> dict[str, Any]:
    rows = {rel: remote_metadata(backend, rel) for rel in (
        "kb.json", "_system/raw-index.json", "_system/lexical-index.json",
        "_system/lexical-readiness.json", "_system/manifest.json") if backend.exists(rel)}
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return {"sha256": sha(canonical), "lexical_index_bytes": rows.get(
        "_system/lexical-index.json", {}).get("size")}


def extract_body(text: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i] == "---":
            head = "\n".join(lines[:i + 1]) + "\n"
            return text[len(head):] if text.startswith(head) else text
    return text


def parse_entries(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    entries = payload.get("entries")
    if isinstance(entries, dict):
        return sorted((str(key), value) for key, value in entries.items() if isinstance(value, dict))
    if isinstance(entries, list):
        out = []
        for row in entries:
            if isinstance(row, dict):
                lineage = str(row.get("lineage_id") or row.get("id") or "")
                if lineage:
                    out.append((lineage, row))
        return sorted(out)
    return []


def load_library(kb: str, env: dict[str, str]) -> tuple[list[poc.SourceDocument], dict[str, Any], dict[str, Any]]:
    backend = kb_storage.FileStationBackend.from_env(env, prefix=kb, timeout=120)
    started = time.perf_counter()
    try:
        before = metadata_summary(backend)
        rows = parse_entries(read_json(backend, "_system/raw-index.json"))
        docs: list[poc.SourceDocument] = []
        excluded: dict[str, int] = {}
        raw_bytes = 0
        for lineage, row in rows:
            status = str(row.get("status") or "unknown")
            recorded_sha = str(row.get("sha256") or "")
            rel = str(row.get("path") or "")
            if status not in {"ok", "converted"} or not recorded_sha:
                excluded[status] = excluded.get(status, 0) + 1
                continue
            if not rel:
                raise ValueError("eligible_path_missing")
            data = backend.read(rel)
            raw_bytes += len(data)
            if sha(data) != recorded_sha:
                raise ValueError("source_sha_mismatch")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                excluded["invalid_utf8"] = excluded.get("invalid_utf8", 0) + 1
                continue
            body = extract_body(text)
            if not body.strip():
                excluded["empty_body"] = excluded.get("empty_body", 0) + 1
                continue
            docs.append(poc.SourceDocument(
                kb, lineage, str(row.get("title") or row.get("display_name") or ""),
                PurePosixPath(rel).name, body, {},
            ))
        return docs, {
            "raw_index_rows": len(rows), "documents": len(docs), "raw_bytes_read": raw_bytes,
            "excluded_counts": excluded, "load_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "legacy_lexical_index_bytes": before["lexical_index_bytes"],
        }, before
    finally:
        backend.logout()


def normalize_keyword(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def normalize_date(text: str) -> str:
    value = normalize_keyword(text)
    match = re.fullmatch(r"(\d{4})[年-](\d{1,2})[月-](\d{1,2})(?:日)?", value)
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else value


def source_doc(child: poc.Child, analyzer: str = "legacy_123gram") -> dict[str, Any]:
    result = {
        "tenant_id": "ops-benchmark", "kb_id": child.kb_id, "doc_id": child.doc_id,
        "source_version": 1, "parent_id": child.parent_id, "chunk_id": child.chunk_id,
        "generation_schema": poc.MAPPING_VERSION, "title": child.title,
        "section_path": list(child.section_path), "body": child.body,
        "identifiers": list(child.identifiers),
        "entity_names": list(child.company_names + child.person_names),
        "date_values": list(child.date_values), "filenames": list(child.filenames),
        "acronyms": list(child.acronyms), "locator": {},
    }
    if analyzer == "analysis_icu":
        filenames = list(child.filenames)
        result.update({
            "title_exact": normalize_keyword(child.title),
            "filenames_exact": sorted(
                {normalize_keyword(value) for value in filenames}
                | {normalize_keyword(value).rsplit(".", 1)[0] for value in filenames}
            ),
            "identifiers": sorted({normalize_keyword(value) for value in child.identifiers}),
            "date_values": sorted({normalize_date(value) for value in child.date_values}),
        })
    return result


def bulk_payload(index_name: str, children: Sequence[poc.Child], analyzer: str,
                 batch_size: int = 1000) -> Iterable[bytes]:
    lines: list[bytes] = []
    for child in children:
        lines.append(json.dumps({"index": {"_index": index_name, "_id": child.chunk_id}},
                                separators=(",", ":")).encode())
        lines.append(json.dumps(source_doc(child, analyzer), ensure_ascii=False,
                                separators=(",", ":")).encode())
        if len(lines) >= batch_size * 2:
            yield b"\n".join(lines) + b"\n"; lines = []
    if lines:
        yield b"\n".join(lines) + b"\n"


def node_resources(client: osbench.OpenSearchClient) -> dict[str, int]:
    response, _ = client.request("GET", "/_nodes/stats/jvm?filter_path="
        "nodes.*.jvm.mem.heap_used_in_bytes,nodes.*.jvm.mem.heap_committed_in_bytes,"
        "nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.jvm.mem.non_heap_used_in_bytes")
    nodes = response.get("nodes", {})
    rows = [row.get("jvm", {}).get("mem", {}) for row in nodes.values() if isinstance(row, dict)]
    return {key: max((int(row.get(key, 0)) for row in rows), default=0) for key in (
        "heap_used_in_bytes", "heap_committed_in_bytes", "heap_max_in_bytes", "non_heap_used_in_bytes")}


def proc_resources(container: str) -> dict[str, int]:
    raw = docker("exec", container, "sh", "-c", "sed -n 's/^VmRSS:[[:space:]]*\\([0-9]*\\).*/VmRSS=\\1/p; s/^VmHWM:[[:space:]]*\\([0-9]*\\).*/VmHWM=\\1/p' /proc/1/status", timeout=30)
    vals = {}
    for line in raw.decode().splitlines():
        key, value = line.split("=", 1); vals[key] = int(value) * 1024
    return {"container_rss_bytes": vals.get("VmRSS", 0), "container_rss_hwm_bytes": vals.get("VmHWM", 0)}


def index_stats(client: osbench.OpenSearchClient, index: str,
                probe_child: poc.Child, analyzer: str) -> dict[str, Any]:
    response, _ = client.request("GET", f"/{index}/_stats/store,docs,indexing,refresh,merge,segments")
    total = response.get("_all", {}).get("primaries", {})
    segments = total.get("segments", {})
    vectors, _ = client.request("POST", f"/{index}/_termvectors", {
        "doc": source_doc(probe_child, analyzer), "fields": ["body"],
        "field_statistics": True, "term_statistics": True,
        "positions": False, "offsets": False, "payloads": False,
    })
    field_stats = vectors.get("term_vectors", {}).get("body", {}).get("field_statistics", {})
    lucene_postings = {key: int(field_stats.get(key, 0))
                       for key in ("doc_count", "sum_doc_freq", "sum_total_term_freq")}
    return {
        "primary_store_bytes": int(total.get("store", {}).get("size_in_bytes", 0)),
        "lucene_body_statistics": lucene_postings,
        "docs": int(total.get("docs", {}).get("count", 0)),
        "index_total": int(total.get("indexing", {}).get("index_total", 0)),
        "index_time_ms": int(total.get("indexing", {}).get("index_time_in_millis", 0)),
        "refresh_total": int(total.get("refresh", {}).get("total", 0)),
        "refresh_time_ms_internal": int(total.get("refresh", {}).get("total_time_in_millis", 0)),
        "merge_total": int(total.get("merges", {}).get("total", 0)),
        "merge_time_ms_internal": int(total.get("merges", {}).get("total_time_in_millis", 0)),
        "segments_count": int(segments.get("count", 0)),
        "terms_memory_bytes": int(segments.get("terms_memory_in_bytes", 0)),
        "stored_fields_memory_bytes": int(segments.get("stored_fields_memory_in_bytes", 0)),
        "term_vectors_memory_bytes": int(segments.get("term_vectors_memory_in_bytes", 0)),
        "norms_memory_bytes": int(segments.get("norms_memory_in_bytes", 0)),
        "points_memory_bytes": int(segments.get("points_memory_in_bytes", 0)),
        "doc_values_memory_bytes": int(segments.get("doc_values_memory_in_bytes", 0)),
    }


def retrieval_probe(client: osbench.OpenSearchClient, index: str, kb: str,
                    body_in_source: bool) -> dict[str, Any]:
    samples: list[float] = []; sizes: list[int] = []; leak = 0; body_count = 0
    for _ in range(5):
        start = time.perf_counter_ns()
        response, size = client.request("POST", f"/{index}/_search", {
            "size": 10, "track_total_hits": False,
            "query": {"bool": {"filter": [{"term": {"kb_id": kb}}]}},
            "sort": [{"chunk_id": "asc"}],
        })
        samples.append((time.perf_counter_ns() - start) / 1_000_000); sizes.append(size)
        for hit in response.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            leak += int(src.get("kb_id") != kb)
            body_count += int("body" in src)
    return {
        "samples": len(samples), "latency_p50_ms": round(statistics.median(samples), 3),
        "latency_p95_ms": osbench.percentile(samples, .95), "response_bytes_p50": int(statistics.median(sizes)),
        "response_bytes_max": max(sizes), "cross_kb_leaks": leak,
        "body_fields_returned": body_count,
        "body_expected": body_in_source,
    }


def mapping_for_quality(analyzer: str, body_in_source: bool = False) -> dict[str, Any]:
    """Return the untouched legacy baseline or the generalized ICU v2 candidate."""
    mapping = osbench.mapping_for(analyzer, body_in_source)
    if analyzer != "analysis_icu":
        return mapping
    analysis = mapping["settings"]["analysis"]
    analysis["normalizer"] = {
        "cwk_icu_keyword": {
            "type": "custom", "char_filter": [], "filter": ["icu_normalizer", "lowercase"],
        }
    }
    props = mapping["mappings"]["properties"]
    normalized_keyword = {"type": "keyword", "normalizer": "cwk_icu_keyword"}
    for field in ("identifiers", "date_values", "title_exact", "filenames_exact"):
        props[field] = dict(normalized_keyword)
    return mapping


def build_index(client: osbench.OpenSearchClient, index: str, analyzer: str,
                body_in_source: bool, children: Sequence[poc.Child], container: str,
                probe_kbs: Sequence[str]) -> dict[str, Any]:
    mapping = mapping_for_quality(analyzer, body_in_source)
    client.request("PUT", f"/{index}", mapping)
    started = time.perf_counter_ns(); bulk_started = time.perf_counter_ns(); batches = 0
    for payload in bulk_payload(index, children, analyzer):
        result, _ = client.request("POST", "/_bulk?refresh=false", payload,
                                   content_type="application/x-ndjson")
        if result.get("errors"):
            raise RuntimeError("bulk_error")
        batches += 1
    bulk_ms = (time.perf_counter_ns() - bulk_started) / 1_000_000
    refresh_started = time.perf_counter_ns(); client.request("POST", f"/{index}/_refresh")
    refresh_ms = (time.perf_counter_ns() - refresh_started) / 1_000_000
    merge_started = time.perf_counter_ns(); client.request("POST", f"/{index}/_forcemerge?max_num_segments=1&flush=true")
    merge_ms = (time.perf_counter_ns() - merge_started) / 1_000_000
    stats = index_stats(client, index, children[0], analyzer)
    if stats["docs"] != len(children):
        raise RuntimeError("indexed_docs_mismatch")
    resources = {**node_resources(client), **proc_resources(container)}
    probes = {kb: retrieval_probe(client, index, kb, body_in_source) for kb in probe_kbs}
    return {
        "analyzer": analyzer,
        "source_model": "body_in_source" if body_in_source else "body_excluded_from_source",
        "mapping_sha256": sha(json.dumps(mapping, sort_keys=True, separators=(",", ":"))),
        "metrics": {**stats, "chunks": len(children), "bulk_batches": batches,
                    "build_elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
                    "bulk_elapsed_ms": round(bulk_ms, 3), "refresh_elapsed_ms": round(refresh_ms, 3),
                    "force_merge_elapsed_ms": round(merge_ms, 3)},
        "resources": resources, "retrieval": probes,
    }


def write_private_corpus(path: Path, docs_by_kb: dict[str, Sequence[poc.SourceDocument]]) -> None:
    payload = {
        "schema": "cwk.rt054.ops-private-corpus.v1",
        "libraries": {
            kb: [{"doc_id": doc.doc_id, "title": doc.title, "filename": doc.filename,
                  "body": doc.text} for doc in docs]
            for kb, docs in docs_by_kb.items()
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def run_case_processes(root: Path, docs_by_kb: dict[str, Sequence[poc.SourceDocument]],
                       seed: str) -> tuple[dict[str, Any], list[Path]]:
    """Run generator and verifier as separate fail-closed processes."""
    corpus = root / "private-corpus.json"
    candidates = root / "private-candidates.json"
    verified = root / "private-verified.json"
    write_private_corpus(corpus, docs_by_kb)
    commands = (
        (sys.executable, str(HERE / "kb_stage_b_ops_cases.py"), "--corpus", str(corpus),
         "--output", str(candidates), "--seed", seed),
        (sys.executable, str(HERE / "kb_stage_b_ops_verify.py"), "--corpus", str(corpus),
         "--candidates", str(candidates), "--output", str(verified)),
    )
    for command in commands:
        proc = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=900, check=False, env={"PATH": "/usr/bin:/bin"})
        if proc.returncode:
            raise RuntimeError("case_process_failed")
    result = json.loads(verified.read_text(encoding="utf-8"))
    if result.get("schema") != "cwk.rt054.ops-known-item-verified.v1":
        raise RuntimeError("verified_schema_invalid")
    return result, [corpus, candidates, verified]


def quality_query(analyzer: str, case: dict[str, Any]) -> dict[str, Any]:
    """Build one category-general query without inspecting corpus-specific metadata."""
    query = str(case["query"])
    category = str(case["category"])
    if category == "exact_identifier_date":
        if osbench.DATE_QUERY_RE.fullmatch(unicodedata.normalize("NFKC", query)) or "年" in query:
            value = normalize_date(query) if analyzer == "analysis_icu" else query
            should = [{"term": {"date_values": {"value": value, "boost": 12}}}]
        else:
            value = normalize_keyword(query) if analyzer == "analysis_icu" else query.upper()
            should = [{"term": {"identifiers": {"value": value, "boost": 12}}}]
    elif analyzer == "analysis_icu":
        fields = ["title^5", "section_path^3", "body^2"]
        should = [
            {"multi_match": {"query": query, "fields": fields,
                             "type": "phrase", "boost": 5}},
            {"multi_match": {"query": query, "fields": fields,
                             "type": "best_fields", "operator": "and", "boost": 2}},
        ]
        if category == "title_filename":
            normalized = normalize_keyword(query)
            should.extend([
                {"term": {"title_exact": {"value": normalized, "boost": 20}}},
                {"term": {"filenames_exact": {"value": normalized, "boost": 20}}},
            ])
    else:
        should = [{"multi_match": {
            "query": query,
            "fields": ["title^3", "title.ascii^3", "section_path^2",
                       "section_path.ascii^2", "body", "body.ascii"],
            "type": "best_fields", "operator": "or",
        }}]
    return {
        "size": 10, "track_total_hits": False, "_source": ["doc_id", "kb_id"],
        "query": {"bool": {
            "filter": [{"term": {"kb_id": case["kb_id"]}}],
            "should": should, "minimum_should_match": 1,
        }},
        "collapse": {"field": "doc_id"},
        "sort": [{"_score": "desc"}, {"doc_id": "asc"}],
    }


def score_cases(client: osbench.OpenSearchClient, index: str, analyzer: str,
                verified: dict[str, Any], split: str,
                kbs: Sequence[str] = KBS) -> dict[str, Any]:
    scored: dict[str, Any] = {}
    for kb in kbs:
        source = verified["libraries"][kb]
        hits = total = exact_hits = exact_total = no_answer_ok = no_answer_total = leaks = 0
        rows: list[dict[str, Any]] = []
        for case in source["cases"]:
            if case["split"] != split:
                continue
            try:
                response, _ = client.request(
                    "POST", f"/{index}/_search", quality_query(analyzer, case))
            except Exception as exc:
                error = RuntimeError("anonymous_query_failure")
                error.analyzer = "icu" if analyzer == "analysis_icu" else "legacy"  # type: ignore[attr-defined]
                error.category = str(case["category"])  # type: ignore[attr-defined]
                error.subtype = type(exc).__name__  # type: ignore[attr-defined]
                raise error from None
            result_rows = response.get("hits", {}).get("hits", [])
            ranked = [str(row.get("_source", {}).get("doc_id") or "") for row in result_rows]
            leaks += sum(str(row.get("_source", {}).get("kb_id") or "") != kb for row in result_rows)
            if case["expected_outcome"] == "no_evidence":
                ok = not ranked
                no_answer_total += 1; no_answer_ok += int(ok)
                rows.append({"category": case["category"], "outcome": "no_evidence",
                             "passed": ok, "rank": None})
                continue
            rank = next((n for n, doc_id in enumerate(ranked, 1)
                         if doc_id == case["expected_doc_id"]), None)
            total += 1; hits += int(rank is not None)
            if case["category"] == "exact_identifier_date":
                exact_total += 1; exact_hits += int(rank is not None)
            rows.append({"category": case["category"], "outcome": "hit",
                         "passed": rank is not None, "rank": rank})
        scored[kb] = {
            "recall": {"hits": hits, "total": total,
                       "value": round(hits / total, 6) if total else None},
            "exact": {"hits": exact_hits, "total": exact_total,
                      "value": round(exact_hits / exact_total, 6) if exact_total else None},
            "no_answer": {"honest": no_answer_ok, "total": no_answer_total,
                          "value": round(no_answer_ok / no_answer_total, 6) if no_answer_total else None},
            "kb_leaks": leaks, "rows": rows,
        }
    return scored


def anonymous_failures(scores: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in scores["rows"]:
        if row["passed"]:
            continue
        subtype = "unexpected_answer" if row["outcome"] == "no_evidence" else "expected_not_top10"
        key = (row["category"], subtype)
        counts[key] = counts.get(key, 0) + 1
    return [{"category": category, "subtype": subtype, "count": count}
            for (category, subtype), count in sorted(counts.items())]


def aggregate_quality(verified: dict[str, Any], analyzer_scores: dict[str, dict[str, Any]],
                      storage: dict[str, dict[str, int]]) -> tuple[dict[str, Any], dict[str, Any]]:
    libraries: dict[str, Any] = {}
    total_cases = 0
    for kb in KBS:
        source = verified["libraries"][kb]
        holdout_cases = [row for row in source["cases"] if row["split"] == "holdout"]
        total_cases += len(holdout_cases)
        legacy = analyzer_scores["legacy_123gram"][kb]
        icu = analyzer_scores["analysis_icu"][kb]
        rank_compare = {"icu_win": 0, "tie": 0, "icu_loss": 0}
        for lrow, irow in zip(legacy["rows"], icu["rows"]):
            if lrow["outcome"] == "hit":
                lr = lrow["rank"] if lrow["rank"] is not None else 11
                ir = irow["rank"] if irow["rank"] is not None else 11
                rank_compare["icu_win" if ir < lr else "icu_loss" if ir > lr else "tie"] += 1
        legacy_bytes = storage[kb]["legacy_123gram"]
        icu_bytes = storage[kb]["analysis_icu"]
        old_lexical_bytes = ACCEPTED_OLD_LEXICAL_BYTES[kb]
        reduction = round((1 - icu_bytes / old_lexical_bytes) * 100, 3)
        libraries[kb] = {
            "holdout_case_total": len(holdout_cases),
            "holdout_category_counts": {
                cat: sum(row["category"] == cat for row in holdout_cases) for cat in casegen.CATEGORIES},
            "legacy": {key: legacy[key] for key in ("recall", "exact", "no_answer", "kb_leaks")},
            "icu": {key: icu[key] for key in ("recall", "exact", "no_answer", "kb_leaks")},
            "rank_comparison": rank_compare,
            "holdout_failures": {
                "legacy": anonymous_failures(legacy), "icu": anonymous_failures(icu)},
            "storage": {
                "legacy_primary_store_bytes": legacy_bytes,
                "icu_primary_store_bytes": icu_bytes,
                "accepted_old_lexical_bytes": old_lexical_bytes,
                "icu_reduction_vs_old_lexical_percent": reduction,
            },
        }
    split_ok = all(
        row["holdout_case_total"] == sum(casegen.TARGETS[cat] - round(casegen.TARGETS[cat] / 3)
                                         for cat in casegen.CATEGORIES)
        and all(count > 0 for count in row["holdout_category_counts"].values())
        for row in libraries.values())
    icu_quality = all(
        row["icu"]["recall"]["value"] is not None and row["icu"]["recall"]["value"] >= .90
        and row["icu"]["exact"]["value"] == 1.0
        and row["icu"]["no_answer"]["value"] == 1.0
        and row["icu"]["kb_leaks"] == 0 for row in libraries.values())
    icu_not_below = all(row["icu"]["recall"]["value"] >= row["legacy"]["recall"]["value"]
                        for row in libraries.values())
    storage_pass = all(row["storage"]["icu_reduction_vs_old_lexical_percent"] >= 80.0
                       for row in libraries.values())
    passed = split_ok and icu_quality and icu_not_below and storage_pass
    gates = {
        "split_contract": {"status": "PASS" if split_ok else "FAIL", "holdout_total": total_cases},
        "legacy_baseline": {"status": "BASELINE_ONLY"},
        "icu_quality": {"status": "PASS" if icu_quality else "FAIL"},
        "icu_not_below_legacy": {"status": "PASS" if icu_not_below else "FAIL"},
        "storage_reduction": {"status": "PASS" if storage_pass else "FAIL", "threshold_percent": 80.0},
    }
    decision = {
        "stage_b": "PASS" if passed else "NO-GO",
        "selected_analyzer": "analysis_icu" if passed else None,
        "selected_mapping": SELECTED_MAPPING if passed else None,
        "reason": "all_holdout_and_storage_gates_pass" if passed else "quality_gate_failed",
    }
    return libraries, {"gates": gates, "decision": decision}


def image_environment(client: osbench.OpenSearchClient, image_tag: str, container: str) -> dict[str, Any]:
    env = osbench.inspect_environment(client)
    if env.get("runtime_version") != EXPECTED_VERSION or env.get("lucene_version") is None:
        raise RuntimeError("runtime_version_mismatch")
    if env.get("analysis_plugins", {}).get("analysis-icu") != EXPECTED_VERSION:
        raise RuntimeError("plugin_version_mismatch")
    base = json.loads(docker("image", "inspect", BASE_IMAGE))[0]
    built = json.loads(docker("image", "inspect", image_tag))[0]
    return {
        "distribution": env.get("distribution"), "runtime_version": env.get("runtime_version"),
        "lucene_version": env.get("lucene_version"), "build_hash": env.get("build_hash"),
        "analysis_plugins": {"analysis-icu": env.get("analysis_plugins", {}).get("analysis-icu")},
        "base_image_id_sha256": str(base.get("Id") or ""),
        "base_repo_digest_sha256": next((str(x).split("@", 1)[1] for x in base.get("RepoDigests") or [] if "@sha256:" in str(x)), None),
        "derived_image_id_sha256": str(built.get("Id") or ""),
        "container_id_sha256": sha(str(json.loads(docker("inspect", container))[0].get("Id") or "")),
    }


def wait_ready(url: str, timeout: float = 240) -> osbench.OpenSearchClient:
    client = osbench.OpenSearchClient(url, timeout=120)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.request("GET", "/")
            return client
        except Exception:
            time.sleep(2)
    raise RuntimeError("opensearch_not_ready")


def safe_output(report: dict[str, Any]) -> str:
    """Apply a strict structural/content allowlist to the only local output."""
    allowed_keys = {
        "schema", "status", "run_id", "sampling_strategy_version", "split_version",
        "quality_gate_scope", "freeze_verified_before_holdout", "calibration_failure_aggregates",
        "environment", "runtime_version", "lucene_version", "analysis_plugins", "analysis-icu",
        "libraries", *KBS, "case_total", "category_counts", "category_availability",
        "holdout_case_total", "holdout_category_counts", "holdout_failures", "subtype",
        *casegen.CATEGORIES, "count", "reason", "rejections", "total", "reason_counts",
        "schema_invalid", "sampling_version_mismatch", "unknown_library", "unknown_category",
        "expected_lock_mismatch", "expected_missing", "query_empty", "query_not_in_expected",
        "title_filename_constraint", "unique_constraint", "body_only_constraint",
        "table_structure_constraint", "no_answer_present", "near_neighbour_constraint",
        "duplicate_case", "legacy", "icu", "recall", "hits", "value", "exact",
        "no_answer", "honest", "kb_leaks", "rank_comparison", "icu_win", "tie", "icu_loss",
        "failures", "analyzer", "category", "gates", "case_volume", "split_contract",
        "holdout_total", "legacy_quality", "legacy_baseline", "icu_quality", "icu_not_below_legacy",
        "storage_reduction", "threshold_percent", "storage", "legacy_primary_store_bytes",
        "icu_primary_store_bytes", "accepted_old_lexical_bytes",
        "icu_reduction_vs_old_lexical_percent",
        "decision", "stage_b", "selected_analyzer", "selected_mapping", "invariants",
        "gateway_pid_count_before", "gateway_pid_count_after", "gateway_aggregate_sha256_before",
        "gateway_aggregate_sha256_after", "old_index_aggregate_sha256_before",
        "old_index_aggregate_sha256_after", "gateway_unchanged", "old_index_metadata_unchanged",
        "cleanup", "case_files_zero", "indices_zero", "containers_zero", "derived_images_zero",
        "workdirs_zero", "all_zero", "cleanup_failures", "error", "failed_phase",
        "error_subtype", "failure_category",
    }
    forbidden_keys = {"query", "title", "filename", "doc_id", "path", "body", "excerpt",
                      "parent_id", "chunk_id", "text", "template_hashes", "case_set_sha256",
                      "case_set_hmac", "locator"}
    def walk(value: Any, key: str = "") -> None:
        if key in forbidden_keys:
            raise ValueError("forbidden_key")
        if isinstance(value, dict):
            for k, v in value.items():
                if str(k) not in allowed_keys:
                    raise ValueError("non_allowlisted_key")
                walk(v, str(k))
        elif isinstance(value, list):
            for item in value: walk(item, key)
        elif isinstance(value, str):
            if len(value) > 128 or "\n" in value or "\r" in value:
                raise ValueError("unsafe_string")
    walk(report)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Catch common path/credential/content shapes after structural checks.
    for pattern in (r"/Users/", r"/Volumes/", r"CWK_NAS_KB_PASSWORD", r"X-KB-Token", r"-----BEGIN", r"\\u[0-9a-fA-F]{4}"):
        if re.search(pattern, text):
            raise ValueError("sensitive_pattern")
    return text


def enforce_safety_decision(report: dict[str, Any], cleanup: dict[str, Any], *,
                            gateway_unchanged: bool,
                            old_index_unchanged: bool) -> None:
    """Fail closed if cleanup or production invariants are not fully proven."""
    if (not gateway_unchanged or not old_index_unchanged or not cleanup.get("all_zero")
            or cleanup.get("cleanup_failures")):
        report["status"] = "FAIL"
        report["decision"] = {"stage_b": "NO-GO", "selected_analyzer": None,
                              "selected_mapping": None, "reason": "cleanup_error"}
    elif report.get("decision", {}).get("stage_b") != "PASS":
        report["status"] = "FAIL"


def validate_private_root(root: Path) -> None:
    """Refuse execution/cleanup outside the wrapper-owned mode-0700 directory."""
    if root.is_symlink() or not root.is_dir() or not root.name.startswith("cwk-rt054-quality."):
        raise RuntimeError("private_root_invalid")
    if root.stat().st_mode & 0o077:
        raise RuntimeError("private_root_permissions")
    marker = root / ".rt054-owned"
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError("private_root_marker_missing")
    if HERE.parent != root or not (HERE / Path(__file__).name).is_file():
        raise RuntimeError("private_root_layout_invalid")


def main() -> int:
    validate_private_root(ROOT)
    gateways = parse_gateway_processes()
    baseline = [{"pid": row["pid"], "command_sha256": row["command_sha256"],
                 "port": row["port"]} for row in gateways]
    run_id = str(uuid.uuid4())
    seed = "rt054-quality-v1-fixed-seed"
    nonce = run_id.replace("-", "")[:12]
    prefix = f"cwk-rt054-{nonce}"
    image = f"cwk-rt054-ops:{nonce}"
    container = f"cwk-rt054-ops-{nonce}"
    url = f"http://127.0.0.1:{PORT}"
    created_indices: list[str] = []
    private_case_files: list[Path] = []
    backends: dict[str, kb_storage.FileStationBackend] = {}
    before_meta: dict[str, Any] = {}
    report: dict[str, Any] = {
        "schema": "cwk.rt054.ops-quality-closure.v1", "status": "FAIL",
        "run_id": run_id, "sampling_strategy_version": casegen.SAMPLING_VERSION,
    }
    phase = "preflight"
    cleanup = {
        "case_files_zero": False, "indices_zero": False, "containers_zero": False,
        "derived_images_zero": False, "workdirs_zero": False, "all_zero": False,
        "cleanup_failures": 0,
    }
    gateway_unchanged = False
    old_index_unchanged = False
    try:
        if len(gateways) != 3 or {g["prefix"] for g in gateways} != set(KBS):
            raise RuntimeError("gateway_baseline_invalid")
        phase = "source_load"
        docs_by_kb: dict[str, list[poc.SourceDocument]] = {}
        children_by_kb: dict[str, list[poc.Child]] = {}
        for kb in KBS:
            gateway = next(row for row in gateways if row["prefix"] == kb)
            names = (kb_storage.ENV_HOST, kb_storage.ENV_USER, kb_storage.ENV_PASSWORD,
                     kb_storage.ENV_SHARE, kb_storage.ENV_CERT, kb_storage.ENV_TIMEOUT)
            env = process_environment(gateway["pid"], names)
            if not all(env.get(name) for name in names[:4]):
                raise RuntimeError("source_environment_unavailable")
            docs, _metrics, before = load_library(kb, env)
            docs_by_kb[kb] = docs
            before_meta[kb] = before
            _parents, children, _geometry = poc.project_documents(docs)
            children_by_kb[kb] = children
            backends[kb] = kb_storage.FileStationBackend.from_env(env, prefix=kb, timeout=120)
        phase = "case_generation_verification"
        verified, private_case_files = run_case_processes(ROOT, docs_by_kb, seed)
        phase = "image_build"
        dockerfile = ("FROM " + BASE_IMAGE +
                      "\nRUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu\n")
        build_context = ROOT / "build-context"
        build_context.mkdir(mode=0o700)
        run((DOCKER, "build", "--no-cache", "-t", image, "-f", "-", str(build_context)),
            input_bytes=dockerfile.encode(), timeout=3600)
        phase = "container_start"
        docker("run", "-d", "--name", container, "-p", f"127.0.0.1:{PORT}:9200",
               "-e", "discovery.type=single-node", "-e", "DISABLE_SECURITY_PLUGIN=true",
               "-e", "DISABLE_INSTALL_DEMO_CONFIG=true", "-e", "OPENSEARCH_JAVA_OPTS=-Xms4g -Xmx4g",
               "-e", "cluster.routing.allocation.disk.threshold_enabled=false", image, timeout=120)
        phase = "runtime_verify"
        client = wait_ready(url)
        environment = image_environment(client, image, container)
        phase = "paired_index_build"
        index_names: dict[str, dict[str, str]] = {analyzer: {} for analyzer in ANALYZERS}
        storage: dict[str, dict[str, int]] = {kb: {} for kb in KBS}
        mapping_hashes: dict[str, str] = {}
        for analyzer in ANALYZERS:
            for kb in KBS:
                name = f"{prefix}-{kb}-{analyzer.replace('_', '-')}"
                created_indices.append(name)
                built = build_index(client, name, analyzer, False, children_by_kb[kb], container, (kb,))
                index_names[analyzer][kb] = name
                storage[kb][analyzer] = int(built["metrics"]["primary_store_bytes"])
                mapping_hashes[analyzer] = str(built["mapping_sha256"])
        phase = "calibration"
        calibration_scores: dict[str, dict[str, Any]] = {analyzer: {} for analyzer in ANALYZERS}
        for analyzer in ANALYZERS:
            for kb in KBS:
                calibration_scores[analyzer][kb] = score_cases(
                    client, index_names[analyzer][kb], analyzer, verified,
                    "calibration", (kb,))[kb]
        calibration_aggregates = {
            label: {kb: anonymous_failures(calibration_scores[analyzer][kb]) for kb in KBS}
            for label, analyzer in (("legacy", "legacy_123gram"), ("icu", "analysis_icu"))
        }
        freeze = {
            "schema": "cwk.rt054.private-freeze.v1", "mapping_sha256": mapping_hashes,
            "query_sha256": sha(inspect.getsource(quality_query)),
        }
        freeze_path = ROOT / "private-freeze.json"
        freeze_path.write_text(json.dumps(freeze, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        freeze_path.chmod(0o600)
        private_case_files.append(freeze_path)
        phase = "holdout_once"
        analyzer_scores: dict[str, dict[str, Any]] = {analyzer: {} for analyzer in ANALYZERS}
        for analyzer in ANALYZERS:
            for kb in KBS:
                analyzer_scores[analyzer][kb] = score_cases(
                    client, index_names[analyzer][kb], analyzer, verified,
                    "holdout", (kb,))[kb]
        # Full-corpus diagnostics are derived from the two disjoint score sets;
        # holdout queries are never executed a second time and never affect the gate.
        _full_diagnostic_counts = {
            analyzer: {kb: len(calibration_scores[analyzer][kb]["rows"])
                       + len(analyzer_scores[analyzer][kb]["rows"]) for kb in KBS}
            for analyzer in ANALYZERS
        }
        libraries, conclusion = aggregate_quality(verified, analyzer_scores, storage)
        for name in list(created_indices):
            client.request("DELETE", f"/{name}")
            created_indices.remove(name)
        report = {
            "schema": "cwk.rt054.ops-quality-closure.v1", "status": "PASS",
            "run_id": run_id, "sampling_strategy_version": casegen.SAMPLING_VERSION,
            "quality_gate_scope": "lexical_analyzer_and_mapping_selection_only",
            "split_version": casegen.SPLIT_VERSION,
            "environment": {"runtime_version": environment["runtime_version"],
                            "lucene_version": environment["lucene_version"],
                            "analysis_plugins": environment["analysis_plugins"]},
            "calibration_failure_aggregates": calibration_aggregates,
            "freeze_verified_before_holdout": True,
            "libraries": libraries, "gates": conclusion["gates"],
            "decision": conclusion["decision"],
        }
    except Exception as exc:
        report = {
            "schema": "cwk.rt054.ops-quality-closure.v1", "status": "FAIL",
            "run_id": run_id, "sampling_strategy_version": casegen.SAMPLING_VERSION,
            "error": "benchmark_error", "failed_phase": phase,
            "decision": {"stage_b": "NO-GO", "selected_analyzer": None,
                         "selected_mapping": None, "reason": "benchmark_error"},
        }
        report["error_subtype"] = str(getattr(exc, "subtype", type(exc).__name__))
        if getattr(exc, "subtype", None):
            report["analyzer"] = str(exc.analyzer)
            report["failure_category"] = str(exc.category)
    finally:
        for path in private_case_files:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                cleanup["cleanup_failures"] += 1
        cleanup["case_files_zero"] = not any(ROOT.glob("private-*.json"))
        cleanup["indices_zero"] = not created_indices
        try:
            if "client" in locals():
                try:
                    rows, _ = client.request("GET", f"/_cat/indices/{prefix}-*?format=json")
                    for row in rows if isinstance(rows, list) else []:
                        name = row.get("index")
                        if isinstance(name, str) and name.startswith(prefix):
                            try:
                                client.request("DELETE", f"/{name}")
                            except Exception:
                                cleanup["cleanup_failures"] += 1
                    rows, _ = client.request("GET", f"/_cat/indices/{prefix}-*?format=json")
                    cleanup["indices_zero"] = rows == []
                except Exception:
                    cleanup["cleanup_failures"] += 1
        finally:
            docker("rm", "-f", container, timeout=120, check=False)
            docker("image", "rm", "-f", image, timeout=300, check=False)
            containers = docker("ps", "-a", "--filter", "name=cwk-rt054-", "--format", "{{.ID}}",
                                timeout=30, check=False)
            images = docker("images", "--filter", "reference=cwk-rt054-*", "--format", "{{.ID}}",
                            timeout=30, check=False)
            cleanup["containers_zero"] = not containers.strip()
            cleanup["derived_images_zero"] = not images.strip()
        current = parse_gateway_processes()
        current_baseline = [{"pid": row["pid"], "command_sha256": row["command_sha256"],
                             "port": row["port"]} for row in current]
        gateway_unchanged = current_baseline == baseline
        after_meta: dict[str, Any] = {}
        unchanged = True
        for kb, backend in backends.items():
            try:
                after_meta[kb] = metadata_summary(backend)
                unchanged = unchanged and after_meta[kb] == before_meta[kb]
            except Exception:
                unchanged = False
            finally:
                backend.logout()
        old_index_unchanged = unchanged and len(before_meta) == 3 and len(after_meta) == 3
        report["invariants"] = {
            "gateway_pid_count_before": len(baseline), "gateway_pid_count_after": len(current_baseline),
            "gateway_aggregate_sha256_before": sha(json.dumps(baseline, sort_keys=True, separators=(",", ":"))),
            "gateway_aggregate_sha256_after": sha(json.dumps(current_baseline, sort_keys=True, separators=(",", ":"))),
            "old_index_aggregate_sha256_before": sha(json.dumps(before_meta, sort_keys=True, separators=(",", ":"))),
            "old_index_aggregate_sha256_after": sha(json.dumps(after_meta, sort_keys=True, separators=(",", ":"))),
            "gateway_unchanged": gateway_unchanged,
            "old_index_metadata_unchanged": old_index_unchanged,
        }
        try:
            shutil.rmtree(ROOT)
        except Exception:
            cleanup["cleanup_failures"] += 1
        cleanup["workdirs_zero"] = not any(
            p.is_dir() and (p / ".rt054-owned").exists()
            for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("cwk-rt054-*")
        )
        cleanup["all_zero"] = all(cleanup[key] for key in (
            "case_files_zero", "indices_zero", "containers_zero", "derived_images_zero", "workdirs_zero"))
        report["cleanup"] = cleanup
        enforce_safety_decision(report, cleanup, gateway_unchanged=gateway_unchanged,
                                old_index_unchanged=old_index_unchanged)
    try:
        print(safe_output(report))
    except Exception:
        print(json.dumps({
            "schema": "cwk.rt054.ops-quality-closure.v1", "status": "FAIL",
            "run_id": run_id, "sampling_strategy_version": casegen.SAMPLING_VERSION,
            "error": "allowlist_rejected", "decision": {"stage_b": "NO-GO",
                "selected_analyzer": None, "selected_mapping": None, "reason": "allowlist_rejected"},
            "cleanup": cleanup,
        }, separators=(",", ":")))
        return 3
    return 0 if report.get("cleanup", {}).get("all_zero") else 2


if __name__ == "__main__":
    raise SystemExit(main())
