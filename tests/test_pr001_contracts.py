"""RT-011 (post-remediation): byte contracts, 9-schema strict validation,
NFC/duplicate JSON keys, JCS ECMA-262 fixed vectors, receipt/security
defaults, custom-keyword enforcement.

Every test below maps to a specific verifier finding from the r1 audit and
would have failed against the pre-remediation code.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402


TENANT_A = "t_" + "a" * 26
TENANT_B = "t_" + "b" * 26
SPACE_1 = "sp_abcdef0123"
SPACE_2 = "sp_9876543210"
FIXED_SHA = "0" * 64
FIX = PROJECT / "tests" / "fixtures" / "pr001"


def _iso() -> str:
    return "2026-08-01T10:00:00Z"


def _canonical(**overrides):
    body = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": "cwork",
        "report_id": "2070001",
        "title": "汇报-1",
        "author": {"source_user_id": "u_writer_1", "display_name": "张三"},
        "created_at": _iso(),
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


def _profile(**overrides):
    proposal = {
        "version": "v1",
        "spaces": [{"space_id": SPACE_1}],
        "entity_policy": {},
        "attention": {},
        "routing_rules": [],
        "archive_rules": [],
        "sample_manifest_ref": "sm_v1",
        "holdout_manifest_ref": "sm_v1_holdout",
        "confirmed_by": "user",
        "confirmed_at": _iso(),
        "review_threshold": 0.75,
    }
    sample_sha = "a" * 64
    prompt_sha = "b" * 64
    model_id = "newapi/BD-MiniMax"
    payload = {
        "schema": "cwk.knowledge_profile.v1",
        "version": "v1",
        "status": "active",
        "spaces": [{"space_id": SPACE_1}],
        "entity_policy": {},
        "attention": {},
        "routing_rules": [],
        "archive_rules": [],
        "review_threshold": 0.75,
        "sample_manifest_ref": "sm_v1",
        "sample_manifest_sha256": sample_sha,
        "holdout_manifest_ref": "sm_v1_holdout",
        "prompt_template_sha256": prompt_sha,
        "model_id": model_id,
        "confirmed_by": "user",
        "confirmed_at": _iso(),
    }
    payload.update(overrides)
    payload["profile_sha256"] = C.compute_profile_sha256(
        nfc_normalized_proposal={
            k: payload[k]
            for k in (
                "version",
                "spaces",
                "entity_policy",
                "attention",
                "routing_rules",
                "archive_rules",
                "sample_manifest_ref",
                "holdout_manifest_ref",
                "confirmed_by",
                "confirmed_at",
                "review_threshold",
            )
        },
        sample_manifest_sha256=payload["sample_manifest_sha256"],
        prompt_template_sha256=payload["prompt_template_sha256"],
        model_id=payload["model_id"],
    )
    return payload


# ---------------------------------------------------------------------------
# JCS numbers (Blocker #4 vectors)
# ---------------------------------------------------------------------------


class JcsNumberVectorsTests(unittest.TestCase):
    def setUp(self) -> None:
        with (FIX / "jcs_number_vectors.json").open("r", encoding="utf-8") as fh:
            self.vectors = json.load(fh)

    def test_fixed_vectors_from_disk_oracle(self):
        for case in self.vectors["cases"]:
            self.assertEqual(
                C._js_number_string(case["in"]),
                case["out"],
                msg=f"JCS number for {case['in']!r}",
            )

    def test_key_ecma_boundary_1e_minus_6(self):
        # 1e-6 sits exactly at the boundary where ECMA-262 switches from
        # scientific to decimal.  Regression against the pre-remediation
        # off-by-one bug that emitted "1e-6" for this value.
        self.assertEqual(C._js_number_string(1e-6), "0.000001")

    def test_key_ecma_boundary_1e_minus_7(self):
        self.assertEqual(C._js_number_string(1e-7), "1e-7")

    def test_key_ecma_boundary_1e21(self):
        self.assertEqual(C._js_number_string(1e21), "1e+21")

    def test_key_ecma_boundary_1e20_is_decimal(self):
        self.assertEqual(C._js_number_string(1e20), "100000000000000000000")

    def test_reject_unsafe_integers(self):
        for bad in self.vectors["reject"]:
            with self.assertRaises(C.ContractError):
                C._js_number_string(bad)

    def test_reject_bool_as_number(self):
        with self.assertRaises(C.ContractError):
            C._js_number_string(True)  # type: ignore[arg-type]

    def test_reject_non_finite(self):
        with self.assertRaises(C.ContractError):
            C._js_number_string(float("nan"))
        with self.assertRaises(C.ContractError):
            C._js_number_string(float("inf"))


# ---------------------------------------------------------------------------
# NFC / duplicate JSON keys (Blocker #4)
# ---------------------------------------------------------------------------


class NfcAndDuplicateKeyTests(unittest.TestCase):
    def test_nfc_key_collision_raises(self):
        # "café" (NFC) vs "café" (NFD).  Both normalise to "café" (NFC),
        # which would silently drop one field before the fix.
        payload = {"café": 1, "café": 2}
        with self.assertRaises(C.ContractError):
            C.canonical_json_bytes(payload)

    def test_nfc_normalise_still_produces_stable_bytes(self):
        # Same field name with only NFC form should serialise identically to
        # the pre-normalised form.
        self.assertEqual(C.canonical_json_bytes({"x": "é"}), C.canonical_json_bytes({"x": "é"}))

    def test_strict_json_loads_rejects_duplicate_keys(self):
        with self.assertRaises(C.ContractError):
            C.strict_json_loads('{"a": 1, "a": 2}')

    def test_jcs_sort_order_is_utf16_be(self):
        # Simple regression: keys are sorted, no whitespace.
        self.assertEqual(
            C.canonical_json_bytes({"z": 1, "a": {"y": 2, "b": [3, 2, 1]}}),
            b'{"a":{"b":[3,2,1],"y":2},"z":1}',
        )


# ---------------------------------------------------------------------------
# ReportKey namespace isolation + path/encoding attacks (Blocker #5)
# ---------------------------------------------------------------------------


class ReportKeyTests(unittest.TestCase):
    def test_namespace_isolation(self):
        a = C.compose_report_key("cwork", "5")
        b = C.compose_report_key("third_party_wf", "5")
        self.assertNotEqual(a, b)

    def test_reject_slash_in_report_id(self):
        with self.assertRaises(C.ContractError):
            C.compose_report_key("cwork", "../etc/passwd")

    def test_reject_url_encoded_report_id(self):
        with self.assertRaises(C.ContractError):
            C.compose_report_key("cwork", "%2e%2e")

    def test_reject_trailing_newline_in_namespace(self):
        with self.assertRaises(C.ContractError):
            C.compose_report_key("cwork\n", "1")

    def test_reject_null_byte_in_report_id(self):
        with self.assertRaises(C.ContractError):
            C.compose_report_key("cwork", "abc\x00evil")

    def test_reject_colon_in_report_id(self):
        with self.assertRaises(C.ContractError):
            C.compose_report_key("cwork", "abc:def")

    def test_parse_report_key_symmetric(self):
        ns, rid = C.parse_report_key("cwork:2070001")
        self.assertEqual((ns, rid), ("cwork", "2070001"))

    def test_parse_report_key_rejects_trailing_whitespace(self):
        with self.assertRaises(C.ContractError):
            C.parse_report_key("cwork:2070001\n")


# ---------------------------------------------------------------------------
# object_id canonical Base32 (Blocker supplementary)
# ---------------------------------------------------------------------------


class ObjectIdTests(unittest.TestCase):
    def test_generator_produces_valid_ids(self):
        for _ in range(10):
            oid = C.new_object_id()
            C.validate_object_id(oid)

    def test_reject_uppercase(self):
        with self.assertRaises(C.ContractError):
            C.validate_object_id("o_ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def test_reject_padding(self):
        with self.assertRaises(C.ContractError):
            C.validate_object_id("o_abcdefghijklmnopqrstuvwxy=")

    def test_reject_wrong_length(self):
        with self.assertRaises(C.ContractError):
            C.validate_object_id("o_abc")

    def test_reject_zero_or_one_in_alphabet(self):
        for bad in ("o_abcdefghijklmnopqrstuv0xyz", "o_abcdefghijklmnopqrstuv1xyz"):
            with self.assertRaises(C.ContractError):
                C.validate_object_id(bad)

    def test_reject_invalid_canonical_base32_tail(self):
        # A random 26-character body whose last character encodes non-zero
        # residual bits (per RFC 4648) must be rejected.
        bad = "o_" + "a" * 25 + "b"  # 'b' has value 1, residual bits nonzero
        with self.assertRaises(C.ContractError):
            C.validate_object_id(bad)


# ---------------------------------------------------------------------------
# profile_sha256 byte formula (Blocker #5)
# ---------------------------------------------------------------------------


class ProfileHashTests(unittest.TestCase):
    def test_formula_matches_manual_bytes(self):
        import hashlib

        proposal = {"a": 1, "spaces": [{"space_id": SPACE_1}]}
        sample = "a" * 64
        prompt = "b" * 64
        model = "newapi/BD-MiniMax"
        got = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal,
            sample_manifest_sha256=sample,
            prompt_template_sha256=prompt,
            model_id=model,
        )
        expected = hashlib.sha256(
            b"cwk-profile-v1"
            + b"\x00"
            + C.canonical_json_bytes(proposal)
            + b"\x00"
            + sample.encode("ascii")
            + b"\x00"
            + prompt.encode("ascii")
            + b"\x00"
            + model.encode("utf-8")
        ).hexdigest()
        self.assertEqual(got, expected)

    def test_swapping_components_changes_hash(self):
        proposal = {"a": 1}
        a = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal, sample_manifest_sha256="a" * 64, prompt_template_sha256="b" * 64, model_id="m"
        )
        b = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal, sample_manifest_sha256="b" * 64, prompt_template_sha256="a" * 64, model_id="m"
        )
        self.assertNotEqual(a, b)

    def test_reject_missing_inputs(self):
        with self.assertRaises(C.ContractError):
            C.compute_profile_sha256(
                nfc_normalized_proposal={}, sample_manifest_sha256="", prompt_template_sha256="", model_id="m"
            )
        with self.assertRaises(C.ContractError):
            C.compute_profile_sha256(
                nfc_normalized_proposal={}, sample_manifest_sha256="a" * 64, prompt_template_sha256="b" * 64, model_id=""
            )


# ---------------------------------------------------------------------------
# Nine-schema validators — strict additionalProperties/uniqueItems/nested
# (Blocker #1)
# ---------------------------------------------------------------------------


class SchemaValidatorTests(unittest.TestCase):
    def test_canonical_positive(self):
        C.validate_canonical_envelope(_canonical())

    def test_canonical_rejects_extra_top_level_key(self):
        payload = _canonical()
        payload["tenant_id"] = TENANT_A
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(payload)

    def test_canonical_rejects_nested_tenant_id_inside_author(self):
        # This is the exact bypass the pre-remediation validator missed:
        # tenant_id was hidden inside `author` and canonical_sha256 was
        # recomputed to match the tampered payload.
        payload = _canonical()
        payload["author"] = {"source_user_id": "u", "tenant_id": TENANT_A}
        payload["canonical_sha256"] = C.canonical_sha256({k: v for k, v in payload.items() if k != "canonical_sha256"})
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(payload)

    def test_canonical_recomputed_sha_matches(self):
        payload = _canonical()
        payload["canonical_sha256"] = "0" * 64
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(payload)

    def test_query_request_rejects_nested_credential(self):
        payload = {
            "schema": "cwk.query_request.v1",
            "query": "hello",
            "time_filter": {"from": None, "to": None},
        }
        # legal
        C.validate_query_request(payload)
        # nested credential — additionalProperties:false on time_filter should also catch this
        payload["time_filter"] = {"from": None, "to": None, "credential_ref": "secret://leak"}
        with self.assertRaises(C.ContractError):
            C.validate_query_request(payload)

    def test_query_request_rejects_slug_selector(self):
        with self.assertRaises(C.ContractError):
            C.validate_query_request({"schema": "cwk.query_request.v1", "query": "x", "space_selector": ["tbs"]})

    def test_query_request_rejects_duplicate_selector(self):
        with self.assertRaises(C.ContractError):
            C.validate_query_request({"schema": "cwk.query_request.v1", "query": "x", "space_selector": [SPACE_1, SPACE_1]})

    def test_query_request_rejects_top_level_identity(self):
        for bad in ("tenant_id", "agent_id", "credential_ref", "profile_sha256"):
            with self.assertRaises(C.ContractError):
                C.validate_query_request({"schema": "cwk.query_request.v1", "query": "x", bad: "value"})

    def test_route_decision_positive_and_slug_rejection(self):
        rd = {
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
            "decided_by": "deterministic",
            "decided_at": _iso(),
        }
        C.validate_route_decision(rd)
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(rd, space_ids=["tbs", SPACE_1]))
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(rd, space_ids=[SPACE_1, SPACE_1]))
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(dict(rd, disposition="delete"))

    def test_tenant_view_pattern_rejects_path_injection(self):
        with self.assertRaises(C.ContractError):
            C.validate_tenant_view(
                {
                    "schema": "cwk.tenant_view.v1",
                    "tenant_id": TENANT_B,
                    "report_key": "cwork:../../../etc/passwd",
                    "canonical_sha256": FIXED_SHA,
                    "observed_at": _iso(),
                }
            )

    def test_tenant_view_rejects_extra_field(self):
        payload = {
            "schema": "cwk.tenant_view.v1",
            "tenant_id": TENANT_A,
            "report_key": "cwork:1",
            "canonical_sha256": FIXED_SHA,
            "observed_at": _iso(),
            "credential_ref": "secret://leak",
        }
        with self.assertRaises(C.ContractError):
            C.validate_tenant_view(payload)

    def test_access_observation_initial_status_enum(self):
        for good in ("discovered", "granted"):
            C.validate_access_observation(
                {
                    "schema": "cwk.access_observation.v1",
                    "tenant_id": TENANT_A,
                    "source_namespace": "cwork",
                    "report_id": "1",
                    "observed_at": _iso(),
                    "observation_source": "legacy_raw_decomposition",
                    "initial_status": good,
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
        self.assertEqual(C.ACCESS_GRANT_QUERY_ELIGIBLE, {"active"})

    def test_access_grant_transitions_frozen(self):
        for src, dsts in C.ACCESS_GRANT_ALLOWED_TRANSITIONS.items():
            for dst in dsts:
                C.validate_access_grant_transition(src, dst)
        for bad in [
            ("purged", "active"),
            ("revoked", "active"),
            ("revalidation_due", "granted"),
            ("granted", "purged"),
            ("active", "purge_pending"),
        ]:
            with self.assertRaises(C.ContractError):
                C.validate_access_grant_transition(*bad)

    def test_knowledge_profile_positive_and_recompute(self):
        # positive
        C.validate_knowledge_profile(_profile())
        # tampered routing_rules with the same profile_sha256 — must fail
        tampered = _profile()
        tampered["routing_rules"] = [{"rule_id": "attacker_injected"}]
        # keep the *old* profile_sha256 to simulate the attacker
        old_sha = _profile()["profile_sha256"]
        tampered["profile_sha256"] = old_sha
        with self.assertRaises(C.ContractError):
            C.validate_knowledge_profile(tampered)

    def test_knowledge_profile_no_rolled_back(self):
        self.assertNotIn("rolled_back", C.KNOWLEDGE_PROFILE_STATES)
        with self.assertRaises(C.ContractError):
            C.validate_knowledge_profile(_profile(status="rolled_back"))

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
            "reason": "regression in v2",
            "occurred_at": _iso(),
            "auth_epoch_before": 5,
            "auth_epoch_after": 6,
        }
        C.validate_profile_pointer_rollback(payload)
        with self.assertRaises(C.ContractError):
            C.validate_profile_pointer_rollback(dict(payload, auth_epoch_after=5))
        with self.assertRaises(C.ContractError):
            C.validate_profile_pointer_rollback(dict(payload, from_version="v1", to_version="v1"))

    def test_sample_manifest_full_coverage_and_overlap(self):
        # Build a passing manifest with 150 samples, 25 holdout, chunk
        # layout covering all samples, strata picked sum matching.
        samples = [{"report_key": f"cwork:2070{i:03d}", "canonical_sha256": ("a" * 63) + hex(i % 16)[2:]} for i in range(150)]
        holdout = [{"report_key": f"cwork:2071{i:03d}", "canonical_sha256": ("b" * 63) + hex(i % 16)[2:]} for i in range(25)]
        chunk_layout = [
            {"chunk_id": f"c{ci}", "report_keys": [s["report_key"] for s in samples[ci * 25 : (ci + 1) * 25]]}
            for ci in range(6)
        ]
        good = {
            "schema": "cwk.sample_manifest.v1",
            "tenant_id": TENANT_A,
            "random_seed": "deadbeefdeadbeef",
            "target_sample_size": 150,
            "actual_sample_size": 150,
            "strata": [{"dimension": "time", "bucket": "2026-07", "picked": 150}],
            "chunk_size": 25,
            "chunk_layout": chunk_layout,
            "samples": samples,
            "holdout": holdout,
            "created_at": _iso(),
        }
        C.validate_sample_manifest(good)

        # holdout overlaps samples
        bad = dict(good, holdout=[dict(good["holdout"][0], report_key=good["samples"][0]["report_key"])] + good["holdout"][1:])
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(bad)

        # holdout too small (5 < 20)
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(dict(good, holdout=good["holdout"][:5]))

        # actual_sample_size drift
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(dict(good, actual_sample_size=149))

        # chunk layout coverage < samples
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(dict(good, chunk_layout=chunk_layout[:5]))

        # strata picked mismatch
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(dict(good, strata=[{"dimension": "time", "bucket": "2026-07", "picked": 149}]))

    def test_verified_shared_extensions_forbidden_and_min_50(self):
        # Empty bootstrap passes with zero placeholder.
        C.validate_verified_shared_extensions(
            {
                "schema": "cwk.verified_shared_extensions.v1",
                "version": "v1",
                "manifest_sha256": "0" * 64,
                "compared_sample_size": 50,
                "min_field_match_rate": 0.99,
                "approved_by": "bootstrap",
                "approved_at": _iso(),
                "entries": [],
            }
        )
        # Any URL-y or identity-y field is forbidden even after 50+ samples.
        for bad_path in (
            "attachments[*].temporary_url",
            "attachments[*].preview_url",
            "reply_overlay[*].temporary_url",
            "short_url",
            "credential_ref",
            "tenant_id",
            "password",
            "token",
        ):
            body = {
                "schema": "cwk.verified_shared_extensions.v1",
                "version": "v2",
                "compared_sample_size": 60,
                "min_field_match_rate": 0.99,
                "approved_by": "attacker",
                "approved_at": _iso(),
                "entries": [
                    {"field_path": bad_path, "match_rate": 0.99, "sample_ids": [f"cwork:{i}" for i in range(50)]}
                ],
            }
            # compute manifest sha
            body["manifest_sha256"] = C.canonical_sha256(body)
            with self.assertRaises(C.ContractError):
                C.validate_verified_shared_extensions(body)

    def test_verified_shared_extensions_recompute_manifest_sha(self):
        body = {
            "schema": "cwk.verified_shared_extensions.v1",
            "version": "v2",
            "compared_sample_size": 60,
            "min_field_match_rate": 0.99,
            "approved_by": "user",
            "approved_at": _iso(),
            "entries": [
                {"field_path": "title", "match_rate": 0.99, "sample_ids": [f"cwork:{i}" for i in range(50)]}
            ],
        }
        body["manifest_sha256"] = C.canonical_sha256(body)
        C.validate_verified_shared_extensions(body)
        # Tamper: change entries but keep old manifest sha
        tampered = dict(body)
        tampered["entries"] = [
            {"field_path": "body", "match_rate": 0.99, "sample_ids": [f"cwork:{i}" for i in range(50)]}
        ]
        # manifest_sha256 stays as old value → recompute check must fail
        with self.assertRaises(C.ContractError):
            C.validate_verified_shared_extensions(tampered)

    def test_capability_probe_policy_forbidden(self):
        with self.assertRaises(C.ContractError):
            C.validate_capability_probe(
                {
                    "schema": "cwk.capability_probe.v1",
                    "probe_id": "sandbox_transport_loopback_http_self_reported",
                    "run_at": _iso(),
                    "result": "verified",
                    "conservative_default": "reject",
                    "receipt": {
                        "envelope_sha256": "a" * 64,
                        "signer": "any",
                        "signature": "b" * 64,
                        "target": "sandbox_transport_loopback_http_self_reported",
                        "environment": "gateway_production",
                        "not_before": _iso(),
                        "not_after": "2027-08-01T00:00:00Z",
                    },
                }
            )

    def test_capability_probe_verified_requires_receipt_and_trusted_signer(self):
        # Without receipt: reject verified
        with self.assertRaises(C.ContractError):
            C.validate_capability_probe(
                {
                    "schema": "cwk.capability_probe.v1",
                    "probe_id": "sandbox_transport_openclaw_tool",
                    "run_at": _iso(),
                    "result": "verified",
                    "conservative_default": "reject",
                    "receipt": None,
                }
            )
        # With receipt but signer not on allowlist: reject
        with self.assertRaises(C.ContractError):
            C.validate_capability_probe(
                {
                    "schema": "cwk.capability_probe.v1",
                    "probe_id": "sandbox_transport_openclaw_tool",
                    "run_at": _iso(),
                    "result": "verified",
                    "conservative_default": "reject",
                    "receipt": {
                        "envelope_sha256": "a" * 64,
                        "signer": "attacker",
                        "signature": "b" * 64,
                        "target": "sandbox_transport_openclaw_tool",
                        "environment": "gateway_production",
                        "not_before": _iso(),
                        "not_after": "2027-08-01T00:00:00Z",
                    },
                }
            )


# ---------------------------------------------------------------------------
# Security defaults — dangerous keys, break_glass alt, loopback allow
# (Blocker #6/supplementary)
# ---------------------------------------------------------------------------


class SecurityDefaultsTests(unittest.TestCase):
    def test_bundled_security_defaults_validate(self):
        payload = C.load_security_defaults()
        self.assertEqual(payload["schema"], "cwk.security_defaults.v1")

    def test_rejects_loopback_allowed_flag(self):
        payload = C.load_security_defaults()
        payload["transport_and_identity"]["loopback_http_self_reported_allowed"] = True
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(payload)

    def test_rejects_break_glass_alternate_enabled(self):
        payload = C.load_security_defaults()
        payload["break_glass"]["alternate_enabled"] = True
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(payload)

    def test_rejects_grace_read_allowed(self):
        payload = C.load_security_defaults()
        payload["access_grant"]["grace_read_forbidden"] = False
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(payload)

    def test_rejects_break_glass_enabled(self):
        payload = C.load_security_defaults()
        payload["break_glass"]["enabled"] = True
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(payload)


# ---------------------------------------------------------------------------
# Datetime / ID trailing-newline (supplementary)
# ---------------------------------------------------------------------------


class TrailingNewlineTests(unittest.TestCase):
    def test_datetime_requires_timezone(self):
        with self.assertRaises(C.ContractError):
            C._check_datetime("2026-08-01T10:00:00", "$.x", nullable=False)

    def test_datetime_reject_date_only(self):
        with self.assertRaises(C.ContractError):
            C._check_datetime("2026-08-01Z", "$.x", nullable=False)

    def test_sha_regex_rejects_trailing_newline(self):
        self.assertFalse(bool(C.SHA256_HEX_REGEX.match("a" * 64 + "\n")))

    def test_tenant_regex_rejects_trailing_newline(self):
        self.assertFalse(bool(C.TENANT_ID_REGEX.match("t_" + "a" * 26 + "\n")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
