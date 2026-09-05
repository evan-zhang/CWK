"""RT-047 P2 判据：Agent 实例级绑定 token 的签发、吊销、换代与登记表。

判据（RT-047 P2 rt-lite）：

P2-1  身份只从验证过的业务 Key 派生：``--verify-env`` 指向环境变量名，Key
      过认证面校验才算数；未设置、为空、校验失败一律拒签。**没有
      ``--owner-ref``，也不许有**——自报身份的 argv 在 argparse 之前就被驳回。
P2-2  token 本体 256 bit，只在签发时出现一次；登记表里只有 sha256 摘要与元数据，
      整个文件 grep 不到明文。
P2-3  revoke 即刻生效（不缓存），reissue 代际递增且旧代在同一次写入里全部失效。
P2-4  ``list`` 只交出元数据与指纹，不交出 token、也不交出摘要。
P2-5  max_active 是「每用户跨 Agent 实例」的上限。

身份验证面用 fake probe 注入：真实探针会调用公司 Skill 的 ``CWorkClient``，
测试里既不该联网也不该有业务 Key。被注入的 probe 同时是断言点——它记录自己
收到的值，于是「派生用的确实是那把 Key」是被观察到的，而不是被声明的。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_token as tokens  # noqa: E402
from kb_ledger import iso, parse_iso  # noqa: E402

BIZ_ENV = "RT047_TEST_BUSINESS_KEY"
BIZ_KEY = "emp-8801-业务密钥-do-not-log"
OTHER_KEY = "emp-9902-另一个人的密钥"
KB_WORK = "libraries/工作库"
KB_OTHER = "libraries/合同库"
AGENT = "ops-mac-01"

NOW = parse_iso("2026-09-05T12:00:00Z")


# ── test doubles ────────────────────────────────────────────────────────────


class FakeProbe:
    """Stands in for the authenticated CWork read.

    Records what it was handed, so a test can assert the key that got hashed
    into ``owner_ref`` is the key that just proved itself — the whole of the
    铁律 rests on those being the same value.
    """

    def __init__(self, *, fail: Optional[Exception] = None, label: str = "fake:probe") -> None:
        self.fail = fail
        self.label = label
        self.calls: List[str] = []

    def __call__(self, app_key: str) -> str:
        self.calls.append(app_key)
        if self.fail is not None:
            raise self.fail
        return self.label


class RegistryCase(unittest.TestCase):
    """A temp registry plus a CLI runner wired to the fake probe."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "tokens.json"
        self.probe = FakeProbe()
        self.env = {BIZ_ENV: BIZ_KEY}

    # -- helpers ------------------------------------------------------------

    def cli(
        self,
        argv: Sequence[str],
        *,
        probe: Optional[FakeProbe] = None,
        now: Optional[object] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tokens.main(
                list(argv),
                probe=probe or self.probe,
                now=now or NOW,
                env=self.env if env is None else env,
            )
        return code, json.loads(out.getvalue()), err.getvalue()

    def init(self, *extra: str) -> dict:
        code, payload, _err = self.cli(["init", "--registry", str(self.registry), *extra])
        self.assertEqual(code, 0, payload)
        return payload

    def issue(self, *extra: str, agent: str = AGENT, kb: str = KB_WORK, **kw) -> dict:
        argv = [
            "issue",
            "--registry",
            str(self.registry),
            "--verify-env",
            BIZ_ENV,
            "--agent-id",
            agent,
            "--kb-id",
            kb,
            *extra,
        ]
        code, payload, _err = self.cli(argv, **kw)
        self.assertEqual(code, 0, payload)
        return payload

    def data(self) -> dict:
        return tokens.load_registry(self.registry)

    def raw_text(self) -> str:
        return self.registry.read_text("utf-8")


# ── P2-1: identity ──────────────────────────────────────────────────────────


class IdentityTests(RegistryCase):
    """P2-1 — owner_ref 只能从过了认证面的业务 Key 派生。"""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.salt = self.data()["owner_ref_salt"]

    def test_a_passing_probe_yields_an_owner_ref_derived_from_that_key(self) -> None:
        identity = tokens.verify_business_key(
            BIZ_ENV, salt_hex=self.salt, env=self.env, probe=self.probe, now=NOW
        )
        self.assertEqual(self.probe.calls, [BIZ_KEY])
        self.assertEqual(identity.owner_ref, tokens.derive_owner_ref(self.salt, BIZ_KEY))
        self.assertEqual(identity.basis, tokens.OWNER_REF_BASIS)
        self.assertEqual(identity.verified_at, iso(NOW))

    def test_the_owner_ref_never_contains_the_key(self) -> None:
        identity = tokens.verify_business_key(
            BIZ_ENV, salt_hex=self.salt, env=self.env, probe=self.probe, now=NOW
        )
        self.assertNotIn(BIZ_KEY, identity.owner_ref)
        self.assertTrue(identity.owner_ref.startswith(tokens.OWNER_REF_PREFIX))

    def test_two_keys_are_two_people_and_one_key_is_one_person(self) -> None:
        mine = tokens.derive_owner_ref(self.salt, BIZ_KEY)
        again = tokens.derive_owner_ref(self.salt, BIZ_KEY)
        theirs = tokens.derive_owner_ref(self.salt, OTHER_KEY)
        self.assertEqual(mine, again)
        self.assertNotEqual(mine, theirs)

    def test_the_same_key_at_two_installations_is_not_correlatable(self) -> None:
        other_salt = tokens.new_registry(now=NOW)["owner_ref_salt"]
        self.assertNotEqual(
            tokens.derive_owner_ref(self.salt, BIZ_KEY),
            tokens.derive_owner_ref(other_salt, BIZ_KEY),
        )

    def test_an_unset_variable_is_refused_not_defaulted(self) -> None:
        with self.assertRaises(tokens.IdentityError) as caught:
            tokens.verify_business_key(
                "RT047_ABSENT", salt_hex=self.salt, env=self.env, probe=self.probe, now=NOW
            )
        self.assertIn("RT047_ABSENT", str(caught.exception))
        self.assertEqual(self.probe.calls, [])

    def test_an_empty_variable_is_refused(self) -> None:
        with self.assertRaises(tokens.IdentityError):
            tokens.verify_business_key(
                BIZ_ENV, salt_hex=self.salt, env={BIZ_ENV: ""}, probe=self.probe, now=NOW
            )
        self.assertEqual(self.probe.calls, [])

    def test_an_empty_verify_env_is_refused(self) -> None:
        with self.assertRaises(tokens.IdentityError):
            tokens.verify_business_key(
                "", salt_hex=self.salt, env=self.env, probe=self.probe, now=NOW
            )

    def test_a_failing_probe_refuses_to_issue(self) -> None:
        probe = FakeProbe(fail=RuntimeError("HTTP 401 unauthorized"))
        with self.assertRaises(tokens.IdentityError) as caught:
            tokens.verify_business_key(
                BIZ_ENV, salt_hex=self.salt, env=self.env, probe=probe, now=NOW
            )
        self.assertIn("401", str(caught.exception))

    def test_a_probe_that_echoes_the_key_has_it_redacted(self) -> None:
        # API clients really do put the credential in the exception text.
        probe = FakeProbe(fail=RuntimeError(f"auth failed for appKey={BIZ_KEY}"))
        with self.assertRaises(tokens.IdentityError) as caught:
            tokens.verify_business_key(
                BIZ_ENV, salt_hex=self.salt, env=self.env, probe=probe, now=NOW
            )
        message = str(caught.exception)
        self.assertNotIn(BIZ_KEY, message)
        self.assertIn("redacted", message)

    def test_a_failing_probe_does_not_chain_the_original_exception(self) -> None:
        # ``raise ... from None``: a chained traceback would print the probe's
        # own message a second time, past the redaction.
        probe = FakeProbe(fail=RuntimeError(f"appKey={BIZ_KEY}"))
        with self.assertRaises(tokens.IdentityError) as caught:
            tokens.verify_business_key(
                BIZ_ENV, salt_hex=self.salt, env=self.env, probe=probe, now=NOW
            )
        self.assertIsNone(caught.exception.__cause__)

    def test_issue_refuses_when_the_probe_fails_and_writes_nothing(self) -> None:
        before = self.raw_text()
        probe = FakeProbe(fail=RuntimeError("网络不可达"))
        code, payload, err = self.cli(
            [
                "issue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                AGENT,
                "--kb-id",
                KB_WORK,
            ],
            probe=probe,
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "identity")
        self.assertIn("kb_token 失败", err)
        self.assertEqual(self.raw_text(), before)

    def test_the_agent_id_is_hashed_never_stored(self) -> None:
        issued = self.issue(agent="laptop-of-evan.local")
        self.assertNotIn("laptop-of-evan.local", self.raw_text())
        self.assertTrue(
            issued["record"]["agent_binding_id"].startswith(tokens.BINDING_PREFIX)
        )

    def test_two_people_naming_their_agent_the_same_do_not_collide(self) -> None:
        mine = tokens.derive_agent_binding_id(self.salt, "owner-aaa", AGENT)
        theirs = tokens.derive_agent_binding_id(self.salt, "owner-bbb", AGENT)
        self.assertNotEqual(mine, theirs)

    def test_a_junk_agent_id_is_refused(self) -> None:
        for junk in ("", " ", "-leading-dash", "has space", "n" * 300, "分号;注入"):
            with self.subTest(junk=junk):
                with self.assertRaises(tokens.UsageError):
                    tokens.validate_agent_id(junk)


class SelfAssertedIdentityTests(RegistryCase):
    """P2-1 铁律 — 自报身份的 argv 在 argparse 之前就被驳回。"""

    def test_the_parser_has_no_owner_ref_flag_at_all(self) -> None:
        parser = tokens.build_parser()
        flags = {
            option
            for action in getattr(parser, "_subparsers", None)._group_actions[0].choices[  # type: ignore[union-attr]
                "issue"
            ]._actions
            for option in action.option_strings
        }
        for forbidden in tokens.SELF_ASSERTED_IDENTITY_FLAGS:
            self.assertNotIn(forbidden, flags)

    def test_each_self_asserted_flag_is_refused_with_the_rule_explained(self) -> None:
        for flag in tokens.SELF_ASSERTED_IDENTITY_FLAGS:
            with self.subTest(flag=flag):
                with self.assertRaises(tokens.SelfAssertedIdentity) as caught:
                    tokens.assert_no_self_asserted_identity(["issue", flag, "owner-forged"])
                self.assertIn("owner_ref", str(caught.exception))

    def test_the_equals_form_is_refused_too(self) -> None:
        with self.assertRaises(tokens.SelfAssertedIdentity):
            tokens.assert_no_self_asserted_identity(["issue", "--owner-ref=owner-forged"])

    def test_a_plaintext_business_key_on_argv_is_refused(self) -> None:
        for flag in tokens.FORBIDDEN_KEY_VALUE_FLAGS:
            with self.subTest(flag=flag):
                with self.assertRaises(tokens.SelfAssertedIdentity):
                    tokens.assert_no_plaintext_business_key(["issue", flag, BIZ_KEY])

    def test_the_shared_credential_flag_guard_still_applies(self) -> None:
        self.init()
        code, payload, _err = self.cli(
            ["list", "--registry", str(self.registry), "--token", "leaked"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "PlaintextCredential")

    def test_main_refuses_self_asserted_identity_in_json(self) -> None:
        self.init()
        code, payload, _err = self.cli(
            [
                "issue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                AGENT,
                "--kb-id",
                KB_WORK,
                "--owner-ref",
                "owner-forged",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "self_asserted_identity")
        self.assertEqual(self.probe.calls, [])

    def test_issue_without_verify_env_cannot_even_parse(self) -> None:
        self.init()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                tokens.build_parser().parse_args(
                    ["issue", "--registry", str(self.registry), "--agent-id", AGENT,
                     "--kb-id", KB_WORK]
                )


# ── the registry file ───────────────────────────────────────────────────────


class RegistryFileTests(RegistryCase):
    def test_init_creates_an_empty_registry_at_0600(self) -> None:
        payload = self.init()
        self.assertEqual(payload["membership_epoch"], 0)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.registry).st_mode), tokens.REGISTRY_MODE
        )
        data = self.data()
        self.assertEqual(data["tokens"], [])
        self.assertEqual(data["receipts"], [])
        self.assertGreaterEqual(len(bytes.fromhex(data["owner_ref_salt"])), tokens.SALT_BYTES)

    def test_init_refuses_to_overwrite_an_existing_registry(self) -> None:
        self.init()
        before = self.raw_text()
        code, payload, _err = self.cli(["init", "--registry", str(self.registry)])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "conflict")
        self.assertEqual(self.raw_text(), before)

    def test_a_missing_registry_is_a_refusal_with_the_fix_in_it(self) -> None:
        with self.assertRaises(tokens.RegistryError) as caught:
            tokens.load_registry(self.registry)
        self.assertIn("init", str(caught.exception))

    def test_a_corrupt_registry_is_refused_not_repaired(self) -> None:
        self.registry.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(tokens.RegistryError):
            tokens.load_registry(self.registry)

    def test_a_foreign_schema_is_refused(self) -> None:
        self.registry.write_text(
            json.dumps({"schema": "something.else", "tokens": []}), encoding="utf-8"
        )
        with self.assertRaises(tokens.RegistryError):
            tokens.load_registry(self.registry)

    def test_a_short_or_junk_salt_is_refused(self) -> None:
        self.init()
        good = self.data()
        for salt in ("", "zz", "ab" * 8):
            with self.subTest(salt=salt):
                self.registry.write_text(
                    json.dumps({**good, "owner_ref_salt": salt}), encoding="utf-8"
                )
                with self.assertRaises(tokens.RegistryError):
                    tokens.load_registry(self.registry)

    def test_saving_keeps_the_mode_and_leaves_no_temp_file(self) -> None:
        self.init()
        self.issue()
        self.assertEqual(
            stat.S_IMODE(os.stat(self.registry).st_mode), tokens.REGISTRY_MODE
        )
        leftovers = [p.name for p in self.registry.parent.iterdir() if p.name.startswith(".kb-token.")]
        self.assertEqual(leftovers, [])

    def test_max_active_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(tokens.UsageError):
                    tokens.new_registry(now=NOW, max_active_per_owner=bad)


