"""RT-011: byte contracts, nine-schema positive/negative, transitions, and
verified-shared-extensions guards.

These tests exercise the frozen v1 contract library
(`scripts/cwk_pr001_contracts.py`) without touching real credentials, real
CWork data, or writing outside the RT-011 fixture area.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402


TENANT_A = "t_" + "a" * 26
TENANT_B = "t_" + "b" * 26
SPACE_1 = "sp_abcdef0123"
SPACE_2 = "sp_9876543210"
FIXED_SHA = "0" * 64


def _iso(seconds: int = 0) -> str:
    base = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
    return base.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_body(**overrides) -> dict:
    body = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": "cwork",
        "report_id": "2070001",
        "title": "汇报-1",
        "author": {"source_user_id": "u_writer_1", "display_name": "张三"},
        "created_at": "2026-08-01T10:00:00Z",
        "source_updated_at": "2026-08-01T12:00:00Z",
        "body": "正文 with unicode é",
        "normalizer_version": "v1",
    }
    body.update(overrides)
    body["canonical_sha256"] = C.canonical_sha256({k: v for k, v in body.items() if k != "canonical_sha256"})
    return body


def _grant(**overrides):
    payload = {
        "schema": "cwk.access_grant.v1",
        "tenant_id": TENANT_A,
        "source_namespace": "cwork",
        "report_id": "2070001",
        "status": "active",
        "roles": ["receiver"],
        "visibility_scope": "full",
        "permission_source": "tenant_appkey_observation",
        "auth_epoch": 1,
        "granted_at": _iso(),
        "last_verified_at": _iso(),
        "lease_expires_at": _iso(),
        "revoked_at": None,
    }
    payload.update(overrides)
    return payload


class ByteContractTests(unittest.TestCase):
    def test_object_id_format_and_regex(self):
        for _ in range(10):
            oid = C.new_object_id()
            self.assertRegex(oid, r"^o_[a-z2-7]{26}$")
            C.validate_object_id(oid)

    def test_object_id_random_bytes_size(self):
        with self.assertRaises(C.ContractError):
            C.new_object_id(random_bytes=b"\x00" * 15)

    def test_object_id_rejects_bad_shapes(self):
        for bad in ("obj_abc", "o_UPPER0000000000000000000000", "o_", "o_" + "0" * 26, "o_abcdefghijklmnop", ""):
            with self.assertRaises(C.ContractError):
                C.validate_object_id(bad)

    def test_report_key_default_and_parse(self):
        key = C.compose_report_key("cwork", "207xxxx")
        self.assertEqual(key, "cwork:207xxxx")
        ns, rid = C.parse_report_key(key)
        self.assertEqual((ns, rid), ("cwork", "207xxxx"))

    def test_report_key_namespace_isolation(self):
        """Same report_id in different namespaces MUST NOT collide."""

        a = C.compose_report_key("cwork", "5")
        b = C.compose_report_key("third_party_wf", "5")
        self.assertNotEqual(a, b)

    def test_report_key_rejects_invalid_shapes(self):
        for ns, rid in [
            ("CWORK", "1"),
            ("cwork", ""),
            ("", "1"),
            ("cwork", "with space"),
            ("cwork", "a" * 257),
            ("1cwork", "1"),
        ]:
            with self.assertRaises(C.ContractError):
                C.compose_report_key(ns, rid)

    def test_nfc_normalization_before_jcs(self):
        """NFC-normalise strings first so JCS bytes are stable across composers."""

        # é in NFC vs decomposed NFD
        nfc = {"x": "é"}
        nfd = {"x": "é"}
        self.assertEqual(C.canonical_json_bytes(nfc), C.canonical_json_bytes(nfd))

    def test_jcs_sort_keys_and_no_whitespace(self):
        payload = {"z": 1, "a": {"y": 2, "b": [3, 2, 1]}}
        data = C.canonical_json_bytes(payload)
        self.assertEqual(data, b'{"a":{"b":[3,2,1],"y":2},"z":1}')

    def test_jcs_rejects_bool_as_number(self):
        # bool must serialise as true/false, never as 1/0.
        self.assertEqual(C.canonical_json_bytes({"t": True, "f": False}), b'{"f":false,"t":true}')

    def test_jcs_rejects_non_finite(self):
        with self.assertRaises(C.ContractError):
            C.canonical_json_bytes({"nan": float("nan")})
        with self.assertRaises(C.ContractError):
            C.canonical_json_bytes({"inf": float("inf")})

    def test_profile_sha256_formula(self):
        proposal = {"a": 1, "spaces": [{"space_id": SPACE_1}]}
        sample_sha = C.canonical_sha256({"tag": "sample"})
        prompt_sha = C.canonical_sha256({"tag": "prompt"})
        result = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal,
            sample_manifest_sha256=sample_sha,
            prompt_template_sha256=prompt_sha,
            model_id="newapi/BD-MiniMax",
        )

        # Recompute the byte formula by hand to freeze it.
        import hashlib

        expected = hashlib.sha256(
            b"cwk-profile-v1"
            + b"\x00"
            + C.canonical_json_bytes(proposal)
            + b"\x00"
            + sample_sha.encode("ascii")
            + b"\x00"
            + prompt_sha.encode("ascii")
            + b"\x00"
            + "newapi/BD-MiniMax".encode("utf-8")
        ).hexdigest()
        self.assertEqual(result, expected)

    def test_profile_sha256_is_domain_separated(self):
        """Swapping component boundaries MUST change the hash."""

        proposal = {"a": 1}
        sample_sha = "aa" * 32
        prompt_sha = "bb" * 32
        base = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal,
            sample_manifest_sha256=sample_sha,
            prompt_template_sha256=prompt_sha,
            model_id="m",
        )
        # Move a byte from model_id into prompt_template_sha256 (must fail
        # because prompt sha regex requires 64 hex).
        with self.assertRaises(C.ContractError):
            C.compute_profile_sha256(
                nfc_normalized_proposal=proposal,
                sample_manifest_sha256=sample_sha,
                prompt_template_sha256=prompt_sha[:-1],
                model_id="1m",
            )
        # If we swap sample/prompt hashes we still get a *different* profile hash
        # because they enter at distinct positions.
        alt = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal,
            sample_manifest_sha256=prompt_sha,
            prompt_template_sha256=sample_sha,
            model_id="m",
        )
        self.assertNotEqual(base, alt)

    def test_profile_sha256_rejects_missing_inputs(self):
        with self.assertRaises(C.ContractError):
            C.compute_profile_sha256(
                nfc_normalized_proposal={},
                sample_manifest_sha256="",
                prompt_template_sha256="",
                model_id="m",
            )
        with self.assertRaises(C.ContractError):
            C.compute_profile_sha256(
                nfc_normalized_proposal={},
                sample_manifest_sha256="a" * 64,
                prompt_template_sha256="b" * 64,
                model_id="",
            )


class SchemaValidatorTests(unittest.TestCase):
    """Positive and negative cases across the nine core schemas."""

    def test_report_key_payload_positive_negative(self):
        C.validate_report_key_payload({"schema": "cwk.report_key.v1", "source_namespace": "cwork", "report_id": "1"})
        with self.assertRaises(C.ContractError):
            C.validate_report_key_payload({"schema": "cwk.report_key.v1"})

    def test_canonical_envelope_positive_and_recomputed_hash(self):
        body = _canonical_body()
        C.validate_canonical_envelope(body)

    def test_canonical_envelope_rejects_forbidden_fields(self):
        body = _canonical_body()
        body["tenant_id"] = TENANT_A
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(body)
        body = _canonical_body()
        body["attachment_url"] = "https://x"
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(body)
        body = _canonical_body()
        body["lane"] = "received"
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(body)

    def test_canonical_envelope_hash_must_match(self):
        body = _canonical_body()
        body["canonical_sha256"] = "0" * 64  # tampered
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(body)

    def test_tenant_view_positive_and_negative(self):
        C.validate_tenant_view(
            {
                "schema": "cwk.tenant_view.v1",
                "tenant_id": TENANT_A,
                "report_key": "cwork:1",
                "canonical_sha256": FIXED_SHA,
                "observed_at": _iso(),
            }
        )
        with self.assertRaises(C.ContractError):
            C.validate_tenant_view(
                {
                    "schema": "cwk.tenant_view.v1",
                    "tenant_id": "not-a-tenant",
                    "report_key": "cwork:1",
                    "canonical_sha256": FIXED_SHA,
                    "observed_at": _iso(),
                }
            )

    def test_tenant_view_rejects_path_injection_report_key(self):
        with self.assertRaises(C.ContractError):
            C.validate_tenant_view(
                {
                    "schema": "cwk.tenant_view.v1",
                    "tenant_id": TENANT_B,
                    "report_key": "cwork:../../../etc/passwd\nagent_id=root",
                    "canonical_sha256": FIXED_SHA,
                    "observed_at": _iso(),
                }
            )

    def test_access_observation_only_discovered_or_granted(self):
        for status in ("discovered", "granted"):
            C.validate_access_observation(
                {
                    "schema": "cwk.access_observation.v1",
                    "tenant_id": TENANT_A,
                    "source_namespace": "cwork",
                    "report_id": "1",
                    "observed_at": _iso(),
                    "observation_source": "legacy_raw_decomposition",
                    "initial_status": status,
                }
            )
        with self.assertRaises(C.ContractError):
            C.validate_access_observation(
                {
                    "schema": "cwk.access_observation.v1",
                    "tenant_id": TENANT_A,
                    "source_namespace": "cwork",
                    "report_id": "1",
                    "observed_at": _iso(),
                    "observation_source": "legacy_raw_decomposition",
                    "initial_status": "active",
                }
            )

    def test_access_grant_seven_states(self):
        self.assertEqual(len(C.ACCESS_GRANT_STATES), 7)
        for status in C.ACCESS_GRANT_STATES:
            C.validate_access_grant(_grant(status=status))

    def test_access_grant_query_eligibility_only_active(self):
        self.assertEqual(C.ACCESS_GRANT_QUERY_ELIGIBLE, {"active"})

    def test_access_grant_transitions(self):
        # Legal edges from the frozen table.
        for src, dsts in C.ACCESS_GRANT_ALLOWED_TRANSITIONS.items():
            for dst in dsts:
                C.validate_access_grant_transition(src, dst)
        # A handful of forbidden edges that would enable a re-activation loophole.
        for src, dst in [
            ("purged", "active"),
            ("revoked", "active"),
            ("revalidation_due", "granted"),
            ("granted", "purged"),
            ("active", "purge_pending"),
        ]:
            with self.assertRaises(C.ContractError):
                C.validate_access_grant_transition(src, dst)

    def test_knowledge_profile_six_states_and_no_rolled_back(self):
        self.assertEqual(len(C.KNOWLEDGE_PROFILE_STATES), 6)
        self.assertNotIn("rolled_back", C.KNOWLEDGE_PROFILE_STATES)

    def test_knowledge_profile_transitions_and_rollback_via_pointer_only(self):
        # active -> superseded is normal supersession; superseded -> active is
        # ONLY reachable via a profile_pointer_rollback audit event.
        C.validate_knowledge_profile_transition("active", "superseded")
        C.validate_knowledge_profile_transition("superseded", "active")
        for bad in [("draft", "active"), ("draft", "superseded"), ("preview", "active"), ("confirmed", "superseded"), ("active", "draft")]:
            with self.assertRaises(C.ContractError):
                C.validate_knowledge_profile_transition(*bad)

    def test_profile_pointer_rollback_positive_negative(self):
        payload = {
            "schema": "cwk.profile_pointer_rollback.v1",
            "event_type": "profile_pointer_rollback",
            "tenant_id": TENANT_A,
            "from_version": "v2",
            "from_profile_sha256": "1" * 64,
            "to_version": "v1",
            "to_profile_sha256": "2" * 64,
            "actor": "admin@example",
            "reason": "regression in v2 routing",
            "occurred_at": _iso(),
            "auth_epoch_before": 5,
            "auth_epoch_after": 6,
        }
        C.validate_profile_pointer_rollback(payload)
        # Rollback must strictly increment auth_epoch.
        bad = dict(payload, auth_epoch_after=5)
        with self.assertRaises(C.ContractError):
            C.validate_profile_pointer_rollback(bad)
        # Rollback must swap versions.
        bad = dict(payload, from_version="v1", to_version="v1")
        with self.assertRaises(C.ContractError):
            C.validate_profile_pointer_rollback(bad)

    def test_route_decision_space_ids_opaque_only(self):
        decision = {
            "schema": "cwk.route_decision.v1",
            "tenant_id": TENANT_A,
            "report_key": "cwork:1",
            "canonical_sha256": FIXED_SHA,
            "disposition": "index",
            "space_ids": [SPACE_1, SPACE_2],
            "confidence": 0.9,
            "reason_codes": ["confirmed_entity"],
            "profile_version": "v1",
            "profile_sha256": "a" * 64,
            "decided_by": "deterministic+model",
            "decided_at": _iso(),
        }
        C.validate_route_decision(decision)
        # Slugs must be rejected.
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(decision, space_ids=["tbs", SPACE_1]))
        # Duplicate space_ids must be rejected.
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(decision, space_ids=[SPACE_1, SPACE_1]))
        # Unknown disposition must be rejected.
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(decision, disposition="delete"))

    def test_query_request_selector_and_forbidden(self):
        req = {"schema": "cwk.query_request.v1", "query": "TBS 风险", "space_selector": [SPACE_1]}
        C.validate_query_request(req)
        with self.assertRaises(C.ContractError):
            C.validate_query_request(dict(req, space_selector=["tbs"]))
        with self.assertRaises(C.ContractError):
            C.validate_query_request(dict(req, tenant_id=TENANT_A))
        with self.assertRaises(C.ContractError):
            C.validate_query_request(dict(req, profile_sha256="f" * 64))
        with self.assertRaises(C.ContractError):
            C.validate_query_request(dict(req, include_dispositions=["bogus"]))

    def test_sample_manifest_holdout_no_overlap(self):
        manifest = {
            "schema": "cwk.sample_manifest.v1",
            "tenant_id": TENANT_A,
            "random_seed": "deadbeefdeadbeef",
            "target_sample_size": 150,
            "actual_sample_size": 2,
            "strata": [],
            "chunk_size": 25,
            "chunk_layout": [{"chunk_id": "c1", "report_keys": ["cwork:1"]}],
            "samples": [
                {"report_key": "cwork:1", "canonical_sha256": "a" * 64},
                {"report_key": "cwork:2", "canonical_sha256": "b" * 64},
            ],
            "holdout": [
                {"report_key": "cwork:99", "canonical_sha256": "c" * 64},
            ],
            "created_at": _iso(),
        }
        C.validate_sample_manifest(manifest)
        overlapping = dict(manifest, holdout=[{"report_key": "cwork:1", "canonical_sha256": "a" * 64}])
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(overlapping)

    def test_sample_manifest_target_bounds(self):
        base = {
            "schema": "cwk.sample_manifest.v1",
            "tenant_id": TENANT_A,
            "random_seed": "deadbeefdeadbeef",
            "target_sample_size": 99,
            "actual_sample_size": 1,
            "strata": [],
            "chunk_size": 20,
            "chunk_layout": [],
            "samples": [],
            "holdout": [],
            "created_at": _iso(),
        }
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(base)
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(dict(base, target_sample_size=201))

    def test_verified_shared_extensions_never_promote_url_or_identity(self):
        base = {
            "schema": "cwk.verified_shared_extensions.v1",
            "version": "v2",
            "manifest_sha256": "1" * 64,
            "compared_sample_size": 200,
            "min_field_match_rate": 0.995,
            "approved_by": "user",
            "approved_at": _iso(),
            "entries": [],
        }
        C.validate_verified_shared_extensions(base)
        for bad_path in (
            "attachments[*].temporary_url",
            "attachments[*].preview_url",
            "short_url",
            "reply_overlay[*].temporary_url",
            "tenant_id",
            "credential_ref",
        ):
            with self.assertRaises(C.ContractError):
                C.validate_verified_shared_extensions(
                    dict(base, entries=[{"field_path": bad_path, "match_rate": 0.99, "sample_ids": ["cwork:1"]}])
                )

    def test_verified_shared_extensions_requires_min_sample_size_50(self):
        with self.assertRaises(C.ContractError):
            C.validate_verified_shared_extensions(
                {
                    "schema": "cwk.verified_shared_extensions.v1",
                    "version": "v2",
                    "manifest_sha256": "1" * 64,
                    "compared_sample_size": 49,
                    "min_field_match_rate": 0.99,
                    "approved_by": "user",
                    "approved_at": _iso(),
                    "entries": [],
                }
            )

    def test_capability_probe_forbidden_forced_conservative(self):
        # Even if someone hand-writes a payload for the forbidden probe, the
        # validator rejects "verified".
        with self.assertRaises(C.ContractError):
            C.validate_capability_probe(
                {
                    "schema": "cwk.capability_probe.v1",
                    "probe_id": "sandbox_transport_loopback_http_self_reported",
                    "run_at": _iso(),
                    "result": "verified",
                    "conservative_default": "reject",
                    "evidence_refs": ["https://x"],
                }
            )

    def test_dispatch_by_schema(self):
        C.validate({"schema": "cwk.report_key.v1", "source_namespace": "cwork", "report_id": "1"})
        with self.assertRaises(C.ContractError):
            C.validate({"schema": "cwk.unknown.v99"})
        with self.assertRaises(C.ContractError):
            C.validate("not-a-dict")


class SecurityDefaultsTests(unittest.TestCase):
    def test_bundled_security_defaults_validate(self):
        payload = C.load_security_defaults()
        C.validate_security_defaults(payload)
        self.assertEqual(payload["schema"], "cwk.security_defaults.v1")
        self.assertEqual(payload["version"], "v1")
        # Forbidden transport is machine-readable.
        self.assertEqual(
            payload["transport_and_identity"]["forbidden_transport"],
            "loopback_http_self_reported_agent_id",
        )
        # Grace-read is forbidden.
        self.assertTrue(payload["access_grant"]["grace_read_forbidden"])
        # No fabricated SLA numbers.
        self.assertIsNone(payload["access_grant"]["revocation_ack_target_seconds"])
        self.assertIsNone(payload["access_grant"]["derived_cleanup_target_seconds"])

    def test_bootstrap_verified_shared_extensions_is_empty(self):
        payload = C.load_verified_shared_extensions_v1()
        self.assertEqual(payload["entries"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
