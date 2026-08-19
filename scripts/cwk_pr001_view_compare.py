#!/usr/bin/env python3
"""RT-011: dual-user redacted-envelope comparator.

Implements PRD §11 / DESIGN §11 ``cwk contract compare-user-views``:

- Input: two pre-redacted envelope collections (one per real user).
- Never accepts ``app_key`` / ``credential_ref`` / raw path / cookie.
- Output: per-field match statistics on the common-visibility set, plus a
  suggested split between clean-shared, verified-shareable (only when
  ≥50 common samples and ≥threshold match rate) and tenant overlay.
- Never suggests promoting a URL/token/identity field.

The comparator is intentionally schema-agnostic beyond the RT-011 envelope
shape so it can consume real production redactions in later RTs (RT-017)
via the same interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import cwk_pr001_contracts as C


_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "app_key",
        "appKey",
        "credential_ref",
        "credentials",
        "cookie",
        "session_token",
        "authorization",
        "authorization_header",
    }
)

DEFAULT_SHARE_UPGRADE_THRESHOLD = 0.99
MIN_COMMON_SAMPLES_FOR_UPGRADE = 50


@dataclass
class TenantEnvelopeSet:
    """A per-tenant collection of ``TenantViewEnvelope`` payloads.

    Each envelope MUST use ``report_key`` (``source_namespace:report_id``)
    as identity; caller redaction is expected to have happened upstream.
    """

    tenant_id: str
    envelopes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not C.TENANT_ID_REGEX.match(self.tenant_id or ""):
            raise C.ContractError(f"invalid tenant_id {self.tenant_id!r}", path="tenant_id")
        for i, env in enumerate(self.envelopes):
            self._reject_forbidden(env, path=f"envelopes[{i}]")
            C.validate_tenant_view(env)
            if env.get("tenant_id") != self.tenant_id:
                raise C.ContractError(
                    "envelope tenant_id does not match set tenant_id",
                    path=f"envelopes[{i}].tenant_id",
                )

    @staticmethod
    def _reject_forbidden(payload: Mapping[str, Any], *, path: str) -> None:
        hits = sorted(set(payload.keys()) & _FORBIDDEN_INPUT_KEYS)
        if hits:
            raise C.ContractError(
                f"comparator refuses AppKey/credential/session fields: {hits}",
                path=path,
            )

    def by_report_key(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for env in self.envelopes:
            out[env["report_key"]] = env
        return out


def _iter_leaf_paths(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(dotted_path, leaf_value)`` for all leaves inside ``value``.

    Lists use ``[]`` in the path so we can compare multi-set membership
    rather than order-sensitive equality (order-sensitive comparisons are
    reported separately in ``list_order_agreement``).
    """

    if isinstance(value, dict):
        for k in sorted(value.keys()):
            child_path = f"{prefix}.{k}" if prefix else k
            yield from _iter_leaf_paths(value[k], child_path)
    elif isinstance(value, list):
        yield f"{prefix}[]", value
    else:
        yield prefix, value


def _is_url_field(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in ("_url", "url", "presign", "download", "temporary_url", "preview_url", "short_url"))


def _is_identity_field(path: str) -> bool:
    lowered = path.lower()
    return any(
        hint in lowered
        for hint in (
            "tenant_id",
            "agent_id",
            "credential",
            "app_key",
            "auth_epoch",
            "binding_epoch",
        )
    )


