#!/usr/bin/env python3
"""Audit that selected raw reports have complete immutable thread evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cwk_thread_timeline import audit


PROJECT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit CWK reply/approval timeline evidence.")
    parser.add_argument("--mirror-root", default=str(PROJECT / "knowledge" / "工作协同镜像"))
    parser.add_argument("--paths-manifest", required=True, help="JSON with changed_relative_paths from raw promotion.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    changed = json.loads(Path(args.paths_manifest).read_text(encoding="utf-8")).get("changed_relative_paths") or []
    raw_paths = [mirror / item for item in changed if item.startswith("raw/") and "/_system/" not in item and item.endswith(".md")]
    result = audit(mirror, raw_paths)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
