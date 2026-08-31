#!/usr/bin/env python3
"""Attach stable raw-source references to every CWK summary frontmatter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from cwk_ai_common import parse_frontmatter
from cwk_wiki_query import DEFAULT_MIRROR, resolve_raw_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def cloud_raw_ids(mirror: Path) -> dict[str, str]:
    path = mirror / "wiki" / "_system" / "cloud-objects.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {
        rel: str(row.get("file_id") or "")
        for rel, row in (payload.get("objects") or {}).items()
        if str(rel).startswith("raw/") and row.get("file_id")
    }


def replace_frontmatter(text: str, values: dict[str, str]) -> str:
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not match:
        raise ValueError("summary lacks frontmatter")
    lines = match.group(1).splitlines()
    keys = set(values)
    kept = [line for line in lines if not any(re.match(rf"^{re.escape(key)}\s*:", line) for key in keys)]
    insertion = next((i + 1 for i, line in enumerate(kept) if line.startswith("report_id:")), len(kept))
    new_lines = kept[:insertion] + [f'{key}: "{value}"' for key, value in values.items()] + kept[insertion:]
    return "---\n" + "\n".join(new_lines) + "\n---\n" + text[match.end():]


def apply_refs(mirror: Path) -> dict[str, Any]:
    mirror = mirror.resolve()
    raw_ids = cloud_raw_ids(mirror)
    changed: list[str] = []
    errors: list[dict[str, str]] = []
    for path in sorted((mirror / "wiki" / "summaries").glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            meta, _ = parse_frontmatter(text)
            report_id = str(meta.get("report_id") or path.stem)
            raw_path = resolve_raw_path(path, str(meta.get("source") or ""))
            if not raw_path.is_file():
                raise ValueError("linked raw file is missing")
            raw_rel = raw_path.relative_to(mirror).as_posix()
            values = {
                "source_report_id": report_id,
                "source_sha256": sha256_file(raw_path),
            }
            if raw_ids.get(raw_rel):
                values["source_cloud_file_id"] = raw_ids[raw_rel]
            updated = replace_frontmatter(text, values)
            if updated != text:
                atomic_text(path, updated)
                changed.append(path.relative_to(mirror).as_posix())
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "schema_version": "cwk.summary_source_refs.v1",
        "summary_count": len(list((mirror / "wiki" / "summaries").glob("*.md"))),
        "changed_count": len(changed),
        "changed_relative_paths": changed,
        "error_count": len(errors),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach stable raw references to CWK summaries.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = apply_refs(Path(args.mirror_root).expanduser().resolve())
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
