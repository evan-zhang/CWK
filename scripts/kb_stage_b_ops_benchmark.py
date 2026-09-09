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
import json
import os
import plistlib
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import kb_stage_b_opensearch_benchmark as osbench  # noqa: E402
import kb_stage_b_poc as poc  # noqa: E402
import kb_storage  # noqa: E402

KBS = ("cwork-3m", "docdb-touqian", "spbp-2027")
ANALYZERS = osbench.ANALYZERS
SOURCE_MODELS = osbench.SOURCE_MODELS
EXPECTED_VERSION = "3.3.2"
DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
PORT = 39254
BASE_IMAGE = "opensearchproject/opensearch:3.3.2"
OLD_LEXICAL_BYTES = {
    "cwork-3m": 1_495_220_459,
    "docdb-touqian": 46_513_952,
    "spbp-2027": 264_466_313,
}
SAFE_REASON = {
    "none", "no_ops_manual_gold", "insufficient_manual_gold", "gold_schema_unsupported",
    "gold_missing_legacy_baseline", "gold_missing_required_categories", "benchmark_error",
    "source_read_error", "opensearch_start_error", "plugin_mismatch", "cleanup_error",
    "gateway_changed", "old_index_metadata_changed", "allowlist_rejected",
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
        expected = OLD_LEXICAL_BYTES[kb]
        if before["lexical_index_bytes"] != expected:
            raise ValueError("legacy_denominator_mismatch")
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
            "legacy_lexical_index_bytes": expected,
        }, before
    finally:
        backend.logout()


def source_doc(child: poc.Child) -> dict[str, Any]:
    return {
        "tenant_id": "ops-benchmark", "kb_id": child.kb_id, "doc_id": child.doc_id,
        "source_version": 1, "parent_id": child.parent_id, "chunk_id": child.chunk_id,
        "generation_schema": poc.MAPPING_VERSION, "title": child.title,
        "section_path": list(child.section_path), "body": child.body,
        "identifiers": list(child.identifiers),
        "entity_names": list(child.company_names + child.person_names),
        "date_values": list(child.date_values), "filenames": list(child.filenames),
        "acronyms": list(child.acronyms), "locator": {},
    }


