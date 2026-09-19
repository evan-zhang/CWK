"""Threaded HTTP API for the retrieval engine."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..client import BackendError
from ..query import RetrievalQuery, UnknownBank
from ...kb_auth import authorize
from .contract import RequestError, error_response, parse_query_request, query_response


MAX_BODY_BYTES = 1024 * 1024


class RetrievalApplication:
    def __init__(self, engine: RetrievalQuery, *, top_k: int, banks: tuple[str, ...] | list[str]) -> None:
        self.engine = engine
        self.top_k = top_k
        self.banks = frozenset(banks)

    def query(self, payload: Any) -> tuple[int, dict[str, Any]]:
        started = time.monotonic()
        try:
            bank, query, top_k = parse_query_request(
                payload, registered_banks=self.banks, default_top_k=self.top_k
            )
        except LookupError:
            return HTTPStatus.NOT_FOUND, error_response("bank_not_found", "registered bank not found")
        except RequestError:
            return HTTPStatus.BAD_REQUEST, error_response("invalid_request", "invalid query request")
        try:
            hits = self.engine.query(bank, query, top_k=top_k)
        except UnknownBank:
            return HTTPStatus.NOT_FOUND, error_response("bank_not_found", "registered bank not found")
        except ValueError:
            return HTTPStatus.BAD_REQUEST, error_response("invalid_request", "invalid query request")
        except BackendError:
            return HTTPStatus.SERVICE_UNAVAILABLE, error_response("backend_unavailable", "retrieval backend unavailable")
        except Exception:
            # A programming/protocol failure must not become an empty answer.
            return HTTPStatus.SERVICE_UNAVAILABLE, error_response("backend_unavailable", "retrieval backend unavailable")
        return HTTPStatus.OK, query_response(hits, (time.monotonic() - started) * 1000)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        return HTTPStatus.OK, {"status": "ok"}

    def readyz(self) -> tuple[int, dict[str, Any]]:
        try:
            ready = self.engine.ready()
        except Exception:
            ready = False
        if ready:
            return HTTPStatus.OK, {"status": "ready"}
        return HTTPStatus.SERVICE_UNAVAILABLE, error_response("not_ready", "retrieval backend is not ready")


def make_handler(application: RetrievalApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CWKRetrieval/1"

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            if self.path == "/healthz":
                status, payload = application.healthz()
            elif self.path == "/readyz":
                status, payload = application.readyz()
            else:
                status, payload = HTTPStatus.NOT_FOUND, error_response("not_found", "route not found")
            self._write(status, payload)

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            if self.path != "/query":
                self._write(HTTPStatus.NOT_FOUND, error_response("not_found", "route not found"))
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._write(HTTPStatus.BAD_REQUEST, error_response("invalid_request", "invalid query request"))
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write(HTTPStatus.BAD_REQUEST, error_response("invalid_request", "invalid query request"))
                return
            bank = payload.get("bank") if isinstance(payload, dict) else ""
            client = str(self.client_address[0]) if getattr(self, "client_address", None) else ""
            refusal = authorize(self.headers, bank or "", endpoint="query", client=client)
            if refusal:
                self._write(*refusal)
                return
            status, response = application.query(payload)
            self._write(status, response)

        def log_message(self, format: str, *args: object) -> None:
            # Query paths and arguments may contain protected source text.
            return

    return Handler


class RetrievalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(application: RetrievalApplication, host: str, port: int) -> ThreadingHTTPServer:
    return RetrievalHTTPServer((host, port), make_handler(application))


def serve(application: RetrievalApplication, host: str, port: int) -> None:
    server = create_server(application, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
