"""RT-061 判据：从现有令牌迁移到成员表，切换当天行为零变化。

夹具按线上现状（2026-09-17 只读核对）搭：登记表里所有令牌都由同一把 OPS Key 签发，
两支有效（一支是问答服务用的），三支已吊销。再加两种线上没有、但迁移必须扛住的情况：
只覆盖部分库的令牌（复核 A1 担心的扩权场景）和单库共享令牌。

- 归属只靠「重算得出」：owner_ref 必须能由通过玄关核实的 Key 重新派生，
  服务归属还要 binding 能由给定的 Agent 标识重新派生。没有任何手填身份。
- 有一支有效令牌证明不了归属，整个迁移不执行，两个文件都不动。
- 迁移后逐支令牌 × 逐个库，scope 与 grants 两种判定完全一致；成员表改坏了，核对会发现。
- 迁移可重复执行；dry-run 什么都不写。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT))

import kb_authz as authz  # noqa: E402
import kb_token as tokens  # noqa: E402
from kb_identity import IdentityResolutionError, PersonIdentity  # noqa: E402
from kb_ledger import parse_iso  # noqa: E402

NOW = parse_iso("2026-09-17T09:00:00Z")
BANKS = ["cwork-3m", "docdb-touqian", "spbp-2027"]
OPS_OWNER = PersonIdentity("1600000000000000002", "1700000000000000001", "管理员")
COLLEAGUE = PersonIdentity("1600000000000000002", "1700000000000000003", "同事")
KEYS = {"OPS_KEY": "ops-business-key", "COLLEAGUE_KEY": "colleague-business-key", "BAD_KEY": "rejected-key"}
PEOPLE = {"ops-business-key": OPS_OWNER, "colleague-business-key": COLLEAGUE}
SERVICE_AGENT = "rt055-internal-rag"


def resolver(app_key):
    if app_key in PEOPLE:
        return PEOPLE[app_key]
    raise IdentityResolutionError("玄关拒绝了这把 Key（resultCode=401）")


def legacy_issue(data, key, agent, kb_ids, **kind):
    """A token exactly as pre-RT-061 kb_token wrote it: no principal, no authz marker."""
    identity = tokens.VerifiedIdentity(owner_ref=tokens.derive_owner_ref(data["owner_ref_salt"], key))
    record, _ = tokens.issue_token(data, identity=identity, raw_agent_id=agent, kb_ids=kb_ids, now=NOW)
    record.pop("authz", None)
    return record


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = root / "rt055-tokens.json"
        self.store = root / "kb-authz.json"
        data = tokens.new_registry(now=NOW)
        data["max_active_per_owner"] = 10
        self.main_token = legacy_issue(data, "ops-business-key", "chat-main-agent", BANKS)
        self.service_token = legacy_issue(data, "ops-business-key", SERVICE_AGENT, BANKS)
        for n in range(3):
            old = legacy_issue(data, "ops-business-key", f"retired-{n}", ["spbp-2027"])
            tokens.revoke_token(data, token_id=old["token_id"], now=NOW)
        self.customize(data)
        tokens.save_registry(self.registry, data, now=NOW)

    def customize(self, data):
        """Subclasses add tokens here."""

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = authz.main(list(argv), env=KEYS, resolver=resolver, now=NOW)
        return code, json.loads(out.getvalue())

    def migrate(self, *extra):
        return self.cli("migrate", "--registry", str(self.registry), "--store", str(self.store),
                        "--verify-env", "OPS_KEY", "--owner-env", "OPS_KEY",
                        "--service", f"rag-answer={SERVICE_AGENT}", *extra)


class ProductionShapeTests(MigrationCase):
    def test_migration_is_behaviour_preserving(self):
        code, payload = self.migrate()
        self.assertEqual(code, 0, payload)
        report = payload["equivalence"]
        self.assertTrue(report["equivalent"], report["differences"])
        self.assertEqual(report["compared"], 2 * 3, "两支有效令牌 × 三个库")
        self.assertEqual(payload["applied"]["tokens_bound"], 2)

    def test_service_token_becomes_a_service_not_the_admin(self):
        self.migrate()
        registry = tokens.load_registry(self.registry)
        principals = {r["token_id"]: r.get("principal", "") for r in tokens.records(registry)}
        self.assertEqual(principals[self.main_token["token_id"]], OPS_OWNER.principal)
        self.assertEqual(principals[self.service_token["token_id"]], "service:rag-answer")
        store = authz.load_store(self.store)
        for bank in BANKS:
            self.assertEqual(authz.owners_of(store, bank), [OPS_OWNER.principal])
            self.assertEqual(authz.role_of(store, bank, "service:rag-answer"), "reader")

    def test_revoked_tokens_are_left_alone(self):
        self.migrate()
        for record in tokens.records(tokens.load_registry(self.registry)):
            if record.get("revoked"):
                self.assertNotIn("principal", record)

    def test_migration_is_idempotent(self):
        self.migrate()
        grants_before = authz.load_store(self.store)["grants"]
        code, payload = self.migrate()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["applied"]["tokens_bound"], 0)
        self.assertEqual(payload["applied"]["grants_added"], 0)
        self.assertEqual(authz.load_store(self.store)["grants"], grants_before)

    def test_dry_run_writes_nothing(self):
        registry_before = self.registry.read_bytes()
        code, payload = self.migrate("--dry-run")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(payload["plan"]["bindings"]), 2)
        self.assertFalse(self.store.exists())
        self.assertEqual(self.registry.read_bytes(), registry_before)

    def test_output_never_carries_a_business_key(self):
        _, payload = self.migrate()
        text = json.dumps(payload, ensure_ascii=False)
        for key in KEYS.values():
            self.assertNotIn(key, text)

    def test_health_check_is_clean_after_migration(self):
        self.migrate()
        code, payload = self.cli("check", "--registry", str(self.registry), "--store", str(self.store))
        self.assertEqual(code, 0, payload)

    def test_equivalence_check_catches_a_broken_membership_table(self):
        self.migrate()
        authz.mutate(self.store, lambda d: authz.remove_member(
            d, actor=authz.OPS_ADMIN, bank_id="docdb-touqian", principal="service:rag-answer", now=NOW))
        code, payload = self.cli("check-equivalence", "--registry", str(self.registry), "--store", str(self.store))
        self.assertEqual(code, 3)
        self.assertEqual([(d["token_id"], d["bank_id"]) for d in payload["differences"]],
                         [(self.service_token["token_id"], "docdb-touqian")])
        code, health = self.cli("check", "--registry", str(self.registry), "--store", str(self.store))
        self.assertEqual(code, 3)
        self.assertIn("service_not_granted", {p["kind"] for p in health["problems"]})


class PartialScopeTests(MigrationCase):
    """A1：同一个人名下有一支只读一部分库的令牌，迁移后不能扩权。"""

    def customize(self, data):
        self.narrow = legacy_issue(data, "ops-business-key", "narrow-agent", ["spbp-2027"])
        self.shared, _ = tokens.issue_shared_token(data, kb_id="docdb-touqian", now=NOW)

    def test_person_level_grants_do_not_widen_a_narrow_token(self):
        code, payload = self.migrate()
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["equivalence"]["equivalent"], payload["equivalence"]["differences"])
        self.assertEqual(payload["equivalence"]["compared"], 4 * 3)
        store = authz.load_store(self.store)
        self.assertEqual(authz.role_of(store, "cwork-3m", OPS_OWNER.principal), "owner",
                         "这个人对 cwork-3m 有权限，但 narrow-agent 那支令牌依然读不了它")


class AttributionRefusalTests(MigrationCase):
    def customize(self, data):
        self.foreign = legacy_issue(data, "someone-elses-key", "their-agent", ["cwork-3m"])

    def test_unprovable_token_stops_the_whole_migration(self):
        registry_before = self.registry.read_bytes()
        code, payload = self.migrate()
        self.assertEqual((code, payload["error"]["kind"]), (2, "conflict"))
        self.assertIn(self.foreign["token_id"], payload["error"]["message"])
        self.assertFalse(self.store.exists())
        self.assertEqual(self.registry.read_bytes(), registry_before)

    def test_a_key_that_does_not_match_proves_nothing(self):
        code, payload = self.cli("migrate", "--registry", str(self.registry), "--store", str(self.store),
                                 "--verify-env", "OPS_KEY", "--verify-env", "COLLEAGUE_KEY", "--owner-env", "OPS_KEY",
                                 "--service", f"rag-answer={SERVICE_AGENT}")
        self.assertEqual(code, 2, "同事的 Key 能过玄关，但重算不出那支令牌的 owner_ref")
        self.assertIn(self.foreign["token_id"], payload["error"]["message"])


class BadInputTests(MigrationCase):
    def test_rejected_key_is_refused(self):
        code, payload = self.cli("migrate", "--registry", str(self.registry), "--store", str(self.store),
                                 "--verify-env", "BAD_KEY", "--owner-env", "BAD_KEY")
        self.assertEqual(code, 2)
        self.assertNotIn("rejected-key", json.dumps(payload, ensure_ascii=False))

    def test_wrong_service_agent_id_is_refused(self):
        code, payload = self.cli("migrate", "--registry", str(self.registry), "--store", str(self.store),
                                 "--verify-env", "OPS_KEY", "--owner-env", "OPS_KEY", "--service", "rag-answer=typo-agent")
        self.assertEqual(code, 2)
        self.assertIn("rag-answer", payload["error"]["message"])
        self.assertFalse(self.store.exists())

    def test_owner_must_be_one_of_the_verified_keys(self):
        code, payload = self.cli("migrate", "--registry", str(self.registry), "--store", str(self.store),
                                 "--verify-env", "OPS_KEY", "--owner-env", "COLLEAGUE_KEY",
                                 "--service", f"rag-answer={SERVICE_AGENT}")
        self.assertEqual((code, payload["error"]["kind"]), (2, "usage"))


if __name__ == "__main__":
    unittest.main()
