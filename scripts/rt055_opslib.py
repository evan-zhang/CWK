#!/usr/bin/env python3
"""RT-055 OPS shared helpers. Executed only inside the rt055- 0700 root on OPS.

The source is public; its runtime artifacts are private. No query/expected/content values may
enter any file this module writes except the 0600 private artifacts explicitly
named by the runner programs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

LIBRARIES = ("cwork-3m", "docdb-touqian", "spbp-2027")
GATEWAY_PORTS = (8787, 8788, 8789)
ENV_NAMES = ("CWK_NAS_KB_HOST", "CWK_NAS_KB_CERT_SHA256", "CWK_NAS_KB_USER",
             "CWK_NAS_KB_PASSWORD", "CWK_NAS_KB_TIMEOUT", "CWK_NAS_KB_SHARE")
B_MANUAL_MAX_RUNES = 200_000
CORPUS_SCHEMA = "cwk.rt055.ops-private-corpus.v1"


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_text(text: str) -> str:
    return sha_bytes(text.encode("utf-8"))


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(paths: Sequence[Path], base: Path | None = None) -> str:
    """Canonical sha256 over sorted relative-name:sha256 lines."""
    rows = []
    for path in sorted(paths):
        name = str(path.relative_to(base)) if base else path.name
        rows.append(f"{name}:{sha_file(path)}")
    return sha_text("\n".join(rows))


def write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(data)
    path.chmod(0o600)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_gateway_processes() -> list[dict[str, Any]]:
    raw = subprocess.run(("/bin/ps", "-axo", "pid=,command="), timeout=30,
                         stdout=subprocess.PIPE, check=True).stdout.decode("utf-8", "replace")
    rows = []
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or "kb_gateway.py" not in parts[1]:
            continue
        argv = shlex.split(parts[1])
        try:
            prefix = argv[argv.index("--prefix") + 1]
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            continue
        rows.append({"pid": int(parts[0]), "prefix": prefix, "port": port,
                     "command_sha256": sha_text(parts[1])})
    return sorted(rows, key=lambda row: row["pid"])


def process_environment(pid: int, names: Iterable[str]) -> dict[str, str]:
    text = subprocess.run(("/bin/ps", "eww", "-p", str(pid), "-o", "command="),
                          timeout=30, stdout=subprocess.PIPE, check=True
                          ).stdout.decode("utf-8", "replace")
    result: dict[str, str] = {}
    for name in names:
        match = re.search(r"(?:^|\s)" + re.escape(name) + r"=([^\s]*)", text)
        if match:
            result[name] = match.group(1)
    return result


def gateway_source_envs() -> dict[str, dict[str, str]]:
    """Read-only extraction of NAS credentials from the running Gateway processes."""
    gateways = find_gateway_processes()
    if len(gateways) != 3 or {row["prefix"] for row in gateways} != set(LIBRARIES):
        raise RuntimeError("gateway_baseline_invalid")
    envs = {}
    for row in gateways:
        env = process_environment(row["pid"], ENV_NAMES)
        if not all(env.get(name) for name in ENV_NAMES[:4]):
            raise RuntimeError("source_environment_unavailable")
        envs[row["prefix"]] = env
    return envs


def gateway_health() -> dict[int, int]:
    """Probe the three unauthenticated /health endpoints (0 = failed)."""
    import urllib.request
    result: dict[int, int] = {}
    for port in GATEWAY_PORTS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
                result[port] = int(resp.status)
        except Exception:
            result[port] = 0
    return result


def extract_body(text: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i] == "---":
            head = "\n".join(lines[:i + 1]) + "\n"
            return text[len(head):] if text.startswith(head) else text
    return text


def snapshot_libraries(envs: dict[str, dict[str, str]], workdir: Path) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    """Read-only corpus snapshot with per-file sha verification (RT-054 mechanism).

    Returns ({kb: [{doc_id,title,filename,body}]}, metadata). No content outside OPS.
    """
    import kb_storage
    libraries: dict[str, list[dict]] = {}
    meta: dict[str, Any] = {}
    for kb in LIBRARIES:
        backend = kb_storage.FileStationBackend.from_env(envs[kb], prefix=kb, timeout=120)
        try:
            rows = json.loads(backend.read("_system/raw-index.json")).get("entries")
            if isinstance(rows, dict):
                entries = sorted((str(k), v) for k, v in rows.items() if isinstance(v, dict))
            elif isinstance(rows, list):
                entries = []
                for row in rows:
                    if isinstance(row, dict):
                        lineage = str(row.get("lineage_id") or row.get("id") or "")
                        if lineage:
                            entries.append((lineage, row))
                entries.sort()
            else:
                raise ValueError("raw_index_unreadable")
            docs: list[dict] = []
            excluded: dict[str, int] = {}
            for lineage, row in entries:
                status = str(row.get("status") or "unknown")
                recorded = str(row.get("sha256") or "")
                rel = str(row.get("path") or "")
                if status not in {"ok", "converted"} or not recorded:
                    excluded[status] = excluded.get(status, 0) + 1
                    continue
                if not rel:
                    raise ValueError("eligible_path_missing")
                data = backend.read(rel)
                if sha_bytes(data) != recorded:
                    raise ValueError("source_sha_mismatch")
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    excluded["invalid_utf8"] = excluded.get("invalid_utf8", 0) + 1
                    continue
                body = extract_body(text)
                if not body.strip():
                    excluded["empty_body"] = excluded.get("empty_body", 0) + 1
                    continue
                docs.append({"doc_id": lineage,
                             "title": str(row.get("title") or row.get("display_name") or ""),
                             "filename": PurePosixPath(rel).name,
                             "body": body})
            libraries[kb] = docs
            meta[kb] = {"documents": len(docs), "raw_index_rows": len(entries),
                        "excluded_counts": excluded}
        finally:
            backend.logout()
    return libraries, meta


def canonical_runes(doc: dict) -> int:
    return len(f"{doc['title']}\n{doc['filename']}\n\n{doc['body']}")


def nas_metadata_summary(env: dict[str, str]):
    """Read-only size/mtime fingerprint of existing KB metadata (RT-054 pattern)."""
    import kb_storage
    backend = kb_storage.FileStationBackend.from_env(env, prefix=env["__prefix"], timeout=120)
    try:
        rows: dict[str, dict] = {}
        for rel in ("kb.json", "_system/raw-index.json", "_system/lexical-index.json",
                    "_system/lexical-readiness.json", "_system/manifest.json"):
            try:
                if not backend.exists(rel):
                    continue
                remote = backend._remote(rel)
                payload = backend._get("entry.cgi", {
                    "api": "SYNO.FileStation.List", "version": "2", "method": "getinfo",
                    "path": json.dumps([remote]),
                    "additional": json.dumps(["size", "time", "type"])})
                files = payload.get("files") or []
                if len(files) == 1 and isinstance(files[0], dict):
                    additional = files[0].get("additional") if isinstance(files[0].get("additional"), dict) else {}
                    times = additional.get("time") if isinstance(additional.get("time"), dict) else {}
                    rows[rel] = {"size": int(additional.get("size") or files[0].get("size") or 0),
                                 "mtime": int(times.get("mtime") or files[0].get("mtime") or 0)}
            except Exception:
                rows[rel] = {"error": True}
        return {"sha256": sha_text(canonical_json(rows)), "files": sorted(rows)}
    finally:
        backend.logout()


class RssSampler:
    """Peak RSS sampler for a fixed set of pids (bytes)."""

    def __init__(self, pids: Sequence[int], interval: float = 2.0):
        self.pids = [int(p) for p in pids]
        self.interval = interval
        self.peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample_once(self) -> int:
        total = 0
        for pid in self.pids:
            try:
                out = subprocess.run(("/bin/ps", "-o", "rss=", "-p", str(pid)),
                                     timeout=10, stdout=subprocess.PIPE, check=False
                                     ).stdout.decode().strip()
                if out:
                    total += int(out.split()[0]) * 1024
            except Exception:
                continue
        return total

    def _run(self) -> None:
        while not self._stop.is_set():
            total = self._sample_once()
            if total:
                self.peak = max(self.peak, total)
                self.samples += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=self.interval * 3)
        total = self._sample_once()
        self.peak = max(self.peak, total)
        return self.peak


def wait_for_http(url: str, timeout: float, expect: tuple[int, ...] = (200,), process=None) -> None:
    import urllib.error
    import urllib.request
    deadline = time.monotonic() + timeout
    last = "not_tried"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("service_process_exited")
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in expect:
                    return
                last = f"status={resp.status}"
        except urllib.error.HTTPError as exc:
            last = f"http_error={exc.code}"
            if exc.code in expect:
                return
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(1.5)
    raise RuntimeError(f"service_not_ready last={last}")


def stop_process(proc: subprocess.Popen, grace: float = 20.0) -> None:
    if proc.poll() is not None:
        return
    if hasattr(proc,'_rt055_firewall'):
        import signal
        proc._rt055_controller_stop=True
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
    else:proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if hasattr(proc,'_rt055_firewall'):
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        else:proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


WARMUP_QUERIES = ("warmup alpha 0001", "warmup beta 0002", "warmup gamma 0003",
                  "warmup delta 0004", "warmup epsilon 0005", "warmup zeta 0006")
