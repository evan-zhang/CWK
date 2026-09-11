"""Synthetic transport/behavior tests, not evidence of native search quality."""
import copy
import io
import http.server
import threading
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kb_retrieval_candidates as candidates

KB = candidates.decision.LIBRARIES


def documents():
    return [candidates.projection.SourceDocument(kb, 'doc-1', '季度计划', 'Plan.PDF',
        '合同 ＡＢＣ－２０２６－００７ 于 2026年9月8日签订。项目预算决策。') for kb in KB]


class FakeOpenSearch:
    """REST storage fake: keyword conjunction only, no claim of ICU emulation."""
    def __init__(self):
        self.indices = {}
        self.configs = {}
        self.calls = []
        self.lexical_hits = []
        self.mutate = lambda response: response

    def __call__(self, method, path, payload, timeout):
        self.calls.append((method, path, copy.deepcopy(payload), timeout))
        index = path.split('/')[1]
        if method == 'PUT':
            if index in self.indices:
                raise candidates.CandidateError('collision')
            self.indices[index] = {}
            self.configs[index] = payload
            return {'acknowledged': True}
        if method == 'DELETE':
            del self.indices[index]
            return {'acknowledged': True}
        if path.endswith('/_bulk'):
            lines = [json.loads(line) for line in payload.splitlines()]
            for i in range(0, len(lines), 2):
                self.indices[index][lines[i]['create']['_id']] = lines[i+1]
            return {'errors': False, 'items': [{'create': {'status': 201}} for _ in lines[::2]]}
        if path.endswith('/_refresh'):
            return {'_shards': {'failed': 0}}
        if path.endswith('/_search'):
            filters = payload['query']['bool']['filter']
            if payload['query']['bool']['must']:
                hits = self.lexical_hits
            else:
                def matches(row):
                    for term in filters:
                        k, v = next(iter(term['term'].items()))
                        actual = row.get(k)
                        if not (v in actual if isinstance(actual, list) else v == actual):
                            return False
                    return True
                selected = [row for row in self.indices[index].values() if matches(row)]
                unique = {row['doc_id']: row for row in selected}
                hits = [{'_source': r} for r in list(unique.values())[:10]]
            return self.mutate({'timed_out': False, '_shards': {'failed': 0}, 'hits': {'hits': hits}})
        if path.endswith('/_mget'):
            return self.mutate({'docs': [{'_id': identifier, 'found': identifier in self.indices[index],
                '_source': self.indices[index].get(identifier, {})} for identifier in payload['ids']]})
        raise AssertionError('unexpected request')


