#!/usr/bin/env python3
"""Build the cloud-first LLM Wiki foundation for the CWK raw evidence corpus.

This intentionally follows the Karpathy/OpenKB split:
  raw/  = immutable evidence, never rewritten by this command
  wiki/ = generated and maintained knowledge/navigation layer

The local mirror is a build cache only.  Call cwk_sync_mirror_to_docdb.py after
this command so every generated artifact is also stored under the user's
personal DocDB `工作协同镜像` folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from cwk_ai_common import parse_frontmatter


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
RAW = MIRROR / "raw"
WIKI = MIRROR / "wiki"
SCHEMA_VERSION = "cwk.cloud_wiki.v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def discover_sources() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(RAW.rglob("*.md")):
        if "_system" in path.parts:
            continue
        source, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        # The 7 July baseline predates the normalized collector and uses CWork
        # field names; newer files use the normalized aliases.  Both are raw
        # evidence, so the wiki catalog must preserve either form.
        report_id = text(source.get("report_id") or source.get("reportRecordId"))
        if not report_id:
            raise RuntimeError(f"raw source is missing report_id: {path}")
        rows.append({
            "report_id": report_id,
            "title": text(source.get("title") or source.get("main") or source.get("reference_title")) or path.stem,
            "writer": text(source.get("writer") or source.get("writeEmpName")) or "未知",
            "created_at": text(source.get("create_time") or source.get("createTime")) or "未知时间",
            "source_lane": text(source.get("source_lane")) or "unknown",
            "raw_path": path.relative_to(MIRROR).as_posix(),
            "sha256": sha256(path),
        })
    duplicate_ids = [key for key, count in Counter(row["report_id"] for row in rows).items() if count > 1]
    if duplicate_ids:
        raise RuntimeError(f"duplicate report_id in raw truth layer: {duplicate_ids[:5]}")
    return rows


AGENTS = """# 工作协同 LLM Wiki Schema\n\n本目录是由 LLM 维护的工作协同知识层。`../raw/` 是唯一事实层：原文不可修改、不可被摘要替代。\n\n## 目录\n\n- `index.md`：内容目录，所有 Wiki 页面都必须有入口。\n- `log.md`：追加式运行日志。\n- `sources/`：每篇原文的可追溯摘要；只在证据充分时写入。\n- `topics/`：跨多篇来源的持续事项、决策脉络、风险和未闭环项。\n- `entities/`：反复出现且对工作协同有意义的人、组织、项目、系统或产品。\n- `_system/`：构建清单、质量门、问答契约和同步状态。\n\n## 强制规则\n\n1. 任何事实、决策、风险和行动项都必须附 `report_id` 与原文短引文。\n2. 不确定时写“待核实”，不得推断或补全。\n3. 原文之间有冲突时并列展示来源与时间，不擅自裁决。\n4. `raw/` 只增不改；Wiki 页面可以生成新版本，但不得覆盖历史证据。\n5. 只有中心且跨来源复现的概念才建立 topic/entity 页面，避免 `PC`、`交流` 等泛化页面。\n6. 问答时 topics/entities 只用于导航，最终事实必须回读 raw 或使用经 raw 校验的逐字引文；证据不足必须拒答。\n7. 涉及审批、回复、转办或结束待办时，Wiki 只提供上下文；最终操作必须回读 CWork 原文并经用户确认。\n"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def build_catalog(rows: list[dict], shard_size: int) -> list[str]:
    changed: list[str] = []
    catalog = WIKI / "sources"
    for old in catalog.glob("catalog-*.md"):
        old.unlink()
    for start in range(0, len(rows), shard_size):
        shard = rows[start : start + shard_size]
        number = start // shard_size + 1
        lines = [
            f"# 原文目录 {number:03d}",
            "",
            "每项链接到不可变原文；本目录不是摘要，也不替代原文证据。",
            "",
        ]
        for row in shard:
            lines.append(
                f"- `{row['report_id']}` · [{row['title']}](../../{row['raw_path']}) · "
                f"{row['writer']} · {row['created_at']} · `{row['source_lane']}`"
            )
        path = catalog / f"catalog-{number:03d}.md"
        write(path, "\n".join(lines))
        changed.append(path.relative_to(MIRROR).as_posix())
    return changed


