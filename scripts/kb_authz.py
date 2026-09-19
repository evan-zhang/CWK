#!/usr/bin/env python3
"""RT-061: 库对象与自有授权——the write face of bank membership.

A CWK bank is a union of *parts* of many upstream sources (some folders of a
few 玄关 spaces, a time window of 工作协同, a batch of uploads).  No upstream
ACL can express that set, so access is a property of the bank itself and CWK
keeps it here, in one file (``cwk.kb.authz.v1``) with four tables:

- ``banks``    bank_id → name, created_by (immutable), created_at, status
- ``sources``  source_id → bank_id, type, selector, credential env name
- ``grants``   (bank_id, principal) → role ∈ owner / writer / reader
- ``persons``  principal → corp, person id, real name, how it was verified

From 玄关 CWK borrows identity only (``kb_identity.py``); nothing it says about
spaces decides access.  Tokens (``kb_token.py``) prove who a caller is, the
grants here decide what they may read, and the read side in production is
``adapters/kb_auth.py`` — this module calls the same
:func:`adapters.kb_auth.membership_verdict`, so the two cannot drift apart.

Ownership lives only in ``grants`` (Q9): a bank can have several owners, an
owner may add owners and manage readers/writers but may not demote or remove
another owner, and no write may leave a bank without an owner.  That last
check runs on freshly re-read data under an exclusive lock, and callers that
edit from a stale view pass ``--expect-version`` so their write is refused
instead of silently overwriting somebody else's.

Authority for the CLI is write access to the protected OPS files, the same
rule ``kb_token.py`` applies to shared tokens: the CLI acts as
:data:`OPS_ADMIN`.  The role rules are enforced for every actor so the future
console can pass a real person as the actor without a second implementation.

Usage::

    python3 scripts/kb_authz.py init        --store auth/registry/kb-authz.json
    python3 scripts/kb_authz.py enroll      --store ... --verify-env XG_BIZ_API_KEY
    python3 scripts/kb_authz.py bank-create --store ... --bank-id cwork-3m --name 工作协同近三月 \\
        --owner person:<corp>:<id>
    python3 scripts/kb_authz.py grant       --store ... --bank-id cwork-3m \\
        --principal person:<corp>:<id> --role reader
    python3 scripts/kb_authz.py migrate     --registry ... --store ... \\
        --verify-env OPS_KEY --owner-env OPS_KEY --service rag-answer=<agent-id>
    python3 scripts/kb_authz.py check-equivalence --registry ... --store ...

Every verb answers one JSON object on stdout, success or failure.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import os
import re
import secrets
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT))

from kb_ledger import dumps, iso, loads, utc_now  # noqa: E402

STORE_SCHEMA = "cwk.kb.authz.v1"
RECEIPT_SCHEMA = "cwk.kb.authz-receipt.v1"
RESULT_SCHEMA = "cwk.kb.authz-result.v1"
MIGRATE_SCHEMA = "cwk.kb.authz-migrate.v1"
EQUIVALENCE_SCHEMA = "cwk.kb.authz-equivalence.v1"
CHECK_SCHEMA = "cwk.kb.authz-check.v1"
ERROR_SCHEMA = "cwk.kb.authz-error.v1"

ROLE_OWNER = "owner"
ROLE_WRITER = "writer"
ROLE_READER = "reader"
ROLES = (ROLE_OWNER, ROLE_WRITER, ROLE_READER)

BANK_ACTIVE = "active"
BANK_ARCHIVED = "archived"

#: The CLI's actor: whoever can write the protected OPS files.
OPS_ADMIN = "admin:ops-registry"
IDENTITY_XUANGUAN = "xuanguan_verified"
MIGRATION_ACTOR = "migration:rt061"

STORE_MODE = 0o600

#: Source types and the selector fields each accepts.  ``upload`` is part of
#: the schema so the shape is settled, but refused until uploads exist.
SOURCE_SELECTORS: Dict[str, Tuple[str, ...]] = {
    "docdb": ("space_id", "path"),
    "cwork": ("from", "to", "keywords"),
    "upload": ("drop_dir",),
}
ENABLED_SOURCE_TYPES = ("docdb", "cwork")

_PERSON = re.compile(r"\Aperson:([0-9]{1,32}):([0-9]{1,32})\Z")
_SERVICE = re.compile(r"\Aservice:[a-z0-9][a-z0-9-]{0,62}\Z")
_ENV_NAME = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")
_DIGITS = re.compile(r"\A[0-9]{1,32}\Z")
_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


# ── errors ──────────────────────────────────────────────────────────────────


class AuthzError(Exception):
    kind = "authz_error"


class UsageError(AuthzError):
    kind = "usage"


class StoreError(AuthzError):
    kind = "store"


class NotFound(AuthzError):
    kind = "not_found"


class Forbidden(AuthzError):
    kind = "forbidden"


class ConflictError(AuthzError):
    kind = "conflict"


class VersionConflict(ConflictError):
    """写入基于的版本已经过期——和「规则不允许」是两回事，别混成一句话。"""

    kind = "version_conflict"


# ── validation ──────────────────────────────────────────────────────────────


def is_person(principal: object) -> bool:
    return isinstance(principal, str) and bool(_PERSON.match(principal))


def is_service(principal: object) -> bool:
    return isinstance(principal, str) and bool(_SERVICE.match(principal))


def validate_principal(principal: object) -> str:
    if not (is_person(principal) or is_service(principal)):
        raise UsageError("principal 只能是 person:<组织ID>:<人员ID> 或 service:<服务名>")
    return str(principal)


def validate_role(role: object) -> str:
    if role not in ROLES:
        raise UsageError(f"role 只能是 {' / '.join(ROLES)}")
    return str(role)


def validate_bank_id(bank_id: object) -> str:
    """Same rule ``kb_token.validate_kb_ids`` applies to token scopes."""
    text = str(bank_id or "").strip()
    if not text or len(text) > 256 or any(ord(ch) < 32 for ch in text):
        raise UsageError(f"bank_id 非法：{text[:40]!r}")
    return text


def validate_name(name: object, *, field: str = "name", limit: int = 128) -> str:
    text = str(name or "").strip()
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        raise UsageError(f"{field} 不能为空、最长 {limit} 字符且不能含控制字符")
    return text


def validate_selector(source_type: str, selector: object) -> Dict[str, Any]:
    allowed = SOURCE_SELECTORS[source_type]
    if not isinstance(selector, dict) or not selector:
        raise UsageError("selector 必须是非空 JSON 对象")
    extra = sorted(set(selector) - set(allowed))
    if extra:
        raise UsageError(f"{source_type} 来源不接受字段：{', '.join(extra)}（可用：{', '.join(allowed)}）")
    cleaned: Dict[str, Any] = {}
    if source_type == "docdb":
        space_id = selector.get("space_id")
        if not isinstance(space_id, str) or not _DIGITS.match(space_id):
            raise UsageError("docdb 来源的 space_id 必须是数字字符串")
        cleaned["space_id"] = space_id
        path = selector.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/") or any(ord(ch) < 32 for ch in path):
            raise UsageError("docdb 来源的 path 必须以 / 开头且不含 ..")
        cleaned["path"] = path
    elif source_type == "cwork":
        for key in ("from", "to"):
            value = selector.get(key)
            if not isinstance(value, str) or not _DATE.match(value):
                raise UsageError(f"cwork 来源的 {key} 必须是 YYYY-MM-DD")
            cleaned[key] = value
        if cleaned["from"] > cleaned["to"]:
            raise UsageError("cwork 来源的 from 不能晚于 to")
        keywords = selector.get("keywords", [])
        if not isinstance(keywords, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 64 for item in keywords
        ):
            raise UsageError("cwork 来源的 keywords 必须是非空短字符串列表")
        cleaned["keywords"] = [item.strip() for item in keywords]
    else:
        drop_dir = selector.get("drop_dir")
        if not isinstance(drop_dir, str) or not drop_dir:
            raise UsageError("upload 来源的 drop_dir 必填")
        cleaned["drop_dir"] = drop_dir
    return cleaned


# ── store file ──────────────────────────────────────────────────────────────


def new_store(*, now: Optional[datetime] = None) -> dict:
    stamp = iso(now or utc_now())
    return {
        "schema": STORE_SCHEMA,
        "version": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "admins": [],
        "banks": {},
        "sources": {},
        "grants": [],
        "persons": {},
        "receipts": [],
    }


def validate_store(data: object) -> dict:
    if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
        raise StoreError(f"授权表 schema 应为 {STORE_SCHEMA}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise StoreError("授权表的 version 必须是非负整数")
    for key, kind in (("banks", dict), ("sources", dict), ("persons", dict), ("grants", list), ("admins", list), ("receipts", list)):
        if not isinstance(data.get(key), kind):
            raise StoreError(f"授权表缺少 {key}")
    return data


def load_store(path: Path | str) -> dict:
    target = Path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise StoreError(f"授权表不存在：{target}——先跑 kb_authz.py init --store {target}") from exc
    except OSError as exc:
        raise StoreError(f"授权表读取失败：{exc}") from exc
    try:
        data = loads(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is fail-closed
        raise StoreError(f"授权表不是合法 JSON：{exc}") from exc
    return validate_store(data)


def save_store(path: Path | str, data: dict, *, now: Optional[datetime] = None) -> Path:
    """Atomic, 0600, whole-file replace — readers never see a half-written table."""
    data["updated_at"] = iso(now or utc_now())
    body = dumps(data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".kb-authz.")
    try:
        os.fchmod(handle, STORE_MODE)
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    os.chmod(target, STORE_MODE)
    return target


@contextlib.contextmanager
def locked(path: Path | str) -> Iterator[None]:
    """Exclusive advisory lock beside the store, held for read-check-write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, STORE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def mutate(
    path: Path | str,
    change: Callable[[dict], Any],
    *,
    expect_version: Optional[int] = None,
    create: bool = False,
    now: Optional[datetime] = None,
) -> Tuple[dict, Any]:
    """Re-read, check, apply and write under one lock.

    ``change`` mutates the store and returns a summary.  It bumps ``version``
    through :func:`_commit` only when something actually changed, so an
    idempotent call leaves the file untouched.
    """
    target = Path(path)
    with locked(target):
        if create and not target.exists():
            data = new_store(now=now)
        else:
            data = load_store(target)
        if expect_version is not None and data["version"] != expect_version:
            raise VersionConflict(
                f"授权表已被改动（当前版本 {data['version']}，你基于版本 {expect_version}）——请刷新后重试"
            )
        before = data["version"]
        result = change(data)
        if data["version"] != before or (create and not target.exists()):
            save_store(target, data, now=now)
    return data, result


