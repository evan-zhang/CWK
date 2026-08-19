#!/usr/bin/env python3
"""RT-016: legacy raw shadow importer + data-level crosswalk producer.

Owned by RT-016.  This module is the **only** legitimate producer of RT-016
crosswalks and review entries in the multitenant runtime.  It never
modifies the legacy raw tree, never enumerates or writes into ``shared/``,
``registry/access-ledger/`` or ``tenants/<tenant>/views/`` directly, and
never issues any real Work-collab API call.  All persistent writes go
through the frozen RT-014 :class:`SharedEvidenceStore` and RT-015
:class:`AccessLedger` / :class:`TenantViewStore` public interfaces plus
RT-016's own durable crosswalk store under
``registry/rt016-crosswalk/<tenant_id>/``.

Frozen boundaries (see PRD §FR-06/FR-20, DESIGN §12 M2/M3, RT-016 plan
§9, references/安全威胁模型 T-04/T-06/T-11/T-12):

- Zero writes to the legacy raw tree.  The importer only reads legacy
  files via a caller-provided :class:`LegacySource` (which anchors an
  ``O_DIRECTORY | O_NOFOLLOW`` dirfd), captures a pre-scan hash and
  refuses to proceed if that hash later drifts.
- The three SHA-256 fields written into every durable crosswalk are
  distinct, never asserted equal:

  * ``legacy_source_sha256`` — SHA-256 of the exact legacy Markdown
    byte sequence, without any normalisation or re-encoding.
  * ``canonical_sha256`` — the RT-011 canonical envelope hash after
    NFC normalisation and RFC 8785 JCS serialisation.
  * ``object_bytes_sha256`` — SHA-256 of the RT-014 catalog storage
    bytes.  Currently identical to ``canonical_sha256`` because RT-014
    stores the same JCS bytes, but semantically distinct: RT-014 may
    later change the object envelope, and downstream consumers MUST
    NOT reuse one value in place of the other.

  Multiple crosswalks may share the same ``canonical_sha256`` (e.g. two
  legacy Markdown files that differ only in overlay/frontmatter but
  produce identical canonical bodies); RT-016 records each legacy source
  separately, keyed by ``legacy_source_sha256``.
- Access observations are always emitted with
  ``initial_status="granted"`` and
  ``observation_source="legacy_raw_decomposition"``.  RT-015 will refuse
  to promote a legacy observation to ``active`` without an authoritative
  receipt; this module never fabricates one.  Tenant view upsert is
  optional and only happens when the caller passes a pre-validated
  ``authority_receipt``; otherwise the tenant view envelope is preserved
  inside the crosswalk record with a fail-closed
  ``tenant_view_deferred_reason``.
- Tenant identity is a mandatory caller input.  The decomposer never
  infers ``tenant_id`` from raw content and refuses any legacy raw that
  purports to name a tenant.
- Legacy timeline snapshots contribute only hashes.  Their full
  reply/node payload is NEVER promoted into the canonical envelope or
  the tenant view; only reply / node IDs plus content SHA-256 fingerprints
  ever enter the RT-011 ``cwk.tenant_view.v1`` overlay.

RT-016 crosswalks live under::

    CWK_INSTANCE_ROOT/registry/rt016-crosswalk/<tenant_id>/
        crosswalks/<crosswalk_key>.json      (0o600, CAS)
        review/<review_id>.json              (0o600, CAS)
        manifests/<run_id>.jsonl             (0o600, append-only)
        locks/<crosswalk_key>.lock           (0o600, flock)

Staging (opaque, per-run) lives under::

    CWK_INSTANCE_ROOT/staging/rt016/<run_id>/

The importer never opens a legacy path from an arbitrary caller string;
callers provide either raw bytes directly, or a :class:`LegacySource`
handle wrapping a validated directory FD tree.  Path handling matches
the RT-012 dir-FD + O_NOFOLLOW pattern.
"""

from __future__ import annotations

import base64
import copy
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import secrets
import stat as _stat_mod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import cwk_access_ledger as _AL
import cwk_atomic_file as _A
import cwk_instance as _I
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _SE
import cwk_tenant_registry as _R
import cwk_tenant_view as _TV


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

DECOMPOSER_VERSION = "v1"
NORMALIZER_VERSION = "v1"

SCHEMA_DIR = _C.SCHEMA_ROOT / "rt016" / "schemas"

# Frozen leaf name for RT-016's own subtree under registry/.  RT-012 does
# not include this name in its REGISTRY_CHILDREN allow-list; RT-016
# creates it lazily under a validated dirfd using its own leaf-name
# grammar.  RT-016 does not add itself to RT-012's frozen registry list
# because that would require modifying a frozen module.
REGISTRY_SUBDIR = "rt016-crosswalk"
STAGING_SUBDIR = "rt016"

# Sub-directories inside each tenant crosswalk namespace.
_TENANT_SUBDIRS: tuple[str, ...] = (
    "crosswalks",
    "review",
    "manifests",
    "locks",
)

# Frozen opaque ID prefixes.
CROSSWALK_KEY_PREFIX = "cw_"
REVIEW_ID_PREFIX = "qe_"
RUN_ID_PREFIX = "run_"

_BASE32_TAIL = "aeimquy4"

_CROSSWALK_KEY_REGEX = re.compile(r"\Acw_[a-z2-7]{26}\Z")
_REVIEW_ID_REGEX = re.compile(r"\Aqe_[a-z2-7]{26}\Z")
_RUN_ID_REGEX = re.compile(r"\Arun_[a-z2-7]{26}\Z")

# Domain separators.
CROSSWALK_KEY_DOMAIN = b"cwk-rt016-crosswalk-key-v1"
REVIEW_ID_DOMAIN = b"cwk-rt016-review-id-v1"
LEGACY_PATH_HASH_DOMAIN = b"cwk-rt016-legacy-path-hash-v1"
STAGING_KEY_DOMAIN = b"cwk-rt016-staging-key-v1"

# v2 identity domains (RT-016 v2 anchor-bound records).  Different from v1
# so v2 keys can never collide with v1 keys and vice versa; see
# `compute_crosswalk_key_v2` / `compute_review_id_v2`.
CROSSWALK_KEY_V2_DOMAIN = b"cwk-rt016-crosswalk-key-v2"
REVIEW_ID_V2_DOMAIN = b"cwk-rt016-review-id-v2"

# Current identity_version for records emitted by ShadowImporter and
# accepted by MigrationReconciler for PASS.
IDENTITY_VERSION = "v2"

_UTC = _dt.timezone.utc

# Frozen schema IDs (owned by RT-016).
_DECOMPOSE_REPORT_SCHEMA_ID = "cwk.pr001.rt016.decompose_report.v1"
_MIGRATION_CROSSWALK_SCHEMA_ID = "cwk.pr001.rt016.migration_crosswalk.v1"
_REVIEW_ENTRY_SCHEMA_ID = "cwk.pr001.rt016.review_entry.v1"
_MANIFEST_ENTRY_SCHEMA_ID = "cwk.pr001.rt016.migration_manifest_entry.v1"

# v2 (anchor-bound) schema IDs.
_MIGRATION_CROSSWALK_V2_SCHEMA_ID = "cwk.pr001.rt016.migration_crosswalk.v2"
_REVIEW_ENTRY_V2_SCHEMA_ID = "cwk.pr001.rt016.review_entry.v2"
_MANIFEST_ENTRY_V2_SCHEMA_ID = "cwk.pr001.rt016.migration_manifest_entry.v2"

# Set of crosswalk schema constants recognised by the loader.  Records
# whose `schema` field is not in this set fail closed at read time.
_KNOWN_CROSSWALK_SCHEMAS = frozenset(
    {"cwk.rt016.migration_crosswalk.v1", "cwk.rt016.migration_crosswalk.v2"}
)
_KNOWN_REVIEW_SCHEMAS = frozenset(
    {"cwk.rt016.review_entry.v1", "cwk.rt016.review_entry.v2"}
)
_KNOWN_MANIFEST_SCHEMAS = frozenset(
    {"cwk.rt016.migration_manifest_entry.v1", "cwk.rt016.migration_manifest_entry.v2"}
)

# Canonical envelope size limit: RT-011 canonical body is capped at 1 MiB
# (schema maxLength).  Anything larger enters quarantine unread.
_CANONICAL_BODY_MAX = 1_048_576

# Actor / reason grammar (matches RT-015 policy exactly; kept local to
# avoid depending on a private RT-015 helper).
_ACTOR_MAX_LEN = 128
_REASON_MAX_LEN = 256

# Frontmatter keys known to legacy raw (defensive; unknown keys are
# recorded in decompose_report.unknown_frontmatter_keys but do NOT alone
# cause quarantine).
_KNOWN_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "report_id",
        "title",
        "writer",
        "writer_id",
        "create_time",
        "update_time",
        "source_lane",
        "collection_mode",
        "change_type",
        "source_scopes",
    }
)
_KNOWN_ROW_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "reportId",
        "reportRecordId",
        "reportEventVO",
        "main",
        "title",
        "reportTitle",
        "content",
        "createTime",
        "updateTime",
        "creator",
        "userName",
        "writeEmpId",
        "writeEmpName",
        "writer_id",
        "source_user_id",
        "read",
        "readStatus",
        "todo",
        "todoStatus",
        "lane",
        "hasNewReply",
        "replyCount",
        "roles",
        "allowedActions",
    }
)
_KNOWN_REPLY_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "replyId",
        "commentId",
        "recordId",
        "content",
        "comment",
        "message",
        "opinion",
        "replyContent",
        "replyEmpName",
        "writeEmpName",
        "creatorName",
        "userName",
        "name",
        "empName",
        "userId",
        "writeEmpId",
        "replyTime",
        "createTime",
        "time",
        "updateTime",
        "sendTime",
        "replyList",
        "children",
        "childList",
        "subReplyList",
    }
)
_KNOWN_NODE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "nodeId",
        "nodeName",
        "type",
        "status",
        "level",
        "createTime",
        "updateTime",
        "finishTime",
        "operateTime",
        "userList",
        "name",
        "userName",
        "empName",
        "writeEmpName",
        "userId",
        "writeEmpId",
        "content",
        "opinion",
        "comment",
        "operate",
    }
)

# Legal timestamp patterns.  RFC 3339 / ISO 8601 with UTC-anchored offset.
# We refuse naive timestamps because we cannot invent a timezone.
_ISO_UTC_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
)

# Body section headers.  The stored value is the heading *text* only;
# the leading ``## `` and any surrounding whitespace / newline are added
# by the parser regex.
_BODY_HEADER = "Original Full Content For AI"
_ROW_HEADER = "List Row Metadata"
_SIMPLE_HEADER = "Record Simple Info"
_NODE_HEADER = "Node / Opinion Chain"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LegacyImportError(Exception):
    """Base error for RT-016.

    ``code`` is drawn from a small closed vocabulary so callers can
    build defensive control flow without parsing free-form messages.
    ``__str__`` never contains absolute host paths, credential material,
    legacy body bytes, or temporary URLs.
    """

    _CODES: frozenset[str] = frozenset(
        {
            "contract",
            "not_initialized",
            "not_found",
            "conflict",
            "state",
            "io",
            "corrupt",
            "log_injection",
            "path_containment",
            "legacy_drift",
            "duplicate_report_id",
            "authority_deferred",
            "ledger_denied",
            "canonical_missing",
            "internal",
        }
    )

    def __init__(
        self,
        message: str,
        *,
        code: str,
        crosswalk_key: str | None = None,
        review_id: str | None = None,
    ) -> None:
        if code not in self._CODES:  # pragma: no cover - defensive
            raise ValueError(f"invalid LegacyImportError code {code!r}")
        super().__init__(message)
        self.code = code
        self.crosswalk_key = crosswalk_key
        self.review_id = review_id

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"[{self.code}]", super().__str__()]
        if self.crosswalk_key is not None:
            parts.append(f"crosswalk_key={self.crosswalk_key}")
        if self.review_id is not None:
            parts.append(f"review_id={self.review_id}")
        return " ".join(parts)


class LogInjectionDetected(LegacyImportError):
    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"log injection detected in {field_name!r}",
            code="log_injection",
        )


class LegacyDriftDetected(LegacyImportError):
    def __init__(self, message: str = "legacy source bytes drifted between pre-scan and import") -> None:
        super().__init__(message, code="legacy_drift")


