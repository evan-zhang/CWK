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
import threading
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
import rt055_runtime as runtime
import rt055_log_firewall as firewall
import rt055_build_readiness as build_readiness
import rt055_candidate_workspace as workspace_api
import uuid
import rt055_window as window  # noqa: E402

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
        self.failed: dict[str, set] = {}
        self.observer=None
        self._import_locks = {}
        self._posted = {}
        self.max_inflight = {}
        self.post_count = {}
        self.completed_before_next_post = {}

    def add_server(self, kb: str, info: dict[str, Any]) -> None:
        self.servers[kb] = info
        self.completed[kb] = set()
        self.imported[kb] = set()
        self.failed[kb] = set()
        self._import_locks[kb] = threading.Lock()
        self._posted[kb] = set()
        self.max_inflight[kb] = 0
        self.post_count[kb] = 0
        self.completed_before_next_post[kb] = 0

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
            raise kbc.CandidateError('invalid request path')
        kb, _ = self._route(path)
        importing = method == 'POST' and path.endswith('/knowledge/manual')
        lock = self._import_locks[kb]
        if not lock.acquire(blocking=False):
            raise kbc.CandidateImportContractError('overlapping native request')
        try:
            if self.observer:self.observer()  # firewall before network I/O
            if importing:
                if self.failed[kb]:raise kbc.CandidateBuildFailed('native ingestion terminal failed')
                if self.imported[kb] != self.completed[kb]:
                    raise kbc.CandidateImportContractError('native import still pending')
                # Payload fingerprints are in-memory values, never receipts or logs.
                identity = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
                if identity in self._posted[kb]:
                    raise kbc.CandidateImportContractError('duplicate native import')
                self._posted[kb].add(identity)  # an ambiguous POST is never retried
                if self.post_count[kb]:self.completed_before_next_post[kb] += 1
                self.post_count[kb] += 1
                self.first_import.setdefault(kb, time.monotonic())
            return self._request(method, path, payload, timeout, kb)
        finally:
            lock.release()

    def _request(self, method, path, payload, timeout, kb):
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
            status=exc.code;exc.close()
            raise build_readiness.NativeHTTPError(status) from None
        except (ValueError, OSError):
            raise kbc.CandidateError("native request failed") from None
        self._instrument(method, path, payload, data, kb)
        if self.observer:self.observer()
        return data

    def _instrument(self, method: str, path: str, payload: Any, data: Any, kb: str) -> None:
        now = time.monotonic()
        if method == "POST" and path.endswith("/knowledge/manual"):
            identifier = (data.get("data") or {}).get("id")
            if isinstance(identifier, str):
                self.knowledge_owner[identifier] = kb
                self.imported[kb].add(identifier)
                self.max_inflight[kb] = max(self.max_inflight[kb], len(self.imported[kb]-self.completed[kb]-self.failed[kb]))
                self.first_import.setdefault(kb, now)
        elif method == "GET" and KNOWLEDGE_PATH_RE.match(path):
            identifier = KNOWLEDGE_PATH_RE.match(path).group(1)
            if (data.get('data') or {}).get('parse_status') in ('failed','error'):
                self.failed[kb].add(identifier)
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


def launch_sidecar(port: int, hf_home: Path, log_path: Path, *, privacy_probe=False, window_id=None, workspace=None) -> subprocess.Popen:
    if workspace is None:raise RuntimeError("candidate_workspace_required")
    workspace_api.validate(workspace,window_id=window_id)
    workspace_api.require_path(workspace,log_path,"logs")
    env = runtime.clean_env(workspace.base)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HOME"] = str(hf_home)
    proc = runtime.spawn(ROOT,
        [str(ROOT / "sidecar" / "venv" / "bin" / "python"),
         str(HERE / "rt055_embed_sidecar.py"),
         "--port", str(port), "--model", EMBED_REPO,
         "--hf-home", str(hf_home), *(["--privacy-probe"] if privacy_probe else [])],
        firewall_log_path=log_path, env=env, window_id=window_id, workspace=workspace)

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


