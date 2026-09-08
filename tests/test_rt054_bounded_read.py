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

    def test_integrity_basis_matrix_requires_both_trusted_length_and_version_or_matching_sha(self):
        payload = b"abcdef"
        digest = hashlib.sha256(payload).hexdigest()
        for length, version, expected, result in (
            (None, None, None, "bounded_read_unavailable"),
            (len(payload), None, None, "bounded_read_unavailable"),
            (None, "v1", None, "bounded_read_unavailable"),
            (len(payload), "v1", None, "version_bound_length"),
            (None, None, digest, "expected_sha256"),
            (len(payload), None, digest, "expected_sha256"),
            (None, "v1", digest, "expected_sha256"),
            (len(payload), "v1", digest, "version_bound_length"),
        ):
            with self.subTest(length=length, version=version, expected=expected is not None):
                raw = Raw(payload, length=length, version=version)
                backend = self.filestation(lambda _req, **_kw: raw)
                if result == "bounded_read_unavailable":
                    with self.assertRaisesRegex(storage.BoundedReadError, result):
                        self.call(backend, expected_sha256=expected)
                else:
                    receipt, copied = self.call(backend, expected_sha256=expected)
                    self.assertEqual(copied, payload)
                    self.assertEqual(receipt.integrity_basis, result)

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

    def test_total_six_attempts_including_login_reopens_each_time_and_blocks_a_seventh_download(self):
        calls, raws = [], []
        def opener(_req, **_kw):
            calls.append(1)
            if len(calls) < 5:
                raise storage.TransientStorageError("private detail")
            raw = Raw(b"ok")
            raws.append(raw)
            return raw
        receipt, copied = self.call(self.filestation(opener))
        self.assertEqual(copied, b"ok")
        self.assertEqual(len(calls), 5)
        self.assertEqual(receipt.transport_attempts["login"], {"success": 1, "error": 0})
        self.assertEqual(receipt.transport_attempts["download"], {"success": 1, "error": 4})
        self.assertEqual(sum(sum(v.values()) for v in receipt.transport_attempts.values()), 6)

        login_calls, download_calls = [], []
        backend = self.filestation(lambda _req, **_kw: download_calls.append(1))
        def login(_request, **_kwargs):
            login_calls.append(1)
            if len(login_calls) <= 5:
                raise storage.TransientStorageError("private detail")
            return b'{"success":true,"data":{"sid":"fake"}}'
        backend._transport = login
        with self.assertRaisesRegex(storage.BoundedReadError, "transient_exhausted"):
            self.call(backend)
        self.assertEqual(len(login_calls), 6)
        self.assertEqual(download_calls, [])

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
                    self.call(self.filestation(lambda _req, **_kw: raw), max_bytes=len(body) + 1, chunk_size=1)
                self.assertTrue(all(size <= 1 for size in raw.read_sizes) if body.startswith(b"{") else True)

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

    def test_cancel_callback_exception_is_redacted_and_closes_open_stream(self):
        raw = Raw(b"ok")
        backend = self.filestation(lambda _req, **_kw: raw)
        with self.assertRaisesRegex(storage.BoundedReadError, "cancelled") as raised:
            self.call(backend, cancel=lambda: (_ for _ in ()).throw(RuntimeError("secret cancel")))
        self.assertNotIn("secret", str(raised.exception))
        # The callback ran before any object transport; no partial receipt or
        # fallback is possible.
        self.assertFalse(raw.closed)
        self.assertEqual(backend.legacy_downloads, 0)

        # A cancellation that arrives after the first transient control call
        # prevents a second login request and remains redacted.
        login_calls, cancelled = [], [False]
        backend = self.filestation(lambda _req, **_kw: (_ for _ in ()).throw(AssertionError("download opened")))
        def login(_request, **_kwargs):
            login_calls.append(1)
            cancelled[0] = True
            raise storage.TransientStorageError("private detail")
        backend._transport = login
        with self.assertRaisesRegex(storage.BoundedReadError, "cancelled") as raised:
            self.call(backend, cancel=lambda: cancelled[0])
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(len(login_calls), 1)

    def test_cancel_after_delivery_closes_raw_stream_without_partial_receipt(self):
        raw, cancelled = Raw(b"abcdef"), [False]
        def deliver(_chunk): cancelled[0] = True
        with self.assertRaisesRegex(storage.BoundedReadError, "cancelled"):
            self.call(self.filestation(lambda _req, **_kw: raw), cancel=lambda: cancelled[0], on_chunk=deliver)
        self.assertTrue(raw.closed)

    def test_default_unpinned_streaming_adapter_reads_and_closes_response(self):
        class Response(Raw):
            status = 200
            def getheader(self, name):
                return {"Content-Length": "2", "ETag": "v1"}.get(name)
        response = Response(b"ok")
        login = Response(b'{"success":true,"data":{"sid":"fake"}}')
        backend = self.filestation(lambda _req, **_kw: response)
        backend._uses_default_transport = True
        backend._streaming_transport = backend._https_streaming_transport
        original = storage.urllib.request.urlopen
        responses = [login, response]
        storage.urllib.request.urlopen = lambda _request, **kwargs: responses.pop(0)
        try:
            receipt, copied = self.call(backend)
        finally:
            storage.urllib.request.urlopen = original
        self.assertEqual(copied, b"ok")
        self.assertEqual(receipt.payload_bytes, 2)
        self.assertTrue(login.closed)
        self.assertTrue(response.closed)

    def test_default_pinned_streaming_adapter_uses_verified_same_connection_and_closes(self):
        class Response(Raw):
            status = 200
            def getheader(self, name):
                return {"Content-Length": "2", "ETag": "v1"}.get(name)
        class Connection:
            pin_verified = True
            def __init__(self): self.requests, self.closed = [], False
            def request(self, *args, **kwargs): self.requests.append((args, kwargs))
            def getresponse(self): return response
            def close(self): self.closed = True
        response, login = Response(b"ok"), Response(b'{"success":true,"data":{"sid":"fake"}}')
        backend = self.filestation(lambda _req, **_kw: response, pin="a" * 64)
        backend._uses_default_transport = True
        backend._streaming_transport = backend._https_streaming_transport
        connections = []
        def make_connection(*_args):
            connection = Connection()
            selected = login if not connections else response
            connection.getresponse = lambda: selected
            connections.append(connection)
            return connection
        backend._pinned_connection_with_timeout = make_connection
        receipt, copied = self.call(backend)
        self.assertEqual(copied, b"ok")
        self.assertEqual(receipt.payload_bytes, 2)
        self.assertEqual(len(connections), 2)
        self.assertTrue(all(connection.requests and connection.closed for connection in connections))
        self.assertTrue(login.closed)
        self.assertTrue(response.closed)

    def test_pinned_raw_retry_reverifies_new_socket_and_closes_every_connection(self):
        class Response(Raw):
            status = 200
            def getheader(self, name):
                return {"Content-Length": "2", "ETag": "v1"}.get(name)
        class BreakBeforeDelivery(Response):
            def read(self, _size): raise storage.TransientStorageError("private")
        class Connection:
            pin_verified = True
            def __init__(self, response): self.response, self.closed, self.requests = response, False, []
            def request(self, *args, **kwargs): self.requests.append((args, kwargs))
            def getresponse(self): return self.response
            def close(self): self.closed = True
        login = Response(b'{"success":true,"data":{"sid":"fake"}}')
        responses = [login, BreakBeforeDelivery(b"xx"), Response(b"ok")]
        connections = []
        backend = self.filestation(lambda _req, **_kw: responses[0], pin="a" * 64)
        backend._uses_default_transport = True
        backend._streaming_transport = backend._https_streaming_transport
        def make_connection(*_args):
            connection = Connection(responses.pop(0))
            connections.append(connection)
            return connection
        backend._pinned_connection_with_timeout = make_connection
        receipt, copied = self.call(backend)
        self.assertEqual(copied, b"ok")
        self.assertEqual(receipt.transport_attempts["download"], {"success": 1, "error": 1})
        self.assertEqual(len(connections), 3)
        self.assertTrue(all(connection.pin_verified and connection.closed for connection in connections))
        self.assertTrue(all(connection.requests for connection in connections))

    def test_three_backend_write_traps_cover_success_failure_and_cancel(self):
        for backend_name in ("memory", "localfs", "filestation"):
            for kwargs, expected in (({}, None), ({"max_bytes": 1}, "capacity_exceeded"), ({"cancel": lambda: True}, "cancelled")):
                with tempfile.TemporaryDirectory() as tmp:
                    if backend_name == "memory":
                        backend = storage.MemoryBackend(); backend.write("raw/object", b"ok")
                    elif backend_name == "localfs":
                        backend = storage.LocalFSBackend(tmp); backend.write("raw/object", b"ok")
                    else:
                        backend = self.filestation(lambda _req, **_kw: Raw(b"ok"))
                    original = backend.write
                    backend.write = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("write trap"))
                    with self.subTest(backend=backend.name, expected=expected):
                        if expected:
                            with self.assertRaisesRegex(storage.BoundedReadError, expected): self.call(backend, **kwargs)
                        else:
                            receipt, copied = self.call(backend, **kwargs)
                            self.assertEqual(copied, b"ok")
                            self.assertEqual(receipt.payload_bytes, 2)
                    backend.write = original

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
            def legacy(self, request, **_kwargs):
                if request.full_url.endswith("/auth.cgi"):
                    return b'{"success":true,"data":{"sid":"fake"}}'
                self.legacy_downloads += 1
                raise AssertionError("bounded read used legacy bytes transport")
        return Backend()


if __name__ == "__main__":
    unittest.main()