class DuplicateReportIdError(LegacyImportError):
    def __init__(self, report_key: str) -> None:
        super().__init__(
            f"duplicate legacy report_id under tenant (report_key={report_key})",
            code="duplicate_report_id",
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(payload: Any) -> bytes:
    return _C.canonical_json_bytes(_C.nfc_normalize(payload))


def _base32_lower_16(digest: bytes) -> str:
    encoded = base64.b32encode(digest[:16]).decode("ascii").lower().rstrip("=")
    if len(encoded) != 26 or encoded[-1] not in _BASE32_TAIL:  # pragma: no cover - defensive
        raise LegacyImportError(
            "base32 encoding failed byte-contract", code="internal"
        )
    return encoded


def new_run_id() -> str:
    """Return a fresh opaque run_id (``run_<26 base32>``)."""

    return RUN_ID_PREFIX + _base32_lower_16(secrets.token_bytes(16))


def compute_crosswalk_key(tenant_id: str, view_key: str, legacy_source_sha256: str) -> str:
    """Return the deterministic per-(tenant, view, legacy source) crosswalk key.

    Deterministic keying lets idempotent reruns detect that a given
    legacy raw has already produced a crosswalk without listing every
    file under the tenant directory.
    """

    tenant_id = _I.validate_tenant_id(tenant_id)
    if not _AL.compute_grant_key.__self__ if False else True:  # noqa: SIM108
        pass  # keep linter quiet
    if not isinstance(view_key, str) or not re.match(r"\Ag_[a-z2-7]{26}\Z", view_key):
        raise LegacyImportError("invalid view_key grammar", code="contract")
    if not isinstance(legacy_source_sha256, str) or not _C.SHA256_HEX_REGEX.match(legacy_source_sha256):
        raise LegacyImportError("invalid legacy_source_sha256 grammar", code="contract")
    material = (
        CROSSWALK_KEY_DOMAIN
        + b"\x00"
        + tenant_id.encode("utf-8")
        + b"\x00"
        + view_key.encode("ascii")
        + b"\x00"
        + legacy_source_sha256.encode("ascii")
    )
    digest = hashlib.sha256(material).digest()
    return CROSSWALK_KEY_PREFIX + _base32_lower_16(digest)


def compute_crosswalk_key_v2(
    tenant_id: str,
    source_namespace: str,
    source_kind: str,
    legacy_path_hash: str,
    legacy_source_sha256: str,
) -> str:
    """Return the deterministic v2 crosswalk key.

    v2 identity binds (tenant_id, source_namespace, source_kind,
    legacy_path_hash, legacy_source_sha256).  This is a strict superset of
    v1's material (v1 only included tenant + view_key + raw sha), which
    means:

    - Identical raw bytes at two different legacy paths under the same
      namespace produce **different** v2 crosswalk keys.
    - Identical raw bytes under two different namespaces produce
      different v2 crosswalk keys.
    - Identical raw bytes under two different `source_kind` values
      (``current_raw`` vs ``timeline_snapshot``) produce different v2
      crosswalk keys.

    Uses a distinct v2 domain separator so v2 keys can never collide
    with v1 keys.
    """

    tenant_id = _I.validate_tenant_id(tenant_id)
    if not isinstance(source_namespace, str) or not _C.SOURCE_NAMESPACE_REGEX.match(
        source_namespace
    ):
        raise LegacyImportError("invalid source_namespace grammar", code="contract")
    if source_kind not in ("current_raw", "timeline_snapshot"):
        raise LegacyImportError("invalid source_kind", code="contract")
    if not isinstance(legacy_path_hash, str) or not _C.SHA256_HEX_REGEX.match(legacy_path_hash):
        raise LegacyImportError("invalid legacy_path_hash grammar", code="contract")
    if not isinstance(legacy_source_sha256, str) or not _C.SHA256_HEX_REGEX.match(legacy_source_sha256):
        raise LegacyImportError("invalid legacy_source_sha256 grammar", code="contract")
    material = (
        CROSSWALK_KEY_V2_DOMAIN
        + b"\x00"
        + tenant_id.encode("utf-8")
        + b"\x00"
        + source_namespace.encode("ascii")
        + b"\x00"
        + source_kind.encode("ascii")
        + b"\x00"
        + legacy_path_hash.encode("ascii")
        + b"\x00"
        + legacy_source_sha256.encode("ascii")
    )
    digest = hashlib.sha256(material).digest()
    return CROSSWALK_KEY_PREFIX + _base32_lower_16(digest)


def compute_review_id_v2(
    tenant_id: str,
    source_namespace: str,
    source_kind: str,
    legacy_path_hash: str,
    legacy_source_sha256: str,
    run_id: str,
) -> str:
    """Return the deterministic v2 review id.

    Same identity semantics as :func:`compute_crosswalk_key_v2` but
    additionally scoped by ``run_id`` so a re-run under a different
    identifier yields a distinct review file (a review is scoped to a
    single run so operators can attribute quarantine reasons).
    """

    tenant_id = _I.validate_tenant_id(tenant_id)
    if not isinstance(source_namespace, str) or not _C.SOURCE_NAMESPACE_REGEX.match(
        source_namespace
    ):
        raise LegacyImportError("invalid source_namespace grammar", code="contract")
    if source_kind not in ("current_raw", "timeline_snapshot"):
        raise LegacyImportError("invalid source_kind", code="contract")
    if not isinstance(legacy_path_hash, str) or not _C.SHA256_HEX_REGEX.match(legacy_path_hash):
        raise LegacyImportError("invalid legacy_path_hash grammar", code="contract")
    if not isinstance(legacy_source_sha256, str) or not _C.SHA256_HEX_REGEX.match(legacy_source_sha256):
        raise LegacyImportError("invalid legacy_source_sha256 grammar", code="contract")
    if not isinstance(run_id, str) or not _RUN_ID_REGEX.match(run_id):
        raise LegacyImportError("invalid run_id grammar", code="contract")
    material = (
        REVIEW_ID_V2_DOMAIN
        + b"\x00"
        + tenant_id.encode("utf-8")
        + b"\x00"
        + source_namespace.encode("ascii")
        + b"\x00"
        + source_kind.encode("ascii")
        + b"\x00"
        + legacy_path_hash.encode("ascii")
        + b"\x00"
        + legacy_source_sha256.encode("ascii")
        + b"\x00"
        + run_id.encode("ascii")
    )
    digest = hashlib.sha256(material).digest()
    return REVIEW_ID_PREFIX + _base32_lower_16(digest)


def compute_review_id(
    tenant_id: str,
    source_namespace: str,
    legacy_source_sha256: str,
    run_id: str,
) -> str:
    """Return the deterministic per-(tenant, source_namespace, legacy source, run) review id.

    ``source_namespace`` was added post RT-016 v1 to close the
    cross-namespace idempotency collision reported by the RT-016
    independent acceptance report (Minor-2): the same raw bytes
    imported under two different ``source_namespace`` values must
    yield two independent review IDs so writes never collide silently.
    """

    tenant_id = _I.validate_tenant_id(tenant_id)
    if not isinstance(source_namespace, str) or not _C.SOURCE_NAMESPACE_REGEX.match(
        source_namespace
    ):
        raise LegacyImportError("invalid source_namespace grammar", code="contract")
    if not isinstance(legacy_source_sha256, str) or not _C.SHA256_HEX_REGEX.match(legacy_source_sha256):
        raise LegacyImportError("invalid legacy_source_sha256 grammar", code="contract")
    if not isinstance(run_id, str) or not _RUN_ID_REGEX.match(run_id):
        raise LegacyImportError("invalid run_id grammar", code="contract")
    material = (
        REVIEW_ID_DOMAIN
        + b"\x00"
        + tenant_id.encode("utf-8")
        + b"\x00"
        + source_namespace.encode("ascii")
        + b"\x00"
        + legacy_source_sha256.encode("ascii")
        + b"\x00"
        + run_id.encode("ascii")
    )
    digest = hashlib.sha256(material).digest()
    return REVIEW_ID_PREFIX + _base32_lower_16(digest)


def compute_legacy_path_hash(path_hint: str) -> str:
    """Return a domain-separated SHA-256 of a legacy path hint.

    Used only as an opaque tie-breaker across manifest entries; never
    exposes the real host path.  Callers may pass any string identifier
    they use to name the legacy source.
    """

    if not isinstance(path_hint, str) or not path_hint or len(path_hint) > 4096:
        raise LegacyImportError("path_hint must be a bounded string", code="contract")
    return hashlib.sha256(
        LEGACY_PATH_HASH_DOMAIN + b"\x00" + path_hint.encode("utf-8", errors="strict")
    ).hexdigest()


def _validate_actor_reason(actor: str, reason: str) -> None:
    """Restrict actor/reason to printable ASCII (\\x20-\\x7e).

    Rejects NUL/CR/LF/ESC/TAB/DEL and anything outside the printable
    ASCII range to defeat log injection.  Length caps match RT-015.
    """

    for name, value, max_len in (
        ("actor", actor, _ACTOR_MAX_LEN),
        ("reason", reason, _REASON_MAX_LEN),
    ):
        if not isinstance(value, str):
            raise LogInjectionDetected(name)
        if not value or len(value) > max_len:
            raise LogInjectionDetected(name)
        for ch in value:
            code_point = ord(ch)
            if code_point < 0x20 or code_point == 0x7f or code_point > 0x7e:
                raise LogInjectionDetected(name)


def _validate_crosswalk_integrity(payload: dict[str, Any]) -> None:
    """Cross-field integrity binding shared by v1 and v2 crosswalks.

    Post RT-016 v1 remediation of Minor-1 (independent acceptance
    report): the byte-level ``_canonical_bytes(payload) != raw`` check
    only proves JCS round-trip stability, not semantic consistency
    between the crosswalk's top-level fields and its embedded
    ``publish_receipt`` / ``tenant_view_envelope`` /
    ``decompose_report`` payloads.  A JCS-preserving attacker with
    write access to ``registry/rt016-crosswalk/`` could otherwise
    silently rewrite ``canonical_sha256`` / ``object_id`` /
    ``observe_grant_key`` / ``view_key`` / ``crosswalk_key`` and have
    the read layer return a semantically inconsistent record.

    This function contains the v1-level (self-referential) integrity
    checks that both v1 and v2 payloads must satisfy.  The additional
    v2 anchor-binding checks (source_kind, legacy_path_hash,
    v2-derived crosswalk_key) live in
    :func:`_validate_crosswalk_integrity_v2` and only apply to v2
    records.  Any inconsistency raises :class:`LegacyImportError` with
    ``code="corrupt"``.
    """

    if not isinstance(payload, dict):
        raise LegacyImportError(
            "crosswalk payload is not an object", code="corrupt"
        )
    schema_id = payload.get("schema")
    crosswalk_key = payload.get("crosswalk_key")
    tenant_id = payload.get("tenant_id")
    source_namespace = payload.get("source_namespace")
    report_id = payload.get("report_id")
    report_key = payload.get("report_key")
    view_key = payload.get("view_key")
    observe_grant_key = payload.get("observe_grant_key")
    canonical_sha256 = payload.get("canonical_sha256")
    object_id = payload.get("object_id")
    object_bytes_sha256 = payload.get("object_bytes_sha256")
    catalog_key = payload.get("catalog_key")
    catalog_revision = payload.get("catalog_revision")
    legacy_source_sha256 = payload.get("legacy_source_sha256")
    tenant_view_envelope = payload.get("tenant_view_envelope") or {}
    publish_receipt = payload.get("publish_receipt") or {}
    decompose_report = payload.get("decompose_report") or {}

    def _fail(msg: str) -> None:
        raise LegacyImportError(
            msg,
            code="corrupt",
            crosswalk_key=crosswalk_key if isinstance(crosswalk_key, str) else None,
        )

    # 1. report_key composition.
    try:
        expected_report_key = _C.compose_report_key(source_namespace, report_id)
    except _C.ContractError as exc:
        _fail(f"invalid source_namespace/report_id composition: {exc}")
    if report_key != expected_report_key:
        _fail("report_key does not match compose_report_key(source_namespace, report_id)")

    # 2. view_key == observe_grant_key == compute_grant_key(tenant, report_key).
    try:
        expected_grant_key = _AL.compute_grant_key(tenant_id, expected_report_key)
    except _AL.AccessLedgerError as exc:
        _fail(f"cannot recompute grant_key: {exc.code}")
    if view_key != expected_grant_key:
        _fail("view_key does not match H(tenant_id, report_key)")
    if observe_grant_key != expected_grant_key:
        _fail("observe_grant_key does not match H(tenant_id, report_key)")

    # 3. crosswalk_key derivation.  v1 uses (tenant, view_key, raw_sha);
    # v2 uses (tenant, ns, source_kind, path_hash, raw_sha).  We pick the
    # correct derivation based on the payload's schema constant so v1
    # records remain readable for audit but never satisfy v2 identity.
    if schema_id == "cwk.rt016.migration_crosswalk.v1":
        try:
            expected_crosswalk_key = compute_crosswalk_key(
                tenant_id, view_key, legacy_source_sha256
            )
        except LegacyImportError as exc:
            _fail(f"cannot recompute v1 crosswalk_key: {exc.code}")
        if crosswalk_key != expected_crosswalk_key:
            _fail("v1 crosswalk_key does not match H(tenant_id, view_key, legacy_source_sha256)")
    elif schema_id == "cwk.rt016.migration_crosswalk.v2":
        source_kind_val = payload.get("source_kind")
        legacy_path_hash_val = payload.get("legacy_path_hash")
        identity_version = payload.get("identity_version")
        if identity_version != "v2":
            _fail("v2 crosswalk missing identity_version=v2")
        try:
            expected_crosswalk_key = compute_crosswalk_key_v2(
                tenant_id,
                source_namespace,
                source_kind_val,
                legacy_path_hash_val,
                legacy_source_sha256,
            )
        except LegacyImportError as exc:
            _fail(f"cannot recompute v2 crosswalk_key: {exc.code}")
        if crosswalk_key != expected_crosswalk_key:
            _fail(
                "v2 crosswalk_key does not match "
                "H(tenant_id, source_namespace, source_kind, legacy_path_hash, legacy_source_sha256)"
            )
    else:
        _fail(f"unknown crosswalk schema {schema_id!r}")

    # 4. canonical_sha256 must agree across every nested embedding.
    if publish_receipt.get("canonical_sha256") != canonical_sha256:
        _fail("publish_receipt.canonical_sha256 disagrees with top-level canonical_sha256")
    if tenant_view_envelope.get("canonical_sha256") != canonical_sha256:
        _fail("tenant_view_envelope.canonical_sha256 disagrees with top-level canonical_sha256")
    if decompose_report.get("canonical_sha256") != canonical_sha256:
        _fail("decompose_report.canonical_sha256 disagrees with top-level canonical_sha256")

    # 5. object_id agrees between top-level and publish_receipt.
    if publish_receipt.get("object_id") != object_id:
        _fail("publish_receipt.object_id disagrees with top-level object_id")

    # 6. catalog_key / catalog_revision agree.
    if publish_receipt.get("catalog_key") != catalog_key:
        _fail("publish_receipt.catalog_key disagrees with top-level catalog_key")
    if publish_receipt.get("catalog_revision") != catalog_revision:
        _fail("publish_receipt.catalog_revision disagrees with top-level catalog_revision")

    # 7. publish_receipt.report_key agrees with top-level report_key.
    if publish_receipt.get("report_key") != report_key:
        _fail("publish_receipt.report_key disagrees with top-level report_key")

    # 8. object_bytes_sha256 agrees with decompose_report.
    if decompose_report.get("object_bytes_sha256") != object_bytes_sha256:
        _fail("decompose_report.object_bytes_sha256 disagrees with top-level object_bytes_sha256")

    # 9. legacy_source_sha256 agrees with decompose_report.
    if decompose_report.get("legacy_source_sha256") != legacy_source_sha256:
        _fail("decompose_report.legacy_source_sha256 disagrees with top-level legacy_source_sha256")

    # 10. tenant_view_envelope.tenant_id / report_key agree.
    if tenant_view_envelope.get("tenant_id") != tenant_id:
        _fail("tenant_view_envelope.tenant_id disagrees with top-level tenant_id")
    if tenant_view_envelope.get("report_key") != report_key:
        _fail("tenant_view_envelope.report_key disagrees with top-level report_key")


@dataclass(frozen=True)
class _BoundReaderExpect:
    """Caller-supplied identity that every finder / CAS read MUST match.

    Whenever the importer looks up an existing crosswalk (finder for
    idempotency, CAS conflict branch during write, reconciler
    per-crosswalk verification) it does so knowing exactly which
    (tenant, source_namespace, source_kind, legacy_path_hash,
    legacy_source_sha256) tuple it expects.  The bound reader refuses
    any payload whose identity fields disagree with those expectations,
    or whose file name key disagrees with the recomputed v2
    crosswalk_key.
    """

    tenant_id: str
    source_namespace: str
    source_kind: str
    legacy_path_hash: str
    legacy_source_sha256: str
    filename_crosswalk_key: str


def _load_crosswalk_payload_bound(
    raw: bytes, *, expect: _BoundReaderExpect
) -> dict[str, Any]:
    """Bound v2 loader: schema + integrity + caller-identity match.

    Any of the following causes a corrupt fail-closed:

    - not a v2 crosswalk (v1 records are auditable via
      :meth:`ShadowImporter.read_crosswalk`, but never accepted at a
      bound-reader entry — v1 has no source_kind / legacy_path_hash
      binding, so it cannot satisfy v2 identity).
    - payload's ``tenant_id`` / ``source_namespace`` / ``source_kind`` /
      ``legacy_path_hash`` / ``legacy_source_sha256`` disagrees with
      ``expect``.
    - recomputed v2 ``crosswalk_key`` (from ``expect``) disagrees with
      the payload's ``crosswalk_key`` or with the file's key inferred
      from its name (``expect.filename_crosswalk_key``).
    """

    payload = _load_crosswalk_payload(
        raw, crosswalk_key=expect.filename_crosswalk_key
    )
    schema_id = payload.get("schema")
    if schema_id != "cwk.rt016.migration_crosswalk.v2":
        raise LegacyImportError(
            "bound reader refused non-v2 crosswalk",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("tenant_id") != expect.tenant_id:
        raise LegacyImportError(
            "crosswalk tenant_id disagrees with bound reader expectation",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("source_namespace") != expect.source_namespace:
        raise LegacyImportError(
            "crosswalk source_namespace disagrees with bound reader expectation",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("source_kind") != expect.source_kind:
        raise LegacyImportError(
            "crosswalk source_kind disagrees with bound reader expectation",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("legacy_path_hash") != expect.legacy_path_hash:
        raise LegacyImportError(
            "crosswalk legacy_path_hash disagrees with bound reader expectation",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("legacy_source_sha256") != expect.legacy_source_sha256:
        raise LegacyImportError(
            "crosswalk legacy_source_sha256 disagrees with bound reader expectation",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    # Recompute v2 key from expect and cross-check both the payload's
    # inner key and the file name.  If the attacker renamed a file into
    # a different key slot (e.g. moved crosswalk B's bytes into A's
    # filename), the filename crosswalk_key will not match the recomputed
    # one and we fail closed.
    recomputed_key = compute_crosswalk_key_v2(
        expect.tenant_id,
        expect.source_namespace,
        expect.source_kind,
        expect.legacy_path_hash,
        expect.legacy_source_sha256,
    )
    if recomputed_key != expect.filename_crosswalk_key:
        raise LegacyImportError(
            "filename crosswalk_key disagrees with recomputed v2 key",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    if payload.get("crosswalk_key") != recomputed_key:
        raise LegacyImportError(
            "payload crosswalk_key disagrees with recomputed v2 key",
            code="corrupt",
            crosswalk_key=expect.filename_crosswalk_key,
        )
    return payload


def _load_crosswalk_payload(raw: bytes, *, crosswalk_key: str | None = None) -> dict[str, Any]:
    """Parse and fully validate crosswalk bytes → payload (v1 or v2).

    Wraps ``json.JSONDecodeError`` / ``UnicodeDecodeError`` /
    ``_C.ContractError`` as :class:`LegacyImportError`
    (``code="corrupt"``) so callers only need to handle a single
    exception family (Info-1 remediation).  Discriminates between v1
    and v2 by the payload's ``schema`` field and validates against the
    corresponding schema.  Then runs the shared cross-field integrity
    binding.  Unknown ``schema`` values fail closed.

    Both v1 and v2 records remain readable through this loader (v1 for
    audit-only auditability of pre-migration records).  Whether a
    record is *accepted* for idempotency lookups or reconciliation
    PASS is enforced by the callers, not by this loader.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LegacyImportError(
            f"crosswalk bytes are not valid UTF-8: {exc}",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        ) from exc
    try:
        payload = _C.strict_json_loads(text)
    except json.JSONDecodeError as exc:
        raise LegacyImportError(
            f"crosswalk bytes are not strict JSON: {exc}",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        ) from exc
    except _C.ContractError as exc:
        raise LegacyImportError(
            f"crosswalk JSON violates duplicate-key rule: {exc}",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        ) from exc
    if not isinstance(payload, dict):
        raise LegacyImportError(
            "crosswalk payload is not a JSON object",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        )
    schema_id = payload.get("schema")
    if schema_id == "cwk.rt016.migration_crosswalk.v2":
        target_schema_id = _MIGRATION_CROSSWALK_V2_SCHEMA_ID
    elif schema_id == "cwk.rt016.migration_crosswalk.v1":
        target_schema_id = _MIGRATION_CROSSWALK_SCHEMA_ID
    else:
        raise LegacyImportError(
            f"unknown crosswalk schema {schema_id!r}",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        )
    try:
        _validate_against(target_schema_id, payload)
        _C.validate_tenant_view(payload["tenant_view_envelope"])
    except LegacyImportError:
        raise
    except _C.ContractError as exc:
        raise LegacyImportError(
            f"crosswalk fails RT-011 tenant_view schema: {exc}",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        ) from exc
    if _canonical_bytes(payload) != raw:
        raise LegacyImportError(
            "crosswalk bytes not canonical JCS",
            code="corrupt",
            crosswalk_key=crosswalk_key,
        )
    _validate_crosswalk_integrity(payload)
    return payload


def _load_review_payload(raw: bytes, *, review_id: str | None = None) -> dict[str, Any]:
    """Parse and fully validate a review entry (v1 or v2)."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LegacyImportError(
            f"review bytes are not valid UTF-8: {exc}",
            code="corrupt",
            review_id=review_id,
        ) from exc
    try:
        payload = _C.strict_json_loads(text)
    except json.JSONDecodeError as exc:
        raise LegacyImportError(
            f"review bytes are not strict JSON: {exc}",
            code="corrupt",
            review_id=review_id,
        ) from exc
    except _C.ContractError as exc:
        raise LegacyImportError(
            f"review JSON violates duplicate-key rule: {exc}",
            code="corrupt",
            review_id=review_id,
        ) from exc
    if not isinstance(payload, dict):
        raise LegacyImportError(
            "review payload is not a JSON object",
            code="corrupt",
            review_id=review_id,
        )
    schema_id = payload.get("schema")
    if schema_id == "cwk.rt016.review_entry.v2":
        target_schema_id = _REVIEW_ENTRY_V2_SCHEMA_ID
    elif schema_id == "cwk.rt016.review_entry.v1":
        target_schema_id = _REVIEW_ENTRY_SCHEMA_ID
    else:
        raise LegacyImportError(
            f"unknown review schema {schema_id!r}",
            code="corrupt",
            review_id=review_id,
        )
    try:
        _validate_against(target_schema_id, payload)
    except LegacyImportError:
        raise
    return payload


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: dict[str, Any] = {}


def _load_schema(schema_id: str) -> Any:
    if schema_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[schema_id]
    filename_by_id = {
        _DECOMPOSE_REPORT_SCHEMA_ID: "decompose_report.schema.json",
        _MIGRATION_CROSSWALK_SCHEMA_ID: "migration_crosswalk.schema.json",
        _REVIEW_ENTRY_SCHEMA_ID: "review_entry.schema.json",
        _MANIFEST_ENTRY_SCHEMA_ID: "migration_manifest_entry.schema.json",
        _MIGRATION_CROSSWALK_V2_SCHEMA_ID: "migration_crosswalk_v2.schema.json",
        _REVIEW_ENTRY_V2_SCHEMA_ID: "review_entry_v2.schema.json",
        _MANIFEST_ENTRY_V2_SCHEMA_ID: "migration_manifest_entry_v2.schema.json",
    }
    filename = filename_by_id.get(schema_id)
    if filename is None:  # pragma: no cover - defensive
        raise LegacyImportError(f"unknown schema id {schema_id!r}", code="contract")
    payload = _C.strict_json_load_path(SCHEMA_DIR / filename)
    _SCHEMA_CACHE[schema_id] = payload
    return payload


def _validate_against(schema_id: str, payload: Any) -> None:
    schema = _load_schema(schema_id)
    try:
        _C._validate_schema(schema, payload, "$", root_schema=schema)
    except _C.ContractError as exc:
        raise LegacyImportError(
            f"schema {schema_id} failed: {exc}", code="contract"
        ) from exc
    forbidden = schema.get("customKeywords", {}).get("deepForbiddenProperties")
    if forbidden:
        try:
            _C._iter_deep_forbidden(payload, frozenset(forbidden), path="$")
        except _C.ContractError as exc:
            raise LegacyImportError(
                f"forbidden field present: {exc}", code="contract"
            ) from exc


# ---------------------------------------------------------------------------
# Directory helpers (RT-016 owned; dirfd anchored)
# ---------------------------------------------------------------------------


def _openat_dir_nofollow(parent_fd: int, name: str) -> int:
    """Open a subdirectory beneath ``parent_fd`` with O_DIRECTORY|O_NOFOLLOW.

    Mirrors the RT-014 / RT-015 helpers so RT-016 does not need to
    modify RT-012's frozen leaf-name allow-list.  Only accepts leaf
    names drawn from the RT-016 grammar (validated by the caller).
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LegacyImportError(
                "child is a symlink; refusing to follow",
                code="path_containment",
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise LegacyImportError(
                "child is not a directory", code="path_containment"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise LegacyImportError("child does not exist", code="not_found") from exc
        raise LegacyImportError(
            f"cannot open child (errno={exc.errno})", code="io"
        ) from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise LegacyImportError(
            f"cannot stat child (errno={exc.errno})", code="io"
        ) from exc
    if not _stat_mod.S_ISDIR(st.st_mode):
        os.close(fd)
        raise LegacyImportError("child is not a directory", code="path_containment")
    return fd


# ---------------------------------------------------------------------------
# Data classes returned by the decomposer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecomposeResult:
    status: str  # "ok" | "quarantined"
    canonical_envelope: Optional[dict[str, Any]]
    tenant_view_envelope: Optional[dict[str, Any]]
    access_observation: Optional[dict[str, Any]]
    decompose_report: dict[str, Any]
    quarantine_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportReceipt:
    outcome: str  # "complete" | "review" | "undecomposable"
    crosswalk_key: Optional[str]
    review_id: Optional[str]
    canonical_sha256: Optional[str]
    object_bytes_sha256: Optional[str]
    view_key: Optional[str]
    grant_key: Optional[str]
    tenant_view_written: bool
    tenant_view_deferred_reason: Optional[str]
    tenant_view_record_revision: Optional[int]
    quarantine_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryReport:
    tenants_scanned: int = 0
    staging_orphans_removed: int = 0
    crosswalk_orphans_removed: int = 0
    review_orphans_removed: int = 0
    manifest_orphans_removed: int = 0
    inconsistencies: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# LegacyRawDecomposer
# ---------------------------------------------------------------------------


class LegacyRawDecomposer:
    """Parse legacy Markdown/JSON raw into the frozen 3-tuple.

    Every decomposition either returns a fully-validated
    :class:`DecomposeResult` with status ``"ok"``, or a quarantined
    :class:`DecomposeResult` with status ``"quarantined"`` and no
    canonical / view / observation payloads.  Never truncates a body,
    never invents a timestamp, never guesses an author.
    """

    __slots__ = ("_dec_ver", "_norm_ver")

    def __init__(
        self,
        *,
        decomposer_version: str = DECOMPOSER_VERSION,
        normalizer_version: str = NORMALIZER_VERSION,
    ) -> None:
        if not re.match(r"^v[0-9]{1,4}$", decomposer_version):
            raise LegacyImportError("invalid decomposer_version", code="contract")
        if not re.match(r"^v[0-9]{1,4}$", normalizer_version):
            raise LegacyImportError("invalid normalizer_version", code="contract")
        self._dec_ver = decomposer_version
        self._norm_ver = normalizer_version

    # -- public entry point ---------------------------------------------

    def decompose(
        self,
        *,
        raw_bytes: bytes,
        tenant_id: str,
        source_namespace: str,
        run_started_at: str,
        source_kind: str = "current_raw",
        timeline_snapshot_bytes: Optional[list[bytes]] = None,
        timeline_event_bytes: Optional[list[bytes]] = None,
    ) -> DecomposeResult:
        """Decompose one legacy raw bytes sequence.

        Parameters
        ----------
        raw_bytes:
            The exact legacy Markdown bytes as they appear on disk;
            SHA-256 of this input becomes ``legacy_source_sha256``.
        tenant_id:
            Caller-supplied tenant ID (from operator migration config).
            Never inferred from raw content.
        source_namespace:
            Caller-supplied source namespace (frozen per operator
            deployment).  Never inferred from raw content.
        run_started_at:
            ISO-8601 timestamp of the migration run (used as the
            ``observed_at`` for the tenant view and access observation).
        source_kind:
            ``"current_raw"`` (default) or ``"timeline_snapshot"``.
        timeline_snapshot_bytes:
            Optional list of full timeline snapshot bytes (SHA-256
            of each is recorded in ``timeline_snapshot_hashes``).
        timeline_event_bytes:
            Optional list of full timeline event bytes (SHA-256 of each
            is recorded in ``timeline_event_hashes`` and cross-checked
            against the events that the raw itself references).
        """

        # Baseline validation.
        if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
            raise LegacyImportError("raw_bytes must be bytes-like", code="contract")
        raw_bytes = bytes(raw_bytes)
        _I.validate_tenant_id(tenant_id)
        if not isinstance(source_namespace, str) or not _C.SOURCE_NAMESPACE_REGEX.match(
            source_namespace
        ):
            raise LegacyImportError("invalid source_namespace", code="contract")
        if not isinstance(run_started_at, str):
            raise LegacyImportError("run_started_at must be string", code="contract")
        # Sanity check the RFC3339 shape.
        _parse_iso_utc(run_started_at, allow_field_name="run_started_at")
        if source_kind not in ("current_raw", "timeline_snapshot"):
            raise LegacyImportError("invalid source_kind", code="contract")

        legacy_source_sha256 = _sha256_bytes(raw_bytes)

        # Prepare a running decompose report skeleton.
        quarantine_reasons: list[str] = []
        field_sources: dict[str, str] = {}
        hit_rules: list[str] = []
        unknown_frontmatter_keys: list[str] = []
        unknown_row_keys: list[str] = []
        unknown_reply_keys: list[str] = []
        unknown_node_keys: list[str] = []
        timeline_check = {
            "snapshot_count": len(timeline_snapshot_bytes or []),
            "event_count": len(timeline_event_bytes or []),
            "matched_event_count": 0,
            "unmatched_event_count": 0,
            "coverage_ok": True,
        }
        body_bytes_length = 0
        body_truncation_would_occur = False

        # 1. Decode text safely.
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            quarantine_reasons.append("malformed_frontmatter")
            return self._quarantine(
                legacy_source_sha256=legacy_source_sha256,
                field_sources=field_sources,
                hit_rules=hit_rules,
                unknown_frontmatter_keys=unknown_frontmatter_keys,
                unknown_row_keys=unknown_row_keys,
                unknown_reply_keys=unknown_reply_keys,
                unknown_node_keys=unknown_node_keys,
                timeline_check=timeline_check,
                body_bytes_length=body_bytes_length,
                body_truncation_would_occur=False,
                quarantine_reasons=quarantine_reasons,
            )

        # 2. Frontmatter.
        try:
            frontmatter = _parse_frontmatter(text)
            hit_rules.append("frontmatter_parsed")
        except _MalformedFrontmatter:
            quarantine_reasons.append("malformed_frontmatter")
            return self._quarantine(
                legacy_source_sha256=legacy_source_sha256,
                field_sources=field_sources,
                hit_rules=hit_rules,
                unknown_frontmatter_keys=unknown_frontmatter_keys,
                unknown_row_keys=unknown_row_keys,
                unknown_reply_keys=unknown_reply_keys,
                unknown_node_keys=unknown_node_keys,
                timeline_check=timeline_check,
                body_bytes_length=body_bytes_length,
                body_truncation_would_occur=False,
                quarantine_reasons=quarantine_reasons,
            )
        if not frontmatter:
            quarantine_reasons.append("missing_frontmatter")
        for key in sorted(frontmatter.keys()):
            if key not in _KNOWN_FRONTMATTER_KEYS:
                unknown_frontmatter_keys.append(key)

        # 3. Row metadata.
        row = _parse_json_section(text, _ROW_HEADER)
        if not isinstance(row, dict):
            row = {}
        else:
            hit_rules.append("row_metadata_parsed")
        for key in sorted(row.keys()):
            if key not in _KNOWN_ROW_KEYS:
                unknown_row_keys.append(key)

        # 4. Simple info (reply structures) and node info (workflow).
        simple = _parse_json_section(text, _SIMPLE_HEADER)
        if not isinstance(simple, dict):
            simple = {}
        else:
            hit_rules.append("simple_info_parsed")
        node = _parse_json_section(text, _NODE_HEADER)
        if not isinstance(node, dict):
            node = {}
        else:
            hit_rules.append("node_chain_parsed")

        replies_raw = simple.get("replyList") if isinstance(simple, dict) else []
        if not isinstance(replies_raw, list):
            replies_raw = []
        nodes_raw = node.get("nodeList") if isinstance(node, dict) else []
        if not isinstance(nodes_raw, list):
            nodes_raw = []

        reply_items = list(_flatten_replies(replies_raw))
        node_items = list(_iter_nodes(nodes_raw))

        # Record unknown reply / node keys (for review, never blocking).
        for item in reply_items:
            for key in item.keys():
                if key not in _KNOWN_REPLY_KEYS:
                    unknown_reply_keys.append(key)
        for item in node_items:
            for key in item.keys():
                if key not in _KNOWN_NODE_KEYS:
                    unknown_node_keys.append(key)
        unknown_reply_keys = sorted(set(unknown_reply_keys))
        unknown_node_keys = sorted(set(unknown_node_keys))

        # 5. Body section.
        body_text = _extract_section_body(text, _BODY_HEADER)
        if body_text is None:
            quarantine_reasons.append("missing_body_section")
        else:
            body_bytes_length = len(body_text.encode("utf-8"))
            if body_bytes_length > _CANONICAL_BODY_MAX:
                body_truncation_would_occur = True
                quarantine_reasons.append("oversize_body")
            elif body_bytes_length == 0:
                quarantine_reasons.append("empty_body")
            elif _contains_forbidden_control_chars(body_text):
                # NUL / bare form feed / vertical tab in body — refuse.
                quarantine_reasons.append("control_chars_in_field")

        # 6. Report id (frontmatter is authoritative).
        report_id_value = frontmatter.get("report_id")
        if report_id_value:
            field_sources["report_id"] = "frontmatter:report_id"
        elif isinstance(row.get("reportId"), str) and row["reportId"]:
            report_id_value = row["reportId"]
            field_sources["report_id"] = "row_metadata:reportId"
            hit_rules.append("report_id_from_row_metadata")
        elif isinstance(row.get("reportId"), int):
            report_id_value = str(row["reportId"])
            field_sources["report_id"] = "row_metadata:reportId"
            hit_rules.append("report_id_from_row_metadata")
        else:
            quarantine_reasons.append("missing_report_id")
        if report_id_value is not None:
            if not isinstance(report_id_value, str):
                report_id_value = str(report_id_value)
            if not _C.REPORT_ID_REGEX.match(report_id_value):
                quarantine_reasons.append("malformed_report_id")

        # 7. Title.
        title_value = frontmatter.get("title") or (
            row.get("main") or row.get("title") or row.get("reportTitle")
        )
        if title_value:
            field_sources["title"] = (
                "frontmatter:title" if frontmatter.get("title") else "row_metadata:title"
            )
            if not isinstance(title_value, str):
                title_value = str(title_value)
            if not title_value.strip():
                quarantine_reasons.append("missing_title")
            if _contains_forbidden_control_chars(title_value):
                quarantine_reasons.append("control_chars_in_field")
        else:
            quarantine_reasons.append("missing_title")

        # 8. Author.  Prefer machine-parsable IDs (writeEmpId, writer_id,
        # source_user_id).  Frontmatter ``writer`` is treated as display
        # name.  If we cannot find a stable machine ID, quarantine.
        author_source_user_id = None
        author_display_name = None
        event_vo = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
        for candidate_key in (
            "source_user_id",
            "writer_id",
            "writeEmpId",
        ):
            for source_container, source_label in (
                (frontmatter, "frontmatter"),
                (row, "row_metadata"),
                (event_vo, "row_metadata.reportEventVO"),
            ):
                value = source_container.get(candidate_key)
                if isinstance(value, str) and value:
                    author_source_user_id = value
                    field_sources["author.source_user_id"] = f"{source_label}:{candidate_key}"
                    hit_rules.append(f"author_from_{source_label.split('.',1)[0]}_{candidate_key}")
                    break
                if isinstance(value, int):
                    author_source_user_id = str(value)
                    field_sources["author.source_user_id"] = f"{source_label}:{candidate_key}"
                    hit_rules.append(f"author_from_{source_label.split('.',1)[0]}_{candidate_key}")
                    break
            if author_source_user_id is not None:
                break

        if author_source_user_id is None:
            quarantine_reasons.append("missing_author")
        elif not re.match(r"^[A-Za-z0-9_\-.]+$", author_source_user_id):
            quarantine_reasons.append("author_source_user_id_invalid")

        display_candidate = (
            frontmatter.get("writer")
            or row.get("writeEmpName")
            or row.get("creator")
            or (event_vo.get("name") if isinstance(event_vo, dict) else None)
        )
        if display_candidate:
            display_candidate = str(display_candidate)
            if _contains_forbidden_control_chars(display_candidate):
                quarantine_reasons.append("control_chars_in_field")
            else:
                author_display_name = display_candidate
                field_sources["author.display_name"] = (
                    "frontmatter:writer" if frontmatter.get("writer") else "row_metadata:writeEmpName"
                )

        # 9. Timestamps.  ``create_time`` -> canonical.created_at,
        # ``update_time`` (or fallback) -> canonical.source_updated_at.
        created_at_value = frontmatter.get("create_time") or _first_text(
            row, ("createTime",)
        ) or (event_vo.get("time") if isinstance(event_vo, dict) else None)
        updated_at_value = (
            frontmatter.get("update_time")
            or _first_text(row, ("updateTime",))
            or created_at_value
        )
        created_at_iso: Optional[str] = None
        source_updated_at_iso: Optional[str] = None
        if created_at_value:
            try:
                created_at_iso = _normalize_timestamp(created_at_value)
                field_sources["created_at"] = "frontmatter:create_time" if frontmatter.get("create_time") else "row_metadata:createTime"
            except _AmbiguousTimezone:
                quarantine_reasons.append("ambiguous_timezone")
            except _UnparseableTimestamp:
                quarantine_reasons.append("unparseable_timestamp")
        else:
            quarantine_reasons.append("missing_created_at")

        if updated_at_value:
            try:
                source_updated_at_iso = _normalize_timestamp(updated_at_value)
                field_sources["source_updated_at"] = "frontmatter:update_time" if frontmatter.get("update_time") else "row_metadata:updateTime"
            except _AmbiguousTimezone:
                quarantine_reasons.append("ambiguous_timezone")
            except _UnparseableTimestamp:
                quarantine_reasons.append("unparseable_timestamp")
        else:
            quarantine_reasons.append("missing_source_updated_at")

        # 10. Timeline event coverage check (only meaningful when the
        # caller passes real timeline_event_bytes).  Every reply/node the
        # raw exposes must be represented in the timeline; if it is not,
        # we cannot claim the raw is coverage-complete.
        raw_event_hashes = _hash_event_payloads(reply_items) | _hash_event_payloads(node_items, node=True)
        timeline_hashes = {_sha256_bytes(b) for b in (timeline_event_bytes or [])}
        # Two independent coverage semantics:
        #   (a) if timeline_event_bytes is None -> no coverage claim.
        #   (b) otherwise every raw_event_hash must appear in
        #       timeline_hashes; ordering is deliberately NOT asserted
        #       (RT-016 records that legacy timelines have no
        #       trustworthy ordering).
        if timeline_event_bytes is not None:
            matched = raw_event_hashes & timeline_hashes
            unmatched = raw_event_hashes - timeline_hashes
            timeline_check["matched_event_count"] = len(matched)
            timeline_check["unmatched_event_count"] = len(unmatched)
            timeline_check["coverage_ok"] = not unmatched
            if unmatched:
                quarantine_reasons.append("timeline_event_hash_mismatch")

        # 11. Whitelist required canonical schema fields; if any are
        # missing at this point, quarantine before building the envelope.
        canonical_envelope: Optional[dict[str, Any]] = None
        tenant_view_envelope: Optional[dict[str, Any]] = None
        access_observation: Optional[dict[str, Any]] = None
        object_bytes_sha256: Optional[str] = None
        canonical_sha256: Optional[str] = None

        if not quarantine_reasons:
            # Build canonical envelope; NFC + JCS + RT-011 validation.
            author_payload = {"source_user_id": author_source_user_id}
            if author_display_name is not None:
                author_payload["display_name"] = author_display_name
            partial = {
                "schema": "cwk.canonical_report.v1",
                "source_namespace": source_namespace,
                "report_id": report_id_value,
                "title": title_value,
                "author": author_payload,
                "created_at": created_at_iso,
                "source_updated_at": source_updated_at_iso,
                "body": body_text,
                "normalizer_version": self._norm_ver,
            }
            partial_normalised = _C.nfc_normalize(partial)
            recomputed = _C.canonical_sha256(partial_normalised)
            envelope = dict(partial_normalised)
            envelope["canonical_sha256"] = recomputed
            try:
                _C.validate_canonical_envelope(envelope)
            except _C.ContractError:
                quarantine_reasons.append("canonical_validation_failed")

            if not quarantine_reasons:
                canonical_envelope = envelope
                canonical_sha256 = envelope["canonical_sha256"]
                object_bytes_sha256 = _sha256_bytes(_canonical_bytes(envelope))
                # Compose report_key + tenant view envelope.
                report_key = _C.compose_report_key(source_namespace, report_id_value)
                tenant_view_envelope = self._build_tenant_view(
                    tenant_id=tenant_id,
                    report_key=report_key,
                    canonical_sha256=canonical_sha256,
                    frontmatter=frontmatter,
                    row=row,
                    reply_items=reply_items,
                    node_items=node_items,
                    observed_at=run_started_at,
                    field_sources=field_sources,
                    hit_rules=hit_rules,
                )
                try:
                    _C.validate_tenant_view(tenant_view_envelope)
                except _C.ContractError:
                    quarantine_reasons.append("canonical_validation_failed")
                    tenant_view_envelope = None
                    canonical_envelope = None
                    canonical_sha256 = None
                    object_bytes_sha256 = None

            if not quarantine_reasons and canonical_envelope is not None:
                access_observation = {
                    "schema": "cwk.access_observation.v1",
                    "tenant_id": tenant_id,
                    "source_namespace": source_namespace,
                    "report_id": report_id_value,
                    "observed_at": run_started_at,
                    "observation_source": "legacy_raw_decomposition",
                    "roles": ["receiver"],
                    "visibility_scope": "unknown",
                    "initial_status": "granted",
                    "evidence_refs": [
                        f"decomposer_version:{self._dec_ver}",
                        f"normalizer_version:{self._norm_ver}",
                        f"legacy_source_sha256:{legacy_source_sha256}",
                    ],
                }
                try:
                    _C.validate_access_observation(access_observation)
                except _C.ContractError:
                    quarantine_reasons.append("canonical_validation_failed")
                    access_observation = None
                    tenant_view_envelope = None
                    canonical_envelope = None
                    canonical_sha256 = None
                    object_bytes_sha256 = None

        # Build the decompose report either way.
        status = "ok" if not quarantine_reasons else "quarantined"
        decompose_report = {
            "schema": "cwk.rt016.decompose_report.v1",
            "decomposer_version": self._dec_ver,
            "normalizer_version": self._norm_ver,
            "legacy_source_sha256": legacy_source_sha256,
            "canonical_sha256": canonical_sha256,
            "object_bytes_sha256": object_bytes_sha256,
            "body_bytes_length": body_bytes_length,
            "body_truncation_would_occur": body_truncation_would_occur,
            "field_sources": dict(sorted(field_sources.items())),
            "hit_rules": sorted(set(hit_rules)),
            "unknown_frontmatter_keys": sorted(set(unknown_frontmatter_keys)),
            "unknown_row_keys": sorted(set(unknown_row_keys)),
            "unknown_reply_keys": unknown_reply_keys,
            "unknown_node_keys": unknown_node_keys,
            "timeline_event_hash_check": timeline_check,
            "decomposition_status": status,
            "quarantine_reasons": sorted(set(quarantine_reasons)),
        }
        _validate_against(_DECOMPOSE_REPORT_SCHEMA_ID, decompose_report)

        return DecomposeResult(
            status=status,
            canonical_envelope=canonical_envelope,
            tenant_view_envelope=tenant_view_envelope,
            access_observation=access_observation,
            decompose_report=decompose_report,
            quarantine_reasons=tuple(sorted(set(quarantine_reasons))),
        )

    # -- helpers --------------------------------------------------------

    def _quarantine(
        self,
        *,
        legacy_source_sha256: str,
        field_sources: dict[str, str],
        hit_rules: list[str],
        unknown_frontmatter_keys: list[str],
        unknown_row_keys: list[str],
        unknown_reply_keys: list[str],
        unknown_node_keys: list[str],
        timeline_check: dict[str, Any],
        body_bytes_length: int,
        body_truncation_would_occur: bool,
        quarantine_reasons: list[str],
    ) -> DecomposeResult:
        payload = {
            "schema": "cwk.rt016.decompose_report.v1",
            "decomposer_version": self._dec_ver,
            "normalizer_version": self._norm_ver,
            "legacy_source_sha256": legacy_source_sha256,
            "canonical_sha256": None,
            "object_bytes_sha256": None,
            "body_bytes_length": body_bytes_length,
            "body_truncation_would_occur": body_truncation_would_occur,
            "field_sources": dict(sorted(field_sources.items())),
            "hit_rules": sorted(set(hit_rules)),
            "unknown_frontmatter_keys": sorted(set(unknown_frontmatter_keys)),
            "unknown_row_keys": sorted(set(unknown_row_keys)),
            "unknown_reply_keys": sorted(set(unknown_reply_keys)),
            "unknown_node_keys": sorted(set(unknown_node_keys)),
            "timeline_event_hash_check": timeline_check,
            "decomposition_status": "quarantined",
            "quarantine_reasons": sorted(set(quarantine_reasons)) or ["missing_frontmatter"],
        }
        _validate_against(_DECOMPOSE_REPORT_SCHEMA_ID, payload)
        return DecomposeResult(
            status="quarantined",
            canonical_envelope=None,
            tenant_view_envelope=None,
            access_observation=None,
            decompose_report=payload,
            quarantine_reasons=tuple(payload["quarantine_reasons"]),
        )

    def _build_tenant_view(
        self,
        *,
        tenant_id: str,
        report_key: str,
        canonical_sha256: str,
        frontmatter: dict[str, Any],
        row: dict[str, Any],
        reply_items: list[dict[str, Any]],
        node_items: list[dict[str, Any]],
        observed_at: str,
        field_sources: dict[str, str],
        hit_rules: list[str],
    ) -> dict[str, Any]:
        # Overlay fields we can safely derive.  Anything ambiguous is
        # left absent rather than guessed.
        overlay: dict[str, Any] = {
            "schema": "cwk.tenant_view.v1",
            "tenant_id": tenant_id,
            "report_key": report_key,
            "canonical_sha256": canonical_sha256,
            "observed_at": observed_at,
        }

        # lane: constrained to a small enum-ish set.
        lane_value = frontmatter.get("source_lane")
        if isinstance(lane_value, str) and lane_value:
            if _is_safe_short_string(lane_value, 64):
                overlay["lane"] = lane_value
                field_sources["view.lane"] = "frontmatter:source_lane"
                hit_rules.append("view_lane_from_frontmatter")

        # read_status: from row.read / row.readStatus.
        raw_read = row.get("read")
        if isinstance(raw_read, bool):
            overlay["read_status"] = "read" if raw_read else "unread"
            field_sources["view.read_status"] = "row_metadata:read"
        elif isinstance(row.get("readStatus"), str) and row["readStatus"] in ("read", "unread"):
            overlay["read_status"] = row["readStatus"]
            field_sources["view.read_status"] = "row_metadata:readStatus"

        # todo_status: from row.todoStatus.
        if isinstance(row.get("todoStatus"), str) and row["todoStatus"] in (
            "pending",
            "done",
            "cancelled",
        ):
            overlay["todo_status"] = row["todoStatus"]
            field_sources["view.todo_status"] = "row_metadata:todoStatus"

        if isinstance(row.get("hasNewReply"), bool):
            overlay["new_reply_flag"] = row["hasNewReply"]
            field_sources["view.new_reply_flag"] = "row_metadata:hasNewReply"

        # Reply overlay: IDs + content_sha256 only, no bodies.
        replies_out: list[dict[str, Any]] = []
        for reply in reply_items:
            reply_id = _first_text(
                reply, ("replyId", "id", "commentId", "recordId")
            )
            if not reply_id or not _is_safe_short_string(reply_id, 128):
                continue
            content = _first_text(
                reply, ("replyContent", "content", "comment", "message", "opinion")
            )
            content_sha = _sha256_bytes(_canonical_bytes(content)) if content else None
            item: dict[str, Any] = {"reply_id": reply_id}
            if content_sha is not None:
                item["content_sha256"] = content_sha
            replies_out.append(item)
        if replies_out:
            overlay["reply_overlay"] = replies_out
            hit_rules.append("view_reply_overlay_ids_only")

        # Node overlay: IDs + type + content_sha256 only.
        nodes_out: list[dict[str, Any]] = []
        for node in node_items:
            node_id = _first_text(node, ("nodeId", "id"))
            if not node_id or not _is_safe_short_string(node_id, 128):
                continue
            item = {"node_id": node_id}
            node_type = _first_text(node, ("type",))
            if node_type and _is_safe_short_string(node_type, 64):
                item["type"] = node_type
            payload = {"context": {k: node.get(k) for k in ("nodeName", "status", "level") if k in node}}
            content_sha = _sha256_bytes(_canonical_bytes(payload)) if payload["context"] else None
            if content_sha is not None:
                item["content_sha256"] = content_sha
            nodes_out.append(item)
        if nodes_out:
            overlay["node_overlay"] = nodes_out
            hit_rules.append("view_node_overlay_ids_only")

        # Attachment permissions: always empty.  Legacy raw does not
        # provide a trustworthy separation between per-tenant attachment
        # tokens and generic attachment metadata; we refuse to guess.
        # (Migration cannot rehydrate temporary URLs safely.)

        return overlay


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


class _UnparseableTimestamp(Exception):
    pass


class _AmbiguousTimezone(Exception):
    pass


def _normalize_timestamp(value: Any) -> str:
    """Convert a legacy timestamp into a strict RFC 3339 UTC ISO string.

    Accepts:

    - ``YYYY-MM-DDTHH:MM:SS[.ffffff]±HH:MM`` (offset-aware);
    - ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``;
    - ``YYYY-MM-DDTHH:MM:SS±HH:MM`` variants.

    Refuses:

    - Naive timestamps like ``2024-06-15 10:30:00`` (raises
      :class:`_AmbiguousTimezone`);
    - Anything else (raises :class:`_UnparseableTimestamp`).
    """

    if isinstance(value, (int, float)):
        raise _UnparseableTimestamp("numeric timestamp not supported")
    if not isinstance(value, str):
        raise _UnparseableTimestamp("timestamp must be a string")
    text = value.strip()
    if not text or text != value:
        raise _UnparseableTimestamp("timestamp has surrounding whitespace")
    if len(text) > 64:
        raise _UnparseableTimestamp("timestamp too long")
    if " " in text and "T" not in text:
        # Legacy 'YYYY-MM-DD HH:MM:SS' with no timezone — refuse; we
        # cannot invent a timezone.
        raise _AmbiguousTimezone("timestamp lacks explicit timezone")
    # Accept 'Z' by translating to '+00:00'.
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise _UnparseableTimestamp(str(exc)) from exc
    if parsed.tzinfo is None:
        raise _AmbiguousTimezone("timestamp lacks explicit timezone")
    parsed_utc = parsed.astimezone(_UTC).replace(microsecond=0)
    return parsed_utc.isoformat().replace("+00:00", "Z")


def _parse_iso_utc(value: str, *, allow_field_name: str = "value") -> None:
    """Validate that ``value`` is a strict RFC 3339 UTC timestamp.

    Raises :class:`LegacyImportError` (contract) on failure.  Used for
    caller-supplied fields where we control the timestamp source (e.g.
    ``run_started_at``).
    """

    try:
        _normalize_timestamp(value)
    except (_UnparseableTimestamp, _AmbiguousTimezone) as exc:
        raise LegacyImportError(
            f"{allow_field_name} must be a strict RFC 3339 UTC timestamp: {exc}",
            code="contract",
        ) from exc


# ---------------------------------------------------------------------------
# Frontmatter / section parsers
# ---------------------------------------------------------------------------


class _MalformedFrontmatter(Exception):
    pass


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse a legacy Markdown frontmatter block into a dict.

    Rejects malformed frontmatter (unterminated fence) and any control
    characters in keys or values other than tab (which is also refused
    in canonical fields).
    """

    if not text.startswith("---"):
        return {}
    match = re.match(r"---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    if not match:
        raise _MalformedFrontmatter("frontmatter block not terminated")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise _MalformedFrontmatter(f"frontmatter line lacks ':': {line!r}")
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise _MalformedFrontmatter("empty frontmatter key")
        if not re.match(r"^[A-Za-z0-9_\-.]+$", key):
            raise _MalformedFrontmatter(f"unsafe frontmatter key {key!r}")
        # Strip a single wrapping " or ' if balanced.
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ('"', "'"):
            raw_value = raw_value[1:-1]
        if _contains_forbidden_control_chars(raw_value):
            raise _MalformedFrontmatter(f"frontmatter value contains control chars for {key!r}")
        fields[key] = raw_value
    return fields


def _parse_json_section(text: str, heading: str) -> Any:
    """Extract a fenced JSON section under ``## <heading>``.

    Returns the parsed value on success; returns ``{}`` when the section
    is absent; returns ``{}`` when JSON is malformed (unknown structure
    is safer than a synthetic guess — malformed sections still surface
    via ``unknown_*_keys``).
    """

    pattern = rf"^##\s+{re.escape(heading)}\s*\n+```json\s*(.*?)\s*```"
    match = re.search(pattern, text, re.S | re.M)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _extract_section_body(text: str, heading: str) -> Optional[str]:
    """Return the section body under ``## <heading>`` verbatim.

    Body ends at the next ``## `` header or end of document.  Returns
    ``None`` if the heading is absent.
    """

    header_pattern = rf"^##\s+{re.escape(heading)}\s*\n"
    match = re.search(header_pattern, text, re.M)
    if not match:
        return None
    start = match.end()
    # Search for the next '## ' at column 0.
    tail = text[start:]
    next_match = re.search(r"^##\s", tail, re.M)
    if next_match:
        body = tail[: next_match.start()]
    else:
        body = tail
    # Strip leading blank line but preserve internal whitespace.
    body = body.lstrip("\n")
    body = body.rstrip("\n")
    return body


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _flatten_replies(reply_list: Any) -> Iterator[dict[str, Any]]:
    if isinstance(reply_list, list):
        for item in reply_list:
            yield from _flatten_replies(item)
        return
    if isinstance(reply_list, dict):
        yield reply_list
        for child_key in ("replyList", "children", "childList", "subReplyList"):
            if child_key in reply_list:
                yield from _flatten_replies(reply_list[child_key])


def _iter_nodes(node_list: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(node_list, list):
        return
    for node in node_list:
        if isinstance(node, dict):
            yield node


def _hash_event_payloads(items: list[dict[str, Any]], *, node: bool = False) -> set[str]:
    """Compute deterministic content SHA-256 for each reply/node dict.

    Uses NFC + JCS canonical bytes so the same content in different key
    orders hashes identically.  Not aligned to RT-007's `event_record`
    because we cannot rely on legacy timeline ordering; the reconciler
    only checks membership, not ordering.
    """

    hashes: set[str] = set()
    for item in items:
        try:
            hashes.add(_sha256_bytes(_canonical_bytes(item)))
        except Exception:
            continue
    return hashes


def _is_safe_short_string(value: str, max_len: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > max_len:
        return False
    if _contains_forbidden_control_chars(value):
        return False
    return True


def _contains_forbidden_control_chars(value: str) -> bool:
    if not isinstance(value, str):
        return False
    for ch in value:
        code_point = ord(ch)
        # Allow tab (0x09), newline (0x0a), carriage return (0x0d)
        # inside multi-line canonical body only.  For overlay/short
        # fields callers pre-filter with _is_safe_short_string.
        if code_point in (0x00, 0x0b, 0x0c, 0x7f):
            return True
        if 0x01 <= code_point <= 0x08:
            return True
        if 0x0e <= code_point <= 0x1f:
            return True
    return False


# ---------------------------------------------------------------------------
# LegacySource: safe dirfd-anchored access to a synthetic legacy tree
# ---------------------------------------------------------------------------


class LegacySource:
    """Encapsulate a legacy raw tree opened with ``O_DIRECTORY|O_NOFOLLOW``.

    All file reads verify that the leaf is a regular file (never a
    symlink or hardlink to a foreign inode).  A pre-scan hash is
    captured lazily on first read of each file and compared on
    subsequent reads to detect drift.  The class never returns an
    absolute path to the caller; only opaque names.
    """

    __slots__ = ("_root", "_pre_scan", "_frozen")

    def __init__(self, root_path: str) -> None:
        if not isinstance(root_path, str) or not os.path.isabs(root_path):
            raise LegacyImportError(
                "legacy source root must be an absolute path", code="contract"
            )
        st = os.lstat(root_path)
        if _stat_mod.S_ISLNK(st.st_mode):
            raise LegacyImportError(
                "legacy source root is a symlink; refusing", code="path_containment"
            )
        if not _stat_mod.S_ISDIR(st.st_mode):
            raise LegacyImportError(
                "legacy source root is not a directory", code="path_containment"
            )
        self._root = root_path
        self._pre_scan: dict[str, str] = {}
        self._frozen = False

    @contextmanager
    def _root_fd(self) -> Iterator[int]:
        fd = _A.open_dir_nofollow(self._root)
        try:
            yield fd
        finally:
            os.close(fd)

    def snapshot(self) -> dict[str, str]:
        """Compute the initial pre-scan: relative_path -> SHA-256(bytes)."""

        result: dict[str, str] = {}
        for rel, data in self._walk_bytes():
            result[rel] = _sha256_bytes(data)
        self._pre_scan = dict(result)
        self._frozen = True
        return result

    def re_scan(self) -> dict[str, str]:
        """Recompute the current tree hash for drift comparison."""

        result: dict[str, str] = {}
        for rel, data in self._walk_bytes():
            result[rel] = _sha256_bytes(data)
        return result

    def verify_no_drift(self) -> None:
        """Raise :class:`LegacyDriftDetected` if any tracked file drifted.

        Must be called by importer.import_batch/reconciler after a run
        to prove the legacy tree was not mutated.  Also detects new
        files (which would indicate a rogue writer) and deleted files.
        """

        if not self._frozen:
            raise LegacyImportError(
                "verify_no_drift called before snapshot", code="state"
            )
        current = self.re_scan()
        if current != self._pre_scan:
            raise LegacyDriftDetected()

    def _walk_bytes(self) -> Iterator[tuple[str, bytes]]:
        # Recursive dirfd-anchored walk.
        with self._root_fd() as rfd:
            yield from self._walk_at(rfd, prefix="")

    def _walk_at(self, parent_fd: int, prefix: str) -> Iterator[tuple[str, bytes]]:
        # Prevent unbounded prefix lengths.
        if len(prefix) > 4096:
            raise LegacyImportError("legacy path too deep", code="path_containment")
        with os.scandir(parent_fd) as entries:
            names = sorted(e.name for e in entries)
        for name in names:
            if name in (".", ".."):
                continue
            # Only allow the RT-016 safe leaf grammar (aligned with
            # cwk_atomic_file._LEAF_ALLOWED, but permit uppercase for
            # legacy hashed file names).
            if not re.match(r"^[A-Za-z0-9_.\-]{1,255}$", name):
                # Skip silently — we don't want to corrupt the pre-scan
                # with names we can't touch atomically.  The caller can
                # inspect the snapshot() diff if concerned.
                continue
            try:
                st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if _stat_mod.S_ISLNK(st.st_mode):
                # Skip symlinks; RT-016 will not read through them.
                continue
            rel = f"{prefix}{name}"
            if _stat_mod.S_ISDIR(st.st_mode):
                subfd = _openat_dir_nofollow(parent_fd, name)
                try:
                    yield from self._walk_at(subfd, prefix=f"{rel}/")
                finally:
                    os.close(subfd)
            elif _stat_mod.S_ISREG(st.st_mode):
                # Reject hardlinks (nlink>1) — could point to foreign
                # inodes and thus a race target.
                if st.st_nlink > 1:
                    continue
                data = self._read_file_at(parent_fd, name)
                yield rel, data

    def _read_file_at(self, parent_fd: int, name: str) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LegacyImportError(
                    "legacy file is a symlink; refusing", code="path_containment"
                ) from exc
            raise LegacyImportError(
                f"cannot open legacy file (errno={exc.errno})", code="io"
            ) from exc
        try:
            data = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 32 * 1024 * 1024:
                    raise LegacyImportError(
                        "legacy file exceeds 32 MiB safety cap", code="contract"
                    )
        finally:
            os.close(fd)
        return data

    def iter_files_with_hashes(self) -> Iterator[tuple[str, bytes, str]]:
        """Iterate ``(relative_path, bytes, sha256)`` over the tree.

        Also updates the pre-scan record (idempotently) so that a later
        :meth:`verify_no_drift` catches modifications between the two
        walks.
        """

        for rel, data in self._walk_bytes():
            sha = _sha256_bytes(data)
            self._pre_scan.setdefault(rel, sha)
            yield rel, data, sha


# ---------------------------------------------------------------------------
# ShadowImporter
# ---------------------------------------------------------------------------


class ShadowImporter:
    """Publish canonical, observe grant, write crosswalk — never touch legacy.

    Each :meth:`import_one` call is:

    1. **Idempotent** — a crosswalk with the deterministic
       ``crosswalk_key = H(tenant, view_key, legacy_source_sha256)``
       is only written if not already present with matching content.
       Repeat runs are no-ops.
    2. **Crash-safe** — publish/observe/crosswalk-write happen in this
       order under a per-crosswalk ``flock``.  If a crash interrupts a
       run, the next :meth:`recover` sweep removes ``.cwk-tmp-``
       orphans; already-committed steps are not undone.
    3. **Fail-closed** — publish failure, observe failure, or ledger
       error aborts the import for this legacy raw and either raises or
       records a review entry depending on the failure kind.  No partial
       crosswalk is ever written.
    4. **Tenant-scoped** — every crosswalk / review / lock file lives
       under ``registry/rt016-crosswalk/<tenant_id>/``.

    Tenant view upsert is optional; when the caller passes a valid
    ``authority_receipt``, the importer additionally promotes the grant
    to ``active`` and calls :meth:`TenantViewStore.upsert_overlay`.  In
    production migration this path is unavailable (no authority chain
    yet exists), so the crosswalk records
    ``tenant_view_deferred_reason='no_authority_receipt_available'``.
    """

    __slots__ = ("_layout", "_tenants", "_evidence", "_ledger", "_views", "_decomposer")

    def __init__(
        self,
        layout: _I.InstanceLayout,
        tenant_registry: _R.TenantRegistry,
        evidence_store: _SE.SharedEvidenceStore,
        ledger: _AL.AccessLedger,
        view_store: _TV.TenantViewStore,
        decomposer: Optional[LegacyRawDecomposer] = None,
    ) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise LegacyImportError("layout must be InstanceLayout", code="contract")
        if not isinstance(tenant_registry, _R.TenantRegistry):
            raise LegacyImportError(
                "tenant_registry must be TenantRegistry", code="contract"
            )
        if not isinstance(evidence_store, _SE.SharedEvidenceStore):
            raise LegacyImportError(
                "evidence_store must be SharedEvidenceStore", code="contract"
            )
        if not isinstance(ledger, _AL.AccessLedger):
            raise LegacyImportError("ledger must be AccessLedger", code="contract")
        if not isinstance(view_store, _TV.TenantViewStore):
            raise LegacyImportError(
                "view_store must be TenantViewStore", code="contract"
            )
        self._layout = layout
        self._tenants = tenant_registry
        self._evidence = evidence_store
        self._ledger = ledger
        self._views = view_store
        self._decomposer = decomposer or LegacyRawDecomposer()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Idempotently create ``registry/rt016-crosswalk/`` root."""

        with self._layout.child_fd("registry") as rfd:
            _A.mkdir_at(rfd, REGISTRY_SUBDIR, mode=_A.DIRECTORY_MODE, exist_ok=True)
            _A.fsync_dir(rfd)

    # ------------------------------------------------------------------
    # Dir helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _registry_rt016_fd(self) -> Iterator[int]:
        with self._layout.child_fd("registry") as rfd:
            if not _A.child_exists(rfd, REGISTRY_SUBDIR):
                raise LegacyImportError(
                    "registry/rt016-crosswalk not initialised",
                    code="not_initialized",
                )
            fd = _openat_dir_nofollow(rfd, REGISTRY_SUBDIR)
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _tenant_fd(self, tenant_id: str, *, create: bool = False) -> Iterator[int]:
        validated = _I.validate_tenant_id(tenant_id)
        with self._registry_rt016_fd() as afd:
            if not _A.child_exists(afd, validated):
                if not create:
                    raise LegacyImportError(
                        "tenant crosswalk subdir missing", code="not_found"
                    )
                _A.mkdir_at(afd, validated, mode=_A.DIRECTORY_MODE, exist_ok=True)
                _A.fsync_dir(afd)
            fd = _openat_dir_nofollow(afd, validated)
            try:
                if create:
                    for sub in _TENANT_SUBDIRS:
                        _A.mkdir_at(fd, sub, mode=_A.DIRECTORY_MODE, exist_ok=True)
                    _A.fsync_dir(fd)
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _sub_fd(self, tenant_fd: int, name: str) -> Iterator[int]:
        if name not in _TENANT_SUBDIRS:
            raise LegacyImportError(
                f"unknown rt016 subdir {name!r}", code="contract"
            )
        if not _A.child_exists(tenant_fd, name):
            raise LegacyImportError(f"{name} missing under tenant", code="not_initialized")
        fd = _openat_dir_nofollow(tenant_fd, name)
        try:
            yield fd
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Import one legacy raw
    # ------------------------------------------------------------------

    def import_one(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        raw_bytes: bytes,
        run_id: str,
        run_started_at: str,
        actor: str,
        reason: str,
        legacy_path_hint: str,
        source_kind: str = "current_raw",
        timeline_snapshot_bytes: Optional[list[bytes]] = None,
        timeline_event_bytes: Optional[list[bytes]] = None,
        authority_receipt: Optional[dict[str, Any]] = None,
        allow_view_upsert: bool = True,
    ) -> ImportReceipt:
        """Import one legacy raw and return a machine-readable receipt.

        The receipt's ``outcome`` is one of:

        - ``"complete"`` — canonical published, observation recorded,
          crosswalk written.  Optionally the tenant view was upserted
          (indicated by ``tenant_view_written``).
        - ``"review"`` — canonical/view could not be safely produced but
          the caller may still surface a review entry.
        - ``"undecomposable"`` — same as review; used when the
          decomposer emits a hard-fail reason (malformed frontmatter,
          missing body section, etc.).

        Idempotent within a run and across runs: repeated calls with
        identical inputs return the same receipt without side-effects.
        """

        _validate_actor_reason(actor, reason)
        _I.validate_tenant_id(tenant_id)
        if not isinstance(source_namespace, str) or not _C.SOURCE_NAMESPACE_REGEX.match(
            source_namespace
        ):
            raise LegacyImportError("invalid source_namespace", code="contract")
        if not isinstance(run_id, str) or not _RUN_ID_REGEX.match(run_id):
            raise LegacyImportError("invalid run_id", code="contract")
        _parse_iso_utc(run_started_at, allow_field_name="run_started_at")
        if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
            raise LegacyImportError("raw_bytes must be bytes-like", code="contract")
        raw_bytes = bytes(raw_bytes)
        legacy_source_sha256 = _sha256_bytes(raw_bytes)
        legacy_path_hash = compute_legacy_path_hash(legacy_path_hint)
        if source_kind not in ("current_raw", "timeline_snapshot"):
            raise LegacyImportError("invalid source_kind", code="contract")

        # Ensure tenant exists in RT-012 registry (otherwise observe
        # will fail with unknown_tenant later; fail early for a nicer
        # error surface).
        try:
            self._tenants.get(tenant_id)
        except _R.TenantNotFound as exc:
            raise LegacyImportError(
                "tenant unknown to registry",
                code="not_found",
            ) from exc

        with self._tenant_fd(tenant_id, create=True) as tfd:
            # Idempotent path: check whether a v2 crosswalk already exists
            # for (tenant_id, source_namespace, source_kind,
            # legacy_path_hash, legacy_source_sha256).  v2 identity binds
            # all five so identical raw bytes under different namespaces /
            # paths / kinds never silently reuse each other's crosswalk.
            existing_ck, existing_payload = self._find_existing_crosswalk_for_legacy(
                tfd,
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                source_kind=source_kind,
                legacy_path_hash=legacy_path_hash,
                legacy_source_sha256=legacy_source_sha256,
            )
            if existing_ck is not None and existing_payload is not None:
                return _receipt_from_crosswalk(existing_payload, outcome="complete")

            existing_review = self._find_existing_review(
                tfd,
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                source_kind=source_kind,
                legacy_path_hash=legacy_path_hash,
                legacy_source_sha256=legacy_source_sha256,
                run_id=run_id,
            )
            if existing_review is not None:
                return _receipt_from_review(existing_review, outcome=existing_review["migration_status"])

            # Decompose fresh.
            result = self._decomposer.decompose(
                raw_bytes=raw_bytes,
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                run_started_at=run_started_at,
                source_kind=source_kind,
                timeline_snapshot_bytes=timeline_snapshot_bytes,
                timeline_event_bytes=timeline_event_bytes,
            )

            if result.status == "quarantined":
                receipt = self._write_review(
                    tfd=tfd,
                    tenant_id=tenant_id,
                    source_namespace=source_namespace,
                    legacy_source_sha256=legacy_source_sha256,
                    legacy_path_hash=legacy_path_hash,
                    source_kind=source_kind,
                    decompose_report=result.decompose_report,
                    quarantine_reasons=list(result.quarantine_reasons),
                    run_id=run_id,
                    run_started_at=run_started_at,
                )
                self._append_manifest_entry(
                    tfd=tfd,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    source_namespace=source_namespace,
                    source_kind=source_kind,
                    legacy_source_sha256=legacy_source_sha256,
                    legacy_path_hash=legacy_path_hash,
                    outcome=receipt.outcome,
                    crosswalk_key=None,
                    review_id=receipt.review_id,
                    canonical_sha256=None,
                    view_key=None,
                    quarantine_reasons=list(result.quarantine_reasons),
                )
                return receipt

            assert result.canonical_envelope is not None
            assert result.tenant_view_envelope is not None
            assert result.access_observation is not None

            # Enforce duplicate report_id detection within the tenant:
            # two different legacy sources decoded to the same report_key
            # AND same canonical_sha256 are fine (idempotent republish
            # under RT-014).  But two different legacy sources → same
            # report_key → DIFFERENT canonical_sha256 signal is expected
            # (a body version bump).  What we must NOT do is silently
            # collapse them: RT-014 keeps both versions.  So we accept.
            # However, if a review entry already exists for a different
            # legacy_source_sha but SAME report_key with different
            # canonical, that's fine; if a crosswalk with a completely
            # different view_key (different tenant) exists that's
            # also fine — different opaque namespace.  There is no
            # duplicate-report_id error condition inside a single
            # tenant beyond what RT-014's catalog handles; we still
            # record it here as a warning.

            # 1. Publish canonical (RT-014).
            envelope_out = _copy_json(result.canonical_envelope)
            try:
                publish_receipt = self._evidence.publish(envelope_out)
            except _SE.SharedEvidenceError as exc:
                # Cannot proceed; convert into a review entry so the
                # legacy raw is auditable.
                self._append_manifest_entry(
                    tfd=tfd,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    source_namespace=source_namespace,
                    source_kind=source_kind,
                    legacy_source_sha256=legacy_source_sha256,
                    legacy_path_hash=legacy_path_hash,
                    outcome="review",
                    crosswalk_key=None,
                    review_id=None,
                    canonical_sha256=None,
                    view_key=None,
                    quarantine_reasons=[f"shared_evidence_error:{exc.code}"[:64]],
                )
                raise LegacyImportError(
                    f"shared evidence publish failed: {exc.code}",
                    code="corrupt",
                ) from exc

            # 2. Observe (RT-015) — grant status becomes 'granted'.
            #
            # On macOS, RT-015's exclusive_lock can occasionally raise
            # ``FileNotFoundError`` under high contention on the same
            # grant_key (documented in cwk_shared_evidence
            # ``_flock_with_retry``).  We retry a bounded number of
            # times without touching the frozen RT-015 module.  Any
            # other RT-015 error is surfaced immediately.
            observation_out = _copy_json(result.access_observation)
            grant_record = None
            import time as _time
            last_exc: BaseException | None = None
            for attempt in range(16):
                try:
                    grant_record = self._ledger.observe(
                        observation=observation_out,
                        actor=actor,
                        reason=reason,
                    )
                    break
                except FileNotFoundError as exc:
                    last_exc = exc
                    # Exponential jitter capped at ~50 ms — plenty for
                    # the transient macOS ENOENT window.
                    _time.sleep(min(0.05, 0.002 * (attempt + 1)))
                    continue
                except _AL.AccessLedgerError as exc:
                    raise LegacyImportError(
                        f"access ledger observe failed: {exc.code}",
                        code="ledger_denied",
                    ) from exc
            if grant_record is None:
                raise LegacyImportError(
                    "access ledger observe repeatedly failed on race",
                    code="ledger_denied",
                ) from last_exc

            observe_grant_key = grant_record.grant_key
            view_key = observe_grant_key  # same derivation

            # 3. Compose durable crosswalk envelope.
            tenant_view_written = False
            tenant_view_deferred_reason: Optional[str] = "no_authority_receipt_available"
            tenant_view_record_revision: Optional[int] = None

            # 3a. Optional tenant view upsert (test-only fake authority
            # path or future real authority integration).
            if allow_view_upsert and authority_receipt is not None:
                try:
                    self._ledger.promote_to_active(
                        tenant_id=tenant_id,
                        source_namespace=source_namespace,
                        report_id=result.canonical_envelope["report_id"],
                        authority_receipt=authority_receipt,
                        actor=actor,
                        reason=reason,
                        lease_ttl_seconds=_AL.DEFAULT_LEASE_TTL_SECONDS,
                    )
                except _AL.AccessLedgerError as exc:
                    if exc.code == "authority_rejected":
                        tenant_view_deferred_reason = "authority_receipt_rejected"
                    elif exc.code == "state":
                        tenant_view_deferred_reason = "access_ledger_denied"
                    elif exc.code == "revocation_in_progress":
                        tenant_view_deferred_reason = "in_flight_revocation"
                    else:
                        tenant_view_deferred_reason = "access_ledger_denied"
                else:
                    # Grant is now active; upsert the overlay.
                    snapshot = _build_migration_snapshot(
                        tenants=self._tenants, tenant_id=tenant_id
                    )
                    try:
                        view_record = self._views.upsert_overlay(
                            snapshot=snapshot,
                            view_envelope=_copy_json(result.tenant_view_envelope),
                        )
                        tenant_view_written = True
                        tenant_view_deferred_reason = None
                        tenant_view_record_revision = view_record.record_revision
                    except _TV.TenantViewError as exc:
                        if exc.code == "canonical_missing":
                            tenant_view_deferred_reason = "canonical_missing"
                        elif exc.code == "denied":
                            tenant_view_deferred_reason = "access_ledger_denied"
                        else:
                            tenant_view_deferred_reason = "access_ledger_denied"

            crosswalk_key = compute_crosswalk_key_v2(
                tenant_id,
                source_namespace,
                source_kind,
                legacy_path_hash,
                legacy_source_sha256,
            )
            now = _utcnow_iso()
            crosswalk_payload: dict[str, Any] = {
                "schema": "cwk.rt016.migration_crosswalk.v2",
                "identity_version": IDENTITY_VERSION,
                "crosswalk_key": crosswalk_key,
                "tenant_id": tenant_id,
                "view_key": view_key,
                "report_key": _C.compose_report_key(
                    source_namespace, result.canonical_envelope["report_id"]
                ),
                "source_namespace": source_namespace,
                "report_id": result.canonical_envelope["report_id"],
                "source_kind": source_kind,
                "legacy_path_hash": legacy_path_hash,
                "legacy_source_sha256": legacy_source_sha256,
                "canonical_sha256": result.canonical_envelope["canonical_sha256"],
                "object_bytes_sha256": _sha256_bytes(
                    _canonical_bytes(result.canonical_envelope)
                ),
                "object_id": publish_receipt.object_id,
                "catalog_key": publish_receipt.catalog_key,
                "catalog_revision": publish_receipt.catalog_revision,
                "publish_receipt": publish_receipt.to_dict(),
                "observe_grant_key": observe_grant_key,
                "observation_source": "legacy_raw_decomposition",
                "initial_status": "granted",
                "tenant_view_envelope": _copy_json(result.tenant_view_envelope),
                "tenant_view_written": tenant_view_written,
                "tenant_view_deferred_reason": tenant_view_deferred_reason,
                "tenant_view_record_revision": tenant_view_record_revision,
                "timeline_snapshot_hashes": sorted(
                    {_sha256_bytes(b) for b in (timeline_snapshot_bytes or [])}
                ),
                "timeline_event_hashes": sorted(
                    {_sha256_bytes(b) for b in (timeline_event_bytes or [])}
                ),
                "decomposer_version": DECOMPOSER_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "decompose_report": _copy_json(result.decompose_report),
                "migration_status": "complete",
                "run_id": run_id,
                "run_started_at": run_started_at,
                "created_at": now,
                "record_revision": 1,
            }
            _validate_against(_MIGRATION_CROSSWALK_V2_SCHEMA_ID, crosswalk_payload)
            _C.validate_tenant_view(crosswalk_payload["tenant_view_envelope"])
            _validate_against(_DECOMPOSE_REPORT_SCHEMA_ID, crosswalk_payload["decompose_report"])

            # 4. Persist crosswalk under an exclusive per-crosswalk lock.
            # CAS conflict branch reads the existing file through the
            # bound reader with our caller-known identity — an attacker
            # who moved a foreign crosswalk into our filename slot fails
            # closed here rather than "idempotently returning" someone
            # else's payload.
            expect = _BoundReaderExpect(
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                source_kind=source_kind,
                legacy_path_hash=legacy_path_hash,
                legacy_source_sha256=legacy_source_sha256,
                filename_crosswalk_key=crosswalk_key,
            )
            with self._sub_fd(tfd, "locks") as lfd:
                with _A.exclusive_lock(lfd, f"cw.{crosswalk_key}.lock"):
                    with self._sub_fd(tfd, "crosswalks") as cfd:
                        leaf = f"{crosswalk_key}.json"
                        if _A.child_exists(cfd, leaf):
                            existing_raw = _A.read_file(cfd, leaf)
                            existing_payload = _load_crosswalk_payload_bound(
                                existing_raw, expect=expect
                            )
                            # Idempotent: bound-reader OK ⇒ identity is
                            # already ours.  Compare the four output
                            # fields that must match by construction.
                            if (
                                existing_payload["canonical_sha256"]
                                != crosswalk_payload["canonical_sha256"]
                                or existing_payload["observe_grant_key"]
                                != crosswalk_payload["observe_grant_key"]
                                or existing_payload["object_id"]
                                != crosswalk_payload["object_id"]
                                or existing_payload["view_key"]
                                != crosswalk_payload["view_key"]
                            ):
                                raise LegacyImportError(
                                    "existing crosswalk drift", code="corrupt",
                                    crosswalk_key=crosswalk_key,
                                )
                            crosswalk_payload = existing_payload
                        else:
                            _A.cas_write(
                                cfd,
                                leaf,
                                _canonical_bytes(crosswalk_payload),
                                expected_previous_sha256=None,
                            )

            # 5. Manifest append (outside the crosswalk lock so multiple
            # writers can still serialise their manifest lines correctly
            # under their own append lock).
            self._append_manifest_entry(
                tfd=tfd,
                run_id=run_id,
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                source_kind=source_kind,
                legacy_source_sha256=legacy_source_sha256,
                legacy_path_hash=legacy_path_hash,
                outcome="complete",
                crosswalk_key=crosswalk_key,
                review_id=None,
                canonical_sha256=crosswalk_payload["canonical_sha256"],
                view_key=view_key,
                quarantine_reasons=None,
            )

        return _receipt_from_crosswalk(crosswalk_payload, outcome="complete")

    # ------------------------------------------------------------------
    # Batch import (with pre/post zero-drift verification)
    # ------------------------------------------------------------------

    def import_batch(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        source: LegacySource,
        run_id: str,
        run_started_at: str,
        actor: str,
        reason: str,
        authority_receipt_factory: Optional[Any] = None,
    ) -> list[ImportReceipt]:
        """Iterate the legacy source and import each ``*.md`` file.

        Automatically calls :meth:`LegacySource.snapshot` before the run
        and :meth:`LegacySource.verify_no_drift` after; any drift raises
        :class:`LegacyDriftDetected` and no additional writes occur.

        ``authority_receipt_factory`` is an optional callable
        ``factory(*, tenant_id, source_namespace, report_id, grant_key)``
        that returns a signed authority receipt for the (test) upsert
        path; production callers pass ``None``.
        """

        if not isinstance(source, LegacySource):
            raise LegacyImportError("source must be LegacySource", code="contract")
        source.snapshot()
        receipts: list[ImportReceipt] = []
        for rel, data, sha in source.iter_files_with_hashes():
            if not rel.endswith(".md"):
                continue
            # Skip legacy `_system` timeline artefacts here — those are
            # processed via timeline_event_bytes; the current raw's
            # `.md` is the primary decomposition target.
            if "/_system/" in rel or rel.startswith("_system/"):
                continue
            receipt: ImportReceipt
            authority_receipt = None
            if authority_receipt_factory is not None:
                # Peek report_id via decomposer (no writes).  This costs
                # one extra decompose call for the happy path but keeps
                # the authority factory generic.
                peek = self._decomposer.decompose(
                    raw_bytes=data,
                    tenant_id=tenant_id,
                    source_namespace=source_namespace,
                    run_started_at=run_started_at,
                )
                if peek.status == "ok":
                    canonical = peek.canonical_envelope
                    view_key = _AL.compute_grant_key(
                        tenant_id,
                        _C.compose_report_key(
                            source_namespace, canonical["report_id"]
                        ),
                    )
                    authority_receipt = authority_receipt_factory(
                        tenant_id=tenant_id,
                        source_namespace=source_namespace,
                        report_id=canonical["report_id"],
                        grant_key=view_key,
                    )
            receipt = self.import_one(
                tenant_id=tenant_id,
                source_namespace=source_namespace,
                raw_bytes=data,
                run_id=run_id,
                run_started_at=run_started_at,
                actor=actor,
                reason=reason,
                legacy_path_hint=rel,
                source_kind="current_raw",
                authority_receipt=authority_receipt,
            )
            receipts.append(receipt)
        source.verify_no_drift()
        return receipts

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def read_crosswalk(
        self, *, tenant_id: str, crosswalk_key: str
    ) -> dict[str, Any]:
        """Load and validate a single crosswalk record by key.

        Public read API for the reconciler and audit consumers.  Never
        returns partial or corrupt payloads.  Post RT-016 v1
        remediation this method runs the full integrity chain:

        1. Bytes decode as strict UTF-8.
        2. Bytes parse as strict JSON (``json.JSONDecodeError`` /
           ``UnicodeDecodeError`` / duplicate-key ``ContractError`` are
           wrapped as ``LegacyImportError(code="corrupt")``).
        3. Envelope schema + RT-011 tenant_view schema pass.
        4. Byte-level JCS round-trip: ``_canonical_bytes(payload) == raw``.
        5. Cross-field consistency (see :func:`_validate_crosswalk_integrity`):
           ``canonical_sha256`` / ``object_id`` / ``catalog_key`` /
           ``catalog_revision`` / ``report_key`` / ``view_key`` /
           ``observe_grant_key`` / ``crosswalk_key`` /
           ``object_bytes_sha256`` / ``legacy_source_sha256`` all agree
           with the embedded ``publish_receipt`` /
           ``tenant_view_envelope`` / ``decompose_report`` and with the
           deterministic derivations from ``(tenant_id, source_namespace,
           report_id, legacy_source_sha256)``.

        Any failure raises :class:`LegacyImportError(code='corrupt')`.
        """

        if not isinstance(crosswalk_key, str) or not _CROSSWALK_KEY_REGEX.match(crosswalk_key):
            raise LegacyImportError("invalid crosswalk_key", code="contract")
        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "crosswalks") as cfd:
                raw = _A.read_file(cfd, f"{crosswalk_key}.json")
        payload = _load_crosswalk_payload(raw, crosswalk_key=crosswalk_key)
        # Additional cross-check: the file name key must match the
        # payload's declared crosswalk_key.  This catches the case where
        # an attacker renamed a file into a different key slot.
        if payload.get("crosswalk_key") != crosswalk_key:
            raise LegacyImportError(
                "crosswalk_key inside payload disagrees with file name",
                code="corrupt",
                crosswalk_key=crosswalk_key,
            )
        # Additional cross-check: the file must live under the tenant
        # subtree that matches the payload's declared tenant_id.
        if payload.get("tenant_id") != tenant_id:
            raise LegacyImportError(
                "crosswalk tenant_id disagrees with parent directory",
                code="corrupt",
                crosswalk_key=crosswalk_key,
            )
        return payload

    def iter_crosswalks(self, *, tenant_id: str) -> Iterator[dict[str, Any]]:
        """Iterate all durable crosswalks for a tenant.

        Used by :class:`~cwk_migration_reconciler.MigrationReconciler` to
        enumerate the new side.  Never touches RT-014 catalogs by
        listing.
        """

        try:
            with self._tenant_fd(tenant_id) as tfd:
                with self._sub_fd(tfd, "crosswalks") as cfd:
                    with os.scandir(cfd) as entries:
                        leaves = sorted(
                            e.name for e in entries
                            if e.name.endswith(".json")
                            and not e.name.startswith(_A.TEMP_PREFIX)
                            and _CROSSWALK_KEY_REGEX.match(e.name[:-5])
                        )
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return
            raise
        for leaf in leaves:
            crosswalk_key = leaf[:-5]
            payload = self.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=crosswalk_key
            )
            yield payload

    def iter_reviews(self, *, tenant_id: str) -> Iterator[dict[str, Any]]:
        """Iterate all durable review entries for a tenant."""

        try:
            with self._tenant_fd(tenant_id) as tfd:
                with self._sub_fd(tfd, "review") as rfd:
                    with os.scandir(rfd) as entries:
                        leaves = sorted(
                            e.name for e in entries
                            if e.name.endswith(".json")
                            and not e.name.startswith(_A.TEMP_PREFIX)
                            and _REVIEW_ID_REGEX.match(e.name[:-5])
                        )
                    for leaf in leaves:
                        try:
                            raw = _A.read_file(rfd, leaf)
                        except FileNotFoundError:
                            continue
                        payload = _load_review_payload(raw, review_id=leaf[:-5])
                        yield payload
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return
            raise

    def iter_manifest(
        self, *, tenant_id: str, run_id: str
    ) -> Iterator[dict[str, Any]]:
        """Iterate parsed manifest entries for one run."""

        if not _RUN_ID_REGEX.match(run_id):
            raise LegacyImportError("invalid run_id", code="contract")
        try:
            with self._tenant_fd(tenant_id) as tfd:
                with self._sub_fd(tfd, "manifests") as mfd:
                    leaf = f"{run_id}.jsonl"
                    if not _A.child_exists(mfd, leaf):
                        return
                    raw = _A.read_file(mfd, leaf)
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return
            raise
        if not raw:
            return
        if not raw.endswith(b"\n"):
            raise LegacyImportError(
                "manifest missing trailing newline", code="corrupt"
            )
        for line in raw.split(b"\n")[:-1]:
            if not line:
                continue
            try:
                payload = _C.strict_json_loads(line.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise LegacyImportError(
                    f"manifest line is not UTF-8: {exc}", code="corrupt"
                ) from exc
            except json.JSONDecodeError as exc:
                raise LegacyImportError(
                    f"manifest line is not strict JSON: {exc}", code="corrupt"
                ) from exc
            except _C.ContractError as exc:
                raise LegacyImportError(
                    f"manifest line violates duplicate-key rule: {exc}",
                    code="corrupt",
                ) from exc
            if not isinstance(payload, dict):
                raise LegacyImportError(
                    "manifest line is not a JSON object", code="corrupt"
                )
            schema_id = payload.get("schema")
            if schema_id == "cwk.rt016.migration_manifest_entry.v2":
                _validate_against(_MANIFEST_ENTRY_V2_SCHEMA_ID, payload)
            elif schema_id == "cwk.rt016.migration_manifest_entry.v1":
                _validate_against(_MANIFEST_ENTRY_SCHEMA_ID, payload)
            else:
                raise LegacyImportError(
                    f"unknown manifest line schema {schema_id!r}", code="corrupt"
                )
            yield payload

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self, *, actor: str, reason: str) -> RecoveryReport:
        """Idempotently sweep .cwk-tmp-* orphans and validate durable state.

        Does NOT re-run publish/observe (those are already durable if
        the corresponding crosswalk was written).  Reports opaque
        inconsistencies for callers to audit; never mutates crosswalk /
        review bodies.
        """

        _validate_actor_reason(actor, reason)
        report_scans = 0
        staging = 0
        cw_orphans = 0
        rv_orphans = 0
        mf_orphans = 0
        inconsistencies: list[dict[str, Any]] = []

        # 1. Staging tmp orphans.
        try:
            with self._layout.child_fd("staging") as sfd:
                if _A.child_exists(sfd, STAGING_SUBDIR):
                    stg_root = _openat_dir_nofollow(sfd, STAGING_SUBDIR)
                    try:
                        staging += len(_A.recover_orphans(stg_root))
                    finally:
                        os.close(stg_root)
        except LegacyImportError:
            pass

        # 2. Per-tenant sweep.
        try:
            with self._registry_rt016_fd() as afd:
                with os.scandir(afd) as entries:
                    tenants = sorted(
                        e.name for e in entries
                        if _C.TENANT_ID_REGEX.match(e.name)
                    )
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return RecoveryReport()
            raise

        for tenant_id in tenants:
            report_scans += 1
            try:
                with self._tenant_fd(tenant_id) as tfd:
                    for sub in _TENANT_SUBDIRS:
                        try:
                            with self._sub_fd(tfd, sub) as sfd:
                                removed = _A.recover_orphans(sfd)
                                if sub == "crosswalks":
                                    cw_orphans += len(removed)
                                elif sub == "review":
                                    rv_orphans += len(removed)
                                elif sub == "manifests":
                                    mf_orphans += len(removed)
                        except LegacyImportError:
                            continue
            except Exception as exc:  # pragma: no cover - opaque
                inconsistencies.append(
                    {
                        "code": "recover_error",
                        "tenant_prefix": tenant_id[:8],
                        "detail": str(exc)[:64],
                    }
                )

        return RecoveryReport(
            tenants_scanned=report_scans,
            staging_orphans_removed=staging,
            crosswalk_orphans_removed=cw_orphans,
            review_orphans_removed=rv_orphans,
            manifest_orphans_removed=mf_orphans,
            inconsistencies=tuple(inconsistencies),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_existing_crosswalk_for_legacy(
        self,
        tenant_fd: int,
        *,
        tenant_id: str,
        source_namespace: str,
        source_kind: str,
        legacy_path_hash: str,
        legacy_source_sha256: str,
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """Return (crosswalk_key, payload) if a v2 crosswalk exists.

        v2 identity binds ``(tenant_id, source_namespace, source_kind,
        legacy_path_hash, legacy_source_sha256)``.  The finder computes
        the deterministic v2 ``crosswalk_key`` from the caller's five
        identity inputs and looks up **exactly** that file — no scan,
        no filter-by-content, no chance for a payload with a different
        identity to be silently returned.

        The candidate is then loaded through :func:`_load_crosswalk_payload_bound`
        which re-verifies that the on-disk payload's own identity fields
        agree with the caller's expectations and that the filename key
        matches the recomputed v2 key.  Any mismatch fails closed as
        ``corrupt``.

        v1 records under the same filename are treated as corrupt for
        the v2 finder (v1 keys can never collide with v2 keys because
        the domain separator differs, so the v2 filename cannot legally
        contain a v1 payload).  v1 records at other filenames are simply
        ignored — they remain readable via
        :meth:`read_crosswalk` for audit only.
        """

        expected_key = compute_crosswalk_key_v2(
            tenant_id,
            source_namespace,
            source_kind,
            legacy_path_hash,
            legacy_source_sha256,
        )
        expect = _BoundReaderExpect(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            source_kind=source_kind,
            legacy_path_hash=legacy_path_hash,
            legacy_source_sha256=legacy_source_sha256,
            filename_crosswalk_key=expected_key,
        )
        try:
            with self._sub_fd(tenant_fd, "crosswalks") as cfd:
                leaf = f"{expected_key}.json"
                if not _A.child_exists(cfd, leaf):
                    return None, None
                try:
                    raw = _A.read_file(cfd, leaf)
                except FileNotFoundError:
                    return None, None
                payload = _load_crosswalk_payload_bound(raw, expect=expect)
                return expected_key, payload
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return None, None
            raise

    def _find_existing_review(
        self,
        tenant_fd: int,
        *,
        tenant_id: str,
        source_namespace: str,
        source_kind: str,
        legacy_path_hash: str,
        legacy_source_sha256: str,
        run_id: str,
    ) -> Optional[dict[str, Any]]:
        """Look up an existing v2 review entry deterministically.

        v2 review_id derivation binds ``(tenant, source_namespace,
        source_kind, legacy_path_hash, legacy_source_sha256, run_id)``
        so the same raw bytes imported under two different namespaces /
        paths / kinds produce two distinct review IDs.  The finder
        computes the expected v2 review_id from the caller's inputs
        and reads exactly that filename — no scan-and-filter.  It also
        cross-checks the on-disk payload's identity fields against the
        caller's inputs (defense-in-depth) and refuses v1 review
        records (which cannot carry ``source_kind`` +
        ``legacy_path_hash`` bindings) at v2 filenames — an impossible
        collision, so any v1 record found at a v2 slot is corrupt.
        """

        expected_review_id = compute_review_id_v2(
            tenant_id,
            source_namespace,
            source_kind,
            legacy_path_hash,
            legacy_source_sha256,
            run_id,
        )
        try:
            with self._sub_fd(tenant_fd, "review") as rfd:
                leaf = f"{expected_review_id}.json"
                if not _A.child_exists(rfd, leaf):
                    return None
                try:
                    raw = _A.read_file(rfd, leaf)
                except FileNotFoundError:
                    return None
                payload = _load_review_payload(raw, review_id=expected_review_id)
                if payload.get("schema") != "cwk.rt016.review_entry.v2":
                    raise LegacyImportError(
                        "review entry at v2 slot is not v2",
                        code="corrupt",
                        review_id=expected_review_id,
                    )
                if (
                    payload.get("tenant_id") != tenant_id
                    or payload.get("source_namespace") != source_namespace
                    or payload.get("source_kind") != source_kind
                    or payload.get("legacy_path_hash") != legacy_path_hash
                    or payload.get("legacy_source_sha256") != legacy_source_sha256
                    or payload.get("run_id") != run_id
                    or payload.get("review_id") != expected_review_id
                ):
                    raise LegacyImportError(
                        "review entry binding fields disagree with caller inputs",
                        code="corrupt",
                        review_id=expected_review_id,
                    )
                return payload
        except LegacyImportError as exc:
            if exc.code in ("not_found", "not_initialized"):
                return None
            raise
        return None

    def _write_review(
        self,
        *,
        tfd: int,
        tenant_id: str,
        source_namespace: str,
        legacy_source_sha256: str,
        legacy_path_hash: str,
        source_kind: str,
        decompose_report: dict[str, Any],
        quarantine_reasons: list[str],
        run_id: str,
        run_started_at: str,
    ) -> ImportReceipt:
        review_id = compute_review_id_v2(
            tenant_id,
            source_namespace,
            source_kind,
            legacy_path_hash,
            legacy_source_sha256,
            run_id,
        )
        now = _utcnow_iso()
        payload = {
            "schema": "cwk.rt016.review_entry.v2",
            "identity_version": IDENTITY_VERSION,
            "review_id": review_id,
            "tenant_id": tenant_id,
            "source_namespace": source_namespace,
            "legacy_source_sha256": legacy_source_sha256,
            "legacy_path_hash": legacy_path_hash,
            "source_kind": source_kind,
            "decomposer_version": DECOMPOSER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "decompose_report": _copy_json(decompose_report),
            "migration_status": _review_migration_status(quarantine_reasons),
            "quarantine_reasons": sorted(set(quarantine_reasons)),
            "run_id": run_id,
            "run_started_at": run_started_at,
            "created_at": now,
            "record_revision": 1,
        }
        _validate_against(_REVIEW_ENTRY_V2_SCHEMA_ID, payload)
        with self._sub_fd(tfd, "locks") as lfd:
            with _A.exclusive_lock(lfd, f"rv.{review_id}.lock"):
                with self._sub_fd(tfd, "review") as rfd:
                    leaf = f"{review_id}.json"
                    if _A.child_exists(rfd, leaf):
                        existing_raw = _A.read_file(rfd, leaf)
                        existing = _load_review_payload(
                            existing_raw, review_id=review_id
                        )
                        # Belt-and-braces: the pre-existing file must
                        # match the caller-declared v2 identity.
                        if existing.get("schema") != "cwk.rt016.review_entry.v2":
                            raise LegacyImportError(
                                "existing review at v2 slot is not v2",
                                code="corrupt",
                                review_id=review_id,
                            )
                        if (
                            existing.get("tenant_id") != tenant_id
                            or existing.get("source_namespace") != source_namespace
                            or existing.get("source_kind") != source_kind
                            or existing.get("legacy_path_hash") != legacy_path_hash
                            or existing.get("legacy_source_sha256") != legacy_source_sha256
                            or existing.get("run_id") != run_id
                        ):
                            raise LegacyImportError(
                                "existing review entry binding fields disagree",
                                code="corrupt",
                                review_id=review_id,
                            )
                        payload = existing
                    else:
                        _A.cas_write(
                            rfd,
                            leaf,
                            _canonical_bytes(payload),
                            expected_previous_sha256=None,
                        )
        return ImportReceipt(
            outcome=payload["migration_status"],
            crosswalk_key=None,
            review_id=review_id,
            canonical_sha256=None,
            object_bytes_sha256=None,
            view_key=None,
            grant_key=None,
            tenant_view_written=False,
            tenant_view_deferred_reason="not_attempted",
            tenant_view_record_revision=None,
            quarantine_reasons=tuple(payload["quarantine_reasons"]),
        )

    def _append_manifest_entry(
        self,
        *,
        tfd: int,
        run_id: str,
        tenant_id: str,
        source_namespace: str,
        source_kind: str,
        legacy_source_sha256: str,
        legacy_path_hash: str,
        outcome: str,
        crosswalk_key: Optional[str],
        review_id: Optional[str],
        canonical_sha256: Optional[str],
        view_key: Optional[str],
        quarantine_reasons: Optional[list[str]],
    ) -> None:
        """Append one v2 manifest entry with namespace/path-aware dedupe.

        Dedupe key is
        ``(source_namespace, source_kind, legacy_path_hash,
        legacy_source_sha256, outcome)`` — v2 identity — so identical
        raw bytes under different namespaces / paths / kinds are counted
        independently.  This is the manifest-level closure of the
        Minor-2 fix at v2 identity granularity.

        v1 manifest lines under the same run_id (from legacy pre-v2
        runs) are refused as corrupt to avoid mixing dedup keys within
        one file; v2 always writes into a fresh run_id file in
        practice because the run_id itself is opaque and per-run.
        """

        now = _utcnow_iso()
        with self._sub_fd(tfd, "locks") as lfd:
            with _A.exclusive_lock(lfd, f"mf.{run_id}.lock"):
                with self._sub_fd(tfd, "manifests") as mfd:
                    leaf = f"{run_id}.jsonl"
                    if _A.child_exists(mfd, leaf):
                        current = _A.read_file(mfd, leaf)
                        current_sha = _sha256_bytes(current)
                        # Count existing entries.
                        existing_entries = current.split(b"\n") if current else []
                        seen: set[tuple[str, str, str, str, str]] = set()
                        entries_count = 0
                        for line in existing_entries:
                            if not line:
                                continue
                            try:
                                parsed = _C.strict_json_loads(line.decode("utf-8"))
                            except (_C.ContractError, ValueError, UnicodeDecodeError):
                                raise LegacyImportError(
                                    "manifest line corrupt", code="corrupt"
                                )
                            line_schema = (
                                parsed.get("schema") if isinstance(parsed, dict) else None
                            )
                            if line_schema == "cwk.rt016.migration_manifest_entry.v2":
                                _validate_against(_MANIFEST_ENTRY_V2_SCHEMA_ID, parsed)
                            else:
                                # Mixing v1 (or unknown) lines into a v2
                                # manifest file breaks the dedupe
                                # invariant; refuse to append.
                                raise LegacyImportError(
                                    "manifest line is not v2",
                                    code="corrupt",
                                )
                            entries_count += 1
                            seen.add(
                                (
                                    parsed["source_namespace"],
                                    parsed["source_kind"],
                                    parsed["legacy_path_hash"],
                                    parsed["legacy_source_sha256"],
                                    parsed["outcome"],
                                )
                            )
                        # Idempotent: skip if same v2 identity + outcome.
                        if (
                            source_namespace,
                            source_kind,
                            legacy_path_hash,
                            legacy_source_sha256,
                            outcome,
                        ) in seen:
                            return
                        entry_seq = entries_count + 1
                    else:
                        current = b""
                        current_sha = None
                        entry_seq = 1

                    entry: dict[str, Any] = {
                        "schema": "cwk.rt016.migration_manifest_entry.v2",
                        "identity_version": IDENTITY_VERSION,
                        "entry_seq": entry_seq,
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "source_namespace": source_namespace,
                        "source_kind": source_kind,
                        "legacy_source_sha256": legacy_source_sha256,
                        "legacy_path_hash": legacy_path_hash,
                        "outcome": outcome,
                        "created_at": now,
                    }
                    if crosswalk_key is not None:
                        entry["crosswalk_key"] = crosswalk_key
                    if review_id is not None:
                        entry["review_id"] = review_id
                    if canonical_sha256 is not None:
                        entry["canonical_sha256"] = canonical_sha256
                    if view_key is not None:
                        entry["view_key"] = view_key
                    if quarantine_reasons:
                        entry["quarantine_reasons"] = sorted(set(quarantine_reasons))
                    _validate_against(_MANIFEST_ENTRY_V2_SCHEMA_ID, entry)

                    new_bytes = current + _canonical_bytes(entry) + b"\n"
                    _A.cas_write(
                        mfd,
                        leaf,
                        new_bytes,
                        expected_previous_sha256=current_sha,
                    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _copy_json(payload: Any) -> Any:
    """Deep copy via strict JSON round-trip to strip references and
    enforce JSON-serialisability.  Never mutates the caller's payload.
    """

    return _C.strict_json_loads(json.dumps(payload, ensure_ascii=False))


def _review_migration_status(reasons: list[str]) -> str:
    """Map quarantine reasons to review vs undecomposable.

    Undecomposable is used when the raw bytes cannot even be
    structurally parsed (missing frontmatter, malformed frontmatter,
    missing body section).  Everything else defaults to review — the
    raw is parseable but its content fails a hard rule such as missing
    author, unparseable timestamp, oversize body, timeline mismatch.
    """

    undecomposable = {
        "malformed_frontmatter",
        "missing_frontmatter",
        "missing_body_section",
        "oversize_body",
        "path_containment_failure",
    }
    if any(r in undecomposable for r in reasons):
        return "undecomposable"
    return "review"


def _receipt_from_crosswalk(payload: dict[str, Any], *, outcome: str) -> ImportReceipt:
    return ImportReceipt(
        outcome=outcome,
        crosswalk_key=payload["crosswalk_key"],
        review_id=None,
        canonical_sha256=payload["canonical_sha256"],
        object_bytes_sha256=payload["object_bytes_sha256"],
        view_key=payload["view_key"],
        grant_key=payload["observe_grant_key"],
        tenant_view_written=payload["tenant_view_written"],
        tenant_view_deferred_reason=payload["tenant_view_deferred_reason"],
        tenant_view_record_revision=payload["tenant_view_record_revision"],
    )


def _receipt_from_review(payload: dict[str, Any], *, outcome: str) -> ImportReceipt:
    return ImportReceipt(
        outcome=outcome,
        crosswalk_key=None,
        review_id=payload["review_id"],
        canonical_sha256=None,
        object_bytes_sha256=None,
        view_key=None,
        grant_key=None,
        tenant_view_written=False,
        tenant_view_deferred_reason="not_attempted",
        tenant_view_record_revision=None,
        quarantine_reasons=tuple(payload.get("quarantine_reasons", [])),
    )


def _build_migration_snapshot(
    *, tenants: _R.TenantRegistry, tenant_id: str
) -> Any:
    """Build an AgentContextSnapshot for the migration upsert path.

    Only used when the caller passes an authority receipt (test path
    with FakeSigningAuthority).  The tenant must already be in
    ``pilot`` or ``active`` status; otherwise the ledger will refuse.
    """

    import cwk_agent_context as _AC

    record = tenants.get(tenant_id)
    return _AC.AgentContextSnapshot(
        agent_id_hash="0" * 64,
        tenant_id=tenant_id,
        tenant_auth_epoch=record.auth_epoch,
        binding_epoch=1,
        binding_secret_epoch=1,
        tenant_status=record.status,
        resolved_at=_utcnow_iso(),
    )


__all__ = [
    "DECOMPOSER_VERSION",
    "DecomposeResult",
    "DuplicateReportIdError",
    "IDENTITY_VERSION",
    "ImportReceipt",
    "LegacyDriftDetected",
    "LegacyImportError",
    "LegacyRawDecomposer",
    "LegacySource",
    "LogInjectionDetected",
    "NORMALIZER_VERSION",
    "RecoveryReport",
    "REGISTRY_SUBDIR",
    "STAGING_SUBDIR",
    "ShadowImporter",
    "compute_crosswalk_key",
    "compute_crosswalk_key_v2",
    "compute_legacy_path_hash",
    "compute_review_id",
    "compute_review_id_v2",
    "new_run_id",
]
