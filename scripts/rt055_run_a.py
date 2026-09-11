#!/usr/bin/env python3
"""RT-055 candidate A runner (impl role).

Launches one loopback OpenSearch per library, builds one adapter instance per
library (per-library rt055-<uuid>-c/p pairs), warms with neutral queries,
scores the frozen holdout via the shared adapter, records per-library
index/build/RSS, cleans only its own indices, and stops its services.
Aggregate-only output; no query/case/content ever reaches the result file.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import kb_retrieval_candidates as kbc  # noqa: E402
import kb_stage_b_poc as poc  # noqa: E402
import rt055_opslib as ops
import rt055_runtime as runtime  # noqa: E402

OS_VERSION = "3.3.2"
HEAP = "distribution-default"
TENANT = "rt055a"


def free_port(rng: random.Random) -> int:
    while True:
        port = rng.randint(39100, 39900)
        probe = subprocess.run(("/bin/sh", "-c", f"lsof -iTCP:{port} -sTCP:LISTEN"),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            return port


class MultiLibraryA:
    """Routes search/build per library to the owning adapter instance."""

    def __init__(self) -> None:
        self.instances: dict[str, kbc.OpenSearchCandidate] = {}
        self.stats_urls: dict[str, str] = {}

    def add(self, kb: str, instance: kbc.OpenSearchCandidate, stats_url: str) -> None:
        self.instances[kb] = instance
        self.stats_urls[kb] = stats_url

    def build(self, kb: str, documents, timeout: float) -> float:
        started = time.monotonic()
        self.instances[kb].build(documents, timeout=timeout)
        return time.monotonic() - started

    def search(self, query: str, kb_id: str, timeout: float = 30) -> list:
        return self.instances[kb_id].search(query, kb_id, timeout=timeout)

    def close(self) -> None:
        failures = 0
        for instance in self.instances.values():
            try:
                instance.close()
            except Exception:
                failures += 1
        if failures:
            raise kbc.CandidateError("cleanup failed; OPS reconciliation required")

    def index_bytes(self) -> dict[str, int]:
        result = {}
        for kb, url in self.stats_urls.items():
            with urllib.request.urlopen(url + "/_stats/store", timeout=30) as resp:
                payload = json.loads(resp.read())
            result[kb] = int(payload.get("_all", {}).get("primaries", {})
                             .get("store", {}).get("size_in_bytes", 0))
        return result


def ensure_icu_plugin() -> None:
    """Install from the official 3.3.2 distribution-build plugin artifact (the
    maven-central path the CLI defaults to has no 3.3.2 zip)."""
    plugin_dir = ROOT / "opensearch" / "plugins" / "analysis-icu"
    if plugin_dir.is_dir() and (plugin_dir / "plugin-descriptor.properties").is_file():
        return
    zip_path = ROOT / "downloads" / "analysis-icu-3.3.2.zip"
    if not zip_path.is_file():
        url = ("https://artifacts.opensearch.org/releases/plugins/"
               "analysis-icu/3.3.2/analysis-icu-3.3.2.zip")
        result = subprocess.run(("/usr/bin/curl", "-fsSL", "--retry", "3",
                                 "-o", str(zip_path), url), timeout=900)
        if result.returncode != 0 or not zip_path.is_file() or zip_path.stat().st_size < 1000:
            raise RuntimeError("icu_zip_download_failed")
    env = runtime.clean_env(ROOT)
    env["OPENSEARCH_JAVA_HOME"] = str(ROOT / "jdk" / "Contents" / "Home")
    env.pop("DISABLE_SECURITY_PLUGIN", None)
    env.pop("DISABLE_INSTALL_DEMO_CONFIG", None)
    result = subprocess.run(
        [str(ROOT / "opensearch" / "bin" / "opensearch-plugin"), "install", "--batch",
         "file://" + str(zip_path)],
        env=env, cwd=str(ROOT / "opensearch"), capture_output=True, timeout=900)
    if result.returncode != 0 or not (plugin_dir / "plugin-descriptor.properties").is_file():
        raise RuntimeError("icu_plugin_install_failed: "
                           + (result.stderr or result.stdout or b"").decode(errors="replace")[-200:])


def launch_opensearch(kb: str, port: int, log_path: Path, mode="run") -> tuple[subprocess.Popen, dict[str, str]]:
    data_dir = ROOT / ("data-" + mode) / f"a-{kb}"
    logs_dir = ROOT / "runtime-logs" / f"a-{kb}"
    data_dir.mkdir(parents=True, exist_ok=False)
    logs_dir.mkdir(parents=True, exist_ok=True)
    env = runtime.clean_env(ROOT)
    env["OPENSEARCH_JAVA_HOME"] = str(ROOT / "jdk" / "Contents" / "Home")
    env["OPENSEARCH_JAVA_OPTS"] = "-Djava.net.preferIPv4Stack=true"
    env["OPENSEARCH_TMPDIR"] = str(ROOT / "tmp")
    env.pop("DISABLE_SECURITY_PLUGIN", None)
    env.pop("DISABLE_INSTALL_DEMO_CONFIG", None)
    transport_port = free_port(random.Random())
    while transport_port == port:transport_port = free_port(random.Random())
    with log_path.open("wb") as log:
        proc = runtime.spawn(ROOT,
            [str(ROOT / "opensearch" / "bin" / "opensearch"),
             "-E", "network.host=127.0.0.1",
             "-E", "transport.host=127.0.0.1",
             "-E", f"transport.port={transport_port}",
             "-E", f"http.port={port}",
             "-E", "discovery.type=single-node",
             "-E", "plugins.security.disabled=true",
             "-E", f"path.data={data_dir}",
             "-E", f"path.logs={logs_dir}",
             "-E", "cluster.routing.allocation.disk.threshold_enabled=false"],
            stdout=log, stderr=subprocess.STDOUT,
            env=env, cwd=str(ROOT / "opensearch"), network_policy="inbound-only")

    base = f"http://127.0.0.1:{port}"
    try:
        ops.wait_for_http(base + "/", timeout=300, process=proc)
        with urllib.request.urlopen(base + "/", timeout=15) as resp:
            version = str(json.loads(resp.read()).get("version", {}).get("number", ""))
        if version != OS_VERSION:
            raise RuntimeError("opensearch_version_mismatch")
        return proc, {"base_url": base, "version": version}
    except BaseException:
        ops.stop_process(proc)
        raise


def verify_icu(base_url: str) -> None:
    with urllib.request.urlopen(base_url + "/_cat/plugins?format=json", timeout=15) as resp:
        plugins = json.loads(resp.read())
    # /_cat/plugins rows: "name" is the node name, "component" is the plugin name.
    names = {str(row.get(key) or "") for row in plugins if isinstance(row, dict)
             for key in ("name", "component")}
    if "analysis-icu" not in names:
        raise RuntimeError("analysis_icu_plugin_missing")


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "run"), required=True)
    parser.add_argument("--case-limit", type=int, default=0,
                        help="smoke only: cap cases per library")
    args = parser.parse_args()

    if (ROOT.is_symlink() or not ROOT.is_dir() or not ROOT.name.startswith("rt055-")
            or ROOT.stat().st_mode & 0o077 or not (ROOT / ".rt055-owned").is_file()):
        print("RUN_A: private_root_invalid")
        return 3
    rng = random.Random(secrets.randbits(64))
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    services: list[subprocess.Popen] = []
    result: dict = {"schema": "cwk.rt055.run-a.result.v1", "mode": args.mode,
                    "started_at": started}

    multi = MultiLibraryA()
    samplers = {}
    try:
        if args.mode == "run":
            runtime.claim_candidate(ROOT, "a")
            verification = ops.read_json(ROOT / "verifier" / "freeze-verification.json")
            if not verification.get("verified"):
                print("RUN_A: freeze verification not passed")
                return 3
            receipt = ops.read_json(ROOT / "freeze" / "freeze-receipt.json")
            if receipt["candidates"]["a"]["digests"]["code_digest"] != ops.file_manifest(
                    [ROOT / p for p in receipt["candidates"]["a"]["code_files"]], base=ROOT):
                print("RUN_A: candidate A code digest drifted from freeze")
                return 3
            corpus = ops.read_json(ROOT / "builder" / "private-corpus.json")
            verified = ops.read_json(ROOT / "verifier" / "private-verified.json")
            cases = load_cases(verified)
            participating = runtime.participating(ROOT)
        else:
            def smoke_doc(kb: str, n: int) -> dict:
                return {"doc_id": f"smoke:{kb}:{n}",
                        "title": f"RT055 Smoke Doc {kb} Alpha",
                        "filename": f"rt055-smoke-{kb}-alpha.md",
                        "body": "RT055 smoke alpha body 测试 正文 "
                                "reference-RT055-A-001 2026-09-11\n"
                                "| 列一 | 数值九千零一 |\n| 列二 | 42 |\n"
                                + "补充行 " * 400}
            corpus = {"libraries": {kb: [smoke_doc(kb, n) for n in (1, 2)]
                                    for kb in ops.LIBRARIES}}
            cases = [kbc.Case(kb, "reference-RT055-A-001", frozenset({f"smoke:{kb}:1"}), True)
                     for kb in ops.LIBRARIES]
            cases += [kbc.Case(kb, "smoke alpha body 测试", frozenset({f"smoke:{kb}:1"}), False)
                      for kb in ops.LIBRARIES]
            cases += [kbc.Case(kb, "zznosuchtermqzxvkj0001", frozenset(), False)
                      for kb in ops.LIBRARIES]
        if args.mode == "smoke": participating = list(ops.LIBRARIES)
        if args.mode == "smoke" and args.case_limit:
            cases = cases[:args.case_limit]

        multi = MultiLibraryA()
        pids: list[int] = []
        build_seconds: dict[str, float] = {}
        if args.mode == "run": runtime.verify_ready(ROOT, "a")
        else: ensure_icu_plugin()
        for kb in participating:
            port = free_port(rng)
            log_path = ROOT / "runtime-logs" / f"opensearch-{kb}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            proc, info = launch_opensearch(kb, port, log_path, args.mode)
            services.append(proc)
            pids.append(proc.pid)
            samplers[kb] = ops.RssSampler([proc.pid]); samplers[kb].start()
            verify_icu(info["base_url"])
            instance = kbc.OpenSearchCandidate(kbc.LoopbackJSON(info["base_url"]), TENANT)
            multi.add(kb, instance, info["base_url"])


        for kb in participating:
            build_start = time.monotonic()
            multi.build(kb, load_documents(corpus, kb), timeout=7200)
            build_seconds[kb] = time.monotonic() - build_start
        for kb in participating:
            for query in ops.WARMUP_QUERIES:
                multi.instances[kb].search(query, kb, timeout=30)
        gateway = runtime.gateway_probe(ROOT, "a", multi, participating)
        metrics = kbc.score_cases(multi, cases, timeout=30)
        store_bytes = multi.index_bytes()
        index_bytes = {kb: runtime.data_bytes(ROOT / ("data-" + args.mode) / f"a-{kb}")
                       for kb in participating}
        for kb in participating:
            if index_bytes[kb] <= 0 or store_bytes[kb] <= 0:
                raise RuntimeError("index_bytes_unmeasured")
        peak_rss = {kb:sampler.stop() for kb,sampler in samplers.items()}
        multi.close()
        leftovers_total = 0
        for kb, url in multi.stats_urls.items():
            with urllib.request.urlopen(url + "/_cat/indices/" + ",".join((multi.instances[kb].child_index, multi.instances[kb].parent_index)) + "?format=json",
                                        timeout=15) as resp:
                leftovers_total += len(json.loads(resp.read()))
        if leftovers_total:
            raise RuntimeError("temporary_indices_not_zero")
        result.update({
            "status": "OK", "mode": args.mode, "gateway_readiness": gateway,
            "libraries": {kb: {**metrics[kb],
                               "index_bytes": index_bytes[kb],
                               "build_seconds": round(build_seconds[kb], 3),
                               "peak_rss_bytes": peak_rss[kb]}
                          for kb in participating},
            "service": {"kind": "opensearch-native-3.3.2", "instances": len(services),
                        "heap": HEAP},
        })
        if args.mode == "run":
            ops.write_private_json(ROOT / "run-a" / "result.json", result)
            print("RUN_A OK", json.dumps({kb: {
                "recall": metrics[kb]["recall_at_10"], "exact": metrics[kb]["exact"],
                "no_answer": metrics[kb]["no_answer"]} for kb in participating}))
        else:
            ops.write_private_json(ROOT / "run-a" / "smoke-result.json", result)
            print("SMOKE_A OK")
        return 0
    except Exception as exc:  # noqa: BLE001

        result["status"] = "FAILED"
        result["error"] = type(exc).__name__
        try:
            ops.write_private_json(ROOT / ("run-a/smoke-result.json" if args.mode == "smoke"
                                           else "run-a/result.json"), result)
        except Exception:
            pass
        print("RUN_A FAILED", type(exc).__name__)
        return 2
    finally:
        for sampler in samplers.values():sampler.stop()
        try: multi.close()
        except Exception: pass
        for proc in services:
            ops.stop_process(proc)
        if args.mode == "smoke":
            for pattern in ("data-smoke/a-*",):
                subprocess.run(("/bin/rm", "-rf") + tuple(str(p) for p in ROOT.glob(pattern)),
                               check=False)


if __name__ == "__main__":
    raise SystemExit(main())
