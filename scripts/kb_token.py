#!/usr/bin/env python3
"""RT-047 P2: per-Agent-instance query tokens (token 多租户).

One Gateway, many Agents, one human behind each Agent.  This module is the
**write face** of that arrangement: it issues, revokes and re-issues the
bearer tokens that :mod:`kb_gateway` checks on the read side.  Usage::

    export CWORK_APP_KEY=...                       # never on the command line
    python3 scripts/kb_token.py init    --registry /path/tokens.json
    python3 scripts/kb_token.py issue   --registry /path/tokens.json \\
        --verify-env CWORK_APP_KEY --agent-id ops-mac-01 \\
        --kb-id libraries/工作库
    python3 scripts/kb_token.py list    --registry /path/tokens.json
    python3 scripts/kb_token.py revoke  --registry /path/tokens.json \\
        --token-id tok-<16 hex>
    python3 scripts/kb_token.py reissue --registry /path/tokens.json \\
        --verify-env CWORK_APP_KEY --agent-id ops-mac-01

Every verb answers JSON on stdout, success or failure, so the caller parses
one shape (RT-044 J5).

Identity (RT-047 P2, Evan 2026-09-05 17:50).  CWK does **not** invent a
second identity system.  A person is the employee-level business AppKey they
already hold — the one 工作协同 and 玄关 authenticate with.  So ``issue`` and
``reissue`` take ``--verify-env VAR``, the *name* of the environment variable
holding that key (CLI-SPEC §一.3: 传变量名不传值), and:

1. run the key through the authenticated face this repo already has —
   ``cwk_backfill_range._inbox_client`` builds the company Skill's
   ``CWorkClient``; the probe is the cheapest authenticated read there is
   (``todoList`` with ``pageSize=1``).  There is no ``whoami`` endpoint on
   that API, so "the key authenticates" is the whole of what a probe can
   establish, and a key that fails the probe is refused outright;
2. derive ``owner_ref`` from the *verified* key by HMAC-SHA256 under the
   registry's own salt.

There is deliberately no ``--owner-ref`` flag, and :func:`
assert_no_self_asserted_identity` refuses argv that carries one.  A token
whose owner is whatever the caller typed would make the registry a record of
claims rather than of identities; the 铁律 is that ``owner_ref`` is a
function of a key that just proved itself, and nothing else.

What is stored, and what is not.  The token itself is 256 bits of CSPRNG
output, printed once at issue time and never again; the registry keeps
``sha256(token)`` plus metadata.  A reader of the registry therefore learns
who holds what and until when, but cannot authenticate as anybody: sha256 of
256 random bits has no invertible preimage.  The raw business key and the
raw ``--agent-id`` are likewise absent — the record carries an
HMAC-derived ``owner_ref`` / ``agent_binding_id`` instead (RT-013's pattern:
hash on ingest, keep the epoch, keep the receipt).

Revocation is immediate because it is not cached: :class:`TokenFile` re-reads
the file on every single lookup.  ``membership_epoch`` is a registry-wide
monotone counter bumped by every mutation, so a downstream cache (should one
ever exist) can observe the change without diffing records.

Only stdlib, plus this repo's own ``kb_ledger`` / ``kb_storage`` helpers.
Never touches ``.env``, never writes a KB, never calls a write verb on CWork.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_ledger import dumps, iso, loads, parse_iso, utc_now  # noqa: E402
from kb_storage import assert_no_plaintext_credential_flags  # noqa: E402

REGISTRY_SCHEMA = "cwk.kb.token-registry.v1"
RECORD_SCHEMA = "cwk.kb.token-record.v1"
RECEIPT_SCHEMA = "cwk.kb.token-receipt.v1"
INIT_SCHEMA = "cwk.kb.token-init.v1"
ISSUE_SCHEMA = "cwk.kb.token-issue.v1"
REISSUE_SCHEMA = "cwk.kb.token-reissue.v1"
REVOKE_SCHEMA = "cwk.kb.token-revoke.v1"
LIST_SCHEMA = "cwk.kb.token-list.v1"
ERROR_SCHEMA = "cwk.kb.token-error.v1"

#: 256 bits.  Anything less and a stored sha256 stops being a one-way
#: function of something unguessable.
TOKEN_BYTES = 32
TOKEN_HEX_LEN = TOKEN_BYTES * 2
SALT_BYTES = 32
DIGEST_HEX_LEN = 64

DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365

#: "每用户跨 Agent 实例" (RT-047 P2): the ceiling counts a person's live
#: tokens across every Agent instance they run, not per device.
DEFAULT_MAX_ACTIVE_PER_OWNER = 5

OWNER_REF_PREFIX = "owner-"
BINDING_PREFIX = "abnd-"
TOKEN_ID_PREFIX = "tok-"
RECEIPT_ID_PREFIX = "rtok-"
DERIVED_ID_CHARS = 24
TOKEN_ID_CHARS = 16

OWNER_REF_BASIS = "verified-business-key-hmac"
REGISTRY_MODE = 0o600

#: The cheapest authenticated read on the CWork open API.
DEFAULT_PROBE_LABEL = "cwork:todoList(pageIndex=1,pageSize=1)"

#: Flags that would let a caller name themselves.  Refused before argparse
#: so the failure explains the rule instead of saying "unrecognized".
SELF_ASSERTED_IDENTITY_FLAGS = (
    "--owner-ref",
    "--owner",
    "--as-owner",
    "--identity",
    "--emp-id",
    "--employee-id",
    "--user-id",
)

#: Flags that would put the business key itself on the command line.
FORBIDDEN_KEY_VALUE_FLAGS = (
    "--app-key",
    "--business-key",
    "--verify-key",
    "--key",
)

_AGENT_ID_ALLOWED = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@\-]{0,255}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"


# ── errors ──────────────────────────────────────────────────────────────────


class TokenError(Exception):
    """Base for every refusal this module makes.  ``kind`` lands in JSON."""

    kind = "token_error"


class UsageError(TokenError):
    kind = "usage"


class IdentityError(TokenError):
    """The business key was absent, empty, or failed the authenticated probe."""

    kind = "identity"


class SelfAssertedIdentity(TokenError):
    """Somebody tried to name themselves instead of proving who they are."""

    kind = "self_asserted_identity"


class RegistryError(TokenError):
    kind = "registry"


class ConflictError(TokenError):
    kind = "conflict"


class NotFound(TokenError):
    kind = "not_found"


# ── argv guards ─────────────────────────────────────────────────────────────


def assert_no_self_asserted_identity(argv: Sequence[str]) -> None:
    """Refuse argv that carries a hand-typed identity.

    This is the 铁律 made mechanical: ``owner_ref`` may only be derived from
    a key that passed :func:`verify_business_key`.  A flag that let a caller
    supply it would turn every authorization decision downstream into a
    restatement of what the caller wished were true.
    """
    for item in argv:
        head = str(item).split("=", 1)[0]
        if head in SELF_ASSERTED_IDENTITY_FLAGS:
            raise SelfAssertedIdentity(
                f"拒绝自报身份 {head}：owner_ref 只能由 --verify-env 指向的业务 Key "
                "经认证面校验后派生，不接受手工传入。"
            )


def assert_no_plaintext_business_key(argv: Sequence[str]) -> None:
    """Refuse argv that carries the business key value rather than its name."""
    for item in argv:
        head = str(item).split("=", 1)[0]
        if head in FORBIDDEN_KEY_VALUE_FLAGS:
            raise SelfAssertedIdentity(
                f"拒绝命令行明文业务 Key {head}：进程表全局可读；用 "
                "--verify-env 传环境变量名，不传值。"
            )


# ── identity ────────────────────────────────────────────────────────────────


def redact(text: str, secret: str) -> str:
    """Strip ``secret`` out of a message before it is ever printed."""
    if secret and secret in text:
        return text.replace(secret, "<redacted-business-key>")
    return text


def cwork_probe(app_key: str) -> str:
    """Prove the key authenticates, using the face this repo already has.

    ``cwk_backfill_range._inbox_client`` is the single place CWK builds the
    company Skill's ``CWorkClient``; reusing it means the token face and the
    collection face agree on what "this key works" means.  The call is the
    cheapest authenticated *read* on that API and is on the read-only side of
    CWK's 红线 — no mark-as-read, no reply, no todo completion.

    Imported lazily: the Skill ships outside this repository, so a machine
    that only ever runs ``revoke`` or ``list`` never needs it installed.
    """
    from cwk_backfill_range import _inbox_client  # noqa: PLC0415 - lazy on purpose

    client = _inbox_client(app_key)
    client.get_todo_list(page_index=1, page_size=1)
    return DEFAULT_PROBE_LABEL


@dataclass(frozen=True)
class VerifiedIdentity:
    """What a passing probe establishes.  Never carries the key."""

    owner_ref: str
    basis: str = OWNER_REF_BASIS
    probe: str = DEFAULT_PROBE_LABEL
    verified_at: str = ""


def derive_owner_ref(salt_hex: str, app_key: str) -> str:
    """``owner_ref`` = HMAC(registry salt, the verified key).

    Salted per registry so the same person at two installations does not get
    a correlatable identifier, and so a stolen registry cannot be matched
    against a rainbow table of employee keys.  The salt lives in the registry
    file next to the digests: that is a single-file design decision, and it
    defends against correlation and precomputation, not against an attacker
    who already holds the file.
    """
    mac = hmac.new(_salt_bytes(salt_hex), f"owner|v1|{app_key}".encode("utf-8"), hashlib.sha256)
    return OWNER_REF_PREFIX + mac.hexdigest()[:DERIVED_ID_CHARS]


def derive_agent_binding_id(salt_hex: str, owner_ref: str, raw_agent_id: str) -> str:
    """``agent_binding_id`` = HMAC(salt, owner ‖ raw agent id).

    Scoped by owner so two people naming their Agent ``ops-mac-01`` do not
    collide, and hashed so the raw Agent name — which is often a hostname —
    never lands on disk (RT-013 pattern).
    """
    validate_agent_id(raw_agent_id)
    mac = hmac.new(
        _salt_bytes(salt_hex),
        f"agent|v1|{owner_ref}|{raw_agent_id}".encode("utf-8"),
        hashlib.sha256,
    )
    return BINDING_PREFIX + mac.hexdigest()[:DERIVED_ID_CHARS]


def _salt_bytes(salt_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(str(salt_hex))
    except ValueError as exc:
        raise RegistryError("登记表的 owner_ref_salt 不是十六进制") from exc
    if len(raw) < SALT_BYTES:
        raise RegistryError(f"登记表的 owner_ref_salt 少于 {SALT_BYTES} 字节")
    return raw


def verify_business_key(
    var_name: str,
    *,
    salt_hex: str,
    env: Optional[Mapping[str, str]] = None,
    probe: Callable[[str], str] = cwork_probe,
    now: Optional[datetime] = None,
) -> VerifiedIdentity:
    """Read the key from ``env[var_name]``, prove it, derive ``owner_ref``.

    Failure is always a refusal, never a fallback: an unset variable, an
    empty value or a probe that raises all end here with
    :class:`IdentityError`.  The probe's own error text is redacted of the
    key before it is quoted, because API clients happily echo their
    credentials back in exception messages.
    """
    source = os.environ if env is None else env
    if not var_name:
        raise IdentityError("--verify-env 必填：给业务 Key 所在的环境变量名，不要给 Key 本身")
    app_key = source.get(var_name, "")
    if not app_key:
        raise IdentityError(
            f"环境变量 {var_name} 未设置或为空——无法验证身份，拒绝签发。"
        )
    try:
        label = probe(app_key)
    except Exception as exc:  # noqa: BLE001 - any probe failure is a refusal
        detail = redact(str(exc) or type(exc).__name__, app_key)
        raise IdentityError(f"业务 Key 未通过认证面校验，拒绝签发：{detail}") from None
    return VerifiedIdentity(
        owner_ref=derive_owner_ref(salt_hex, app_key),
        basis=OWNER_REF_BASIS,
        probe=str(label or DEFAULT_PROBE_LABEL),
        verified_at=iso(now or utc_now()),
    )


# ── validation ──────────────────────────────────────────────────────────────


def validate_agent_id(raw_agent_id: object) -> str:
    if not isinstance(raw_agent_id, str) or not _AGENT_ID_ALLOWED.match(raw_agent_id):
        raise UsageError(
            "--agent-id 只能是字母数字开头、由字母数字与 . _ : @ - 组成的 1~256 字符串"
        )
    return raw_agent_id


def validate_kb_ids(kb_ids: Sequence[str]) -> List[str]:
    """Non-empty, de-duplicated, order-preserving, control-character free."""
    cleaned: List[str] = []
    for item in kb_ids or ():
        text = str(item).strip()
        if not text:
            raise UsageError("--kb-id 不能为空")
        if len(text) > 256 or any(ord(ch) < 32 for ch in text):
            raise UsageError(f"--kb-id 非法：{text[:40]!r}")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise UsageError("--kb-id 至少给一个：token 的授权面就是这份名单")
    return cleaned


def validate_ttl_days(ttl_days: object) -> int:
    try:
        days = int(ttl_days)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UsageError("--ttl-days 必须是整数") from exc
    if not 1 <= days <= MAX_TTL_DAYS:
        raise UsageError(f"--ttl-days 取值范围是 1~{MAX_TTL_DAYS}")
    return days


def validate_registry(data: object) -> dict:
    if not isinstance(data, dict):
        raise RegistryError("登记表不是 JSON 对象")
    if data.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError(
            f"登记表 schema 应为 {REGISTRY_SCHEMA}，实际 {data.get('schema')!r}"
        )
    if not isinstance(data.get("tokens"), list):
        raise RegistryError("登记表缺少 tokens 列表")
    _salt_bytes(str(data.get("owner_ref_salt") or ""))
    if isinstance(data.get("membership_epoch"), bool) or not isinstance(
        data.get("membership_epoch"), int
    ):
        raise RegistryError("登记表的 membership_epoch 必须是整数")
    return data


# ── registry file ───────────────────────────────────────────────────────────


def new_registry(
    *, now: Optional[datetime] = None, max_active_per_owner: int = DEFAULT_MAX_ACTIVE_PER_OWNER
) -> dict:
    stamp = iso(now or utc_now())
    if isinstance(max_active_per_owner, bool) or not isinstance(max_active_per_owner, int):
        raise UsageError("--max-active 必须是整数")
    if max_active_per_owner < 1:
        raise UsageError("--max-active 至少为 1")
    return {
        "schema": REGISTRY_SCHEMA,
        "created_at": stamp,
        "updated_at": stamp,
        "membership_epoch": 0,
        "max_active_per_owner": max_active_per_owner,
        "owner_ref_salt": secrets.token_hex(SALT_BYTES),
        "tokens": [],
        "receipts": [],
    }


def load_registry(path: Path | str) -> dict:
    target = Path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryError(
            f"登记表不存在：{target}——先跑 kb_token.py init --registry {target}"
        ) from exc
    except OSError as exc:
        raise RegistryError(f"登记表读取失败：{exc}") from exc
    try:
        data = loads(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is fail-closed
        raise RegistryError(f"登记表不是合法 JSON：{exc}") from exc
    return validate_registry(data)


def save_registry(path: Path | str, data: dict, *, now: Optional[datetime] = None) -> Path:
    """Atomic, 0600, whole-file replace.

    One ``os.replace`` per mutation is what makes ``reissue`` atomic: the old
    generation's revocation and the new generation's record land together or
    not at all, so no reader can observe a window with two live generations.
    """
    data["updated_at"] = iso(now or utc_now())
    body = dumps(data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".kb-token.")
    try:
        os.fchmod(handle, REGISTRY_MODE)
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    os.chmod(target, REGISTRY_MODE)
    return target


def init_registry(
    path: Path | str,
    *,
    now: Optional[datetime] = None,
    max_active_per_owner: int = DEFAULT_MAX_ACTIVE_PER_OWNER,
) -> dict:
    target = Path(path)
    if target.exists():
        raise ConflictError(f"登记表已存在：{target}——init 不覆盖已有登记表")
    data = new_registry(now=now, max_active_per_owner=max_active_per_owner)
    save_registry(target, data, now=now)
    return data


# ── record helpers ──────────────────────────────────────────────────────────


def _new_receipt_id() -> str:
    body = "".join(secrets.choice(_ID_ALPHABET) for _ in range(26))
    return f"{RECEIPT_ID_PREFIX}{body}"


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(dumps(dict(payload))).hexdigest()


def token_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def token_id_for(digest: str) -> str:
    """The public handle for a token: a truncation of its own digest.

    Deterministic rather than random so ``list`` and ``revoke`` speak the
    same name without the registry needing a second identifier, and safe to
    print because it is a one-way function of a value nobody can invert.
    """
    return TOKEN_ID_PREFIX + digest[:TOKEN_ID_CHARS]


def records(data: Mapping[str, Any]) -> List[dict]:
    return [row for row in data.get("tokens", []) if isinstance(row, dict)]


def expires_at_of(record: Mapping[str, Any]) -> Optional[datetime]:
    try:
        return parse_iso(str(record.get("expires_at") or ""))
    except ValueError:
        return None


def record_status(record: Mapping[str, Any], now: datetime) -> str:
    if bool(record.get("revoked")):
        return STATUS_REVOKED
    moment = expires_at_of(record)
    if moment is None or moment <= now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def find_record(data: Mapping[str, Any], token_id: str) -> dict:
    for row in records(data):
        if row.get("token_id") == token_id:
            return row
    raise NotFound(f"登记表里没有 token_id={token_id!r}")


def binding_records(data: Mapping[str, Any], agent_binding_id: str) -> List[dict]:
    return [row for row in records(data) if row.get("agent_binding_id") == agent_binding_id]


def _bump_epoch(data: dict) -> int:
    epoch = int(data.get("membership_epoch", 0)) + 1
    data["membership_epoch"] = epoch
    return epoch


def _append_receipt(
    data: dict,
    *,
    action: str,
    record: Mapping[str, Any],
    actor: str,
    reason: str,
    now: datetime,
    epoch_before: int,
    epoch_after: int,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict:
    body: Dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": _new_receipt_id(),
        "action": action,
        "committed_at": iso(now),
        "actor": actor or "unspecified",
        "reason": reason or "unspecified",
        "token_id": record.get("token_id", ""),
        "owner_ref": record.get("owner_ref", ""),
        "agent_binding_id": record.get("agent_binding_id", ""),
        "kb_ids": list(record.get("kb_ids", [])),
        "generation": record.get("generation", 0),
        "membership_epoch_before": epoch_before,
        "membership_epoch_after": epoch_after,
    }
    if extra:
        body.update(dict(extra))
    body["receipt_sha256"] = canonical_sha256(body)
    data.setdefault("receipts", []).append(body)
    return body


def public_view(record: Mapping[str, Any], now: datetime) -> dict:
    """Metadata and the fingerprint.  Never the token, never its digest."""
    return {
        "token_id": record.get("token_id", ""),
        "owner_ref": record.get("owner_ref", ""),
        "owner_ref_basis": record.get("owner_ref_basis", OWNER_REF_BASIS),
        "agent_binding_id": record.get("agent_binding_id", ""),
        "kb_ids": list(record.get("kb_ids", [])),
        "generation": record.get("generation", 0),
        "membership_epoch": record.get("membership_epoch", 0),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at", ""),
        "revoked": bool(record.get("revoked")),
        "revoked_at": record.get("revoked_at"),
        "status": record_status(record, now),
    }


# ── mutations ───────────────────────────────────────────────────────────────


def active_count_for_owner(data: Mapping[str, Any], owner_ref: str, now: datetime) -> int:
    return sum(
        1
        for row in records(data)
        if row.get("owner_ref") == owner_ref and record_status(row, now) == STATUS_ACTIVE
    )


def issue_token(
    data: dict,
    *,
    identity: VerifiedIdentity,
    raw_agent_id: str,
    kb_ids: Sequence[str],
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: Optional[datetime] = None,
    actor: str = "",
    reason: str = "",
) -> Tuple[dict, str]:
    """Mint one token for ``(owner_ref, agent_binding_id)``.

    Returns ``(record, plaintext_token)``.  The plaintext is the caller's
    only copy — this function does not keep it and neither does the
    registry.
    """
    moment = now or utc_now()
    salt = str(data.get("owner_ref_salt") or "")
    scope = validate_kb_ids(kb_ids)
    days = validate_ttl_days(ttl_days)
    binding = derive_agent_binding_id(salt, identity.owner_ref, raw_agent_id)

    for row in binding_records(data, binding):
        if record_status(row, moment) == STATUS_ACTIVE:
            raise ConflictError(
                f"该 Agent 实例已有有效 token（{row.get('token_id')}）——"
                "换代请用 reissue，停用请用 revoke"
            )

    ceiling = int(data.get("max_active_per_owner", DEFAULT_MAX_ACTIVE_PER_OWNER))
    if active_count_for_owner(data, identity.owner_ref, moment) >= ceiling:
        raise ConflictError(
            f"该用户跨 Agent 实例的有效 token 已达上限 {ceiling}——先 revoke 一个再签发"
        )

    generation = max((int(row.get("generation", 0)) for row in binding_records(data, binding)), default=0) + 1
    plaintext = secrets.token_hex(TOKEN_BYTES)
    digest = token_digest(plaintext)
    epoch_before = int(data.get("membership_epoch", 0))
    epoch = _bump_epoch(data)

    record = {
        "schema": RECORD_SCHEMA,
        "token_id": token_id_for(digest),
        "token_sha256": digest,
        "owner_ref": identity.owner_ref,
        "owner_ref_basis": identity.basis,
        "identity_probe": identity.probe,
        "agent_binding_id": binding,
        "kb_ids": scope,
        "generation": generation,
        "membership_epoch": epoch,
        "created_at": iso(moment),
        "expires_at": iso(moment + timedelta(days=days)),
        "revoked": False,
        "revoked_at": None,
    }
    data.setdefault("tokens", []).append(record)
    _append_receipt(
        data,
        action="issue",
        record=record,
        actor=actor,
        reason=reason,
        now=moment,
        epoch_before=epoch_before,
        epoch_after=epoch,
        extra={"ttl_days": days},
    )
    return record, plaintext


def revoke_token(
    data: dict,
    *,
    token_id: str,
    now: Optional[datetime] = None,
    actor: str = "",
    reason: str = "",
) -> dict:
    """Mark one token dead.  Effective on the gateway's very next request."""
    moment = now or utc_now()
    record = find_record(data, token_id)
    if bool(record.get("revoked")):
        raise ConflictError(f"{token_id} 已经是吊销状态")
    epoch_before = int(data.get("membership_epoch", 0))
    epoch = _bump_epoch(data)
    record["revoked"] = True
    record["revoked_at"] = iso(moment)
    record["membership_epoch"] = epoch
    _append_receipt(
        data,
        action="revoke",
        record=record,
        actor=actor,
        reason=reason,
        now=moment,
        epoch_before=epoch_before,
        epoch_after=epoch,
    )
    return record


