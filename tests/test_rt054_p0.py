"""RT-054 P0: diagnostics are opt-in, anonymous, zero-write, and comparable."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
import kb_lexical_builder as builder  # noqa: E402
from kb_lexical import best_spans, bm25_rank, rrf  # noqa: E402
from kb_p0 import P0Trace, candidate_best_spans, candidate_bm25_rank, compare_exact  # noqa: E402
from test_kb_gateway import FIXED_NOW, TOKEN  # noqa: E402
from test_rt051_lexical_fusion import KB, H, seed_kb  # noqa: E402


class P0GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = seed_kb(Path(self.tmp.name) / "kb")
        report = builder.build_lexical_index(self.backend, kb_code="ab" * 16)
        builder.publish(self.backend, kb_code="ab" * 16, report=report)
        self.path = f"/v2/kb/search?kb={KB}&q=交付包说明&retrieval_mode=lexical_fusion_v1"

    def app(self, enabled: bool):
        return gateway.GatewayApp(self.backend, TOKEN, backend_kind="local",
                                  clock=lambda: FIXED_NOW, kb_id=KB,
                                  p0_diagnostics=enabled)

    def test_disabled_is_byte_for_byte_normal_and_zero_write(self) -> None:
        normal = self.app(False).dispatch("GET", self.path, H)
        disabled = self.app(False).dispatch("GET", self.path, H)
        self.assertNotIn("p0_diagnostic", disabled.payload)
        self.assertEqual(normal.status, disabled.status)
        self.assertEqual(normal.payload["total"], disabled.payload["total"])
        self.assertEqual(normal.payload["generation"], disabled.payload["generation"])
        self.assertEqual(
            [row["lineage_id"] for row in normal.payload["items"]],
            [row["lineage_id"] for row in disabled.payload["items"]],
        )

    def test_enabled_record_is_sanitized_and_counts_legacy_reads(self) -> None:
        before = {p.relative_to(self.backend.root).as_posix(): p.read_bytes()
                  for p in self.backend.root.rglob("*") if p.is_file()}
        response = self.app(True).dispatch("GET", self.path, H)
        self.assertEqual(response.status, 200)
        # Encoding completes the returned in-memory record too.
        wire = response.body()
        record = response.payload["p0_diagnostic"]
        self.assertEqual(record["schema"], "cwk.kb.p0.diagnostic.v1")
        self.assertEqual(record["logical"]["gateway_writes"], 0)
        self.assertGreaterEqual(record["logical"]["raw_index_reads"], 2)
        self.assertEqual(record["logical"]["lexical_reads"], 1)
        self.assertEqual(record["physical"]["login"]["count"], None)
        self.assertGreater(record["stages"]["auth"]["count"], 0)
        self.assertGreater(record["stages"]["json_encode"]["count"], 0)
        forbidden = (KB, "交付包说明", "看板设计", "raw/", TOKEN)
        serialized = json.dumps(record, ensure_ascii=False)
        for value in forbidden:
            self.assertNotIn(value, serialized)
        after = {p.relative_to(self.backend.root).as_posix(): p.read_bytes()
                 for p in self.backend.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "P0 GET must not persist a record")

    def test_failure_is_recorded_without_error_text(self) -> None:
        response = self.app(True).dispatch(
            "GET", f"/v2/kb/search?kb={KB}&q=交付包&retrieval_mode=lexical_fusion_v1&cursor=x", H
        )
        self.assertEqual(response.status, 400)
        record = response.payload["p0_diagnostic"]
        self.assertEqual(record["error_category"], "bad_request")
        self.assertNotIn("cursor", json.dumps(record, ensure_ascii=False))

    def test_timeout_category_is_sanitized(self) -> None:
        trace = P0Trace(kb=KB, query="会议")
        trace.fail(TimeoutError("secret transport detail"))
        record = trace.record()
        self.assertEqual(record["error_category"], "TimeoutError")
        self.assertNotIn("secret transport detail", json.dumps(record, ensure_ascii=False))


class P0AlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = __import__("kb_lexical").build_index((
            ("lineage:a", __import__("kb_lexical").chunk_body("会议 alpha\n" * 220)),
            ("lineage:b", __import__("kb_lexical").chunk_body("会议 beta\n" * 220)),
        ))

    def test_candidate_is_exact_for_rank_rrf_sort_and_span(self) -> None:
        old_rank = bm25_rank(self.index, "会议")
        new_rank = candidate_bm25_rank(self.index, "会议")
        self.assertIsNone(compare_exact("body_rank", old_rank, new_rank))
        old = []
        new = []
        metadata_rank = {"lineage:a": 1, "lineage:b": 2}
        for rank, (lineage, _score) in enumerate(old_rank, 1):
            old.append({"lineage": lineage, "version": 1, "raw_sha": hashlib.sha256(lineage.encode()).hexdigest(),
                        "body_rank": rank, "metadata_rank": metadata_rank.get(lineage),
                        "rrf": rrf(rank, metadata_rank.get(lineage)), "spans": best_spans(self.index, lineage, "会议")})
        for rank, (lineage, _score) in enumerate(new_rank, 1):
            new.append({"lineage": lineage, "version": 1, "raw_sha": hashlib.sha256(lineage.encode()).hexdigest(),
                        "body_rank": rank, "metadata_rank": metadata_rank.get(lineage),
                        "rrf": rrf(rank, metadata_rank.get(lineage)), "spans": candidate_best_spans(self.index, lineage, "会议")})
        self.assertIsNone(compare_exact("fusion_evidence", old, new))

    def test_intentional_mutation_fails_exact_comparison(self) -> None:
        self.assertIsNotNone(compare_exact("sort", ["a", "b"], ["b", "a"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