# ── P2-2: issue ─────────────────────────────────────────────────────────────


class IssueTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()

    def test_the_token_is_256_bits_of_hex_shown_once(self) -> None:
        payload = self.issue()
        token = payload["token"]
        self.assertEqual(len(token), tokens.TOKEN_HEX_LEN)
        self.assertEqual(len(bytes.fromhex(token)), tokens.TOKEN_BYTES)
        self.assertTrue(payload["token_shown_once"])

    def test_two_issues_never_produce_the_same_token(self) -> None:
        first = self.issue(agent="agent-a")["token"]
        second = self.issue(agent="agent-b")["token"]
        self.assertNotEqual(first, second)

    def test_P2_2_the_registry_holds_no_plaintext_token_anywhere(self) -> None:
        """grep 判据：整份登记表里找不到 token 明文，只找得到摘要。"""
        minted = [self.issue(agent=f"agent-{n}")["token"] for n in range(3)]
        blob = self.raw_text()
        for token in minted:
            with self.subTest(token=token[:8]):
                self.assertNotIn(token, blob)
                self.assertIn(tokens.token_digest(token), blob)

    def test_the_record_carries_the_digest_and_the_response_does_not(self) -> None:
        payload = self.issue()
        record = payload["record"]
        self.assertNotIn("token_sha256", record)
        # The plaintext appears exactly once in the whole response — as the
        # top-level ``token`` field — and nowhere inside the record.
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(blob.count(payload["token"]), 1)
        self.assertNotIn(payload["token"], json.dumps(record, ensure_ascii=False))
        stored = self.data()["tokens"][0]
        self.assertEqual(stored["token_sha256"], tokens.token_digest(payload["token"]))
        self.assertEqual(stored["token_id"], tokens.token_id_for(stored["token_sha256"]))

    def test_the_business_key_never_reaches_the_registry_or_the_response(self) -> None:
        payload = self.issue()
        self.assertNotIn(BIZ_KEY, self.raw_text())
        self.assertNotIn(BIZ_KEY, json.dumps(payload, ensure_ascii=False))

    def test_the_default_ttl_is_ninety_days(self) -> None:
        payload = self.issue()
        expires = parse_iso(payload["record"]["expires_at"])
        self.assertEqual(expires - NOW, timedelta(days=tokens.DEFAULT_TTL_DAYS))

    def test_an_out_of_range_ttl_is_refused(self) -> None:
        for bad in ("0", "-5", str(tokens.MAX_TTL_DAYS + 1)):
            with self.subTest(bad=bad):
                code, payload, _err = self.cli(
                    [
                        "issue",
                        "--registry",
                        str(self.registry),
                        "--verify-env",
                        BIZ_ENV,
                        "--agent-id",
                        AGENT,
                        "--kb-id",
                        KB_WORK,
                        "--ttl-days",
                        bad,
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(payload["error"]["kind"], "usage")

    def test_the_first_generation_is_one_and_the_epoch_moves(self) -> None:
        payload = self.issue()
        self.assertEqual(payload["record"]["generation"], 1)
        self.assertEqual(payload["membership_epoch"], 1)

    def test_a_second_token_for_the_same_agent_is_a_conflict_pointing_at_reissue(self) -> None:
        self.issue()
        code, payload, _err = self.cli(
            [
                "issue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                AGENT,
                "--kb-id",
                KB_WORK,
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "conflict")
        self.assertIn("reissue", payload["error"]["message"])
        self.assertEqual(len(self.data()["tokens"]), 1)

    def test_kb_ids_are_deduplicated_and_order_preserved(self) -> None:
        self.assertEqual(
            tokens.validate_kb_ids([KB_WORK, KB_OTHER, KB_WORK]), [KB_WORK, KB_OTHER]
        )

    def test_an_empty_or_control_character_kb_id_is_refused(self) -> None:
        for bad in ([], [""], ["  "], ["库\x00名"], ["x" * 300]):
            with self.subTest(bad=bad):
                with self.assertRaises(tokens.UsageError):
                    tokens.validate_kb_ids(bad)

    def test_an_issue_receipt_is_written_and_self_hashed(self) -> None:
        self.issue()
        receipt = self.data()["receipts"][-1]
        self.assertEqual(receipt["action"], "issue")
        self.assertEqual(receipt["membership_epoch_before"], 0)
        self.assertEqual(receipt["membership_epoch_after"], 1)
        body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        self.assertEqual(tokens.canonical_sha256(body), receipt["receipt_sha256"])

    def test_the_receipt_carries_no_token_and_no_digest(self) -> None:
        payload = self.issue()
        receipt = self.data()["receipts"][-1]
        blob = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(payload["token"], blob)
        self.assertNotIn(tokens.token_digest(payload["token"]), blob)


class MaxActivePerOwnerTests(RegistryCase):
    """P2-5 — 上限是「每用户跨 Agent 实例」，不是每设备。"""

    def test_the_ceiling_counts_across_agent_instances(self) -> None:
        self.init("--max-active", "2")
        self.issue(agent="agent-a")
        self.issue(agent="agent-b")
        code, payload, _err = self.cli(
            [
                "issue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                "agent-c",
                "--kb-id",
                KB_WORK,
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "conflict")
        self.assertIn("上限 2", payload["error"]["message"])

    def test_revoking_one_frees_a_slot(self) -> None:
        self.init("--max-active", "1")
        first = self.issue(agent="agent-a")
        code, _payload, _err = self.cli(
            ["revoke", "--registry", str(self.registry), "--token-id", first["record"]["token_id"]]
        )
        self.assertEqual(code, 0)
        self.issue(agent="agent-b")

    def test_another_person_has_their_own_ceiling(self) -> None:
        self.init("--max-active", "1")
        self.issue(agent="agent-a")
        code, payload, _err = self.cli(
            [
                "issue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                "agent-a",
                "--kb-id",
                KB_WORK,
            ],
            env={BIZ_ENV: OTHER_KEY},
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.data()["tokens"]), 2)


# ── P2-3: revoke and reissue ────────────────────────────────────────────────


class RevokeTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.issued = self.issue()
        self.token = self.issued["token"]
        self.token_id = self.issued["record"]["token_id"]

    def revoke(self, token_id: Optional[str] = None) -> Tuple[int, dict, str]:
        return self.cli(
            [
                "revoke",
                "--registry",
                str(self.registry),
                "--token-id",
                token_id or self.token_id,
                "--actor",
                "evan",
                "--reason",
                "笔记本丢了",
            ]
        )

    def test_revoke_is_immediate_and_says_so(self) -> None:
        code, payload, _err = self.revoke()
        self.assertEqual(code, 0)
        self.assertEqual(payload["effective"], "immediate")
        self.assertEqual(payload["record"]["status"], tokens.STATUS_REVOKED)
        self.assertTrue(payload["record"]["revoked"])

    def test_a_revoked_token_stops_deciding_ok(self) -> None:
        self.assertTrue(tokens.decide(self.data(), self.token, kb_id=KB_WORK, now=NOW).ok)
        self.revoke()
        decision = tokens.decide(self.data(), self.token, kb_id=KB_WORK, now=NOW)
        self.assertFalse(decision.ok)
        self.assertEqual(decision.reason, "revoked")

    def test_revoke_takes_a_fingerprint_not_a_token(self) -> None:
        code, payload, _err = self.revoke(token_id=self.token)
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "not_found")

    def test_revoking_twice_is_a_conflict_not_a_silent_no_op(self) -> None:
        self.revoke()
        epoch = self.data()["membership_epoch"]
        code, payload, _err = self.revoke()
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "conflict")
        self.assertEqual(self.data()["membership_epoch"], epoch)

    def test_an_unknown_token_id_is_not_found(self) -> None:
        code, payload, _err = self.revoke(token_id="tok-0000000000000000")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "not_found")

    def test_revoke_bumps_the_epoch_and_writes_a_receipt(self) -> None:
        before = self.data()["membership_epoch"]
        self.revoke()
        data = self.data()
        self.assertEqual(data["membership_epoch"], before + 1)
        receipt = data["receipts"][-1]
        self.assertEqual(receipt["action"], "revoke")
        self.assertEqual(receipt["actor"], "evan")
        self.assertEqual(receipt["reason"], "笔记本丢了")

    def test_revoke_needs_no_business_key_at_all(self) -> None:
        code, _payload, _err = self.cli(
            ["revoke", "--registry", str(self.registry), "--token-id", self.token_id],
            env={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.probe.calls, [BIZ_KEY])  # only the issue call


class ReissueTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.first = self.issue()

    def reissue(self, *extra: str, **kw) -> Tuple[int, dict, str]:
        return self.cli(
            [
                "reissue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                AGENT,
                *extra,
            ],
            **kw,
        )

    def test_the_generation_advances_and_the_old_one_dies(self) -> None:
        code, payload, _err = self.reissue()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["record"]["generation"], 2)
        self.assertEqual(payload["superseded_token_ids"], [self.first["record"]["token_id"]])

        data = self.data()
        old = tokens.decide(data, self.first["token"], kb_id=KB_WORK, now=NOW)
        new = tokens.decide(data, payload["token"], kb_id=KB_WORK, now=NOW)
        self.assertEqual((old.status, old.reason), ("unauthorized", "revoked"))
        self.assertTrue(new.ok)

    def test_every_older_generation_is_dead_not_just_the_previous_one(self) -> None:
        second = self.reissue()[1]
        third = self.reissue()[1]
        data = self.data()
        for dead in (self.first["token"], second["token"]):
            with self.subTest(token=dead[:8]):
                self.assertFalse(tokens.decide(data, dead, kb_id=KB_WORK, now=NOW).ok)
        self.assertTrue(tokens.decide(data, third["token"], kb_id=KB_WORK, now=NOW).ok)
        self.assertEqual(third["record"]["generation"], 3)

    def test_one_reissue_is_one_epoch_bump_for_both_halves(self) -> None:
        before = self.data()["membership_epoch"]
        payload = self.reissue()[1]
        self.assertEqual(payload["membership_epoch"], before + 1)
        data = self.data()
        killed = tokens.find_record(data, self.first["record"]["token_id"])
        self.assertEqual(killed["membership_epoch"], before + 1)

    def test_no_reader_can_see_two_live_generations(self) -> None:
        # One ``os.replace`` per mutation: whatever a concurrent reader loads,
        # exactly one generation of this binding is active in it.
        self.reissue()
        data = self.data()
        binding = self.first["record"]["agent_binding_id"]
        live = [
            row
            for row in tokens.binding_records(data, binding)
            if tokens.record_status(row, NOW) == tokens.STATUS_ACTIVE
        ]
        self.assertEqual(len(live), 1)

    def test_the_scope_is_inherited_when_not_restated(self) -> None:
        payload = self.reissue()[1]
        self.assertEqual(payload["record"]["kb_ids"], [KB_WORK])

    def test_the_scope_can_be_narrowed_or_widened_explicitly(self) -> None:
        payload = self.reissue("--kb-id", KB_OTHER)[1]
        self.assertEqual(payload["record"]["kb_ids"], [KB_OTHER])
        data = self.data()
        self.assertFalse(tokens.decide(data, payload["token"], kb_id=KB_WORK, now=NOW).ok)
        self.assertTrue(tokens.decide(data, payload["token"], kb_id=KB_OTHER, now=NOW).ok)

    def test_reissue_without_a_prior_token_says_use_issue(self) -> None:
        code, payload, _err = self.cli(
            [
                "reissue",
                "--registry",
                str(self.registry),
                "--verify-env",
                BIZ_ENV,
                "--agent-id",
                "never-seen-agent",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "not_found")
        self.assertIn("issue", payload["error"]["message"])

    def test_reissue_still_requires_a_verified_key(self) -> None:
        probe = FakeProbe(fail=RuntimeError("HTTP 403"))
        before = self.raw_text()
        code, payload, _err = self.reissue(probe=probe)
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "identity")
        self.assertEqual(self.raw_text(), before)

    def test_a_different_person_cannot_reissue_someone_elses_binding(self) -> None:
        code, payload, _err = self.reissue(env={BIZ_ENV: OTHER_KEY})
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["kind"], "not_found")