def init_store(path: Path | str, *, now: Optional[datetime] = None) -> dict:
    target = Path(path)
    with locked(target):
        if target.exists():
            raise ConflictError(f"授权表已存在：{target}——init 不覆盖已有授权表")
        data = new_store(now=now)
        save_store(target, data, now=now)
    return data


# ── receipts ────────────────────────────────────────────────────────────────


def _commit(
    data: dict,
    *,
    action: str,
    actor: str,
    now: datetime,
    details: Mapping[str, Any],
    operator: str = "",
    reason: str = "",
) -> dict:
    before = int(data["version"])
    data["version"] = before + 1
    body: Dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": "raz-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(26)),
        "action": action,
        "actor": actor,
        "operator": operator or "unspecified",
        "reason": reason or "unspecified",
        "committed_at": iso(now),
        "version_before": before,
        "version_after": before + 1,
        "details": dict(details),
    }
    body["receipt_sha256"] = hashlib.sha256(dumps(body)).hexdigest()
    data["receipts"].append(body)
    return body


# ── reads ───────────────────────────────────────────────────────────────────


def is_admin(data: Mapping[str, Any], actor: str) -> bool:
    return actor == OPS_ADMIN or actor in (data.get("admins") or [])


def bank_entry(data: Mapping[str, Any], bank_id: str) -> dict:
    entry = data["banks"].get(bank_id)
    if not isinstance(entry, dict):
        raise NotFound(f"没有这个库：{bank_id}")
    return entry


