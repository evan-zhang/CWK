#!/usr/bin/env python3
"""Restore CWK artifacts into an empty directory using only DocDB objects."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cwk_docdb_cloud import DocDBCloudRepository


def restore(
    repo: DocDBCloudRepository,
    destination: Path,
    *,
    prefixes: tuple[str, ...],
    report_ids: set[str],
    min_index_version: int,
    max_parallel: int,
) -> dict[str, Any]:
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"restore destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    _, catalog = repo.bootstrap(min_index_version=min_index_version)
    selected: list[tuple[str, dict[str, Any]]] = []
    for rel, row in sorted((catalog.get("objects") or {}).items()):
        if prefixes and not any(rel.startswith(prefix) for prefix in prefixes):
            continue
        if report_ids and not any(report_id in Path(rel).name for report_id in report_ids):
            continue
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise RuntimeError(f"unsafe catalog path: {rel}")
        selected.append((rel, row))
    restored: list[str] = []
    failures: list[dict[str, str]] = []

    def restore_one(rel: str, row: dict[str, Any]) -> str:
        file_id = str(row.get("file_id") or "")
        expected_sha = str(row.get("content_sha256") or "")
        if not file_id or not expected_sha:
            raise RuntimeError("catalog object is missing file_id or content_sha256")
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        repo.download_object(row, target, expected_sha, force=True)
        return rel

    with ThreadPoolExecutor(max_workers=max(1, max_parallel), thread_name_prefix="cwk-restore") as pool:
        futures = {pool.submit(restore_one, rel, row): rel for rel, row in selected}
        for future in as_completed(futures):
            rel = futures[future]
            try:
                restored.append(future.result())
            except Exception as exc:
                failures.append({"relative_path": rel, "error": str(exc)[:500]})

    reconstructed_index = ""
    index_parts = [str(value) for value in (catalog.get("index_files") or [])]
    parts_selected = bool(index_parts) and all(f"wiki/_system/{name}" in set(restored) for name in index_parts)
    if parts_selected and not failures:
        try:
            target = destination / "wiki" / "_system" / str(catalog.get("index_file") or "search-index.json.gz")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.assembling")
            digest = hashlib.sha256()
            with temp.open("wb") as output:
                for name in index_parts:
                    with (destination / "wiki" / "_system" / name).open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(chunk)
                            digest.update(chunk)
            expected_artifact = str(catalog.get("index_artifact_sha256") or "")
            if not expected_artifact or digest.hexdigest() != expected_artifact:
                temp.unlink(missing_ok=True)
                raise RuntimeError("reconstructed search index checksum mismatch")
            os.replace(temp, target)
            reconstructed_index = target.relative_to(destination).as_posix()
        except Exception as exc:
            failures.append({"relative_path": "wiki/_system/search-index.json.gz", "error": str(exc)[:500]})
    expected_set = {rel for rel, _ in selected}
    restored_set = set(restored)
    missing_after_restore = sorted(expected_set - restored_set)
    unexpected_after_restore = sorted(restored_set - expected_set)
    return {
        "schema_version": "cwk.cloud_restore.v1",
        "index_version": catalog.get("index_version"),
        "selected_count": len(selected),
        "restored_count": len(restored),
        "failure_count": len(failures),
        "restored_relative_paths": sorted(restored),
        "missing_after_restore": missing_after_restore,
        "unexpected_after_restore": unexpected_after_restore,
        "reconstructed_index": reconstructed_index,
        "failures": failures,
        "overall_pass": bool(selected) and not failures and not missing_after_restore and not unexpected_after_restore,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore CWK artifacts from DocDB into an empty directory.")
    parser.add_argument("destination")
    parser.add_argument("--prefix", action="append", default=[])
    parser.add_argument("--report-id", action="append", default=[])
    parser.add_argument("--min-index-version", type=int, default=0)
    parser.add_argument("--max-parallel", type=int, default=8)
    parser.add_argument("--sender-id", default=os.environ.get("CWK_SENDER_ID", ""))
    parser.add_argument("--account-id", default=os.environ.get("CWK_ACCOUNT_ID", "default"))
    parser.add_argument("--project-id", default=os.environ.get("CWK_DOCDB_PROJECT_ID", ""))
    parser.add_argument("--root-file-id", default=os.environ.get("CWK_DOCDB_ROOT_FILE_ID", ""))
    parser.add_argument("--cache-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.max_parallel < 1 or args.max_parallel > 16:
        raise SystemExit("--max-parallel must be between 1 and 16")
    repo = DocDBCloudRepository(
        sender_id=args.sender_id,
        account_id=args.account_id,
        project_id=args.project_id,
        root_file_id=args.root_file_id,
        cache_root=Path(args.cache_root).expanduser().resolve() if args.cache_root else None,
    )
    result = restore(
        repo,
        Path(args.destination).expanduser().resolve(),
        prefixes=tuple(args.prefix),
        report_ids=set(args.report_id),
        min_index_version=args.min_index_version,
        max_parallel=args.max_parallel,
    )
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0 if result["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
