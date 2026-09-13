"""RT-055 shadow reconciliation runner.

Reads the authorized query batch on OPS, sends each query to the legacy
read-only gateway and the new retrieval service, and stores only aggregate
safe comparison fields plus document identifiers. Query text and response
content never enter the output files or logs.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BANKS = ("cwork-3m", "docdb-touqian", "spbp-2027")
LEGACY_PORTS = {"cwork-3m": 8787, "docdb-touqian": 8788, "spbp-2027": 8789}
DEFAULT_CASES = "/Users/xgstudio/rt055-3eeb7c3e9d70/rt055-ac1ca0c7-6983-4f6e-91ce-8eb45e7673af/verifier/private-verified.json"
DEFAULT_OUTPUT = "/Users/xgstudio/rt055-production/shadow"


@dataclass(frozen=True)
class Case:
    bank: str
    query: str
    ordinal: int


def load_cases(path: str | Path) -> list[Case]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    libraries = payload.get("libraries") if isinstance(payload, dict) else None
    if not isinstance(libraries, dict):
        raise ValueError("shadow case file has no libraries")
    result: list[Case] = []
    for bank in BANKS:
        library = libraries.get(bank)
        rows = library.get("cases") if isinstance(library, dict) else None
        if not isinstance(rows, list):
            raise ValueError("shadow case library is invalid")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("query"), str):
                raise ValueError("shadow case row is invalid")
            ordinal = row.get("ordinal", len(result))
            if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                raise ValueError("shadow case ordinal is invalid")
            result.append(Case(bank, row["query"], ordinal))
    return result


def _json_request(request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            value = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("backend_request_failed") from None
    if not isinstance(value, dict):
        raise RuntimeError("backend_response_invalid")
    return status, value


def legacy_doc_ids(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("legacy_response_invalid")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("legacy_result_invalid")
        value = row.get("lineage_id", row.get("doc_id", row.get("id")))
        if not isinstance(value, str) or not value:
            raise RuntimeError("legacy_id_invalid")
        ids.append(value)
    return ids


def retrieval_doc_ids(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("hits")
    if not isinstance(rows, list):
        raise RuntimeError("retrieval_response_invalid")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("doc_id"), str) or not row["doc_id"]:
            raise RuntimeError("retrieval_id_invalid")
        ids.append(row["doc_id"])
    return ids


class ShadowRunner:
    def __init__(self, *, old_token: str, new_url: str, timeout: float = 30.0, top_k: int = 10) -> None:
        if not old_token:
            raise ValueError("old token is required")
        self.old_token = old_token
        self.new_url = new_url.rstrip("/") + "/query"
        self.timeout = timeout
        self.top_k = top_k

    def _legacy(self, case: Case) -> tuple[list[str], float]:
        params = urllib.parse.urlencode({"kb": case.bank, "q": case.query, "limit": str(self.top_k)})
        request = urllib.request.Request(
            f"http://127.0.0.1:{LEGACY_PORTS[case.bank]}/query?{params}",
            headers={"X-KB-Token": self.old_token, "Accept": "application/json"},
            method="GET",
        )
        started = time.monotonic()
        _, payload = _json_request(request, self.timeout)
        return legacy_doc_ids(payload), (time.monotonic() - started) * 1000

    def _retrieval(self, case: Case) -> tuple[list[str], float]:
        body = json.dumps({"bank": case.bank, "query": case.query, "top_k": self.top_k}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.new_url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        _, payload = _json_request(request, self.timeout)
        return retrieval_doc_ids(payload), (time.monotonic() - started) * 1000

    def run_case(self, case: Case, cycle: int) -> dict[str, Any]:
        legacy_ids: list[str] = []
        retrieval_ids: list[str] = []
        legacy_ms: float | None = None
        retrieval_ms: float | None = None
        errors: list[str] = []
        try:
            legacy_ids, legacy_ms = self._legacy(case)
        except RuntimeError:
            errors.append("legacy")
        try:
            retrieval_ids, retrieval_ms = self._retrieval(case)
        except RuntimeError:
            errors.append("retrieval")
        both_ok = not errors
        return {
            "cycle": cycle,
            "ordinal": case.ordinal,
            "bank": case.bank,
            "legacy_doc_ids": legacy_ids,
            "retrieval_doc_ids": retrieval_ids,
            "legacy_no_answer": not legacy_ids if "legacy" not in errors else None,
            "retrieval_no_answer": not retrieval_ids if "retrieval" not in errors else None,
            "no_answer_match": (not legacy_ids) == (not retrieval_ids) if both_ok else None,
            "doc_id_set_match": set(legacy_ids) == set(retrieval_ids) if both_ok else None,
            "legacy_took_ms": round(legacy_ms, 3) if legacy_ms is not None else None,
            "retrieval_took_ms": round(retrieval_ms, 3) if retrieval_ms is not None else None,
            "errors": errors,
        }


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    successful = [row for row in rows if not row["errors"]]
    no_answer_rows = [row for row in successful if row["no_answer_match"] is not None]
    set_rows = [row for row in successful if row["doc_id_set_match"] is not None]
    latencies = [row["retrieval_took_ms"] for row in successful if row["retrieval_took_ms"] is not None]
    return {
        "cases": len(rows),
        "successful_pairs": len(successful),
        "errors": sum(bool(row["errors"]) for row in rows),
        "no_answer_matches": sum(bool(row["no_answer_match"]) for row in no_answer_rows),
        "no_answer_comparisons": len(no_answer_rows),
        "doc_id_set_matches": sum(bool(row["doc_id_set_match"]) for row in set_rows),
        "doc_id_set_comparisons": len(set_rows),
        "retrieval_latency_avg_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "retrieval_latency_max_ms": round(max(latencies), 3) if latencies else None,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def run_cycle(runner: ShadowRunner, cases: list[Case], output: Path, cycle: int) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    records_path = output / "records.jsonl"
    records: list[dict[str, Any]] = []
    with records_path.open("a", encoding="utf-8") as handle:
        for case in cases:
            record = runner.run_case(case, cycle)
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
    summary = summarize(records)
    summary.update({"cycle": cycle, "updated_at": time.time()})
    atomic_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="RT-055 legacy/new shadow reconciler")
    parser.add_argument("--cases", default=os.environ.get("CWK_SHADOW_CASES", DEFAULT_CASES))
    parser.add_argument("--output", default=os.environ.get("CWK_SHADOW_OUTPUT", DEFAULT_OUTPUT))
    parser.add_argument("--new-url", default=os.environ.get("CWK_SHADOW_NEW_URL", "http://127.0.0.1:18887"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--duration", type=float, default=86400.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    token = os.environ.get("CWK_SHADOW_OLD_TOKEN", "")
    cases = load_cases(args.cases)
    runner = ShadowRunner(old_token=token, new_url=args.new_url, timeout=args.timeout, top_k=args.top_k)
    output = Path(args.output)
    started = time.monotonic()
    cycle = 0
    while True:
        run_cycle(runner, cases, output, cycle)
        cycle += 1
        if args.once or time.monotonic() - started >= args.duration:
            break
        time.sleep(max(0.0, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
