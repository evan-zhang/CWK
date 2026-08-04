#!/usr/bin/env python3
"""Promote collected CWork Markdown into the local raw truth source.

The destination is deliberately local-only.  Raw evidence must never be
included in the DocDB sync prefixes; only the compiled Wiki may be synced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
DATE_RE = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def normalize_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10**12:
            number /= 1000
        try:
            return datetime.fromtimestamp(number).astimezone().strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return normalize_date(int(text))
    match = DATE_RE.search(text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def list_row_metadata(text: str) -> dict[str, Any]:
    match = re.search(r"## List Row Metadata\s+```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def nested_values(row: dict[str, Any], keys: Iterable[str]) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        if row.get(key) not in (None, ""):
            values.append(row[key])
    event = row.get("reportEventVO")
    if isinstance(event, dict):
        for key in keys:
            if event.get(key) not in (None, ""):
                values.append(event[key])
    return values


def business_date(text: str) -> tuple[str, str]:
    """Return business date and the field family that supplied it."""
    fields = parse_frontmatter(text)
    row = list_row_metadata(text)

    # A real received/inbox timestamp wins when the upstream API provides it.
    receive_keys = ("received_time", "receive_time", "inbox_time", "receivedTime", "receiveTime", "inboxTime")
    for value in [fields.get(key) for key in receive_keys] + nested_values(row, receive_keys):
        resolved = normalize_date(value)
        if resolved:
            return resolved, "received_time"

    # The report's own send/report time is the stable business date today.
    report_keys = ("report_time", "reportTime", "time")
    for value in [fields.get(key) for key in report_keys] + nested_values(row, report_keys):
        resolved = normalize_date(value)
        if resolved:
            return resolved, "report_time"
    meta_match = re.search(r"<meta>.*?\*\*时间\*\*\s*:\s*([^\n<]+)", text, re.S)
    if meta_match:
        resolved = normalize_date(meta_match.group(1))
        if resolved:
            return resolved, "meta_time"

    create_keys = ("create_time", "createTime", "created_at", "createdAt")
    for value in [fields.get(key) for key in create_keys] + nested_values(row, create_keys):
        resolved = normalize_date(value)
        if resolved:
            return resolved, "create_time"
    return "unknown", "unknown"


def raw_index(raw_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not raw_root.exists():
        return result
    for path in sorted(raw_root.rglob("*.md")):
        if "_system" in path.parts:
            continue
        rid = parse_frontmatter(path.read_text(encoding="utf-8", errors="ignore")).get("report_id")
        rid = rid or path.name.split("-", 1)[0]
        if rid:
            result.setdefault(str(rid), path)
    return result


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def rebuild_manifest(mirror_root: Path, *, cloud_first: bool = False) -> Path:
    raw_root = mirror_root / "raw"
    records = []
    for rid, path in sorted(raw_index(raw_root).items()):
        records.append(
            {
                "report_id": rid,
                "sha256": sha256_bytes(path.read_bytes()),
                "canonical_path": path.relative_to(mirror_root).as_posix(),
            }
        )
    output = raw_root / "_system" / "raw-manifest.json"
    payload = {
        "schema_version": "cwk.raw-truth-source.v2",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_of_truth": "personal_docdb/raw" if cloud_first else "工作协同镜像/raw",
        "persistence_policy": "cloud_first_private_docdb" if cloud_first else "local_private",
        "record_count": len(records),
        "records": records,
    }
    atomic_write(output, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
    return output


def promote(source_dirs: list[Path], mirror_root: Path, *, cloud_first: bool = False) -> dict[str, Any]:
    mirror_root = mirror_root.expanduser().resolve()
    raw_root = mirror_root / "raw"
    existing = raw_index(raw_root)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "invalid": 0}
    changed: list[str] = []
    date_sources: dict[str, int] = {}

    source_paths: list[Path] = []
    for directory in source_dirs:
        source_paths.extend(sorted(directory.expanduser().resolve().glob("*.md")))
    for source in source_paths:
        data = source.read_bytes()
        text = data.decode("utf-8", errors="replace")
        fields = parse_frontmatter(text)
        rid = fields.get("report_id") or source.name.split("-", 1)[0]
        if not rid:
            counts["invalid"] += 1
            continue
        date, date_source = business_date(text)
        date_sources[date_source] = date_sources.get(date_source, 0) + 1
        if rid in existing:
            destination = existing[rid]
            if sha256_bytes(destination.read_bytes()) == sha256_bytes(data):
                counts["unchanged"] += 1
                continue
            counts["updated"] += 1
        else:
            parent = raw_root / "unknown" if date == "unknown" else raw_root / date[:7] / date
            destination = parent / source.name
            counts["created"] += 1
            existing[rid] = destination
        atomic_write(destination, data)
        changed.append(destination.relative_to(mirror_root).as_posix())

    manifest_path = rebuild_manifest(mirror_root, cloud_first=cloud_first)
    return {
        "schema_version": "cwk.raw-promotion.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_dirs": [str(path.expanduser().resolve()) for path in source_dirs],
        "mirror_root": str(mirror_root),
        "counts": counts,
        "date_sources": date_sources,
        "changed_relative_paths": changed,
        "raw_manifest": str(manifest_path),
        "raw_record_count": len(existing),
        "raw_local_only": not cloud_first,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote staged CWork raw files into the local truth source.")
    parser.add_argument("--source-dir", action="append", required=True)
    parser.add_argument("--mirror-root", default=str(MIRROR))
    parser.add_argument("--manifest-out")
    parser.add_argument("--cloud-first", action="store_true")
    args = parser.parse_args()
    result = promote([Path(value) for value in args.source_dir], Path(args.mirror_root), cloud_first=args.cloud_first)
    if args.manifest_out:
        output = Path(args.manifest_out).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(output, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