def role_of(data: Mapping[str, Any], bank_id: str, principal: str) -> Optional[str]:
    for grant in data["grants"]:
        if isinstance(grant, dict) and grant.get("bank_id") == bank_id and grant.get("principal") == principal:
            role = grant.get("role")
            return role if role in ROLES else None
    return None


def owners_of(data: Mapping[str, Any], bank_id: str) -> List[str]:
    return [
        str(grant["principal"])
        for grant in data["grants"]
        if isinstance(grant, dict) and grant.get("bank_id") == bank_id and grant.get("role") == ROLE_OWNER
    ]


def decide(data: Mapping[str, Any], principal: str, bank_id: str) -> Tuple[str, str]:
    """May ``principal`` read ``bank_id``?  Exactly what production enforces."""
    from adapters.kb_auth import membership_verdict  # noqa: PLC0415

    return membership_verdict(data, principal, bank_id)


def list_banks(data: Mapping[str, Any], principal: str) -> List[dict]:
    """Every active bank ``principal`` holds a role on, with that role."""
    out = []
    for bank_id, entry in sorted(data["banks"].items()):
        if not isinstance(entry, dict) or entry.get("status") != BANK_ACTIVE:
            continue
        role = role_of(data, bank_id, principal)
        if role:
            out.append({"bank_id": bank_id, "name": entry.get("name", ""), "role": role})
    return out


def members_of(data: Mapping[str, Any], bank_id: str) -> List[dict]:
    rows = []
    for grant in data["grants"]:
        if not isinstance(grant, dict) or grant.get("bank_id") != bank_id:
            continue
        person = data["persons"].get(grant.get("principal"), {})
        rows.append({
            "principal": grant.get("principal"),
            "name": person.get("name", "") if isinstance(person, dict) else "",
            "role": grant.get("role"),
            "granted_by": grant.get("granted_by"),
            "granted_at": grant.get("granted_at"),
        })
    order = {role: index for index, role in enumerate(ROLES)}
    rows.sort(key=lambda row: (order.get(row["role"], 9), str(row["principal"])))
    return rows


# ── mutations ───────────────────────────────────────────────────────────────


def _require_manager(data: Mapping[str, Any], actor: str, bank_id: str) -> None:
    if is_admin(data, actor):
        return
    if role_of(data, bank_id, actor) != ROLE_OWNER:
        raise Forbidden("只有该库的所有者或管理员能做这个操作")


def _require_active(entry: Mapping[str, Any], bank_id: str) -> None:
    if entry.get("status") != BANK_ACTIVE:
        raise ConflictError(f"库 {bank_id} 已归档，不能再改动")


def _require_known(data: Mapping[str, Any], principal: str) -> None:
    if is_person(principal) and principal not in data["persons"]:
        raise NotFound(f"人员目录里没有 {principal}——先用本人业务 Key 跑 enroll 报到")


def upsert_person(data: dict, person: Any, *, now: Optional[datetime] = None, operator: str = "") -> bool:
    """Record a person whose identity 玄关 just confirmed.  True if anything changed."""
    moment = now or utc_now()
    principal = validate_principal(person.principal)
    if not is_person(principal):
        raise UsageError("只有人员能进人员目录")
    stamp = iso(moment)
    current = data["persons"].get(principal)
    entry = {
        "corp_id": str(person.corp_id),
        "person_id": str(person.person_id),
        "name": validate_name(person.name, field="姓名", limit=64),
        "identity_source": IDENTITY_XUANGUAN,
        "first_seen": current.get("first_seen", stamp) if isinstance(current, dict) else stamp,
        "last_verified": stamp,
    }
    data["persons"][principal] = entry
    changed_name = not isinstance(current, dict) or current.get("name") != entry["name"]
    _commit(
        data,
        action="enroll" if not isinstance(current, dict) else "reverify",
        actor=OPS_ADMIN,
        now=moment,
        operator=operator,
        details={"principal": principal, "name_changed": changed_name},
    )
    return True