class ExactAndATests(unittest.TestCase):
    def setUp(self):
        self.service = FakeOpenSearch()
        self.a = candidates.OpenSearchCandidate(self.service, 'tenant-test')
        self.a.build(documents())

    def tearDown(self):
        self.a.close()

    def test_nfkc_dash_case_and_calendar_dates_match_ingested_keywords(self):
        for query in ('abc-2026-007', 'ＡＢＣ–２０２６–００７', '2026/09/08', '2026.9.8', 'plan.pdf', '(plan.pdf)', '（Plan.PDF）', '[plan.pdf]', "'plan.pdf'"):
            with self.subTest(query=query):
                hits = self.a.search(query, KB[0])
                self.assertEqual([(h.kb_id, h.doc_id) for h in hits], [(KB[0], 'doc-1')])
                self.assertIn('项目预算决策', hits[0].text)
        self.assertEqual(candidates.exact_fields('2026-02-30')['date_values'], [])

    def test_exact_zero_does_not_fall_back_and_multiple_terms_are_conjoined(self):
        before = len(self.service.calls)
        self.assertEqual(self.a.search('ABC-2026-007 2026/09/09', KB[0]), [])
        calls = self.service.calls[before:]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]['query']['bool']['must'], [])

    def test_plain_text_uses_icu_bm25_collapse_and_one_parent_batch(self):
        rows = [r for r in self.service.indices[self.a.child_index].values() if r['kb_id'] == KB[0]]
        self.service.lexical_hits = [{'_source': rows[0]}]
        hits = self.a.search('项目预算决策', KB[0])
        self.assertEqual(hits[0].doc_id, 'doc-1')
        search = self.service.calls[-2][2]
        self.assertEqual(search['collapse'], {'field': 'doc_id'})
        self.assertEqual(search['size'], 10)
        self.assertEqual(search['query']['bool']['must'][0]['multi_match']['query'], '项目预算决策')
        self.assertNotIn('rescore', search)
        self.assertNotIn('body', search['_source'])
        config = self.service.configs[self.a.child_index]
        self.assertIn('body', config['mappings']['_source']['excludes'])
        self.assertEqual(config['settings']['analysis']['analyzer']['cwk_official_icu']['tokenizer'], 'icu_tokenizer')
        self.assertTrue(self.service.calls[-1][1].endswith('/_mget'))
        self.assertLess(self.service.calls[-1][3], self.service.calls[-2][3])

    def test_tenant_and_kb_filters_are_present_in_both_channels(self):
        for query in ('abc-2026-007', '预算'):
            self.a.search(query, KB[1])
            req = [c for c in self.service.calls if c[1].endswith('/_search')][-1][2]
            self.assertIn({'term': {'tenant_id': 'tenant-test'}}, req['query']['bool']['filter'])
            self.assertIn({'term': {'kb_id': KB[1]}}, req['query']['bool']['filter'])

    def test_parent_scope_and_partial_search_fail_closed(self):
        for mutate in (
            lambda r: {**r, 'timed_out': True} if 'hits' in r else r,
            lambda r: {**r, '_shards': {'failed': 1}} if 'hits' in r else r,
            lambda r: {'docs': []} if 'docs' in r else r,
        ):
            self.service.mutate = mutate
            with self.assertRaises(candidates.CandidateError): self.a.search('abc-2026-007', KB[0])
        self.service.mutate = lambda r: r
        parent = next(r for r in self.service.indices[self.a.parent_index].values() if r['kb_id'] == KB[0])
        parent['tenant_id'] = 'foreign'
        with self.assertRaises(candidates.CandidateLeak): self.a.search('abc-2026-007', KB[0])

    def test_large_parent_and_duplicate_collapsed_docs_fail(self):
        row = next(r for r in self.service.indices[self.a.child_index].values() if r['kb_id'] == KB[0])
        self.service.lexical_hits = [{'_source': row}, {'_source': row}]
        with self.assertRaises(candidates.CandidateError): self.a.search('预算', KB[0])
        self.service.indices[self.a.parent_index][row['parent_id']]['text'] = 'x' * (candidates.MAX_PARENT_BYTES + 1)
        with self.assertRaises(candidates.CandidateError): self.a.search('abc-2026-007', KB[0])

    def test_cleanup_only_owned_indices_and_cannot_rebuild(self):
        self.service.indices['production'] = {'untouched': True}
        self.a.close()
        self.assertEqual(self.service.indices, {'production': {'untouched': True}})
        with self.assertRaises(candidates.CandidateError): self.a.build(documents())

    def test_context_cleanup_runs_after_search_or_build_failure(self):
        service = FakeOpenSearch()
        with self.assertRaises(RuntimeError):
            with candidates.OpenSearchCandidate(service, 'test') as candidate:
                candidate.build(documents())
                raise RuntimeError('synthetic failure')
        self.assertEqual(service.indices, {})

    def test_failed_second_create_and_cleanup_failure_remain_visible(self):
        service = FakeOpenSearch()
        def transport(method, path, payload, timeout):
            if method == 'PUT' and path.endswith('-c'): raise candidates.CandidateError('create failed')
            return service(method, path, payload, timeout)
        a = candidates.OpenSearchCandidate(transport, 'test')
        with self.assertRaises(candidates.CandidateError): a.build(documents())
        self.assertFalse(a.ready)
        a.close()
        self.assertEqual(service.indices, {})
        original = self.a.request
        self.a.request = lambda *args: {'acknowledged': False}
        with self.assertRaises(candidates.CandidateError): self.a.close()
        self.assertEqual(len(self.a._owned), 2)
        self.a.request = original


class FakeWeKnora:
    def __init__(self):
        self.docs = {}
        self.calls = []
        self.status = 'completed'
        self.result = []
        self.fail_import_at = None

    def __call__(self, method, path, payload, timeout):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path.endswith('/knowledge/manual'):
            if len(self.docs) == self.fail_import_at: raise candidates.CandidateError('native import failed')
            identifier = 'native-' + str(len(self.docs))
            self.docs[identifier] = {'id': identifier, 'knowledge_base_id': path.split('/')[4], 'parse_status': self.status}
            return {'success': True, 'data': self.docs[identifier]}
        if method == 'GET':
            return {'success': True, 'data': self.docs[path.rsplit('/', 1)[-1]]}
        if path.endswith('/hybrid-search'):
            return {'success': True, 'data': self.result}
        raise AssertionError('unexpected request')


