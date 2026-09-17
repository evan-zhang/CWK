#!/usr/bin/env python3
"""RT-061: who holds this business AppKey — resolved through 玄关, identity only.

CWK authorizes libraries itself (``kb_authz.py``); from 玄关 it borrows exactly
one thing, the answer to "whose key is this".  Two read-only calls on the open
platform give it:

1. ``project/personal/getProjectId`` — the caller's personal space id;
2. ``project/list`` — every space the key can see.  The row whose id is the
   personal space id was created by the caller, so its ``createBy`` is the
   person id, ``creator`` the real name and ``corpId`` the organisation.

Verified on 2026-09-17 with two different keys of the same person: person id,
personal space id and name were identical, so the person id is a stable key
for authorization rows and a key rotation does not orphan anyone's grants.

What the space list is **not** used for: deciding access.  Everything else in
the ``project/list`` response is discarded here, because a CWK library is a
union of parts of many upstream spaces and no upstream ACL can express it.

The key is only ever a request header.  It never reaches a message, an
exception or stdout; the CLI takes the *name* of the environment variable
holding it (传变量名不传值), like every other tool in this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

DEFAULT_BASE = "https://sg-al-cwork-web.mediportal.com.cn/open-api"
BASE_ENV = "XG_OPEN_API_BASE"
PERSONAL_PROJECT_PATH = "/document-database/project/personal/getProjectId"
PROJECT_LIST_PATH = "/document-database/project/list"
PERSONAL_TYPE = "personal"
PROBE_LABEL = "xuanguan:project/personal/getProjectId+project/list"
WHOAMI_SCHEMA = "cwk.kb.identity-whoami.v1"
ERROR_SCHEMA = "cwk.kb.identity-error.v1"

#: 玄关 answers HTTP 200 for a rejected key and puts the verdict here
#: (observed: 1 = ok, 401 = "Token校验失败").
RESULT_OK = 1
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_NAME_CHARS = 64

#: Ids are 19-digit strings today.  Kept as strings end to end: they exceed
#: JavaScript's safe-integer range, so any numeric round trip corrupts them.
_ID = re.compile(r"\A[0-9]{1,32}\Z")

Transport = Callable[[str, Mapping[str, str], float], bytes]


class IdentityResolutionError(Exception):
    """The key could not be turned into a person.  Always a refusal."""


@dataclass(frozen=True)
class PersonIdentity:
    corp_id: str
    person_id: str
    name: str

    @property
    def principal(self) -> str:
        return f"person:{self.corp_id}:{self.person_id}"


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _data(send: Transport, url: str, app_key: str, timeout: float) -> object:
    try:
        raw = send(url, {"appKey": app_key, "Accept": "application/json"}, timeout)
    except urllib.error.HTTPError as exc:
        raise IdentityResolutionError(f"玄关返回 HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise IdentityResolutionError(f"玄关不可达：{type(exc).__name__}") from None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IdentityResolutionError("玄关返回的不是 JSON") from None
    if not isinstance(body, dict):
        raise IdentityResolutionError("玄关返回的不是 JSON 对象")
    code = body.get("resultCode")
    if isinstance(code, bool) or code != RESULT_OK:
        message = str(body.get("resultMsg") or "")[:60]
        raise IdentityResolutionError(f"玄关拒绝了这把 Key（resultCode={code!r} {message}）".rstrip())
    return body.get("data")


def resolve_person(
    app_key: str,
    *,
    base: Optional[str] = None,
    transport: Optional[Transport] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PersonIdentity:
    """Resolve the person behind ``app_key``, or raise.

    Every ambiguity is a refusal: no personal space, more than one row
    claiming to be it, an id that is not a digit string, an empty name.  A
    caller that cannot tell who someone is must not guess.
    """
    if not isinstance(app_key, str) or not app_key:
        raise IdentityResolutionError("业务 Key 为空")
    root = (base or os.getenv(BASE_ENV) or DEFAULT_BASE).rstrip("/")
    send = transport or _urllib_transport

    personal_id = _data(send, root + PERSONAL_PROJECT_PATH, app_key, timeout)
    if not isinstance(personal_id, str) or not _ID.match(personal_id):
        raise IdentityResolutionError("玄关返回的个人空间 ID 形状不对")

    rows = _data(send, root + PROJECT_LIST_PATH, app_key, timeout)
    if not isinstance(rows, list):
        raise IdentityResolutionError("玄关返回的空间列表形状不对")
    matches = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("id")) == personal_id and row.get("type") == PERSONAL_TYPE
    ]
    if len(matches) != 1:
        raise IdentityResolutionError("空间列表里找不到唯一的个人空间，无法确定身份")
    row = matches[0]

    person_id = row.get("createBy")
    corp_id = row.get("corpId")
    name = row.get("creator")
    if not isinstance(person_id, str) or not _ID.match(person_id):
        raise IdentityResolutionError("个人空间的 createBy 不是数字字符串，无法作为人员 ID")
    if not isinstance(corp_id, str) or not _ID.match(corp_id):
        raise IdentityResolutionError("个人空间的 corpId 不是数字字符串，无法确定组织")
    if not isinstance(name, str) or not name.strip():
        raise IdentityResolutionError("个人空间没有创建人姓名")
    name = name.strip()
    if len(name) > MAX_NAME_CHARS or any(ord(ch) < 32 for ch in name):
        raise IdentityResolutionError("创建人姓名超长或含控制字符")
    return PersonIdentity(corp_id=corp_id, person_id=person_id, name=name)


def resolve_from_env(
    var_name: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
) -> PersonIdentity:
    source = os.environ if env is None else env
    if not var_name:
        raise IdentityResolutionError("--verify-env 必填：给业务 Key 所在的环境变量名，不要给 Key 本身")
    app_key = source.get(var_name, "")
    if not app_key:
        raise IdentityResolutionError(f"环境变量 {var_name} 未设置或为空")
    return resolve_person(app_key, transport=transport)


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用玄关解析业务 Key 属于谁（只读，输出 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)
    whoami = sub.add_parser("whoami", help="解析一把 Key 的人员 ID、组织与姓名")
    whoami.add_argument("--verify-env", required=True, metavar="VAR_NAME", help="业务 Key 所在的环境变量名")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
) -> int:
    from kb_storage import assert_no_plaintext_credential_flags  # noqa: PLC0415
    from kb_token import TokenError, assert_no_plaintext_business_key  # noqa: PLC0415

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        assert_no_plaintext_business_key(argv)
        args = build_parser().parse_args(argv)
        person = resolve_from_env(args.verify_env, env=env, transport=transport)
        payload = {
            "schema": WHOAMI_SCHEMA,
            "ok": True,
            "principal": person.principal,
            "corp_id": person.corp_id,
            "person_id": person.person_id,
            "name": person.name,
        }
        code = 0
    except (IdentityResolutionError, TokenError) as exc:
        payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": "identity", "message": str(exc)}}
        code = 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