def create_bank(
    data: dict,
    *,
    actor: str,
    bank_id: str,
    name: str,
    owner: str,
    now: Optional[datetime] = None,
    operator: str = "",
    reason: str = "",
) -> dict:
    moment = now or utc_now()
    if not is_admin(data, actor):
        raise Forbidden("建库目前只允许管理员执行")
    bank = validate_bank_id(bank_id)
    title = validate_name(name)
    owner = validate_principal(owner)
    if not is_person(owner):
        raise UsageError("库的所有者必须是人，不能是服务")
    _require_known(data, owner)
    if bank in data["banks"]:
        raise ConflictError(f"库 {bank} 已存在")
    stamp = iso(moment)
    data["banks"][bank] = {"name": title, "created_by": actor, "created_at": stamp, "status": BANK_ACTIVE}
    data["grants"].append({"bank_id": bank, "principal": owner, "role": ROLE_OWNER, "granted_by": actor, "granted_at": stamp})
    _commit(data, action="bank_create", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank, "owner": owner})
    return data["banks"][bank]


def archive_bank(
    data: dict, *, actor: str, bank_id: str, now: Optional[datetime] = None, operator: str = "", reason: str = ""
) -> dict:
    moment = now or utc_now()
    entry = bank_entry(data, bank_id)
    _require_manager(data, actor, bank_id)
    _require_active(entry, bank_id)
    entry["status"] = BANK_ARCHIVED
    entry["archived_at"] = iso(moment)
    _commit(data, action="bank_archive", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank_id})
    return entry


def set_member(
    data: dict,
    *,
    actor: str,
    bank_id: str,
    principal: str,
    role: str,
    now: Optional[datetime] = None,
    operator: str = "",
    reason: str = "",
) -> bool:
    """Add a member or change their role.  Returns False when nothing changed."""
    moment = now or utc_now()
    entry = bank_entry(data, bank_id)
    principal = validate_principal(principal)
    role = validate_role(role)
    _require_manager(data, actor, bank_id)
    _require_active(entry, bank_id)
    _require_known(data, principal)
    admin = is_admin(data, actor)
    if is_service(principal):
        if not admin:
            raise Forbidden("给服务授权只允许管理员执行")
        if role != ROLE_READER:
            raise UsageError("服务只能是只读成员")

    current = role_of(data, bank_id, principal)
    if current == role:
        return False
    if current == ROLE_OWNER:
        if not admin and principal != actor:
            raise Forbidden("所有者不能降级其他所有者，只能降级自己")
        if len([p for p in owners_of(data, bank_id) if p != principal]) < 1:
            raise ConflictError("库至少要保留一位所有者——先加一位新所有者，再降级")

    stamp = iso(moment)
    for grant in data["grants"]:
        if isinstance(grant, dict) and grant.get("bank_id") == bank_id and grant.get("principal") == principal:
            grant.update({"role": role, "granted_by": actor, "granted_at": stamp})
            break
    else:
        data["grants"].append({"bank_id": bank_id, "principal": principal, "role": role, "granted_by": actor, "granted_at": stamp})
    _commit(data, action="grant", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank_id, "principal": principal, "role_before": current, "role_after": role})
    return True


def remove_member(
    data: dict,
    *,
    actor: str,
    bank_id: str,
    principal: str,
    now: Optional[datetime] = None,
    operator: str = "",
    reason: str = "",
) -> None:
    moment = now or utc_now()
    entry = bank_entry(data, bank_id)
    principal = validate_principal(principal)
    _require_manager(data, actor, bank_id)
    _require_active(entry, bank_id)
    current = role_of(data, bank_id, principal)
    if current is None:
        raise NotFound(f"{principal} 不是库 {bank_id} 的成员")
    if is_service(principal) and not is_admin(data, actor):
        raise Forbidden("服务的授权只允许管理员移除——移除后该库的问答会中断")
    if current == ROLE_OWNER:
        if not is_admin(data, actor) and principal != actor:
            raise Forbidden("所有者不能移除其他所有者，只能移除自己")
        if len([p for p in owners_of(data, bank_id) if p != principal]) < 1:
            raise ConflictError("库至少要保留一位所有者——先加一位新所有者，再移除")
    data["grants"] = [
        grant for grant in data["grants"]
        if not (isinstance(grant, dict) and grant.get("bank_id") == bank_id and grant.get("principal") == principal)
    ]
    _commit(data, action="revoke", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank_id, "principal": principal, "role_before": current})


def add_source(
    data: dict,
    *,
    actor: str,
    bank_id: str,
    source_type: str,
    selector: Mapping[str, Any],
    credential_env: str = "",
    now: Optional[datetime] = None,
    operator: str = "",
    reason: str = "",
) -> str:
    moment = now or utc_now()
    entry = bank_entry(data, bank_id)
    _require_manager(data, actor, bank_id)
    _require_active(entry, bank_id)
    if source_type not in SOURCE_SELECTORS:
        raise UsageError(f"来源类型只能是 {' / '.join(SOURCE_SELECTORS)}")
    if source_type not in ENABLED_SOURCE_TYPES:
        raise UsageError(f"来源类型 {source_type} 还没启用")
    cleaned = validate_selector(source_type, dict(selector))
    if credential_env and not _ENV_NAME.match(credential_env):
        raise UsageError("--credential-env 只接受环境变量名（大写字母数字与下划线），不接受 Key 本身")
    source_id = "src-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(20))
    data["sources"][source_id] = {
        "bank_id": bank_id,
        "type": source_type,
        "selector": cleaned,
        "credential_env": credential_env,
        "added_by": actor,
        "added_at": iso(moment),
    }
    _commit(data, action="source_add", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank_id, "source_id": source_id, "type": source_type})
    return source_id


