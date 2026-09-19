"""RT-068 判据：成员管理页面——操作者是登录的那个人，不是知道密码的任何人。

这一页刻意不放在明文控制台上：那里是局域网可达 + 共享密码，给它写权限等于
谁知道密码就能给自己开任意库。它放在已经有 TLS 和本人登录态的注册面上，
权限判定复用 RT-061 的角色规则，本文件锁住这条边界与规则的落地。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_admin  # noqa: E402
import kb_authz  # noqa: E402
from kb_identity import PersonIdentity  # noqa: E402

CORP = "1600000000000000002"
OWNER = PersonIdentity(CORP, "1700000000000000001", "所有者")
MATE = PersonIdentity(CORP, "1700000000000000003", "同事")
OTHER = PersonIdentity(CORP, "1700000000000000005", "旁人")
SERVICE = "service:rag-answer"
WRITE_HEADERS = {kb_admin.MEMBER_WRITE_HEADER: "1"}


class MembersCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / "kb-authz.json"
        kb_authz.init_store(self.store)
        data = kb_authz.load_store(self.store)
        for person in (OWNER, MATE, OTHER):
            kb_authz.upsert_person(data, person)
        kb_authz.create_bank(data, actor=kb_authz.OPS_ADMIN, bank_id="bank-a", name="甲库", owner=OWNER.principal)
        kb_authz.create_bank(data, actor=kb_authz.OPS_ADMIN, bank_id="bank-b", name="乙库", owner=OTHER.principal)
        kb_authz.set_member(data, actor=kb_authz.OPS_ADMIN, bank_id="bank-a", principal=SERVICE, role="reader")
        kb_authz.save_store(self.store, data)
        self.app = kb_admin.AdminApp({
            "KB_ADMIN_ENABLED": "false",
            "KB_ADMIN_FACE": "register",
            "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store),
            "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
            "KB_ADMIN_AUDIT_PATH": str(root / "audit.jsonl"),
            "KB_REGISTER_ALLOW_HTTP": "true",
        })

    def cookie(self, person: PersonIdentity) -> dict:
        import time
        token = kb_admin._seal_session(self.app.session_secret, principal=person.principal,
                                       name=person.name, exp=int(time.time()) + 600)
        return {"Cookie": f"{kb_admin.SESSION_COOKIE}={token}"}

    def view(self, person=None):
        headers = self.cookie(person) if person else {}
        status, payload, _ = self.app.handle("GET", "/api/members", headers, b"")
        return status, payload

    def write(self, person, bank, principal, role, *, version=None, headers=None):
        body = {"bank_id": bank, "principal": principal, "role": role}
        if version is None:
            version = kb_authz.load_store(self.store)["version"]
        body["expect_version"] = version
        request = dict(self.cookie(person) if person else {})
        request.update(WRITE_HEADERS if headers is None else headers)
        status, payload, _ = self.app.handle("POST", "/api/members", request, json.dumps(body).encode())
        return status, payload

    def role_of(self, bank, principal):
        return kb_authz.role_of(kb_authz.load_store(self.store), bank, principal)


class SessionIsTheAuthorityTests(MembersCase):
    def test_no_session_sees_and_changes_nothing(self):
        self.assertEqual(self.view()[0], 401)
        self.assertEqual(self.write(None, "bank-a", MATE.principal, "reader")[0], 401)

    def test_the_page_needs_no_admin_key(self):
        """整条路径不碰共享管理密钥——这正是它不放在明文控制台上的原因。"""
        status, payload = self.view(OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(payload["me"]["principal"], OWNER.principal)
        self.assertFalse(self.app.enabled, "管理面是关的，这一页照样可用")

    def test_an_owner_only_sees_their_own_banks(self):
        _, payload = self.view(OWNER)
        self.assertEqual([b["bank_id"] for b in payload["banks"]], ["bank-a"])
        _, other = self.view(OTHER)
        self.assertEqual([b["bank_id"] for b in other["banks"]], ["bank-b"])

    def test_a_plain_member_manages_nothing(self):
        self.write(OWNER, "bank-a", MATE.principal, "writer")
        status, payload = self.view(MATE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["banks"], [])
        self.assertEqual(payload["persons"], [], "不能管理任何库的人，也看不到人员目录")

    def test_managing_someone_elses_bank_is_refused(self):
        status, payload = self.write(OWNER, "bank-b", MATE.principal, "reader")
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        self.assertIsNone(self.role_of("bank-b", MATE.principal))


class RoleRulesAreTheAuthzRulesTests(MembersCase):
    def test_add_change_and_remove(self):
        self.assertEqual(self.write(OWNER, "bank-a", MATE.principal, "reader")[0], 200)
        self.assertEqual(self.role_of("bank-a", MATE.principal), "reader")
        self.assertEqual(self.write(OWNER, "bank-a", MATE.principal, "writer")[0], 200)
        self.assertEqual(self.role_of("bank-a", MATE.principal), "writer")
        self.assertEqual(self.write(OWNER, "bank-a", MATE.principal, None)[0], 200)
        self.assertIsNone(self.role_of("bank-a", MATE.principal))

    def test_the_last_owner_cannot_be_removed_through_the_page(self):
        status, payload = self.write(OWNER, "bank-a", OWNER.principal, None)
        self.assertEqual((status, payload["error"]), (409, "conflict"))
        self.assertIn("至少要保留一位所有者", payload["message"])
        self.assertEqual(self.role_of("bank-a", OWNER.principal), "owner")

    def test_a_rule_refusal_is_not_reported_as_a_concurrent_edit(self):
        """两者都是 409，但一个重试有用、一个重试无用——不能给同一句解释。"""
        _, rule = self.write(OWNER, "bank-a", OWNER.principal, "reader")
        stale = kb_authz.load_store(self.store)["version"] - 1
        _, concurrent = self.write(OWNER, "bank-a", MATE.principal, "reader", version=stale)
        self.assertEqual(rule["error"], "conflict")
        self.assertEqual(concurrent["error"], "version_conflict")
        self.assertNotEqual(rule["message"], concurrent["message"])

    def test_an_owner_cannot_demote_another_owner(self):
        self.write(OWNER, "bank-a", MATE.principal, "owner")
        status, payload = self.write(MATE, "bank-a", OWNER.principal, "reader")
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        self.assertEqual(self.role_of("bank-a", OWNER.principal), "owner")

    def test_an_owner_cannot_touch_the_service_grant(self):
        status, payload = self.write(OWNER, "bank-a", SERVICE, None)
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        self.assertEqual(self.role_of("bank-a", SERVICE), "reader",
                         "移掉它这个库的问答就断了，只有管理员能动")

    def test_only_enrolled_people_can_be_added(self):
        stranger = f"person:{CORP}:1799999999999999999"
        status, payload = self.write(OWNER, "bank-a", stranger, "reader")
        self.assertEqual((status, payload["error"]), (404, "not_found"))

    def test_a_nonsense_role_is_rejected(self):
        status, payload = self.write(OWNER, "bank-a", MATE.principal, "superuser")
        self.assertEqual((status, payload["error"]), (400, "rejected"))


class ConcurrentEditTests(MembersCase):
    def test_a_stale_page_cannot_overwrite_a_newer_change(self):
        stale = kb_authz.load_store(self.store)["version"]
        self.assertEqual(self.write(OWNER, "bank-a", MATE.principal, "reader", version=stale)[0], 200)
        status, payload = self.write(OWNER, "bank-a", OTHER.principal, "reader", version=stale)
        self.assertEqual((status, payload["error"]), (409, "version_conflict"))
        self.assertIsNone(self.role_of("bank-a", OTHER.principal))

    def test_the_view_hands_out_the_version_the_write_needs(self):
        _, payload = self.view(OWNER)
        self.assertEqual(self.write(OWNER, "bank-a", MATE.principal, "reader", version=payload["version"])[0], 200)


class CrossSiteAndTransportTests(MembersCase):
    def test_a_write_without_the_page_marker_is_refused(self):
        """跨站表单加不上这个头（会触发预检），所以它是一道便宜的第二把锁。"""
        status, payload = self.write(OWNER, "bank-a", MATE.principal, "reader", headers={})
        self.assertEqual((status, payload["error"]), (400, "invalid_request"))
        self.assertIsNone(self.role_of("bank-a", MATE.principal))

    def test_the_page_is_not_served_over_plaintext_in_production(self):
        app = kb_admin.AdminApp({
            "KB_ADMIN_FACE": "register", "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store), "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
        })
        status, payload, _ = app.handle("GET", "/members", {}, b"")
        self.assertEqual(status, 403)
        self.assertNotIn("<table", payload["html"])
        self.assertEqual(app.handle("GET", "/members", {}, b"", direct_tls=True)[0], 200)

    def test_the_admin_face_has_no_member_routes_at_all(self):
        admin = kb_admin.AdminApp({"KB_ADMIN_FACE": "admin", "KB_ADMIN_ENABLED": "true",
                                   "KB_ADMIN_KEY_ENV": "RT068_KEY"})
        for method, route in (("GET", "/members"), ("GET", "/api/members"), ("POST", "/api/members")):
            with self.subTest(route):
                self.assertEqual(admin.handle(method, route, WRITE_HEADERS, b"{}")[0], 404)


class PageContentTests(MembersCase):
    def test_the_page_tells_the_two_kinds_of_409_apart(self):
        page = kb_admin._MEMBERS_HTML
        self.assertIn("version_conflict", page)
        self.assertIn("刚被别人改过", page)
        self.assertIn("这个改动不被允许", page)

    def test_the_page_sends_the_marker_header_and_the_version(self):
        page = kb_admin._MEMBERS_HTML
        self.assertIn(kb_admin.MEMBER_WRITE_HEADER, page)
        self.assertIn("expect_version", page)

    def test_the_page_never_carries_a_key_or_a_registry_path(self):
        page = kb_admin._MEMBERS_HTML
        for forbidden in ("X-KB-Admin-Key", "app_key", "KB_ADMIN_KEY", "rt055-tokens"):
            self.assertNotIn(forbidden, page)


if __name__ == "__main__":
    unittest.main()