class NativeBTests(unittest.TestCase):
    def make(self):
        self.service = FakeWeKnora()
        self.b = candidates.WeKnoraCandidate(self.service, {kb: 'kb-' + str(i) for i, kb in enumerate(KB)})

    def test_native_ingestion_receipts_and_no_exact_or_rerank_injection(self):
        self.make()
        self.b.build(documents())
        imports = [c for c in self.service.calls if c[1].endswith('/manual')]
        self.assertEqual([c[2]['content'] for c in imports], [candidates.canonical_text(d) for d in documents()])
        self.assertTrue(all(c[2]['status'] == 'publish' for c in imports))
        self.service.result = [{'knowledge_id': 'native-0', 'content': '证据'}]
        self.assertEqual(self.b.search('ＡＢＣ－２０２６－００７', KB[0])[0].doc_id, 'doc-1')
        request = self.service.calls[-1]
        self.assertEqual(request[1], '/api/v1/knowledge-bases/kb-0/hybrid-search')
        self.assertEqual(request[2], {'query_text': 'ＡＢＣ－２０２６－００７', 'match_count': 10})

    def test_pending_import_is_not_ready_and_failed_partial_import_cannot_promote(self):
        self.make()
        self.service.status = 'processing'
        with self.assertRaises(candidates.CandidateError): self.b.build(documents())
        self.assertFalse(self.b.ready)
        for d in self.service.docs.values(): d['parse_status'] = 'completed'
        self.b.check_ready()
        self.assertTrue(self.b.ready)
        self.make()
        self.service.fail_import_at = 1
        with self.assertRaises(candidates.CandidateError): self.b.build(documents())
        with self.assertRaises(candidates.CandidateError): self.b.check_ready()
        self.assertFalse(self.b.ready)

    def test_unknown_and_cross_library_native_hits_are_leaks(self):
        self.make()
        self.b.build(documents())
        for identifier in ('foreign-id', 'native-1'):
            self.service.result = [{'knowledge_id': identifier, 'content': 'foreign'}]
            with self.assertRaises(candidates.CandidateLeak): self.b.search('项目', KB[0])

    def test_native_top_ten_budget_not_refilled_after_duplicates(self):
        self.make()
        self.b.build(documents())
        self.service.result = [{'knowledge_id': 'native-0', 'content': 'same'}] * 10 + [{'knowledge_id': 'native-0', 'content': 'extra'}]
        hits = self.b.search('项目', KB[0])
        self.assertEqual(len(hits), 10)
        self.assertEqual(len({h.doc_id for h in hits}), 1)
        self.service.result[-1]['knowledge_id'] = 'foreign'
        with self.assertRaises(candidates.CandidateLeak): self.b.search('项目', KB[0])

    def test_native_null_is_zero_hits_but_missing_data_is_error(self):
        self.make()
        self.b.build(documents())
        self.service.result = None
        self.assertEqual(self.b.search('absent', KB[0]), [])
        self.b.request = lambda *a: {'success': True}
        with self.assertRaises(candidates.CandidateError): self.b.search('absent', KB[0])
        self.b.request = lambda *a: {'success': False, 'data': None}
        with self.assertRaises(candidates.CandidateError): self.b.search('absent', KB[0])

    def test_native_null_no_answer_is_scored_correct_not_error(self):
        self.make()
        self.b.build(documents())
        def native(method, path, payload, timeout):
            if payload['query_text'].endswith('no-answer'):
                return {'success': True, 'data': None}
            ordinal = path.split('/')[4].split('-')[-1]
            return {'success': True, 'data': [{'knowledge_id': 'native-' + ordinal, 'content': 'synthetic'}]}
        self.b.request = native
        result = candidates.score_cases(self.b, cases())
        for metrics in result.values():
            self.assertEqual(metrics['no_answer_correct'], 1)
            self.assertEqual(metrics['system_error_count'], 0)

    def test_bindings_must_be_distinct_and_search_requires_ready(self):
        with self.assertRaises(candidates.CandidateError):
            candidates.WeKnoraCandidate(lambda *a: {}, {kb: 'same' for kb in KB})
        self.make()
        with self.assertRaises(candidates.CandidateError): self.b.search('项目', KB[0])
        self.assertEqual(self.service.calls, [])