def remove_source(
    data: dict, *, actor: str, source_id: str, now: Optional[datetime] = None, operator: str = "", reason: str = ""
) -> dict:
    moment = now or utc_now()
    source = data["sources"].get(source_id)
    if not isinstance(source, dict):
        raise NotFound(f"没有这个来源：{source_id}")
    bank_id = str(source.get("bank_id"))
    _require_manager(data, actor, bank_id)
    _require_active(bank_entry(data, bank_id), bank_id)
    del data["sources"][source_id]
    _commit(data, action="source_remove", actor=actor, now=moment, operator=operator, reason=reason,
            details={"bank_id": bank_id, "source_id": source_id})
    return source


# ── migration ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifiedKey:
    env_name: str
    owner_ref: str
    person: Any


def plan_migration(
    registry: Mapping[str, Any],
    store: Mapping[str, Any],
    *,
    keys: Sequence[VerifiedKey],
    owner_env: str,
    services: Mapping[str, str],
    now: datetime,
) -> dict:
    """Work out, without writing anything, who each live token belongs to.

    A token is attributed only by re-deriving what the registry already
    holds: its ``owner_ref`` must equal HMAC(salt, a key that just passed
    玄关), and a service attribution additionally needs its binding to
    re-derive from the stated Agent id under that key.  One live token that
    cannot be attributed this way stops the whole migration.
    """
    import kb_token  # noqa: PLC0415

    salt = str(registry.get("owner_ref_salt") or "")
    by_owner = {key.owner_ref: key for key in keys}
    owner_key = next((key for key in keys if key.env_name == owner_env), None)
    if owner_key is None:
        raise UsageError("--owner-env 必须是 --verify-env 里的一个")

    bindings: Dict[str, str] = {}
    unattributed: List[str] = []
    live_banks: List[str] = []
    grants: List[Tuple[str, str]] = []
    for record in kb_token.records(registry):
        if kb_token.record_status(record, now) != kb_token.STATUS_ACTIVE:
            continue
        token_id = str(record.get("token_id") or "")
        scope = [str(item) for item in record.get("kb_ids", [])]
        for bank in scope:
            if bank not in live_banks:
                live_banks.append(bank)
        if kb_token.token_kind(record) == kb_token.TOKEN_KIND_SHARED_KB:
            continue
        principal = str(record.get("principal") or "")
        if not principal:
            key = by_owner.get(str(record.get("owner_ref") or ""))
            if key is not None:
                principal = key.person.principal
                for service, agent_id in services.items():
                    if record.get("agent_binding_id") == kb_token.derive_agent_binding_id(salt, key.owner_ref, agent_id):
                        principal = kb_token.SERVICE_PRINCIPAL_PREFIX + service
                        break
        if not principal:
            unattributed.append(token_id)
            continue
        if not str(record.get("principal") or ""):
            bindings[token_id] = principal
        for bank in scope:
            grants.append((bank, principal))

    if unattributed:
        raise ConflictError(
            "以下有效令牌无法用给定的 Key 证明归属，迁移整体不执行："
            + ", ".join(sorted(unattributed))
            + "——补上签发它们的那把 Key（--verify-env），或先吊销"
        )
    unused_services = sorted(
        service for service in services
        if kb_token.SERVICE_PRINCIPAL_PREFIX + service not in {p for _, p in grants}
    )
    if unused_services:
        raise ConflictError(f"--service 没有匹配到任何有效令牌：{', '.join(unused_services)}——检查 Agent 标识")

    return {
        "owner": owner_key.person.principal,
        "persons": sorted({key.person.principal for key in keys}),
        "banks_to_create": [bank for bank in live_banks if bank not in store["banks"]],
        "grants": sorted(set(grants)),
        "bindings": dict(sorted(bindings.items())),
    }


def apply_migration(
    data: dict,
    plan: Mapping[str, Any],
    *,
    keys: Sequence[VerifiedKey],
    now: datetime,
    operator: str = "",
    reason: str = "",
) -> dict:
    """Idempotently write a plan into the store.  Never downgrades a role."""
    summary = {"persons_enrolled": 0, "banks_created": 0, "grants_added": 0}
    for key in keys:
        if key.person.principal not in data["persons"]:
            summary["persons_enrolled"] += 1
        upsert_person(data, key.person, now=now, operator=operator)
    owner = str(plan["owner"])
    for bank in plan["banks_to_create"]:
        if bank not in data["banks"]:
            create_bank(data, actor=OPS_ADMIN, bank_id=bank, name=bank, owner=owner, now=now,
                        operator=operator, reason=reason or MIGRATION_ACTOR)
            summary["banks_created"] += 1
    for bank, principal in plan["grants"]:
        if role_of(data, bank, principal) is None:
            set_member(data, actor=OPS_ADMIN, bank_id=bank, principal=principal, role=ROLE_READER, now=now,
                       operator=operator, reason=reason or MIGRATION_ACTOR)
            summary["grants_added"] += 1
    return summary


