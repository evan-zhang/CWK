"""Small bounded OpenSearch REST client with no response-body logging."""

from __future__ import annotations

import base64
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BackendError(RuntimeError):
    """Base class for backend failures exposed as service 503 responses."""


class BackendUnavailable(BackendError):
    """The backend cannot serve a request or returned an unusable response."""


class BackendNotFound(BackendError):
    """The backend returned HTTP 404 for a resource lookup."""


class BackendProtocolError(BackendError):
    """The backend response violates the narrow adapter contract."""


# Compatibility name for callers that used the experiment's terminology.
OpenSearchError = BackendError


class OpenSearchClient:
    """Standard-library transport; credentials can only come from settings."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OpenSearch endpoint must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("OpenSearch endpoint must not contain credentials, query, or path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("OpenSearch timeout must be positive")
        if bool(username) != bool(password):
            raise ValueError("OpenSearch credentials must be supplied together")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._authorization = None
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self._authorization = f"Basic {token}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise BackendProtocolError("invalid backend path")
        budget = self.timeout if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise BackendProtocolError("invalid backend timeout")
        if isinstance(payload, bytes):
            body = payload
            content_type = "application/x-ndjson"
        elif payload is None:
            body = None
            content_type = "application/json"
        else:
            try:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise BackendProtocolError("invalid backend request") from exc
            content_type = "application/json"
        headers = {"Accept": "application/json", "Content-Type": content_type}
        if self._authorization:
            headers["Authorization"] = self._authorization
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=budget) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                code = exc.code
            finally:
                exc.close()
            if code == 404:
                raise BackendNotFound("backend resource not found") from None
            raise BackendUnavailable("backend request failed") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise BackendUnavailable("backend unavailable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BackendProtocolError("backend response too large")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackendProtocolError("backend response is not JSON") from None
        if not isinstance(value, dict):
            raise BackendProtocolError("backend response must be an object")
        return value

    def index_exists(self, index_name: str) -> bool:
        path = "/" + urllib.parse.quote(index_name, safe="")
        try:
            self.request("HEAD", path)
        except BackendNotFound:
            return False
        return True

    def is_ready(self, index_name: str) -> bool:
        try:
            health = self.request("GET", "/_cluster/health")
            if health.get("status") == "red":
                return False
            return self.index_exists(index_name)
        except BackendError:
            return False
