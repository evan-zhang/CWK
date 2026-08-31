#!/usr/bin/env python3
"""RT-011 (post-remediation): read-only external capability probes.

Redesign for the r1 remediation:

- Probe payload no longer carries free-form ``notes`` or arbitrary
  ``evidence_refs`` strings — the schema only accepts a structured
  ``receipt`` sub-object.
- A probe result MAY be ``verified`` only when a well-formed
  ``ReceiptEnvelope`` is present, is signed by a signer on the frozen
  ``TRUSTED_PROBE_SIGNERS`` allowlist, targets the specific ``probe_id``,
  runs in a whitelisted ``environment`` (``gateway_production`` or
  ``gateway_control``), is inside its validity window and passes signature
  verification.
- ``TRUSTED_PROBE_SIGNERS`` is empty in RT-011.  RT-023 will publish real
  signers.  Tests use ``_register_test_probe_signer`` (imported from the
  contracts module) to inject a signer for the duration of a single test.
- ``aggregate`` requires every frozen probe_id to be present exactly once
  before it can even consider ``all_verified``; a subset input can never
  produce ``all_verified=true``.  The permanently-forbidden
  ``sandbox_transport_loopback_http_self_reported`` must remain
  conservative for the aggregate to be complete.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import cwk_pr001_contracts as C


ALL_PROBE_IDS: tuple[str, ...] = (
    "report_id_global_uniqueness",
    "permission_authoritative_events",
    "permission_authoritative_api",
    "trusted_agent_identity_openclaw_tool",
    "trusted_agent_identity_uds_peercred",
    "sandbox_transport_openclaw_tool",
    "sandbox_transport_uds",
    "sandbox_transport_loopback_http_self_reported",
    "verified_shared_extensions_dual_user_sample",
)

POLICY_FORBIDDEN_UPGRADE: frozenset[str] = frozenset(
    {"sandbox_transport_loopback_http_self_reported"}
)

CONSERVATIVE_DEFAULTS: dict[str, str] = {
    "report_id_global_uniqueness": "compose_report_key(source_namespace, report_id)",
    "permission_authoritative_events": "revalidation_due if lease >15min; fail closed",
    "permission_authoritative_api": "revalidation_due if lease >15min; fail closed",
    "trusted_agent_identity_openclaw_tool": "reject query (fail closed)",
    "trusted_agent_identity_uds_peercred": "reject query (fail closed)",
    "sandbox_transport_openclaw_tool": "reject query (fail closed)",
    "sandbox_transport_uds": "reject query (fail closed)",
    "sandbox_transport_loopback_http_self_reported": "reject (policy-forbidden)",
    "verified_shared_extensions_dual_user_sample": "field remains in tenant overlay",
}


@dataclass(frozen=True)
class ReceiptEnvelope:
    """A signed capability receipt.

    Callers use :func:`build_receipt` to compute ``envelope_sha256`` and
    ``signature`` from a registered test signer; production code will call
    the equivalent code path against a real HSM / KMS in RT-023.
    """

    probe_id: str
    signer: str
    envelope_sha256: str
    signature: str
    target: str
    environment: str
    not_before: str
    not_after: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_sha256": self.envelope_sha256,
            "signer": self.signer,
            "signature": self.signature,
            "target": self.target,
            "environment": self.environment,
            "not_before": self.not_before,
            "not_after": self.not_after,
        }


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_receipt(
    *,
    probe_id: str,
    signer: str,
    environment: str,
    not_before: str,
    not_after: str,
    payload_body: Mapping[str, Any],
) -> ReceiptEnvelope:
    """Assemble a receipt envelope and sign it using the process-local secret.

    Only useful inside tests (RT-023 will replace this with an HSM path).
    ``payload_body`` is any dict describing the external evidence; its
    canonical sha256 becomes ``envelope_sha256``.
    """

    if environment not in ("gateway_production", "gateway_control"):
        raise C.ContractError("environment must be gateway_production or gateway_control")
    envelope_sha256 = C.canonical_sha256(payload_body)
    secret = C._TEST_PROBE_SIGNING_SECRETS.get(signer)
    if secret is None:
        raise C.ContractError(f"no registered signing secret for signer {signer!r}")
    signature = hashlib.sha256(
        secret + envelope_sha256.encode("ascii") + probe_id.encode("ascii")
    ).hexdigest()
    return ReceiptEnvelope(
        probe_id=probe_id,
        signer=signer,
        envelope_sha256=envelope_sha256,
        signature=signature,
        target=probe_id,
        environment=environment,
        not_before=not_before,
        not_after=not_after,
    )


def run_probe(
    probe_id: str,
    *,
    receipt: ReceiptEnvelope | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Return a ``cwk.capability_probe.v1`` payload for ``probe_id``.

    - Without a receipt: always ``conservative_unknown``.
    - With a receipt: the schema custom keyword validates trust anchor +
      target + signature; any failure raises ``ContractError``.
    - The permanently-forbidden probe never emits ``verified`` even with a
      receipt.
    """

    if probe_id not in ALL_PROBE_IDS:
        raise C.ContractError(f"unknown probe_id {probe_id!r}", path="probe_id")

    ts = now or _utcnow()

    if receipt is None or probe_id in POLICY_FORBIDDEN_UPGRADE:
        payload = {
            "schema": "cwk.capability_probe.v1",
            "probe_id": probe_id,
            "run_at": ts,
            "result": "conservative_unknown",
            "conservative_default": CONSERVATIVE_DEFAULTS[probe_id],
            "receipt": None,
        }
        C.validate_capability_probe(payload)
        return payload

    # Time-window check before signature verification.
    if receipt.target != probe_id:
        raise C.ContractError("receipt.target must equal probe_id", path="receipt.target")
    _now = datetime.now(tz=timezone.utc)
    try:
        nb = datetime.fromisoformat(receipt.not_before.replace("Z", "+00:00"))
        na = datetime.fromisoformat(receipt.not_after.replace("Z", "+00:00"))
    except ValueError as exc:
        raise C.ContractError(f"invalid receipt validity window: {exc}", path="receipt")
    if _now < nb or _now > na:
        # A receipt outside its window is treated as conservative_unknown;
        # we do not raise (that would let a malicious payload crash the CLI).
        payload = {
            "schema": "cwk.capability_probe.v1",
            "probe_id": probe_id,
            "run_at": ts,
            "result": "conservative_unknown",
            "conservative_default": CONSERVATIVE_DEFAULTS[probe_id],
            "receipt": None,
        }
        C.validate_capability_probe(payload)
        return payload

    payload = {
        "schema": "cwk.capability_probe.v1",
        "probe_id": probe_id,
        "run_at": ts,
        "result": "verified",
        "conservative_default": CONSERVATIVE_DEFAULTS[probe_id],
        "receipt": receipt.to_dict(),
    }
    C.validate_capability_probe(payload)  # runs signer/signature check
    return payload


