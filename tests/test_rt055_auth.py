"""RT-055 synthetic-only auth contract; no OPS, real corpus, or credentials."""
from __future__ import annotations
import hashlib, json, os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import sys
from pathlib import Path as _Path

ROOT = _Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.kb_auth import authorize


class AuthDecisionTests(unittest.TestCase):
    def registry(self, root: Path, token: str, *, bank="cwork-3m", expires="2099-01-01T00:00:00Z", revoked=False):
        path = root / "tokens.json"
        path.write_text(json.dumps({
            "schema": "cwk.kb.token-registry.v1", "tokens": [{
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "token_id": "tok-synthetic", "kb_ids": [bank],
                "expires_at": expires, "revoked": revoked,
            }],
        }))
        return path

    def check(self, headers, bank, path, enabled="true"):
        with patch.dict(os.environ, {"RAG_AUTH_ENABLED": enabled, "RAG_AUTH_REGISTRY": str(path)}, clear=False):
            return authorize(headers, bank)

    def test_valid_token_passes(self):
        with TemporaryDirectory() as td:
            p = self.registry(Path(td), "synthetic-valid")
            self.assertIsNone(self.check({"X-KB-Token": "synthetic-valid"}, "cwork-3m", p))

    def test_missing_and_invalid_tokens_are_401(self):
        with TemporaryDirectory() as td:
            p = self.registry(Path(td), "synthetic-valid")
            for headers in ({}, {"X-KB-Token": "synthetic-invalid"}):
                result = self.check(headers, "cwork-3m", p)
                self.assertEqual(result[0], 401)

    def test_expired_token_is_401(self):
        with TemporaryDirectory() as td:
            p = self.registry(Path(td), "synthetic-expired", expires="2000-01-01T00:00:00Z")
            self.assertEqual(self.check({"X-KB-Token": "synthetic-expired"}, "cwork-3m", p)[0], 401)

    def test_valid_token_outside_scope_is_403(self):
        with TemporaryDirectory() as td:
            p = self.registry(Path(td), "synthetic-cross", bank="docdb-touqian")
            result = self.check({"X-KB-Token": "synthetic-cross"}, "cwork-3m", p)
            self.assertEqual(result[0], 403)

    def test_disabled_auth_does_not_read_registry(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "does-not-exist.json"
            self.assertIsNone(self.check({}, "cwork-3m", p, enabled="false"))

    def test_registry_is_reread_for_revocation(self):
        with TemporaryDirectory() as td:
            root = Path(td); p = self.registry(root, "synthetic-revoke")
            headers = {"X-KB-Token": "synthetic-revoke"}
            self.assertIsNone(self.check(headers, "cwork-3m", p))
            data = json.loads(p.read_text()); data["tokens"][0]["revoked"] = True; p.write_text(json.dumps(data))
            self.assertEqual(self.check(headers, "cwork-3m", p)[0], 401)


if __name__ == "__main__":
    unittest.main()
