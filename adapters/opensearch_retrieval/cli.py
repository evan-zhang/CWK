"""CLI entry point: ``build-index``, ``smoke-test`` and ``serve``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .client import BackendError, OpenSearchClient
from .config import ConfigError, Settings
from .indexing import IndexBuilder, ProjectionError, SourceDocument
from .query import RetrievalQuery
from .service import RetrievalApplication, serve


SYNTHETIC_DOC_ID = "synthetic-rt055-001"
SYNTHETIC_QUERY = "RT-055-SYNTH-001"


def synthetic_documents(bank: str) -> list[SourceDocument]:
    """Return a safe smoke fixture without reading any repository corpus."""
    return [SourceDocument(
        bank=bank,
        doc_id=SYNTHETIC_DOC_ID,
        title="Synthetic retrieval sample",
        filename="synthetic-rt055.txt",
        text="RT-055-SYNTH-001 is a synthetic retrieval fixture for smoke testing.",
    )]


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="OpenSearch endpoint; defaults to CWK_OPENSEARCH_URL")
    parser.add_argument("--index", dest="index_name", help="index name; defaults to CWK_OPENSEARCH_INDEX")
    parser.add_argument("--tenant", dest="tenant_id", help="tenant id; defaults to CWK_RETRIEVAL_TENANT")
    parser.add_argument("--banks", help="comma-separated registered bank ids")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CWK OpenSearch dual-channel retrieval")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-index", help="project explicit source JSON and upsert the index")
    _add_connection_options(build)
    build.add_argument("--input", type=Path, help="JSON array or object containing documents")
    build.add_argument("--synthetic", action="store_true", help="use the built-in synthetic smoke document")
    build.add_argument("--bank", default="cwork-3m", help="bank for --synthetic")

    smoke = sub.add_parser("smoke-test", help="run one positive or no-answer API-compatible query")
    _add_connection_options(smoke)
    smoke.add_argument("--bank", default="cwork-3m")
    smoke.add_argument("--query", default=SYNTHETIC_QUERY)
    smoke.add_argument("--expect-doc-id")
    smoke.add_argument("--expect-no-answer", action="store_true")
    smoke.add_argument("--top-k", type=int)

    serve_parser = sub.add_parser("serve", help="serve POST /query and health/readiness endpoints")
    _add_connection_options(serve_parser)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--top-k", type=int)
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    changes: dict[str, Any] = {}
    for argument, field in (("url", "opensearch_url"), ("index_name", "index_name"), ("tenant_id", "tenant_id"), ("host", "host"), ("port", "port"), ("top_k", "top_k")):
        value = getattr(args, argument, None)
        if value is not None:
            changes[field] = value
    if getattr(args, "banks", None) is not None:
        changes["banks"] = tuple(part.strip() for part in args.banks.split(",") if part.strip())
    return settings.with_overrides(**changes)


def _read_documents(path: Path) -> list[SourceDocument]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError("source JSON cannot be read") from exc
    if isinstance(value, dict):
        value = value.get("documents")
    if not isinstance(value, list):
        raise ProjectionError("source JSON must contain a documents array")
    return list(value)  # coerce_documents performs strict shape validation.


def _client(settings: Settings) -> OpenSearchClient:
    return OpenSearchClient(
        settings.opensearch_url,
        timeout=settings.request_timeout,
        username=settings.username,
        password=settings.password,
    )


def _engine(settings: Settings) -> RetrievalQuery:
    return RetrievalQuery(
        _client(settings), index_name=settings.index_name, tenant_id=settings.tenant_id, banks=settings.banks
    )


def run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.command == "build-index":
        if bool(args.input) == bool(args.synthetic):
            raise ConfigError("choose exactly one of --input and --synthetic")
        values = synthetic_documents(args.bank) if args.synthetic else _read_documents(args.input)
        stats = IndexBuilder(
            _client(settings), index_name=settings.index_name, tenant_id=settings.tenant_id, banks=settings.banks
        ).build(values)
        print(json.dumps({"status": "ok", **stats.as_dict()}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "smoke-test":
        hits = _engine(settings).query(args.bank, args.query, top_k=args.top_k or settings.top_k)
        matched = args.expect_doc_id is None or any(hit.doc_id == args.expect_doc_id for hit in hits)
        expected_no_answer = not args.expect_no_answer or not hits
        if not matched or not expected_no_answer:
            print(json.dumps({"status": "failed", "hit_count": len(hits), "no_answer": not hits}, sort_keys=True))
            return 1
        print(json.dumps({"status": "ok", "hit_count": len(hits), "no_answer": not hits}, sort_keys=True))
        return 0
    if args.command == "serve":
        engine = _engine(settings)
        serve(RetrievalApplication(engine, top_k=settings.top_k, banks=settings.banks), settings.host, settings.port)
        return 0
    raise ConfigError("unknown command")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (BackendError, ConfigError, ProjectionError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
