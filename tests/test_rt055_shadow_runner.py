import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import rt055_shadow_runner as runner


class ShadowRunnerTests(unittest.TestCase):
    def test_load_cases_keeps_query_in_memory_and_returns_safe_case_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps({"libraries": {
                "cwork-3m": {"cases": [{"query": "secret synthetic", "ordinal": 4}]},
                "docdb-touqian": {"cases": []},
                "spbp-2027": {"cases": []},
            }}), encoding="utf-8")
            cases = runner.load_cases(path)
        self.assertEqual(cases, [runner.Case("cwork-3m", "secret synthetic", 4)])

    def test_response_id_extractors_are_narrow(self):
        self.assertEqual(runner.legacy_doc_ids({"results": [{"lineage_id": "doc-a"}, {"id": "doc-b"}]}), ["doc-a", "doc-b"])
        self.assertEqual(runner.retrieval_doc_ids({"hits": [{"doc_id": "doc-a"}]}), ["doc-a"])
        with self.assertRaises(RuntimeError):
            runner.retrieval_doc_ids({"hits": [{"title": "not-an-id"}]})

    def test_summary_is_aggregate_only(self):
        rows = [
            {"errors": [], "no_answer_match": True, "doc_id_set_match": False, "retrieval_took_ms": 5.0},
            {"errors": ["legacy"], "no_answer_match": None, "doc_id_set_match": None, "retrieval_took_ms": None},
        ]
        summary = runner.summarize(rows)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["successful_pairs"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["no_answer_matches"], 1)
        self.assertEqual(summary["doc_id_set_matches"], 0)
        self.assertNotIn("query", summary)


if __name__ == "__main__":
    unittest.main()