def reissue_token(
    data: dict,
    *,
    identity: VerifiedIdentity,
    raw_agent_id: str,
    kb_ids: Optional[Sequence[str]] = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: Optional[datetime] = None,
    actor: str = "",
    reason: str = "",
) -> Tuple[dict, str, List[str]]:
    """Advance the generation for one Agent instance; kill every older one.

    Atomic in the sense that matters: the whole thing happens in memory and
    the caller writes the registry once, so a reader never sees generation
    *n* and *n+1* both live.  One epoch bump covers the pair, because it is
    one authorization event.
    """
    moment = now or utc_now()
    salt = str(data.get("owner_ref_salt") or "")
    binding = derive_agent_binding_id(salt, identity.owner_ref, raw_agent_id)
    prior = binding_records(data, binding)
    if not prior:
        raise NotFound("该 Agent 实例还没有 token——第一支请用 issue")

    latest = max(prior, key=lambda row: int(row.get("generation", 0)))
    scope = validate_kb_ids(kb_ids if kb_ids else latest.get("kb_ids", []))
    days = validate_ttl_days(ttl_days)

    epoch_before = int(data.get("membership_epoch", 0))
    epoch = _bump_epoch(data)

    superseded: List[str] = []
    for row in prior:
        if bool(row.get("revoked")):
            continue
        row["revoked"] = True
        row["revoked_at"] = iso(moment)
        row["membership_epoch"] = epoch
        superseded.append(str(row.get("token_id", "")))

    generation = max(int(row.get("generation", 0)) for row in prior) + 1
    ceiling = int(data.get("max_active_per_owner", DEFAULT_MAX_ACTIVE_PER_OWNER))
    if active_count_for_owner(data, identity.owner_ref, moment) >= ceiling:
        raise ConflictError(
            f"该用户跨 Agent 实例的有效 token 已达上限 {ceiling}——先 revoke 一个再换代"
        )

    plaintext = secrets.token_hex(TOKEN_BYTES)
    digest = token_digest(plaintext)
    record = {
        "schema": RECORD_SCHEMA,
        "token_id": token_id_for(digest),
        "token_sha256": digest,
        "owner_ref": identity.owner_ref,
        "owner_ref_basis": identity.basis,
        "identity_probe": identity.probe,
        "agent_binding_id": binding,
        "kb_ids": scope,
        "generation": generation,
        "membership_epoch": epoch,
        "created_at": iso(moment),
        "expires_at": iso(moment + timedelta(days=days)),
        "revoked": False,
        "revoked_at": None,
    }
    data.setdefault("tokens", []).append(record)
    _append_receipt(
        data,
        action="reissue",
        record=record,
        actor=actor,
        reason=reason,
        now=moment,
        epoch_before=epoch_before,
        epoch_after=epoch,
        extra={"ttl_days": days, "superseded_token_ids": superseded},
    )
    return record, plaintext, superseded


