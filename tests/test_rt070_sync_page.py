"""RT-070 判据：页面上的手工同步。

同步改的是"所有人能查到什么"，所以它的门槛要和成员管理一样硬：
本人身份 + 管理员 + 页面标记头。另外它是一条会跑一两分钟的流水线，
所以还要挡住并发——两次同时跑会互相覆盖索引。
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
from kb_identity import PersonIdentity  # noqa: E402

CORP = "1600000000000000002"
BOSS = PersonIdentity(CORP, "1700000000000000001", "管理员")
MATE = PersonIdentity(CORP, "1700000000000000003", "同事")
WRITE = {kb_admin.MEMBER_WRITE_HEADER: "1"}


class SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "kb-authz.json"
        kb_authz.init_store(self.store)
        data = kb_authz.load_store(self.store)
        for person in (BOSS, MATE):
            kb_authz.upsert_person(data, person)
        kb_authz.set_admin(data, actor=kb_authz.OPS_ADMIN, principal=BOSS.principal, admin=True)
        kb_authz.save_store(self.store, data)
        self.meta = self.root / "meta.json"
        self.script = self.root / "fake_sync.py"
        self.marker = self.root / "ran.txt"
        self.write_script(exit_code=0, payload={"total": 770, "switched": True})
        self.app = self.make_app()

    def write_script(self, *, exit_code: int, payload: dict, sleep: float = 0.0) -> None:
        self.script.write_text(
            "import json, sys, time, pathlib\n"
            f"time.sleep({sleep})\n"
            f"pathlib.Path({str(self.marker)!r}).write_text('ran')\n"
            f"print(json.dumps({payload!r}))\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8")

    def make_app(self, **over) -> kb_admin.AdminApp:
        env = {
            "KB_ADMIN_ENABLED": "false",
            "KB_ADMIN_FACE": "register",
            "KB_REGISTER_ENABLED": "true",
            "KB_AUTHZ_STORE": str(self.store),
            "KB_REGISTER_SESSION_SECRET": "unit-test-session-secret-32b",
            "KB_ADMIN_AUDIT_PATH": str(self.root / "audit.jsonl"),
            "KB_REGISTER_ALLOW_HTTP": "true",
            "KB_SYNC_ENABLED": "true",
            "KB_SYNC_SCRIPT": str(self.script),
            "KB_NAS_MOUNT": str(self.root / "nas"),
            "KB_SNAPSHOT_INDEX": str(self.root / "rag-index.json"),
            "KB_SNAPSHOT_META": str(self.meta),
        }
        env.update(over)
        return kb_admin.AdminApp(env)

    def cookie(self, person: PersonIdentity) -> dict:
        token = kb_admin._seal_session(self.app.session_secret, principal=person.principal,
                                       name=person.name, exp=int(time.time()) + 600)
        return {"Cookie": f"{kb_admin.SESSION_COOKIE}={token}"}

    def run_sync(self, person=None, *, headers=None, app=None):
        request = dict(self.cookie(person) if person else {})
        request.update(WRITE if headers is None else headers)
        return (app or self.app).handle("POST", "/api/sync", request, b"")

    def status(self, person=None, app=None):
        headers = self.cookie(person) if person else {}
        return (app or self.app).handle("GET", "/api/sync", headers, b"")


class OnlyAdminsTests(SyncCase):
    def test_without_a_session_nothing_is_visible_or_runnable(self):
        self.assertEqual(self.status()[0], 401)
        self.assertEqual(self.run_sync()[0], 401)
        self.assertFalse(self.marker.exists())

    def test_an_ordinary_member_is_refused(self):
        status, payload, _ = self.run_sync(MATE)
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        self.assertFalse(self.marker.exists(), "被拒时流水线根本不该启动")

    def test_an_admin_can_see_the_state_and_run_it(self):
        self.assertEqual(self.status(BOSS)[0], 200)
        status, payload, _ = self.run_sync(BOSS)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["total"], 770)
        self.assertTrue(self.marker.exists())

    def test_a_write_without_the_page_marker_is_refused(self):
        self.assertEqual(self.run_sync(BOSS, headers={})[0], 400)
        self.assertFalse(self.marker.exists())


class FailureTests(SyncCase):
    def test_a_failing_pipeline_is_reported_with_its_reason(self):
        self.write_script(exit_code=3, payload={"error": {"message": "docdb-touqian: 只读到 88 条，拒绝切换"}})
        status, payload, _ = self.run_sync(BOSS)
        self.assertEqual((status, payload["error"]), (500, "sync_failed"))
        self.assertIn("拒绝切换", payload["message"])

    def test_an_unconfigured_sync_is_a_404_not_a_crash(self):
        app = self.make_app(KB_SYNC_ENABLED="false")
        self.assertEqual(self.run_sync(BOSS, app=app)[0], 404)
        status, payload, _ = self.status(BOSS, app=app)
        self.assertEqual((status, payload["enabled"]), (200, False))

    def test_a_timeout_leaves_production_alone(self):
        self.write_script(exit_code=0, payload={"total": 1}, sleep=2)
        original = kb_admin.SYNC_TIMEOUT_SECONDS
        kb_admin.SYNC_TIMEOUT_SECONDS = 1
        try:
            status, payload, _ = self.run_sync(BOSS)
        finally:
            kb_admin.SYNC_TIMEOUT_SECONDS = original
        self.assertEqual((status, payload["error"]), (504, "timeout"))
        self.assertIn("保持原样", payload["message"])


class ConcurrencyTests(SyncCase):
    def test_two_people_cannot_run_it_at_once(self):
        import threading
        self.write_script(exit_code=0, payload={"total": 770}, sleep=1.5)
        outcomes = []
        barrier = threading.Barrier(2)

        def press():
            barrier.wait()
            outcomes.append(self.run_sync(BOSS)[0])

        threads = [threading.Thread(target=press) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(sorted(outcomes), [200, 409], "第二个人应被挡下，而不是并发跑第二条流水线")

    def test_the_flag_clears_after_a_run_so_the_next_one_works(self):
        self.assertEqual(self.run_sync(BOSS)[0], 200)
        self.assertFalse(self.app._sync_running)
        self.assertEqual(self.run_sync(BOSS)[0], 200)


class StatusTests(SyncCase):
    def test_status_shows_how_fresh_each_bank_is(self):
        self.meta.write_text(json.dumps({
            "generated_at": "2026-09-20T05:00:00Z", "total": 770,
            "banks": {"cwork-3m": {"bank": "cwork-3m", "usable": 564,
                                   "newest_update": "2026-09-19T15:30:06Z",
                                   "skipped_unconverted": 0}},
        }), encoding="utf-8")
        status, payload, _ = self.status(BOSS)
        self.assertEqual(status, 200)
        self.assertEqual(payload["meta"]["banks"]["cwork-3m"]["usable"], 564)

    def test_a_missing_meta_is_not_an_error(self):
        status, payload, _ = self.status(BOSS)
        self.assertEqual((status, payload["meta"]), (200, None))

    def test_the_page_says_what_happens_on_failure(self):
        page = kb_admin._SYNC_HTML
        self.assertIn("保持原样", page)
        self.assertIn("内容截止", page)
        self.assertNotIn("X-KB-Admin-Key", page)

    def test_the_admin_face_has_no_sync_routes(self):
        admin = kb_admin.AdminApp({"KB_ADMIN_FACE": "admin", "KB_ADMIN_ENABLED": "true",
                                   "KB_ADMIN_KEY_ENV": "RT070_KEY"})
        for method, route in (("GET", "/sync"), ("GET", "/api/sync"), ("POST", "/api/sync")):
            with self.subTest(route):
                self.assertEqual(admin.handle(method, route, WRITE, b"")[0], 404)


if __name__ == "__main__":
    unittest.main()
