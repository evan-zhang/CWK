#!/usr/bin/env python3
"""Safely materialize structured CWK history without publishing raw evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", value or "").strip("-")
    return value[:100] or "untitled"


def safe_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -*#|:：")
    return text[:limit]


def raw_metadata(run_dir: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for path in sorted((run_dir / "raw").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.match(r"---\n(.*?)\n---", text, re.S)
        fields: dict[str, str] = {}
        if match:
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip().strip('"')
        rid = fields.get("report_id") or path.name.split("-", 1)[0]
        result[rid] = fields
    return result


def bullet_lines(values: list[Any], empty: str = "暂无") -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = safe_text(value)
        if text and text not in cleaned:
            cleaned.append(text)
    return [f"- {value}" for value in cleaned[:8]] or [f"- {empty}"]


def append_section(path: Path, title: str, marker: str, section: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if marker in current:
            return False
        updated = current.rstrip() + "\n\n" + section.strip() + "\n"
    else:
        updated = f"# {safe_text(title, 120)}\n\n{section.strip()}\n"
    path.write_text(updated, encoding="utf-8")
    return True


def history_section(run_name: str, ext: dict[str, Any], meta: dict[str, str]) -> str:
    marker = f"<!-- run:{run_name} -->"
    lines = [
        marker,
        f"## {safe_text(meta.get('create_time') or '未知时间', 80)} · {safe_text(ext.get('change_type') or 'unknown', 40)}",
        "",
        f"- 汇报人：{safe_text(meta.get('writer') or '未知', 80)}",
        f"- 事件锚点：{safe_text(ext.get('event_anchor') or '未命名事项', 120)}",
        f"- 来源范围：{safe_text(ext.get('source_scopes') or meta.get('source_scopes') or '未知', 120)}",
        f"- 运行：`{run_name}`",
        "",
        "### 动作",
        "",
        *bullet_lines(ext.get("actions") or []),
        "",
        "### 风险与未闭环",
        "",
        *bullet_lines((ext.get("risks") or []) + (ext.get("open_loops") or [])),
        "",
        "### 决策",
        "",
        *bullet_lines(ext.get("decision_points") or []),
    ]
    return "\n".join(lines)


def event_section(run_name: str, data: dict[str, Any]) -> str:
    marker = f"<!-- run:{run_name} -->"
    lines = [
        marker,
        f"## 更新 · {run_name}",
        "",
        f"- 证据数：{len(data.get('related_raw_ids') or [])}",
        f"- 当前状态：{safe_text(data.get('current_state') or '暂无', 400)}",
        "",
        "### 待跟进",
        "",
        *bullet_lines(data.get("open_loops") or []),
        "",
        "### 决策与意见",
        "",
        *bullet_lines(data.get("decisions_and_opinions") or []),
        "",
        "### 证据 ID",
        "",
        *[f"- `{safe_text(rid, 80)}`" for rid in (data.get("related_raw_ids") or [])[:80]],
    ]
    return "\n".join(lines)


def entity_section(run_name: str, data: dict[str, Any]) -> str:
    marker = f"<!-- run:{run_name} -->"
    lines = [marker, f"## 更新 · {run_name}", ""]
    for item in (data.get("recent_activity") or [])[:30]:
        if isinstance(item, dict):
            lines.append(f"- `{safe_text(item.get('source_id'), 80)}` {safe_text(item.get('title'), 160)}")
    if len(lines) == 3:
        lines.append("- 暂无活动摘要。")
    return "\n".join(lines)


def rebuild_index(root: Path, output: Path, title: str) -> bool:
    entries = []
    for path in sorted(root.rglob("*.md")) if root.exists() else []:
        relative = Path(os.path.relpath(path, output.parent)).as_posix()
        entries.append(f"- [{path.stem}]({relative})")
    content = f"# {title}\n\n" + ("\n".join(entries) if entries else "- 暂无记录。") + "\n"
    if output.exists() and output.read_text(encoding="utf-8") == content:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    return True


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT))
    except ValueError:
        return str(path)


def materialize(run_dir: Path, mirror_root: Path | None = None) -> dict[str, Any]:
    if not run_dir.exists():
        raise SystemExit(f"run not found: {run_dir}")
    mirror_root = (mirror_root or Path(os.environ.get("CWK_MIRROR_ROOT", str(MIRROR)))).expanduser().resolve()
    metadata = raw_metadata(run_dir)
    changed: list[Path] = []
    counts = {"history_pages": 0, "event_pages": 0, "entity_pages": 0}

    for path in sorted((run_dir / "extracted").glob("*.json")):
        ext = load_json(path)
        ids = ext.get("source_ids") or []
        if not ids:
            continue
        rid = str(ids[0])
        meta = metadata.get(rid, {})
        title = safe_text(ext.get("title") or meta.get("title") or rid, 120)
        created = safe_text(meta.get("create_time") or "unknown", 10)
        month = created[:7] if re.fullmatch(r"\d{4}-\d{2}.*", created) else "unknown"
        output = mirror_root / "history" / month / f"{rid}-{slug(title)}.md"
        marker = f"<!-- run:{run_dir.name} -->"
        if append_section(output, title, marker, history_section(run_dir.name, ext, meta)):
            counts["history_pages"] += 1
            changed.append(output)

    for path in sorted((run_dir / "events").glob("*.json")):
        data = load_json(path)
        name = safe_text(data.get("event") or path.stem, 120)
        if not name or name in {"未命名事项", "New", "更新", "跟进"}:
            continue
        output = mirror_root / "events" / f"{slug(name)}.md"
        marker = f"<!-- run:{run_dir.name} -->"
        if append_section(output, name, marker, event_section(run_dir.name, data)):
            counts["event_pages"] += 1
            changed.append(output)

    for path in sorted((run_dir / "entities").glob("*.json")):
        data = load_json(path)
        name = safe_text(data.get("entity_name") or path.stem, 120)
        entity_type = slug(str(data.get("entity_type") or "unknown"))
        output = mirror_root / "entities" / entity_type / f"{slug(name)}.md"
        marker = f"<!-- run:{run_dir.name} -->"
        if append_section(output, name, marker, entity_section(run_dir.name, data)):
            counts["entity_pages"] += 1
            changed.append(output)

    event_index = mirror_root / "_index" / "event-index.md"
    entity_index = mirror_root / "_index" / "entity-index.md"
    history_index = mirror_root / "_index" / "history-index.md"
    if rebuild_index(mirror_root / "events", event_index, "工作协同事件索引"):
        changed.append(event_index)
    if rebuild_index(mirror_root / "entities", entity_index, "工作协同实体索引"):
        changed.append(entity_index)
    if rebuild_index(mirror_root / "history", history_index, "工作协同历史记录索引"):
        changed.append(history_index)
    manifest = {
        "schema_version": "cwk.safe_materialize.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_name": run_dir.name,
        "counts": counts,
        "mirror_root": str(mirror_root),
        "changed_files": [display_path(path) for path in changed],
        "changed_relative_paths": [str(path.relative_to(mirror_root)) for path in changed],
        "raw_published": False,
    }
    output = run_dir / "safe-materialize-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize safe structured CWK knowledge pages.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(MIRROR)))
    args = parser.parse_args()
    print(
        json.dumps(
            materialize(PROJECT / "runs" / args.run_name, Path(args.mirror_root)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