# ── read side (this is what the gateway imports) ────────────────────────────


@dataclass(frozen=True)
class TokenDecision:
    """The gateway's verdict on one presented bearer value.

    ``status`` is deliberately three-valued.  "unauthorized" and "forbidden"
    are different facts — the first says *we do not know you*, the second
    says *we know you and this is not your library* — and collapsing them
    would either leak library membership to strangers or hide a
    misconfiguration from a legitimate holder.
    """

    status: str
    reason: str
    token_id: str = ""
    owner_ref: str = ""
    agent_binding_id: str = ""
    membership_epoch: int = 0
    generation: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def forbidden(self) -> bool:
        return self.status == "forbidden"


def decide(
    data: Mapping[str, Any],
    presented: str,
    *,
    kb_id: str,
    now: Optional[datetime] = None,
) -> TokenDecision:
    """Look one presented token up and answer ok / unauthorized / forbidden.

    Digest comparison uses :func:`hmac.compare_digest` and the loop does not
    break on a hit, so the work done is a function of the registry's size and
    not of how early the match sits.
    """
    moment = now or utc_now()
    digest = token_digest(presented)
    match: Optional[Mapping[str, Any]] = None
    for row in records(data):
        stored = str(row.get("token_sha256") or "")
        if len(stored) == DIGEST_HEX_LEN and hmac.compare_digest(digest, stored):
            match = row
    if match is None:
        return TokenDecision("unauthorized", "unknown_token")

    identity = {
        "token_id": str(match.get("token_id", "")),
        "owner_ref": str(match.get("owner_ref", "")),
        "agent_binding_id": str(match.get("agent_binding_id", "")),
        "membership_epoch": int(match.get("membership_epoch", 0) or 0),
        "generation": int(match.get("generation", 0) or 0),
    }
    status = record_status(match, moment)
    if status == STATUS_REVOKED:
        return TokenDecision("unauthorized", "revoked", **identity)
    if status == STATUS_EXPIRED:
        return TokenDecision("unauthorized", "expired", **identity)

    scope = [str(item) for item in match.get("kb_ids", [])]
    if not kb_id or kb_id not in scope:
        return TokenDecision("forbidden", "kb_not_in_scope", **identity)
    return TokenDecision("ok", "authorized", **identity)