# ── P2-4: list ──────────────────────────────────────────────────────────────


class ListTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.work = self.issue(agent="agent-a", kb=KB_WORK)
        self.other = self.issue(agent="agent-b", kb=KB_OTHER)

    def listing(self, *extra: str) -> dict:
        code, payload, _err = self.cli(["list", "--registry", str(self.registry), *extra])
        self.assertEqual(code, 0, payload)
        return payload

    def test_P2_4_list_shows_metadata_and_a_fingerprint_only(self) -> None:
        payload = self.listing()
        blob = json.dumps(payload, ensure_ascii=False)
        for issued in (self.work, self.other):
            self.assertNotIn(issued["token"], blob)
            self.assertNotIn(tokens.token_digest(issued["token"]), blob)
        for row in payload["tokens"]:
            self.assertNotIn("token", row)
            self.assertNotIn("token_sha256", row)
            self.assertTrue(row["token_id"].startswith(tokens.TOKEN_ID_PREFIX))
            self.assertIn(row["status"], (tokens.STATUS_ACTIVE, tokens.STATUS_REVOKED, tokens.STATUS_EXPIRED))

    def test_list_needs_no_business_key(self) -> None:
        code, payload, _err = self.cli(["list", "--registry", str(self.registry)], env={})
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 2)

    def test_list_can_be_filtered_by_library(self) -> None:
        payload = self.listing("--kb-id", KB_WORK)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tokens"][0]["token_id"], self.work["record"]["token_id"])

    def test_list_can_be_filtered_by_status(self) -> None:
        self.cli(
            ["revoke", "--registry", str(self.registry), "--token-id", self.work["record"]["token_id"]]
        )
        self.assertEqual(self.listing("--status", tokens.STATUS_REVOKED)["count"], 1)
        self.assertEqual(self.listing("--status", tokens.STATUS_ACTIVE)["count"], 1)

    def test_list_reports_the_epoch_and_the_ceiling(self) -> None:
        payload = self.listing()
        self.assertEqual(payload["membership_epoch"], self.data()["membership_epoch"])
        self.assertEqual(payload["max_active_per_owner"], tokens.DEFAULT_MAX_ACTIVE_PER_OWNER)