def equivalence_report(registry: Mapping[str, Any], store: Mapping[str, Any], *, now: datetime) -> dict:
    """Compare both production rules for every live token × every known bank.

    Uses :mod:`adapters.kb_auth` itself, not a re-implementation, so what is
    proven equal is what 8787/8790 would actually decide.
    """
    import kb_token  # noqa: PLC0415
    from adapters.kb_auth import MODE_GRANTS, MODE_SCOPE, record_verdict  # noqa: PLC0415

    banks = sorted(set(store["banks"]) | {
        str(item) for record in kb_token.records(registry) for item in record.get("kb_ids", [])
    })
    compared = 0
    differences = []
    for record in kb_token.records(registry):
        if kb_token.record_status(record, now) != kb_token.STATUS_ACTIVE:
            continue
        for bank in banks:
            compared += 1
            scope_status, _ = record_verdict(record, bank, mode=MODE_SCOPE, authz=store)
            grants_status, grants_reason = record_verdict(record, bank, mode=MODE_GRANTS, authz=store)
            if scope_status != grants_status:
                differences.append({
                    "token_id": record.get("token_id"),
                    "bank_id": bank,
                    "scope": scope_status,
                    "grants": grants_status,
                    "grants_reason": grants_reason,
                })
    return {"compared": compared, "banks": banks, "differences": differences, "equivalent": not differences}


