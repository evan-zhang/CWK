#!/usr/bin/env python3
"""RT-051 P2: the thin read-side client behind kb_wizard's v2 verbs.

One function, ``call``: build a GET for a ``/v2/kb/*`` operation, send it
with the token from the environment, and validate the answer against the
contract the gateway itself declares.  No storage imports, no backend
construction, no second protocol implementation — the gateway is the only
authority on its own shapes, this module just refuses to misread them.

Connection config comes from the environment
(``CWK_KB_GW_URL`` + ``CWK_KB_GW_TOKEN``), never from argv: the command
line is world-readable in the process table, and a URL that carries a
cursor or ref must not land in a shell history either (C05).

Exit-code semantics (C05): 0 success; 1 remote/permission/budget failure;
2 request or protocol error.  :meth:`ClientError.exit_code` encodes the
split so every verb shares it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional

ENV_URL = "CWK_KB_GW_URL"
ENV_TOKEN = "CWK_KB_GW_TOKEN"
TOKEN_HEADER = "X-KB-Token"
DEFAULT_TIMEOUT = 5.0

OPERATIONS = (
    "capabilities",
    "list",
    "search",
    "resolve",
    "inspect",
    "read",
    "continue",
    "renew",
)


class ClientError(Exception):
    """A structured failure: HTTP status, machine code, message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def exit_code(self) -> int:
        """0/1/2 (C05)：2 = 请求/协议类（改调用方可解），1 = 其余。

        本地拒绝（status=0：缺连接配置、协议不识别、未知操作）和 HTTP 400
        都是调用方可修的 → 2；unreachable 是基础设施问题、401/403 是权限、
        5xx 是远端——都不是改请求能解的 → 1。
        """
        if self.status == 400:
            return 2
        if self.status == 0 and self.code != "unreachable":
            return 2
        return 1


def connection_from_env(env: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    source = os.environ if env is None else env
    base = (source.get(ENV_URL) or "").strip().rstrip("/")
    token = (source.get(ENV_TOKEN) or "").strip()
    return base, token


def call(
    op: str,
    params: Mapping[str, object],
    *,
    env: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """GET ``/v2/kb/<op>`` and return the validated success payload.

    Raises :class:`ClientError` for every non-success: transport failure,
    non-JSON body, HTTP error, or an ``ok:false`` body.  The error message
    never embeds the URL — query strings carry refs/cursors, and those do
    not belong in stderr either (C04).
    """
    if op not in OPERATIONS:
        raise ClientError(0, "bad_request", f"未知 v2 操作 {op!r}")
    base, token = connection_from_env(env)
    if not base or not token:
        raise ClientError(
            0,
            "missing_connection",
            f"缺少受控连接配置：需要环境变量 {ENV_URL} 与 {ENV_TOKEN}"
            "（由宿主注册注入，不走命令行）",
        )
    query = urllib.parse.urlencode(
        {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
    )
    url = f"{base}/v2/kb/{op}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - best-effort body for diagnostics
            body = b""
    except (urllib.error.URLError, OSError) as exc:
        raise ClientError(0, "unreachable", f"网关不可达：{exc}") from None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ClientError(status, "protocol", "网关响应不是 JSON") from None
    if not isinstance(payload, dict):
        raise ClientError(status, "protocol", "网关响应不是 JSON 对象")
    if status != 200 or not payload.get("ok"):
        err = payload.get("error")
        if isinstance(err, dict):
            raise ClientError(
                status,
                str(err.get("code") or "error"),
                str(err.get("message") or "网关返回非成功响应"),
            )
        raise ClientError(status, "error", "网关返回非成功响应")
    return payload
