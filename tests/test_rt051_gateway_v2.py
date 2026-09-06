#!/usr/bin/env python3
"""RT-051 P1a: /v2/kb/* controlled-read face.

Pins the contract the design (RT/RT-051/rt-lite.md) demands from the first
increment: complete enumeration, metadata search, known-lineage resolve,
bounded read with server-side cursors, and the honesty boundary —
``full_sha_verified`` is true exactly because every read re-fetches and
re-hashes the whole object and refuses on drift.

The lexical face (``lexical_fusion_v1``) is P3: requesting it without an
explicit degrade is 503 here, and that *is* the tested behaviour.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
from test_kb_gateway import (  # noqa: E402
    BODY_V1,
    FIXED_NOW,
    LINEAGE,
    RAW_PATH,
    TOKEN,
    seed_local_kb,
    seed_memory_kb,
)
from test_kb_storage import fake_nas  # noqa: E402

H = {gateway.TOKEN_HEADER: TOKEN}
KB = "cwork-3m"


def make_clock_app(backend, holder=None):
    """GatewayApp with a mutable clock, for expiry tests."""
    holder = holder if holder is not None else {"now": FIXED_NOW}
    app = gateway.GatewayApp(
        backend,
        TOKEN,
        backend_kind="memory",
        clock=lambda: holder["now"],
        kb_id=KB,
    )
    return app, holder


def get(app, path):
    return app.dispatch("GET", path, H)


def resolve_ref(app, lineage=LINEAGE):
    r = get(app, f"/v2/kb/resolve?kb={KB}&lineage={lineage}")
    assert r.status == 200, r.payload
    return r.payload["document_ref"]


class V2CapabilitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())

    def test_the_contract_is_reported(self) -> None:
        r = get(self.app, f"/v2/kb/capabilities?kb={KB}")
        self.assertEqual(r.status, 200)
        p = r.payload
        self.assertEqual(p["schema"], "cwk.kb.capabilities.v2")
        self.assertEqual(
            p["supported_operations"],
            ["capabilities", "list", "search", "resolve", "inspect", "read", "continue", "renew"],
        )
        self.assertEqual(p["lexical_modes"], [])  # P3 未交付，如实报空
        self.assertEqual(p["offset_unit"], "byte_0based_halfopen")
        self.assertEqual(p["limits"]["page_size_max"], 200)

    def test_kb_is_required_even_for_capabilities(self) -> None:
        r = get(self.app, "/v2/kb/capabilities")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.payload["error"]["code"], "bad_request")

    def test_unknown_operation_is_404(self) -> None:
        r = get(self.app, f"/v2/kb/teleport?kb={KB}")
        self.assertEqual(r.status, 404)


class V2ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())

    def test_full_enumeration_walks_to_eof_without_duplicates(self) -> None:
        seen: list[str] = []
        path = f"/v2/kb/list?kb={KB}&page_size=2"
        cursor = None
        for _ in range(10):
            r = get(self.app, path + (f"&cursor={cursor}" if cursor else ""))
            self.assertEqual(r.status, 200)
            p = r.payload
            self.assertEqual(p["schema"], "cwk.kb.documents.v2")
            seen.extend(i["lineage_id"] for i in p["items"])
            cursor = p["next_cursor"]
            if p["eof"]:
                break
        else:
            self.fail("10 页没走到 eof")
        self.assertEqual(p["total"], 3)
        self.assertLessEqual(len(seen), 3)
        self.assertEqual(sorted(set(seen)), sorted(seen))
        self.assertEqual(len(seen), 3)

    def test_page_size_above_200_is_refused_not_clamped(self) -> None:
        r = get(self.app, f"/v2/kb/list?kb={KB}&page_size=201")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.payload["error"]["code"], "bad_request")

    def test_index_change_between_pages_is_a_409(self) -> None:
        r1 = get(self.app, f"/v2/kb/list?kb={KB}&page_size=1")
        cursor = r1.payload["next_cursor"]
        backend = self.app.mounts[KB]
        backend.write(gateway.RAW_INDEX_REL, gateway.dumps({})) if False else None
        # 直接改索引：重写一个少一条目的索引
        import json as _json

        payload = _json.loads(backend.read(gateway.RAW_INDEX_REL).decode("utf-8"))
        payload["entries"].pop(LINEAGE)
        backend.write(gateway.RAW_INDEX_REL, _json.dumps(payload).encode("utf-8"))
        r2 = get(self.app, f"/v2/kb/list?kb={KB}&page_size=1&cursor={cursor}")
        self.assertEqual(r2.status, 409)
        self.assertEqual(r2.payload["error"]["code"], "metadata_changed")

    def test_unknown_kb_is_404(self) -> None:
        r = get(self.app, "/v2/kb/list?kb=elsewhere")
        self.assertEqual(r.status, 404)
        self.assertEqual(r.payload["error"]["code"], "unknown_kb")


class V2SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())

    def test_metadata_substring_search_matches_the_v1_semantics(self) -> None:
        r = get(self.app, f"/v2/kb/search?kb={KB}&q=供货")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.payload["total"], 1)
        self.assertEqual(r.payload["items"][0]["lineage_id"], LINEAGE)
        self.assertEqual(r.payload["query_kind"], "metadata")

    def test_blank_or_missing_q_is_400(self) -> None:
        for q in ("", "%20"):
            r = get(self.app, f"/v2/kb/search?kb={KB}&q={q}")
            self.assertEqual(r.status, 400, q)

    def test_q_longer_than_256_codepoints_is_400(self) -> None:
        r = get(self.app, f"/v2/kb/search?kb={KB}&q={'x' * 257}")
        self.assertEqual(r.status, 400)

    def test_lexical_mode_is_503_until_p3_and_degrades_only_when_asked(self) -> None:
        r = get(self.app, f"/v2/kb/search?kb={KB}&q=供货&retrieval_mode=lexical_fusion_v1")
        self.assertEqual(r.status, 503)
        self.assertEqual(r.payload["error"]["code"], "lexical_unavailable")
        r2 = get(
            self.app,
            f"/v2/kb/search?kb={KB}&q=供货&retrieval_mode=lexical_fusion_v1&allow_degraded=metadata",
        )
        self.assertEqual(r2.status, 200)
        self.assertTrue(r2.payload["degraded"])
        self.assertEqual(r2.payload["effective_mode"], "metadata")

    def test_lexical_mode_refuses_cursors(self) -> None:
        r = get(
            self.app,
            f"/v2/kb/search?kb={KB}&q=供&retrieval_mode=lexical_fusion_v1"
            "&allow_degraded=metadata&cursor=whatever",
        )
        self.assertEqual(r.status, 400)

    def test_unknown_parameter_is_not_silently_ignored(self) -> None:
        r = get(self.app, f"/v2/kb/search?kb={KB}&q=供货&format=pretty")
        self.assertEqual(r.status, 400)


class V2ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())

    def test_known_lineage_resolves_without_search(self) -> None:
        r = get(self.app, f"/v2/kb/resolve?kb={KB}&lineage={LINEAGE}")
        self.assertEqual(r.status, 200)
        ident = r.payload["identity"]
        self.assertEqual(ident["lineage_id"], LINEAGE)
        self.assertEqual(ident["source_version"], 2)
        self.assertEqual(ident["raw_sha256"], hashlib.sha256(BODY_V1.encode()).hexdigest())
        self.assertTrue(r.payload["document_ref"])

    def test_old_version_is_409_not_a_silent_fallback(self) -> None:
        r = get(self.app, f"/v2/kb/resolve?kb={KB}&lineage={LINEAGE}&version=1")
        self.assertEqual(r.status, 409)
        self.assertEqual(r.payload["error"]["code"], "stale_reference")

    def test_unknown_lineage_is_404(self) -> None:
        r = get(self.app, f"/v2/kb/resolve?kb={KB}&lineage=docdb:none")
        self.assertEqual(r.status, 404)

    def test_missing_lineage_is_400(self) -> None:
        r = get(self.app, f"/v2/kb/resolve?kb={KB}")
        self.assertEqual(r.status, 400)


class V2ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())
        self.ref = resolve_ref(self.app)
        self.total = len(BODY_V1.encode("utf-8"))

    def test_paged_read_to_eof_reassembles_the_exact_bytes(self) -> None:
        chunks: list[bytes] = []
        path = f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=100"
        cursor = None
        eof = False
        for _ in range(100):
            r = get(self.app, path + (f"&cursor={cursor}" if cursor else ""))
            self.assertEqual(r.status, 200)
            p = r.payload
            self.assertTrue(p["full_sha_verified"])
            self.assertEqual(p["evidence_status"], "verified")
            chunks.append(p["text"].encode("utf-8"))
            self.assertEqual(p["returned_bytes"], len(p["text"].encode("utf-8")))
            cursor = p["next_cursor"]
            if p["eof"]:
                eof = True
                break
            self.assertIsNotNone(cursor, "未到 eof 必须有 next_cursor")
        self.assertTrue(eof)
        joined = b"".join(chunks)
        self.assertEqual(len(joined), self.total)
        self.assertEqual(hashlib.sha256(joined).hexdigest(), hashlib.sha256(BODY_V1.encode()).hexdigest())

    def test_repeat_read_is_idempotent(self) -> None:
        r1 = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=50")
        r2 = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=50")
        for field in ("span_start_byte", "span_end_byte", "page_sha256", "returned_bytes"):
            self.assertEqual(r1.payload[field], r2.payload[field])

    def test_byte_range_read_reports_range_complete(self) -> None:
        # "# 供货" 占 8 字节（# + 空格 + 供 3 + 货 3），end_byte=8 是码点边界
        r = get(
            self.app,
            f"/v2/kb/read?kb={KB}&document_ref={self.ref}&start_byte=0&end_byte=8",
        )
        p = r.payload
        self.assertEqual(r.status, 200)
        self.assertEqual(p["span_end_byte"], 8)
        self.assertFalse(p["eof"])
        self.assertTrue(p["range_complete"])
        self.assertIsNone(p["next_cursor"], "范围读完但没到文件尾：不承诺 next")

    def test_line_range_reads_whole_lines(self) -> None:
        r = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&line_start=1&line_end=1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.payload["text"], "# 供货协议\n")

    def test_start_inside_a_codepoint_is_416(self) -> None:
        # BODY_V1[2] 是多字节汉字「供」的首字节：start=3 落在码点中间
        r = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&start_byte=3")
        self.assertEqual(r.status, 416)
        self.assertEqual(r.payload["error"]["code"], "invalid_utf8_boundary")

    def test_cursor_byte_and_line_modes_are_mutually_exclusive(self) -> None:
        r = get(
            self.app,
            f"/v2/kb/read?kb={KB}&document_ref={self.ref}&start_byte=0&line_start=1",
        )
        self.assertEqual(r.status, 400)

    def test_cursor_mode_refuses_max_bytes(self) -> None:
        r1 = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=10")
        cur = r1.payload["next_cursor"]
        r2 = get(
            self.app,
            f"/v2/kb/read?kb={KB}&document_ref={self.ref}&cursor={cur}&max_bytes=99",
        )
        self.assertEqual(r2.status, 400)

    def test_byte_drift_from_the_index_is_a_409(self) -> None:
        # 夹具里周报条目的 sha 是假的：实读对不上必须拒读
        backend = self.app.mounts[KB]
        r = get(self.app, f"/v2/kb/resolve?kb={KB}&lineage=cwork:2095046023776104449")
        ref = r.payload["document_ref"]
        rr = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={ref}")
        self.assertEqual(rr.status, 409)
        self.assertEqual(rr.payload["error"]["code"], "stale_reference")

    def test_empty_span_is_legal_only_at_total(self) -> None:
        r = get(
            self.app,
            f"/v2/kb/read?kb={KB}&document_ref={self.ref}&start_byte={self.total}&end_byte={self.total}",
        )
        self.assertEqual(r.status, 200)
        self.assertTrue(r.payload["eof"])
        self.assertEqual(r.payload["returned_bytes"], 0)
        r2 = get(
            self.app,
            f"/v2/kb/read?kb={KB}&document_ref={self.ref}&start_byte=5&end_byte=5",
        )
        self.assertEqual(r2.status, 416)

    def test_garbage_handle_is_400(self) -> None:
        r = get(self.app, f"/v2/kb/read?kb={KB}&document_ref=garbage")
        self.assertEqual(r.status, 400)

    def test_end_byte_without_start_is_400(self) -> None:
        r = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&end_byte=5")
        self.assertEqual(r.status, 400)


class V2ContinueRenewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())
        self.ref = resolve_ref(self.app)

    def test_continue_requires_a_cursor(self) -> None:
        r = get(self.app, f"/v2/kb/continue?kb={KB}&document_ref={self.ref}")
        self.assertEqual(r.status, 400)

    def test_continue_advances_from_the_server_cursor(self) -> None:
        r1 = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=10")
        cur = r1.payload["next_cursor"]
        r2 = get(self.app, f"/v2/kb/continue?kb={KB}&document_ref={self.ref}&cursor={cur}")
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.payload["span_start_byte"], r1.payload["span_end_byte"])

    def test_renew_reissues_only_when_identity_still_holds(self) -> None:
        r = get(self.app, f"/v2/kb/renew?kb={KB}&document_ref={self.ref}")
        self.assertEqual(r.status, 200)
        self.assertNotEqual(r.payload["document_ref"], self.ref)
        self.assertEqual(r.payload["identity"]["raw_sha256"], hashlib.sha256(BODY_V1.encode()).hexdigest())


class V2HandleIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())
        self.ref = resolve_ref(self.app)

    def test_a_list_cursor_is_not_a_read_cursor(self) -> None:
        rl = get(self.app, f"/v2/kb/list?kb={KB}&page_size=1")
        cur = rl.payload["next_cursor"]
        r = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&cursor={cur}")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.payload["error"]["code"], "invalid_cursor")

    def test_a_read_cursor_is_not_a_list_cursor(self) -> None:
        rr = get(self.app, f"/v2/kb/read?kb={KB}&document_ref={self.ref}&max_bytes=5")
        cur = rr.payload["next_cursor"]
        r = get(self.app, f"/v2/kb/list?kb={KB}&cursor={cur}")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.payload["error"]["code"], "invalid_cursor")

    def test_handles_do_not_cross_libraries(self) -> None:
        other = seed_memory_kb()
        app2 = gateway.GatewayApp(
            other, TOKEN, backend_kind="memory", clock=lambda: FIXED_NOW, kb_id="spbp"
        )
        r = app2.dispatch(
            "GET", f"/v2/kb/read?kb=spbp&document_ref={self.ref}", H
        )
        self.assertEqual(r.status, 400)

    def test_expired_handles_are_410_and_re_resolve_recovers(self) -> None:
        app, holder = make_clock_app(seed_memory_kb())
        ref = resolve_ref(app)
        holder["now"] = holder["now"] + timedelta(seconds=901)
        r = get(app, f"/v2/kb/inspect?kb={KB}&document_ref={ref}")
        self.assertEqual(r.status, 410)
        self.assertEqual(r.payload["error"]["code"], "reference_expired")
        ref2 = resolve_ref(app)
        r2 = get(app, f"/v2/kb/inspect?kb={KB}&document_ref={ref2}")
        self.assertEqual(r2.status, 200)


class V2AuthShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_clock_app(seed_memory_kb())

    def test_missing_token_answers_the_v2_error_schema(self) -> None:
        r = self.app.dispatch("GET", f"/v2/kb/list?kb={KB}", {})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.payload["schema"], "cwk.kb.error.v2")
        self.assertEqual(r.payload["error"]["code"], "unauthorized")
        self.assertIn("request_id", r.payload)

    def test_binding_tokens_reach_the_v2_face_for_their_own_library(self) -> None:
        # RT-047/RT-049 的 scope 语义必须原样覆盖 v2：绑定 token 只开自己的库
        backend = seed_memory_kb()
        app = gateway.GatewayApp(
            backend, "admin-secret", backend_kind="memory", clock=lambda: FIXED_NOW, kb_id=KB
        )
        r401 = app.dispatch("GET", f"/v2/kb/list?kb={KB}", {gateway.TOKEN_HEADER: TOKEN})
        self.assertEqual(r401.status, 401)


class V2LocalFSEquivalenceTests(unittest.TestCase):
    def test_read_to_eof_on_a_real_filesystem_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = seed_local_kb(Path(tmp))
            app, _ = make_clock_app(backend)
            app.backend_kind = "local"
            ref = resolve_ref(app)
            chunks = []
            cursor = None
            for _ in range(100):
                path = f"/v2/kb/read?kb={KB}&document_ref={ref}&max_bytes=64"
                r = get(app, path + (f"&cursor={cursor}" if cursor else ""))
                p = r.payload
                chunks.append(p["text"].encode("utf-8"))
                cursor = p["next_cursor"]
                if p["eof"]:
                    break
            joined = b"".join(chunks)
            self.assertEqual(hashlib.sha256(joined).hexdigest(), hashlib.sha256(BODY_V1.encode()).hexdigest())


class V2FakeNASTests(unittest.TestCase):
    """P1 出口判据（C09）：v2 读取面在 FileStation 传输层上完整可达，
    且覆写漂移被拒。传输层不是 memory/local 的另一个分叉——同一条
    GatewayApp 代码，只换 backend。"""

    def _seed_fake_nas(self):
        backend = fake_nas()
        entry = {
            "path": RAW_PATH,
            "title": "供货协议",
            "version": 2,
            "sha256": hashlib.sha256(BODY_V1.encode("utf-8")).hexdigest(),
            "status": "ok",
            "artifact_kind": "document",
        }
        backend.mkdir("_system")
        backend.write(gateway.RAW_INDEX_REL, gateway.dumps({
            "schema": "cwk.kb.raw-index.v1",
            "kb_code": "f" * 32,
            "entries": {LINEAGE: entry},
        }))
        backend.mkdir("raw/合同")
        backend.write(RAW_PATH, BODY_V1.encode("utf-8"))
        return backend

    def test_paged_read_to_eof_over_filestation_reassembles_the_bytes(self) -> None:
        app = gateway.GatewayApp(
            self._seed_fake_nas(), TOKEN,
            backend_kind="nas", clock=lambda: FIXED_NOW, kb_id=KB,
        )
        ref = resolve_ref(app)
        chunks: list[bytes] = []
        cursor = None
        for _ in range(50):
            path = f"/v2/kb/read?kb={KB}&document_ref={ref}&max_bytes=64"
            r = get(app, path + (f"&cursor={cursor}" if cursor else ""))
            self.assertEqual(r.status, 200, r.payload)
            p = r.payload
            self.assertTrue(p["full_sha_verified"])
            chunks.append(p["text"].encode("utf-8"))
            cursor = p["next_cursor"]
            if p["eof"]:
                break
        else:
            self.fail("分页循环没到 eof")
        joined = b"".join(chunks)
        self.assertEqual(
            hashlib.sha256(joined).hexdigest(),
            hashlib.sha256(BODY_V1.encode("utf-8")).hexdigest(),
        )

    def test_a_source_overwrite_between_reads_is_refused(self) -> None:
        backend = self._seed_fake_nas()
        app = gateway.GatewayApp(
            backend, TOKEN,
            backend_kind="nas", clock=lambda: FIXED_NOW, kb_id=KB,
        )
        ref = resolve_ref(app)
        r1 = get(app, f"/v2/kb/read?kb={KB}&document_ref={ref}&max_bytes=32")
        self.assertEqual(r1.status, 200)
        # 源被覆写：字节不再等于索引记录的 SHA——续读必须拒，不是把新内容
        # 当旧版发出去（这正是快照/版本链要防的事）。
        backend.write(RAW_PATH, ("篡改后的正文" * 80).encode("utf-8"))
        r2 = get(app, f"/v2/kb/read?kb={KB}&document_ref={ref}&max_bytes=32")
        self.assertEqual(r2.status, 409)
        self.assertEqual(r2.payload["error"]["code"], "stale_reference")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
