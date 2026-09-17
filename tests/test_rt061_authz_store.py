"""RT-061 判据：库、来源、成员、人员目录四张表的写面。

锁住的规则（rt-lite「两个已定的设计问题」与「验证」）：

- 所有权只在成员表里；可以有多位所有者；所有者不能降级或移除别的所有者；
  任何写入都不能让库没有所有者。
- 只读/可写成员不能管理成员；越权是 Forbidden，不是崩溃。
- 服务身份只能是只读成员，只有管理员能给它授权或移除它。
- 基于旧版本号的写入被拒绝；锁内重读，所以「至少一位所有者」看到的是最新数据。
- 每次变更都有回执，历史不因撤销而丢失；授权表里没有任何凭据。
- 判定直接用线上 ``adapters.kb_auth`` 的实现。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_authz as authz  # noqa: E402
from kb_identity import PersonIdentity  # noqa: E402
from kb_ledger import parse_iso  # noqa: E402

NOW = parse_iso("2026-09-17T09:00:00Z")
CORP = "1600000000000000002"
ALICE = PersonIdentity(CORP, "1700000000000000001", "张三")
BOB = PersonIdentity(CORP, "1700000000000000003", "李四")
CAROL = PersonIdentity(CORP, "1700000000000000005", "王五")
SERVICE = "service:rag-answer"
ADMIN = authz.OPS_ADMIN


def seeded() -> dict:
    data = authz.new_store(now=NOW)
    for person in (ALICE, BOB, CAROL):
        authz.upsert_person(data, person, now=NOW)
    authz.create_bank(data, actor=ADMIN, bank_id="bank-a", name="甲库", owner=ALICE.principal, now=NOW)
    return data


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.data = seeded()

    def test_creation_makes_exactly_one_owner_and_no_owner_field_on_the_bank(self):
        self.assertEqual(authz.owners_of(self.data, "bank-a"), [ALICE.principal])
        self.assertNotIn("owner", self.data["banks"]["bank-a"])
        self.assertEqual(self.data["banks"]["bank-a"]["created_by"], ADMIN)

    def test_owner_can_add_members_and_more_owners(self):
        authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, role="reader", now=NOW)
        authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=CAROL.principal, role="owner", now=NOW)
        self.assertEqual(sorted(authz.owners_of(self.data, "bank-a")), sorted([ALICE.principal, CAROL.principal]))

    def test_readers_and_writers_cannot_manage_members(self):
        authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=BOB.principal, role="writer", now=NOW)
        for action in (
            lambda: authz.set_member(self.data, actor=BOB.principal, bank_id="bank-a", principal=CAROL.principal, role="reader", now=NOW),
            lambda: authz.remove_member(self.data, actor=BOB.principal, bank_id="bank-a", principal=ALICE.principal, now=NOW),
            lambda: authz.archive_bank(self.data, actor=BOB.principal, bank_id="bank-a", now=NOW),
            lambda: authz.add_source(self.data, actor=BOB.principal, bank_id="bank-a", source_type="cwork",
                                     selector={"from": "2026-06-01", "to": "2026-09-01"}, now=NOW),
        ):
            with self.assertRaises(authz.Forbidden):
                action()

    def test_owner_cannot_demote_or_remove_another_owner(self):
        authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, role="owner", now=NOW)
        with self.assertRaises(authz.Forbidden):
            authz.set_member(self.data, actor=BOB.principal, bank_id="bank-a", principal=ALICE.principal, role="reader", now=NOW)
        with self.assertRaises(authz.Forbidden):
            authz.remove_member(self.data, actor=BOB.principal, bank_id="bank-a", principal=ALICE.principal, now=NOW)

    def test_transfer_is_add_then_self_demote(self):
        authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, role="owner", now=NOW)
        authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=ALICE.principal, role="reader", now=NOW)
        self.assertEqual(authz.owners_of(self.data, "bank-a"), [BOB.principal])

    def test_last_owner_cannot_leave_even_by_an_admin(self):
        with self.assertRaises(authz.ConflictError):
            authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=ALICE.principal, role="reader", now=NOW)
        with self.assertRaises(authz.ConflictError):
            authz.remove_member(self.data, actor=ADMIN, bank_id="bank-a", principal=ALICE.principal, now=NOW)
        with self.assertRaises(authz.ConflictError):
            authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=ALICE.principal, role="writer", now=NOW)

    def test_admin_may_demote_an_owner_when_another_remains(self):
        authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=BOB.principal, role="owner", now=NOW)
        authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=ALICE.principal, role="reader", now=NOW)
        self.assertEqual(authz.owners_of(self.data, "bank-a"), [BOB.principal])

    def test_unchanged_role_writes_nothing(self):
        version = self.data["version"]
        receipts = len(self.data["receipts"])
        self.assertFalse(authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=ALICE.principal, role="owner", now=NOW))
        self.assertEqual((self.data["version"], len(self.data["receipts"])), (version, receipts))

    def test_only_enrolled_people_can_be_granted(self):
        stranger = f"person:{CORP}:1799999999999999999"
        with self.assertRaises(authz.NotFound):
            authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=stranger, role="reader", now=NOW)
        with self.assertRaises(authz.NotFound):
            authz.create_bank(self.data, actor=ADMIN, bank_id="bank-b", name="乙库", owner=stranger, now=NOW)

    def test_only_admins_create_banks_for_now(self):
        with self.assertRaises(authz.Forbidden):
            authz.create_bank(self.data, actor=ALICE.principal, bank_id="bank-b", name="乙库", owner=ALICE.principal, now=NOW)


class ServiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.data = seeded()

    def test_service_is_reader_only_and_admin_managed(self):
        with self.assertRaises(authz.Forbidden):
            authz.set_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=SERVICE, role="reader", now=NOW)
        with self.assertRaises(authz.UsageError):
            authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=SERVICE, role="owner", now=NOW)
        authz.set_member(self.data, actor=ADMIN, bank_id="bank-a", principal=SERVICE, role="reader", now=NOW)
        with self.assertRaises(authz.Forbidden):
            authz.remove_member(self.data, actor=ALICE.principal, bank_id="bank-a", principal=SERVICE, now=NOW)

    def test_service_cannot_own_a_bank(self):
        with self.assertRaises(authz.UsageError):
            authz.create_bank(self.data, actor=ADMIN, bank_id="bank-b", name="乙库", owner=SERVICE, now=NOW)


class DecisionTests(unittest.TestCase):
    def test_decide_is_production_membership(self):
        data = seeded()
        authz.set_member(data, actor=ADMIN, bank_id="bank-a", principal=BOB.principal, role="reader", now=NOW)
        self.assertEqual(authz.decide(data, BOB.principal, "bank-a")[0], "ok")
        self.assertEqual(authz.decide(data, CAROL.principal, "bank-a"), ("forbidden", "not_a_member"))
        authz.remove_member(data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, now=NOW)
        self.assertEqual(authz.decide(data, BOB.principal, "bank-a")[0], "forbidden")
        authz.archive_bank(data, actor=ALICE.principal, bank_id="bank-a", now=NOW)
        self.assertEqual(authz.decide(data, ALICE.principal, "bank-a"), ("forbidden", "bank_inactive"))

    def test_list_banks_shows_role_and_hides_archived(self):
        data = seeded()
        authz.create_bank(data, actor=ADMIN, bank_id="bank-b", name="乙库", owner=BOB.principal, now=NOW)
        authz.set_member(data, actor=BOB.principal, bank_id="bank-b", principal=ALICE.principal, role="writer", now=NOW)
        self.assertEqual(
            [(b["bank_id"], b["role"]) for b in authz.list_banks(data, ALICE.principal)],
            [("bank-a", "owner"), ("bank-b", "writer")],
        )
        authz.archive_bank(data, actor=BOB.principal, bank_id="bank-b", now=NOW)
        self.assertEqual([b["bank_id"] for b in authz.list_banks(data, ALICE.principal)], ["bank-a"])
        with self.assertRaises(authz.ConflictError):
            authz.set_member(data, actor=ADMIN, bank_id="bank-b", principal=CAROL.principal, role="reader", now=NOW)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.data = seeded()

    def test_several_sources_of_the_same_type_each_get_a_stable_id(self):
        first = authz.add_source(self.data, actor=ALICE.principal, bank_id="bank-a", source_type="docdb",
                                 selector={"space_id": "1764536926399946754", "path": "/投前"}, now=NOW)
        second = authz.add_source(self.data, actor=ALICE.principal, bank_id="bank-a", source_type="docdb",
                                  selector={"space_id": "1764536926399940000", "path": "/"}, now=NOW)
        self.assertNotEqual(first, second)
        self.assertEqual(self.data["sources"][first]["selector"]["path"], "/投前")
        authz.remove_source(self.data, actor=ALICE.principal, source_id=first, now=NOW)
        self.assertEqual(list(self.data["sources"]), [second])

    def test_selectors_are_validated_per_type(self):
        bad = [
            ("docdb", {"space_id": 1764536926399946754}),
            ("docdb", {"space_id": "1", "path": "/a/../b"}),
            ("docdb", {"space_id": "1", "folder": "/"}),
            ("cwork", {"from": "2026-09-01", "to": "2026-06-01"}),
            ("cwork", {"from": "2026/06/01", "to": "2026-09-01"}),
            ("cwork", {"from": "2026-06-01", "to": "2026-09-01", "keywords": [""]}),
            ("upload", {"drop_dir": "/data/drop"}),
            ("nas", {"path": "/"}),
        ]
        for source_type, selector in bad:
            with self.subTest(source_type=source_type, selector=selector), self.assertRaises(authz.UsageError):
                authz.add_source(self.data, actor=ALICE.principal, bank_id="bank-a", source_type=source_type,
                                 selector=selector, now=NOW)

    def test_credentials_are_referenced_by_variable_name_only(self):
        with self.assertRaises(authz.UsageError):
            authz.add_source(self.data, actor=ALICE.principal, bank_id="bank-a", source_type="cwork",
                             selector={"from": "2026-06-01", "to": "2026-09-01"}, credential_env="sk-live-abc123")
        source_id = authz.add_source(self.data, actor=ALICE.principal, bank_id="bank-a", source_type="cwork",
                                     selector={"from": "2026-06-01", "to": "2026-09-01", "keywords": ["投前"]},
                                     credential_env="CWORK_APP_KEY", now=NOW)
        self.assertEqual(self.data["sources"][source_id]["credential_env"], "CWORK_APP_KEY")


class ReceiptTests(unittest.TestCase):
    def test_every_change_leaves_a_receipt_and_revocation_keeps_history(self):
        data = seeded()
        authz.set_member(data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, role="reader", now=NOW)
        authz.remove_member(data, actor=ALICE.principal, bank_id="bank-a", principal=BOB.principal, now=NOW)
        actions = [r["action"] for r in data["receipts"]]
        self.assertEqual(actions[-2:], ["grant", "revoke"])
        self.assertEqual(data["receipts"][-1]["details"]["role_before"], "reader")
        self.assertEqual(data["receipts"][-1]["version_after"], data["version"])
        versions = [r["version_after"] for r in data["receipts"]]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))
        for receipt in data["receipts"]:
            self.assertEqual(len(receipt["receipt_sha256"]), 64)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "kb-authz.json"
        data = seeded()
        authz.save_store(self.path, data, now=NOW)

    def test_file_is_0600_and_carries_no_credential_shaped_values(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        text = self.path.read_text("utf-8")
        for word in ("appKey", "token", "password", "secret"):
            self.assertNotIn(word, text)

    def test_stale_version_is_refused(self):
        version = authz.load_store(self.path)["version"]
        authz.mutate(self.path, lambda d: authz.set_member(
            d, actor=ADMIN, bank_id="bank-a", principal=BOB.principal, role="reader", now=NOW), expect_version=version)
        with self.assertRaises(authz.ConflictError):
            authz.mutate(self.path, lambda d: authz.set_member(
                d, actor=ADMIN, bank_id="bank-a", principal=CAROL.principal, role="reader", now=NOW), expect_version=version)
        self.assertIsNone(authz.role_of(authz.load_store(self.path), "bank-a", CAROL.principal))

    def test_concurrent_owner_exits_cannot_leave_the_bank_ownerless(self):
        """两位所有者同时退出：锁内重读，最多一人成功。"""
        authz.mutate(self.path, lambda d: authz.set_member(
            d, actor=ADMIN, bank_id="bank-a", principal=BOB.principal, role="owner", now=NOW))
        outcomes = []
        barrier = threading.Barrier(2)

        def leave(person):
            barrier.wait()
            try:
                authz.mutate(self.path, lambda d: authz.remove_member(
                    d, actor=person.principal, bank_id="bank-a", principal=person.principal, now=NOW))
                outcomes.append("left")
            except authz.ConflictError:
                outcomes.append("refused")

        threads = [threading.Thread(target=leave, args=(p,)) for p in (ALICE, BOB)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["left", "refused"])
        self.assertEqual(len(authz.owners_of(authz.load_store(self.path), "bank-a")), 1)

    def test_failed_change_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        with self.assertRaises(authz.Forbidden):
            authz.mutate(self.path, lambda d: authz.set_member(
                d, actor=BOB.principal, bank_id="bank-a", principal=CAROL.principal, role="reader", now=NOW))
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_store_is_an_error_not_an_empty_table(self):
        self.path.write_text("{}")
        with self.assertRaises(authz.StoreError):
            authz.load_store(self.path)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / "kb-authz.json")
        self.people = {"KEY_ALICE": ALICE, "KEY_BOB": BOB}

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = authz.main(list(argv), env={"KEY_ALICE": "alice-key", "KEY_BOB": "bob-key"},
                              resolver=lambda key: {"alice-key": ALICE, "bob-key": BOB}[key], now=NOW)
        return code, json.loads(out.getvalue())

    def test_end_to_end_enroll_create_grant_show(self):
        self.assertEqual(self.cli("init", "--store", self.store)[0], 0)
        self.assertEqual(self.cli("enroll", "--store", self.store, "--verify-env", "KEY_ALICE")[0], 0)
        self.assertEqual(self.cli("enroll", "--store", self.store, "--verify-env", "KEY_BOB")[0], 0)
        code, created = self.cli("bank-create", "--store", self.store, "--bank-id", "bank-a", "--name", "甲库",
                                 "--owner", ALICE.principal, "--service", SERVICE)
        self.assertEqual(code, 0, created)
        self.assertEqual({m["principal"]: m["role"] for m in created["members"]},
                         {ALICE.principal: "owner", SERVICE: "reader"})
        code, granted = self.cli("grant", "--store", self.store, "--bank-id", "bank-a", "--principal", BOB.principal,
                                 "--role", "reader", "--expect-version", str(created["version"]))
        self.assertEqual(code, 0, granted)
        code, shown = self.cli("show", "--store", self.store, "--principal", BOB.principal)
        self.assertEqual([b["bank_id"] for b in shown["banks"]], ["bank-a"])
        code, stale = self.cli("grant", "--store", self.store, "--bank-id", "bank-a", "--principal", BOB.principal,
                               "--role", "writer", "--expect-version", str(created["version"]))
        self.assertEqual((code, stale["error"]["kind"]), (2, "conflict"))

    def test_refusals_are_json(self):
        self.cli("init", "--store", self.store)
        code, payload = self.cli("grant", "--store", self.store, "--bank-id", "nope", "--principal", ALICE.principal, "--role", "reader")
        self.assertEqual((code, payload["ok"], payload["error"]["kind"]), (2, False, "not_found"))
        code, payload = self.cli("enroll", "--store", self.store, "--app-key", "alice-key")
        self.assertEqual(code, 2)
        self.assertNotIn("alice-key", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