# ── the read side the gateway uses ──────────────────────────────────────────


class DecideTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.issued = self.issue()
        self.token = self.issued["token"]

    def test_a_live_in_scope_token_is_ok(self) -> None:
        decision = tokens.decide(self.data(), self.token, kb_id=KB_WORK, now=NOW)
        self.assertTrue(decision.ok)
        self.assertEqual(decision.reason, "authorized")
        self.assertEqual(decision.token_id, self.issued["record"]["token_id"])
        self.assertEqual(decision.owner_ref, self.issued["record"]["owner_ref"])

    def test_an_out_of_scope_library_is_forbidden_not_unauthorized(self) -> None:
        decision = tokens.decide(self.data(), self.token, kb_id=KB_OTHER, now=NOW)
        self.assertTrue(decision.forbidden)
        self.assertEqual(decision.reason, "kb_not_in_scope")

    def test_an_empty_kb_id_is_forbidden_rather_than_a_wildcard(self) -> None:
        decision = tokens.decide(self.data(), self.token, kb_id="", now=NOW)
        self.assertTrue(decision.forbidden)

    def test_an_expired_token_is_unauthorized(self) -> None:
        later = NOW + timedelta(days=tokens.DEFAULT_TTL_DAYS + 1)
        decision = tokens.decide(self.data(), self.token, kb_id=KB_WORK, now=later)
        self.assertEqual((decision.status, decision.reason), ("unauthorized", "expired"))

    def test_expiry_is_exclusive_at_the_boundary(self) -> None:
        edge = NOW + timedelta(days=tokens.DEFAULT_TTL_DAYS)
        self.assertFalse(tokens.decide(self.data(), self.token, kb_id=KB_WORK, now=edge).ok)
        self.assertTrue(
            tokens.decide(
                self.data(), self.token, kb_id=KB_WORK, now=edge - timedelta(seconds=1)
            ).ok
        )

    def test_a_record_without_a_parseable_expiry_is_treated_as_expired(self) -> None:
        data = self.data()
        data["tokens"][0]["expires_at"] = "not-a-date"
        self.assertEqual(tokens.record_status(data["tokens"][0], NOW), tokens.STATUS_EXPIRED)

    def test_unknown_empty_and_near_miss_tokens_are_all_unauthorized(self) -> None:
        near_miss = ("0" if self.token[0] != "0" else "1") + self.token[1:]
        for presented in ("", "not-a-token", near_miss, self.token.upper()):
            with self.subTest(presented=presented[:12]):
                decision = tokens.decide(self.data(), presented, kb_id=KB_WORK, now=NOW)
                self.assertEqual((decision.status, decision.reason), ("unauthorized", "unknown_token"))
                self.assertEqual(decision.owner_ref, "")

    def test_a_truncated_stored_digest_can_never_match(self) -> None:
        data = self.data()
        data["tokens"][0]["token_sha256"] = tokens.token_digest(self.token)[:16]
        self.assertFalse(tokens.decide(data, self.token, kb_id=KB_WORK, now=NOW).ok)


