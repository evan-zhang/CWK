#!/usr/bin/env python3
"""RT-013: AgentContext — the trusted-source-only Agent identity adapter.

Owned by RT-013.  Every downstream RT (Query Broker, Collector, Scheduler,
Profile) *must* obtain the current Agent identity via
:meth:`AgentContext.from_trusted` and MUST NOT construct an AgentContext
from a request body, CLI query field, environment variable, or any
user-controlled input.

Design invariants (FROZEN):

- The raw ``agent_id`` NEVER lives inside an :class:`AgentContext`
  instance; only its HMAC hash and a small immutable snapshot do.
- Only sources listed in :data:`TRUSTED_AGENT_SOURCES` may construct
  contexts.  RT-013 registers ``"admin_cli"`` (used by the
  ``cwk-tenant bind/rebind/…`` provider) and ``"gateway_authenticated_context"``
  (reserved for the future RT-023 transport).  Adding a new source is a
  frozen extension point — updates to this set require an RT-023-level
  independent review.
- ``__init__`` is intentionally private (the class has no keyword-only
  factories other than :meth:`from_trusted`).  A caller who tries to
  bypass :meth:`from_trusted` (e.g. by directly calling ``__init__``) will
  raise :class:`AgentContextError`.
- ``__repr__`` / ``__str__`` / ``__hash__`` never leak the raw agent id
  or credential material; only the first eight hex characters of the
  hash plus tenant / epoch information are shown.
- :meth:`snapshot` returns an immutable dataclass suitable for use as
  part of a cache key by RT-022; this is the *only* form that leaves the
  module.  Cache implementations must treat the snapshot as opaque.

Never touches ``.env`` / ``CWORK_APP_KEY`` / real gateway / DocDB / cron;
only stdlib imports.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

import cwk_agent_binding as _AB
import cwk_instance as I
import cwk_tenant_registry as R


TRUSTED_AGENT_SOURCES: frozenset[str] = frozenset(
    {
        "admin_cli",
        # `gateway_authenticated_context` is present for RT-023 to activate
        # once the trusted transport is real; RT-013 keeps it in the frozen
        # allowlist so callers cannot secretly bootstrap it via a private
        # extension mechanism.  Until RT-023 validates the transport, no
        # code in this repository constructs an AgentContext from it.
        "gateway_authenticated_context",
    }
)


class AgentContextError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


_UTC = _dt.timezone.utc


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class AgentContextSnapshot:
    """Immutable summary suitable for cache-key composition.

    RT-022 will hash this snapshot together with the ``space_selector[]``
    and query hash to form the query broker's cache key.  Nothing about
    the raw agent id, credential material, or absolute host paths lives
    in a snapshot.
    """

    agent_id_hash: str
    tenant_id: str
    tenant_auth_epoch: int
    binding_epoch: int
    binding_secret_epoch: int
    tenant_status: str
    resolved_at: str


class AgentContext:
    """Resolved Agent identity — construct via :meth:`from_trusted` only.

    Instances are cheap and immutable.  RT-022 builds one context per
    request; the object owns no file descriptors.  It is safe to hold
    across a request but not longer — subsequent requests should
    re-resolve to refresh the ``binding_epoch`` / ``tenant_auth_epoch``
    snapshot.  The stored snapshot is the *only* stable identity data
    downstream code should hash into cache keys.
    """

    _ALLOWED_CONSTRUCTOR: ClassVar[str] = "from_trusted"

    def __init__(
        self,
        *,
        _snapshot: AgentContextSnapshot,
        _source: str,
        _construction_token: object,
    ) -> None:
        # A private token pattern: only :meth:`from_trusted` knows how to
        # create the sentinel required to reach ``__init__``.  Any external
        # caller that tries to construct an ``AgentContext`` directly hits
        # a stable, non-swallowable error.
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise AgentContextError(
                "AgentContext must be constructed via from_trusted(...)",
                code="direct_construction",
            )
        if not isinstance(_snapshot, AgentContextSnapshot):
            raise AgentContextError(
                "snapshot must be an AgentContextSnapshot",
                code="snapshot_type",
            )
        if _source not in TRUSTED_AGENT_SOURCES:
            raise AgentContextError(
                f"source {_source!r} is not trusted", code="untrusted_source"
            )
        self._snapshot = _snapshot
        self._source = _source

    @classmethod
    def from_trusted(
        cls,
        *,
        raw_agent_id: str,
        source: str,
        purpose: str,
        layout: I.InstanceLayout,
    ) -> "AgentContext":
        """Resolve a raw agent id from a trusted transport.

        ``source`` MUST be in :data:`TRUSTED_AGENT_SOURCES`.  RT-013's own
        admin CLI passes ``"admin_cli"``; RT-023 will pass
        ``"gateway_authenticated_context"``.  Any other source raises
        :class:`AgentContextError` — this is the only ingress for a raw
        agent id in the entire runtime.

        ``raw_agent_id`` is HMAC-hashed via the binding registry's current
        secret and never stored inside the returned context.  If the
        binding is unknown / revoked / suspended / offboarded, the tenant
        is in a disallowed status for ``purpose``, or the binding secret
        material is missing, the call fails closed.
        """

        if source not in TRUSTED_AGENT_SOURCES:
            raise AgentContextError(
                f"source {source!r} is not in TRUSTED_AGENT_SOURCES; "
                "request-body / self-reported identity is forbidden",
                code="untrusted_source",
            )
        # `resolve` performs raw_agent_id shape validation, HMAC, binding
        # lookup, status gating, and tenant operation-matrix enforcement.
        binding_registry = _AB.BindingRegistry(layout)
        record = binding_registry.resolve(raw_agent_id, purpose=purpose)
        tenant = R.TenantRegistry(layout).get(record.tenant_id)
        snapshot = AgentContextSnapshot(
            agent_id_hash=record.agent_id_hash,
            tenant_id=record.tenant_id,
            tenant_auth_epoch=tenant.auth_epoch,
            binding_epoch=record.binding_epoch,
            binding_secret_epoch=record.binding_secret_epoch,
            tenant_status=tenant.status,
            resolved_at=_utcnow_iso(),
        )
        # Deliberately drop the raw_agent_id reference before returning.
        # (Python is a GC language; we can only remove the local name.)
        del raw_agent_id
        return cls(
            _snapshot=snapshot,
            _source=source,
            _construction_token=_CONSTRUCTION_TOKEN,
        )

    @classmethod
    def from_snapshot(
        cls,
        *,
        snapshot: AgentContextSnapshot,
        source: str,
    ) -> "AgentContext":
        """Wrap an already-computed snapshot — used by RT-022 when it wants
        to serialise/deserialise the identity across a controlled queue
        without leaking the raw agent id.  ``source`` still must be in
        :data:`TRUSTED_AGENT_SOURCES`.  This is NOT a way to bypass the
        resolve pipeline: the snapshot must have been produced by a prior
        :meth:`from_trusted` call.
        """

        if source not in TRUSTED_AGENT_SOURCES:
            raise AgentContextError(
                f"source {source!r} is not in TRUSTED_AGENT_SOURCES",
                code="untrusted_source",
            )
        return cls(
            _snapshot=snapshot,
            _source=source,
            _construction_token=_CONSTRUCTION_TOKEN,
        )

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def source(self) -> str:
        return self._source

    @property
    def agent_id_hash(self) -> str:
        return self._snapshot.agent_id_hash

    @property
    def tenant_id(self) -> str:
        return self._snapshot.tenant_id

    @property
    def tenant_status(self) -> str:
        return self._snapshot.tenant_status

    @property
    def tenant_auth_epoch(self) -> int:
        return self._snapshot.tenant_auth_epoch

    @property
    def binding_epoch(self) -> int:
        return self._snapshot.binding_epoch

    @property
    def binding_secret_epoch(self) -> int:
        return self._snapshot.binding_secret_epoch

    def snapshot(self) -> AgentContextSnapshot:
        """Return the immutable snapshot used for cache-key composition."""

        return self._snapshot

    def redact(self) -> dict[str, Any]:
        """Return the log-safe representation.

        Only surfaces the first eight hex characters of the hash and the
        first eight chars of the tenant id; no raw agent id, no credential
        material, no absolute host paths.
        """

        return {
            "agent_id_hash_prefix": self._snapshot.agent_id_hash[:8],
            "tenant_id_prefix": self._snapshot.tenant_id[:8],
            "tenant_auth_epoch": self._snapshot.tenant_auth_epoch,
            "binding_epoch": self._snapshot.binding_epoch,
            "binding_secret_epoch": self._snapshot.binding_secret_epoch,
            "tenant_status": self._snapshot.tenant_status,
            "source": self._source,
            "resolved_at": self._snapshot.resolved_at,
        }

    # ------------------------------------------------------------------
    # Repr / equality
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        # Deliberately truncate hashes / tenant ids so a stray log line
        # cannot exfiltrate the full identity.
        return (
            f"AgentContext(source={self._source!r}, "
            f"hash={self._snapshot.agent_id_hash[:8]}..., "
            f"tenant={self._snapshot.tenant_id[:8]}..., "
            f"binding_epoch={self._snapshot.binding_epoch}, "
            f"tenant_auth_epoch={self._snapshot.tenant_auth_epoch})"
        )

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentContext):
            return NotImplemented
        return self._snapshot == other._snapshot and self._source == other._source

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash((self._snapshot, self._source))


# Sentinel token used to gate direct __init__ calls.  Not exported.
_CONSTRUCTION_TOKEN = object()


__all__ = [
    "AgentContext",
    "AgentContextError",
    "AgentContextSnapshot",
    "TRUSTED_AGENT_SOURCES",
]