def cases():
    return [case for kb in KB for case in (
        candidates.Case(kb, 'private-exact-query', frozenset({'doc-1'}), True),
        candidates.Case(kb, 'private-no-answer', frozenset()),
    )]


class ScoringTests(unittest.TestCase):
    def test_errors_are_never_correct_no_answer_and_no_details_escape(self):
        class Bad:
            def search(self, *args, **kwargs): raise candidates.CandidateTimeout('SECRET QUERY RESPONSE')
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            result = candidates.score_cases(Bad(), cases())
        self.assertEqual(out.getvalue(), '')
        self.assertNotIn('SECRET', json.dumps(result))
        for metrics in result.values():
            self.assertEqual(metrics['system_error_count'], 2)
            self.assertEqual(metrics['timeout_count'], 2)
            self.assertEqual(metrics['exact_system_error_count'], 1)
            self.assertEqual(metrics['no_answer_correct'], 0)
            self.assertGreater(metrics['p95_ms'], 0)
            self.assertNotIn('index_bytes', metrics)  # never fake resource measurements

    def test_quality_scores_and_cross_library_leak_is_failure(self):
        class Good:
            def search(self, query, kb_id, timeout):
                return [] if query.endswith('no-answer') else [candidates.Hit(kb_id, 'doc-1')]
        result = candidates.score_cases(Good(), cases())
        for metrics in result.values():
            self.assertEqual((metrics['exact'], metrics['no_answer'], metrics['recall_at_10']), (1, 1, 1))
        class Leak:
            def search(self, query, kb_id, timeout): return [candidates.Hit('foreign', 'doc-1')]
        result = candidates.score_cases(Leak(), cases())
        self.assertTrue(all(m['leak_count'] == 2 and m['recall_hits_at_10'] == 0 for m in result.values()))

    def test_missing_categories_rejected_before_search(self):
        class Never:
            def search(self, *a, **kw): raise AssertionError('must not run')
        with self.assertRaises(candidates.CandidateError): candidates.score_cases(Never(), cases()[:-1])
        with self.assertRaises(candidates.CandidateError): candidates.score_cases(Never(), cases() + cases())


class TransportTests(unittest.TestCase):
    def test_remote_ambiguous_or_credential_urls_rejected(self):
        for url in ('https://127.0.0.1:1', 'http://localhost:1', 'http://example.com:1',
                    'http://127.0.0.1:1/base', 'http://u:p@127.0.0.1:1', 'http://127.0.0.1:1?token=x'):
            with self.subTest(url=url), self.assertRaises(candidates.CandidateError): candidates.LoopbackJSON(url)

    def test_error_body_and_redirect_never_forwarded(self):
        client = candidates.LoopbackJSON('http://127.0.0.1:9876')
        with patch.object(client.opener, 'open', side_effect=urllib.error.HTTPError('private-query', 500, 'private detail', {}, io.BytesIO(b'private raw'))):
            with self.assertRaises(candidates.CandidateError) as error: client('POST', '/search', {'query': 'secret'}, 1)
        self.assertEqual(str(error.exception), 'native request failed')
        with self.assertRaises(candidates.CandidateError):
            candidates._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://remote/')

    def test_real_loopback_http_json_and_redirect_refusal(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path == '/redirect':
                    self.send_response(307)
                    self.send_header('Location', '/must-not-follow')
                    self.end_headers()
                else:
                    body = b'{"success": true, "data": null}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            client = candidates.LoopbackJSON('http://127.0.0.1:' + str(server.server_port))
            with patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                self.assertEqual(client('POST', '/search', {'query': 'synthetic'}, 1), {'success': True, 'data': None})
            with self.assertRaises(candidates.CandidateError): client('POST', '/redirect', {}, 1)
            self.assertEqual(seen, ['/search', '/redirect'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_timeout_classification_and_response_limit(self):
        client = candidates.LoopbackJSON('http://127.0.0.1:9876')
        with patch.object(client.opener, 'open', side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(candidates.CandidateTimeout): client('POST', '/search', {}, 1)
        with patch.object(client.opener, 'open', return_value=io.BytesIO(b' ' * (candidates.MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(candidates.CandidateError): client('POST', '/search', {}, 1)


if __name__ == '__main__':
    unittest.main()