def bulk_payload(index_name: str, children: Sequence[poc.Child], batch_size: int = 1000) -> Iterable[bytes]:
    lines: list[bytes] = []
    for child in children:
        lines.append(json.dumps({"index": {"_index": index_name, "_id": child.chunk_id}},
                                separators=(",", ":")).encode())
        lines.append(json.dumps(source_doc(child), ensure_ascii=False,
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
                probe_child: poc.Child) -> dict[str, Any]:
    response, _ = client.request("GET", f"/{index}/_stats/store,docs,indexing,refresh,merge,segments")
    total = response.get("_all", {}).get("primaries", {})
    segments = total.get("segments", {})
    vectors, _ = client.request("POST", f"/{index}/_termvectors", {
        "doc": source_doc(probe_child), "fields": ["body"],
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


def build_index(client: osbench.OpenSearchClient, index: str, analyzer: str,
                body_in_source: bool, children: Sequence[poc.Child], container: str,
                probe_kbs: Sequence[str]) -> dict[str, Any]:
    mapping = osbench.mapping_for(analyzer, body_in_source)
    client.request("PUT", f"/{index}", mapping)
    started = time.perf_counter_ns(); bulk_started = time.perf_counter_ns(); batches = 0
    for payload in bulk_payload(index, children):
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
    stats = index_stats(client, index, children[0])
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


def discover_gold(paths: Sequence[str]) -> dict[str, Any]:
    # Only machine-readable, explicitly target-library cases are eligible.
    accepted = []
    reason = "no_ops_manual_gold"
    for value in paths:
        try:
            obj = json.loads(Path(value).read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = obj.get("cases") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            kb = str(row.get("kb_id") or row.get("kb") or "")
            query = row.get("query") or row.get("question") or row.get("q")
            expected = row.get("expected_doc_ids") or row.get("expected_docs") or row.get("expected_lineages")
            if kb in KBS and isinstance(query, str) and query.strip() and isinstance(expected, list) and expected:
                accepted.append(row)
    if accepted:
        reason = "gold_missing_legacy_baseline"
    categories = sorted({str(row.get("category") or row.get("query_category") or row.get("type") or "uncategorized") for row in accepted})
    return {"cases": accepted, "summary": {"case_count": len(accepted), "category_count": len(categories),
        "category_counts": {cat: sum(str(row.get("category") or row.get("query_category") or row.get("type") or "uncategorized") == cat for row in accepted) for cat in categories},
        "status": "UNKNOWN", "reason": reason}}


def image_environment(client: osbench.OpenSearchClient, image_tag: str, container: str) -> dict[str, Any]:
    env = osbench.inspect_environment(client)
    if env.get("runtime_version") != EXPECTED_VERSION or env.get("lucene_version") is None:
        raise RuntimeError("runtime_version_mismatch")
    if env.get("analysis_plugins") != {"analysis-icu": EXPECTED_VERSION, "analysis-smartcn": EXPECTED_VERSION}:
        raise RuntimeError("plugin_version_mismatch")
    base = json.loads(docker("image", "inspect", BASE_IMAGE))[0]
    built = json.loads(docker("image", "inspect", image_tag))[0]
    return {
        "distribution": env.get("distribution"), "runtime_version": env.get("runtime_version"),
        "lucene_version": env.get("lucene_version"), "build_hash": env.get("build_hash"),
        "analysis_plugins": env.get("analysis_plugins"),
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
    forbidden_keys = {"query", "title", "filename", "doc_id", "path", "body", "excerpt", "parent_id", "chunk_id", "text", "template_hashes"}
    def walk(value: Any, key: str = "") -> None:
        if key in forbidden_keys:
            raise ValueError("forbidden_key")
        if isinstance(value, dict):
            for k, v in value.items(): walk(v, str(k))
        elif isinstance(value, list):
            for item in value: walk(item, key)
        elif isinstance(value, str):
            if len(value) > 300 or "\n" in value or "\r" in value:
                raise ValueError("unsafe_string")
    walk(report)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Catch common path/credential/content shapes after structural checks.
    for pattern in (r"/Users/", r"/Volumes/", r"CWK_NAS_KB_PASSWORD", r"X-KB-Token", r"-----BEGIN", r"\\u[0-9a-fA-F]{4}"):
        if re.search(pattern, text):
            raise ValueError("sensitive_pattern")
    return text


def aggregate_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    """Keep projection counts and limits, never content-derived fingerprints."""
    return {
        "parent": dict(geometry["parent"]),
        "child": dict(geometry["child"]),
        "overlap": dict(geometry["overlap"]),
        "template_dedup": {
            "template_count": int(geometry["template_dedup"]["template_count"]),
            "removed_line_instances": int(
                geometry["template_dedup"]["removed_line_instances"]),
        },
    }


def main() -> int:
    state_path = ROOT / "state.json"
    state = json.loads(state_path.read_text())
    gateways = parse_gateway_processes()
    baseline = state.get("gateway_baseline") or []
    nonce = sha(str(time.time_ns()))[:12]
    prefix = f"cwk-rt054-{nonce}"
    image = f"cwk-rt054-ops:{nonce}"
    container = f"cwk-rt054-ops-{nonce}"
    url = f"http://127.0.0.1:{PORT}"
    created_indices: list[str] = []
    backends: dict[str, kb_storage.FileStationBackend] = {}
    before_meta: dict[str, Any] = {}
    report: dict[str, Any] = {"schema": "cwk.rt054.ops-three-library-benchmark.v1", "status": "FAIL"}
    error = "none"
    phase = "preflight"
    cleanup = {"indices_zero": False, "containers_zero": False, "derived_images_zero": False,
               "workdirs_zero": False, "gateway_unchanged": False, "old_index_metadata_unchanged": False,
               "failures": 0}
    try:
        if len(gateways) != 3 or {g["prefix"] for g in gateways} != set(KBS):
            raise RuntimeError("gateway_baseline_invalid")
        phase = "source_load"
        docs_by_kb: dict[str, list[poc.SourceDocument]] = {}
        children_by_kb: dict[str, list[poc.Child]] = {}
        geometry_by_kb: dict[str, Any] = {}
        source_metrics: dict[str, Any] = {}
        for kb in KBS:
            gateway = next(row for row in gateways if row["prefix"] == kb)
            names = (kb_storage.ENV_HOST, kb_storage.ENV_USER, kb_storage.ENV_PASSWORD,
                     kb_storage.ENV_SHARE, kb_storage.ENV_CERT, kb_storage.ENV_TIMEOUT)
            env = process_environment(gateway["pid"], names)
            if not all(env.get(name) for name in names[:4]):
                raise RuntimeError("source_environment_unavailable")
            docs, metrics, before = load_library(kb, env)
            docs_by_kb[kb] = docs; source_metrics[kb] = metrics; before_meta[kb] = before
            cleaned, parents, children = None, None, None
            parents, children, geometry = poc.project_documents(docs)
            children_by_kb[kb] = children
            geometry_by_kb[kb] = aggregate_geometry(geometry)
            source_metrics[kb].update({"parents": len(parents), "children": len(children)})
            backends[kb] = kb_storage.FileStationBackend.from_env(env, prefix=kb, timeout=120)
        phase = "image_build"
        gold = discover_gold(state.get("gold_candidates", []))
        dockerfile = ("FROM " + BASE_IMAGE + "\nRUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu" +
                      " && /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-smartcn\n")
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
        phase = "index_matrix"
        shared_runs: list[dict[str, Any]] = []
        isolated: dict[str, list[dict[str, Any]]] = {kb: [] for kb in KBS}
        all_children = [child for kb in KBS for child in children_by_kb[kb]]
        for analyzer in ANALYZERS:
            for body in (False, True):
                shared_name = f"{prefix}-shared-{analyzer.replace('_', '-')}-{'source' if body else 'nosource'}"
                created_indices.append(shared_name)
                shared_runs.append(build_index(client, shared_name, analyzer, body, all_children,
                                               container, KBS))
                client.request("DELETE", f"/{shared_name}"); created_indices.remove(shared_name)
                for kb in KBS:
                    name = f"{prefix}-{kb}-{analyzer.replace('_', '-')}-{'source' if body else 'nosource'}"
                    created_indices.append(name)
                    row = build_index(client, name, analyzer, body, children_by_kb[kb], container, (kb,))
                    isolated[kb].append(row)
                    client.request("DELETE", f"/{name}"); created_indices.remove(name)
        per_library: dict[str, Any] = {}
        for kb in KBS:
            runs = isolated[kb]
            by_key = {(row["analyzer"], row["source_model"]): row for row in runs}
            comparisons = {}
            for source_model in SOURCE_MODELS:
                comparisons[source_model] = {}
                for analyzer in ANALYZERS:
                    size = by_key[(analyzer, source_model)]["metrics"]["primary_store_bytes"]
                    comparisons[source_model][analyzer] = {
                        "primary_store_bytes": size,
                        "reduction_vs_old_lexical_percent": round((1 - size / OLD_LEXICAL_BYTES[kb]) * 100, 3),
                    }
            per_library[kb] = {"source": source_metrics[kb], "geometry": geometry_by_kb[kb],
                               "runs": runs, "comparisons": comparisons}
        leak = max(row["cross_kb_leaks"] for runrow in shared_runs for row in runrow["retrieval"].values())
        reduction_pass = all(
            per_library[kb]["comparisons"]["body_excluded_from_source"][analyzer]["reduction_vs_old_lexical_percent"] >= 80
            for kb in KBS for analyzer in ("analysis_icu", "analysis_smartcn"))
        report = {
            "schema": "cwk.rt054.ops-three-library-benchmark.v1", "status": "FAIL",
            "environment": environment, "libraries": per_library,
            "shared_physical_index_runs": shared_runs,
            "gold": gold["summary"],
            "gates": {
                "storage_reduction_all_libraries": {"status": "PASS" if reduction_pass else "FAIL", "threshold_percent": 80},
                "real_gold_recall": {"status": "UNKNOWN", "threshold": ">=0.90_and_not_below_legacy", "reason": gold["summary"]["reason"]},
                "exact_top10": {"status": "UNKNOWN", "threshold": 1.0, "reason": gold["summary"]["reason"]},
                "no_answer": {"status": "UNKNOWN", "reason": gold["summary"]["reason"]},
                "cross_kb_leak": {"status": "PASS" if leak == 0 else "FAIL", "leaks": leak, "threshold": 0},
            },
            "decision": {"stage_b": "NO-GO", "selected_analyzer": None,
                         "selected_mapping": None, "reason": "insufficient_manual_gold"},
        }
    except Exception:
        error = "benchmark_error"
        report = {"schema": "cwk.rt054.ops-three-library-benchmark.v1", "status": "FAIL",
                  "error": error, "failed_phase": phase,
                  "decision": {"stage_b": "NO-GO", "selected_analyzer": None,
                  "selected_mapping": None, "reason": error}}
    finally:
        cleanup["indices_zero"] = not created_indices
        try:
            if 'client' in locals():
                try:
                    rows, _ = client.request("GET", f"/_cat/indices/{prefix}-*?format=json")
                    for row in rows if isinstance(rows, list) else []:
                        name = row.get("index")
                        if isinstance(name, str) and name.startswith(prefix):
                            try: client.request("DELETE", f"/{name}")
                            except Exception: cleanup["failures"] += 1
                    rows, _ = client.request("GET", f"/_cat/indices/{prefix}-*?format=json")
                    cleanup["indices_zero"] = rows == []
                except Exception:
                    cleanup["failures"] += 1
        finally:
            docker("rm", "-f", container, timeout=120, check=False)
            docker("image", "rm", "-f", image, timeout=300, check=False)
            containers = docker("ps", "-a", "--filter", "name=cwk-rt054-", "--format", "{{.ID}}", timeout=30, check=False)
            images = docker("images", "--filter", "reference=cwk-rt054-*", "--format", "{{.ID}}", timeout=30, check=False)
            cleanup["containers_zero"] = not containers.strip()
            cleanup["derived_images_zero"] = not images.strip()
        current = parse_gateway_processes()
        cleanup["gateway_unchanged"] = [
            {"pid": row["pid"], "command_sha256": row["command_sha256"], "port": row["port"]} for row in current
        ] == baseline
        unchanged = True
        for kb, backend in backends.items():
            try: unchanged = unchanged and metadata_summary(backend) == before_meta[kb]
            except Exception: unchanged = False
            finally: backend.logout()
        cleanup["old_index_metadata_unchanged"] = unchanged and len(before_meta) == 3
        marker = Path.home() / ".cwk-rt054-active"
        try: marker.unlink(missing_ok=True)
        except Exception: cleanup["failures"] += 1
        try: shutil.rmtree(ROOT)
        except Exception: cleanup["failures"] += 1
        cleanup["workdirs_zero"] = not any(p.is_dir() and (p / ".rt054-owned").exists()
                                             for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("cwk-rt054-*"))
        cleanup["all_zero"] = all(cleanup[key] for key in (
            "indices_zero", "containers_zero", "derived_images_zero", "workdirs_zero"))
        report["cleanup"] = cleanup
        if not cleanup["gateway_unchanged"] or not cleanup["old_index_metadata_unchanged"] or not cleanup["all_zero"] or cleanup["failures"]:
            report["status"] = "FAIL"
            report["decision"] = {"stage_b": "NO-GO", "selected_analyzer": None,
                                  "selected_mapping": None, "reason": "cleanup_error"}
    try:
        print(safe_output(report))
    except Exception:
        print(json.dumps({"schema": "cwk.rt054.ops-three-library-benchmark.v1", "status": "FAIL",
                          "error": "allowlist_rejected", "decision": {"stage_b": "NO-GO",
                          "selected_analyzer": None, "selected_mapping": None,
                          "reason": "allowlist_rejected"}, "cleanup": cleanup}, separators=(",", ":")))
        return 3
    return 0 if report.get("cleanup", {}).get("all_zero") else 2


if __name__ == "__main__":
    raise SystemExit(main())
