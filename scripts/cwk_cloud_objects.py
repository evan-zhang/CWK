#!/usr/bin/env python3
"""Merge successful DocDB sync receipts into the CWK cloud object catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_wiki_query import DEFAULT_MIRROR


SUCCESS_ACTIONS = {
    "create", "update_version", "unchanged",
    "physical_create", "physical_update", "physical_update_version", "physical_unchanged",
    "physical_chunked_create", "physical_chunked_update",
    "skip_existing",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # Machine-owned commit pointer: compact encoding keeps the catalog
            # comfortably below DocDB's reliable whole-file size boundary.
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def merge(
    mirror: Path,
    sync_manifests: list[Path],
    *,
    reset: bool = False,
    allow_large_prune: bool = False,
    max_prune_count: int = 25,
    max_prune_ratio: float = 0.05,
) -> dict[str, Any]:
    target = mirror / "wiki" / "_system" / "cloud-objects.json"
    try:
        catalog = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        catalog = {"schema_version": "cwk.cloud_objects.v1", "objects": {}}
    if reset:
        catalog = {"schema_version": "cwk.cloud_objects.v1", "objects": {}}
    objects = catalog.setdefault("objects", {})
    committed_before = set(objects)
    merged = 0
    errors: list[dict[str, str]] = []
    project_id = str(catalog.get("project_id") or "")
    root_file_id = str(catalog.get("root_file_id") or "")
    receipts: list[str] = list(catalog.get("sync_receipts") or [])
    for manifest_path in sync_manifests:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append({"manifest": str(manifest_path), "error": str(exc)})
            continue
        project_id = str(payload.get("project_id") or project_id)
        root_file_id = str(payload.get("root_file_id") or root_file_id)
        receipt_dry_run = bool(payload.get("dry_run"))
        receipts.append(str(manifest_path.resolve()))
        for row in payload.get("results") or []:
            rel = str(row.get("relative_path") or "")
            action = str(row.get("action") or "")
            file_id = str(row.get("file_id") or "")
            if not rel or action not in SUCCESS_ACTIONS or not file_id:
                continue
            local = mirror / rel
            previous = objects.get(rel) if isinstance(objects.get(rel), dict) else {}
            unverified_existing = receipt_dry_run or action == "skip_existing"
            content_sha256 = (
                str(previous.get("content_sha256") or "")
                if unverified_existing
                else str(row.get("content_sha256") or "")
            )
            # Old receipts without a checksum cannot prove that the current
            # local bytes are what reached DocDB. Keep the checksum empty and
            # require a fresh sync before the hard coverage gate can pass.
            objects[rel] = {
                "file_id": file_id,
                "content_sha256": content_sha256,
                "storage": (
                    str(row.get("storage") or "")
                    if row.get("storage")
                    else
                    str(previous.get("storage") or "")
                    if unverified_existing and previous.get("storage")
                    else ("physical" if action.startswith("physical_") else "text")
                ),
                "last_action": action,
                "folder_name": str(row.get("folder_name") or ""),
                "synced_at": str(payload.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds")),
            }
            for key in ("compression", "artifact_sha256", "parts", "size"):
                if row.get(key) not in (None, "", []):
                    objects[rel][key] = row[key]
            merged += 1
    excluded = {
        "wiki/_system/cloud-objects.json",
        "wiki/_system/search-index.json",
        "wiki/_system/search-index.json.gz",
    }
    expected = {
        path.relative_to(mirror).as_posix()
        for root_name in ("raw", "wiki")
        for path in (mirror / root_name).rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".md", ".html", ".json", ".gz", ".bin"}
        and path.relative_to(mirror).as_posix() not in excluded
    }
    pruned_paths = sorted(committed_before - expected)
    prune_ratio = len(pruned_paths) / max(1, len(committed_before))
    unsafe_prune = bool(committed_before) and (
        not expected
        or (len(pruned_paths) > max_prune_count and prune_ratio > max_prune_ratio)
    )
    if unsafe_prune and not reset and not allow_large_prune:
        raise RuntimeError(
            "refusing to publish a destructive cloud catalog prune: "
            f"committed={len(committed_before)} expected={len(expected)} "
            f"pruned={len(pruned_paths)} ratio={prune_ratio:.4f}; "
            "verify the local mirror mount or pass --allow-large-prune for an approved migration"
        )
    objects = {rel: row for rel, row in objects.items() if rel in expected}
    catalog.update(
        {
            "schema_version": "cwk.cloud_objects.v1",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "project_id": project_id,
            "root_file_id": root_file_id,
            "object_count": len(objects),
            "sync_receipts": sorted(set(receipts))[-100:],
            "objects": dict(sorted(objects.items())),
        }
    )
    try:
        index_meta = json.loads((mirror / "wiki" / "_system" / "index-meta.json").read_text(encoding="utf-8"))
        catalog["index_version"] = int(index_meta.get("index_version") or 0)
        catalog["index_sha256"] = str(index_meta.get("index_sha256") or "")
        catalog["index_file"] = str(index_meta.get("index_file") or "search-index.json.gz")
        catalog["index_files"] = [str(value) for value in (index_meta.get("index_files") or [])]
        catalog["index_artifact_sha256"] = str(index_meta.get("index_artifact_sha256") or "")
    except (OSError, ValueError, TypeError):
        pass
    atomic_json(target, catalog)
    return {
        "merged": merged,
        "object_count": len(objects),
        "pruned_count": len(pruned_paths),
        "prune_ratio": prune_ratio,
        "errors": errors,
        "path": str(target),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge CWK DocDB sync receipts into a cloud object catalog.")
    parser.add_argument("sync_manifests", nargs="+")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--output", default="")
    parser.add_argument("--reset", action="store_true", help="Rebuild the catalog only from the supplied receipts.")
    parser.add_argument(
        "--allow-large-prune", action="store_true",
        help="Allow an approved large catalog prune after separately verifying the local mirror.",
    )
    args = parser.parse_args()
    result = merge(
        Path(args.mirror_root).expanduser().resolve(),
        [Path(value).expanduser().resolve() for value in args.sync_manifests],
        reset=args.reset,
        allow_large_prune=args.allow_large_prune,
    )
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