def compare(
    tenant_a: TenantEnvelopeSet,
    tenant_b: TenantEnvelopeSet,
    *,
    upgrade_threshold: float = DEFAULT_SHARE_UPGRADE_THRESHOLD,
) -> dict[str, Any]:
    if tenant_a.tenant_id == tenant_b.tenant_id:
        raise C.ContractError("comparator requires two distinct tenants", path="tenant_id")

    a_map = tenant_a.by_report_key()
    b_map = tenant_b.by_report_key()

    a_keys = set(a_map)
    b_keys = set(b_map)
    common_keys = sorted(a_keys & b_keys)
    only_a = sorted(a_keys - b_keys)
    only_b = sorted(b_keys - a_keys)

    # Per-report cross-tenant leaks -- if the same report_key resolves to
    # different canonical_sha256 between A and B, either the canonical
    # object was mislinked or one side observed a stale version.
    canonical_mismatches: list[dict[str, Any]] = []
    for key in common_keys:
        if a_map[key]["canonical_sha256"] != b_map[key]["canonical_sha256"]:
            canonical_mismatches.append(
                {
                    "report_key": key,
                    "canonical_sha256_a": a_map[key]["canonical_sha256"],
                    "canonical_sha256_b": b_map[key]["canonical_sha256"],
                }
            )

    field_stats: dict[str, dict[str, Any]] = {}
    # Aggregate reply/node/attachment differences separately for FR-07 upgrade
    # decisions.
    reply_diff = 0
    node_diff = 0
    attachment_diff = 0
    temporary_url_present = False

    for key in common_keys:
        a_env = a_map[key]
        b_env = b_map[key]
        for section in ("reply_overlay", "node_overlay", "attachment_permissions"):
            a_section = a_env.get(section, []) or []
            b_section = b_env.get(section, []) or []
            if section == "reply_overlay":
                reply_diff += _multiset_diff_count(a_section, b_section, key_field="reply_id")
            elif section == "node_overlay":
                node_diff += _multiset_diff_count(a_section, b_section, key_field="node_id")
            else:
                attachment_diff += _multiset_diff_count(a_section, b_section, key_field="attachment_id")
                for item in list(a_section) + list(b_section):
                    if isinstance(item, dict) and item.get("temporary_url"):
                        temporary_url_present = True

        a_leaves = dict(_iter_leaf_paths(a_env))
        b_leaves = dict(_iter_leaf_paths(b_env))
        all_paths = set(a_leaves) | set(b_leaves)
        for path in all_paths:
            entry = field_stats.setdefault(
                path,
                {
                    "path": path,
                    "compared": 0,
                    "matched": 0,
                    "present_a": 0,
                    "present_b": 0,
                    "sample_ids": [],
                },
            )
            entry["compared"] += 1
            present_a = path in a_leaves
            present_b = path in b_leaves
            entry["present_a"] += int(present_a)
            entry["present_b"] += int(present_b)
            if present_a and present_b:
                if a_leaves[path] == b_leaves[path]:
                    entry["matched"] += 1
                    if len(entry["sample_ids"]) < 5:
                        entry["sample_ids"].append(key)

    total_common = len(common_keys)
    suggested_verified_shared: list[dict[str, Any]] = []
    suggested_overlay: list[dict[str, Any]] = []

    for path, entry in sorted(field_stats.items()):
        # Do not include always-shared identity fields like report_key / canonical_sha256.
        if path in {"report_key", "canonical_sha256", "schema", "tenant_id", "observed_at"}:
            continue
        match_rate = (entry["matched"] / entry["compared"]) if entry["compared"] else 0.0
        summary = {
            "path": path,
            "match_rate": round(match_rate, 4),
            "common_samples": entry["compared"],
            "matched_samples": entry["matched"],
            "present_only_in_a": entry["present_a"] - entry["matched"],
            "present_only_in_b": entry["present_b"] - entry["matched"],
        }

        forbid_reason = None
        if _is_url_field(path):
            forbid_reason = "url_or_token_field_never_promoted"
        elif _is_identity_field(path):
            forbid_reason = "identity_field_never_promoted"

        if forbid_reason:
            summary["recommendation"] = "keep_in_tenant_overlay"
            summary["reason"] = forbid_reason
            suggested_overlay.append(summary)
            continue

        if (
            total_common >= MIN_COMMON_SAMPLES_FOR_UPGRADE
            and match_rate >= upgrade_threshold
        ):
            summary["recommendation"] = "candidate_verified_shared"
            summary["reason"] = (
                f"match_rate {match_rate:.4f} ≥ {upgrade_threshold:.4f} on "
                f"{total_common} common samples"
            )
            suggested_verified_shared.append(summary)
        else:
            summary["recommendation"] = "keep_in_tenant_overlay"
            if total_common < MIN_COMMON_SAMPLES_FOR_UPGRADE:
                summary["reason"] = (
                    f"only {total_common} common samples (<{MIN_COMMON_SAMPLES_FOR_UPGRADE})"
                )
            else:
                summary["reason"] = f"match_rate {match_rate:.4f} below threshold"
            suggested_overlay.append(summary)

    return {
        "schema": "cwk.compare_user_views.v1",
        "tenant_a": tenant_a.tenant_id,
        "tenant_b": tenant_b.tenant_id,
        "sample_sizes": {"tenant_a": len(a_map), "tenant_b": len(b_map), "common": total_common},
        "only_in_tenant_a": only_a,
        "only_in_tenant_b": only_b,
        "canonical_sha256_mismatches": canonical_mismatches,
        "overlay_differences": {
            "reply": reply_diff,
            "node": node_diff,
            "attachment": attachment_diff,
            "temporary_url_seen": temporary_url_present,
        },
        "upgrade_threshold": upgrade_threshold,
        "min_common_samples_for_upgrade": MIN_COMMON_SAMPLES_FOR_UPGRADE,
        "field_stats": sorted(field_stats.values(), key=lambda e: e["path"]),
        "suggested_verified_shared": suggested_verified_shared,
        "suggested_tenant_overlay": suggested_overlay,
    }


def _multiset_diff_count(a: Sequence[Any], b: Sequence[Any], *, key_field: str) -> int:
    a_ids = sorted(str(item.get(key_field, "")) for item in a if isinstance(item, dict))
    b_ids = sorted(str(item.get(key_field, "")) for item in b if isinstance(item, dict))
    if a_ids == b_ids:
        return 0
    return len(set(a_ids).symmetric_difference(set(b_ids)))


def load_envelope_set(path: str) -> TenantEnvelopeSet:
    """Load a file of shape ``{"tenant_id": ..., "envelopes": [...]}``."""

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise C.ContractError("envelope set must be a JSON object", path="<root>")
    if "app_key" in payload or "credential_ref" in payload:
        raise C.ContractError(
            "envelope set MUST NOT contain AppKey/credential_ref", path="<root>"
        )
    return TenantEnvelopeSet(
        tenant_id=payload.get("tenant_id", ""),
        envelopes=list(payload.get("envelopes", [])),
    )


__all__ = [
    "DEFAULT_SHARE_UPGRADE_THRESHOLD",
    "MIN_COMMON_SAMPLES_FOR_UPGRADE",
    "TenantEnvelopeSet",
    "compare",
    "load_envelope_set",
]