def launch_server(port: int, data_dir: Path, log_path: Path, *, window_id=None, workspace=None) -> subprocess.Popen:
    if not all((ROOT / "jieba" / name).is_file() for name in runtime.JIEBA_FILES):
        raise RuntimeError("native_dictionary_assets_missing")
    if workspace is None:raise RuntimeError("candidate_workspace_required")
    workspace_api.validate(workspace,window_id=window_id)
    workspace_api.require_path(workspace,data_dir,"data")
    workspace_api.require_path(workspace,log_path,"logs")
    data_dir.mkdir(mode=0o700,parents=True, exist_ok=False)
    workspace_api.native_config(ROOT,workspace,data_dir,create=True)
    env = runtime.clean_env(workspace.base)
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
    proc = runtime.spawn(ROOT,[str(ROOT / "bin" / "weknora-server")],
                            firewall_log_path=log_path,
                            env=env, cwd=str(data_dir), window_id=window_id, workspace=workspace)

    try:
        ops.wait_for_http(f"http://127.0.0.1:{port}{BASE}/knowledge-bases", timeout=180,
                          expect=(200, 401), process=proc)
    except Exception:
        ops.stop_process(proc)
        raise
    return proc


def setup_instance(kb: str, port: int, sidecar_port: int, data_dir: Path,
                   log_path: Path, rng: random.Random, *, window_id=None, workspace=None) -> dict[str, Any]:
    proc = launch_server(port, data_dir, log_path, window_id=window_id, workspace=workspace)
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
    from rt055_scoring_input import load_cases as load_private_trials
    return load_private_trials(verified)


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


