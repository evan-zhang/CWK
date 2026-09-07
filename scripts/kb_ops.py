#!/usr/bin/env python3
"""Read-only RT-052 KB operations status projection.

This intentionally reads registry JSON directly and emits a small allowlist;
it never imports the token management face or a KB write primitive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from kb_gateway import LEXICAL_READINESS_REL, RAW_INDEX_REL, load_index
from kb_ledger import parse_iso
from kb_lexical import corpus_digest, eligible_rows
from kb_storage import LocalFSBackend, NotFound, StorageError

TOKEN_ALLOW = {"token_id_suffix", "agent_binding_id", "kb_ids", "created_at", "expires_at", "remaining_days", "status"}
_REFRESH = re.compile(r"^kb-refresh-(\d{4}-\d{2}-\d{2})\.json$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def registry_projection(path: Path, now: datetime) -> tuple[list[dict], str | None]:
    try:
        raw = json.loads(path.read_text("utf-8"))
        records = raw["records"]
        if not isinstance(records, list):
            raise ValueError("records")
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return [], "registry_unavailable"
    out = []
    for record in records:
        try:
            expiry = parse_iso(str(record["expires_at"]))
            status = "revoked" if record.get("revoked_at") else ("expired" if expiry <= now else "active")
            out.append({"token_id_suffix": str(record.get("token_id", ""))[-8:],
                        "agent_binding_id": str(record.get("agent_binding_id", "")),
                        "kb_ids": sorted(str(x) for x in record.get("kb_ids", [])),
                        "created_at": str(record.get("created_at", "")), "expires_at": str(record["expires_at"]),
                        "remaining_days": max(0, (expiry - now).days), "status": status})
        except (KeyError, ValueError, TypeError):
            return [], "registry_unavailable"
    return out, None


def lexical_status(backend, index) -> tuple[str, str | None, bool | None]:
    try:
        data = json.loads(backend.read(LEXICAL_READINESS_REL).decode("utf-8"))
    except NotFound:
        return "unknown", None, None
    except (StorageError, ValueError, UnicodeDecodeError, KeyError):
        return "corrupt", None, None
    required = ("generation", "corpus_digest", "engine", "coverage_complete", "excluded_counts")
    if not all(k in data for k in required):
        return "corrupt", None, None
    rows = eligible_rows((key, value.version, value.sha256, value.status) for key, value in index.items())
    current = corpus_digest(rows)
    up_to_date = str(data["corpus_digest"]) == current
    return ("ready" if up_to_date else "stale"), str(data["generation"])[-8:], up_to_date


def refresh_projection(log_dir: Path, kb_ids: Sequence[str]) -> dict:
    candidates = []
    try:
        for child in log_dir.iterdir():
            hit = _REFRESH.match(child.name)
            if hit:
                datetime.strptime(hit.group(1), "%Y-%m-%d")
                candidates.append((hit.group(1), child))
    except OSError:
        candidates = []
    if not candidates:
        return {kb: {"status": "never_run"} for kb in kb_ids}
    date, latest = max(candidates)
    try:
        payload = json.loads(latest.read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {kb: {"status": "error"} for kb in kb_ids}
    today = _now().date().isoformat()
    results = payload.get("libraries", payload.get("results", {}))
    if not isinstance(results, Mapping):
        results = {}
    state = "current" if date == today else "stale"
    return {kb: {"status": state, "date": date, "ok": bool((results.get(kb) or {}).get("ok")),
                 "duration": (results.get(kb) or {}).get("duration")} for kb in kb_ids}


def status(root: Path, registry: Path, *, selected: Sequence[str] = (), expected_mounts: Sequence[str] = (),
           log_dir: Path | None = None) -> tuple[dict, int]:
    mounted = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    chosen = sorted(set(selected or mounted))
    libraries = []
    complete = True
    for kb in chosen:
        try:
            index = load_index(LocalFSBackend(root / kb))
            lex, generation, fresh = lexical_status(LocalFSBackend(root / kb), index)
            libraries.append({"kb_id": kb, "total": len(index),
                              "readable_total": sum(1 for row in index.values() if row.status in ("ok", "placeholder")),
                              "lexical_status": lex, "generation_suffix": generation, "up_to_date": fresh,
                              "error": None})
        except Exception:  # output must remain safe and aggregate per library
            complete = False
            libraries.append({"kb_id": kb, "total": None, "readable_total": None, "lexical_status": "unknown",
                              "generation_suffix": None, "up_to_date": None, "error": "source_unavailable"})
    tokens, registry_error = registry_projection(registry, _now())
    expected = set(expected_mounts)
    mounts = {"mounted_not_selected": sorted(set(mounted) - set(chosen)),
              "expected_not_mounted": sorted(expected - set(mounted))}
    payload = {"schema": "cwk.kb.ops-status.v1", "ok": registry_error is None, "complete": complete,
               "libraries": libraries, "tokens": tokens if registry_error is None else [],
               "registry_status": registry_error or "available", "mount_differences": mounts,
               "refresh": refresh_projection(log_dir or root / "ops", chosen)}
    return payload, (2 if registry_error else 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KB read-only operations status")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("status")
    cmd.add_argument("--json", action="store_true")
    cmd.add_argument("--root", default=str(Path.home() / "CWK" / "libraries"))
    cmd.add_argument("--registry", default=str(Path.home() / "CWK" / "ops" / "tokens.json"))
    cmd.add_argument("--log-dir")
    cmd.add_argument("--kb", action="append", default=[])
    cmd.add_argument("--mounts", default="")
    args = parser.parse_args(argv)
    payload, code = status(Path(args.root), Path(args.registry), selected=args.kb,
                           expected_mounts=[x for x in args.mounts.split(",") if x],
                           log_dir=Path(args.log_dir) if args.log_dir else None)
    # JSON is the canonical safe output; text intentionally exposes only its allowlisted fields.
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
