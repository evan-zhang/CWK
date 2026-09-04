"""RT-042 storage-layer criteria: J1 (dual backend), J2 (anti-idle), J3 (path
escape), J7 (real-NAS smoke, skipped without credentials).

Everything except J7 runs offline against fakes: no network, no credentials,
no NAS.  The FileStation backend is exercised through an injected transport
that returns recorded FileStation envelopes, so the request construction,
the retry policy and the idempotency tolerations are all covered without a
device.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import secrets
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_kb_storage as storage  # noqa: E402
from cwk_kb_create import KbSpec, SourceSpec, create_kb  # noqa: E402
from cwk_kb_ledger import (  # noqa: E402
    TIMESTAMP_CLASS_PATHS,
    WriteReconcileFailed,
    loads,
    record_write,
)

# A fixed spec: same kb_code, same created_at, so two builds differ only
# where wall-clock time legitimately enters (the timestamp-class files).
FIXED_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
FIXED_CODE = "0" * 32


def fixed_spec() -> KbSpec:
    return KbSpec(
        display_name="判据库",
        kb_code=FIXED_CODE,
        owner_ref="owner-42",
        created_at=FIXED_NOW,
        sources=(SourceSpec(source_type="cwork"),),
    )


# ── test doubles ────────────────────────────────────────────────────────────


class NoOpBackend:
    """The 「无操作桩」: accepts every write and stores nothing.

    J2's anti-idle criterion is defined against this object.  It is a test
    double on purpose — shipping a backend that silently discards writes
    would be a foot-gun in production code.
    """

    name = "noop"

    def mkdir(self, path: str) -> None:
        storage.normalize_path(path)

    def write(self, path: str, data: bytes) -> str:
        storage.normalize_path(path)
        return storage.sha256_bytes(data)

    def read(self, path: str) -> bytes:
        raise storage.NotFound(f"NoOpBackend 没有存任何东西：{path}")

    def exists(self, path: str) -> bool:
        return False

    def list_dir(self, path: str) -> List[str]:
        return []

    def walk_files(self, path: str = ".") -> List[str]:
        return []

    def sha256(self, path: str) -> str:
        raise storage.NotFound(path)

    def remove(self, path: str) -> None:
        return None

    def remove_dir(self, path: str) -> None:
        return None


class OneByteOffBackend(storage.MemoryBackend):
    """Writes everything faithfully except one target path, which is corrupted.

    This is J1's red method: if the comparison cannot see a single flipped
    byte, the comparison is not doing anything.
    """

    def __init__(self, victim: str) -> None:
        super().__init__()
        self.victim = victim

    def write(self, path: str, data: bytes) -> str:
        if storage.normalize_path(path) == self.victim:
            data = bytes(data)
            data = data[:-1] + bytes([data[-1] ^ 0x01])
        return super().write(path, data)


class FakeFileStation:
    """A recorded FileStation transport.

    Holds an in-memory file table and answers the same JSON envelopes the
    device does, so request construction is exercised end to end.  ``faults``
    lets a test make the first N calls fail transiently.
    """

    def __init__(self, *, faults: int = 0, fault_kind: str = "http500") -> None:
        self.files: Dict[str, bytes] = {}
        self.folders = {"/kb"}
        self.calls: List[str] = []
        self.faults = faults
        self.fault_kind = fault_kind
        self.login_count = 0

    def __call__(self, request: urllib.request.Request) -> bytes:
        if self.faults > 0:
            self.faults -= 1
            if self.fault_kind == "http500":
                raise storage.TransientStorageError("FileStation HTTP 503")
            raise storage.TransientStorageError("connection reset")

        url = request.full_url
        body = request.data or b""
        if "auth.cgi" in url:
            fields = self._form(body)
            if fields.get("method") == "logout":
                return b'{"success": true}'
            self.login_count += 1
            assert "passwd" not in url, "密码不得出现在 URL 里"
            return b'{"success": true, "data": {"sid": "fake-sid"}}'

        if request.get_header("Content-type", "").startswith("multipart/form-data"):
            return self._upload(request, body)

        if request.method == "GET":
            fields = dict(
                part.split("=", 1)
                for part in url.split("?", 1)[1].split("&")
                if "=" in part
            )
            return self._download(fields)

        return self._entry(self._form(body))

    @staticmethod
    def _form(body: bytes) -> Dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()}

    def _upload(self, request: urllib.request.Request, body: bytes) -> bytes:
        # DSM 7.x reads the session id from the query string on the upload
        # endpoint.  A sid that only appears in the multipart body is not a
        # session as far as the device is concerned: 119 Invalid session.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        if not query.get("_sid"):
            self.calls.append("upload REJECTED no _sid in query")
            return b'{"error": {"code": 119}}'
        boundary = request.get_header("Content-type").split("boundary=")[1]
        parts = body.split(f"--{boundary}".encode("utf-8"))
        fields: Dict[str, bytes] = {}
        filename = ""
        payload = b""
        for part in parts:
            if b'name="file"' in part:
                filename = part.split(b'filename="')[1].split(b'"')[0].decode("utf-8")
                payload = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
            elif b'name="' in part:
                name = part.split(b'name="')[1].split(b'"')[0].decode("utf-8")
                fields[name] = part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
        folder = fields["path"].decode("utf-8")
        self.calls.append(f"upload {folder}/{filename}")
        self.files[f"{folder}/{filename}"] = payload
        return b'{"success": true}'

    def _download(self, fields: Dict[str, str]) -> bytes:
        path = json.loads(urllib.parse.unquote_plus(fields["path"]))
        self.calls.append(f"download {path}")
        if path not in self.files:
            return b'{"success": false, "error": {"code": 408}}'
        return self.files[path]

    def _entry(self, fields: Dict[str, str]) -> bytes:
        api = fields.get("api")
        self.calls.append(f"{api}.{fields.get('method')}")
        if api == "SYNO.FileStation.CreateFolder":
            # folder_path is a JSON **array** on this endpoint; joining the raw
            # field would invent a folder literally named ``["/kb"]/wiki`` and
            # every later exists() would answer False without anyone noticing.
            parents = json.loads(fields["folder_path"])
            if isinstance(parents, str):
                parents = [parents]
            targets = [f"{str(parent).rstrip('/')}/{fields['name']}" for parent in parents]
            if any(target in self.folders for target in targets):
                return b'{"success": false, "error": {"code": 408}}'
            self.folders.update(targets)
            return b'{"success": true}'
        if api == "SYNO.FileStation.Delete":
            for path in json.loads(fields["path"]):
                self.files.pop(path, None)
            return b'{"success": true}'
        if api == "SYNO.FileStation.List" and fields.get("method") == "getinfo":
            paths = json.loads(fields["path"])
            found = [{"path": p} for p in paths if p in self.files or p in self.folders]
            return json.dumps({"success": True, "data": {"files": found}}).encode("utf-8")
        if api == "SYNO.FileStation.List" and fields.get("method") != "getinfo":
            # 真机 DSM 7.x：method=list 的 folder_path 走引号字符串形态
            folder = json.loads(fields["folder_path"]).rstrip("/")
            names = set()
            for path in list(self.files) + list(self.folders):
                if not path.startswith(folder + "/"):
                    continue
                rest = path[len(folder) + 1 :]
                head = rest.split("/")[0]
                names.add((head, "/" in rest or f"{folder}/{head}" in self.folders))
            files = [{"name": name, "isdir": isdir} for name, isdir in sorted(names)]
            return json.dumps({"success": True, "data": {"files": files}}).encode("utf-8")
        return b'{"success": true, "data": {}}'


def fake_nas(**kwargs) -> storage.FileStationBackend:
    transport = FakeFileStation(**kwargs)
    backend = storage.FileStationBackend(
        storage.NasCredentials(host="nas.test", user="svc", password="pw", share="/kb"),
        transport=transport,
        retry=storage.RetryPolicy(attempts=4, base_delay=0, sleep=lambda _: None),
    )
    backend.fake = transport  # type: ignore[attr-defined]
    return backend


# ── J3: path escape ─────────────────────────────────────────────────────────


class PathSafetyTests(unittest.TestCase):
    """J3 — ``../``, absolute paths and symlink escapes are refused."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "kb"
        self.backend = storage.LocalFSBackend(self.root)

    def test_traversal_and_absolute_paths_are_rejected(self) -> None:
        for bad in (
            "../escape.txt",
            "raw/../../escape.txt",
            "/etc/passwd",
            "~/secrets",
            "C:/windows",
            "raw\\win.txt",
            "",
            ".",
            "..",
            "a\x00b",
        ):
            with self.subTest(path=bad):
                with self.assertRaises(storage.UnsafePath):
                    storage.normalize_path(bad)

    def test_every_write_path_refuses_escapes(self) -> None:
        for bad in ("../escape.txt", "/etc/passwd", "raw/../../x"):
            for backend in (self.backend, storage.MemoryBackend()):
                with self.subTest(path=bad, backend=backend.name):
                    with self.assertRaises(storage.UnsafePath):
                        backend.write(bad, b"x")
                    with self.assertRaises(storage.UnsafePath):
                        backend.mkdir(bad)

    def test_read_and_exists_also_refuse_escapes(self) -> None:
        with self.assertRaises(storage.UnsafePath):
            self.backend.read("../../etc/passwd")
        with self.assertRaises(storage.UnsafePath):
            self.backend.exists("../outside")

    def test_symlink_escape_is_refused_even_though_every_segment_is_legal(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(storage.UnsafePath):
            self.backend.write("link/planted.txt", b"x")
        self.assertFalse((outside / "planted.txt").exists())

    def test_write_refuses_to_follow_a_symlinked_file(self) -> None:
        victim = Path(self.tmp.name) / "victim.txt"
        victim.write_bytes(b"original")
        (self.root / "inside.txt").symlink_to(victim)
        with self.assertRaises(storage.UnsafePath):
            self.backend.write("inside.txt", b"overwritten")
        self.assertEqual(victim.read_bytes(), b"original")

    def test_nas_remote_path_is_built_from_a_normalised_relative_path(self) -> None:
        backend = fake_nas()
        self.assertEqual(backend._remote("wiki/index.md"), "/kb/wiki/index.md")
        with self.assertRaises(storage.UnsafePath):
            backend._remote("../../etc/passwd")


# ── J1: dual-backend equivalence ────────────────────────────────────────────


class DualBackendEquivalenceTests(unittest.TestCase):
    """J1 — same operation sequence on two backends, zero byte differences.

    Excluded: the timestamp-class files, which get a structural assertion
    instead of a byte comparison.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build_pair(self):
        local = storage.LocalFSBackend(Path(self.tmp.name) / "local")
        memory = storage.MemoryBackend()
        create_kb(local, fixed_spec())
        create_kb(memory, fixed_spec())
        return local, memory

    def test_terminal_state_is_byte_identical_outside_the_timestamp_class(self) -> None:
        local, memory = self.build_pair()
        self.assertEqual(local.walk_files("."), memory.walk_files("."))
        compared = 0
        for path in local.walk_files("."):
            if path in TIMESTAMP_CLASS_PATHS:
                continue
            with self.subTest(path=path):
                self.assertEqual(
                    local.sha256(path), memory.sha256(path), f"{path} 两后端不一致"
                )
            compared += 1
        self.assertGreaterEqual(compared, 13, "比对面太小，判据会失去意义")

    def test_excluded_files_get_a_structural_equivalence_assertion(self) -> None:
        local, memory = self.build_pair()
        for path in TIMESTAMP_CLASS_PATHS:
            with self.subTest(path=path):
                left = local.read(path)
                right = memory.read(path)
                if path.endswith(".json"):
                    a, b = loads(left), loads(right)
                    self.assertEqual(sorted(a), sorted(b))
                    self.assertEqual(a["entry_count"], b["entry_count"])
                    self.assertEqual(sorted(a["entries"]), sorted(b["entries"]))
                elif path.endswith(".jsonl"):
                    a = [json.loads(line) for line in left.decode().splitlines()]
                    b = [json.loads(line) for line in right.decode().splitlines()]
                    self.assertEqual(len(a), len(b))
                    self.assertEqual([r["event"] for r in a], [r["event"] for r in b])
                else:
                    self.assertEqual(
                        len(left.decode().splitlines()), len(right.decode().splitlines())
                    )

    def test_red_method_one_flipped_byte_makes_the_diff_non_empty(self) -> None:
        """J1 红法：故意让一个后端写偏一个字节 → diff 必须非空。"""
        local = storage.LocalFSBackend(Path(self.tmp.name) / "red")
        create_kb(local, fixed_spec())
        corrupt = OneByteOffBackend("wiki/index.md")
        # record_write catches the corruption at the write itself, which is
        # the stronger failure; assert the reconciliation fires.
        with self.assertRaises(WriteReconcileFailed):
            create_kb(corrupt, fixed_spec())

    def test_red_method_corruption_is_visible_to_the_plain_file_diff_too(self) -> None:
        """Same flipped byte, seen by the sha256 comparison rather than the ledger."""
        left = storage.MemoryBackend()
        right = OneByteOffBackend("wiki/index.md")
        payload = b"# index\n"
        left.write("wiki/index.md", payload)
        storage.MemoryBackend.write(right, "wiki/index.md", payload)  # faithful copy
        self.assertEqual(left.sha256("wiki/index.md"), right.sha256("wiki/index.md"))
        right.write("wiki/index.md", payload)  # corrupting path
        diff = [
            path
            for path in left.walk_files(".")
            if left.sha256(path) != right.sha256(path)
        ]
        self.assertEqual(diff, ["wiki/index.md"])


# ── J2: anti-idle ───────────────────────────────────────────────────────────


class AntiIdleTests(unittest.TestCase):
    """J2 — swap in the no-op backend and every write path must go red."""

    def test_noop_backend_fails_the_write_reconciliation(self) -> None:
        with self.assertRaises(WriteReconcileFailed):
            record_write(NoOpBackend(), "kb.json", b"{}")

    def test_noop_backend_fails_the_whole_build(self) -> None:
        with self.assertRaises(WriteReconcileFailed):
            create_kb(NoOpBackend(), fixed_spec())

    def test_the_same_assertions_pass_on_a_real_backend(self) -> None:
        """The control arm: the criterion is red for NoOp, green for LocalFS.

        Without this half, "NoOp is red" would be satisfied by a test that is
        red for everything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Two roots: a build refuses a non-empty destination, so the
            # single-write arm cannot share a tree with the whole-build arm.
            single = storage.LocalFSBackend(Path(tmp) / "single")
            record_write(single, "kb.json", b"{}")
            self.assertTrue(single.exists("kb.json"))
            whole = storage.LocalFSBackend(Path(tmp) / "kb")
            create_kb(whole, fixed_spec())
            self.assertTrue(whole.exists("kb.json"))

    def test_write_path_contract_helper_is_red_under_noop_and_green_otherwise(self) -> None:
        def write_path_contract(backend) -> None:
            record_write(backend, "wiki/index.md", b"# index\n")
            assert backend.exists("wiki/index.md"), "写了却不存在"
            assert backend.read("wiki/index.md") == b"# index\n", "读回内容不符"
            assert backend.walk_files(".") == ["wiki/index.md"], "walk 看不到写入"

        write_path_contract(storage.MemoryBackend())
        with self.assertRaises((AssertionError, WriteReconcileFailed, storage.NotFound)):
            write_path_contract(NoOpBackend())


# ── credentials ─────────────────────────────────────────────────────────────


class CredentialTests(unittest.TestCase):
    def test_credentials_come_only_from_the_environment(self) -> None:
        env = {
            storage.ENV_HOST: "nas.test",
            storage.ENV_USER: "svc",
            storage.ENV_PASSWORD: "secret",
            storage.ENV_SHARE: "kb",
        }
        creds = storage.credentials_from_env(env)
        self.assertEqual(creds.share, "/kb")
        self.assertNotIn("secret", repr(creds))
        self.assertNotIn("password", creds.redacted())

    def test_missing_variables_fail_loudly(self) -> None:
        with self.assertRaises(storage.MissingCredentials) as ctx:
            storage.credentials_from_env({storage.ENV_HOST: "nas.test"})
        for name in (storage.ENV_USER, storage.ENV_PASSWORD, storage.ENV_SHARE):
            self.assertIn(name, str(ctx.exception))

    def test_plaintext_credential_flags_are_refused(self) -> None:
        for argv in (
            ["--name", "x", "--password", "hunter2"],
            ["--name", "x", "--password=hunter2"],
            ["--token=abc"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(storage.PlaintextCredential):
                    storage.assert_no_plaintext_credential_flags(argv)

    def test_no_cli_accepts_a_credential_on_the_command_line(self) -> None:
        import cwk_kb_create
        import cwk_kb_doctor
        import cwk_kb_migrate

        for module in (cwk_kb_create, cwk_kb_migrate, cwk_kb_doctor):
            declared = {
                option
                for action in module.build_parser()._actions
                for option in action.option_strings
            }
            with self.subTest(module=module.__name__):
                self.assertFalse(
                    declared & set(storage.FORBIDDEN_CREDENTIAL_FLAGS),
                    "CLI 声明了会把凭据放进进程表的参数",
                )

    def test_no_credential_literal_is_baked_into_the_storage_module(self) -> None:
        source = (PROJECT / "scripts" / "cwk_kb_storage.py").read_text(encoding="utf-8")
        # The only places a password may appear are the env-var name, the
        # dataclass field and the FileStation form key.
        for line in source.splitlines():
            if "passwd" not in line and "password" not in line.lower():
                continue
            with self.subTest(line=line.strip()[:60]):
                self.assertNotRegex(
                    line,
                    r"""(password|passwd)\s*[:=]\s*["'][^"'\s]+["']""",
                    "源码里出现了疑似硬编码凭据",
                )

    def test_password_is_posted_not_put_in_the_query_string(self) -> None:
        backend = fake_nas()
        backend.login()
        self.assertEqual(backend.fake.login_count, 1)
        backend.login()
        self.assertEqual(backend.fake.login_count, 1, "sid 应被复用，不该重复登录")


class SessionTeardownTests(unittest.TestCase):
    """每个 CLI 出口都要 logout：失败的那次也占着 NAS 会话。"""

    class RecordingBackend(storage.MemoryBackend):
        name = "recording"

        def __init__(self) -> None:
            super().__init__()
            self.logouts = 0

        def logout(self) -> None:
            self.logouts += 1

    def run_cli(self, module, argv):
        import contextlib

        made: List["SessionTeardownTests.RecordingBackend"] = []

        def fake_build_backend(_kind, **_kwargs):
            backend = SessionTeardownTests.RecordingBackend()
            made.append(backend)
            return backend

        buffer = io.StringIO()
        with mock.patch.object(module, "build_backend", fake_build_backend):
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = module.main(argv)
        return made, code, buffer.getvalue()

    def test_every_cli_logs_out_even_when_the_run_fails(self) -> None:
        import cwk_kb_create
        import cwk_kb_doctor
        import cwk_kb_migrate

        cases = (
            (cwk_kb_create, ["--name", "库", "--backend", "nas"]),
            (cwk_kb_migrate, ["apply", "--source-root", "x", "--dest-backend", "nas"]),
            (cwk_kb_doctor, ["verify", "--manifest", "--backend", "nas"]),
        )
        for module, argv in cases:
            with self.subTest(module=module.__name__):
                made, _code, _out = self.run_cli(module, argv)
                self.assertTrue(made, "CLI 没有建后端，测试没测到东西")
                for backend in made:
                    self.assertEqual(
                        backend.logouts, 1, f"{module.__name__} 没有释放 NAS 会话"
                    )

    def test_close_backend_tolerates_a_backend_without_a_session(self) -> None:
        storage.close_backend(storage.MemoryBackend())
        storage.close_backend(None)

    def test_a_failing_logout_does_not_replace_the_real_error(self) -> None:
        class Rude(storage.MemoryBackend):
            def logout(self) -> None:
                raise storage.RemoteStorageError("logout 也挂了")

        storage.close_backend(Rude())


# ── FileStation behaviour ───────────────────────────────────────────────────


class FileStationTests(unittest.TestCase):
    def test_round_trip_write_read_sha_list_delete(self) -> None:
        backend = fake_nas()
        payload = "内容\n".encode("utf-8")
        digest = backend.write("wiki/index.md", payload)
        self.assertEqual(digest, storage.sha256_bytes(payload))
        self.assertEqual(backend.read("wiki/index.md"), payload)
        self.assertEqual(backend.sha256("wiki/index.md"), digest)
        self.assertTrue(backend.exists("wiki/index.md"))
        self.assertIn("index.md", backend.list_dir("wiki"))
        backend.remove("wiki/index.md")
        self.assertFalse(backend.exists("wiki/index.md"))

    def test_transient_failures_are_retried_then_succeed(self) -> None:
        backend = fake_nas(faults=2)
        backend.write("wiki/index.md", b"x")
        self.assertEqual(backend.read("wiki/index.md"), b"x")

    def test_retry_gives_up_after_the_configured_attempts(self) -> None:
        backend = fake_nas(faults=99)
        with self.assertRaises(storage.TransientStorageError):
            backend.write("wiki/index.md", b"x")

    def test_permanent_failures_are_not_retried(self) -> None:
        attempts = {"n": 0}

        def transport(request):
            attempts["n"] += 1
            raise storage.RemoteStorageError("FileStation HTTP 401")

        backend = storage.FileStationBackend(
            storage.NasCredentials(host="nas.test", user="svc", password="pw", share="/kb"),
            transport=transport,
            retry=storage.RetryPolicy(attempts=4, base_delay=0, sleep=lambda _: None),
        )
        with self.assertRaises(storage.RemoteStorageError):
            backend.login()
        self.assertEqual(attempts["n"], 1, "永久错误不该重试")

    def test_mkdir_and_remove_are_idempotent(self) -> None:
        backend = fake_nas()
        backend.mkdir("wiki/summaries")
        backend.mkdir("wiki/summaries")  # 408 already-exists is tolerated
        backend.remove("wiki/missing.md")  # 408 not-found is tolerated
        backend.write("wiki/a.md", b"1")
        backend.write("wiki/a.md", b"2")
        self.assertEqual(backend.read("wiki/a.md"), b"2")

    def test_walk_files_recurses(self) -> None:
        backend = fake_nas()
        backend.write("wiki/index.md", b"a")
        backend.write("wiki/summaries/one.md", b"b")
        backend.write("kb.json", b"{}")
        self.assertEqual(
            backend.walk_files("."),
            ["kb.json", "wiki/index.md", "wiki/summaries/one.md"],
        )

    def test_http_status_maps_to_transient_or_permanent(self) -> None:
        """5xx / 429 are worth retrying; 401 / 403 / 404 are not."""
        creds = storage.NasCredentials(host="nas.test", user="u", password="p", share="/kb")
        backend = storage.FileStationBackend(creds)
        request = urllib.request.Request("https://nas.test:5001/webapi/entry.cgi")
        cases = (
            (500, storage.TransientStorageError),
            (503, storage.TransientStorageError),
            (429, storage.TransientStorageError),
            (401, storage.RemoteStorageError),
            (403, storage.RemoteStorageError),
            (404, storage.RemoteStorageError),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                def raise_http(*_args, **_kwargs):
                    raise urllib.error.HTTPError(request.full_url, code, "boom", {}, None)

                with mock.patch("urllib.request.urlopen", raise_http):
                    with self.assertRaises(expected):
                        backend._https_transport(request)

    def test_connection_errors_are_transient(self) -> None:
        creds = storage.NasCredentials(host="nas.test", user="u", password="p", share="/kb")
        backend = storage.FileStationBackend(creds)
        request = urllib.request.Request("https://nas.test:5001/webapi/entry.cgi")

        def raise_url_error(*_args, **_kwargs):
            raise urllib.error.URLError("connection reset by peer")

        with mock.patch("urllib.request.urlopen", raise_url_error):
            with self.assertRaises(storage.TransientStorageError):
                backend._https_transport(request)

    def test_host_must_not_carry_a_scheme(self) -> None:
        backend = storage.FileStationBackend(
            storage.NasCredentials(host="https://nas.test", user="u", password="p", share="/kb"),
            transport=lambda r: b"{}",
        )
        with self.assertRaises(storage.MissingCredentials):
            backend._base_url()

    def test_default_port_is_the_https_management_port(self) -> None:
        backend = fake_nas()
        self.assertEqual(backend._base_url(), "https://nas.test:5001/webapi")


class SidInBodyBackend(storage.FileStationBackend):
    """The pre-fix upload: ``_sid`` as a form field, plain endpoint URL.

    Used to prove the fake actually checks where the session id sits — a fake
    that accepts both placements locks nothing.
    """

    def _upload_request(self, *, fields, filename, payload):
        import secrets as _secrets

        boundary = "----cwk-kb-" + _secrets.token_hex(16)
        body = self._multipart(
            boundary,
            fields={**fields, "_sid": self.login()},
            filename=filename,
            payload=payload,
        )
        return urllib.request.Request(
            f"{self._base_url()}/entry.cgi",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


class WireContractTests(unittest.TestCase):
    """The fake must fail the requests a real DSM 7.x would fail."""

    def test_upload_takes_the_sid_from_the_query_string(self) -> None:
        backend = fake_nas()
        backend.write("wiki/index.md", b"x")
        upload = [call for call in backend.fake.calls if call.startswith("upload")]
        self.assertEqual(upload, ["upload /kb/wiki/index.md"])

    def test_a_sid_that_only_rides_in_the_body_is_refused(self) -> None:
        """红法：把 upload 的 sid 改回 body → 必须红（119 Invalid session）。"""
        transport = FakeFileStation()
        backend = SidInBodyBackend(
            storage.NasCredentials(host="nas.test", user="svc", password="pw", share="/kb"),
            transport=transport,
            retry=storage.RetryPolicy(attempts=2, base_delay=0, sleep=lambda _: None),
        )
        with self.assertRaises(storage.TransientStorageError) as ctx:
            backend.write("wiki/index.md", b"x")
        self.assertIn("119", str(ctx.exception))
        self.assertIn("upload REJECTED no _sid in query", transport.calls)
        self.assertEqual(transport.files, {}, "被拒绝的上传不该留下文件")

    def test_mkdir_creates_a_folder_the_device_can_then_see(self) -> None:
        """CreateFolder 的 folder_path 是数组形态；拼错了 exists 就永远是 False。"""
        backend = fake_nas()
        backend.mkdir("wiki/summaries")
        self.assertIn("/kb/wiki", backend.fake.folders)
        self.assertIn("/kb/wiki/summaries", backend.fake.folders)
        self.assertTrue(backend.exists("wiki"))
        self.assertTrue(backend.exists("wiki/summaries"))
        self.assertIn("summaries", backend.list_dir("wiki"))

    def test_mkdir_stays_idempotent_now_that_the_folder_is_real(self) -> None:
        backend = fake_nas()
        backend.mkdir("wiki/summaries")
        backend.mkdir("wiki/summaries")  # 408 already-exists, tolerated
        self.assertTrue(backend.exists("wiki/summaries"))

    def test_a_file_that_starts_with_a_brace_reads_back_unchanged(self) -> None:
        """负例：JSON 文件不是错误信封，不许被当成信封吞掉。"""
        backend = fake_nas()
        cases = {
            "kb.json": b'{"schema": "cwk.kb.identity.v1", "kb_code": "0000"}',
            "_system/odd.json": b'{"success": true, "data": {"note": "this is a file"}}',
            "_system/null.json": '{"success": null, "note": "抓到的响应"}'.encode("utf-8"),
            "_system/zero.json": b'{"success": 0, "records": []}',
            "_system/capture.json": (
                '{"success": false, "error": {"code": 408}, "note": "存档的失败响应"}'
            ).encode("utf-8"),
        }
        for path, payload in cases.items():
            with self.subTest(path=path):
                digest = backend.write(path, payload)
                self.assertEqual(backend.read(path), payload)
                self.assertEqual(backend.sha256(path), digest)

    def test_a_real_error_envelope_is_still_recognised(self) -> None:
        backend = fake_nas()
        with self.assertRaises(storage.NotFound):
            backend.read("wiki/missing.md")


# ── certificate pinning ─────────────────────────────────────────────────────


REAL_CERT = b"DER-of-the-real-nas"
EVIL_CERT = b"DER-of-the-attackers-box"
REAL_PIN = hashlib.sha256(REAL_CERT).hexdigest()
EVIL_PIN = hashlib.sha256(EVIL_CERT).hexdigest()


def http_response(status: int = 200, body: bytes = b'{"success": true}') -> bytes:
    reason = {200: "OK", 401: "Unauthorized", 503: "Service Unavailable"}[status]
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode("utf-8") + body


class FakeTLSSocket:
    """A socket that serves one certificate and one canned HTTP response.

    ``sent`` is the point of the object: a pin failure must leave it empty.
    """

    def __init__(self, der: bytes, response: bytes) -> None:
        self.der = der
        self.response = response
        self.sent = bytearray()
        self.closed = False

    def getpeercert(self, binary_form: bool = False):
        return self.der if binary_form else {}

    def sendall(self, data) -> None:
        self.sent += bytes(data)

    def send(self, data) -> int:
        self.sendall(data)
        return len(data)

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BytesIO(self.response)

    def settimeout(self, _timeout) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class PinnedConnectionHarness:
    """Replaces the real TLS handshake with a scripted one."""

    def __init__(self, certs, responses=None) -> None:
        self.certs = list(certs)
        self.responses = list(responses or [])
        self.sockets: List[FakeTLSSocket] = []
        self.connects = 0

    def connect(self, connection) -> None:
        self.connects += 1
        der = self.certs.pop(0) if self.certs else b""
        response = self.responses.pop(0) if self.responses else http_response()
        sock = FakeTLSSocket(der, response)
        self.sockets.append(sock)
        connection.sock = sock

    def patch(self):
        return mock.patch.object(
            http.client.HTTPSConnection, "connect", lambda conn: self.connect(conn)
        )


def pinned_backend(pin: str = REAL_PIN) -> storage.FileStationBackend:
    return storage.FileStationBackend(
        storage.NasCredentials(host="nas.test", user="svc", password="pw", share="/kb"),
        cert_sha256=pin,
        retry=storage.RetryPolicy(attempts=2, base_delay=0, sleep=lambda _: None),
    )


class CertificatePinningTests(unittest.TestCase):
    """The pin must bind the connection that carries the request.

    Verifying a fingerprint on a separate probe socket is a TOCTOU hole: the
    probe can be routed to the real NAS while the request goes elsewhere.
    """

    def setUp(self) -> None:
        self.request = urllib.request.Request(
            "https://nas.test:5001/webapi/entry.cgi", data=b"api=x", method="POST"
        )

    def test_a_matching_pin_lets_the_request_through(self) -> None:
        harness = PinnedConnectionHarness([REAL_CERT])
        backend = pinned_backend()
        with harness.patch():
            self.assertEqual(backend._https_transport(self.request), b'{"success": true}')
        self.assertIn(b"POST /webapi/entry.cgi", bytes(harness.sockets[0].sent))
        self.assertIn(b"api=x", bytes(harness.sockets[0].sent))

    def test_a_mismatched_pin_is_refused_before_a_single_byte_is_sent(self) -> None:
        """红法：证书换成攻击者的 → 拒绝，且请求一个字节都没发出。"""
        harness = PinnedConnectionHarness([EVIL_CERT])
        backend = pinned_backend()
        with harness.patch():
            with self.assertRaises(storage.RemoteStorageError) as ctx:
                backend._https_transport(self.request)
        self.assertIn(EVIL_PIN, str(ctx.exception))
        self.assertEqual(bytes(harness.sockets[0].sent), b"", "指纹不符却已经发出了请求")

    def test_a_pin_mismatch_is_permanent_not_retried(self) -> None:
        harness = PinnedConnectionHarness([EVIL_CERT, EVIL_CERT])
        backend = pinned_backend()
        with harness.patch():
            with self.assertRaises(storage.RemoteStorageError):
                storage.retry_call(backend.retry, lambda: backend._https_transport(self.request))
        self.assertEqual(harness.connects, 1, "指纹不符是永久错误，不该重试")

    def test_the_pin_is_re_verified_on_every_new_connection(self) -> None:
        """TOCTOU 红法：第一次连真机、第二次被换成假机 → 第二次必须红。"""
        harness = PinnedConnectionHarness([REAL_CERT, EVIL_CERT])
        backend = pinned_backend()
        with harness.patch():
            backend._https_transport(self.request)
            with self.assertRaises(storage.RemoteStorageError):
                backend._https_transport(self.request)
        self.assertEqual(harness.connects, 2, "第二次请求必须重新建连并重新验指纹")
        self.assertEqual(bytes(harness.sockets[1].sent), b"")

    def test_no_probe_connection_is_used_to_verify_the_pin(self) -> None:
        """指纹只能来自业务连接本身，不许另开一条连接去探。"""
        backend = pinned_backend()
        self.assertFalse(
            hasattr(backend, "_verify_pin"), "独立预验方法必须已废弃"
        )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("不允许另开连接探证书（TOCTOU）")

        harness = PinnedConnectionHarness([REAL_CERT])
        with mock.patch.object(storage.ssl, "get_server_certificate", forbidden):
            with harness.patch():
                backend._https_transport(self.request)

    def test_a_server_that_presents_no_certificate_is_refused(self) -> None:
        harness = PinnedConnectionHarness([b""])
        backend = pinned_backend()
        with harness.patch():
            with self.assertRaises(storage.RemoteStorageError):
                backend._https_transport(self.request)

    def test_pinned_transport_maps_http_status_like_the_plain_one(self) -> None:
        cases = ((503, storage.TransientStorageError), (401, storage.RemoteStorageError))
        for status, expected in cases:
            with self.subTest(status=status):
                harness = PinnedConnectionHarness(
                    [REAL_CERT], [http_response(status, b"boom")]
                )
                backend = pinned_backend()
                with harness.patch():
                    with self.assertRaises(expected):
                        backend._https_transport(self.request)

    def test_the_pinned_path_is_what_a_pinned_backend_actually_calls(self) -> None:
        """The unpinned branch must not be reachable while a pin is configured."""
        backend = pinned_backend()
        called: Dict[str, int] = {"pinned": 0}

        def pinned(_request):
            called["pinned"] += 1
            return b"{}"

        with mock.patch.object(backend, "_pinned_transport", pinned):
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("绕过了 pin")):
                backend._https_transport(self.request)
        self.assertEqual(called["pinned"], 1)


# ── J7: real-machine smoke ──────────────────────────────────────────────────

_HAS_NAS_CREDS = bool(os.environ.get(storage.ENV_HOST))
_SKIP_REASON = "SKIP-reason: no NAS creds (CWK_NAS_KB_HOST unset)"

try:  # pytest is not installed in CI, and `make test` runs plain unittest.
    import pytest  # type: ignore

    _nas_mark = pytest.mark.skipif(not _HAS_NAS_CREDS, reason=_SKIP_REASON)
except ImportError:  # pragma: no cover - depends on the local interpreter

    def _nas_mark(func):
        return func


def requires_nas(func):
    """Skip unless real NAS credentials are present.

    Both decorators are applied on purpose: ``pytest.mark.skipif`` is what the
    RT asks for, and ``unittest.skipUnless`` is what actually skips when the
    suite runs under ``make test``'s plain unittest runner (a pytest mark on a
    TestCase method is inert there, and the test would really try to reach the
    NAS).
    """
    return unittest.skipUnless(_HAS_NAS_CREDS, _SKIP_REASON)(_nas_mark(func))


class NasSmokeTests(unittest.TestCase):
    """J7 — probe dir → write → read → sha256 → delete against the real NAS.

    Not part of CI: credentials live only on the operator's machine.  Run it
    with the four ``CWK_NAS_KB_*`` variables exported.
    """

    @requires_nas
    def test_probe_directory_round_trip(self) -> None:
        backend = storage.FileStationBackend.from_env()
        probe = f"_probe/rt042-{secrets.token_hex(8)}"
        payload = f"rt042 smoke {datetime.now(timezone.utc).isoformat()}\n".encode("utf-8")
        try:
            backend.mkdir(probe)
            self.assertTrue(backend.exists(probe))
            digest = backend.write(f"{probe}/probe.txt", payload)
            self.assertEqual(backend.read(f"{probe}/probe.txt"), payload)
            self.assertEqual(backend.sha256(f"{probe}/probe.txt"), digest)
            self.assertEqual(digest, storage.sha256_bytes(payload))
            self.assertIn("probe.txt", backend.list_dir(probe))
        finally:
            backend.remove(f"{probe}/probe.txt")
            backend.remove_dir(probe)
            backend.logout()
        self.assertFalse(backend.exists(f"{probe}/probe.txt"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
