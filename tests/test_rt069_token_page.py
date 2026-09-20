"""RT-069 判据：自助签令牌——只能签给自己，而且必须当场再证明一次身份。

RT-047 的铁律是"令牌的主人只能由当场核实过的 Key 推导"。页面化最容易在这里破功：
做成"管理员替同事签"，主人就成了一句声明。所以这一页的签发要同时满足两件事——
**会话说你是谁**，并且**你当场交出的 Key 解析出同一个人**。少一个都不签。

于是：会话被盗签不出令牌（没有 Key），拿着别人的 Key 也签不出（会话对不上）。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_admin  # noqa: E402
import kb_authz  # noqa: E402
import kb_token  # noqa: E402
from kb_identity import IdentityResolutionError, PersonIdentity  # noqa: E402

CORP = "1600000000000000002"
ME = PersonIdentity(CORP, "1700000000000000001", "本人")
MATE = PersonIdentity(CORP, "1700000000000000003", "同事")
KEYS = {"my-key": ME, "mate-key": MATE}
WRITE = {kb_admin.MEMBER_WRITE_HEADER: "1"}


class TokenPageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = root / "tokens.json"
        self.store = root / "kb-authz.json"
        kb_token.init_registry(self.registry)
        kb_authz.init_store(self.store)
        data = kb_authz.load_store(self.store)
        for person in (ME, MATE):
            kb_authz.upsert_person(data, person)
        kb_authz.create_bank(data, actor=kb_authz.OPS_ADMIN, bank_id="bank-a", name="甲库", owner=ME.principal)
        kb_authz.save_store(self.store, data)
        self.app = kb_admin.AdminApp({
            "KB_ADMIN_ENABLED": "false",
            "KB_ADMIN_FACE": "register",
            "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store),
            "KB_REGISTRY_PATH": str(self.registry),
            "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
            "KB_ADMIN_AUDIT_PATH": str(root / "audit.jsonl"),
            "KB_REGISTER_ALLOW_HTTP": "true",
        })
        self.app.resolve_person = self.resolve

    @staticmethod
    def resolve(app_key, **kwargs):
        if app_key in KEYS:
            return KEYS[app_key]
        raise IdentityResolutionError("玄关拒绝了这把 Key")

    def cookie(self, person: PersonIdentity) -> dict:
        token = kb_admin._seal_session(self.app.session_secret, principal=person.principal,
                                       name=person.name, exp=int(time.time()) + 600)
        return {"Cookie": f"{kb_admin.SESSION_COOKIE}={token}"}

    def issue(self, person, key, *, agent="my-mac", label="", headers=None):
        request = dict(self.cookie(person) if person else {})
        request.update(WRITE if headers is None else headers)
        body = json.dumps({"app_key": key, "agent_id": agent, "label": label}).encode()
        status, payload, _ = self.app.handle("POST", "/api/tokens", request, body)
        return status, payload

    def listing(self, person):
        status, payload, _ = self.app.handle("GET", "/api/tokens", self.cookie(person) if person else {}, b"")
        return status, payload

    def revoke(self, person, token_id):
        request = dict(self.cookie(person)); request.update(WRITE)
        status, payload, _ = self.app.handle("POST", "/api/tokens/revoke", request,
                                             json.dumps({"token_id": token_id}).encode())
        return status, payload

    def records(self):
        return kb_token.records(kb_token.load_registry(self.registry))

    def make_admin(self, person: PersonIdentity) -> None:
        kb_authz.mutate(self.store, lambda d: kb_authz.set_admin(
            d, actor=kb_authz.OPS_ADMIN, principal=person.principal, admin=True))


class OnlyForYourselfTests(TokenPageCase):
    def test_issuing_needs_both_the_session_and_the_key(self):
        self.assertEqual(self.issue(None, "my-key")[0], 401, "没会话不能签")
        status, payload = self.issue(ME, "")
        self.assertEqual((status, payload["error"]), (400, "missing_key"), "光有会话也不能签")
        self.assertEqual(self.records(), [])

    def test_someone_elses_key_cannot_mint_a_token_in_my_session(self):
        status, payload = self.issue(ME, "mate-key")
        self.assertEqual((status, payload["error"]), (403, "identity_mismatch"))
        self.assertEqual(self.records(), [])

    def test_my_key_in_someone_elses_session_is_refused_too(self):
        kb_authz.mutate(self.store, lambda d: kb_authz.set_member(
            d, actor=kb_authz.OPS_ADMIN, bank_id="bank-a", principal=MATE.principal, role="reader"))
        status, payload = self.issue(MATE, "my-key")
        self.assertEqual((status, payload["error"]), (403, "identity_mismatch"))
        self.assertEqual(self.records(), [])

    def test_a_rejected_key_is_refused_without_echoing_it(self):
        status, payload = self.issue(ME, "not-a-real-key")
        self.assertEqual((status, payload["error"]), (401, "identity_refused"))
        self.assertNotIn("not-a-real-key", json.dumps(payload, ensure_ascii=False))

    def test_the_issued_token_belongs_to_the_person_who_asked(self):
        status, payload = self.issue(ME, "my-key", label="我的电脑")
        self.assertEqual(status, 200, payload)
        record = self.records()[0]
        self.assertEqual(record["principal"], ME.principal)
        self.assertEqual(record["authz"], kb_token.AUTHZ_GRANTS, "跟随成员表，加减权限不用重签")
        self.assertEqual(record["label"], "我的电脑")
        self.assertEqual(record["kb_ids"], ["bank-a"], "快照取的是他此刻的成员资格")


class SecretHandlingTests(TokenPageCase):
    def test_the_plaintext_is_returned_once_and_never_stored(self):
        _, payload = self.issue(ME, "my-key")
        plaintext = payload["token"]
        self.assertEqual(len(plaintext), 64)
        raw = self.registry.read_text("utf-8")
        self.assertNotIn(plaintext, raw, "登记表只存摘要")
        self.assertNotIn("my-key", raw, "业务 Key 更不会进登记表")
        _, listing = self.listing(ME)
        self.assertNotIn(plaintext, json.dumps(listing, ensure_ascii=False), "列表里再也拿不到明文")

    def test_the_listing_hides_digests_and_derived_identifiers(self):
        self.issue(ME, "my-key")
        _, listing = self.listing(ME)
        row = listing["tokens"][0]
        for hidden in ("token_sha256", "owner_ref", "owner_ref_basis", "agent_binding_id"):
            self.assertNotIn(hidden, row)
        self.assertIn("token_id", row)

    def test_the_page_itself_carries_no_secret(self):
        page = kb_admin._TOKENS_HTML
        for forbidden in ("token_sha256", "owner_ref", "X-KB-Admin-Key", "KB_REGISTRY_PATH"):
            self.assertNotIn(forbidden, page)
        self.assertIn("只出现这一次", page)


class ListingAndRevokeTests(TokenPageCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.mine = self.issue(ME, "my-key")
        kb_authz.mutate(self.store, lambda d: kb_authz.set_member(
            d, actor=kb_authz.OPS_ADMIN, bank_id="bank-a", principal=MATE.principal, role="reader"))
        _, self.theirs = self.issue(MATE, "mate-key", agent="mate-mac")

    def test_a_person_sees_only_their_own_tokens(self):
        _, listing = self.listing(MATE)
        self.assertEqual([t["token_id"] for t in listing["tokens"]], [self.theirs["token_id"]])
        self.assertEqual(listing["all_tokens"], [], "不是管理员就看不到别人的")

    def test_an_administrator_sees_everyone_but_still_cannot_sign_for_them(self):
        self.make_admin(ME)
        _, listing = self.listing(ME)
        self.assertTrue(listing["me"]["admin"])
        self.assertEqual([t["token_id"] for t in listing["all_tokens"]], [self.theirs["token_id"]])
        self.assertEqual(self.issue(ME, "mate-key")[0], 403, "管理员也签不出别人的令牌")

    def test_i_can_revoke_my_own_token_and_it_is_immediate(self):
        status, payload = self.revoke(ME, self.mine["token_id"])
        self.assertEqual((status, payload["effective"]), (200, "immediate"))
        record = kb_token.find_record(kb_token.load_registry(self.registry), self.mine["token_id"])
        self.assertTrue(record["revoked"])

    def test_i_cannot_revoke_somebody_elses(self):
        status, payload = self.revoke(ME, self.theirs["token_id"])
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        record = kb_token.find_record(kb_token.load_registry(self.registry), self.theirs["token_id"])
        self.assertFalse(record["revoked"])

    def test_an_administrator_can_revoke_anybody(self):
        self.make_admin(ME)
        self.assertEqual(self.revoke(ME, self.theirs["token_id"])[0], 200)

    def test_revoking_something_that_does_not_exist_is_a_404(self):
        self.assertEqual(self.revoke(ME, "tok-nope")[0], 404)


class GuardrailTests(TokenPageCase):
    def test_a_person_with_no_membership_is_told_what_to_do_first(self):
        status, payload = self.issue(MATE, "mate-key")
        self.assertEqual((status, payload["error"]), (409, "no_banks"))
        self.assertIn("先请管理员", payload["message"])
        self.assertEqual(self.records(), [])

    def test_the_same_purpose_cannot_hold_two_live_tokens(self):
        self.assertEqual(self.issue(ME, "my-key", agent="my-mac")[0], 200)
        status, payload = self.issue(ME, "my-key", agent="my-mac")
        self.assertEqual((status, payload["error"]), (409, "conflict"))

    def test_the_per_person_ceiling_still_applies(self):
        data = kb_token.load_registry(self.registry)
        data["max_active_per_owner"] = 2
        kb_token.save_registry(self.registry, data)
        self.assertEqual(self.issue(ME, "my-key", agent="one")[0], 200)
        self.assertEqual(self.issue(ME, "my-key", agent="two")[0], 200)
        status, payload = self.issue(ME, "my-key", agent="three")
        self.assertEqual((status, payload["error"]), (409, "conflict"))
        self.assertIn("上限", payload["message"])

    def test_a_write_without_the_page_marker_is_refused(self):
        self.assertEqual(self.issue(ME, "my-key", headers={})[0], 400)
        self.assertEqual(self.records(), [])

    def test_the_admin_face_has_no_token_page_routes(self):
        admin = kb_admin.AdminApp({"KB_ADMIN_FACE": "admin", "KB_ADMIN_ENABLED": "true",
                                   "KB_ADMIN_KEY_ENV": "RT069_KEY"})
        for method, route in (("GET", "/tokens"), ("GET", "/api/tokens"),
                              ("POST", "/api/tokens"), ("POST", "/api/tokens/revoke")):
            with self.subTest(route):
                self.assertEqual(admin.handle(method, route, WRITE, b"{}")[0], 404)

    def test_the_page_is_not_served_over_plaintext_in_production(self):
        app = kb_admin.AdminApp({
            "KB_ADMIN_FACE": "register", "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store), "KB_REGISTRY_PATH": str(self.registry),
            "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
        })
        status, payload, _ = app.handle("GET", "/tokens", {}, b"")
        self.assertEqual(status, 403)
        self.assertNotIn("<input", payload["html"])


if __name__ == "__main__":
    unittest.main()