def run_default_matrix(*, now: str | None = None) -> list[dict[str, Any]]:
    """Run the frozen probe matrix in policy-only mode (all conservative)."""

    return [run_probe(pid, receipt=None, now=now) for pid in ALL_PROBE_IDS]


def aggregate(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of probe results into a decision summary.

    - Every payload is validated first (via ``validate_capability_probe``);
      any invalid entry raises ``ContractError`` immediately.
    - Duplicate ``probe_id`` values are rejected.
    - ``all_verified`` is true only if:
        * every frozen probe_id is present exactly once, AND
        * every non-forbidden probe is ``verified``, AND
        * every forbidden probe is ``conservative_unknown``.
    - Missing probe_ids are listed under ``missing_probe_ids``.
    """

    seen_ids: dict[str, str] = {}
    for probe in probes:
        C.validate_capability_probe(probe)
        pid = probe["probe_id"]
        if pid in seen_ids:
            raise C.ContractError(f"duplicate probe_id {pid!r} in aggregate", path="aggregate.probes")
        seen_ids[pid] = probe["result"]

    verified = sorted(pid for pid, r in seen_ids.items() if r == "verified")
    unknown = sorted(pid for pid, r in seen_ids.items() if r == "conservative_unknown")
    forbidden = sorted(pid for pid in seen_ids if pid in POLICY_FORBIDDEN_UPGRADE)
    missing = sorted(set(ALL_PROBE_IDS) - set(seen_ids))

    complete = not missing
    all_non_forbidden_verified = all(
        seen_ids.get(pid) == "verified"
        for pid in ALL_PROBE_IDS
        if pid not in POLICY_FORBIDDEN_UPGRADE
    )
    all_forbidden_conservative = all(
        seen_ids.get(pid) == "conservative_unknown" for pid in POLICY_FORBIDDEN_UPGRADE
    )
    all_verified = complete and all_non_forbidden_verified and all_forbidden_conservative

    conservative_map = {pid: CONSERVATIVE_DEFAULTS[pid] for pid in ALL_PROBE_IDS}

    return {
        "schema": "cwk.capability_probe_aggregate.v1",
        "run_at": _utcnow(),
        "results": seen_ids,
        "verified": verified,
        "conservative_unknown": unknown,
        "policy_forbidden_probe_ids": forbidden,
        "missing_probe_ids": missing,
        "conservative_defaults": conservative_map,
        "complete": complete,
        "all_verified": all_verified,
    }


def load_probe_file(path: str) -> dict[str, Any]:
    payload = C.strict_json_load_path(__import__("pathlib").Path(path))
    C.validate_capability_probe(payload)
    return payload


__all__ = [
    "ALL_PROBE_IDS",
    "CONSERVATIVE_DEFAULTS",
    "POLICY_FORBIDDEN_UPGRADE",
    "ReceiptEnvelope",
    "aggregate",
    "build_receipt",
    "load_probe_file",
    "run_default_matrix",
    "run_probe",
]
