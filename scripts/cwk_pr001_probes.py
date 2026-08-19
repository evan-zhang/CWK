#!/usr/bin/env python3
"""RT-011: read-only external capability probes.

Per PR-001 plan §3, RT-011 must "只允许 verified / conservative_unknown；无真实样本时必须
conservative_unknown，不能伪造 PASS." (only ``verified`` / ``conservative_unknown``;
without real samples MUST be conservative_unknown, MUST NOT fabricate PASS.)

Five probe families are shipped as skeletons:

1. ``report_id_global_uniqueness`` — is ``report_id`` globally unique across
   ``source_namespace``?  Default: ``conservative_unknown`` with the
   conservative default ``ReportKey = source_namespace + report_id``.
2. ``permission_authoritative_*`` — does the source expose authoritative
   revocation events / API?  Default: ``conservative_unknown`` with the
   conservative default "15-minute lease revalidation, fail closed".
3. ``trusted_agent_identity_*`` — does Gateway provide unforgeable Agent
   identity via OpenClaw Tool metadata or UDS SO_PEERCRED?  Default:
   ``conservative_unknown``.
4. ``sandbox_transport_*`` — is the sandbox restricted to the approved
   transport?  ``sandbox_transport_loopback_http_self_reported`` is
   policy-forbidden and MUST always emit ``conservative_unknown``.
5. ``verified_shared_extensions_dual_user_sample`` — do we have ≥50
   unique common-visibility samples proving a tenant-overlay field is
   safely shareable?  Default: ``conservative_unknown``.

An **evidence bundle** may upgrade a probe to ``verified`` only if the
``evidence_kind`` is ``controlled_environment_receipt``.  Any evidence with
``kind=="fixture"`` (or a ref starting with ``fixture://``) MUST NOT
upgrade the result; fixtures are for shape validation, not truth.
"""

from __future__ import annotations

import json
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


@dataclass
class EvidenceBundle:
    """Evidence that may upgrade a probe to ``verified``.

    ``kind == "controlled_environment_receipt"`` is the only value that
    permits upgrade.  Anything else (``"fixture"``, ``"mock"``,
    ``"assertion"``, ``"documentation"``, ...) is accepted for record-keeping
    but never promotes the probe.
    """

    kind: str
    refs: tuple[str, ...] = ()
    notes: str = ""
    sample_size: int = 0
    unique_report_key_pairs: int = 0

    def is_authoritative(self, *, probe_id: str) -> bool:
        if probe_id in POLICY_FORBIDDEN_UPGRADE:
            return False
        if self.kind != "controlled_environment_receipt":
            return False
        if not self.refs:
            return False
        if any(str(ref).startswith("fixture://") for ref in self.refs):
            return False
        if probe_id == "verified_shared_extensions_dual_user_sample":
            # Extra minimum threshold per PRD FR-07 and DESIGN §11.
            if self.unique_report_key_pairs < 50:
                return False
        return True


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_probe(
    probe_id: str,
    *,
    evidence: EvidenceBundle | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Return a ``cwk.capability_probe.v1`` payload for ``probe_id``.

    The function itself never fabricates ``verified``: only an authoritative
    evidence bundle can upgrade the result.
    """

    if probe_id not in ALL_PROBE_IDS:
        raise C.ContractError(f"unknown probe_id {probe_id!r}", path="probe_id")

    evidence = evidence or EvidenceBundle(kind="none")
    authoritative = evidence.is_authoritative(probe_id=probe_id)

    result = "verified" if authoritative else "conservative_unknown"
    payload: dict[str, Any] = {
        "schema": "cwk.capability_probe.v1",
        "probe_id": probe_id,
        "run_at": now or _utcnow(),
        "result": result,
        "conservative_default": CONSERVATIVE_DEFAULTS[probe_id],
        "evidence_refs": list(evidence.refs),
        "notes": evidence.notes or None,
    }
    C.validate_capability_probe(payload)
    return payload


def run_default_matrix(*, now: str | None = None) -> list[dict[str, Any]]:
    """Run the frozen probe matrix in policy-only mode (all conservative)."""

    return [run_probe(pid, evidence=None, now=now) for pid in ALL_PROBE_IDS]


def aggregate(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of probe results into a decision summary.

    Returns a dict with:
      - ``schema`` and ``run_at``;
      - ``results`` mapping probe_id → result;
      - ``all_verified`` bool — True only if every probe returned ``verified``;
      - ``verified`` and ``conservative_unknown`` lists;
      - ``policy_forbidden_probe_ids`` list (those that MUST remain
        conservative_unknown by policy);
      - ``conservative_defaults`` mapping for consumers to inspect.

    In RT-011 policy-only mode ``all_verified`` MUST be false because no
    controlled-environment receipts are attached.
    """

    seen_ids: dict[str, str] = {}
    verified: list[str] = []
    unknown: list[str] = []
    forbidden: list[str] = []
    conservative_map: dict[str, str] = {}
    for probe in probes:
        C.validate_capability_probe(probe)
        pid = probe["probe_id"]
        if pid in seen_ids:
            raise C.ContractError(f"duplicate probe_id {pid!r} in aggregate", path="aggregate.probes")
        seen_ids[pid] = probe["result"]
        conservative_map[pid] = probe["conservative_default"]
        if probe["result"] == "verified":
            verified.append(pid)
        else:
            unknown.append(pid)
        if pid in POLICY_FORBIDDEN_UPGRADE:
            forbidden.append(pid)

    all_verified = bool(verified) and not unknown
    return {
        "schema": "cwk.capability_probe_aggregate.v1",
        "run_at": _utcnow(),
        "results": seen_ids,
        "verified": sorted(verified),
        "conservative_unknown": sorted(unknown),
        "policy_forbidden_probe_ids": sorted(forbidden),
        "conservative_defaults": conservative_map,
        "all_verified": all_verified,
    }


def load_probe_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    C.validate_capability_probe(payload)
    return payload


__all__ = [
    "ALL_PROBE_IDS",
    "CONSERVATIVE_DEFAULTS",
    "EvidenceBundle",
    "POLICY_FORBIDDEN_UPGRADE",
    "aggregate",
    "load_probe_file",
    "run_default_matrix",
    "run_probe",
]
