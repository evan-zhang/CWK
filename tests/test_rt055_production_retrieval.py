import json
import threading
import urllib.error
import urllib.request
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.opensearch_retrieval.client import BackendUnavailable
from adapters.opensearch_retrieval.indexing import IndexBuilder, SourceDocument, build_index_template, project_documents
from adapters.opensearch_retrieval.query import RetrievalHit, RetrievalQuery, extract_exact_fields, is_no_answer, merge_channel_hits
from adapters.opensearch_retrieval.service import RetrievalApplication, create_server


FIXTURE = Path(__file__).parent / "fixtures" / "rt055_retrieval_synthetic.json"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
OPENSEARCH_DOCKERFILE = ROOT / "deploy" / "opensearch.Dockerfile"


class TemplateAndProjectionTests(unittest.TestCase):
    def test_template_has_icu_bm25_parent_child_and_source_boundary(self):
        template = build_index_template()
        self.assertEqual(template["settings"]["similarity"]["cwk_bm25"]["type"], "BM25")
        self.assertEqual(template["settings"]["analysis"]["analyzer"]["cwk_icu"]["tokenizer"], "icu_tokenizer")
        properties = template["mappings"]["properties"]
        self.assertEqual(properties["join"]["relations"], {"document": "chunk"})
        self.assertEqual(template["mappings"]["_source"]["excludes"], ["body"])
        self.assertEqual(properties["identifiers"]["type"], "keyword")

    def test_projection_is_stable_and_keeps_bank_document_parent_child_scope(self):
        document = SourceDocument(
            "cwork-3m", "synthetic-rt055-001", "Synthetic sample", "synthetic.txt", "RT-055-SYNTH-001"
        )
        first = project_documents([document], tenant_id="tenant-test")
        second = project_documents([document], tenant_id="tenant-test")
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(first.document_count, 1)
        self.assertEqual(first.parents[0]["join"], "document")
        self.assertEqual(first.children[0]["join"]["name"], "chunk")
        self.assertEqual(first.children[0]["join"]["parent"], first.parents[0]["parent_id"])
        self.assertEqual(first.children[0]["identifiers"], ["rt-055-synth-001"])

    def test_builder_accepts_synthetic_fixture_and_bulk_is_idempotent_shape(self):
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))["documents"]

        class FakeClient:
            def __init__(self):
                self.exists = False
                self.calls = []

            def index_exists(self, name):
                self.calls.append(("HEAD", name, None))
                return self.exists

            def request(self, method, path, payload=None):
                self.calls.append((method, path, payload))
                if method == "PUT":
                    self.exists = True
                    return {"acknowledged": True}
                if path.endswith("/_bulk"):
                    lines = payload.decode("utf-8").splitlines()
                    rows = [json.loads(lines[index + 1]) for index in range(0, len(lines), 2)]
                    assert rows[0]["kind"] == "parent"
                    return {"errors": False, "items": [{"index": {"status": 201}} for _ in rows]}
                if path.endswith("/_refresh"):
                    return {"_shards": {"failed": 0}}
                raise AssertionError((method, path))

        client = FakeClient()
        stats = IndexBuilder(
            client, index_name="cwk-retrieval-v1", tenant_id="default", banks=("cwork-3m", "docdb-touqian")
        ).build(values)
        self.assertEqual(stats.document_count, 2)
        self.assertEqual(stats.parent_count, 2)
        self.assertEqual(stats.child_count, 2)
        self.assertEqual(stats.batch_count, 1)
        self.assertTrue(any(call[0] == "PUT" for call in client.calls))


class DeploymentContractTests(unittest.TestCase):
    def test_compose_is_loopback_bound_and_forwards_only_env_credentials(self):
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:${OPENSEARCH_PORT:-9200}:9200"', compose)
        self.assertIn('"127.0.0.1:${RETRIEVAL_PORT:-8787}:8787"', compose)
        self.assertIn("CWK_OPENSEARCH_USERNAME: ${CWK_OPENSEARCH_USERNAME:-}", compose)
        self.assertIn("CWK_OPENSEARCH_PASSWORD: ${CWK_OPENSEARCH_PASSWORD:-}", compose)
        self.assertNotIn("username:", compose)
        self.assertNotIn("password:", compose)

    def test_opensearch_image_installs_icu_at_build_time(self):
        dockerfile = OPENSEARCH_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("analysis-icu", dockerfile)
        self.assertIn("opensearch-plugin install --batch", dockerfile)


class ExactAndMergeTests(unittest.TestCase):
    def test_exact_resolver_normalizes_identifiers_dates_and_filenames(self):
        fields = extract_exact_fields("ＡＢＣ–２０２６–００７ 2026/09/08 (Plan.PDF)")
        self.assertEqual(fields["identifiers"], ["abc-2026-007"])
        self.assertEqual(fields["date_values"], ["2026-09-08"])
        self.assertEqual(fields["filenames"], ["plan.pdf"])
        self.assertEqual(extract_exact_fields("2026-02-30")["date_values"], [])

    def test_merge_collapses_by_document_and_marks_hybrid(self):
        exact = [RetrievalHit("doc-a", 2.0, "exact", "parent-a", "chunk-a")]
        lexical = [
            RetrievalHit("doc-a", 1.0, "lexical", "parent-a", "chunk-b"),
            RetrievalHit("doc-b", 3.0, "lexical", "parent-b", "chunk-c"),
        ]
        result = merge_channel_hits(exact, lexical, top_k=10)
        self.assertEqual([hit.doc_id for hit in result], ["doc-b", "doc-a"])
        self.assertEqual(result[1].channel, "hybrid")
        self.assertEqual(result[1].score, 2.0)

    def test_no_answer_is_a_real_zero_hit_decision(self):
        self.assertTrue(is_no_answer([]))
        self.assertFalse(is_no_answer([RetrievalHit("doc", 1.0, "lexical")]))


