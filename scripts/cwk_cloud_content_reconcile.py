#!/usr/bin/env python3
"""Verify existing DocDB text objects and backfill trusted content hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from cwk_cloud_objects import atomic_json
from cwk_docdb_cloud import DocDBCloudRepository
from cwk_wiki_query import DEFAULT_MIRROR


def reconcile(
    mirror: Path,
    *,
    prefixes: tuple[str, ...],
    sender_id: str,
    account_id: str,
    batch_size: int,
) -> dict:
    path = mirror / "wiki" / "_system" / "cloud-objects.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    objects = catalog.get("objects") or {}
    selected = [
        (rel, row) for rel, row in sorted(objects.items())
        if any(rel.startswith(prefix) for prefix in prefixes)
        and row.get("file_id") and not row.get("content_sha256")
        and (mirror / rel).is_file()
    ]
    verified: list[str] = []
    mismatched: list[str] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cwk-reconcile-") as tmp:
        repo = DocDBCloudRepository(sender_id=sender_id, account_id=account_id, cache_root=Path(tmp))

        def verify(item: tuple[str, dict]) -> tuple[str, str, str]:
            rel, row = item
            target = Path(tmp) / "objects" / f"{row['file_id']}-{Path(rel).name}"
            repo.download(str(row["file_id"]), target, force=True)
            remote_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            local_hash = hashlib.sha256((mirror / rel).read_bytes()).hexdigest()
            return rel, remote_hash, local_hash

        with ThreadPoolExecutor(max_workers=max(1, batch_size)) as pool:
            futures = {pool.submit(verify, item): item for item in selected}
            for future in as_completed(futures):
                rel, row = futures[future]
                try:
                    _, remote_hash, local_hash = future.result()
                    if remote_hash == local_hash:
                        row["content_sha256"] = local_hash
                        row["verified_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                        verified.append(rel)
                    else:
                        mismatched.append(rel)
                except Exception as exc:
                    errors.append(f"{rel}: {exc}")
    catalog["objects"] = objects
    catalog["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_json(path, catalog)
    return {
        "schema_version": "cwk.cloud_content_reconcile.v1",
        "selected_count": len(selected),
        "verified_count": len(verified),
        "mismatch_count": len(mismatched),
        "error_count": len(errors),
        "verified_relative_paths": verified,
        "mismatched_relative_paths": mismatched,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify existing DocDB text and backfill trustworthy hashes.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--prefix", action="append", default=[])
    parser.add_argument("--sender-id", default=os.environ.get("CWK_SENDER_ID", ""))
    parser.add_argument("--account-id", default=os.environ.get("CWK_ACCOUNT_ID", "default"))
    parser.add_argument("--batch-size", type=int, default=8, help="Maximum parallel download verifications.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = reconcile(
        Path(args.mirror_root).expanduser().resolve(),
        prefixes=tuple(args.prefix or ["wiki/"]),
        sender_id=args.sender_id,
        account_id=args.account_id,
        batch_size=args.batch_size,
    )
    target = Path(args.output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ["selected_count", "verified_count", "mismatch_count", "error_count"]}, ensure_ascii=False))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
