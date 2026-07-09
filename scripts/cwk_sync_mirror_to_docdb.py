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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
DOCDB = Path(os.environ.get("CMS_DOCDB_SKILL_DIR", str(Path.home() / ".agents" / "skills" / "cms-docdb")))
AUTH = Path(
    os.environ.get(
        "CMS_AUTH_LOGIN",
        str(Path.home() / ".agents" / "skills" / "cms-auth-skills" / "scripts" / "auth" / "login.py"),
    )
)
DEFAULT_PROJECT_ID = os.environ.get("CWK_DOCDB_PROJECT_ID", "")
DEFAULT_ROOT_FILE_ID = os.environ.get("CWK_DOCDB_ROOT_FILE_ID", "")
DEFAULT_ROOT_NAME = "工作协同镜像"
DEFAULT_SENDER_ID = os.environ.get("CWK_SENDER_ID", "")
DEFAULT_ACCOUNT_ID = os.environ.get("CWK_ACCOUNT_ID", "default")


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
                if payload.get("resultCode") == 1 or "服务器繁忙" not in msg:
                    return payload
                last_error = msg
        else:
            last_error = f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}\n{proc.stdout.strip()}"
        if attempt < retries - 1:
            time.sleep(1 + attempt * 2)
    raise RuntimeError(last_error)


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


def iter_items(limit: int | None, only_prefix: str | None) -> list[SyncItem]:
    files = sorted(path for path in MIRROR.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".html"})
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
                file_name=path.name,
                expected_ancestor=expected_ancestor,
            )
        )
    return items


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
        suffix = item.path.suffix or ".md"
        upload_path = Path(tmp) / f"{item.path.stem[:18].encode('utf-8').hex()[:24]}{suffix}"
        shutil.copy2(item.path, upload_path)
        payload = run_json([sys.executable, str(DOCDB / "scripts/upload/upload-whole-file.py"), str(upload_path)], env)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"upload resource failed for {item.rel}")
    return extract_resource_id(payload, item)


def physical_save_or_update(item: SyncItem, existing: dict | None, project_id: str, env: dict[str, str]) -> dict:
    resource_id = upload_resource(item, env)
    size = str(item.path.stat().st_size)
    suffix = item.path.suffix.lstrip(".") or "md"
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
    physical = any(item.rel.as_posix().startswith(prefix) for prefix in physical_prefixes)
    size = item.path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        return {
            "relative_path": item.rel.as_posix(),
            "action": "skip_too_large",
            "file_id": existing.get("id") if existing else None,
            "folder_name": item.folder_name,
            "size": size,
        }
    if existing and create_missing_only:
        return {
            "relative_path": item.rel.as_posix(),
            "action": "skip_existing",
            "file_id": existing.get("id"),
            "folder_name": item.folder_name,
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
        }
    if physical:
        return physical_save_or_update(item, existing, project_id, env)

    content = item.path.read_text(encoding="utf-8")
    cmd = [
        sys.executable,
        str(DOCDB / "scripts/upload/upload-content.py"),
        content,
        item.file_name,
        "--file-suffix",
        item.path.suffix.lstrip(".") or "md",
        "--project-id",
        project_id,
    ]
    if existing:
        cmd.extend(
            [
                "--update-file-id",
                str(existing["id"]),
                "--version-name",
                datetime.now().strftime("cwk-sync-%Y%m%d-%H%M%S"),
                "--version-remark",
                f"工作协同镜像同步：{item.rel.as_posix()}",
            ]
        )
        action = "update_version"
    else:
        cmd.extend(["--folder-name", item.folder_name])
        action = "create"
    payload = run_json(cmd, env)
    if payload.get("resultCode") != 1:
        raise RuntimeError(payload.get("resultMsg") or f"upload failed for {item.rel}")
    data = payload.get("data") or {}
    return {
        "relative_path": item.rel.as_posix(),
        "action": action,
        "file_id": data.get("fileId") or (existing.get("id") if existing else None),
        "folder_name": item.folder_name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local CWork mirror Markdown files to docdb.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--root-file-id", default=DEFAULT_ROOT_FILE_ID)
    parser.add_argument("--sender-id", default=DEFAULT_SENDER_ID)
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-prefix", default=None, help="Only sync relative paths with this prefix, e.g. raw/")
    parser.add_argument("--physical-prefix", action="append", default=[], help="Use physical-file upload for matching relative path prefixes.")
    parser.add_argument("--max-bytes", type=int, default=None, help="Skip files larger than this many bytes.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--create-missing-only", action="store_true")
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args()
    if not args.project_id or not args.root_file_id:
        raise SystemExit("DocDB sync requires --project-id/--root-file-id or CWK_DOCDB_PROJECT_ID/CWK_DOCDB_ROOT_FILE_ID.")

    app_key = os.environ.get("XG_BIZ_API_KEY") or os.environ.get("XG_APP_KEY") or resolve_app_key(args.sender_id, args.account_id)
    env = os.environ.copy()
    env["XG_BIZ_API_KEY"] = app_key

    results = []
    for index, item in enumerate(iter_items(args.limit, args.only_prefix), 1):
        existing = find_existing(item, args.project_id, args.root_file_id, env)
        results.append(
            upload_or_update(
                item,
                existing,
                args.project_id,
                env,
                args.dry_run,
                args.create_missing_only,
                args.physical_prefix,
                args.max_bytes,
            )
        )
        if index % 10 == 0:
            print(f"processed {index}", file=sys.stderr, flush=True)

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "project_id": args.project_id,
        "root_file_id": args.root_file_id,
        "mirror_root": str(MIRROR.relative_to(PROJECT)),
        "counts": {
            "total": len(results),
            "create": sum(1 for r in results if r["action"] == "create"),
            "update_version": sum(1 for r in results if r["action"] == "update_version"),
            "physical_create": sum(1 for r in results if r["action"] == "physical_create"),
            "physical_update_version": sum(1 for r in results if r["action"] == "physical_update_version"),
            "skip_existing": sum(1 for r in results if r["action"] == "skip_existing"),
            "skip_too_large": sum(1 for r in results if r["action"] == "skip_too_large"),
        },
        "results": results,
    }
    output = Path(args.manifest) if args.manifest else PROJECT / "runs" / "docdb-mirror-sync-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(output)


if __name__ == "__main__":
    main()
