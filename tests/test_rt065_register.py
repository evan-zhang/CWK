"""RT-065: 用户自助注册——填工作协同 Key，写入人员目录，建立薄会话。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_admin
import kb_authz
import kb_identity


KEY = "synthetic-business-key-do-not-log"
PERSON = "1700000000000000001"
CORP = "1600000000000000002"
NAME = "测试用户"


class FakePerson:
    corp_id = CORP
    person_id = PERSON
    name = NAME

    @property
    def principal(self) -> str:
        return f"person:{CORP}:{PERSON}"


class RegisterHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "kb-authz.json"
        kb_authz.init_store(self.store)
        self.audit = root / "audit.jsonl"
        self.env = {
            "KB_ADMIN_ENABLED": "false",
            "KB_ADMIN_FACE": "register",
            "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store),
            "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
            "KB_REGISTER_ALLOW_HTTP": "true",
            "KB_ADMIN_AUDIT_PATH": str(self.audit),
        }
        self.app = kb_admin.AdminApp(self.env)
        self.app.resolve_person = lambda app_key, **kwargs: FakePerson()
        self.server = kb_admin.AdminHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path: str, method="GET", body=None, headers=None, cookie=None):
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                raw = response.read()
                set_cookie = response.headers.get("Set-Cookie")
                payload = json.loads(raw) if raw and not path.startswith("/register") else raw.decode("utf-8")
                return response.status, payload, set_cookie
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw.decode("utf-8", errors="replace")
            return error.code, payload, error.headers.get("Set-Cookie")

    def test_register_page_is_a_simple_form(self):
        status, html, _ = self.request("/register")
        self.assertEqual(status, 200)
        self.assertIn("工作协同 Key", html)
        self.assertIn("/api/register", html)
        self.assertNotIn(KEY, html)

    def test_register_writes_person_and_sets_session_cookie(self):
        status, payload, cookie = self.request("/api/register", method="POST", body={"app_key": KEY})
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], NAME)
        self.assertEqual(payload["principal"], f"person:{CORP}:{PERSON}")
        self.assertIsNotNone(cookie)
        self.assertIn(kb_admin.SESSION_COOKIE + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        store = kb_authz.load_store(self.store)
        self.assertIn(payload["principal"], store["persons"])
        self.assertEqual(store["persons"][payload["principal"]]["name"], NAME)
        # Key must never appear in the audit file.
        audit = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(KEY, audit)
        self.assertIn('"action":"register"', audit.replace(" ", ""))

        token = cookie.split(";", 1)[0]
        status, session, _ = self.request("/api/session", cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(session["name"], NAME)

    def test_bad_key_is_refused_and_store_unchanged(self):
        def refuse(app_key, **kwargs):
            raise kb_identity.IdentityResolutionError("玄关拒绝了这把 Key（resultCode=401）")

        self.app.resolve_person = refuse
        before = self.store.read_bytes()
        status, payload, cookie = self.request("/api/register", method="POST", body={"app_key": KEY})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "identity_refused")
        self.assertIsNone(cookie)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertNotIn(KEY, json.dumps(payload))

    def test_https_is_required_when_allow_http_is_off(self):
        self.app.allow_http_register = False
        status, payload, _ = self.request("/api/register", method="POST", body={"app_key": KEY})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "https_required")

    def test_a_forged_proxy_header_does_not_unlock_plaintext_registration(self):
        """X-Forwarded-Proto 是客户端发的。没有反代覆盖它时，它什么也不证明。"""
        self.app.allow_http_register = False
        status, payload, _ = self.request(
            "/api/register", method="POST", body={"app_key": KEY},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "https_required")

    def test_the_header_counts_only_when_a_proxy_is_declared(self):
        self.app.allow_http_register = False
        self.app.trust_proxy = True
        status, _, cookie = self.request(
            "/api/register", method="POST", body={"app_key": KEY},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertIn("Secure", cookie or "")

    def test_disabled_register_is_a_404(self):
        app = kb_admin.AdminApp({"KB_ADMIN_ENABLED": "false", "KB_ADMIN_FACE": "register"})
        self.assertFalse(app.register_enabled)
        self.assertEqual(app.handle("GET", "/register", {})[0], 404)
        self.assertEqual(app.handle("POST", "/api/register", {}, b'{"app_key":"x"}')[0], 404)

    def test_logout_clears_session(self):
        _, _, cookie = self.request("/api/register", method="POST", body={"app_key": KEY})
        token = cookie.split(";", 1)[0]
        status, _, clear = self.request("/api/logout", method="POST", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", clear or "")
        # Client drops the cookie after Set-Cookie Max-Age=0; without it, session is gone.
        self.assertEqual(self.request("/api/session")[0], 401)

    def test_portal_shows_the_register_link_only_for_a_configured_https_url(self):
        import kb_portal
        console = {"KB_PORTAL_CONSOLE_URL": "http://192.168.91.72:8791/console"}
        plain = kb_portal.PortalApp(console).page()
        self.assertNotIn(">注册<", plain, "没配注册地址时不该猜一个出来")
        insecure = kb_portal.PortalApp(dict(console, KB_PORTAL_REGISTER_URL="http://192.168.91.72:8793/register")).page()
        self.assertNotIn(">注册<", insecure, "明文的注册链接不能出现在门户上")
        secure = kb_portal.PortalApp(dict(console, KB_PORTAL_REGISTER_URL="https://192.168.91.72:8793/register")).page()
        self.assertIn("href='https://192.168.91.72:8793/register'", secure)
        self.assertIn(">注册<", secure)


if __name__ == "__main__":
    unittest.main()