class FakeSearchClient:
    def __init__(self, *, exact_hits=None, lexical_hits=None, ready=True):
        self.exact_hits = exact_hits
        self.lexical_hits = lexical_hits
        self.ready_value = ready
        self.calls = []

    def is_ready(self, index_name):
        return self.ready_value

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/_search"):
            is_exact = payload["query"]["bool"]["must"] == []
            raw_hits = self.exact_hits if is_exact else self.lexical_hits
            return {
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {"hits": raw_hits or []},
            }
        if path.endswith("/_mget"):
            docs = []
            for item in payload["docs"]:
                parent_id = item["_id"]
                docs.append({
                    "_id": parent_id,
                    "found": True,
                    "_source": {
                        "tenant_id": "tenant-test",
                        "bank": "cwork-3m",
                        "kind": "parent",
                        "doc_id": "doc-1",
                        "parent_id": parent_id,
                    },
                })
            return {"docs": docs}
        raise AssertionError((method, path, payload))


def raw_hit(score=1.0):
    return [{
        "_source": {
            "tenant_id": "tenant-test",
            "bank": "cwork-3m",
            "kind": "chunk",
            "doc_id": "doc-1",
            "parent_id": "parent-1",
            "chunk_id": "chunk-1",
        },
        "_score": score,
    }]


class QueryEngineTests(unittest.TestCase):
    def test_exact_hit_and_lexical_hit_are_merged_after_parent_expansion(self):
        client = FakeSearchClient(exact_hits=raw_hit(3.0), lexical_hits=raw_hit(1.0))
        engine = RetrievalQuery(client, index_name="idx", tenant_id="tenant-test", banks=("cwork-3m",))
        result = engine.query("cwork-3m", "RT-055-SYNTH-001", top_k=5)
        self.assertEqual([(hit.doc_id, hit.channel) for hit in result], [("doc-1", "hybrid")])
        self.assertEqual(len([call for call in client.calls if call[1].endswith("/_search")]), 2)
        self.assertEqual(client.calls[-1][1], "/idx/_mget")
        self.assertIn("docs", client.calls[-1][2])
        self.assertNotIn("ids", client.calls[-1][2])

    def test_exact_zero_does_not_fallback_to_lexical(self):
        client = FakeSearchClient(exact_hits=[], lexical_hits=raw_hit())
        engine = RetrievalQuery(client, index_name="idx", tenant_id="tenant-test", banks=("cwork-3m",))
        self.assertEqual(engine.query("cwork-3m", "RT-055-SYNTH-001", top_k=5), [])
        self.assertEqual(len([call for call in client.calls if call[1].endswith("/_search")]), 1)
        self.assertTrue(is_no_answer([]))


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSearchClient(exact_hits=raw_hit(), lexical_hits=raw_hit())
        engine = RetrievalQuery(self.client, index_name="idx", tenant_id="tenant-test", banks=("cwork-3m",))
        self.application = RetrievalApplication(engine, top_k=5, banks=("cwork-3m",))
        self.server = create_server(self.application, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_health_ready_and_query_success_contract(self):
        self.assertEqual(self._request("GET", "/healthz")[0], 200)
        self.assertEqual(self._request("GET", "/readyz")[0], 200)
        status, payload = self._request("POST", "/query", {"bank": "cwork-3m", "query": "ordinary words"})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"hits", "no_answer", "took_ms"})
        self.assertFalse(payload["no_answer"])

    def test_query_invalid_bank_and_invalid_body_are_400_or_404(self):
        self.assertEqual(self._request("POST", "/query", {"bank": "missing-bank", "query": "x"})[0], 404)
        self.assertEqual(self._request("POST", "/query", {"bank": "cwork-3m"})[0], 400)
        self.assertEqual(self._request("GET", "/unknown")[0], 404)

    def test_query_backend_failure_is_503_not_no_answer(self):
        class BrokenEngine:
            banks = {"cwork-3m"}
            def query(self, bank, query, *, top_k):
                raise BackendUnavailable("backend unavailable")
            def ready(self):
                return False

        app = RetrievalApplication(BrokenEngine(), top_k=5, banks=("cwork-3m",))
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_base, self.base = self.base, f"http://127.0.0.1:{server.server_port}"
        try:
            status, payload = self._request("POST", "/query", {"bank": "cwork-3m", "query": "ordinary words"})
            self.assertEqual(status, 503)
            self.assertNotIn("hits", payload)
            self.assertEqual(self._request("GET", "/readyz")[0], 503)
        finally:
            self.base = old_base
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
