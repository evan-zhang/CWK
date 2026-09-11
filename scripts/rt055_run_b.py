#!/usr/bin/env python3
"""RT-055 candidate B runner (impl role).

Runs one native WeKnora server + one loopback embedding sidecar per library
(physically isolated data planes so every per-library metric is a real
measurement). A single adapter instance consumes them through the OPS-owned
routing transport; the adapter contract (nonempty distinct participating KB bindings) is
preserved because every kb_id is a real KB on exactly one server. All model
traffic stays on 127.0.0.1 (SSRF_WHITELIST env is the native supported config
that permits the loopback model endpoint). Aggregate-only output.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import kb_retrieval_candidates as kbc  # noqa: E402
import kb_stage_b_poc as poc  # noqa: E402
import rt055_opslib as ops
import rt055_runtime as runtime  # noqa: E402

KB_PATH_RE = re.compile(r"^/api/v1/knowledge-bases/([A-Za-z0-9_-]+)")
KNOWLEDGE_PATH_RE = re.compile(r"^/api/v1/knowledge/([A-Za-z0-9_-]+)$")
BUILD_TIMEOUT = 7200.0
MODEL_NAME = "bge-m3"
EMBED_REPO = "BAAI/bge-m3"
MODEL_DIM = 1024
BASE = "/api/v1"


def free_port(rng: random.Random) -> int:
    while True:
        port = rng.randint(41100, 41900)
        probe = subprocess.run(("/bin/sh", "-c", f"lsof -iTCP:{port} -sTCP:LISTEN"),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            return port


class RoutingTransport:
    """OPS-owned authenticated transport: one logical service, N loopback servers.

    Routing is by real kb ids (path) and a knowledge-id table built from the
    import receipts. Also instruments per-kb import/ready timestamps.
    """

    def __init__(self) -> None:
        self.servers: dict[str, dict[str, Any]] = {}   # kb -> server info
        self.knowledge_owner: dict[str, str] = {}       # knowledge_id -> kb
        self.first_import: dict[str, float] = {}
        self.completed: dict[str, set] = {}
        self.imported: dict[str, set] = {}

    def add_server(self, kb: str, info: dict[str, Any]) -> None:
        self.servers[kb] = info
        self.completed[kb] = set()
        self.imported[kb] = set()

    def build_seconds(self) -> dict[str, float]:
        out = {}
        for kb, info in self.servers.items():
            ready = info.get("ready_ts")
            first = self.first_import.get(kb)
            out[kb] = round(ready - first, 3) if ready and first else 0.0
        return out

    def _route(self, path: str) -> tuple[str, str]:
        match = KB_PATH_RE.match(path)
        if match and match.group(1) in {info["kb_id"] for info in self.servers.values()}:
            for kb, info in self.servers.items():
                if info["kb_id"] == match.group(1):
                    return kb, path
            raise kbc.CandidateError("invalid request path")
        match = KNOWLEDGE_PATH_RE.match(path)
        if match:
            kb = self.knowledge_owner.get(match.group(1))
            if kb:
                return kb, path
        raise kbc.CandidateError("invalid request path")

    def __call__(self, method: str, path: str, payload: Any, timeout: float) -> Any:
        if not isinstance(path, str) or not path.startswith(BASE):
            raise kbc.CandidateError("invalid request path")
        kb, route_path = self._route(path)
        info = self.servers[kb]
        url = info["base_url"] + path
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False,
                                                      allow_nan=False).encode()
        request = urllib.request.Request(url, data=raw, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": "Bearer " + info["token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(kbc.MAX_RESPONSE_BYTES + 1)
            if len(body) > kbc.MAX_RESPONSE_BYTES:
                raise kbc.CandidateError("response size exceeded")
            data = json.loads(body) if body else {}
        except (TimeoutError, socket_timeout()):
            raise kbc.CandidateTimeout("request deadline exceeded") from None
        except urllib.error.HTTPError as exc:
            exc.close()
            raise kbc.CandidateError("native request failed") from None
        except (ValueError, OSError):
            raise kbc.CandidateError("native request failed") from None
        self._instrument(method, path, payload, data, kb)
        return data

    def _instrument(self, method: str, path: str, payload: Any, data: Any, kb: str) -> None:
        now = time.monotonic()
        if method == "POST" and path.endswith("/knowledge/manual"):
            identifier = (data.get("data") or {}).get("id")
            if isinstance(identifier, str):
                self.knowledge_owner[identifier] = kb
                self.imported[kb].add(identifier)
                self.first_import.setdefault(kb, now)
        elif method == "GET" and KNOWLEDGE_PATH_RE.match(path):
            identifier = KNOWLEDGE_PATH_RE.match(path).group(1)
            if ((data.get("data") or {}).get("parse_status") == "completed"
                    and identifier in self.imported.get(kb, set())):
                self.completed[kb].add(identifier)
                if len(self.completed[kb]) == len(self.imported[kb]) and self.imported[kb]:
                    self.servers[kb]["ready_ts"] = now


def socket_timeout():
    import socket
    return socket.timeout


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise kbc.CandidateError("redirect refused")


def api_call(base_url: str, method: str, path: str, payload: Any, token: str | None,
             timeout: float = 60) -> Any:
    runtime.require_loopback_url(base_url)
    if method == "POST" and path == BASE + "/models" and isinstance(payload, dict):
        runtime.require_loopback_url(payload.get("parameters", {}).get("base_url", ""))
    raw = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base_url + path, data=raw, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def launch_sidecar(port: int, hf_home: Path, log_path: Path, *, privacy_probe=False) -> subprocess.Popen:
    env = runtime.clean_env(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HOME"] = str(hf_home)
    with log_path.open("wb") as log:
        proc = runtime.spawn(ROOT,
            [str(ROOT / "sidecar" / "venv" / "bin" / "python"),
             str(HERE / "rt055_embed_sidecar.py"),
             "--port", str(port), "--model", EMBED_REPO,
             "--hf-home", str(hf_home), *(["--privacy-probe"] if privacy_probe else [])],
            stdout=log, stderr=subprocess.STDOUT, env=env)

    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                payload = json.loads(resp.read())
            if payload.get("ok") and payload.get("dimension") == MODEL_DIM:
                return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("sidecar_exited")
            time.sleep(3)
    ops.stop_process(proc)
    raise RuntimeError("sidecar_not_ready")


def launch_server(port: int, data_dir: Path, log_path: Path) -> subprocess.Popen:
    if not all((ROOT / "jieba" / name).is_file() for name in runtime.JIEBA_FILES):
        raise RuntimeError("native_dictionary_assets_missing")
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    env = runtime.clean_env(ROOT)
    env.update({
        "DB_DRIVER": "sqlite", "DB_PATH": str(data_dir / "app.db"),
        "RETRIEVE_DRIVER": "sqlite", "JIEBA_DICT_DIR": str(ROOT / "jieba"),
        "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(port),
        "GIN_MODE": "release", "LOG_LEVEL": "fatal", "LOG_PATH": "/dev/null",
        "LANGFUSE_ENABLED": "false", "OTEL_SDK_DISABLED": "true",
        "SSRF_WHITELIST": "127.0.0.1",
    })
    env.pop("REDIS_ADDR", None)
    for name in list(env):
        if name.startswith("LANGFUSE") and name != "LANGFUSE_ENABLED":
            env.pop(name)
    with log_path.open("wb") as log:
        proc = runtime.spawn(ROOT,[str(ROOT / "bin" / "weknora-server")],
                                stdout=log, stderr=subprocess.STDOUT,
                                env=env, cwd=str(ROOT / "weknora"))

    try:
        ops.wait_for_http(f"http://127.0.0.1:{port}{BASE}/knowledge-bases", timeout=180,
                          expect=(200, 401), process=proc)
    except Exception:
        ops.stop_process(proc)
        raise
    return proc


def setup_instance(kb: str, port: int, sidecar_port: int, data_dir: Path,
                   log_path: Path, rng: random.Random) -> dict[str, Any]:
    proc = launch_server(port, data_dir, log_path)
    try:
        base = f"http://127.0.0.1:{port}"
        password = "Rt055-" + secrets.token_hex(12)
        registration = api_call(base, "POST", BASE + "/auth/register",
                                {"username": f"rt055b{kb[:6]}", "email": f"rt055-{kb}@example.com",
                                 "password": password}, None)
        if not registration.get("success"):
            raise RuntimeError("register_failed")
        session = api_call(base, "POST", BASE + "/auth/login",
                           {"email": f"rt055-{kb}@example.com", "password": password}, None)
        token = str(session.get("token") or "")
        if not token:
            raise RuntimeError("login_failed")
        model = api_call(base, "POST", BASE + "/models",
                         {"name": MODEL_NAME, "display_name": MODEL_NAME,
                          "type": "Embedding", "source": "remote",
                          "parameters": {"base_url": f"http://127.0.0.1:{sidecar_port}/v1",
                                         "api_key": "rt055-local-only",
                                         "provider": "openai",
                                         "embedding_parameters": {"dimension": MODEL_DIM}}},
                         token)
        model_id = str((model.get("data") or {}).get("id") or "")
        if not model_id:
            raise RuntimeError("model_create_failed")
        # RETRIEVE_DRIVER=sqlite registers the tenant's built-in sqlite engine;
        # a nil vector_store_id resolves to the tenant effective engines (native
        # behavior, and API-created sqlite stores are not a supported engine type).
        kb_payload = {"name": f"rt055-b-{kb}", "embedding_model_id": model_id,
                      "indexing_strategy": {"vector_enabled": True, "keyword_enabled": True,
                                            "wiki_enabled": False, "graph_enabled": False}}
        created = api_call(base, "POST", BASE + "/knowledge-bases", kb_payload, token)
        kb_id = str((created.get("data") or {}).get("id") or "")
        if not kb_id:
            raise RuntimeError("kb_create_failed")
        return {"proc": proc, "base_url": base, "port": port, "token": token,
                "kb_id": kb_id, "data_dir": data_dir, "sidecar_port": sidecar_port}
    except BaseException:
        ops.stop_process(proc)
        raise


def load_documents(corpus: dict, kb: str):
    return [poc.SourceDocument(kb, doc["doc_id"], doc["title"], doc["filename"],
                               doc["body"], {})
            for doc in corpus["libraries"][kb]]


def load_cases(verified: dict) -> list[kbc.Case]:
    cases = []
    for kb, lib in verified["libraries"].items():
        for row in lib["cases"]:
            expected = frozenset(() if row["expected_outcome"] == "no_evidence"
                                 else (row["expected_doc_id"],))
            cases.append(kbc.Case(kb, row["query"], expected, bool(row["exact"])))
    return cases


def sqlite_bytes(data_dir: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = data_dir / f"app.db{suffix}"
        if path.is_file():
            total += path.stat().st_size
    return total


def log_leak_check(log_paths: list[Path], needles: list[str]) -> bool:
    for path in log_paths:
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for needle in needles:
            if needle in text:
                return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "run"), required=True)
    args = parser.parse_args()

    if (ROOT.is_symlink() or not ROOT.is_dir() or not ROOT.name.startswith("rt055-")
            or ROOT.stat().st_mode & 0o077 or not (ROOT / ".rt055-owned").is_file()):
        print("RUN_B: private_root_invalid")
        return 3
    rng = random.Random(secrets.randbits(64))
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    processes: list[subprocess.Popen] = []
    result: dict = {"schema": "cwk.rt055.run-b.result.v1", "mode": args.mode,
                    "started_at": started}
    tag = "smoke" if args.mode == "smoke" else "run"
    data_root = ROOT / f"data-{tag}"

    samplers = {}
    try:
        if args.mode == "run":
            runtime.claim_candidate(ROOT, "b")
            runtime.verify_ready(ROOT, "b")
            verification = ops.read_json(ROOT / "verifier" / "freeze-verification.json")
            if not verification.get("verified"):
                print("RUN_B: freeze verification not passed")
                return 3
            receipt = ops.read_json(ROOT / "freeze" / "freeze-receipt.json")
            if receipt["candidates"]["b"]["digests"]["code_digest"] != ops.file_manifest(
                    [ROOT / p for p in receipt["candidates"]["b"]["code_files"]], base=ROOT):
                print("RUN_B: candidate B code digest drifted from freeze")
                return 3
            corpus = ops.read_json(ROOT / "builder" / "private-corpus.json")
            verified = ops.read_json(ROOT / "verifier" / "private-verified.json")
            cases = load_cases(verified)
            participating = runtime.participating(ROOT)
        else:
            def smoke_doc(kb: str) -> dict:
                return {"doc_id": f"smoke:{kb}:1",
                        "title": f"RT055 Smoke Doc {kb} Alpha",
                        "filename": f"rt055-smoke-{kb}-alpha.md",
                        "body": "RT055 smoke alpha body 测试 正文 "
                                "reference-RT055-B-001 2026-09-11\n"
                                "| 列一 | 数值九千零一 |\n"
                                + "补充行 " * 80}
            corpus = {"libraries": {kb: [smoke_doc(kb)] for kb in ops.LIBRARIES}}
            cases = [kbc.Case(kb, "reference-RT055-B-001", frozenset({f"smoke:{kb}:1"}), True)
                     for kb in ops.LIBRARIES]
            cases += [kbc.Case(kb, "smoke alpha body 测试", frozenset({f"smoke:{kb}:1"}), False)
                      for kb in ops.LIBRARIES]
            cases += [kbc.Case(kb, "zznosuchtermqzxvkj0001", frozenset(), False)
                      for kb in ops.LIBRARIES]

        if args.mode == "smoke": participating = list(ops.LIBRARIES)
        transport = RoutingTransport()
        per_lib_pids: dict[str, list[int]] = {}
        log_paths: list[Path] = []
        for kb in participating:
            sidecar_port = free_port(rng)
            server_port = free_port(rng)
            sidecar_log = ROOT / "runtime-logs" / f"sidecar-{kb}.log"
            server_log = ROOT / "runtime-logs" / f"weknora-{kb}.log"
            sidecar_log.parent.mkdir(parents=True, exist_ok=True)
            sidecar = launch_sidecar(sidecar_port, ROOT / "sidecar" / "hf", sidecar_log)
            processes.append(sidecar)
            server = setup_instance(kb, server_port, sidecar_port,
                                    data_root / f"b-{kb}", server_log, rng)
            processes.append(server["proc"])
            transport.add_server(kb, server)
            per_lib_pids[kb] = [sidecar.pid, server["proc"].pid]
            samplers[kb] = ops.RssSampler(per_lib_pids[kb]); samplers[kb].start()
            log_paths += [sidecar_log, server_log]

        metrics, build_seconds, gateway_results = {}, {}, []
        build_started = time.monotonic()
        for kb in participating:
            candidate = kbc.WeKnoraCandidate(transport, {kb:transport.servers[kb]['kb_id']})
            started_library = time.monotonic()
            try: candidate.build(load_documents(corpus,kb),timeout=BUILD_TIMEOUT)
            except kbc.CandidateError:
                if not candidate_ready_to_poll(candidate): raise
                while time.monotonic()-started_library < BUILD_TIMEOUT and not candidate.ready:
                    time.sleep(5)
                    try: candidate.check_ready(120)
                    except kbc.CandidateError: continue
            if not candidate.ready: raise RuntimeError('candidate_b_build_timeout')
            build_seconds[kb]=time.monotonic()-started_library
            for query in ops.WARMUP_QUERIES: candidate.search(query,kb,timeout=30)
            gateway_results.append(runtime.gateway_probe(ROOT,'b',candidate,[kb]))
            metrics.update(kbc.score_cases(candidate,[c for c in cases if c.kb_id==kb],timeout=30))
        build_total=time.monotonic()-build_started
        peaks={kb:sampler.stop() for kb,sampler in samplers.items()}
        index_bytes={kb:runtime.data_bytes(transport.servers[kb]['data_dir']) for kb in participating}
        gateway={key:all(row[key] for row in gateway_results) for key in gateway_results[0]}
        for kb in participating:
            if index_bytes[kb] <= 0 or build_seconds[kb] <= 0:
                raise RuntimeError("resource_measurement_incomplete")
        if not log_leak_check(log_paths, list(ops.WARMUP_QUERIES)):
            raise RuntimeError("log_confidentiality_gate_failed")
        result.update({
            "status": "OK", "mode": args.mode, "gateway_readiness": gateway,
            "libraries": {kb: {**metrics[kb],
                               "index_bytes": index_bytes[kb],
                               "build_seconds": build_seconds[kb],
                               "peak_rss_bytes": peaks[kb]}
                          for kb in participating},
            "service": {"kind": "weknora-native-sqlite-sidecar",
                        "instances": len(participating) * 2,
                        "model": MODEL_NAME, "build_total_seconds": round(build_total, 3)},
            "log_gate": "pass",
        })
        out = ROOT / "run-b" / ("smoke-result.json" if args.mode == "smoke" else "result.json")
        ops.write_private_json(out, result)
        if args.mode == "run":
            print("RUN_B OK", json.dumps({kb: {
                "recall": metrics[kb]["recall_at_10"], "exact": metrics[kb]["exact"],
                "no_answer": metrics[kb]["no_answer"]} for kb in participating}))
        else:
            print("SMOKE_B OK")
        return 0
    except Exception as exc:  # noqa: BLE001

        result["status"] = "FAILED"
        result["error"] = type(exc).__name__
        try:
            ops.write_private_json(ROOT / "run-b" / ("smoke-result.json" if args.mode == "smoke"
                                                     else "result.json"), result)
        except Exception:
            pass
        print("RUN_B FAILED", type(exc).__name__)
        return 2
    finally:
        for sampler in samplers.values():sampler.stop()
        for proc in processes:
            ops.stop_process(proc)
        if args.mode == "smoke":
            subprocess.run(("/bin/rm", "-rf", str(data_root)), check=False)


def candidate_ready_to_poll(candidate: kbc.WeKnoraCandidate) -> bool:
    return bool(getattr(candidate, "_import_complete", False))


def load_documents_all(corpus: dict):
    docs = []
    for kb in corpus['libraries']:
        docs.extend(load_documents(corpus, kb))
    return docs


if __name__ == "__main__":
    raise SystemExit(main())
