"""RT-054 P0: diagnostics are opt-in, anonymous, zero-write, and comparable."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
import kb_lexical_builder as builder  # noqa: E402
import kb_token  # noqa: E402
from kb_lexical import best_spans, bm25_rank, rrf  # noqa: E402
from kb_p0 import P0Trace, TraceBackend, candidate_best_spans, candidate_bm25_rank, compare_exact  # noqa: E402
from kb_storage import StorageError  # noqa: E402
from test_kb_gateway import FIXED_NOW, KB_ID, OTHER_KB, TOKEN, issue_binding_token  # noqa: E402
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

    @staticmethod
    def _structural_payload(body: bytes) -> dict:
        """Remove only intentionally per-process opaque handles/request ids."""
        def scrub(value):
            if isinstance(value, dict):
                return {key: (None if key in {"document_ref", "request_id"} else scrub(item))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        return scrub(json.loads(body))

    def test_disabled_is_byte_for_byte_normal_for_success_and_error_matrix(self) -> None:
        # Fixed clock makes this a response-byte comparison, not a fragile
        # partial-field assertion.  The paths construct success/no-answer and
        # each currently reachable error status without changing storage.
        matrix = [
            (self.path, H),
            (f"/v2/kb/search?kb={KB}&q=definitely-no-answer&retrieval_mode=lexical_fusion_v1", H),
            (f"/v2/kb/search?kb={KB}&q=x&retrieval_mode=lexical_fusion_v1&cursor=x", H),
            (f"/v2/kb/resolve?kb={KB}&lineage=docdb:a&version=99", H),
            (f"/v2/kb/search?kb=not-mounted&q=x", H),
            (self.path, {}),
        ]
        for target, headers in matrix:
            with self.subTest(target=target):
                normal = self.app(False).dispatch("GET", target, headers)
                explicit_disabled = gateway.GatewayApp(
                    self.backend, TOKEN, backend_kind="local", clock=lambda: FIXED_NOW,
                    kb_id=KB, p0_diagnostics=False,
                ).dispatch("GET", target, headers)
                self.assertEqual(normal.status, explicit_disabled.status)
                self.assertEqual(self._structural_payload(normal.body()),
                                 self._structural_payload(explicit_disabled.body()))
                self.assertNotIn("p0_diagnostic", explicit_disabled.payload)
        # Missing lexical payload is a constructible source-unavailable 503,
        # not a simulated response object.
        self.backend.remove("_system/lexical-index.json")
        left = self.app(False).dispatch("GET", self.path, H)
        right = self.app(False).dispatch("GET", self.path, H)
        self.assertEqual(left.status, 503)
        self.assertEqual(self._structural_payload(left.body()), self._structural_payload(right.body()))

    def test_disabled_is_structurally_equal_for_403(self) -> None:
        registry = Path(self.tmp.name) / "tokens.json"
        _record, bearer = issue_binding_token(registry)
        tokens = kb_token.TokenFile(registry)
        target = "/query?q=x"
        normal = gateway.GatewayApp(self.backend, TOKEN, tokens=tokens, kb_id=OTHER_KB,
                                    clock=lambda: FIXED_NOW)
        disabled = gateway.GatewayApp(self.backend, TOKEN, tokens=tokens, kb_id=OTHER_KB,
                                      clock=lambda: FIXED_NOW, p0_diagnostics=False)
        left = normal.dispatch("GET", target, {gateway.TOKEN_HEADER: bearer})
        right = disabled.dispatch("GET", target, {gateway.TOKEN_HEADER: bearer})
        self.assertEqual(left.status, 403)
        self.assertEqual(self._structural_payload(left.body()), self._structural_payload(right.body()))
        self.assertNotIn("p0_diagnostic", right.payload)

    def test_disabled_has_no_extra_backend_calls_or_writes(self) -> None:
        class WriteTrap:
            def __init__(self, backend): self.backend, self.reads, self.writes = backend, 0, 0
            def read(self, path): self.reads += 1; return self.backend.read(path)
            def write(self, *args, **kwargs): self.writes += 1; raise AssertionError("P0 GET wrote")
            def __getattr__(self, name): return getattr(self.backend, name)
        trap = WriteTrap(self.backend)
        baseline = self.app(False).dispatch("GET", self.path, H)
        app = gateway.GatewayApp(trap, TOKEN, backend_kind="local", clock=lambda: FIXED_NOW, kb_id=KB)
        observed = app.dispatch("GET", self.path, H)
        self.assertEqual(self._structural_payload(baseline.body()),
                         self._structural_payload(observed.body()))
        self.assertEqual(trap.writes, 0)
        self.assertEqual(trap.reads, 3, "disabled must retain legacy reads exactly")

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
        self.assertEqual(record["error_category"], "timeout")
        self.assertNotIn("secret transport detail", json.dumps(record, ensure_ascii=False))

    def test_failure_categories_are_whitelisted_and_do_not_leak_inputs(self) -> None:
        trace = P0Trace(kb="secret-kb", query="secret query")
        for exc, expected in ((TimeoutError("token query"), "timeout"),
                              (OSError("/private/path"), "os_error"),
                              (StorageError("body title"), "storage_error")):
            trace.fail(exc)
            record = trace.record()
            self.assertEqual(record["error_category"], expected)
            blob = json.dumps(record, ensure_ascii=False)
            for forbidden in ("secret-kb", "secret query", "token query", "/private/path", "body title"):
                self.assertNotIn(forbidden, blob)

    def test_nested_timing_has_exclusive_self_time_and_explicit_overlap(self) -> None:
        trace = P0Trace(kb=KB)
        trace.started_ns = 0
        ticks = iter((0, 10, 30, 50, 60))
        with mock.patch("kb_p0.time.perf_counter_ns", side_effect=lambda: next(ticks)):
            with trace.stage("auth"):
                with trace.stage("json_decode"):
                    pass
            record = trace.record()
        self.assertEqual(record["stages"]["auth"]["total_elapsed_ns"], 50)
        self.assertEqual(record["stages"]["auth"]["self_elapsed_ns"], 30)
        self.assertEqual(record["stages"]["json_decode"]["total_elapsed_ns"], 20)
        self.assertEqual(record["measured_self_time_sum_ns"], 50)
        self.assertGreaterEqual(record["overlap_ns"], 10)

    def test_transport_hook_counts_attempts_without_faking_wire_bytes(self) -> None:
        class ObservedBackend:
            @contextmanager
            def p0_transport_observer(self, observer):
                self.observer = observer
                try: yield
                finally: self.observer = None
            def read(self, _path):
                self.observer("login", attempt=1, payload_bytes=12, wire_bytes=None)
                self.observer("download", attempt=1, retry=False)
                self.observer("download", attempt=2, payload_bytes=7, retry=True)
                return b"payload"
        trace = P0Trace(kb=KB)
        self.assertEqual(TraceBackend(ObservedBackend(), trace).read("_system/raw-index.json"), b"payload")
        physical = trace.record()["physical"]
        self.assertEqual(physical["login"]["count"], 1)
        self.assertEqual(physical["download"]["count"], 2)
        self.assertEqual(physical["retry"]["count"], 1)
        self.assertIsNone(physical["download"]["wire_bytes"])

    def test_rss_units_follow_macos_and_linux_contracts(self) -> None:
        with mock.patch("kb_p0.resource.getrusage", return_value=type("R", (), {"ru_maxrss": 7})()), \
             mock.patch("kb_p0.sys.platform", "darwin"):
            self.assertEqual(__import__("kb_p0").rss_bytes(), 7)
        with mock.patch("kb_p0.resource.getrusage", return_value=type("R", (), {"ru_maxrss": 7})()), \
             mock.patch("kb_p0.sys.platform", "linux"):
            self.assertEqual(__import__("kb_p0").rss_bytes(), 7 * 1024)


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

    def test_matrix_covers_cjk_ascii_ties_repeats_and_no_answer(self) -> None:
        # The scorer primitive cannot produce a full gateway response; this
        # is explicitly scorer equivalence (rank/span), not a false claim of
        # full fusion response equivalence.
        index = __import__("kb_lexical").build_index((
            ("a", __import__("kb_lexical").chunk_body("会议会议 alpha alpha\n" * 160)),
            ("b", __import__("kb_lexical").chunk_body("会议 beta beta\n" * 160)),
            ("c", __import__("kb_lexical").chunk_body("中文 CJK 三元 gram\n" * 160)),
        ))
        for query in ("会", "会议", "会议会", "alpha", "alpha alpha", "missing", "中文", "CJK"):
            with self.subTest(query=query):
                old = bm25_rank(index, query)
                new = candidate_bm25_rank(index, query)
                self.assertIsNone(compare_exact("rank", old, new))
                self.assertEqual(
                    {lineage: best_spans(index, lineage, query) for lineage, _ in old},
                    {lineage: candidate_best_spans(index, lineage, query) for lineage, _ in new},
                )

    def test_deliberate_avgdl_postings_span_and_tie_mutations_red(self) -> None:
        old = bm25_rank(self.index, "会议")
        with mock.patch("kb_p0.idf", return_value=0.0):
            self.assertIsNotNone(compare_exact("avgdl_or_postings_score", old,
                                               candidate_bm25_rank(self.index, "会议")))
        lineage = old[0][0]
        spans = best_spans(self.index, lineage, "会议")
        self.assertIsNotNone(compare_exact("span", spans, tuple(reversed(spans))))
        self.assertIsNotNone(compare_exact("tie_break", old, tuple(reversed(old))))

    def test_deliberate_sanitization_and_accounting_mutations_red(self) -> None:
        trace = P0Trace(kb=KB, query="secret")
        clean = trace.record()
        tampered = dict(clean)
        tampered["kb_anonymous"] = KB
        self.assertIsNotNone(compare_exact("sanitization", clean, tampered))
        tampered = dict(clean)
        tampered["measured_self_time_sum_ns"] = 1
        self.assertIsNotNone(compare_exact("accounting", clean, tampered))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
