"""RT-065 加固判据：注册面与管理面分开、加密不可伪造、注册不可滥用。

复核实测发现三个洞（2026-09-19），这里逐个锁住：

1. **加密要求挡不住任何东西**：`X-Forwarded-Proto` 是客户端自己发的头，没有反代
   覆盖它时谁都能写 `https`，Key 照样明文过网。而且明文时页面照样给表单，
   用户填完点提交，Key 已经上路了才被拒——闸装在门外。
2. **注册页挂在管理台上**：要给同事用就得把管理台端口开到局域网，而管理面只有
   一把共享密钥守着。注册面必须是独立的一个面，上面没有任何管理接口。
3. **注册接口没有限速**：它把收到的任何字符串拿去问玄关，等于一个「这把 Key 有效吗」
   的免费验证器，也能借我们去压玄关。

真实 TLS 那条端到端判据需要 openssl 生成自签证书；没有 openssl 的环境跳过，
其余判据不依赖它。
"""
from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_admin  # noqa: E402
import kb_authz  # noqa: E402

KEY = "synthetic-business-key-do-not-log"
CORP = "1600000000000000002"
PERSON = "1700000000000000001"


class FakePerson:
    corp_id = CORP
    person_id = PERSON
    name = "测试用户"

    @property
    def principal(self) -> str:
        return f"person:{CORP}:{PERSON}"


def make_app(tmp: Path, **extra) -> kb_admin.AdminApp:
    store = tmp / "kb-authz.json"
    if not store.exists():
        kb_authz.init_store(store)
    env = {
        "KB_ADMIN_ENABLED": "true",
        "KB_ADMIN_KEY_ENV": "RT065_TEST_ADMIN_KEY",
        "KB_ADMIN_FACE": "register",
        "KB_REGISTER_ENABLED": "true",
        "KB_AUTHZ_STORE": str(store),
        "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
        "KB_ADMIN_AUDIT_PATH": str(tmp / "audit.jsonl"),
    }
    env.update(extra)
    app = kb_admin.AdminApp(env)
    app.resolve_person = lambda app_key, **kwargs: FakePerson()
    return app


