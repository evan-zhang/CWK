#!/usr/bin/env python3
"""Minimal DocDB repository adapter for CWK cloud-first query and restore."""

from __future__ import annotations

import hashlib
import fcntl
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from cwk_sync_mirror_to_docdb import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_ROOT_FILE_ID,
    DEFAULT_SENDER_ID,
    DOCDB,
    resolve_app_key,
    resolve_docdb_target,
    sanitize_error,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_json(args: list[str], env: dict[str, str], retries: int = 4) -> dict[str, Any]:
    last_error = "DocDB command failed"
    for attempt in range(max(1, retries)):
        proc = subprocess.run(args, cwd=str(DOCDB), env=env, text=True, capture_output=True)
        lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
        payload: dict[str, Any] = {}
        if lines:
            try:
                payload = json.loads(lines[-1])
            except json.JSONDecodeError:
                payload = {}
        if proc.returncode == 0 and payload.get("resultCode") == 1:
            return payload
        last_error = sanitize_error(
            payload.get("resultMsg") or proc.stderr.strip() or proc.stdout.strip() or "DocDB command failed"
        )
        transient = any(token in last_error.lower() for token in (
            "频繁", "繁忙", "timeout", "timed out", "temporarily", "connection reset", "429", "401",
        ))
        if not transient or attempt >= retries - 1:
            break
        time.sleep(min(8, 1 * (2 ** attempt)))
    raise RuntimeError(last_error)


def nested_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("fileId", "file_id", "name", "fileName")):
            rows.append(value)
        for item in value.values():
            rows.extend(nested_rows(item))
    elif isinstance(value, list):
        for item in value:
            rows.extend(nested_rows(item))
    return rows


