"""Wave-0 contract tests for the neutral PilotAdmissionProvider v1 ABI."""

from __future__ import annotations

import ast
import builtins
import copy
import datetime as dt
import inspect
import json
import os
import pathlib
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_agent_context as AC  # noqa: E402
import cwk_pilot_admission_api as PA  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402


TENANT = "t_" + "a" * 26
PURPOSE = "query_broker"
NOW = dt.datetime(2026, 8, 20, 0, 2, 30, tzinfo=dt.timezone.utc)
RUNTIME_PATH = PROJECT / "scripts" / "cwk_pilot_admission_api.py"
SCHEMA_PATH = (
    PROJECT
    / "PR"
    / "PR-001-multitenant-knowledge-spaces"
    / "contracts"
    / "cross_rt"
    / "schemas"
    / "pilot_admission_snapshot_v1.schema.json"
)

FIXED_JCS = (
    b'{"admission_policy_revision":1,'
    b'"admission_policy_sha256":"1111111111111111111111111111111111111111111111111111111111111111",'
    b'"admitted":true,"as_of":"2026-08-20T00:00:00Z",'
    b'"expires_at":"2026-08-20T00:05:00Z","purpose":"query_broker",'
    b'"schema":"cwk.pilot_admission_snapshot.v1",'
    b'"tenant_id":"t_aaaaaaaaaaaaaaaaaaaaaaaaaa"}'
)
FIXED_SHA = "f5d1f7b4269b71db7f50985d00b600c8a950eb7e09844bbbee99bbf8694f2528"


def _agent(*, tenant_id: str = TENANT) -> AC.AgentContextSnapshot:
    return AC.AgentContextSnapshot(
        agent_id_hash="2" * 64,
        tenant_id=tenant_id,
        tenant_auth_epoch=7,
        binding_epoch=5,
        binding_secret_epoch=3,
        tenant_status="pilot",
        resolved_at="2026-08-20T00:02:00Z",
    )


def _payload(*, recompute: bool = True, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": PA.PILOT_ADMISSION_SNAPSHOT_SCHEMA,
        "tenant_id": TENANT,
        "purpose": PURPOSE,
        "admitted": True,
        "admission_policy_revision": 1,
        "admission_policy_sha256": "1" * 64,
        "as_of": "2026-08-20T00:00:00Z",
        "expires_at": "2026-08-20T00:05:00Z",
    }
    payload.update(overrides)
    if recompute:
        payload["snapshot_sha256"] = PA.compute_pilot_admission_snapshot_sha256(payload)
    return payload


def _validate(payload: object, **overrides: object) -> PA.PilotAdmissionSnapshotV1:
    args: dict[str, object] = {
        "agent_snapshot": _agent(),
        "expected_purpose": PURPOSE,
        "now": NOW,
    }
    args.update(overrides)
    return PA.validate_pilot_admission_snapshot(payload, **args)  # type: ignore[arg-type]


