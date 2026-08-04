#!/usr/bin/env python3
"""Safely reconcile Wiki source metadata without destroying quality state."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from cwk_ai_common import parse_frontmatter


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"
FALLBACK_MARKER = "本页为本次重组阶段生成的本地兜底摘要"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_rows(mirror: Path) -> list[dict[str, str]]:
    raw_root = mirror / "raw"
    raw_manifest = raw_root / "_system" / "raw-manifest.json"
    records: list[dict[str, Any]] = []
    if raw_manifest.exists():
        payload = json.loads(raw_manifest.read_text(encoding="utf-8"))
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in records:
        report_id = str(item.get("report_id") or "").strip()
        relative = str(item.get("canonical_path") or "").strip()
        if not report_id or not relative or report_id in seen:
            continue
        path = mirror / relative
        if not path.is_file():
            continue
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        parts = path.relative_to(raw_root).parts
        month = parts[0] if parts and parts[0] not in {"_system", "unknown"} else "unknown"
        rows.append(
            {
                "report_id": report_id,
                "sha256": sha256_file(path),
                "source_lane": str(meta.get("source_lane") or "unknown"),
                "month": month,
                "canonical_path": relative,
            }
        )
        seen.add(report_id)
    # A missing/stale raw manifest must not make sources disappear silently.
    for path in sorted(raw_root.rglob("*.md")):
        if "_system" in path.parts:
            continue
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        report_id = str(meta.get("report_id") or path.name.split("-", 1)[0]).strip()
        if not report_id or report_id in seen:
            continue
        parts = path.relative_to(raw_root).parts
        rows.append(
            {
                "report_id": report_id,
                "sha256": sha256_file(path),
                "source_lane": str(meta.get("source_lane") or "unknown"),
                "month": parts[0] if parts and parts[0] not in {"unknown"} else "unknown",
                "canonical_path": path.relative_to(mirror).as_posix(),
            }
        )
        seen.add(report_id)
    return sorted(rows, key=lambda item: item["report_id"])


def reconcile_manifest(mirror: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    rows = source_rows(mirror)
    summaries = mirror / "wiki" / "summaries"
    disk_ids = {path.stem for path in summaries.glob("*.md")}
    fallback_ids = {
        path.stem
        for path in summaries.glob("*.md")
        if FALLBACK_MARKER in path.read_text(encoding="utf-8", errors="replace")
    }
    lanes = Counter(row["source_lane"] for row in rows)
    months = Counter(row["month"] for row in rows)
    result = dict(manifest)
    withheld_ids = set(result.get("withheld_report_ids", [])) & disk_ids
    reconciled_fields = {
            "source_count": len(rows),
            "source_hashes": {row["report_id"]: row["sha256"] for row in rows},
            "source_lanes": dict(sorted(lanes.items())),
            "source_months": dict(sorted(months.items())),
            "compiled_report_ids": sorted(disk_ids),
            "fallback_report_ids": sorted(fallback_ids),
            "ai_refined_report_ids": sorted(disk_ids - fallback_ids - withheld_ids),
    }
    changed = any(result.get(key) != value for key, value in reconciled_fields.items())
    result.update(reconciled_fields)
    # Withholding is a privacy state, not a summary-rendering style. Preserve
    # it for every still-compiled report even if the page no longer contains
    # the fallback marker.
    result["withheld_report_ids"] = sorted(withheld_ids)
    result["failure_queue"] = [
        item for item in result.get("failure_queue", [])
        if isinstance(item, dict) and str(item.get("report_id") or "") in fallback_ids
    ]
    if changed or result.get("withheld_report_ids") != manifest.get("withheld_report_ids") or result.get("failure_queue") != manifest.get("failure_queue"):
        result["last_source_reconcile_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return result


def reconcile_file(mirror: Path, *, write: bool = True) -> dict[str, Any]:
    path = mirror / "wiki" / "_system" / "manifest.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    after = reconcile_manifest(mirror, before)
    changed = before != after
    if changed and write:
        atomic_json(path, after)
    return {
        "schema_version": "cwk.wiki_manifest_reconcile.v1",
        "mirror_root": str(mirror),
        "changed": changed,
        "written": bool(changed and write),
        "source_count": after.get("source_count", 0),
        "source_hashes": len(after.get("source_hashes", {})),
        "compiled": len(after.get("compiled_report_ids", [])),
        "ai_refined": len(after.get("ai_refined_report_ids", [])),
        "fallback": len(after.get("fallback_report_ids", [])),
        "withheld": len(after.get("withheld_report_ids", [])),
        "failures": len(after.get("failure_queue", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely merge raw source state into the CWK Wiki manifest.")
    parser.add_argument("--mirror-root", default=str(DEFAULT_MIRROR))
    parser.add_argument("--check", action="store_true", help="Report drift without writing it.")
    parser.add_argument("--manifest-out", default="")
    args = parser.parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    result = reconcile_file(mirror, write=not args.check)
    if args.manifest_out:
        Path(args.manifest_out).expanduser().resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if args.check and result["changed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
