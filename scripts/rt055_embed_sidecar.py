#!/usr/bin/env python3
"""RT-055 loopback-only OpenAI-compatible embedding sidecar (candidate B native
model layer). Binds 127.0.0.1 only, keeps the HF cache inside the private
rt055- root, never logs inputs, and serves /v1/embeddings from a local
sentence-transformers model. Deleted with the private root at cleanup."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BATCH_TEXTS = 256
MAX_TEXT_CHARS = 20000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--hf-home", required=True)
    args = parser.parse_args()

    hf_home = Path(args.hf_home).resolve()
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    import urllib.request

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model, device="cpu", local_files_only=True)
    dim = int(model.get_sentence_embedding_dimension())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *log_args):  # silence all request logging
            return

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "model": args.model, "dimension": dim})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/embeddings":
                self._send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 * 1024 * 1024:
                    self._send(413, {"error": "payload_too_large"})
                    return
                payload = json.loads(self.rfile.read(length) or b"{}")
                texts = payload.get("input")
                if isinstance(texts, str):
                    texts = [texts]
                if (not isinstance(texts, list) or not texts
                        or len(texts) > MAX_BATCH_TEXTS
                        or not all(isinstance(t, str) and t for t in texts)
                        or any(len(t) > MAX_TEXT_CHARS for t in texts)):
                    self._send(400, {"error": "invalid_input"})
                    return
                started = time.time()
                vectors = model.encode(texts, batch_size=32, normalize_embeddings=True)
                data = [{"object": "embedding", "index": i,
                         "embedding": [float(x) for x in vectors[i]]}
                        for i in range(len(texts))]
                self._send(200, {
                    "object": "list", "model": args.model, "data": data,
                    "usage": {"prompt_tokens": sum(len(t) for t in texts),
                              "total_tokens": sum(len(t) for t in texts)},
                    "elapsed_ms": round((time.time() - started) * 1000, 1)})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": type(exc).__name__})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(json.dumps({"sidecar": "up", "model": args.model, "dimension": dim,
                      "port": args.port, "boot_id": uuid.uuid4().hex[:12]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