class FrozenSurfaceTests(unittest.TestCase):
    def test_versions_purposes_domain_and_exact_fields(self):
        self.assertEqual(
            PA.PILOT_ADMISSION_PROVIDER_API_VERSION,
            "cwk.pilot_admission_provider.v1",
        )
        self.assertEqual(PA.PILOT_ADMISSION_SNAPSHOT_SCHEMA, "cwk.pilot_admission_snapshot.v1")
        self.assertEqual(
            PA.PILOT_ADMISSION_PURPOSES,
            ("collector_run", "profile_workflow", "query_broker"),
        )
        self.assertEqual(
            PA.PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN,
            b"cwk-pilot-admission-snapshot-v1\x00",
        )
        self.assertEqual(PA.PILOT_ADMISSION_FIXED_VECTOR_JCS, FIXED_JCS)
        self.assertEqual(PA.PILOT_ADMISSION_FIXED_VECTOR_SHA256, FIXED_SHA)
        self.assertEqual(PA.PILOT_ADMISSION_MAX_TTL_SECONDS, 300)
        self.assertEqual(
            PA.PILOT_ADMISSION_SNAPSHOT_FIELDS,
            tuple(field.name for field in fields(PA.PilotAdmissionSnapshotV1)),
        )
        self.assertEqual(len(PA.PILOT_ADMISSION_SNAPSHOT_FIELDS), 9)

    def test_protocol_has_constructor_bound_purpose_and_keyword_only_snapshot(self):
        self.assertTrue(inspect.isclass(PA.PilotAdmissionProviderV1))
        signature = inspect.signature(PA.PilotAdmissionProviderV1.snapshot)
        self.assertEqual(tuple(signature.parameters), ("self", "agent_snapshot"))
        self.assertEqual(
            signature.parameters["agent_snapshot"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        self.assertNotIn("purpose", signature.parameters)

        init_signature = inspect.signature(PA.NullPilotAdmissionProvider)
        self.assertEqual(tuple(init_signature.parameters), ("purpose",))
        self.assertEqual(
            init_signature.parameters["purpose"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_schema_is_closed_and_matches_runtime_surface(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["$id"], PA.PILOT_ADMISSION_SNAPSHOT_SCHEMA)
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(schema["unevaluatedProperties"], False)
        self.assertEqual(tuple(schema["required"]), PA.PILOT_ADMISSION_SNAPSHOT_FIELDS)
        self.assertEqual(tuple(schema["properties"]), PA.PILOT_ADMISSION_SNAPSHOT_FIELDS)
        self.assertEqual(schema["properties"]["purpose"]["enum"], list(PA.PILOT_ADMISSION_PURPOSES))
        self.assertEqual(schema["properties"]["admission_policy_revision"]["maximum"], 2**53 - 1)
        self.assertEqual(schema["contractVector"]["domain"].encode(), PA.PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN)
        self.assertEqual(schema["contractVector"]["jcsPreimage"].encode(), FIXED_JCS)
        self.assertEqual(schema["contractVector"]["snapshotSha256"], FIXED_SHA)


class FixedVectorTests(unittest.TestCase):
    def test_exact_preimage_and_hash(self):
        payload = _payload()
        preimage = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
        self.assertEqual(C.canonical_json_bytes(preimage), FIXED_JCS)
        self.assertEqual(PA.compute_pilot_admission_snapshot_sha256(preimage), FIXED_SHA)
        self.assertEqual(payload["snapshot_sha256"], FIXED_SHA)

    def test_hash_excludes_only_snapshot_sha256(self):
        complete = _payload()
        original = PA.compute_pilot_admission_snapshot_sha256(complete)
        complete["snapshot_sha256"] = "f" * 64
        self.assertEqual(PA.compute_pilot_admission_snapshot_sha256(complete), original)

        for removed in (
            "schema",
            "tenant_id",
            "purpose",
            "admitted",
            "admission_policy_revision",
            "admission_policy_sha256",
            "as_of",
            "expires_at",
        ):
            broken = _payload()
            del broken[removed]
            with self.assertRaises(PA.PilotAdmissionContractError, msg=removed):
                PA.compute_pilot_admission_snapshot_sha256(broken)

        extra = _payload()
        extra["hidden_exclusion"] = "bad"
        with self.assertRaises(PA.PilotAdmissionContractError):
            PA.compute_pilot_admission_snapshot_sha256(extra)

    def test_changing_each_hashed_field_changes_hash(self):
        base = _payload()
        base_hash = PA.compute_pilot_admission_snapshot_sha256(base)
        replacements: dict[str, object] = {
            "schema": "cwk.pilot_admission_snapshot.v2",
            "tenant_id": "t_" + "b" * 26,
            "purpose": "collector_run",
            "admitted": False,
            "admission_policy_revision": 2,
            "admission_policy_sha256": "2" * 64,
            "as_of": "2026-08-20T00:00:01Z",
            "expires_at": "2026-08-20T00:04:59Z",
        }
        for key, value in replacements.items():
            changed = dict(base)
            changed[key] = value
            self.assertNotEqual(
                PA.compute_pilot_admission_snapshot_sha256(changed),
                base_hash,
                key,
            )


class StructureAndTypeTests(unittest.TestCase):
    def test_valid_mapping_returns_exact_immutable_snapshot(self):
        result = _validate(_payload())
        self.assertIsInstance(result, PA.PilotAdmissionSnapshotV1)
        self.assertEqual(tuple(result.to_payload()), PA.PILOT_ADMISSION_SNAPSHOT_FIELDS)
        self.assertEqual(result.snapshot_sha256, FIXED_SHA)
        with self.assertRaises(FrozenInstanceError):
            result.admitted = False  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            result.extra = "forbidden"  # type: ignore[attr-defined]

    def test_missing_extra_and_non_mapping_rejected(self):
        for removed in PA.PILOT_ADMISSION_SNAPSHOT_FIELDS:
            broken = _payload()
            del broken[removed]
            with self.assertRaises(PA.PilotAdmissionContractError, msg=removed):
                _validate(broken)
        extra = _payload()
        extra["extra"] = 1
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(extra)
        for bad in (None, [], "snapshot", 1):
            with self.assertRaises(PA.PilotAdmissionContractError, msg=repr(bad)):
                _validate(bad)

    def test_schema_tenant_and_purpose_are_exact(self):
        cases = (
            ("schema", "cwk.pilot_admission_snapshot.v2", {}),
            ("tenant_id", "T_" + "a" * 26, {}),
            ("tenant_id", TENANT + "x", {}),
            ("tenant_id", "t_" + "b" * 26, {}),
            ("purpose", "scheduler_run", {}),
            ("purpose", "collector_run", {}),
        )
        for field, value, kwargs in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(PA.PilotAdmissionContractError):
                    _validate(_payload(**{field: value}), **kwargs)

        collector = _payload(purpose="collector_run")
        self.assertEqual(
            _validate(collector, expected_purpose="collector_run").purpose,
            "collector_run",
        )
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(_payload(), agent_snapshot=_agent(tenant_id="t_" + "b" * 26))

    def test_bool_and_number_type_quirks_fail_closed(self):
        for bad in (0, 1, "true", None, 1.0):
            with self.subTest(admitted=bad):
                with self.assertRaises(PA.PilotAdmissionContractError):
                    _validate(_payload(admitted=bad))
        for bad in (True, False, 0, -1, 1.0, "1", 2**53):
            with self.subTest(revision=bad):
                with self.assertRaises(PA.PilotAdmissionContractError):
                    _validate(_payload(admission_policy_revision=bad))

    def test_hash_fields_are_lowercase_exact_sha256(self):
        for field in ("admission_policy_sha256", "snapshot_sha256"):
            for bad in ("A" * 64, "a" * 63, "a" * 65, 7, None):
                with self.subTest(field=field, bad=bad):
                    payload = _payload(recompute=False)
                    payload["snapshot_sha256"] = FIXED_SHA
                    payload[field] = bad
                    if field == "admission_policy_sha256" and isinstance(bad, str):
                        payload["snapshot_sha256"] = PA.compute_pilot_admission_snapshot_sha256(payload)
                    with self.assertRaises(PA.PilotAdmissionContractError):
                        _validate(payload)

    def test_corrupt_integrity_hash_rejected(self):
        payload = _payload()
        payload["snapshot_sha256"] = "0" * 64
        with self.assertRaisesRegex(PA.PilotAdmissionContractError, "snapshot_sha256 mismatch"):
            _validate(payload)


class TimeContractTests(unittest.TestCase):
    def test_strict_rfc3339_utc_second_precision(self):
        bad_values = (
            "2026-08-20T00:00:00+00:00",
            "2026-08-20T00:00:00.000Z",
            "2026-08-20t00:00:00Z",
            "2026-08-20T00:00:00z",
            "2026-08-20T00:00:60Z",
            "2026-02-30T00:00:00Z",
            " 2026-08-20T00:00:00Z",
            123,
        )
        for field in ("as_of", "expires_at"):
            for bad in bad_values:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(PA.PilotAdmissionContractError):
                        _validate(_payload(**{field: bad}))

    def test_ttl_open_lower_bound_and_closed_300_second_upper_bound(self):
        boundary = _payload(
            as_of="2026-08-20T00:00:00Z",
            expires_at="2026-08-20T00:05:00Z",
        )
        self.assertEqual(_validate(boundary).expires_at, "2026-08-20T00:05:00Z")
        for expires_at in ("2026-08-20T00:00:00Z", "2026-08-20T00:05:01Z"):
            with self.subTest(expires_at=expires_at):
                with self.assertRaises(PA.PilotAdmissionContractError):
                    _validate(
                        _payload(
                            as_of="2026-08-20T00:00:00Z",
                            expires_at=expires_at,
                        )
                    )

    def test_as_of_is_inclusive_and_expiry_is_exclusive(self):
        payload = _payload(
            as_of="2026-08-20T00:02:30Z",
            expires_at="2026-08-20T00:05:00Z",
        )
        self.assertTrue(_validate(payload).admitted)
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(payload, now=dt.datetime(2026, 8, 20, 0, 2, 29, tzinfo=dt.timezone.utc))
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(payload, now=dt.datetime(2026, 8, 20, 0, 5, 0, tzinfo=dt.timezone.utc))

    def test_now_must_be_aware_zero_offset_utc(self):
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(_payload(), now=dt.datetime(2026, 8, 20, 0, 2, 30))
        offset = dt.timezone(dt.timedelta(hours=8))
        with self.assertRaises(PA.PilotAdmissionContractError):
            _validate(_payload(), now=dt.datetime(2026, 8, 20, 8, 2, 30, tzinfo=offset))


class DenialAndUnavailableTests(unittest.TestCase):
    def test_denied_snapshot_is_valid_evidence_but_never_authorizes(self):
        denied = _payload(admitted=False)
        self.assertFalse(_validate(denied).admitted)
        with self.assertRaises(PA.PilotAdmissionDenied) as caught:
            PA.require_pilot_admission(
                denied,
                agent_snapshot=_agent(),
                expected_purpose=PURPOSE,
                now=NOW,
            )
        self.assertEqual(caught.exception.code, "denied")
        self.assertEqual(str(caught.exception), "pilot admission denied")

    def test_admitted_snapshot_passes_require_helper(self):
        result = PA.require_pilot_admission(
            _payload(),
            agent_snapshot=_agent(),
            expected_purpose=PURPOSE,
            now=NOW,
        )
        self.assertTrue(result.admitted)

    def test_null_provider_is_purpose_bound_and_stably_unavailable(self):
        provider = PA.NullPilotAdmissionProvider(purpose=PURPOSE)
        self.assertIsInstance(provider, PA.PilotAdmissionProviderV1)
        self.assertEqual(provider.API_VERSION, PA.PILOT_ADMISSION_PROVIDER_API_VERSION)
        self.assertEqual(provider.purpose, PURPOSE)
        with self.assertRaises(FrozenInstanceError):
            provider.purpose = "collector_run"  # type: ignore[misc]
        with self.assertRaises(PA.PilotAdmissionUnavailable) as caught:
            provider.snapshot(agent_snapshot=_agent())
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(str(caught.exception), "pilot admission provider unavailable")
        with self.assertRaises(TypeError):
            provider.snapshot(_agent())  # type: ignore[misc]
        with self.assertRaises(TypeError):
            PA.NullPilotAdmissionProvider(PURPOSE)  # type: ignore[misc]

    def test_null_provider_rejects_bad_purpose_and_bad_agent_type(self):
        for purpose in ("", "scheduler_run", None, 1):
            with self.subTest(purpose=purpose):
                with self.assertRaises(PA.PilotAdmissionContractError):
                    PA.NullPilotAdmissionProvider(purpose=purpose)  # type: ignore[arg-type]
        provider = PA.NullPilotAdmissionProvider(purpose=PURPOSE)
        with self.assertRaises(PA.PilotAdmissionContractError):
            provider.snapshot(agent_snapshot=object())  # type: ignore[arg-type]


class NoAmbientAuthorityTests(unittest.TestCase):
    def test_runtime_source_has_no_filesystem_environment_or_cwd_surface(self):
        tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"), filename=str(RUNTIME_PATH))
        imported_roots: set[str] = set()
        called_names: set[str] = set()
        called_attrs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_attrs.add(node.func.attr)
        self.assertTrue({"os", "pathlib", "subprocess", "socket"}.isdisjoint(imported_roots))
        self.assertTrue({"open", "getcwd", "getenv", "read_text", "read_bytes"}.isdisjoint(called_names))
        self.assertTrue({"open", "getcwd", "getenv", "read_text", "read_bytes"}.isdisjoint(called_attrs))

    def test_hash_validation_denial_and_null_provider_do_not_read_ambient_state(self):
        denied = _payload(admitted=False)
        provider = PA.NullPilotAdmissionProvider(purpose=PURPOSE)
        with (
            mock.patch.object(builtins, "open", side_effect=AssertionError("filesystem read")),
            mock.patch.object(pathlib.Path, "open", side_effect=AssertionError("path read")),
            mock.patch.object(os, "getenv", side_effect=AssertionError("environment read")),
            mock.patch.object(os, "getcwd", side_effect=AssertionError("cwd read")),
        ):
            self.assertEqual(PA.compute_pilot_admission_snapshot_sha256(denied), denied["snapshot_sha256"])
            self.assertFalse(_validate(denied).admitted)
            with self.assertRaises(PA.PilotAdmissionDenied):
                PA.require_pilot_admission(
                    denied,
                    agent_snapshot=_agent(),
                    expected_purpose=PURPOSE,
                    now=NOW,
                )
            with self.assertRaises(PA.PilotAdmissionUnavailable):
                provider.snapshot(agent_snapshot=_agent())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
