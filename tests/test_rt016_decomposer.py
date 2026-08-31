"""RT-016 LegacyRawDecomposer behaviour tests.

Every test targets one behaviour of the decomposer only.  No
InstanceLayout, no writes to disk, no RT-014 / RT-015 side effects.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    C,
    R16,
    sample_frontmatter,
    sample_raw,
    utc_iso,
)


TENANT_ID = "t_" + "a" * 26
RUN_STARTED_AT = "2026-08-19T10:00:00Z"


def _fresh_decomposer() -> R16.LegacyRawDecomposer:
    return R16.LegacyRawDecomposer()


class HappyPathTests(unittest.TestCase):
    def test_produces_full_tuple(self):
        raw = sample_raw()
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "ok")
        self.assertIsNotNone(r.canonical_envelope)
        self.assertIsNotNone(r.tenant_view_envelope)
        self.assertIsNotNone(r.access_observation)
        # Canonical envelope passes RT-011 validator.
        C.validate_canonical_envelope(r.canonical_envelope)
        C.validate_tenant_view(r.tenant_view_envelope)
        C.validate_access_observation(r.access_observation)
        # Observation constraints per RT-016 contract.
        self.assertEqual(r.access_observation["initial_status"], "granted")
        self.assertEqual(
            r.access_observation["observation_source"], "legacy_raw_decomposition"
        )
        # decompose_report shape.
        self.assertEqual(r.decompose_report["decomposition_status"], "ok")
        self.assertEqual(r.decompose_report["quarantine_reasons"], [])
        self.assertRegex(
            r.decompose_report["canonical_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_three_hashes_are_distinct(self):
        raw = sample_raw()
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "ok")
        legacy_source = r.decompose_report["legacy_source_sha256"]
        canonical = r.canonical_envelope["canonical_sha256"]
        object_bytes = r.decompose_report["object_bytes_sha256"]
        # By construction all three are distinct semantic identifiers:
        # legacy_source is the exact raw file bytes;
        # canonical_sha256 is SHA over the envelope *minus* its own
        #   canonical_sha256 field (RT-011 recipe);
        # object_bytes_sha256 is SHA over the RT-014 catalog storage bytes
        #   (the full envelope including canonical_sha256).
        self.assertNotEqual(legacy_source, canonical)
        self.assertNotEqual(canonical, object_bytes)
        self.assertNotEqual(legacy_source, object_bytes)

    def test_tenant_view_never_contains_bodies(self):
        raw = sample_raw(
            replies=[{"replyId": "r1", "content": "sensitive text", "replyEmpName": "Alice"}]
        )
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        view = r.tenant_view_envelope
        self.assertIn("reply_overlay", view)
        for item in view["reply_overlay"]:
            self.assertNotIn("content", item)
            self.assertNotIn("body", item)
            self.assertNotIn("text", item)
            self.assertIn("reply_id", item)

    def test_frontmatter_scope_reject_out_of_scope_keys_are_recorded(self):
        # unknown key does not blow up decomposition; it is surfaced in
        # decompose_report.unknown_frontmatter_keys.
        raw = sample_raw(
            extra_frontmatter=['custom_key: "value"'],
        )
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "ok")
        self.assertIn(
            "custom_key", r.decompose_report["unknown_frontmatter_keys"]
        )


class OverlayEquivalenceTests(unittest.TestCase):
    def test_two_legacy_files_with_same_body_produce_same_canonical(self):
        raw_a = sample_raw(source_lane="inbox")
        raw_b = sample_raw(source_lane="outbox")
        d = _fresh_decomposer()
        r_a = d.decompose(
            raw_bytes=raw_a,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        r_b = d.decompose(
            raw_bytes=raw_b,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r_a.status, "ok")
        self.assertEqual(r_b.status, "ok")
        # Same body / author / title / timestamps yields same canonical.
        self.assertEqual(
            r_a.canonical_envelope["canonical_sha256"],
            r_b.canonical_envelope["canonical_sha256"],
        )
        # Legacy source sha256 differs (frontmatter changed).
        self.assertNotEqual(
            r_a.decompose_report["legacy_source_sha256"],
            r_b.decompose_report["legacy_source_sha256"],
        )
        # Overlay lane differs.
        self.assertEqual(r_a.tenant_view_envelope.get("lane"), "inbox")
        self.assertEqual(r_b.tenant_view_envelope.get("lane"), "outbox")

    def test_body_change_produces_new_canonical(self):
        raw_v1 = sample_raw(body="first draft")
        raw_v2 = sample_raw(body="second draft")
        d = _fresh_decomposer()
        r_v1 = d.decompose(
            raw_bytes=raw_v1,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        r_v2 = d.decompose(
            raw_bytes=raw_v2,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r_v1.status, "ok")
        self.assertEqual(r_v2.status, "ok")
        self.assertNotEqual(
            r_v1.canonical_envelope["canonical_sha256"],
            r_v2.canonical_envelope["canonical_sha256"],
        )


class QuarantineTests(unittest.TestCase):
    def test_missing_body_section(self):
        raw = sample_raw(replace_body_header="## Wrong Header")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("missing_body_section", r.quarantine_reasons)
        self.assertIsNone(r.canonical_envelope)
        self.assertIsNone(r.tenant_view_envelope)
        self.assertIsNone(r.access_observation)

    def test_missing_report_id(self):
        raw = sample_raw(omit_frontmatter=("report_id",))
        # The row metadata still has reportId, so this specific case
        # falls back and should succeed.  For a strict "missing_report_id"
        # we need to also remove it from row metadata.  Use row_extra to
        # replace with an entirely blank row_id? Actually the fallback
        # uses row.reportId; craft an override:
        raw2 = sample_raw(omit_frontmatter=("report_id",), row_extra={"reportId": ""})
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw2,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("missing_report_id", r.quarantine_reasons)

    def test_missing_author(self):
        raw = sample_raw(writer_id="", row_extra={"writeEmpId": "", "source_user_id": ""})
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("missing_author", r.quarantine_reasons)

    def test_missing_created_at(self):
        raw = sample_raw(
            omit_frontmatter=("create_time",),
            row_extra={"createTime": ""},
        )
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("missing_created_at", r.quarantine_reasons)

    def test_ambiguous_timezone(self):
        raw = sample_raw(create_time="2024-06-15 10:30:00")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("ambiguous_timezone", r.quarantine_reasons)

    def test_unparseable_timestamp(self):
        raw = sample_raw(create_time="never")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("unparseable_timestamp", r.quarantine_reasons)

    def test_oversize_body_quarantines_without_truncation(self):
        # Body > 1 MiB.
        body = "A" * (1_048_576 + 128)
        raw = sample_raw(body=body)
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("oversize_body", r.quarantine_reasons)
        self.assertTrue(r.decompose_report["body_truncation_would_occur"])

    def test_empty_body(self):
        raw = sample_raw(body="")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("empty_body", r.quarantine_reasons)

    def test_malformed_frontmatter(self):
        raw = b"---\nreport_id 2070001\n---\n## Original Full Content For AI\n\nbody\n"
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("malformed_frontmatter", r.quarantine_reasons)

    def test_control_chars_rejected_in_title(self):
        raw = sample_raw(title="hello\x00world")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        # Frontmatter rejects control chars via _MalformedFrontmatter →
        # quarantine.
        self.assertEqual(r.status, "quarantined")

    def test_author_source_user_id_invalid(self):
        raw = sample_raw(writer_id="not a valid id!!")
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("author_source_user_id_invalid", r.quarantine_reasons)


class TimelineCoverageTests(unittest.TestCase):
    def test_timeline_event_coverage_ok_when_all_referenced(self):
        replies = [{"replyId": "r1", "content": "hi"}]
        raw = sample_raw(replies=replies)
        # Compute the canonical bytes of the reply the way the
        # decomposer will (NFC + JCS).  Pass those as timeline_event_bytes.
        import cwk_pr001_contracts as _C
        reply_bytes = _C.canonical_json_bytes(_C.nfc_normalize(replies[0]))
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
            timeline_event_bytes=[reply_bytes],
        )
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.decompose_report["timeline_event_hash_check"]["coverage_ok"])

    def test_timeline_event_hash_mismatch_quarantines(self):
        replies = [{"replyId": "r1", "content": "hi"}]
        raw = sample_raw(replies=replies)
        # Pass unrelated timeline events → coverage fails.
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
            timeline_event_bytes=[b'{"replyId": "other"}'],
        )
        self.assertEqual(r.status, "quarantined")
        self.assertIn("timeline_event_hash_mismatch", r.quarantine_reasons)

    def test_timeline_none_means_no_coverage_claim(self):
        raw = sample_raw(replies=[{"replyId": "r1", "content": "hi"}])
        d = _fresh_decomposer()
        r = d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
            timeline_event_bytes=None,
        )
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.decompose_report["timeline_event_hash_check"]["coverage_ok"])


class InputValidationTests(unittest.TestCase):
    def test_raw_bytes_type_check(self):
        d = _fresh_decomposer()
        with self.assertRaises(R16.LegacyImportError):
            d.decompose(
                raw_bytes="not bytes",  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                source_namespace="cwork",
                run_started_at=RUN_STARTED_AT,
            )

    def test_tenant_id_validated(self):
        import cwk_instance as _I
        d = _fresh_decomposer()
        with self.assertRaises(_I.TenantIdError):
            d.decompose(
                raw_bytes=sample_raw(),
                tenant_id="bad",
                source_namespace="cwork",
                run_started_at=RUN_STARTED_AT,
            )

    def test_source_namespace_validated(self):
        d = _fresh_decomposer()
        with self.assertRaises(R16.LegacyImportError):
            d.decompose(
                raw_bytes=sample_raw(),
                tenant_id=TENANT_ID,
                source_namespace="BAD-NS",
                run_started_at=RUN_STARTED_AT,
            )

    def test_run_started_at_must_be_iso_utc(self):
        d = _fresh_decomposer()
        with self.assertRaises(R16.LegacyImportError):
            d.decompose(
                raw_bytes=sample_raw(),
                tenant_id=TENANT_ID,
                source_namespace="cwork",
                run_started_at="not a date",
            )

    def test_source_kind_enum(self):
        d = _fresh_decomposer()
        with self.assertRaises(R16.LegacyImportError):
            d.decompose(
                raw_bytes=sample_raw(),
                tenant_id=TENANT_ID,
                source_namespace="cwork",
                run_started_at=RUN_STARTED_AT,
                source_kind="bogus",
            )


class ImmutabilityTests(unittest.TestCase):
    def test_decomposer_never_mutates_input(self):
        raw = sample_raw()
        original = bytes(raw)
        d = _fresh_decomposer()
        d.decompose(
            raw_bytes=raw,
            tenant_id=TENANT_ID,
            source_namespace="cwork",
            run_started_at=RUN_STARTED_AT,
        )
        self.assertEqual(raw, original)


if __name__ == "__main__":
    unittest.main()