def health_check(registry: Mapping[str, Any], store: Mapping[str, Any], *, now: datetime) -> dict:
    """Problems that would bite after switching to grants mode."""
    import kb_token  # noqa: PLC0415

    problems = []
    service_principals = set()
    for record in kb_token.records(registry):
        if kb_token.record_status(record, now) != kb_token.STATUS_ACTIVE:
            continue
        kind = kb_token.token_kind(record)
        principal = str(record.get("principal") or "")
        if kind != kb_token.TOKEN_KIND_SHARED_KB and not principal:
            problems.append({"kind": "token_without_principal", "token_id": record.get("token_id")})
        if principal.startswith(kb_token.SERVICE_PRINCIPAL_PREFIX):
            service_principals.add(principal)
        for bank in record.get("kb_ids", []):
            if bank not in store["banks"]:
                problems.append({"kind": "token_bank_missing", "token_id": record.get("token_id"), "bank_id": bank})
    for bank_id, entry in sorted(store["banks"].items()):
        if not isinstance(entry, dict) or entry.get("status") != BANK_ACTIVE:
            continue
        if not owners_of(store, bank_id):
            problems.append({"kind": "bank_without_owner", "bank_id": bank_id})
        for principal in sorted(service_principals):
            if role_of(store, bank_id, principal) is None:
                problems.append({"kind": "service_not_granted", "bank_id": bank_id, "principal": principal})
    for grant in store["grants"]:
        principal = grant.get("principal") if isinstance(grant, dict) else None
        if is_person(principal) and principal not in store["persons"]:
            problems.append({"kind": "grant_to_unknown_person", "principal": principal, "bank_id": grant.get("bank_id")})
    return {"ok": not problems, "problems": problems}


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="库与授权：成员、来源、人员目录、迁移与等价性核对（输出一律 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)

    def store(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--store", required=True, metavar="PATH", help="授权表 JSON 路径")
        return p

    def audit(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--actor", default="", help="操作者标识，写进回执")
        p.add_argument("--reason", default="", help="操作理由，写进回执")
        return p

    def versioned(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--expect-version", type=int, default=None, help="基于哪个版本改；版本已变则拒绝")
        return p

    store(sub.add_parser("init", help="创建空授权表"))
    audit(store(sub.add_parser("enroll", help="用本人业务 Key 经玄关核实身份，写入人员目录"))).add_argument(
        "--verify-env", required=True, metavar="VAR_NAME", help="业务 Key 所在的环境变量名")

    create = versioned(audit(store(sub.add_parser("bank-create", help="建库并指定第一位所有者"))))
    create.add_argument("--bank-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--owner", required=True, help="person:<组织ID>:<人员ID>，须已 enroll")
    create.add_argument("--service", action="append", default=[], metavar="service:<名称>",
                        help="同一次写入里给服务只读授权（如 service:rag-answer），可重复；漏了问答会中断")

    archive = versioned(audit(store(sub.add_parser("bank-archive", help="归档库：所有人立即失去访问"))))
    archive.add_argument("--bank-id", required=True)

    grant = versioned(audit(store(sub.add_parser("grant", help="加成员或改角色"))))
    grant.add_argument("--bank-id", required=True)
    grant.add_argument("--principal", required=True)
    grant.add_argument("--role", required=True, choices=ROLES)

    revoke = versioned(audit(store(sub.add_parser("revoke", help="移除成员"))))
    revoke.add_argument("--bank-id", required=True)
    revoke.add_argument("--principal", required=True)

    source_add = versioned(audit(store(sub.add_parser("source-add", help="给库加一条来源"))))
    source_add.add_argument("--bank-id", required=True)
    source_add.add_argument("--type", required=True, choices=tuple(SOURCE_SELECTORS))
    source_add.add_argument("--selector-json", required=True, help='如 {"space_id":"123","path":"/投前"}')
    source_add.add_argument("--credential-env", default="", help="采集用 Key 的环境变量名（不是 Key 本身）")

    source_remove = versioned(audit(store(sub.add_parser("source-remove", help="移除一条来源"))))
    source_remove.add_argument("--source-id", required=True)

    show = store(sub.add_parser("show", help="查看库、成员与人员目录"))
    show.add_argument("--bank-id", default=None)
    show.add_argument("--principal", default=None, help="只看这个主体能访问的库")

    migrate = audit(store(sub.add_parser("migrate", help="从现有令牌推导成员表（行为不变）")))
    migrate.add_argument("--registry", required=True, metavar="PATH")
    migrate.add_argument("--verify-env", action="append", default=[], required=True, metavar="VAR_NAME",
                         help="签发过现有令牌的业务 Key 的环境变量名，可重复")
    migrate.add_argument("--owner-env", required=True, metavar="VAR_NAME", help="其持有人成为新建库的所有者")
    migrate.add_argument("--service", action="append", default=[], metavar="NAME=AGENT_ID",
                         help="把以该 Agent 标识签发的令牌认作服务身份，可重复")
    migrate.add_argument("--dry-run", action="store_true", help="只算不写")

    for name, help_text in (("check-equivalence", "逐支令牌 × 逐个库比对两种判定"),
                            ("check", "切换到成员表判定前的体检")):
        p = store(sub.add_parser(name, help=help_text))
        p.add_argument("--registry", required=True, metavar="PATH")
    return parser


def _parse_services(items: Sequence[str]) -> Dict[str, str]:
    import kb_token  # noqa: PLC0415

    services: Dict[str, str] = {}
    for item in items:
        name, sep, agent_id = str(item).partition("=")
        if not sep:
            raise UsageError("--service 格式是 NAME=AGENT_ID")
        try:
            kb_token.validate_service_name(name)
            kb_token.validate_agent_id(agent_id)
        except kb_token.TokenError as exc:
            raise UsageError(str(exc)) from exc
        if name in services:
            raise UsageError(f"--service {name} 重复")
        services[name] = agent_id
    return services


def _verify_keys(names: Sequence[str], salt: str, *, env: Mapping[str, str], resolver: Callable[[str], Any]) -> List[VerifiedKey]:
    import kb_token  # noqa: PLC0415
    from kb_identity import IdentityResolutionError  # noqa: PLC0415

    keys: List[VerifiedKey] = []
    for name in dict.fromkeys(names):
        app_key = env.get(name, "")
        if not app_key:
            raise UsageError(f"环境变量 {name} 未设置或为空")
        try:
            person = resolver(app_key)
        except IdentityResolutionError as exc:
            raise AuthzError(f"{name} 未通过玄关身份核实：{kb_token.redact(str(exc), app_key)}") from None
        keys.append(VerifiedKey(env_name=name, owner_ref=kb_token.derive_owner_ref(salt, app_key), person=person))
    return keys


def _result(action: str, data: Mapping[str, Any], **extra: Any) -> dict:
    payload = {"schema": RESULT_SCHEMA, "ok": True, "action": action, "version": data["version"]}
    payload.update(extra)
    return payload


def run(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str],
    resolver: Callable[[str], Any],
    now: datetime,
) -> Tuple[dict, int]:
    audit = {"operator": getattr(args, "actor", ""), "reason": getattr(args, "reason", "")}
    expect = getattr(args, "expect_version", None)

    if args.command == "init":
        data = init_store(args.store, now=now)
        return _result("init", data, store=str(args.store), mode="0600"), 0

    if args.command == "enroll":
        from kb_identity import IdentityResolutionError  # noqa: PLC0415
        import kb_token  # noqa: PLC0415

        app_key = env.get(args.verify_env, "")
        if not app_key:
            raise UsageError(f"环境变量 {args.verify_env} 未设置或为空")
        try:
            person = resolver(app_key)
        except IdentityResolutionError as exc:
            raise AuthzError(f"身份核实失败：{kb_token.redact(str(exc), app_key)}") from None
        data, _ = mutate(args.store, lambda d: upsert_person(d, person, now=now, operator=audit["operator"]), now=now)
        return _result("enroll", data, principal=person.principal, name=person.name), 0

    if args.command == "bank-create":
        def create(d: dict) -> dict:
            entry = create_bank(d, actor=OPS_ADMIN, bank_id=args.bank_id, name=args.name, owner=args.owner, now=now, **audit)
            for service in args.service:
                if not is_service(service):
                    raise UsageError(f"--service 必须是 service:<名称>：{service!r}")
                set_member(d, actor=OPS_ADMIN, bank_id=args.bank_id, principal=service, role=ROLE_READER, now=now, **audit)
            return entry

        data, entry = mutate(args.store, create, expect_version=expect, now=now)
        return _result("bank_create", data, bank_id=args.bank_id, bank=entry, members=members_of(data, args.bank_id)), 0

    if args.command == "bank-archive":
        data, entry = mutate(args.store, lambda d: archive_bank(
            d, actor=OPS_ADMIN, bank_id=args.bank_id, now=now, **audit), expect_version=expect, now=now)
        return _result("bank_archive", data, bank_id=args.bank_id, bank=entry), 0

    if args.command == "grant":
        data, changed = mutate(args.store, lambda d: set_member(
            d, actor=OPS_ADMIN, bank_id=args.bank_id, principal=args.principal, role=args.role, now=now, **audit),
            expect_version=expect, now=now)
        return _result("grant", data, changed=changed, effective="next_request"), 0

    if args.command == "revoke":
        data, _ = mutate(args.store, lambda d: remove_member(
            d, actor=OPS_ADMIN, bank_id=args.bank_id, principal=args.principal, now=now, **audit),
            expect_version=expect, now=now)
        return _result("revoke", data, effective="next_request"), 0

    if args.command == "source-add":
        try:
            selector = loads(args.selector_json.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise UsageError(f"--selector-json 不是合法 JSON 对象：{exc}") from exc
        data, source_id = mutate(args.store, lambda d: add_source(
            d, actor=OPS_ADMIN, bank_id=args.bank_id, source_type=args.type, selector=selector,
            credential_env=args.credential_env, now=now, **audit), expect_version=expect, now=now)
        return _result("source_add", data, source_id=source_id), 0

    if args.command == "source-remove":
        data, source = mutate(args.store, lambda d: remove_source(
            d, actor=OPS_ADMIN, source_id=args.source_id, now=now, **audit), expect_version=expect, now=now)
        return _result("source_remove", data, source_id=args.source_id, bank_id=source.get("bank_id")), 0

    if args.command == "show":
        data = load_store(args.store)
        if args.principal:
            principal = validate_principal(args.principal)
            return _result("show", data, principal=principal, banks=list_banks(data, principal)), 0
        bank_ids = [validate_bank_id(args.bank_id)] if args.bank_id else sorted(data["banks"])
        banks = []
        for bank_id in bank_ids:
            entry = bank_entry(data, bank_id)
            banks.append({
                "bank_id": bank_id,
                **entry,
                "members": members_of(data, bank_id),
                "sources": [dict(source_id=sid, **src) for sid, src in sorted(data["sources"].items())
                            if isinstance(src, dict) and src.get("bank_id") == bank_id],
            })
        persons = [dict(principal=p, **entry) for p, entry in sorted(data["persons"].items())]
        return _result("show", data, banks=banks, persons=persons), 0

    import kb_token  # noqa: PLC0415

    try:
        registry = kb_token.load_registry(args.registry)
    except kb_token.TokenError as exc:
        raise StoreError(str(exc)) from exc

    if args.command == "migrate":
        services = _parse_services(args.service)
        keys = _verify_keys(args.verify_env, str(registry.get("owner_ref_salt") or ""), env=env, resolver=resolver)
        current = load_store(args.store) if Path(args.store).exists() else new_store(now=now)
        plan = plan_migration(registry, current, keys=keys, owner_env=args.owner_env, services=services, now=now)
        persons = {key.person.principal: key.person.name for key in keys}

        def bind_all(target: dict) -> int:
            return sum(
                1 for token_id, principal in plan["bindings"].items()
                if kb_token.bind_principal(target, token_id=token_id, principal=principal, now=now,
                                           actor=audit["operator"] or MIGRATION_ACTOR,
                                           reason=audit["reason"] or MIGRATION_ACTOR)
            )

        # Rehearse the whole migration in memory first.  Nothing reaches disk
        # unless the rehearsed result is provably equivalent, so a failure
        # anywhere — including in the check itself — leaves both files as
        # they were.
        trial_store, trial_registry = copy.deepcopy(current), copy.deepcopy(registry)
        apply_migration(trial_store, plan, keys=keys, now=now, **audit)
        bind_all(trial_registry)
        trial = equivalence_report(trial_registry, trial_store, now=now)
        base = {"schema": MIGRATE_SCHEMA, "plan": plan, "names": persons}
        if args.dry_run or not trial["equivalent"]:
            return (dict(base, ok=trial["equivalent"], dry_run=args.dry_run, written=False, equivalence=trial),
                    0 if trial["equivalent"] else 3)

        data, summary = mutate(args.store, lambda d: apply_migration(
            d, plan, keys=keys, now=now, **audit), create=True, now=now)
        registry = kb_token.load_registry(args.registry)
        bound = bind_all(registry)
        if bound:
            kb_token.save_registry(args.registry, registry, now=now)
        report = equivalence_report(registry, data, now=now)
        payload = dict(base, ok=report["equivalent"], dry_run=False, written=True,
                       applied=dict(summary, tokens_bound=bound), equivalence=report, version=data["version"])
        return payload, 0 if report["equivalent"] else 3

    store_data = load_store(args.store)
    if args.command == "check-equivalence":
        report = equivalence_report(registry, store_data, now=now)
        return {"schema": EQUIVALENCE_SCHEMA, "ok": report["equivalent"], **report}, 0 if report["equivalent"] else 3

    if args.command == "check":
        report = health_check(registry, store_data, now=now)
        return {"schema": CHECK_SCHEMA, **report}, 0 if report["ok"] else 3

    raise UsageError(f"未知子命令 {args.command!r}")


def _default_resolver(app_key: str) -> Any:
    from kb_identity import resolve_person  # noqa: PLC0415

    return resolve_person(app_key)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    resolver: Optional[Callable[[str], Any]] = None,
    now: Optional[datetime] = None,
) -> int:
    from kb_storage import assert_no_plaintext_credential_flags  # noqa: PLC0415
    import kb_token  # noqa: PLC0415

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        kb_token.assert_no_plaintext_business_key(argv)
        args = build_parser().parse_args(argv)
        payload, code = run(
            args,
            env=os.environ if env is None else env,
            resolver=resolver or _default_resolver,
            now=now or utc_now(),
        )
    except (AuthzError, kb_token.TokenError) as exc:
        payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": exc.kind, "message": str(exc)}}
        print(f"kb_authz 失败：{exc}", file=sys.stderr)
        code = 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary; JSON either way
        payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": type(exc).__name__, "message": str(exc)}}
        print(f"kb_authz 失败：{type(exc).__name__}", file=sys.stderr)
        code = 2
    sys.stdout.write(dumps(payload).decode("utf-8"))
    sys.stdout.flush()
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
