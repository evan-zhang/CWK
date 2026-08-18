#!/usr/bin/env python3
"""Sync the project-local CWork mirror to the personal docdb mirror folder.

The sync is intentionally conservative:
- search before every write;
- update existing files as a new version;
- create only when no exact path match exists;
- support small batches via --limit.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
DOCDB = Path(os.environ.get("CMS_DOCDB_SKILL_DIR", str(Path.home() / ".agents" / "skills" / "cms-docdb")))
AUTH_SKILL = Path(os.environ.get("CMS_AUTH_SKILL_DIR", str(Path.home() / ".agents" / "skills" / "cms-auth-skills")))
AUTH = Path(
    os.environ.get(
        "CMS_AUTH_LOGIN",
        str(AUTH_SKILL / "scripts" / "auth" / "login.py"),
    )
)
DEFAULT_PROJECT_ID = os.environ.get("CWK_DOCDB_PROJECT_ID", "")
DEFAULT_ROOT_FILE_ID = os.environ.get("CWK_DOCDB_ROOT_FILE_ID", "")
DEFAULT_ROOT_NAME = "工作协同镜像"
DEFAULT_SENDER_ID = os.environ.get("CWK_SENDER_ID", "")
DEFAULT_ACCOUNT_ID = os.environ.get("CWK_ACCOUNT_ID", "default")
DEFAULT_RETRY_QUEUE = PROJECT / "runs" / "docdb-sync-retry-queue.json"
DEFAULT_SYNC_STATE = PROJECT / "runs" / "docdb-sync-state.json"
TRANSIENT_ERRORS = (
    "服务器繁忙", "文件信息查询失败", "请求太过频繁", "频繁", "timeout", "timed out",
    "temporarily unavailable", "connection reset", "429", "401",
)
_UPLOAD_CONTENT_MODULE = None
_UPLOAD_CONTENT_LOCK = threading.Lock()
RAW_CHUNK_THRESHOLD_BYTES = 2_000_000
RAW_CHUNK_BYTES = 1_200_000
CLOUD_FILE_NAMES = {
    # `manifest.json` is too generic for the DocDB search API and can produce
    # a server-side file-info lookup failure. Keep the local canonical name
    # while publishing a stable, unique cloud-side name.
    # The prior remote manifest object repeatedly failed its file-info lookup.
    # Publish future versions through a fresh, stable object name rather than
    # retrying a corrupt/stale DocDB file ID indefinitely.
    "wiki/_system/manifest.json": "cwk-wiki-manifest-v2.json",
    "wiki/_system/cloud-objects.json": "cwk-cloud-objects.json",
    "wiki/_system/index-meta.json": "cwk-index-meta.json",
    # DocDB's object pipeline materializes `.gz` uploads as zero-byte files.
    # Publish the opaque compressed index as `.bin`; its catalog path and hash
    # remain `wiki/_system/search-index.json.gz` for local decompression.
    "wiki/_system/search-index.json.gz": "cwk-search-index.bin",
}
COMMIT_POINTER_PATHS = {
    "wiki/_system/manifest.json",
    "wiki/_system/cloud-objects.json",
}
SUCCESS_ACTIONS = {
    "create", "update_version", "physical_create", "physical_update_version",
    "physical_chunked_create", "physical_chunked_update", "skip_existing", "unchanged",
}


def sanitize_error(value: object) -> str:
    return str(value)[:1000]


@dataclass
class SyncItem:
    path: Path
    rel: Path
    folder_name: str
    file_name: str
    expected_ancestor: str


def run_json(cmd: list[str], env: dict[str, str], retries: int = 3) -> dict:
    last_error = ""
    for attempt in range(retries):
        proc = subprocess.run(cmd, cwd=str(DOCDB), env=env, text=True, capture_output=True)
        if proc.returncode == 0:
            lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
            if not lines:
                last_error = f"command returned no JSON: {' '.join(cmd)}\n{proc.stdout.strip()}"
            else:
                payload = json.loads(lines[-1])
                msg = payload.get("resultMsg") or ""
                if payload.get("resultCode") == 1 or not any(token.lower() in msg.lower() for token in TRANSIENT_ERRORS):
                    return payload
                last_error = msg
        else:
            last_error = f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}\n{proc.stdout.strip()}"
        if attempt < retries - 1:
            time.sleep(1 + attempt)
    raise RuntimeError(sanitize_error(last_error))


def resolve_app_key(sender_id: str, account_id: str) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            str(AUTH),
            "--resolve-app-key",
            "--sender-id",
            sender_id,
            "--account-id",
            account_id,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "failed to resolve app key")
    key = proc.stdout.strip().splitlines()[-1].strip()
    if not key:
        raise RuntimeError("empty app key")
    return key


def extract_id(payload: dict, keys: list[str]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in keys:
            if data.get(key):
                return str(data[key])
    if isinstance(data, (str, int)) and str(data):
        return str(data)
    raise RuntimeError(payload.get("resultMsg") or f"missing id in response; expected one of {keys}")


def get_personal_project_id(env: dict[str, str]) -> str:
    payload = run_json([sys.executable, str(DOCDB / "scripts/browse/get-personal-project-id.py")], env)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or "failed to resolve personal DocDB project id")
    return extract_id(payload, ["projectId", "id"])


def child_items(payload: dict) -> list[dict]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items: list[dict] = []
        for key in ["folders", "files", "list", "rows", "items", "children"]:
            value = data.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
        return items
    return []


def ensure_root_folder(project_id: str, env: dict[str, str], dry_run: bool) -> str:
    payload = run_json([sys.executable, str(DOCDB / "scripts/browse/get-level1-folders.py"), project_id], env)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or "failed to browse personal DocDB root")
    for item in child_items(payload):
        if item.get("name") == DEFAULT_ROOT_NAME and str(item.get("type")) in {"1", "folder", ""}:
            return str(item.get("id") or item.get("fileId"))
    if dry_run:
        return "0"
    payload = run_json(
        [
            sys.executable,
            str(DOCDB / "scripts/upload/create-folder.py"),
            "0",
            DEFAULT_ROOT_NAME,
            "--project-id",
            project_id,
        ],
        env,
    )
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"failed to create {DEFAULT_ROOT_NAME} folder")
    return extract_id(payload, ["folderId", "fileId", "id"])


def resolve_docdb_target(project_id: str, root_file_id: str, env: dict[str, str], dry_run: bool) -> tuple[str, str]:
    if not project_id:
        project_id = get_personal_project_id(env)
    if not root_file_id:
        root_file_id = ensure_root_folder(project_id, env, dry_run)
    return str(project_id), str(root_file_id)


def iter_items(
    limit: int | None,
    only_prefix: str | None,
    paths_manifest: str | None = None,
    *,
    allow_raw: bool = False,
) -> list[SyncItem]:
    # JSON manifests are part of the cloud-side truth/audit layer.  Keep them
    # alongside Markdown/HTML rather than leaving state only on this machine.
    files = sorted(path for path in MIRROR.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".html", ".json", ".gz", ".bin"})
    # The compressed index is the cloud canonical artifact. The uncompressed
    # JSON copy is a local build aid and would add ~20 MB to every sync.
    files = [
        path for path in files
        if path.relative_to(MIRROR).as_posix() not in {
            "wiki/_system/search-index.json",
            "wiki/_system/search-index.json.gz",
        }
    ]
    if not allow_raw:
        files = [path for path in files if not path.relative_to(MIRROR).as_posix().startswith("raw/")]
    if paths_manifest:
        payload = json.loads(Path(paths_manifest).expanduser().resolve().read_text(encoding="utf-8"))
        allowed = {str(value) for value in (payload.get("changed_relative_paths") or [])}
        files = [path for path in files if path.relative_to(MIRROR).as_posix() in allowed]
    if only_prefix:
        files = [path for path in files if path.relative_to(MIRROR).as_posix().startswith(only_prefix)]
    if limit is not None:
        files = files[:limit]
    items: list[SyncItem] = []
    for path in files:
        rel = path.relative_to(MIRROR)
        parent = rel.parent
        folder_name = DEFAULT_ROOT_NAME if str(parent) == "." else f"{DEFAULT_ROOT_NAME}/{parent.as_posix()}"
        expected_ancestor = DEFAULT_ROOT_NAME if str(parent) == "." else f"{DEFAULT_ROOT_NAME}/{parent.as_posix()}"
        items.append(
            SyncItem(
                path=path,
                rel=rel,
                folder_name=folder_name,
                file_name=CLOUD_FILE_NAMES.get(rel.as_posix(), path.name),
                expected_ancestor=expected_ancestor,
            )
        )
    return items


def load_retry_paths(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(value) for value in payload.get("failed_relative_paths", [])}


def write_retry_paths(path: Path, paths: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "failed_relative_paths": sorted(paths),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sync_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"schema_version": "cwk.docdb_sync_state.v1", "objects": {}}
    if not isinstance(payload.get("objects"), dict):
        payload["objects"] = {}
    payload.setdefault("schema_version", "cwk.docdb_sync_state.v1")
    return payload


def bootstrap_sync_state(path: Path, receipts_dir: Path) -> dict:
    """Recover durable file IDs and hashes from prior successful receipts.

    This makes a fresh install or a deleted state file self-healing without
    asking DocDB's search endpoint to rediscover every existing object.
    """
    state = load_sync_state(path)
    objects = state.setdefault("objects", {})
    if objects:
        return state
    receipts: list[tuple[str, Path, dict]] = []
    for receipt in receipts_dir.glob("docdb-*.json"):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if payload.get("dry_run"):
            continue
        receipts.append((str(payload.get("generated_at") or ""), receipt, payload))
    for generated_at, _receipt, payload in sorted(receipts):
        for row in payload.get("results") or []:
            rel = str(row.get("relative_path") or "")
            file_id = str(row.get("file_id") or "")
            if not rel or not file_id or row.get("action") not in SUCCESS_ACTIONS:
                continue
            objects[rel] = {
                "file_id": file_id,
                "content_sha256": (
                    "" if row.get("action") == "skip_existing"
                    else str(row.get("content_sha256") or "")
                ),
                "file_name": str(row.get("file_name") or CLOUD_FILE_NAMES.get(rel, Path(rel).name)),
                "synced_at": generated_at,
            }
    return state


def write_sync_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def partition_retry_paths(retry_paths: set[str], only_prefix: str | None, available: set[str]) -> tuple[set[str], set[str]]:
    matching = {rel for rel in retry_paths if not only_prefix or rel.startswith(only_prefix)}
    return matching & available, matching - available


def partition_commit_items(items: list[SyncItem]) -> tuple[list[SyncItem], list[SyncItem]]:
    regular = [item for item in items if item.rel.as_posix() not in COMMIT_POINTER_PATHS]
    commit = [item for item in items if item.rel.as_posix() in COMMIT_POINTER_PATHS]
    return regular, commit


def enforce_raw_cloud_pause(*, allow_raw: bool, experimental_cloud_raw: bool) -> None:
    """Require a second explicit opt-in while raw cloud publishing is paused."""
    if allow_raw and not experimental_cloud_raw:
        raise SystemExit(
            "CWK raw cloud publishing is paused. For a controlled Cloud-First experiment, "
            "also pass --experimental-cloud-raw."
        )


def healed_cloud_name(item: SyncItem) -> str:
    """Return a stable alternate name when a remote file ID is unrecoverable."""
    suffix = Path(item.file_name).suffix
    stem = item.file_name[:-len(suffix)] if suffix else item.file_name
    path_key = hashlib.sha256(item.rel.as_posix().encode("utf-8")).hexdigest()[:10]
    return f"{stem}.cwk-heal-{path_key}{suffix}"


def write_sync_manifest(output: Path, args: argparse.Namespace, results: list[dict]) -> dict:
    counts = {
        "total": len(results),
        "create": sum(1 for r in results if r.get("action") == "create"),
        "update_version": sum(1 for r in results if r.get("action") == "update_version"),
        "physical_create": sum(1 for r in results if r.get("action") == "physical_create"),
        "physical_update_version": sum(1 for r in results if r.get("action") == "physical_update_version"),
        "physical_chunked_create": sum(1 for r in results if r.get("action") == "physical_chunked_create"),
        "physical_chunked_update": sum(1 for r in results if r.get("action") == "physical_chunked_update"),
        "skip_existing": sum(1 for r in results if r.get("action") == "skip_existing"),
        "unchanged": sum(1 for r in results if r.get("action") == "unchanged"),
        "skip_too_large": sum(1 for r in results if r.get("action") == "skip_too_large"),
        "failed": sum(1 for r in results if r.get("action") == "failed"),
    }
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "project_id": args.project_id,
        "root_file_id": args.root_file_id,
        "allow_raw": bool(getattr(args, "allow_raw", False)),
        "experimental_cloud_raw": bool(getattr(args, "experimental_cloud_raw", False)),
        "mirror_root": str(MIRROR.relative_to(PROJECT)) if MIRROR.is_relative_to(PROJECT) else str(MIRROR),
        "counts": counts,
        "results": results,
        "retry_queue": str(Path(args.retry_queue).expanduser().resolve()),
        "sync_state": str(Path(args.sync_state).expanduser().resolve()),
        "stale_retry_paths_removed": sorted(getattr(args, "stale_retry_paths_removed", [])),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def find_existing(item: SyncItem, project_id: str, root_file_id: str, env: dict[str, str]) -> dict | None:
    payload = run_json(
        [
            sys.executable,
            str(DOCDB / "scripts/query/search.py"),
            item.file_name,
            "--project-id",
            project_id,
            "--root-file-id",
            root_file_id,
        ],
        env,
    )
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"search failed for {item.rel}")
    files = (payload.get("data") or {}).get("files") or []
    exact = [f for f in files if f.get("name") == item.file_name and f.get("ancestorNames") == item.expected_ancestor]
    if exact:
        return exact[0]
    return None


def resolve_existing_for_publish(
    item: SyncItem,
    catalog_row: dict,
    project_id: str,
    root_file_id: str,
    env: dict[str, str],
    assume_missing: bool,
) -> dict | None:
    """Resolve the live target for a publish operation.

    Commit pointers are deliberately re-discovered by their exact cloud path
    instead of trusting a cached file ID.  A stale pointer ID can otherwise
    keep every later generation pinned to an unreadable DocDB object.
    """
    if assume_missing:
        return None
    if item.rel.as_posix() in COMMIT_POINTER_PATHS:
        return find_existing(item, project_id, root_file_id, env)
    if catalog_row.get("file_id"):
        return {"id": str(catalog_row["file_id"]), "name": item.file_name}
    return find_existing(item, project_id, root_file_id, env)


def extract_resource_id(payload: dict, item: SyncItem) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ["resourceId", "id"]:
            if data.get(key):
                return str(data[key])
    if isinstance(data, (str, int)) and str(data):
        return str(data)
    raise RuntimeError(payload.get("resultMsg") or f"upload resource failed for {item.rel}")


def upload_resource(item: SyncItem, env: dict[str, str]) -> str:
    # Keep the multipart upload filename short and ASCII. The docdb save step
    # still stores the original mirror file name.
    with tempfile.TemporaryDirectory(prefix="cwk-raw-upload-") as tmp:
        # Raw Markdown can contain multi-megabyte embedded payloads. Upload it
        # as an opaque resource so DocDB does not route it through the text
        # materializer (which truncates/zeros large Markdown bodies). The
        # saved file keeps its original display name and suffix.
        opaque = item.rel.as_posix().startswith("raw/") or item.path.suffix.lower() in {".gz", ".bin"}
        suffix = ".bin" if opaque else (item.path.suffix or ".md")
        upload_path = Path(tmp) / f"{item.path.stem[:18].encode('utf-8').hex()[:24]}{suffix}"
        shutil.copy2(item.path, upload_path)
        payload = run_json([sys.executable, str(DOCDB / "scripts/upload/upload-whole-file.py"), str(upload_path)], env)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"upload resource failed for {item.rel}")
    return extract_resource_id(payload, item)


def upload_text_api(**kwargs) -> dict:
    """Call the approved cms-docdb upload script in-process.

    This avoids placing confidential Markdown bodies in the OS process list and
    also removes one Python startup per object.
    """
    global _UPLOAD_CONTENT_MODULE
    if _UPLOAD_CONTENT_MODULE is None:
        with _UPLOAD_CONTENT_LOCK:
            if _UPLOAD_CONTENT_MODULE is None:
                script = DOCDB / "scripts" / "upload" / "upload-content.py"
                spec = importlib.util.spec_from_file_location("cwk_cms_docdb_upload_content", script)
                if not spec or not spec.loader:
                    raise RuntimeError(f"cannot load approved DocDB upload script: {script}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _UPLOAD_CONTENT_MODULE = module
    last_result: dict | None = None
    for attempt in range(3):
        try:
            payload = _UPLOAD_CONTENT_MODULE.call_api(**kwargs)
        except SystemExit as exc:
            raise RuntimeError(f"DocDB upload-content failed: {exc}") from exc
        result = _UPLOAD_CONTENT_MODULE.process_result(payload)
        last_result = result
        message = str(result.get("resultMsg") or "")
        transient = any(token.lower() in message.lower() for token in TRANSIENT_ERRORS)
        if result.get("resultCode") == 1 or not transient:
            return result
        if attempt < 2:
            time.sleep(1 + attempt)
    return last_result or {"resultCode": 0, "resultMsg": "DocDB upload-content returned no result"}


def physical_save_or_update(item: SyncItem, existing: dict | None, project_id: str, env: dict[str, str]) -> dict:
    resource_id = upload_resource(item, env)
    size = str(item.path.stat().st_size)
    suffix = Path(item.file_name).suffix.lstrip(".") or item.path.suffix.lstrip(".") or "md"
    if existing:
        action = "physical_update_version"
        payload = run_json(
            [
                sys.executable,
                str(DOCDB / "scripts/manage/update-file-version.py"),
                str(existing["id"]),
                project_id,
                resource_id,
                "--name",
                item.file_name,
                "--version-status",
                "3",
                "--version-name",
                datetime.now().strftime("cwk-raw-%Y%m%d-%H%M%S"),
                "--version-remark",
                f"工作协同镜像 raw 物理文件同步：{item.rel.as_posix()}",
                "--suffix",
                suffix,
                "--size",
                size,
            ],
            env,
        )
    else:
        action = "physical_create"
        payload = run_json(
            [
                sys.executable,
                str(DOCDB / "scripts/upload/save-file-by-path.py"),
                project_id,
                item.file_name,
                resource_id,
                "--path",
                item.folder_name,
                "--suffix",
                suffix,
                "--size",
                size,
                "--is-sensitive",
                "0",
            ],
            env,
        )
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"physical save failed for {item.rel}")
    data = payload.get("data")
    if isinstance(data, dict):
        file_id = data.get("fileId") or data.get("id")
    elif isinstance(data, (str, int)):
        file_id = str(data)
    else:
        file_id = None
    return {
        "relative_path": item.rel.as_posix(),
        "action": action,
        "file_id": file_id or (existing.get("id") if existing else None),
        "folder_name": item.folder_name,
        "size": int(size),
    }


def upload_chunked_raw(
    item: SyncItem,
    catalog_row: dict,
    project_id: str,
    root_file_id: str,
    env: dict[str, str],
    dry_run: bool,
) -> dict:
    """Publish a large raw object as deterministic gzip parts.

    DocDB's Markdown materializer returns zero bytes for large `.md` files.
    Versioned opaque parts keep exact bytes and let the catalog switch to the
    complete new generation atomically.
    """
    raw_bytes = item.path.read_bytes()
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    compressed = gzip.compress(raw_bytes, compresslevel=9, mtime=0)
    artifact_sha256 = hashlib.sha256(compressed).hexdigest()
    report_id = item.path.name.split("-", 1)[0]
    part_rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cwk-raw-parts-") as tmp:
        for index, offset in enumerate(range(0, len(compressed), RAW_CHUNK_BYTES)):
            blob = compressed[offset : offset + RAW_CHUNK_BYTES]
            name = f"{report_id}.raw-{content_sha256[:16]}-{index:03d}.bin"
            path = Path(tmp) / name
            path.write_bytes(blob)
            part_rel = item.rel.parent / name
            part_item = SyncItem(
                path=path,
                rel=part_rel,
                folder_name=item.folder_name,
                file_name=name,
                expected_ancestor=item.expected_ancestor,
            )
            existing = find_existing(part_item, project_id, root_file_id, env)
            if dry_run:
                file_id = str((existing or {}).get("id") or "")
            elif existing:
                # The name is content-addressed; an exact-name hit is an
                # immutable part from this same logical generation.
                file_id = str(existing.get("id") or "")
            else:
                saved = physical_save_or_update(part_item, None, project_id, env)
                file_id = str(saved.get("file_id") or "")
            if not dry_run and not file_id:
                raise RuntimeError(f"chunk upload returned no file id for {part_rel.as_posix()}")
            part_rows.append(
                {
                    "name": name,
                    "remote_relative_path": part_rel.as_posix(),
                    "file_id": file_id,
                    "content_sha256": hashlib.sha256(blob).hexdigest(),
                    "size": len(blob),
                }
            )
    return {
        "relative_path": item.rel.as_posix(),
        "action": "physical_chunked_update" if catalog_row.get("parts") else "physical_chunked_create",
        "file_id": str(part_rows[0].get("file_id") or "") if part_rows else "",
        "folder_name": item.folder_name,
        "size": len(raw_bytes),
        "content_sha256": content_sha256,
        "storage": "physical_gzip_parts",
        "compression": "gzip",
        "artifact_sha256": artifact_sha256,
        "parts": part_rows,
    }


def upload_or_update(
    item: SyncItem,
    existing: dict | None,
    project_id: str,
    env: dict[str, str],
    dry_run: bool,
    create_missing_only: bool,
    physical_prefixes: list[str],
    max_bytes: int | None,
) -> dict:
    content_sha256 = hashlib.sha256(item.path.read_bytes()).hexdigest()
    physical = any(item.rel.as_posix().startswith(prefix) for prefix in physical_prefixes)
    size = item.path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        return {
            "relative_path": item.rel.as_posix(),
            "action": "skip_too_large",
            "file_id": existing.get("id") if existing else None,
            "folder_name": item.folder_name,
            "size": size,
            "content_sha256": content_sha256,
        }
    if existing and create_missing_only:
        return {
            "relative_path": item.rel.as_posix(),
            "action": "skip_existing",
            "file_id": existing.get("id"),
            "folder_name": item.folder_name,
            "content_sha256": content_sha256,
        }
    if dry_run:
        return {
            "relative_path": item.rel.as_posix(),
            "action": (
                "skip_existing"
                if existing and create_missing_only
                else "physical_update_version"
                if physical and existing
                else "physical_create"
                if physical
                else "update_version"
                if existing
                else "create"
            ),
            "file_id": existing.get("id") if existing else None,
            "folder_name": item.folder_name,
            "content_sha256": content_sha256,
        }
    if physical:
        result = physical_save_or_update(item, existing, project_id, env)
        result["content_sha256"] = content_sha256
        return result

    try:
        content = item.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"binary/non-UTF8 object requires --physical-prefix for {item.rel.as_posix()}"
        ) from exc
    upload_kwargs = {
        "content": content,
        "file_name": item.file_name,
        "file_suffix": item.path.suffix.lstrip(".") or "md",
        "project_id": int(project_id),
    }
    if existing:
        upload_kwargs.update(
            {
                "update_file_id": int(existing["id"]),
                "version_name": datetime.now().strftime("cwk-sync-%Y%m%d-%H%M%S"),
                "version_remark": f"工作协同镜像同步：{item.rel.as_posix()}",
            }
        )
        action = "update_version"
    else:
        upload_kwargs["folder_name"] = item.folder_name
        action = "create"
    payload = upload_text_api(**upload_kwargs)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"upload failed for {item.rel}")
    data = payload.get("data") or {}
    return {
        "relative_path": item.rel.as_posix(),
        "action": action,
        "file_id": data.get("fileId") or (existing.get("id") if existing else None),
        "folder_name": item.folder_name,
        "content_sha256": content_sha256,
    }


def publish_with_stale_id_recovery(
    item: SyncItem,
    existing: dict | None,
    project_id: str,
    root_file_id: str,
    env: dict[str, str],
    dry_run: bool,
    create_missing_only: bool,
    physical_prefixes: list[str],
    max_bytes: int | None,
) -> dict:
    try:
        result = upload_or_update(
            item, existing, project_id, env, dry_run,
            create_missing_only, physical_prefixes, max_bytes,
        )
        result.setdefault("file_name", item.file_name)
        return result
    except Exception as exc:
        error = sanitize_error(exc)
        if not existing or "文件信息查询失败" not in error or dry_run:
            raise RuntimeError(f"publish: {error}") from exc
        healed_item = replace(item, file_name=healed_cloud_name(item))
        try:
            healed_existing = find_existing(healed_item, project_id, root_file_id, env)
            result = upload_or_update(
                healed_item, healed_existing, project_id, env, False,
                create_missing_only, physical_prefixes, max_bytes,
            )
            result.update({
                "file_name": healed_item.file_name,
                "self_healed": True,
                "stale_file_id": str(existing.get("id") or ""),
            })
            return result
        except Exception as heal_exc:
            raise RuntimeError(
                f"publish_stale_id; self_heal_failed: {sanitize_error(heal_exc)}"
            ) from heal_exc


def main() -> None:
    global MIRROR
    parser = argparse.ArgumentParser(description="Sync local CWork mirror Markdown files to docdb.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--root-file-id", default=DEFAULT_ROOT_FILE_ID)
    parser.add_argument("--sender-id", default=DEFAULT_SENDER_ID)
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-prefix", default=None, help="Only sync relative paths with this prefix, e.g. raw/")
    parser.add_argument(
        "--allow-raw",
        action="store_true",
        help="Explicitly permit raw/ uploads for an approved Cloud-First migration. Raw is denied by default.",
    )
    parser.add_argument(
        "--experimental-cloud-raw",
        action="store_true",
        help="Second explicit unlock required while Cloud-First raw publishing is paused.",
    )
    parser.add_argument("--paths-manifest", default=None, help="Only sync paths listed in a safe-materialize manifest.")
    parser.add_argument("--physical-prefix", action="append", default=[], help="Use physical-file upload for matching relative path prefixes.")
    parser.add_argument("--max-bytes", type=int, default=None, help="Skip files larger than this many bytes.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--create-missing-only", action="store_true")
    parser.add_argument(
        "--assume-missing",
        action="store_true",
        help="Skip search and create directly; only safe with a pre-audited manifest of paths absent from DocDB.",
    )
    parser.add_argument(
        "--object-catalog",
        default="",
        help="Optional cloud-objects.json used to resolve existing file IDs without one search request per path.",
    )
    parser.add_argument("--max-parallel", type=int, default=1, help="Concurrent DocDB object syncs (1-16).")
    parser.add_argument(
        "--mirror-root",
        default=os.environ.get("CWK_MIRROR_ROOT", str(MIRROR)),
        help="Local cache of the cloud mirror to sync (defaults to this package's mirror).",
    )
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--retry-queue", default=str(DEFAULT_RETRY_QUEUE), help="Persistent queue for failed relative paths.")
    parser.add_argument("--sync-state", default=str(DEFAULT_SYNC_STATE), help="Durable successful file-id/hash receipt cache.")
    parser.add_argument("--retry-only", action="store_true", help="Process only currently queued retry paths.")
    args = parser.parse_args()
    enforce_raw_cloud_pause(
        allow_raw=bool(args.allow_raw),
        experimental_cloud_raw=bool(args.experimental_cloud_raw),
    )
    MIRROR = Path(args.mirror_root).expanduser().resolve()
    if not MIRROR.is_dir():
        raise SystemExit(f"mirror root does not exist: {MIRROR}")
    if str(args.only_prefix or "").startswith("raw/") and not args.allow_raw:
        raise SystemExit(
            "raw/ sync is denied by default; an approved experiment requires both "
            "--allow-raw and --experimental-cloud-raw"
        )
    if args.max_parallel < 1 or args.max_parallel > 16:
        raise SystemExit("--max-parallel must be between 1 and 16")

    app_key = (
        os.environ.get("XG_BIZ_API_KEY")
        or os.environ.get("XG_APP_KEY")
        or os.environ.get("CWORK_APP_KEY")
        or resolve_app_key(args.sender_id, args.account_id)
    )
    env = os.environ.copy()
    env["XG_BIZ_API_KEY"] = app_key
    os.environ["XG_BIZ_API_KEY"] = app_key
    args.project_id, args.root_file_id = resolve_docdb_target(args.project_id, args.root_file_id, env, args.dry_run)
    if args.allow_raw:
        personal_project_id = get_personal_project_id(env)
        if str(args.project_id) != str(personal_project_id):
            raise SystemExit("raw/ upload is permitted only to the authenticated user's personal/private DocDB project")
        if not any(str(prefix).startswith("raw/") for prefix in args.physical_prefix):
            raise SystemExit("raw/ upload requires --physical-prefix raw/ so original bytes are never sent to the text materializer")

    output = Path(args.manifest) if args.manifest else PROJECT / "runs" / "docdb-mirror-sync-manifest.json"
    retry_queue = Path(args.retry_queue).expanduser().resolve()
    retry_paths = load_retry_paths(retry_queue)
    sync_state_path = Path(args.sync_state).expanduser().resolve()
    sync_state = bootstrap_sync_state(sync_state_path, PROJECT / "runs")
    sync_state_objects = sync_state.setdefault("objects", {})
    local_catalog_objects: dict[str, dict] = {}
    local_catalog_path = MIRROR / "wiki" / "_system" / "cloud-objects.json"
    try:
        local_catalog_objects = json.loads(local_catalog_path.read_text(encoding="utf-8")).get("objects") or {}
    except (OSError, ValueError, TypeError):
        pass
    all_prefix_items = {
        item.rel.as_posix(): item
        for item in iter_items(None, args.only_prefix, None, allow_raw=args.allow_raw)
    }
    active_retry_paths, stale_retry_paths = partition_retry_paths(
        retry_paths, args.only_prefix, set(all_prefix_items),
    )
    retry_paths.difference_update(stale_retry_paths)
    args.stale_retry_paths_removed = stale_retry_paths
    items = [] if args.retry_only else iter_items(args.limit, args.only_prefix, args.paths_manifest, allow_raw=args.allow_raw)
    selected = {item.rel.as_posix(): item for item in items}
    for rel in sorted(active_retry_paths):
        selected.setdefault(rel, all_prefix_items[rel])

    results: list[dict] = []
    catalog_objects: dict[str, dict] = {}
    if args.object_catalog:
        catalog_path = Path(args.object_catalog).expanduser().resolve()
        try:
            catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog_objects = {
                str(rel): row for rel, row in (catalog_payload.get("objects") or {}).items()
                if isinstance(row, dict) and row.get("file_id")
            }
        except (OSError, ValueError, TypeError) as exc:
            raise SystemExit(f"invalid object catalog {catalog_path}: {exc}") from exc

    def sync_item(item: SyncItem) -> dict:
        rel = item.rel.as_posix()
        try:
            catalog_row = catalog_objects.get(rel) or sync_state_objects.get(rel) or local_catalog_objects.get(rel) or {}
            effective_item = replace(item, file_name=str(catalog_row.get("file_name") or item.file_name))
            content_sha256 = hashlib.sha256(item.path.read_bytes()).hexdigest()
            if catalog_row.get("file_id") and catalog_row.get("content_sha256") == content_sha256:
                return {
                    "relative_path": rel,
                    "action": "unchanged",
                    "file_id": str(catalog_row["file_id"]),
                    "folder_name": item.folder_name,
                    "file_name": effective_item.file_name,
                    "content_sha256": content_sha256,
                }
            if (
                rel.startswith("raw/")
                and item.path.stat().st_size > RAW_CHUNK_THRESHOLD_BYTES
                and any(rel.startswith(prefix) for prefix in args.physical_prefix)
            ):
                return upload_chunked_raw(
                    effective_item, catalog_row, args.project_id, args.root_file_id, env, args.dry_run,
                )
            try:
                existing = resolve_existing_for_publish(
                    effective_item,
                    catalog_row,
                    args.project_id,
                    args.root_file_id,
                    env,
                    args.assume_missing,
                )
            except Exception as exc:
                raise RuntimeError(f"resolve_existing: {sanitize_error(exc)}") from exc
            try:
                return publish_with_stale_id_recovery(
                    effective_item, existing, args.project_id, args.root_file_id, env,
                    args.dry_run, args.create_missing_only, args.physical_prefix, args.max_bytes,
                )
            except Exception as exc:
                raise RuntimeError(sanitize_error(exc)) from exc
        except Exception as exc:
            error = sanitize_error(exc)
            return {
                "relative_path": rel,
                "action": "failed",
                "error": error,
                "retryable": any(token.lower() in error.lower() for token in TRANSIENT_ERRORS),
            }

    values = list(selected.values())
    regular_values, commit_values = partition_commit_items(values)

    def record_result(result: dict, fallback_item: SyncItem, index: int) -> None:
        rel = str(result.get("relative_path") or fallback_item.rel.as_posix())
        results.append(result)
        if not args.dry_run:
            if result.get("action") == "failed":
                retry_paths.add(rel)
            else:
                retry_paths.discard(rel)
                file_id = str(result.get("file_id") or "")
                if file_id:
                    sync_state_objects[rel] = {
                        "file_id": file_id,
                        "content_sha256": str(result.get("content_sha256") or ""),
                        "file_name": str(result.get("file_name") or fallback_item.file_name),
                        "synced_at": datetime.now().isoformat(timespec="seconds"),
                    }
        if index % 10 == 0:
            if not args.dry_run:
                write_retry_paths(retry_queue, retry_paths)
                write_sync_state(sync_state_path, sync_state)
            write_sync_manifest(output, args, sorted(results, key=lambda row: str(row.get("relative_path") or "")))
            print(f"processed {index}", file=sys.stderr, flush=True)
    processed = 0
    with ThreadPoolExecutor(max_workers=args.max_parallel, thread_name_prefix="cwk-docdb") as executor:
        futures = {executor.submit(sync_item, item): item for item in regular_values}
        for future in as_completed(futures):
            processed += 1
            record_result(future.result(), futures[future], processed)
    # Commit pointers are published only after every data object in the batch
    # has settled, and are deliberately serialized to avoid racing large index
    # uploads against the object that declares the new generation complete.
    for item in commit_values:
        processed += 1
        record_result(sync_item(item), item, processed)
    results.sort(key=lambda row: str(row.get("relative_path") or ""))
    if not args.dry_run:
        write_retry_paths(retry_queue, retry_paths)
        write_sync_state(sync_state_path, sync_state)
    manifest = write_sync_manifest(output, args, results)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(output)
    if manifest["counts"]["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
