#!/usr/bin/env python3
"""Build a safe changed-relative-paths manifest from file modification time."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def parse_since(value: str) -> float:
    return datetime.fromisoformat(value).astimezone().timestamp()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror-root", required=True)
    parser.add_argument("--prefix", default="wiki/")
    parser.add_argument("--modified-since", required=True, help="ISO-8601 local timestamp")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    mirror = Path(args.mirror_root).expanduser().resolve()
    since = parse_since(args.modified_since)
    paths = sorted(
        path.relative_to(mirror).as_posix()
        for path in mirror.rglob("*")
        if path.is_file()
        and path.relative_to(mirror).as_posix().startswith(args.prefix)
        and path.stat().st_mtime >= since
    )
    payload = {
        "schema_version": "cwk.changed_paths.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "modified_since": args.modified_since,
        "prefix": args.prefix,
        "changed_relative_paths": paths,
    }
    target = Path(args.output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "paths": len(paths)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
