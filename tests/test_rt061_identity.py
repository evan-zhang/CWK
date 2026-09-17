"""RT-061 判据：从玄关只借身份——人员 ID、组织、真名，其余一概不信。

玄关的真实形状（2026-09-17 只读实测）：Key 无效时 HTTP 仍是 200，
``resultCode`` 是 401；成功是 1。人员 ID 与组织 ID 是 19 位数字字符串。
测试用注入的 transport，不联网、不含真实 Key。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_identity as identity  # noqa: E402

KEY = "synthetic-business-key-do-not-log"
PERSONAL = "1764536926399946754"
PERSON = "1700000000000000001"
CORP = "1600000000000000002"


def rows(**overrides):
    personal = {"id": PERSONAL, "type": "personal", "createBy": PERSON, "creator": "张三", "corpId": CORP, "role": 1}
    personal.update(overrides)
    shared = {"id": "1764536926399940000", "type": "common", "createBy": "1799999999999999999", "creator": "李四", "corpId": CORP}
    return [shared, personal]


class FakeTransport:
    def __init__(self, *, personal=PERSONAL, listing=None, code=1, fail=None):
        self.personal = personal
        self.listing = rows() if listing is None else listing
        self.code = code
        self.fail = fail
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.fail is not None:
            raise self.fail
        if url.endswith(identity.PERSONAL_PROJECT_PATH):
            data = self.personal
        elif url.endswith(identity.PROJECT_LIST_PATH):
            data = self.listing
        else:
            raise AssertionError(url)
        body = {"resultCode": self.code, "resultMsg": None if self.code == 1 else "Token校验失败", "data": data if self.code == 1 else None}
        return json.dumps(body).encode("utf-8")


class ResolvePersonTests(unittest.TestCase):
    def test_personal_space_creator_is_the_person(self):
        transport = FakeTransport()
        person = identity.resolve_person(KEY, transport=transport, base="https://example.invalid/open-api")
        self.assertEqual((person.corp_id, person.person_id, person.name), (CORP, PERSON, "张三"))
        self.assertEqual(person.principal, f"person:{CORP}:{PERSON}")

    def test_key_travels_only_as_the_appkey_header(self):
        transport = FakeTransport()
        identity.resolve_person(KEY, transport=transport, base="https://example.invalid/open-api")
        for url, headers in transport.calls:
            self.assertNotIn(KEY, url)
            self.assertEqual(headers["appKey"], KEY)

    def test_rejected_key_is_a_refusal_and_the_message_never_carries_the_key(self):
        with self.assertRaises(identity.IdentityResolutionError) as raised:
            identity.resolve_person(KEY, transport=FakeTransport(code=401), base="https://example.invalid")
        self.assertIn("401", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))

    def test_network_and_http_failures_are_refusals(self):
        for failure in (urllib.error.URLError("down"), TimeoutError(), OSError("reset"),
                        urllib.error.HTTPError("https://x", 502, "bad gateway", {}, None)):
            with self.assertRaises(identity.IdentityResolutionError):
                identity.resolve_person(KEY, transport=FakeTransport(fail=failure), base="https://example.invalid")

    def test_ambiguity_is_never_guessed_away(self):
        cases = {
            "no personal row": [r for r in rows() if r["type"] != "personal"],
            "two personal rows": rows() + [dict(rows()[1])],
            "numeric person id": rows(createBy=1700000000000000001),
            "empty name": rows(creator="  "),
            "control char in name": rows(creator="张\n三"),
            "missing corp": rows(corpId=None),
        }
        for label, listing in cases.items():
            with self.subTest(label), self.assertRaises(identity.IdentityResolutionError):
                identity.resolve_person(KEY, transport=FakeTransport(listing=listing), base="https://example.invalid")

    def test_personal_id_must_be_a_digit_string(self):
        for bad in (None, 1764536926399946754, "", "abc"):
            with self.subTest(bad), self.assertRaises(identity.IdentityResolutionError):
                identity.resolve_person(KEY, transport=FakeTransport(personal=bad), base="https://example.invalid")

    def test_non_json_body_is_a_refusal(self):
        def transport(url, headers, timeout):
            return b"<html>gateway</html>"

        with self.assertRaises(identity.IdentityResolutionError):
            identity.resolve_person(KEY, transport=transport, base="https://example.invalid")

    def test_empty_key_is_refused_before_any_call(self):
        transport = FakeTransport()
        with self.assertRaises(identity.IdentityResolutionError):
            identity.resolve_person("", transport=transport)
        self.assertEqual(transport.calls, [])


class WhoamiCliTests(unittest.TestCase):
    def run_cli(self, argv, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = identity.main(argv, env=env, transport=FakeTransport())
        return code, json.loads(out.getvalue())

    def test_whoami_reads_the_key_by_variable_name(self):
        code, payload = self.run_cli(["whoami", "--verify-env", "SYNTH_KEY"], {"SYNTH_KEY": KEY})
        self.assertEqual(code, 0)
        self.assertEqual(payload["principal"], f"person:{CORP}:{PERSON}")
        self.assertNotIn(KEY, json.dumps(payload, ensure_ascii=False))

    def test_unset_variable_is_a_json_refusal(self):
        code, payload = self.run_cli(["whoami", "--verify-env", "SYNTH_KEY"], {})
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_key_value_on_the_command_line_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, payload = self.run_cli(["whoami", "--app-key", KEY], {})
        self.assertEqual(code, 2)
        self.assertNotIn(KEY, json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