class DocDBCloudRepository:
    def __init__(
        self,
        *,
        sender_id: str = DEFAULT_SENDER_ID,
        account_id: str = DEFAULT_ACCOUNT_ID,
        project_id: str = DEFAULT_PROJECT_ID,
        root_file_id: str = DEFAULT_ROOT_FILE_ID,
        cache_root: Path | None = None,
    ) -> None:
        key = resolve_app_key(sender_id, account_id)
        self.env = os.environ.copy()
        self.env["XG_BIZ_API_KEY"] = key
        self.env["XG_APP_KEY"] = key
        self.project_id, self.root_file_id = resolve_docdb_target(project_id, root_file_id, self.env, False)
        self.cache_root = (cache_root or Path(tempfile.gettempdir()) / "cwk-cloud-cache").resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def search_exact(self, file_name: str) -> str:
        payload = command_json(
            [
                sys.executable,
                str(DOCDB / "scripts" / "query" / "search.py"),
                file_name,
                "--project-id", self.project_id,
                "--root-file-id", self.root_file_id,
            ],
            self.env,
        )
        matches: list[tuple[str, str]] = []
        for row in nested_rows(payload.get("data")):
            name = str(row.get("name") or row.get("fileName") or "")
            file_id = str(row.get("fileId") or row.get("file_id") or row.get("id") or "")
            if name == file_name and file_id:
                matches.append((name, file_id))
        unique = sorted({file_id for _, file_id in matches})
        if len(unique) != 1:
            raise RuntimeError(f"expected one cloud file named {file_name!r}, found {len(unique)}")
        return unique[0]

    def list_tree(self, *, prefixes: tuple[str, ...] = ("raw", "wiki"), max_workers: int = 8) -> list[dict[str, str]]:
        """List physical cloud files under selected top-level folders."""

        def browse(folder_id: str) -> list[dict[str, Any]]:
            payload = command_json(
                [sys.executable, str(DOCDB / "scripts" / "browse" / "browse.py"), str(folder_id)],
                self.env,
            )
            data = payload.get("data")
            return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

        roots = [
            row for row in browse(self.root_file_id)
            if int(row.get("type") or 0) == 1 and str(row.get("name") or "") in set(prefixes)
        ]
        pending = [(str(row.get("id") or ""), str(row.get("name") or "")) for row in roots if row.get("id")]
        files: list[dict[str, str]] = []
        while pending:
            current, pending = pending, []
            with ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="cwk-docdb-browse") as pool:
                futures = {pool.submit(browse, folder_id): (folder_id, rel) for folder_id, rel in current}
                for future in as_completed(futures):
                    _, parent_rel = futures[future]
                    for row in future.result():
                        name = str(row.get("name") or "")
                        if not name or "/" in name or name in {".", ".."}:
                            raise RuntimeError(f"unsafe cloud object name under {parent_rel!r}")
                        rel = f"{parent_rel}/{name}"
                        if int(row.get("type") or 0) == 1:
                            folder_id = str(row.get("id") or "")
                            if folder_id:
                                pending.append((folder_id, rel))
                        else:
                            files.append({"relative_path": rel, "file_id": str(row.get("id") or "")})
        return sorted(files, key=lambda row: (row["relative_path"], row["file_id"]))

    def download(self, file_id: str, target: Path, expected_sha256: str = "", *, force: bool = False) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(f".{target.name}.lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not force and target.is_file() and (not expected_sha256 or sha256_file(target) == expected_sha256):
                return target
            temp = target.with_name(f".{target.name}.{os.getpid()}.download")
            command_json(
                [
                    sys.executable,
                    str(DOCDB / "scripts" / "query" / "download-file.py"),
                    str(file_id),
                    "--output", str(temp),
                ],
                self.env,
            )
            if expected_sha256 and sha256_file(temp) != expected_sha256:
                temp.unlink(missing_ok=True)
                raise RuntimeError(f"cloud checksum mismatch for file_id={file_id}")
            os.replace(temp, target)
        return target

    def download_object(self, row: dict[str, Any], target: Path, expected_sha256: str = "", *, force: bool = False) -> Path:
        parts = [value for value in (row.get("parts") or []) if isinstance(value, dict)]
        if not parts:
            file_id = str(row.get("file_id") or "")
            if not file_id:
                raise RuntimeError("cloud object has no file_id")
            return self.download(file_id, target, expected_sha256, force=force)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(f".{target.name}.object.lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not force and target.is_file() and expected_sha256 and sha256_file(target) == expected_sha256:
                return target
            assembled = target.with_name(f".{target.name}.{os.getpid()}.gz")
            with assembled.open("wb") as output:
                for part in parts:
                    part_id = str(part.get("file_id") or "")
                    part_sha = str(part.get("content_sha256") or "")
                    if not part_id or not part_sha:
                        raise RuntimeError("chunked cloud object has an uncommitted part")
                    part_target = self.cache_root / "object-parts" / f"{part_id}-{part_sha}.bin"
                    self.download(part_id, part_target, part_sha, force=force)
                    with part_target.open("rb") as source:
                        shutil.copyfileobj(source, output)
            artifact_sha = str(row.get("artifact_sha256") or "")
            if not artifact_sha or sha256_file(assembled) != artifact_sha:
                assembled.unlink(missing_ok=True)
                raise RuntimeError("chunked cloud artifact checksum mismatch")
            temp = target.with_name(f".{target.name}.{os.getpid()}.decompressed")
            try:
                with gzip.open(assembled, "rb") as source, temp.open("wb") as output:
                    shutil.copyfileobj(source, output)
            finally:
                assembled.unlink(missing_ok=True)
            if expected_sha256 and sha256_file(temp) != expected_sha256:
                temp.unlink(missing_ok=True)
                raise RuntimeError("chunked cloud object checksum mismatch")
            os.replace(temp, target)
        return target

    def bootstrap(self, *, min_index_version: int = 0) -> tuple[Path, dict[str, Any]]:
        catalog_id = self.search_exact("cwk-cloud-objects.json")
        # Catalog is the commit pointer and must be refreshed on every query;
        # cached content objects are then safe to reuse by checksum.
        catalog_path = self.download(catalog_id, self.cache_root / "cloud-objects.json", force=True)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        index_version = int(catalog.get("index_version") or 0)
        if index_version < min_index_version:
            raise RuntimeError(f"cloud index is stale: {index_version} < required {min_index_version}")
        index_rel = "wiki/_system/" + str(catalog.get("index_file") or "search-index.json.gz")
        cache_mirror = self.cache_root / f"index-v{index_version}"
        index_target = cache_mirror / index_rel
        index_parts = [str(value) for value in (catalog.get("index_files") or [])]
        if index_parts:
            index_target.parent.mkdir(parents=True, exist_ok=True)
            assembled = index_target.with_name(f".{index_target.name}.assembling")
            with assembled.open("wb") as output:
                for name in index_parts:
                    part_rel = "wiki/_system/" + name
                    row = (catalog.get("objects") or {}).get(part_rel) or {}
                    part_id = str(row.get("file_id") or "")
                    part_sha = str(row.get("content_sha256") or "")
                    if not part_id or not part_sha:
                        raise RuntimeError(f"cloud index part is not committed: {part_rel}")
                    part_target = self.cache_root / "parts" / f"{part_id}-{part_sha}.bin"
                    self.download(part_id, part_target, part_sha)
                    with part_target.open("rb") as source:
                        shutil.copyfileobj(source, output)
            expected_artifact = str(catalog.get("index_artifact_sha256") or "")
            if expected_artifact and sha256_file(assembled) != expected_artifact:
                assembled.unlink(missing_ok=True)
                raise RuntimeError("assembled cloud index checksum mismatch")
            os.replace(assembled, index_target)
        else:
            row = (catalog.get("objects") or {}).get(index_rel) or {}
            index_id = str(row.get("file_id") or self.search_exact(Path(index_rel).name))
            expected_sha = str(row.get("content_sha256") or "")
            self.download(index_id, index_target, expected_sha)
        return cache_mirror, catalog

    def raw_text(self, catalog: dict[str, Any], raw_rel: str) -> tuple[str, str, str]:
        row = (catalog.get("objects") or {}).get(raw_rel) or {}
        file_id = str(row.get("file_id") or "")
        expected_sha = str(row.get("content_sha256") or "")
        if not file_id or not expected_sha:
            raise RuntimeError(f"raw object is not committed in cloud catalog: {raw_rel}")
        suffix = Path(raw_rel).suffix or ".md"
        target = self.cache_root / "raw" / f"{file_id}-{expected_sha}{suffix}"
        self.download_object(row, target, expected_sha)
        return target.read_text(encoding="utf-8", errors="replace"), file_id, str(target)

    def preview_url(self, file_id: str) -> str:
        payload = command_json(
            [sys.executable, str(DOCDB / "scripts" / "query" / "get-download-info.py"), str(file_id)],
            self.env,
        )
        data = payload.get("data") or {}
        return str(data.get("previewUrl") or data.get("downloadUrl") or "")

    def clear_cache(self) -> None:
        if self.cache_root.exists():
            shutil.rmtree(self.cache_root)