class FaceSeparationTests(unittest.TestCase):
    """管理面和注册面互不包含对方的路由。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_register_face_carries_no_management_route(self):
        app = make_app(self.root)
        for route in ("/console", "/", "/api/overview", "/api/audit", "/api/services", "/api/libraries"):
            with self.subTest(route):
                status, payload, _ = app.handle("GET", route, {}, b"")
                self.assertEqual(status, 404, f"{route} 不该出现在注册面上")
                self.assertEqual(payload, {"error": "not_found"})

    def test_admin_face_carries_no_registration_route(self):
        app = make_app(self.root, KB_ADMIN_FACE="admin")
        for method, route in (("GET", "/register"), ("POST", "/api/register"),
                              ("GET", "/api/session"), ("POST", "/api/logout")):
            with self.subTest(route):
                status, payload, _ = app.handle(method, route, {}, b'{"app_key":"x"}')
                self.assertEqual(status, 404, f"{route} 不该出现在管理面上")

    def test_an_unknown_face_falls_back_to_admin(self):
        """配置写错时退回管理面（回环、要密钥），而不是退回公开面。"""
        app = make_app(self.root, KB_ADMIN_FACE="registr")
        self.assertEqual(app.face, kb_admin.FACE_ADMIN)
        self.assertEqual(app.handle("GET", "/register", {}, b"")[0], 404)


class PlaintextIsRefusedBeforeTheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(Path(self.tmp.name))

    def test_the_plaintext_page_has_no_field_to_type_a_key_into(self):
        status, payload, _ = self.app.handle("GET", "/register", {}, b"")
        self.assertEqual(status, 403)
        page = payload["html"]
        self.assertNotIn("<input", page, "明文页面不能提供输入框")
        self.assertNotIn("/api/register", page, "明文页面不能提供提交入口")
        self.assertIn("加密", page)

    def test_a_forged_header_cannot_unlock_the_page_or_the_endpoint(self):
        forged = {"X-Forwarded-Proto": "https"}
        self.assertEqual(self.app.handle("GET", "/register", forged, b"")[0], 403)
        status, payload, _ = self.app.handle("POST", "/api/register", forged, b'{"app_key":"' + KEY.encode() + b'"}')
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "https_required")

    def test_forwarded_header_variants_are_all_ignored_by_default(self):
        for header in ({"Forwarded": "proto=https"}, {"X-Forwarded-Proto": "https, http"},
                       {"X-Forwarded-Proto": "HTTPS"}):
            with self.subTest(header=header):
                self.assertEqual(self.app.handle("GET", "/register", header, b"")[0], 403)

    def test_a_declared_proxy_makes_the_header_count(self):
        app = make_app(Path(self.tmp.name), KB_REGISTER_TRUST_PROXY="true")
        self.assertEqual(app.handle("GET", "/register", {"X-Forwarded-Proto": "https"}, b"")[0], 200)
        self.assertEqual(app.handle("GET", "/register", {}, b"")[0], 403, "代理没说是 https 就仍然拒绝")

    def test_a_real_tls_connection_needs_no_header_at_all(self):
        status, payload, headers = self.app.handle(
            "POST", "/api/register", {}, b'{"app_key":"k"}', direct_tls=True, client="10.0.0.9")
        self.assertEqual(status, 200, payload)
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])

    def test_local_debugging_switch_still_works_but_marks_the_cookie_insecure(self):
        app = make_app(Path(self.tmp.name), KB_REGISTER_ALLOW_HTTP="true")
        self.assertEqual(app.handle("GET", "/register", {}, b"")[0], 200)
        status, _, headers = app.handle("POST", "/api/register", {}, b'{"app_key":"k"}', client="127.0.0.1")
        self.assertEqual(status, 200)
        self.assertNotIn("Secure", headers["Set-Cookie"])


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(Path(self.tmp.name), KB_REGISTER_RATE_LIMIT="3")

    def post(self, client: str):
        return self.app.handle("POST", "/api/register", {}, b'{"app_key":"k"}', direct_tls=True, client=client)[0]

    def test_a_caller_cannot_use_registration_as_a_key_checking_oracle(self):
        self.assertEqual([self.post("10.0.0.9") for _ in range(4)], [200, 200, 200, 429])

    def test_the_cap_is_per_caller(self):
        for _ in range(3):
            self.post("10.0.0.9")
        self.assertEqual(self.post("10.0.0.10"), 200, "别人用光了额度不该把我也挡住")

    def test_refusal_tells_the_caller_when_to_come_back(self):
        for _ in range(3):
            self.post("10.0.0.9")
        status, payload, headers = self.app.handle(
            "POST", "/api/register", {}, b'{"app_key":"k"}', direct_tls=True, client="10.0.0.9")
        self.assertEqual((status, payload["error"]), (429, "rate_limited"))
        self.assertIn("Retry-After", headers)


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


@unittest.skipUnless(openssl_available(), "需要 openssl 生成自签证书")
class RealTlsEndToEndTests(unittest.TestCase):
    """真的跑一次 https：自签证书、真实握手、真实 Cookie。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        self.app = make_app(root)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self.server = kb_admin.AdminHTTPServer(("127.0.0.1", 0), self.app, tls=context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"https://127.0.0.1:{self.server.server_port}"
        self.client = ssl.create_default_context(cafile=str(cert))

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5, context=self.client) as response:
                return response.status, response.read(), response.headers.get("Set-Cookie")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers.get("Set-Cookie")

    def test_registration_over_real_tls_needs_no_forwarded_header(self):
        status, page, _ = self.request("/register")
        self.assertEqual(status, 200)
        self.assertIn("工作协同 Key", page.decode("utf-8"))

        status, raw, cookie = self.request("/api/register", method="POST", body={"app_key": KEY})
        self.assertEqual(status, 200, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["principal"], f"person:{CORP}:{PERSON}")
        self.assertIn("Secure", cookie)
        store = kb_authz.load_store(self.app.authz_store)
        self.assertIn(f"person:{CORP}:{PERSON}", store["persons"])
        self.assertNotIn(KEY, self.app.authz_store.read_text("utf-8"))


class StartupGuardTests(unittest.TestCase):
    def test_the_register_face_refuses_to_start_without_a_certificate(self):
        with self.assertRaises(SystemExit):
            kb_admin.main(["--face", "register", "--port", "0"])

    def test_a_certificate_without_its_key_is_refused(self):
        with self.assertRaises(SystemExit):
            kb_admin.main(["--face", "register", "--tls-cert", "/nonexistent.pem", "--port", "0"])


if __name__ == "__main__":
    unittest.main()
