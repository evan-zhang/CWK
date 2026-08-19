#!/usr/bin/env python3
"""RT-011 (post-remediation): dual-user redacted comparator.

Redesign for r1 remediation:

- Input schema is now :data:`cwk.dual_user_observation.v1`.  The
  observation splits fields into ``canonical_fields`` (a fixed allowlist:
  ``title``, ``body``, ``author.source_user_id``, ``author.display_name``,
  ``created_at``, ``source_updated_at``) and ``overlay_fields`` (everything
  tenant-specific).  Only candidate fields inside ``canonical_fields`` can
  ever be recommended as ``candidate_verified_shared``.
- Overlay lists (``attachment_permissions``, ``reply_overlay``,
  ``node_overlay``) are recursed into so a temporary URL cannot slip past
  the top-level check.  Any ``temporary_url``/``preview_url``/etc. found
  aborts upgrade suggestions across the run.
- The upgrade threshold has a hard floor at 0.99; lower values raise
  ``ContractError``.
- For a candidate to be recommended it must:
    * appear in every one of the ≥50 unique common report_keys
      (100% coverage — 49/50 is not enough);
    * match at that same rate (99%+ per manifest);
    * have consistent canonical_sha256 across both tenants for those keys.
- Duplicate ``report_key`` entries within one tenant abort the run;
  conflicting canonical_sha256 for the same report_key between tenants
  disables promotion of every candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import cwk_pr001_contracts as C


DEFAULT_SHARE_UPGRADE_THRESHOLD = 0.99
MIN_COMMON_SAMPLES_FOR_UPGRADE = 50
THRESHOLD_FLOOR = 0.99

# The only field paths that may EVER be recommended for verified_shared
# upgrade.  Everything else stays overlay forever.
CANDIDATE_CANONICAL_FIELDS: tuple[str, ...] = (
    "canonical_fields.title",
    "canonical_fields.body",
    "canonical_fields.author.source_user_id",
    "canonical_fields.author.display_name",
    "canonical_fields.created_at",
    "canonical_fields.source_updated_at",
)


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
        "password",
        "token",
    }
)


@dataclass
class TenantObservationSet:
    tenant_id: str
    observations: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not C.TENANT_ID_REGEX.match(self.tenant_id or ""):
            raise C.ContractError(f"invalid tenant_id {self.tenant_id!r}", path="tenant_id")
        seen_keys: set[str] = set()
        for i, obs in enumerate(self.observations):
            _reject_forbidden(obs, path=f"observations[{i}]")
            C.validate_dual_user_observation(obs)
            if obs["tenant_id"] != self.tenant_id:
                raise C.ContractError(
                    "observation tenant_id does not match set tenant_id",
                    path=f"observations[{i}].tenant_id",
                )
            report_key = obs["report_key"]
            if report_key in seen_keys:
                raise C.ContractError(
                    f"duplicate report_key {report_key!r} in tenant observation set",
                    path=f"observations[{i}].report_key",
                )
            seen_keys.add(report_key)

    def by_report_key(self) -> dict[str, dict[str, Any]]:
        return {obs["report_key"]: obs for obs in self.observations}


def _reject_forbidden(payload: Mapping[str, Any], *, path: str) -> None:
    _scan_forbidden(payload, path=path)


def _scan_forbidden(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        hits = sorted(set(value.keys()) & _FORBIDDEN_INPUT_KEYS)
        if hits:
            raise C.ContractError(
                f"comparator refuses AppKey/credential/session/token fields: {hits}",
                path=path,
            )
        for k, v in value.items():
            _scan_forbidden(v, path=f"{path}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _scan_forbidden(item, path=f"{path}[{i}]")


def _dig(obj: Any, dotted_path: str) -> tuple[bool, Any]:
    """Return ``(present, value)`` for ``obj`` at the dotted path.

    Presence is False if any segment is missing.  Values may be ``None``.
    """

    current: Any = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _detect_temporary_url(value: Any) -> bool:
    """Recursively scan ``value`` for any temporary URL / token field."""

    banned = ("temporary_url", "preview_url", "presign_url", "download_url", "short_url")
    if isinstance(value, dict):
        for k, v in value.items():
            if k in banned:
                return True
            if _detect_temporary_url(v):
                return True
    elif isinstance(value, list):
        for item in value:
            if _detect_temporary_url(item):
                return True
    return False


def compare(
    tenant_a: TenantObservationSet,
    tenant_b: TenantObservationSet,
    *,
    upgrade_threshold: float = DEFAULT_SHARE_UPGRADE_THRESHOLD,
) -> dict[str, Any]:
    if tenant_a.tenant_id == tenant_b.tenant_id:
        raise C.ContractError("comparator requires two distinct tenants", path="tenant_id")
    if upgrade_threshold < THRESHOLD_FLOOR:
        raise C.ContractError(
            f"upgrade_threshold {upgrade_threshold} below floor {THRESHOLD_FLOOR}",
            path="upgrade_threshold",
        )

    a_map = tenant_a.by_report_key()
    b_map = tenant_b.by_report_key()
    common_keys = sorted(set(a_map) & set(b_map))
    only_a = sorted(set(a_map) - set(b_map))
    only_b = sorted(set(b_map) - set(a_map))

    # Detect canonical_sha256 mismatches for the same report_key.
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

    # Overlay diff statistics (never trigger upgrades).
    reply_diff = 0
    node_diff = 0
    attachment_diff = 0
    temporary_url_seen = False
    for key in common_keys:
        a_overlay = a_map[key].get("overlay_fields") or {}
        b_overlay = b_map[key].get("overlay_fields") or {}
        reply_diff += _multiset_diff_count(a_overlay.get("reply_overlay") or [], b_overlay.get("reply_overlay") or [], key_field="reply_id")
        node_diff += _multiset_diff_count(a_overlay.get("node_overlay") or [], b_overlay.get("node_overlay") or [], key_field="node_id")
        attachment_diff += _multiset_diff_count(a_overlay.get("attachment_permissions") or [], b_overlay.get("attachment_permissions") or [], key_field="attachment_id")
        if _detect_temporary_url(a_overlay) or _detect_temporary_url(b_overlay):
            temporary_url_seen = True

    # Candidate-field stats (only paths in CANDIDATE_CANONICAL_FIELDS).
    field_stats: dict[str, dict[str, Any]] = {}
    for path in CANDIDATE_CANONICAL_FIELDS:
        field_stats[path] = {
            "path": path,
            "common_samples": 0,
            "matched_samples": 0,
            "sample_ids": [],
        }

    for key in common_keys:
        for path in CANDIDATE_CANONICAL_FIELDS:
            entry = field_stats[path]
            present_a, val_a = _dig(a_map[key], path)
            present_b, val_b = _dig(b_map[key], path)
            if present_a and present_b:
                entry["common_samples"] += 1
                if val_a == val_b:
                    entry["matched_samples"] += 1
                    if len(entry["sample_ids"]) < 50:
                        entry["sample_ids"].append(key)

    suggestions_verified: list[dict[str, Any]] = []
    suggestions_overlay: list[dict[str, Any]] = []

    # Global upgrade block flags.
    block_reason: str | None = None
    if temporary_url_seen:
        block_reason = "temporary_url_present_in_overlay"
    elif canonical_mismatches:
        block_reason = "canonical_sha256_mismatch_between_tenants"
    elif len(common_keys) < MIN_COMMON_SAMPLES_FOR_UPGRADE:
        block_reason = f"fewer than {MIN_COMMON_SAMPLES_FOR_UPGRADE} unique common report_keys"

    for path, entry in field_stats.items():
        common = entry["common_samples"]
        matched = entry["matched_samples"]
        match_rate = (matched / common) if common else 0.0
        summary = {
            "path": path,
            "common_samples": common,
            "matched_samples": matched,
            "match_rate": round(match_rate, 6),
        }

        if block_reason is not None:
            summary["recommendation"] = "keep_in_tenant_overlay"
            summary["reason"] = block_reason
            suggestions_overlay.append(summary)
            continue

        # Coverage: field must appear in ALL common keys (100%) and there
        # must be at least MIN_COMMON_SAMPLES_FOR_UPGRADE unique keys.
        if common < MIN_COMMON_SAMPLES_FOR_UPGRADE or common < len(common_keys):
            summary["recommendation"] = "keep_in_tenant_overlay"
            summary["reason"] = (
                f"field only present in {common}/{len(common_keys)} common samples "
                f"(need == len(common) and >={MIN_COMMON_SAMPLES_FOR_UPGRADE})"
            )
            suggestions_overlay.append(summary)
            continue

        if match_rate < upgrade_threshold:
            summary["recommendation"] = "keep_in_tenant_overlay"
            summary["reason"] = f"match_rate {match_rate:.4f} below threshold {upgrade_threshold}"
            suggestions_overlay.append(summary)
            continue

        summary["recommendation"] = "candidate_verified_shared"
        summary["reason"] = (
            f"match_rate {match_rate:.4f} on all {common} unique common samples"
        )
        suggestions_verified.append(summary)

    return {
        "schema": "cwk.compare_user_views.v1",
        "tenant_a": tenant_a.tenant_id,
        "tenant_b": tenant_b.tenant_id,
        "sample_sizes": {"tenant_a": len(a_map), "tenant_b": len(b_map), "common": len(common_keys)},
        "only_in_tenant_a": only_a,
        "only_in_tenant_b": only_b,
        "canonical_sha256_mismatches": canonical_mismatches,
        "overlay_differences": {
            "reply": reply_diff,
            "node": node_diff,
            "attachment": attachment_diff,
            "temporary_url_seen": temporary_url_seen,
        },
        "upgrade_threshold": upgrade_threshold,
        "min_common_samples_for_upgrade": MIN_COMMON_SAMPLES_FOR_UPGRADE,
        "upgrade_block_reason": block_reason,
        "field_stats": [field_stats[p] for p in CANDIDATE_CANONICAL_FIELDS],
        "suggested_verified_shared": suggestions_verified,
        "suggested_tenant_overlay": suggestions_overlay,
    }


def _multiset_diff_count(a: Sequence[Any], b: Sequence[Any], *, key_field: str) -> int:
    a_ids = sorted(str(item.get(key_field, "")) for item in a if isinstance(item, dict))
    b_ids = sorted(str(item.get(key_field, "")) for item in b if isinstance(item, dict))
    if a_ids == b_ids:
        return 0
    return len(set(a_ids).symmetric_difference(set(b_ids)))


def load_observation_set(path: str) -> TenantObservationSet:
    payload = C.strict_json_load_path(Path(path))
    if not isinstance(payload, dict):
        raise C.ContractError("observation set must be a JSON object", path="<root>")
    _reject_forbidden(payload, path="<root>")
    return TenantObservationSet(
        tenant_id=payload.get("tenant_id", ""),
        observations=list(payload.get("observations", [])),
    )


__all__ = [
    "CANDIDATE_CANONICAL_FIELDS",
    "DEFAULT_SHARE_UPGRADE_THRESHOLD",
    "MIN_COMMON_SAMPLES_FOR_UPGRADE",
    "THRESHOLD_FLOOR",
    "TenantObservationSet",
    "compare",
    "load_observation_set",
]