@dataclass
class TokenFile:
    """A registry path that is re-read on every lookup.

    Not a cache.  "revoke 即刻生效" is only true if the process asking the
    question has no memory of the previous answer, and the file is a few
    kilobytes on local disk, so the honest implementation is also the cheap
    one.  A registry that has gone missing or unparseable answers
    *unauthorized* rather than raising: a gateway whose auth store broke must
    refuse everyone, not crash or let everyone through.
    """

    path: Path
    _last_error: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def load(self) -> dict:
        return load_registry(self.path)

    def decide(
        self, presented: str, *, kb_id: str, now: Optional[datetime] = None
    ) -> TokenDecision:
        try:
            data = self.load()
        except TokenError as exc:
            self._last_error = str(exc)
            return TokenDecision("unauthorized", "registry_unreadable")
        self._last_error = ""
        return decide(data, presented, kb_id=kb_id, now=now)

    def summary(self, *, now: Optional[datetime] = None) -> dict:
        """Counts and the epoch — nothing that identifies a holder."""
        moment = now or utc_now()
        data = self.load()
        rows = records(data)
        return {
            "enabled": True,
            "file": str(self.path),
            "records": len(rows),
            "active": sum(1 for row in rows if record_status(row, moment) == STATUS_ACTIVE),
            "membership_epoch": int(data.get("membership_epoch", 0)),
            "max_active_per_owner": int(
                data.get("max_active_per_owner", DEFAULT_MAX_ACTIVE_PER_OWNER)
            ),
        }