def main() -> None:
    global MIRROR, RAW, WIKI
    parser = argparse.ArgumentParser(description="Bootstrap the cloud-first CWK LLM Wiki structure.")
    parser.add_argument("--shard-size", type=int, default=100)
    parser.add_argument(
        "--mirror-root",
        default=os.environ.get("CWK_MIRROR_ROOT", str(MIRROR)),
        help="Local cache of the cloud mirror; final artifacts must be synced to DocDB.",
    )
    args = parser.parse_args()
    if args.shard_size < 1:
        raise SystemExit("--shard-size must be positive")

    MIRROR = Path(args.mirror_root).expanduser().resolve()
    RAW = MIRROR / "raw"
    WIKI = MIRROR / "wiki"
    if not RAW.is_dir():
        raise SystemExit(f"raw truth layer does not exist: {RAW}")

    rows = discover_sources()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    by_lane = Counter(row["source_lane"] for row in rows)
    by_month = Counter(row["created_at"][:7] for row in rows if len(row["created_at"]) >= 7)
    changed = build_catalog(rows, args.shard_size)

    static = {
        WIKI / "AGENTS.md": AGENTS,
        WIKI / "index.md": "\n".join([
            "# 工作协同 LLM Wiki", "",
            "这是工作协同原文的受约束导航与综合层。原文事实以 [`../raw/`](../raw/) 为准。", "",
            "## Sources", "",
            *[f"- [原文目录 {i:03d}](sources/catalog-{i:03d}.md)" for i in range(1, (len(rows) - 1) // args.shard_size + 2)],
            "", "## Topics", "", "- 尚未编译。由受证据约束的 AI 增量维护。", "",
            "## Entities", "", "- 尚未编译。由受证据约束的 AI 增量维护。", "",
            "## System", "", "- [_system/manifest.json](_system/manifest.json)", "- [_system/status.md](_system/status.md)",
        ]),
        WIKI / "log.md": "\n".join([
            "# Operations Log", "",
            f"## [{now}] bootstrap | cloud-first foundation", "",
            f"- Registered {len(rows)} immutable CWork raw sources.",
            "- Generated source catalogs, schema, and build manifest.",
            "- No source content was modified; no CWork mutation was called.",
        ]),
        WIKI / "_system" / "status.md": "\n".join([
            "# Cloud Wiki Status", "",
            f"- Schema: `{SCHEMA_VERSION}`",
            f"- Raw truth sources: **{len(rows)}**",
            f"- AI-compiled source summaries: **0**",
            f"- Topic pages: **0**",
            f"- Entity pages: **0**",
            "- Mode: `foundation_ready`",
            "- Persistence: all generated files must be synchronised to the personal DocDB mirror.",
        ]),
    }
    for path, content in static.items():
        write(path, content)
        changed.append(path.relative_to(MIRROR).as_posix())

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "source_count": len(rows),
        "source_hashes": {row["report_id"]: row["sha256"] for row in rows},
        "source_lanes": dict(sorted(by_lane.items())),
        "source_months": dict(sorted(by_month.items())),
        "compiled_report_ids": [],
        "changed_relative_paths": sorted(set(changed + ["wiki/_system/manifest.json"])),
    }
    manifest_path = WIKI / "_system" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Repair the migration-era paths in the raw manifest as part of the first
    # cloud-wiki build.  The physical source is unchanged; this is only the
    # authoritative registry that lets a future build prove where each raw
    # document lives.
    raw_manifest = {
        "schema_version": "cwk.raw-truth-source.v2",
        "generated_at": now,
        "source_of_truth": "工作协同镜像/raw",
        "record_count": len(rows),
        "records": [
            {
                "report_id": row["report_id"],
                "sha256": row["sha256"],
                "canonical_path": row["raw_path"],
            }
            for row in rows
        ],
    }
    raw_manifest_path = RAW / "_system" / "raw-manifest.json"
    raw_manifest_path.write_text(json.dumps(raw_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_count": len(rows), "catalog_shards": (len(rows) - 1) // args.shard_size + 1, "wiki_root": str(WIKI)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