class TokenFileTests(RegistryCase):
    """The gateway's handle: no cache, fail closed."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.issued = self.issue()
        self.handle = tokens.TokenFile(self.registry)

    def test_P2_3_a_revocation_lands_on_the_very_next_lookup(self) -> None:
        self.assertTrue(self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW).ok)
        self.cli(
            ["revoke", "--registry", str(self.registry), "--token-id", self.issued["record"]["token_id"]]
        )
        decision = self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW)
        self.assertEqual((decision.status, decision.reason), ("unauthorized", "revoked"))

    def test_a_missing_registry_refuses_everyone(self) -> None:
        self.registry.unlink()
        decision = self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW)
        self.assertEqual((decision.status, decision.reason), ("unauthorized", "registry_unreadable"))

    def test_a_corrupt_registry_refuses_everyone(self) -> None:
        self.registry.write_text("{ truncated", encoding="utf-8")
        decision = self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW)
        self.assertEqual(decision.status, "unauthorized")
        self.assertFalse(decision.ok)

    def test_a_registry_that_comes_back_starts_working_again(self) -> None:
        blob = self.registry.read_bytes()
        self.registry.unlink()
        self.assertFalse(self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW).ok)
        self.registry.write_bytes(blob)
        self.assertTrue(self.handle.decide(self.issued["token"], kb_id=KB_WORK, now=NOW).ok)

    def test_the_summary_counts_without_naming_anybody(self) -> None:
        summary = self.handle.summary(now=NOW)
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["membership_epoch"], 1)
        self.assertEqual(summary["max_active_per_owner"], tokens.DEFAULT_MAX_ACTIVE_PER_OWNER)
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(self.issued["token"], blob)
        self.assertNotIn(self.issued["record"]["owner_ref"], blob)

    def test_the_summary_of_a_broken_registry_raises_so_startup_can_refuse(self) -> None:
        self.registry.unlink()
        with self.assertRaises(tokens.TokenError):
            self.handle.summary(now=NOW)


# ── output contract ─────────────────────────────────────────────────────────


class JsonContractTests(RegistryCase):
    def test_every_verb_answers_parseable_json_on_stdout(self) -> None:
        # init / issue / list / revoke / reissue, then two failures.
        self.init()
        issued = self.issue()
        probes: List[Tuple[List[str], int]] = [
            (["list", "--registry", str(self.registry)], 0),
            (
                ["revoke", "--registry", str(self.registry), "--token-id", issued["record"]["token_id"]],
                0,
            ),
            (
                [
                    "reissue",
                    "--registry",
                    str(self.registry),
                    "--verify-env",
                    BIZ_ENV,
                    "--agent-id",
                    AGENT,
                ],
                0,
            ),
            (["revoke", "--registry", str(self.registry), "--token-id", "tok-nope"], 2),
            (["list", "--registry", str(self.registry) + ".absent"], 2),
        ]
        for argv, expected in probes:
            with self.subTest(argv=argv[0]):
                code, payload, _err = self.cli(argv)
                self.assertEqual(code, expected, payload)
                self.assertIsInstance(payload, dict)
                self.assertIn("ok", payload)
                self.assertIs(payload["ok"], expected == 0)
                self.assertTrue(str(payload["schema"]).startswith("cwk.kb.token"))

    def test_a_failure_also_prints_a_human_line_on_stderr(self) -> None:
        code, _payload, err = self.cli(["list", "--registry", str(self.registry)])
        self.assertEqual(code, 2)
        self.assertIn("kb_token 失败", err)

    def test_the_module_never_writes_to_a_default_global_registry(self) -> None:
        # ``--registry`` is required on every verb: there is no ambient
        # location a mistyped command could quietly create or clobber.
        parser = tokens.build_parser()
        for verb in ("init", "issue", "reissue", "revoke", "list"):
            with self.subTest(verb=verb):
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args([verb])


class ProbeWiringTests(unittest.TestCase):
    """The real probe is read-only and lazily imported."""

    SOURCE = (PROJECT / "scripts" / "kb_token.py").read_text("utf-8")

    def probe_body(self) -> str:
        return self.SOURCE.split("def cwork_probe")[1].split("\ndef ")[0]

    def test_the_probe_is_the_cheapest_authenticated_read(self) -> None:
        self.assertIn("get_todo_list(page_index=1, page_size=1)", self.probe_body())

    def test_the_probe_calls_no_cwork_write_verb(self) -> None:
        """CWK 红线：只读采集——探针不许标已读、不许回复、不许办待办。"""
        body = self.probe_body()
        for forbidden in ("mark_read", "reply", "send", "complete_todo", "delete", "update"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(f".{forbidden}(", body)

    def test_the_client_is_imported_lazily_not_at_module_scope(self) -> None:
        """The company Skill ships outside this repo.

        ``revoke`` and ``list`` must keep working on a machine that has never
        installed it, so the import lives inside :func:`cwork_probe` rather
        than at module scope.  Checked over the AST because the module
        docstring names the same module in prose.
        """
        import ast

        tree = ast.parse(self.SOURCE)
        top_level: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top_level.append(node.module or "")
        self.assertNotIn("cwk_backfill_range", top_level)
        self.assertIn("from cwk_backfill_range import _inbox_client", self.probe_body())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
