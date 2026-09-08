"""RT-054 bounded-read contract: local-only fakes, no NAS or credentials."""

from __future__ import annotations

import hashlib
import io
import inspect
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_storage as storage  # noqa: E402
import kb_gateway  # noqa: E402
import kb_lexical_builder  # noqa: E402
import kb_ingest  # noqa: E402


class Raw:
    def __init__(self, data=b"payload", *, length="auto", version="v1", pin=True):
        self.stream = io.BytesIO(data)
        self.content_length = len(data) if length == "auto" else length
        self.object_version = version
        self.tls_verified = pin
        self.closed = False
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self):
        self.closed = True


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.deadline = time.monotonic() + 5

    def call(self, backend, **kwargs):
        out = []
        receipt = backend.read_bounded(
            "raw/object", max_bytes=kwargs.pop("max_bytes", 64),
            chunk_size=kwargs.pop("chunk_size", 3), deadline=kwargs.pop("deadline", self.deadline),
            cancel=kwargs.pop("cancel", lambda: False), expected_sha256=kwargs.pop("expected_sha256", None),
            on_chunk=kwargs.pop("on_chunk", out.append), **kwargs,
        )
        return receipt, b"".join(out)

    def test_memory_and_localfs_are_callback_only_and_legacy_read_is_unchanged(self):
        payload = b"abcdef"
        memory = storage.MemoryBackend()
        memory.write("raw/object", payload)
        with tempfile.TemporaryDirectory() as tmp:
            local = storage.LocalFSBackend(tmp)
            local.write("raw/object", payload)
            for backend in (memory, local):
                receipt, copied = self.call(backend)
                self.assertEqual(copied, payload)
                self.assertEqual(receipt.payload_bytes, len(payload))
                self.assertEqual(receipt.integrity_basis, "version_bound_length")
                self.assertLessEqual(receipt.peak_in_memory_bytes, 3)
                self.assertEqual(backend.read("raw/object"), payload)

    def test_no_integrity_basis_is_refused_but_expected_sha_allows_stream(self):
        raw = Raw(b"abcdef", length=None, version=None)
        backend = self.filestation(lambda _req, **_kw: raw)
        with self.assertRaisesRegex(storage.BoundedReadError, "bounded_read_unavailable"):
            self.call(backend)
        raw = Raw(b"abcdef", length=None, version=None)
        backend = self.filestation(lambda _req, **_kw: raw)
        receipt, copied = self.call(backend, expected_sha256=hashlib.sha256(b"abcdef").hexdigest())
        self.assertEqual(copied, b"abcdef")
        self.assertEqual(receipt.integrity_basis, "expected_sha256")

    def test_sha_mismatch_truncation_and_growth_fail_without_receipt(self):
        for raw, expected, code, cap in (
            (Raw(b"abc", length=3), "0" * 64, "integrity_mismatch", 3),
            (Raw(b"abc", length=4), None, "incomplete_stream", 64),
            (Raw(b"abcd", length=3), None, "capacity_exceeded", 3),
        ):
            with self.subTest(code=code):
                backend = self.filestation(lambda _req, **_kw: raw)
                with self.assertRaisesRegex(storage.BoundedReadError, code):
                    self.call(backend, max_bytes=cap, expected_sha256=expected)
                self.assertTrue(raw.closed)
                self.assertEqual(backend.legacy_downloads, 0)

    def test_sixth_pre_delivery_transient_succeeds_and_reopens_each_time(self):
        calls, raws = [], []
        def opener(_req, **_kw):
            calls.append(1)
            if len(calls) < 6:
                raise storage.TransientStorageError("private detail")
            raw = Raw(b"ok")
            raws.append(raw)
            return raw
        receipt, copied = self.call(self.filestation(opener))
        self.assertEqual(copied, b"ok")
        self.assertEqual(len(calls), 6)
        self.assertEqual(receipt.transport_attempts["download"], {"success": 1, "error": 5})

    def test_after_first_chunk_transient_is_not_retried_or_spliced(self):
        class BreakAfterFirst(Raw):
            def read(self, size):
                if self.read_sizes:
                    raise storage.TransientStorageError("private")
                return super().read(size)
        calls = []
        def opener(_req, **_kw):
            calls.append(1)
            return BreakAfterFirst(b"abcdef")
        with self.assertRaisesRegex(storage.BoundedReadError, "transport_failed"):
            self.call(self.filestation(opener))
        self.assertEqual(len(calls), 1)

    def test_each_read_is_capped_at_remaining_plus_one(self):
        raw = Raw(b"abcd", length=3)
        backend = self.filestation(lambda _req, **_kw: raw)
        with self.assertRaisesRegex(storage.BoundedReadError, "capacity_exceeded"):
            self.call(backend, max_bytes=3, chunk_size=8)
        self.assertEqual(raw.read_sizes[0], 4)
        self.assertEqual(backend.legacy_downloads, 0)

    def test_deadline_cancel_and_consumer_failure_are_redacted_and_zero_write(self):
        backend = storage.MemoryBackend()
        backend.write("raw/object", b"abcdef")
        before = dict(backend.files)
        for kwargs, code in (
            ({"deadline": time.monotonic() - 1}, "deadline_exceeded"),
            ({"cancel": lambda: True}, "cancelled"),
            ({"on_chunk": lambda _chunk: (_ for _ in ()).throw(RuntimeError("secret"))}, "consumer_failed"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(storage.BoundedReadError, code):
                    self.call(backend, **kwargs)
                self.assertEqual(before, backend.files)

    def test_consumer_can_copy_but_receipt_does_not_claim_to_bound_it(self):
        backend = storage.MemoryBackend()
        backend.write("raw/object", b"abcdefgh")
        copied = []
        receipt, _ = self.call(backend, on_chunk=lambda chunk: copied.append(bytes(chunk)))
        self.assertEqual(b"".join(copied), b"abcdefgh")
        self.assertLessEqual(receipt.peak_in_memory_bytes, 3)
        self.assertFalse(hasattr(receipt, "consumer_bytes"))

    def test_filestation_brace_payloads_and_envelopes_fail_closed_with_limited_buffer(self):
        for body, code in (
            (b'{"success":false,"error":{"code":1}}', "filestation_error"),
            (b'{"ordinary":true}', "transport_failed"),
            (b"{" + b"x" * (storage.MAX_ERROR_ENVELOPE_BYTES + 1), "transport_failed"),
        ):
            with self.subTest(code=code):
                raw = Raw(body, length=len(body))
                with self.assertRaisesRegex(storage.BoundedReadError, code):
                    self.call(self.filestation(lambda _req, **_kw: raw), max_bytes=len(body) + 1)

    def test_pin_is_checked_on_every_retry_and_legacy_download_is_never_used(self):
        calls = []
        def opener(_req, **_kw):
            calls.append(1)
            return Raw(b"ok", pin=len(calls) == 1)
        backend = self.filestation(opener, pin="a" * 64)
        receipt, _ = self.call(backend)
        self.assertEqual(receipt.payload_bytes, 2)
        self.assertEqual(backend.legacy_downloads, 0)
        bad = self.filestation(lambda _req, **_kw: Raw(b"ok", pin=False), pin="a" * 64)
        with self.assertRaisesRegex(storage.BoundedReadError, "tls_verification_failed"):
            self.call(bad)

    def test_retry_rechecks_version_and_pin_before_any_second_delivery(self):
        class FirstFails(Raw):
            def read(self, _size):
                raise storage.TransientStorageError("private")
        raws = [FirstFails(b"old", version="v1"), Raw(b"new", version="v2")]
        def opener(_req, **_kw):
            return raws.pop(0)
        with self.assertRaisesRegex(storage.BoundedReadError, "integrity_mismatch"):
            self.call(self.filestation(opener))

        raws = [FirstFails(b"old", pin=True), Raw(b"new", pin=False)]
        def pinned_opener(_req, **_kw):
            return raws.pop(0)
        with self.assertRaisesRegex(storage.BoundedReadError, "tls_verification_failed"):
            self.call(self.filestation(pinned_opener, pin="a" * 64))

    def test_remaining_deadline_is_passed_to_stream_open_and_checked_after_blocking_read(self):
        captured = []
        class Blocking(Raw):
            def read(self, size):
                time.sleep(0.01)
                return super().read(size)
        raw = Blocking(b"ok")
        def opener(_req, **kwargs):
            captured.append(kwargs["timeout"])
            return raw
        with self.assertRaisesRegex(storage.BoundedReadError, "deadline_exceeded"):
            self.call(self.filestation(opener), deadline=time.monotonic() + 0.001)
        self.assertGreater(captured[0], 0)
        self.assertTrue(raw.closed)

    def test_p1b_routes_have_no_bounded_read_connection(self):
        # This is intentionally a static guard as none of these routes is in
        # scope to execute: connecting the API would itself be forbidden P1b.
        for module in (kb_gateway, kb_lexical_builder, kb_ingest):
            with self.subTest(module=module.__name__):
                self.assertNotIn("read_bounded", inspect.getsource(module))

    def filestation(self, opener, pin=None):
        class Backend(storage.FileStationBackend):
            def __init__(self):
                super().__init__(
                    storage.NasCredentials(host="fake.invalid", user="u", password="p", share="/kb"),
                    transport=self.legacy, streaming_transport=opener, cert_sha256=pin,
                    retry=storage.RetryPolicy(attempts=1, sleep=lambda _seconds: None),
                )
                self.legacy_downloads = 0
            def legacy(self, request):
                if request.full_url.endswith("/auth.cgi"):
                    return b'{"success":true,"data":{"sid":"fake"}}'
                self.legacy_downloads += 1
                raise AssertionError("bounded read used legacy bytes transport")
        return Backend()


if __name__ == "__main__":
    unittest.main()
