#!/usr/bin/env python3
"""Read-only RT-052 KB operations status projection.

The survey accepts already-built backends so its caller controls the storage
topology. It projects registry data through a small allowlist and never
imports the token-management face or a KB write primitive.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from kb_gateway import GatewayError, LEXICAL_READINESS_REL, load_index
from kb_ledger import parse_iso
from kb_lexical import corpus_digest, eligible_rows
from kb_storage import NotFound, StorageError, build_backend, close_backend

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
            state = "revoked" if record.get("revoked_at") else ("expired" if expiry <= now else "active")
            entry = {"token_id_suffix": str(record.get("token_id", ""))[-8:], "agent_binding_id": str(record.get("agent_binding_id", "")), "kb_ids": sorted(str(x) for x in record.get("kb_ids", [])), "created_at": str(record.get("created_at", "")), "expires_at": str(record["expires_at"]), "remaining_days": max(0, (expiry - now).days), "status": state}
            out.append({key: entry[key] for key in TOKEN_ALLOW})
        except (KeyError, ValueError, TypeError):
            return [], "registry_unavailable"
    return out, None


def lexical_status(backend: Any, index: Mapping[str, Any]) -> tuple[str, str | None, bool | None]:
    """Read only the small readiness projection, never the lexical index."""
    try:
        data = json.loads(backend.read(LEXICAL_READINESS_REL).decode("utf-8"))
    except NotFound:
        return "unknown", None, None
    except (StorageError, ValueError, UnicodeDecodeError, KeyError, TypeError):
        return "corrupt", None, None
    required = ("generation", "corpus_digest", "engine", "coverage_complete", "excluded_counts")
    if not isinstance(data, Mapping) or not all(key in data for key in required):
        return "corrupt", None, None
    rows = eligible_rows((key, value.version, value.sha256, value.status) for key, value in index.items())
    up_to_date = str(data["corpus_digest"]) == corpus_digest(rows)
    return ("ready" if up_to_date else "stale"), str(data["generation"])[-8:], up_to_date


def refresh_projection(log_dir: Path | None, kb_ids: Sequence[str]) -> dict:
    if log_dir is None:
        return {kb: {"status": "never_run"} for kb in kb_ids}
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
    results = payload.get("libraries", payload.get("results", {}))
    if not isinstance(results, Mapping):
        results = {}
    state = "current" if date == _now().date().isoformat() else "stale"
    return {kb: {"status": state, "date": date, "ok": bool((results.get(kb) or {}).get("ok")), "duration": (results.get(kb) or {}).get("duration")} for kb in kb_ids}


def status(backends: Mapping[str, Any], registry: Path, *, selected: Sequence[str] = (), mounted: Sequence[str] | None = None, log_dir: Path | None = None) -> tuple[dict, int]:
    """Survey injected backends; mounted describes availability, selected narrows output."""
    mounted_ids = sorted(set(mounted if mounted is not None else backends))
    chosen = sorted(set(selected or mounted_ids))
    libraries, complete = [], True
    for kb in chosen:
        backend = backends.get(kb)
        try:
            if backend is None:
                raise StorageError("unavailable")
            index = load_index(backend)
            lex, generation, fresh = lexical_status(backend, index)
            libraries.append({"kb_id": kb, "total": len(index), "readable_total": sum(1 for row in index.values() if row.status in ("ok", "placeholder")), "lexical_status": lex, "generation_suffix": generation, "up_to_date": fresh, "error": None})
        except (GatewayError, StorageError, OSError, ValueError, TypeError, UnicodeDecodeError):
            complete = False
            libraries.append({"kb_id": kb, "total": None, "readable_total": None, "lexical_status": None, "generation_suffix": None, "up_to_date": None, "error": "source_unavailable"})
    tokens, registry_error = registry_projection(registry, _now())
    return ({"schema": "cwk.kb.ops-status.v1", "ok": registry_error is None, "complete": complete, "libraries": libraries, "tokens": tokens if registry_error is None else [], "registry_status": registry_error or "available", "mount_differences": {"mounted_not_selected": sorted(set(mounted_ids) - set(chosen)), "expected_not_mounted": sorted(set(chosen) - set(mounted_ids))}, "refresh": refresh_projection(log_dir, chosen)}, 2 if registry_error else 0)


def _mount_names(root: Path) -> list[str]:
    try:
        return sorted(child.name for child in root.iterdir() if child.is_dir())
    except OSError:
        return []


def _build_local_backends(root: Path) -> tuple[dict[str, Any], list[str]]:
    mounts = _mount_names(root)
    return {kb: build_backend("local", root=root / kb) for kb in mounts}, mounts


def _split_mounts(raw: str) -> list[str]:
    return sorted({item.strip() for item in raw.split(",") if item.strip()})


def _build_nas_backends(mounts: Sequence[str]) -> dict[str, Any]:
    return {kb: build_backend("nas", prefix=kb) for kb in mounts}


def _close_all(backends: Mapping[str, Any]) -> None:
    seen: set[int] = set()
    for backend in backends.values():
        if id(backend) not in seen:
            seen.add(id(backend))
            close_backend(backend)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KB read-only operations status")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("status")
    cmd.add_argument("--json", action="store_true")
    cmd.add_argument("--backend", choices=("local", "nas"), default="local")
    cmd.add_argument("--root", default=str(Path.home() / "CWK" / "libraries"))
    cmd.add_argument("--registry", default=str(Path.home() / "CWK" / "ops" / "tokens.json"))
    cmd.add_argument("--log-dir")
    cmd.add_argument("--kb", action="append", default=[])
    cmd.add_argument("--mounts", default="")
    args = parser.parse_args(argv)
    mounts, backends = _split_mounts(args.mounts), {}
    if args.backend == "nas" and not mounts:
        parser.error("--backend nas requires --mounts a,b,c")
    try:
        if args.backend == "local":
            backends, mounted = _build_local_backends(Path(args.root))
        else:
            backends, mounted = _build_nas_backends(mounts), mounts
        payload, code = status(backends, Path(args.registry), selected=args.kb, mounted=mounted, log_dir=Path(args.log_dir) if args.log_dir else (Path(args.root) / "ops" if args.backend == "local" else None))
        print(json.dumps(payload, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True))
        return code
    except (StorageError, OSError, ValueError, TypeError, UnicodeDecodeError):
        print(json.dumps({"schema": "cwk.kb.ops-status.v1", "ok": False, "error": "source_unavailable"}, sort_keys=True))
        return 2
    finally:
        _close_all(backends)


if __name__ == "__main__":
    raise SystemExit(main())
