#!/usr/bin/env python3
"""Build a deterministic, versioned search index for CWK Wiki retrieval.

The index is a derived artifact.  It contains navigation metadata and term
statistics, but no raw report bodies.  Rebuilding an unchanged corpus keeps
the same monotonic ``index_version`` and checksum.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_wiki_query import DEFAULT_MIRROR, load_navigation, load_summaries, load_summary_quality, tokenize
import cwk_entity_catalog as entity_catalog


SCHEMA = "cwk.search_index.v1"
INDEX_CHUNK_BYTES = 1_500_000


def atomic_json(path: Path, payload: dict[str, Any], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with open(name, "wb") as raw_handle:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as handle:
                handle.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_index_chunks(system: Path, compressed_path: Path, index_version: int) -> list[str]:
    data = compressed_path.read_bytes()
    names: list[str] = []
    staged: list[tuple[Path, Path]] = []
    for offset in range(0, len(data), INDEX_CHUNK_BYTES):
        name = f"search-index-v{index_version:06d}-{len(names):03d}.bin"
        target = system / name
        temp = system / f".{name}.{os.getpid()}.tmp"
        temp.write_bytes(data[offset : offset + INDEX_CHUNK_BYTES])
        staged.append((temp, target))
        names.append(name)
    # Publish the complete new generation before pruning the old one. The
    # cloud catalog is committed later and therefore remains the sole reader
    # pointer to one complete generation.
    for temp, target in staged:
        os.replace(temp, target)
    keep = set(names)
    for stale in system.glob("search-index-*.bin"):
        if stale.name not in keep:
            stale.unlink()
    return names


def relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def token_stats(text: str) -> tuple[dict[str, int], int]:
    counts = Counter(tokenize(text))
    return dict(sorted(counts.items())), sum(counts.values())


def canonical_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def build_core(mirror: Path) -> dict[str, Any]:
    summaries = load_summaries(mirror)
    navigation = load_navigation(mirror)
    summary_quality, quality_counts = load_summary_quality(mirror)
    summary_rows: list[dict[str, Any]] = []
    nav_rows: list[dict[str, Any]] = []
    summary_df: Counter[str] = Counter()
    nav_df: Counter[str] = Counter()

    for doc in summaries:
        counts, length = token_stats(doc.search_text)
        summary_df.update(counts.keys())
        summary_rows.append(
            {
                "report_id": doc.report_id,
                "title": doc.title,
                "writer": doc.writer,
                "date": doc.date,
                "source_lane": doc.source_lane,
                "summary_path": relative(doc.summary_path, mirror),
                "raw_path": relative(doc.raw_path, mirror),
                "raw_sha256": doc.raw_sha256 or (sha256_file(doc.raw_path) if doc.raw_path.is_file() else ""),
                "evidence_quotes": doc.evidence_quotes,
                "summary_quality": summary_quality.get(doc.report_id, "unknown"),
                "term_counts": counts,
                "length": length,
            }
        )

    for doc in navigation:
        counts, length = token_stats(doc.search_text)
        nav_df.update(counts.keys())
        nav_rows.append(
            {
                "kind": doc.kind,
                "title": doc.title,
                "path": relative(doc.path, mirror),
                "report_ids": doc.report_ids,
                "term_counts": counts,
                "length": length,
            }
        )

    return {
        "schema_version": SCHEMA,
        # Keep the complete deterministic term set. Cloud transport is already
        # versioned and chunked, so retrieval fidelity no longer needs to be
        # traded for a single-file size limit.
        "serialization": "compact-json-v3-full-terms",
        "summary_docs": summary_rows,
        "navigation_docs": nav_rows,
        "statistics": {
            "summary_document_frequency": dict(sorted(summary_df.items())),
            "navigation_document_frequency": dict(sorted(nav_df.items())),
            "summary_average_length": sum(row["length"] for row in summary_rows) / max(1, len(summary_rows)),
            "navigation_average_length": sum(row["length"] for row in nav_rows) / max(1, len(nav_rows)),
            "summary_quality": quality_counts,
        },
    }


def build_index(mirror: Path, *, force: bool = False) -> dict[str, Any]:
    system = mirror / "wiki" / "_system"
    index_path = system / "search-index.json"
    compressed_path = system / "search-index.json.gz"
    meta_path = system / "index-meta.json"
    manifest_path = system / "manifest.json"
    # Build the entity catalog first and bind its hash into the index
    # payload so cwk_wiki_query can reject any (index, catalog) pair that
    # was not produced in the same build.  Scanning raw here reuses the
    # anchor cache for warm-path speed.
    catalog_payload, anchor_cache, registry_source = entity_catalog.build_catalog(
        mirror, scan_raw=True, use_cache=True
    )
    entity_catalog.write_catalog(mirror, catalog_payload, anchor_cache)
    core = build_core(mirror)
    core["entity_catalog_sha256"] = catalog_payload["catalog_sha256"]
    core["entity_catalog_schema"] = catalog_payload["schema_version"]
    core["entity_catalog_registry_version"] = catalog_payload["registry"]["version"]
    index_sha256 = canonical_hash(core)
    old_meta: dict[str, Any] = {}
    try:
        old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    old_parts = [str(value) for value in (old_meta.get("index_files") or [])]
    parts_ready = bool(old_parts) and all((system / name).is_file() for name in old_parts)
    changed = force or old_meta.get("index_sha256") != index_sha256 or not compressed_path.exists() or not parts_ready
    old_version = int(old_meta.get("index_version") or 0)
    index_version = old_version + 1 if changed else max(1, old_version)
    generated_at = (
        datetime.now().astimezone().isoformat(timespec="seconds")
        if changed
        else str(old_meta.get("generated_at") or "")
    )
    payload = {
        **core,
        "index_version": index_version,
        "index_sha256": index_sha256,
        "generated_at": generated_at,
    }
    meta = {
        "schema_version": "cwk.search_index_meta.v1",
        "generated_at": generated_at,
        "index_version": index_version,
        "index_sha256": index_sha256,
        "summary_count": len(core["summary_docs"]),
        "navigation_count": len(core["navigation_docs"]),
        "entity_catalog_sha256": catalog_payload["catalog_sha256"],
        "entity_catalog_schema": catalog_payload["schema_version"],
        "entity_catalog_registry_version": catalog_payload["registry"]["version"],
        "entity_catalog_registry_source": str(registry_source),
        "entity_catalog_families_total": catalog_payload["statistics"]["families_total"],
        "changed": changed,
        "changed_relative_paths": [],
    }
    if changed:
        atomic_json(index_path, payload, compact=True)
        atomic_gzip_json(compressed_path, payload)
        index_files = write_index_chunks(system, compressed_path, index_version)
        meta["index_file"] = compressed_path.name
        meta["index_files"] = index_files
        meta["index_artifact_sha256"] = sha256_file(compressed_path)
        meta["compressed_size_bytes"] = compressed_path.stat().st_size
        # RT-010 follow-up (independent audit blocker E): the entity
        # catalog and its anchor cache are LOCAL-ONLY derived artifacts.
        # The manifest / index already carry ``entity_catalog_sha256``,
        # ``entity_catalog_schema`` and ``entity_catalog_registry_
        # version`` so the cloud side can detect drift, but the payload
        # files themselves must never enter the DocDB sync surface — a
        # gz binary going through the UTF-8 sync path corrupts and the
        # anchor cache leaks per-machine build state.  Callers that
        # need the local artifact rebuild it deterministically from the
        # sync'd summaries + registry via ``cwk_entity_catalog``.
        meta["changed_relative_paths"] = [
            *(f"wiki/_system/{name}" for name in index_files),
            "wiki/_system/index-meta.json",
            "wiki/_system/manifest.json",
        ]
        atomic_json(meta_path, meta)
    else:
        meta["index_file"] = str(old_meta.get("index_file") or compressed_path.name)
        meta["index_files"] = old_parts
        meta["index_artifact_sha256"] = str(old_meta.get("index_artifact_sha256") or sha256_file(compressed_path))
        meta["compressed_size_bytes"] = int(old_meta.get("compressed_size_bytes") or compressed_path.stat().st_size)
        meta["changed_relative_paths"] = ["wiki/_system/manifest.json"]

    manifest: dict[str, Any] = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    manifest.update(
        {
            "search_index_schema": SCHEMA,
            "search_index_version": index_version,
            "search_index_sha256": index_sha256,
            "search_index_file": compressed_path.name,
            "search_index_files": meta.get("index_files") or [],
            "search_index_artifact_sha256": meta.get("index_artifact_sha256") or "",
            "search_index_summary_count": len(core["summary_docs"]),
            "search_index_navigation_count": len(core["navigation_docs"]),
            "entity_catalog_schema": catalog_payload["schema_version"],
            "entity_catalog_sha256": catalog_payload["catalog_sha256"],
            "entity_catalog_registry_version": catalog_payload["registry"]["version"],
            "entity_catalog_families_total": catalog_payload["statistics"]["families_total"],
        }
    )
    atomic_json(manifest_path, manifest)
    return meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the versioned CWK Wiki search index.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    result = build_index(mirror, force=args.force)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
