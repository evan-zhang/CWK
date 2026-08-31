"""VG-A §4: view is overlay-only; no body / credential / temp URL / host
path / full reply-node payload.

Scenario 4 (§8 of PR-001 plan): every field that could carry real
tenant / credential / raw material is banished from tenant view / grant
record / cleanup outbox / tombstone / receipt files on disk.  Only
opaque IDs (grant_key, view_key, canonical_sha256) plus overlay hashes
may appear.  Temp URLs are permitted ONLY inside
``attachment_permissions[].temporary_url`` — never in any other
artefact.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


_FORBIDDEN_IN_NON_OVERLAY = (
    b"CWORK_APP_KEY",
    b"AppKey",
    b"app_secret",
    b"credential_ref",
    b"presign.vga.example",  # our fake temp URL host
    b"synthesised-fake-token",
)


class OverlayNoLeakTests(H.VgaTestBase):
    def test_view_disk_bytes_do_not_contain_canonical_body(self) -> None:
        env = H.canonical_envelope(body="the-vga-canonical-body-should-not-leak")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id,
                canonical_sha256=env["canonical_sha256"],
                include_attachment_temp_url=False,
            )
        views_dir = self.fx.root / "tenants" / self.fx.a_id / "views"
        for path in views_dir.iterdir():
            data = path.read_bytes()
            self.assertNotIn(
                b"the-vga-canonical-body-should-not-leak", data,
                f"canonical body leaked into {path}"
            )

    def test_grant_record_bytes_do_not_contain_canonical_body(self) -> None:
        env = H.canonical_envelope(body="grant-view-forbidden-body")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        grant_dir = (
            self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "grants"
        )
        for path in grant_dir.iterdir():
            data = path.read_bytes()
            self.assertNotIn(b"grant-view-forbidden-body", data)

    def test_view_upsert_rejects_body_field(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            snap = self.fx.snapshot(self.fx.a_id)
            view = H.view_envelope(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            view["body"] = "smuggled body"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_view_upsert_rejects_reply_full_payload(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            snap = self.fx.snapshot(self.fx.a_id)
            view = H.view_envelope(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            view["reply_overlay"][0]["body"] = "smuggled reply body"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_view_upsert_rejects_node_full_payload(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            snap = self.fx.snapshot(self.fx.a_id)
            view = H.view_envelope(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            view["node_overlay"][0]["text"] = "smuggled node text"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_view_upsert_rejects_credential_field(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            snap = self.fx.snapshot(self.fx.a_id)
            view = H.view_envelope(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            view["credential_ref"] = "secret://leak"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_view_upsert_rejects_absolute_path_field(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            snap = self.fx.snapshot(self.fx.a_id)
            view = H.view_envelope(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            view["path"] = "/Users/attacker/leak"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_temp_url_only_in_attachment_permissions(self) -> None:
        """Attachment temp URL is preserved in the tenant overlay, but
        does not leak into any grant/tombstone/receipt/canonical file.
        """

        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id,
                canonical_sha256=env["canonical_sha256"],
                include_attachment_temp_url=True,
            )
        snap = self.fx.snapshot(self.fx.a_id)
        read = self.fx.view_store.read_view(
            snapshot=snap, source_namespace="cwork", report_id="3080001"
        )
        att = read.view["attachment_permissions"][0]
        self.assertTrue(att["temporary_url"].startswith("https://"))
        # Verify no non-view directory contains the fake presign token.
        for path in self.fx.root.rglob("*"):
            if not path.is_file():
                continue
            parts = path.parts
            if "views" in parts:
                continue
            data = path.read_bytes()
            self.assertNotIn(
                b"synthesised-fake-token", data,
                f"temp URL token leaked into {path}"
            )
            self.assertNotIn(
                b"presign.vga.example", data,
                f"temp URL host leaked into {path}"
            )

    def test_canonical_object_has_no_temp_url_or_credential_or_body_leak(self) -> None:
        env = H.canonical_envelope(body="a-body-that-should-not-become-a-credential")
        self.fx.publish(env)
        object_dir = self.fx.root / "shared" / "objects"
        object_files = [p for p in object_dir.rglob("*.json") if not p.name.startswith(".cwk-tmp-")]
        for path in object_files:
            data = path.read_bytes()
            for token in (b"credential_ref", b"app_key", b"CWORK_APP_KEY",
                          b"presign.vga.example", b"synthesised-fake-token",
                          b"reply_body", b"node_body"):
                self.assertNotIn(token, data)

    def test_no_credential_field_written_anywhere_by_normal_flow(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
        for path in self.fx.root.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            for token in (b"CWORK_APP_KEY", b"AppKey", b"app_secret"):
                self.assertNotIn(token, data, f"{token!r} present in {path}")

    def test_view_references_grant_key_not_report_id_in_filename(self) -> None:
        env = H.canonical_envelope(report_id="3080123")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(
                tenant_id=self.fx.a_id,
                signer=auth,
                report_id="3080123",
            )
            self.fx.upsert_view(
                tenant_id=self.fx.a_id,
                canonical_sha256=env["canonical_sha256"],
                report_id="3080123",
            )
        views_dir = self.fx.root / "tenants" / self.fx.a_id / "views"
        for path in views_dir.iterdir():
            self.assertNotIn("3080123", path.name)
            self.assertRegex(path.name, r"^g_[a-z2-7]{26}\.json$")


if __name__ == "__main__":
    unittest.main()
