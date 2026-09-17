"""RT-061 判据：线上鉴权的两种判定模式。

``RAG_AUTHZ_MODE=scope``（默认）必须与 RT-061 之前逐字节相同；
``grants`` 下由成员表决定，而旧令牌仍被自己的 ``kb_ids`` 收窄——切换只可能收紧。
全部合成夹具，不碰 OPS。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import kb_auth  # noqa: E402

ALICE = "person:1600000000000000002:1700000000000000001"
BOB = "person:1600000000000000002:1700000000000000003"
SERVICE = "service:rag-answer"


def token_row(token, **fields):
    row = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "token_id": f"tok-{token}",
        "expires_at": "2099-01-01T00:00:00Z",
        "revoked": False,
    }
    row.update(fields)
    return row


def store(banks=("bank-a", "bank-b"), grants=(), archived=()):
    return {
        "schema": kb_auth.AUTHZ_SCHEMA,
        "banks": {b: {"name": b, "status": "archived" if b in archived else "active"} for b in banks},
        "grants": [{"bank_id": b, "principal": p, "role": r} for b, p, r in grants],
    }


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "tokens.json"
        self.authz = self.root / "kb-authz.json"

    def write(self, rows, authz=None):
        self.registry.write_text(json.dumps({"schema": kb_auth.REGISTRY_SCHEMA, "tokens": rows}))
        if authz is not None:
            self.authz.write_text(json.dumps(authz))

    def check(self, token, bank, *, mode=None, authz_path=True):
        env = {"RAG_AUTH_ENABLED": "true", "RAG_AUTH_REGISTRY": str(self.registry)}
        if mode is not None:
            env["RAG_AUTHZ_MODE"] = mode
        if authz_path:
            env["RAG_AUTHZ_PATH"] = str(self.authz)
        with patch.dict(os.environ, env, clear=False):
            if mode is None:
                os.environ.pop("RAG_AUTHZ_MODE", None)
            result = kb_auth.authorize({"X-KB-Token": token}, bank)
        return 200 if result is None else result[0]


class ScopeModeIsUnchangedTests(Harness):
    def test_default_mode_ignores_the_membership_table_entirely(self):
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")],
                   authz=store(grants=[("bank-b", ALICE, "reader")]))
        self.assertEqual(self.check("t", "bank-a"), 200)
        self.assertEqual(self.check("t", "bank-b"), 403)
        self.authz.write_text("not json")
        self.assertEqual(self.check("t", "bank-a"), 200, "scope 模式不应读成员表")

    def test_typo_in_the_switch_fails_closed(self):
        self.write([token_row("t", kb_ids=["bank-a"])], authz=store())
        self.assertEqual(self.check("t", "bank-a", mode="grant"), 401)


class GrantsModeTests(Harness):
    def test_new_token_follows_membership_live(self):
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")],
                   authz=store(grants=[("bank-a", ALICE, "reader")]))
        self.assertEqual(self.check("t", "bank-b", mode="grants"), 403)
        self.authz.write_text(json.dumps(store(grants=[("bank-a", ALICE, "reader"), ("bank-b", ALICE, "reader")])))
        self.assertEqual(self.check("t", "bank-b", mode="grants"), 200, "加成员后下一次请求即生效，令牌没动")
        self.authz.write_text(json.dumps(store(grants=[("bank-b", ALICE, "reader")])))
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 403, "收回后下一次请求即失效")

    def test_legacy_token_is_narrowed_by_its_own_scope(self):
        self.write([token_row("old", kb_ids=["bank-a"], principal=ALICE)],
                   authz=store(grants=[("bank-a", ALICE, "owner"), ("bank-b", ALICE, "owner")]))
        self.assertEqual(self.check("old", "bank-a", mode="grants"), 200)
        self.assertEqual(self.check("old", "bank-b", mode="grants"), 403, "旧令牌不能因为人有权限而扩权")

    def test_every_role_can_read(self):
        for role in ("owner", "writer", "reader"):
            self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")],
                       authz=store(grants=[("bank-a", ALICE, role)]))
            self.assertEqual(self.check("t", "bank-a", mode="grants"), 200, role)

    def test_unknown_role_is_not_membership(self):
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")],
                   authz=store(grants=[("bank-a", ALICE, "admin")]))
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 403)

    def test_token_without_principal_is_403(self):
        self.write([token_row("t", kb_ids=["bank-a"])], authz=store(grants=[("bank-a", ALICE, "reader")]))
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 403)

    def test_archived_or_unknown_bank_is_403_for_everyone(self):
        self.write([token_row("t", kb_ids=["bank-a", "bank-z"], principal=ALICE, authz="grants"),
                    token_row("s", kb_ids=["bank-a"], token_kind="shared_kb")],
                   authz=store(grants=[("bank-a", ALICE, "owner")], archived=("bank-a",)))
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 403)
        self.assertEqual(self.check("t", "bank-z", mode="grants"), 403)
        self.assertEqual(self.check("s", "bank-a", mode="grants"), 403)

    def test_shared_token_keeps_its_single_bank(self):
        self.write([token_row("s", kb_ids=["bank-a"], token_kind="shared_kb")], authz=store())
        self.assertEqual(self.check("s", "bank-a", mode="grants"), 200)
        self.assertEqual(self.check("s", "bank-b", mode="grants"), 403)

    def test_membership_table_failure_fails_closed(self):
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")], authz=store())
        for broken in ("not json", json.dumps({"schema": "other"}), json.dumps({"schema": kb_auth.AUTHZ_SCHEMA})):
            self.authz.write_text(broken)
            self.assertEqual(self.check("t", "bank-a", mode="grants"), 401)
        self.authz.unlink()
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 401)
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants")])
        self.assertEqual(self.check("t", "bank-a", mode="grants", authz_path=False), 401)

    def test_token_validity_still_comes_first(self):
        self.write([token_row("t", kb_ids=["bank-a"], principal=ALICE, authz="grants", revoked=True),
                    token_row("e", kb_ids=["bank-a"], principal=ALICE, authz="grants", expires_at="2000-01-01T00:00:00Z")],
                   authz=store(grants=[("bank-a", ALICE, "reader")]))
        self.assertEqual(self.check("t", "bank-a", mode="grants"), 401)
        self.assertEqual(self.check("e", "bank-a", mode="grants"), 401)
        self.assertEqual(self.check("nobody", "bank-a", mode="grants"), 401)


class SwitchOnlyNarrowsTests(unittest.TestCase):
    def test_grants_mode_never_admits_a_legacy_token_scope_mode_refuses(self):
        """穷举：任意成员表 × 任意旧令牌范围，grants 放行的集合 ⊆ scope 放行的集合。"""
        banks = ("bank-a", "bank-b", "bank-c")
        scopes = [list(c) for n in range(1, 4) for c in itertools.combinations(banks, n)]
        memberships = list(itertools.product((None, "reader", "owner"), repeat=len(banks)))
        for scope, membership, principal in itertools.product(scopes, memberships, (ALICE, "")):
            grants = [(b, ALICE, r) for b, r in zip(banks, membership) if r]
            authz = store(banks=banks, grants=grants)
            record = {"kb_ids": scope, "principal": principal}
            for bank in banks + ("bank-x",):
                scope_ok = kb_auth.record_verdict(record, bank, mode="scope", authz=authz)[0] == "ok"
                grants_ok = kb_auth.record_verdict(record, bank, mode="grants", authz=authz)[0] == "ok"
                if grants_ok:
                    self.assertTrue(scope_ok, (scope, membership, principal, bank))


if __name__ == "__main__":
    unittest.main()
