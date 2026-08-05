#!/usr/bin/env python3
"""Hard-gate CWK cloud coverage against the local expected artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_docdb_cloud import DocDBCloudRepository
from cwk_sync_mirror_to_docdb import (
    CLOUD_FILE_NAMES,
    DEFAULT_RETRY_QUEUE,
    get_personal_project_id,
    load_retry_paths,
)
from cwk_wiki_query import DEFAULT_MIRROR


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def live_verify(
    expected: list[str], objects: dict[str, dict[str, Any]], *, repo: DocDBCloudRepository, workers: int,
) -> tuple[list[str], list[str]]:
    verified: list[str] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cwk-live-audit-") as tmp:
        def verify(rel: str) -> str:
            row = objects[rel]
            expected_sha = str(row.get("content_sha256") or "")
            if not expected_sha:
                raise RuntimeError("missing committed checksum")
            target = Path(tmp) / "objects" / f"{row['file_id']}-{Path(rel).name}"
            repo.download_object(row, target, expected_sha, force=True)
            return rel

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(verify, rel): rel for rel in expected}
            for future in as_completed(futures):
                rel = futures[future]
                try:
                    verified.append(future.result())
                except Exception as exc:
                    errors.append(f"{rel}: {exc}")
    return sorted(verified), errors


def cloud_relative_path(local_rel: str) -> str:
    path = Path(local_rel)
    return (path.parent / CLOUD_FILE_NAMES.get(local_rel, path.name)).as_posix()


def audit(
    mirror: Path,
    *,
    prefixes: tuple[str, ...],
    live: bool,
    sender_id: str,
    account_id: str,
    live_workers: int,
    retry_queue: Path | None = None,
) -> dict[str, Any]:
    catalog_path = mirror / "wiki" / "_system" / "cloud-objects.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {"schema_version": "cwk.cloud_coverage_audit.v1", "overall_pass": False, "error": str(exc)}
    objects = catalog.get("objects") or {}
    expected = sorted(
        path.relative_to(mirror).as_posix()
        for path in mirror.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".md", ".html", ".json", ".gz", ".bin"}
        and any(path.relative_to(mirror).as_posix().startswith(prefix) for prefix in prefixes)
        and path != catalog_path
        and path.relative_to(mirror).as_posix() not in {
            "wiki/_system/search-index.json",
            "wiki/_system/search-index.json.gz",
        }
    )
    missing = [rel for rel in expected if rel not in objects or not objects[rel].get("file_id")]
    extra = sorted(
        rel for rel in objects
        if any(str(rel).startswith(prefix) for prefix in prefixes) and rel not in set(expected)
    )
    unhashed = [rel for rel in expected if rel in objects and not objects[rel].get("content_sha256")]
    mismatched = [
        rel for rel in expected
        if rel in objects
        and objects[rel].get("content_sha256")
        and objects[rel]["content_sha256"] != sha256_file(mirror / rel)
    ]
    live_errors: list[str] = []
    live_verified = 0
    remote_missing: list[str] = []
    remote_extra: list[str] = []
    retained_index_versions: list[str] = []
    retained_legacy_objects: list[str] = []
    remote_duplicates: list[str] = []
    catalog_project_id = str(catalog.get("project_id") or "")
    catalog_root_file_id = str(catalog.get("root_file_id") or "")
    personal_project_id = ""
    private_target_verified = False
    target_errors: list[str] = []
    repo: DocDBCloudRepository | None = None
    if live:
        try:
            if not catalog_project_id or not catalog_root_file_id:
                raise RuntimeError("catalog does not identify its DocDB project/root")
            repo = DocDBCloudRepository(
                sender_id=sender_id,
                account_id=account_id,
                project_id=catalog_project_id,
                root_file_id=catalog_root_file_id,
                cache_root=Path(tempfile.mkdtemp(prefix="cwk-live-repo-")),
            )
            personal_project_id = get_personal_project_id(repo.env)
            private_target_verified = (
                str(repo.project_id) == personal_project_id == catalog_project_id
                and str(repo.root_file_id) == catalog_root_file_id
            )
            if not private_target_verified:
                raise RuntimeError("cloud target is not the authenticated user's personal/private DocDB project")
        except Exception as exc:
            target_errors.append(str(exc))
    retry_queue_path = (retry_queue or DEFAULT_RETRY_QUEUE).expanduser().resolve()
    retry_queue_pending = sorted(load_retry_paths(retry_queue_path))
    if live and repo is not None and private_target_verified and not missing and not unhashed:
        verified, live_errors = live_verify(
            expected, objects, repo=repo, workers=live_workers,
        )
        live_verified = len(verified)
        # DocDB can browse only from a top-level root (``raw`` / ``wiki``),
        # while this audit also accepts nested scopes such as
        # ``wiki/sources/``. Browse the required roots, then filter the
        # returned tree to the requested prefixes locally.
        normalized_prefixes = tuple(prefix.rstrip("/") for prefix in prefixes)
        browse_roots = tuple(sorted({prefix.split("/", 1)[0] for prefix in normalized_prefixes if prefix}))
        remote_rows = repo.list_tree(prefixes=browse_roots, max_workers=live_workers)
        all_remote_paths = [row["relative_path"] for row in remote_rows]
        remote_paths = [
            row["relative_path"]
            for row in remote_rows
            if any(row["relative_path"].startswith(prefix) for prefix in normalized_prefixes)
        ]
        counts: dict[str, int] = {}
        for rel in remote_paths:
            counts[rel] = counts.get(rel, 0) + 1
        remote_duplicates = sorted(rel for rel, count in counts.items() if count > 1)
        expected_remote: set[str] = set()
        for rel in expected:
            row = objects.get(rel) or {}
            parts = [value for value in (row.get("parts") or []) if isinstance(value, dict)]
            if parts:
                expected_remote.update(str(part.get("remote_relative_path") or "") for part in parts)
                retained_legacy_objects.append(cloud_relative_path(rel))
            else:
                expected_remote.add(cloud_relative_path(rel))
        expected_remote.discard("")
        # The catalog itself is the cloud commit pointer and is deliberately
        # excluded from its own object map.
        if any(prefix.startswith("wiki/") for prefix in prefixes):
            expected_remote.add("wiki/_system/cwk-cloud-objects.json")
            # The catalog is the commit pointer for every wiki sub-scope,
            # including a nested audit such as ``wiki/sources/``.
            if "wiki/_system/cwk-cloud-objects.json" in all_remote_paths:
                remote_paths.append("wiki/_system/cwk-cloud-objects.json")
        actual_remote = set(remote_paths)
        remote_missing = sorted(expected_remote - actual_remote)
        raw_extra = sorted(actual_remote - expected_remote)
        # Older immutable index generations remain referenced by prior
        # catalog versions in DocDB history. Report them explicitly but do not
        # treat them as active-tree corruption.
        retained_index_versions = [
            rel for rel in raw_extra
            if rel.startswith("wiki/_system/") and Path(rel).name.startswith("search-index") and rel.endswith(".bin")
        ]
        retained_legacy_objects = sorted(set(raw_extra) & set(retained_legacy_objects))
        remote_extra = sorted(set(raw_extra) - set(retained_index_versions) - set(retained_legacy_objects))
    overall = live and private_target_verified and not target_errors and not retry_queue_pending and (
        not missing and not extra and not unhashed and not mismatched and not live_errors
        and live_verified == len(expected)
        and not remote_missing and not remote_extra and not remote_duplicates
    )
    live_failed_paths = [value.split(": ", 1)[0] for value in live_errors]
    return {
        "schema_version": "cwk.cloud_coverage_audit.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "prefixes": list(prefixes),
        "catalog_object_count": len(objects),
        "expected_count": len(expected),
        "mapped_count": len(expected) - len(missing),
        "hash_match_count": len(expected) - len(missing) - len(unhashed) - len(mismatched),
        "live_requested": live,
        "project_id": catalog_project_id,
        "root_file_id": catalog_root_file_id,
        "personal_project_id": personal_project_id,
        "private_target_verified": private_target_verified,
        "target_errors": target_errors,
        "retry_queue": str(retry_queue_path),
        "retry_queue_pending": retry_queue_pending,
        "live_verified_count": live_verified,
        "missing": missing,
        "extra": extra,
        "missing_checksums": unhashed,
        "hash_mismatches": mismatched,
        "live_errors": live_errors,
        "remote_missing": remote_missing,
        "remote_extra": remote_extra,
        "remote_duplicates": remote_duplicates,
        "retained_index_versions": retained_index_versions,
        "retained_legacy_objects": sorted(retained_legacy_objects),
        # Compatible with cwk_sync_mirror_to_docdb.py --paths-manifest so a
        # failed exact-byte audit can be repaired without hand-copying paths.
        "changed_relative_paths": sorted(set(missing + unhashed + mismatched + live_failed_paths)),
        "overall_pass": overall,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit CWK cloud coverage and optionally verify objects live.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--prefix", action="append", default=[])
    parser.set_defaults(live=True)
    parser.add_argument("--live", dest="live", action="store_true", help="Verify every active cloud object (default).")
    parser.add_argument(
        "--no-live", dest="live", action="store_false",
        help="Diagnostic catalog-only audit; always returns a failing hard-gate result.",
    )
    parser.add_argument("--sender-id", default=os.environ.get("CWK_SENDER_ID", ""))
    parser.add_argument("--account-id", default=os.environ.get("CWK_ACCOUNT_ID", "default"))
    parser.add_argument("--live-workers", type=int, default=8)
    parser.add_argument("--retry-queue", default=str(DEFAULT_RETRY_QUEUE))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = audit(
        Path(args.mirror_root).expanduser().resolve(),
        prefixes=tuple(args.prefix or ["wiki/"]),
        live=args.live,
        sender_id=args.sender_id,
        account_id=args.account_id,
        live_workers=args.live_workers,
        retry_queue=Path(args.retry_queue),
    )
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0 if result.get("overall_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