# ── CLI ─────────────────────────────────────────────────────────────────────


def error_payload(kind: str, message: str, **extra: object) -> dict:
    payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": kind, "message": message}}
    payload.update(extra)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KB 绑定 token 登记表：init / issue / reissue / revoke / list（输出一律 JSON）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_registry(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--registry", required=True, metavar="PATH", help="登记表 JSON 文件路径")
        return p

    def with_audit(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--actor", default="", help="操作者标识，写进审计回执")
        p.add_argument("--reason", default="", help="操作理由，写进审计回执")
        return p

    def with_identity(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--verify-env",
            required=True,
            metavar="VAR_NAME",
            help="业务 Key 所在的环境变量名（传变量名，不传 Key）；owner_ref 由它派生",
        )
        p.add_argument("--agent-id", required=True, help="Agent 实例标识；只用于派生，不落盘")
        return p

    init = with_registry(sub.add_parser("init", help="创建空登记表"))
    init.add_argument(
        "--max-active",
        type=int,
        default=DEFAULT_MAX_ACTIVE_PER_OWNER,
        help=f"每用户跨 Agent 实例的有效 token 上限（默认 {DEFAULT_MAX_ACTIVE_PER_OWNER}）",
    )

    issue = with_audit(with_identity(with_registry(sub.add_parser("issue", help="签发一支新 token"))))
    issue.add_argument("--kb-id", action="append", default=[], required=True, help="授权库标识，可重复")
    issue.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)

    reissue = with_audit(
        with_identity(with_registry(sub.add_parser("reissue", help="代际递增：旧代全部失效")))
    )
    reissue.add_argument("--kb-id", action="append", default=[], help="不给则沿用上一代的授权面")
    reissue.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)

    revoke = with_audit(with_registry(sub.add_parser("revoke", help="即刻吊销一支 token")))
    revoke.add_argument("--token-id", required=True, help="来自 list 的 token_id（指纹，不是 token）")

    listing = with_registry(sub.add_parser("list", help="只列元数据与指纹"))
    listing.add_argument("--kb-id", help="只看授权面包含该库的 token")
    listing.add_argument("--status", choices=(STATUS_ACTIVE, STATUS_REVOKED, STATUS_EXPIRED))
    return parser