def _main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "run"), required=True)
    parser.add_argument('--window-id')
    args = parser.parse_args()
    if args.mode=='run' and not args.window_id:parser.error('--window-id required for formal run')
    w=window.directory(ROOT,args.window_id) if args.mode=='run' else ROOT
    attempt=None
    workspace=None
    corpus=None
    cases=[]
    log_scanned=False
    log_needles=[]
    completed={}
    log_root=ROOT/"runtime-logs"


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
            runtime.verify_ready(ROOT,"b",args.window_id)
            attempt=runtime.claim_candidate(ROOT, "b",args.window_id)
            workspace=workspace_api.create(ROOT,args.window_id,"b",attempt)
            log_root=workspace.base/"logs"
            runtime.verify_ready(ROOT, "b",args.window_id)
            verification = window.verification(ROOT,args.window_id)
            if not verification.get("verified"):
                print("RUN_B: freeze verification not passed")
                return 3
            receipt = window.receipt(ROOT,args.window_id)
            if receipt["candidates"]["b"]["digests"]["code_digest"] != ops.file_manifest(
                    [ROOT / p for p in receipt["candidates"]["b"]["code_files"]], base=ROOT):
                print("RUN_B: candidate B code digest drifted from freeze")
                return 3
            corpus = ops.read_json(ROOT / "builder" / "private-corpus.json")
            verified = ops.read_json(ROOT / "verifier" / "private-verified.json")
            cases = load_cases(verified)
            participating = runtime.participating(ROOT)
            result['window_id']=args.window_id
            for kb in participating:
                saved=window.library_result(ROOT,args.window_id,"b",kb)
                if saved is not None:completed[kb]=saved
            participating=[kb for kb in participating if kb not in completed]
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

        if args.mode == "smoke":
            participating = list(ops.LIBRARIES)
            workspace=workspace_api.create(ROOT,str(uuid.uuid4()),"b",str(uuid.uuid4()),synthetic=True)
            log_root=workspace.base/"logs"
        log_needles=workspace_api.needles(corpus,cases)+list(ops.WARMUP_QUERIES)+(workspace_api.needles(verified,[]) if args.mode=="run" else [])
        firewall.bind(workspace,log_needles)
        data_root=workspace.base/"data"
        transport = RoutingTransport()
        per_lib_pids: dict[str, list[int]] = {}
        log_paths: list[Path] = []
        for kb in participating:
            sidecar_port = free_port(rng)
            server_port = free_port(rng)
            sidecar_log = log_root / f"sidecar-{kb}.log"
            server_log = log_root / f"weknora-{kb}.log"
            sidecar_log.parent.mkdir(parents=True, exist_ok=True)
            sidecar = launch_sidecar(sidecar_port, ROOT / "sidecar" / "hf", sidecar_log, window_id=args.window_id if args.mode=="run" else None, workspace=workspace)
            processes.append(sidecar)
            server = setup_instance(kb, server_port, sidecar_port,
                                    data_root / f"b-{kb}", server_log, rng, window_id=args.window_id if args.mode=="run" else None, workspace=workspace)
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
            build_readiness.build_b(candidate,load_documents(corpus,kb),transport,kb,
                workspace.ledger.parent/'build-status.json',firewall.get(workspace),timeout=BUILD_TIMEOUT)
            build_seconds[kb]=time.monotonic()-started_library
            for query in ops.WARMUP_QUERIES: candidate.search(query,kb,timeout=30)
            gateway_results.append(runtime.gateway_probe(ROOT,'b',candidate,[kb]))
            rows=[c for c in cases if c.kb_id==kb]
            firewall.get(workspace).health()
            if args.mode=='run':
                metrics.update(window.score_library(ROOT,args.window_id,'b',kb,attempt,candidate,rows,timeout=30))
            else:
                metrics.update(kbc.score_library_cases(candidate,rows,kb,timeout=30))
            if args.mode=='run':window.library_scored(ROOT,args.window_id,'b',kb,metrics[kb])
        build_total=time.monotonic()-build_started
        peaks={kb:sampler.stop() for kb,sampler in samplers.items()}
        index_bytes={kb:runtime.data_bytes(transport.servers[kb]['data_dir'],exclude=('config','migrations')) for kb in participating}
        gateway_rows=gateway_results+[v['gateway_readiness'] for v in completed.values()]
        gateway={key:all(row[key] for row in gateway_rows) for key in gateway_rows[0]}
        for kb in participating:
            if index_bytes[kb] <= 0 or build_seconds[kb] <= 0:
                raise RuntimeError("resource_measurement_incomplete")
        for proc in processes:ops.stop_process(proc)
        firewall.finalize(workspace)
        workspace_api.scan(workspace,log_needles);log_scanned=True
        if not log_leak_check(log_paths, list(ops.WARMUP_QUERIES)):
            raise RuntimeError("log_confidentiality_gate_failed")
        if args.mode=='run':
            for kb,gate_result in zip(participating,gateway_results):
                window.library_complete(ROOT,args.window_id,'b',kb,{'metrics':{**metrics[kb],
                    'index_bytes':index_bytes[kb],'build_seconds':build_seconds[kb],'peak_rss_bytes':peaks[kb]},'gateway_readiness':gate_result})
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
        out = w / "run-b" / ("smoke-result.json" if args.mode == "smoke" else "result.json")
        if args.mode=='run':result['libraries']={kb:window.library_result(ROOT,args.window_id,'b',kb)['metrics'] for kb in runtime.participating(ROOT)}
        if args.mode=='run':window.write_once(out,result)
        else:ops.write_private_json(out, result)
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
        result["build_error"] = build_readiness.error_code(exc)
        try:
            if args.mode=='smoke':ops.write_private_json(ROOT/'run-b/smoke-result.json',result)
            elif attempt:window.write_once(w/'run-b/attempts'/attempt/'failure.json',result)
        except Exception:
            pass
        print("RUN_B FAILED", type(exc).__name__)
        return 2
    finally:
        for sampler in samplers.values():sampler.stop()
        for proc in processes:
            ops.stop_process(proc)
        if workspace is not None:
            failed=False
            try:
                try:firewall.finalize(workspace)
                except Exception:failed=True
                if not log_scanned and not (workspace.ledger.parent/'log-scan.json').exists():
                    try:workspace_api.scan(workspace,log_needles)
                    except Exception:failed=True
            finally:workspace_api.cleanup(workspace)
            if failed:raise firewall.FirewallError('UNVERIFIED')


def candidate_ready_to_poll(candidate: kbc.WeKnoraCandidate) -> bool:
    return bool(getattr(candidate, "_import_complete", False))


def load_documents_all(corpus: dict):
    docs = []
    for kb in corpus['libraries']:
        docs.extend(load_documents(corpus, kb))
    return docs


def main() -> int:
    try:return _main()
    except firewall.FirewallError:
        print('CANDIDATE_LOG_FIREWALL_FAILED')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
