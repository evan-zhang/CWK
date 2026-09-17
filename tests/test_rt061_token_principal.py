"""RT-061 判据：令牌记下"这是谁"，额度与 Agent 绑定跟着人走，服务有自己的身份。

- 探针返回人员身份时，记录带 ``principal``；只返回标签的旧探针仍可签 listed 令牌。
- ``--authz grants`` 必须有人员身份，否则拒签（fail-closed）。
- 同一个人的两把 Key：共用一个额度，同一个 Agent 不能同时有两支有效令牌（A3）。
- RT-061 之前按 Key 绑定签出的令牌，换代时能被找到并作废。
- 服务令牌：主体是 ``service:<名>``，不占任何人的额度，一个服务同时只有一支有效令牌。
- 新令牌在 RT-061 之前的读侧（只认 kb_ids）照常工作——回滚不会让它们全体 401。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT))

import kb_token as tokens  # noqa: E402
from adapters import kb_auth  # noqa: E402
from kb_identity import PersonIdentity  # noqa: E402
from kb_ledger import parse_iso  # noqa: E402

NOW = parse_iso("2026-09-17T09:00:00Z")
ALICE = PersonIdentity("1600000000000000002", "1700000000000000001", "张三")
KEYS = {"KEY_A1": "alice-first-key", "KEY_A2": "alice-second-key", "KEY_LEGACY": "legacy-key"}


def person_probe(app_key):
    if app_key in ("alice-first-key", "alice-second-key"):
        return ALICE
    if app_key == "legacy-key":
        return "fake:label-only"
    raise RuntimeError("rejected")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "tokens.json"
        self.cli("init", "--registry", str(self.registry), "--max-active", "2")

    def cli(self, *argv, expect=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = tokens.main(list(argv), probe=person_probe, now=NOW, env=KEYS)
        payload = json.loads(out.getvalue())
        self.assertEqual(code, expect, payload)
        return payload

    def issue(self, key, agent, *extra, expect=0):
        return self.cli("issue", "--registry", str(self.registry), "--verify-env", key, "--agent-id", agent,
                        "--kb-id", "bank-a", *extra, expect=expect)

    def data(self):
        return tokens.load_registry(self.registry)


class PrincipalTests(Case):
    def test_person_probe_puts_the_principal_on_the_record(self):
        payload = self.issue("KEY_A1", "agent-1", "--label", "张三的写作助手")
        self.assertEqual(payload["record"]["principal"], ALICE.principal)
        self.assertEqual(payload["record"]["authz"], "listed")
        self.assertEqual(payload["record"]["label"], "张三的写作助手")
        self.assertEqual(payload["identity"]["person_name"], "张三")
        self.assertNotIn("alice-first-key", self.registry.read_text("utf-8"))

    def test_label_only_probe_still_issues_listed_tokens(self):
        payload = self.issue("KEY_LEGACY", "agent-1")
        self.assertEqual(payload["record"]["principal"], "")

    def test_grants_token_requires_a_person(self):
        payload = self.issue("KEY_LEGACY", "agent-1", "--authz", "grants", expect=2)
        self.assertEqual(payload["error"]["kind"], "identity")
        self.assertEqual(tokens.records(self.data()), [])

    def test_label_with_control_chars_is_refused(self):
        self.issue("KEY_A1", "agent-1", "--label", "bad\nlabel", expect=2)


class OnePersonManyKeysTests(Case):
    def test_second_key_cannot_mint_a_second_live_token_for_the_same_agent(self):
        self.issue("KEY_A1", "agent-1")
        payload = self.issue("KEY_A2", "agent-1", expect=2)
        self.assertEqual(payload["error"]["kind"], "conflict")

    def test_ceiling_counts_the_person_not_the_key(self):
        self.issue("KEY_A1", "agent-1")
        self.issue("KEY_A2", "agent-2")
        payload = self.issue("KEY_A1", "agent-3", expect=2)
        self.assertIn("上限", payload["error"]["message"])

    def test_reissue_with_the_other_key_supersedes_the_first(self):
        first = self.issue("KEY_A1", "agent-1", "--authz", "grants")
        payload = self.cli("reissue", "--registry", str(self.registry), "--verify-env", "KEY_A2", "--agent-id", "agent-1")
        self.assertEqual(payload["superseded_token_ids"], [first["record"]["token_id"]])
        self.assertEqual(payload["record"]["authz"], "grants", "未显式给出时沿用上一代")

    def test_pre_rt061_binding_is_found_on_reissue(self):
        data = self.data()
        legacy = tokens.VerifiedIdentity(owner_ref=tokens.derive_owner_ref(data["owner_ref_salt"], "alice-first-key"))
        record, _ = tokens.issue_token(data, identity=legacy, raw_agent_id="agent-1", kb_ids=["bank-a"], now=NOW)
        tokens.save_registry(self.registry, data, now=NOW)
        payload = self.cli("reissue", "--registry", str(self.registry), "--verify-env", "KEY_A1", "--agent-id", "agent-1")
        self.assertEqual(payload["superseded_token_ids"], [record["token_id"]])
        self.assertEqual(payload["record"]["principal"], ALICE.principal)
        self.assertNotEqual(payload["record"]["agent_binding_id"], record["agent_binding_id"])


class ServiceTokenTests(Case):
    def test_service_token_has_its_own_principal_and_no_ceiling(self):
        self.issue("KEY_A1", "agent-1")
        self.issue("KEY_A1", "agent-2")
        payload = self.cli("issue-service", "--registry", str(self.registry), "--service", "rag-answer",
                           "--kb-id", "bank-a", "--kb-id", "bank-b")
        record = payload["record"]
        self.assertEqual((record["principal"], record["token_kind"], record["owner_ref"]),
                         ("service:rag-answer", "service", ""))
        self.assertEqual(len(payload["token"]), 64)

    def test_one_live_token_per_service_and_rotation_supersedes(self):
        first = self.cli("issue-service", "--registry", str(self.registry), "--service", "rag-answer", "--kb-id", "bank-a")
        self.cli("issue-service", "--registry", str(self.registry), "--service", "rag-answer", "--kb-id", "bank-a", expect=2)
        rotated = self.cli("rotate-service", "--registry", str(self.registry), "--service", "rag-answer")
        self.assertEqual(rotated["superseded_token_ids"], [first["record"]["token_id"]])
        self.assertEqual(rotated["record"]["generation"], 2)
        self.assertEqual(rotated["record"]["kb_ids"], ["bank-a"])

    def test_service_name_is_validated(self):
        self.cli("issue-service", "--registry", str(self.registry), "--service", "RAG Answer", "--kb-id", "bank-a", expect=2)


class RollbackCompatibilityTests(Case):
    def test_new_tokens_work_under_the_pre_rt061_reader(self):
        """回滚到只认 kb_ids 的读侧时，新令牌按快照工作，而不是全体 401。"""
        payload = self.issue("KEY_A1", "agent-1", "--authz", "grants")
        plaintext = payload["token"]
        env = {"RAG_AUTH_ENABLED": "true", "RAG_AUTH_REGISTRY": str(self.registry)}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("RAG_AUTHZ_MODE", None)
            self.assertIsNone(kb_auth.authorize({"X-KB-Token": plaintext}, "bank-a"))
            self.assertEqual(kb_auth.authorize({"X-KB-Token": plaintext}, "bank-b")[0], 403)
        registry = json.loads(self.registry.read_text("utf-8"))
        self.assertEqual(registry["schema"], "cwk.kb.token-registry.v1", "schema 版本不升，旧读侧才读得懂")
        self.assertEqual(
            registry["tokens"][0]["token_sha256"], hashlib.sha256(plaintext.encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