def _emit(payload: dict) -> None:
    sys.stdout.write(dumps(payload).decode("utf-8"))
    sys.stdout.flush()


def run(
    args: argparse.Namespace,
    *,
    probe: Callable[[str], str],
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    moment = now or utc_now()

    if args.command == "init":
        data = init_registry(
            args.registry, now=moment, max_active_per_owner=args.max_active
        )
        return {
            "schema": INIT_SCHEMA,
            "ok": True,
            "registry": str(Path(args.registry)),
            "membership_epoch": data["membership_epoch"],
            "max_active_per_owner": data["max_active_per_owner"],
            "mode": "0600",
            "at": iso(moment),
        }

    data = load_registry(args.registry)

    if args.command == "issue":
        identity = verify_business_key(
            args.verify_env,
            salt_hex=str(data.get("owner_ref_salt") or ""),
            env=env,
            probe=probe,
            now=moment,
        )
        record, plaintext = issue_token(
            data,
            identity=identity,
            raw_agent_id=args.agent_id,
            kb_ids=args.kb_id,
            ttl_days=args.ttl_days,
            now=moment,
            actor=args.actor,
            reason=args.reason,
        )
        save_registry(args.registry, data, now=moment)
        return {
            "schema": ISSUE_SCHEMA,
            "ok": True,
            "token": plaintext,
            "token_shown_once": True,
            "note": "这是 token 唯一一次出现；登记表只存 sha256 摘要，丢了只能 reissue",
            "identity": {
                "owner_ref": identity.owner_ref,
                "basis": identity.basis,
                "probe": identity.probe,
                "verified_at": identity.verified_at,
            },
            "record": public_view(record, moment),
            "membership_epoch": data["membership_epoch"],
            "at": iso(moment),
        }

    if args.command == "reissue":
        identity = verify_business_key(
            args.verify_env,
            salt_hex=str(data.get("owner_ref_salt") or ""),
            env=env,
            probe=probe,
            now=moment,
        )
        record, plaintext, superseded = reissue_token(
            data,
            identity=identity,
            raw_agent_id=args.agent_id,
            kb_ids=args.kb_id or None,
            ttl_days=args.ttl_days,
            now=moment,
            actor=args.actor,
            reason=args.reason,
        )
        save_registry(args.registry, data, now=moment)
        return {
            "schema": REISSUE_SCHEMA,
            "ok": True,
            "token": plaintext,
            "token_shown_once": True,
            "note": "旧代已在同一次写入里全部失效",
            "identity": {
                "owner_ref": identity.owner_ref,
                "basis": identity.basis,
                "probe": identity.probe,
                "verified_at": identity.verified_at,
            },
            "record": public_view(record, moment),
            "superseded_token_ids": superseded,
            "membership_epoch": data["membership_epoch"],
            "at": iso(moment),
        }

    if args.command == "revoke":
        record = revoke_token(
            data, token_id=args.token_id, now=moment, actor=args.actor, reason=args.reason
        )
        save_registry(args.registry, data, now=moment)
        return {
            "schema": REVOKE_SCHEMA,
            "ok": True,
            "record": public_view(record, moment),
            "effective": "immediate",
            "membership_epoch": data["membership_epoch"],
            "at": iso(moment),
        }

    if args.command == "list":
        rows = [public_view(row, moment) for row in records(data)]
        if args.kb_id:
            rows = [row for row in rows if args.kb_id in row["kb_ids"]]
        if args.status:
            rows = [row for row in rows if row["status"] == args.status]
        rows.sort(key=lambda row: (row["owner_ref"], row["agent_binding_id"], row["generation"]))
        return {
            "schema": LIST_SCHEMA,
            "ok": True,
            "registry": str(Path(args.registry)),
            "membership_epoch": int(data.get("membership_epoch", 0)),
            "max_active_per_owner": int(
                data.get("max_active_per_owner", DEFAULT_MAX_ACTIVE_PER_OWNER)
            ),
            "count": len(rows),
            "tokens": rows,
            "at": iso(moment),
        }

    raise UsageError(f"未知子命令 {args.command!r}")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    probe: Callable[[str], str] = cwork_probe,
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        assert_no_plaintext_business_key(argv)
        assert_no_self_asserted_identity(argv)
        args = build_parser().parse_args(argv)
        _emit(run(args, probe=probe, now=now, env=env))
        return 0
    except TokenError as exc:
        _emit(error_payload(exc.kind, str(exc)))
        print(f"kb_token 失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary; JSON either way
        _emit(error_payload(type(exc).__name__, str(exc)))
        print(f"kb_token 失败：{exc}", file=sys.stderr)
        return 2


__all__ = [
    "DEFAULT_MAX_ACTIVE_PER_OWNER",
    "DEFAULT_TTL_DAYS",
    "REGISTRY_SCHEMA",
    "ConflictError",
    "IdentityError",
    "NotFound",
    "RegistryError",
    "SelfAssertedIdentity",
    "TokenDecision",
    "TokenError",
    "TokenFile",
    "UsageError",
    "VerifiedIdentity",
    "assert_no_self_asserted_identity",
    "decide",
    "derive_agent_binding_id",
    "derive_owner_ref",
    "init_registry",
    "issue_token",
    "load_registry",
    "main",
    "public_view",
    "record_status",
    "records",
    "reissue_token",
    "revoke_token",
    "save_registry",
    "token_digest",
    "verify_business_key",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
