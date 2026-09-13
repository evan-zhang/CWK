"""Validated, environment-only service configuration.

No credential has a file or command-line default. The username/password are
read only from the process environment and are never included in repr output.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Mapping


DEFAULT_BANKS = ("cwork-3m", "docdb-touqian", "spbp-2027")
_BANK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,180}$")


class ConfigError(ValueError):
    """Configuration is missing or outside the supported contract."""


def _positive_int(value: str, name: str, *, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if result < 1 or result > maximum:
        raise ConfigError(f"{name} is outside the supported range")
    return result


def _endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("CWK_OPENSEARCH_URL must be an http(s) endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("CWK_OPENSEARCH_URL must not contain credentials or query data")
    if parsed.path not in {"", "/"}:
        raise ConfigError("CWK_OPENSEARCH_URL must not contain a path")
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError("CWK_OPENSEARCH_URL has an invalid port") from exc
    return value.rstrip("/")


def _banks(value: str) -> tuple[str, ...]:
    banks = tuple(part.strip() for part in value.split(",") if part.strip())
    if not banks or len(set(banks)) != len(banks) or any(not _BANK_RE.fullmatch(bank) for bank in banks):
        raise ConfigError("CWK_RETRIEVAL_BANKS must contain distinct safe bank ids")
    return banks


@dataclass(frozen=True)
class Settings:
    """Runtime settings with safe defaults and redacted representation."""

    opensearch_url: str = "http://127.0.0.1:9200"
    index_name: str = "cwk-retrieval-v1"
    tenant_id: str = "default"
    host: str = "127.0.0.1"
    port: int = 8787
    banks: tuple[str, ...] = DEFAULT_BANKS
    top_k: int = 10
    request_timeout: float = 10.0
    username: str | None = dataclasses.field(default=None, repr=False)
    password: str | None = dataclasses.field(default=None, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        url = _endpoint(env.get("CWK_OPENSEARCH_URL", cls.opensearch_url))
        banks = _banks(env.get("CWK_RETRIEVAL_BANKS", ",".join(DEFAULT_BANKS)))
        port = _positive_int(env.get("CWK_RETRIEVAL_PORT", str(cls.port)), "CWK_RETRIEVAL_PORT", maximum=65535)
        top_k = _positive_int(env.get("CWK_RETRIEVAL_TOP_K", str(cls.top_k)), "CWK_RETRIEVAL_TOP_K", maximum=100)
        try:
            timeout = float(env.get("CWK_OPENSEARCH_TIMEOUT", str(cls.request_timeout)))
        except ValueError as exc:
            raise ConfigError("CWK_OPENSEARCH_TIMEOUT must be a number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("CWK_OPENSEARCH_TIMEOUT must be positive")
        username = env.get("CWK_OPENSEARCH_USERNAME") or None
        password = env.get("CWK_OPENSEARCH_PASSWORD") or None
        if bool(username) != bool(password):
            raise ConfigError("OpenSearch username and password must be supplied together")
        return cls(
            opensearch_url=url,
            index_name=env.get("CWK_OPENSEARCH_INDEX", cls.index_name),
            tenant_id=env.get("CWK_RETRIEVAL_TENANT", cls.tenant_id),
            host=env.get("CWK_RETRIEVAL_HOST", cls.host),
            port=port,
            banks=banks,
            top_k=top_k,
            request_timeout=timeout,
            username=username,
            password=password,
        ).with_overrides()

    def with_overrides(self, **changes: object) -> "Settings":
        """Return a validated copy for explicit CLI overrides."""
        result = dataclasses.replace(self, **changes)
        _endpoint(result.opensearch_url)
        _banks(",".join(result.banks))
        if not _INDEX_RE.fullmatch(result.index_name) or not result.tenant_id or any(char.isspace() for char in result.tenant_id):
            raise ConfigError("index and tenant identifiers are invalid")
        if not 1 <= result.port <= 65535 or not 1 <= result.top_k <= 100:
            raise ConfigError("CLI settings are outside the supported range")
        if not result.index_name or not result.tenant_id:
            raise ConfigError("index and tenant must not be empty")
        return result
