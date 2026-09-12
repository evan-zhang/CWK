"""Public deterministic native pending/terminal/deadline observations."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import kb_retrieval_candidates as kbc
import rt055_build_readiness as build
import rt055_run_b as b
import rt055_workload_readiness as work
import rt055_log_firewall as fw

class NativeReadinessTests(unittest.TestCase):
    def candidate(self,statuses):
        def request(method,path,payload,timeout):
            i=path.rsplit('/',1)[-1]
            return {'success':True,'data':{'id':i,'knowledge_base_id':'public-kb','parse_status':statuses[i]}}
        c=kbc.WeKnoraCandidate(request,{'cwork-3m':'public-kb'});c.documents={i:('cwork-3m','public-doc') for i in statuses};c._import_complete=True;return c
    def test_pending_and_later_terminal_immediately_rejected(self):
        c=self.candidate({'public-1':'pending','public-2':'failed'})
        with self.assertRaises(kbc.CandidateBuildFailed):c.check_ready()
        self.assertFalse(c.ready)
    def test_pending_completed_unknown(self):
        for status,exc in [('pending',kbc.CandidatePending),('processing',kbc.CandidatePending),('unknown',kbc.CandidateError)]:
            with self.assertRaises(exc):self.candidate({'public-1':status}).check_ready()
        c=self.candidate({'public-1':'completed'});c.check_ready();self.assertTrue(c.ready)
    def transport(self):
        t=b.RoutingTransport();t.add_server('cwork-3m',{'kb_id':'public-kb'});return t
    def test_terminal_not_polled_and_count_only_error(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'status.json';t=self.transport();c=Mock();c.build.side_effect=kbc.CandidateBuildFailed('PUBLIC must not appear')
            with patch.object(build.time,'sleep') as sleep:
                with self.assertRaises(kbc.CandidateBuildFailed):build.build_b(c,[],t,'cwork-3m',path,Mock())
                sleep.assert_not_called();c.check_ready.assert_not_called()
            row=json.loads(path.read_text());self.assertEqual(row['error'],'NATIVE_TERMINAL_FAILED');self.assertNotIn('must not appear',path.read_text())
    def test_deadline_pending_no_timeout_increase(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'status.json';t=self.transport();c=Mock(ready=False);c.build.side_effect=kbc.CandidatePending('pending');c.check_ready.side_effect=kbc.CandidatePending('pending')
            with self.assertRaises(kbc.CandidateTimeout):build.build_b(c,[],t,'cwork-3m',path,Mock(),timeout=.015,poll_seconds=.005)
            self.assertEqual(json.loads(path.read_text())['error'],'BUILD_DEADLINE')
            self.assertEqual(build.TIMEOUT,b.BUILD_TIMEOUT);self.assertEqual(build.TIMEOUT,7200)
    def test_count_instrumentation_does_not_emit_identifiers(self):
        t=self.transport();t._instrument('POST','/api/v1/knowledge-bases/public-kb/knowledge/manual',None,{'data':{'id':'public-id'}},'cwork-3m')
        t._instrument('GET','/api/v1/knowledge/public-id',None,{'data':{'parse_status':'failed'}},'cwork-3m')
        c=build.counts(t);self.assertEqual(c['cwork-3m'],{'imported':1,'completed':0,'pending':0,'failed':1});self.assertNotIn('public-id',json.dumps(c))
    def test_public_workload_counts_size_and_invalid_gate(self):
        for kb,n in work.COUNTS.items():
            docs=work.documents(kb);self.assertEqual(len(docs),n);self.assertLessEqual(max(len(d.text.encode()) for d in docs),524288)
        self.assertFalse(work.safe_result({}));self.assertFalse(work.safe_result({'status':'PASS'}))
    def test_sql_fault_probe_is_after_normal_build_and_search(self):
        import inspect
        code=inspect.getsource(work.run)
        self.assertLess(code.index('build.build_b('),code.index("db.execute('BEGIN IMMEDIATE')"))
        self.assertLess(code.index('hits=adapter.search('),code.index("db.execute('BEGIN IMMEDIATE')"))
        self.assertIn("timeout=build.TIMEOUT",code)
    def test_public_utf8_bytes_and_character_limits(self):
        for kb in work.COUNTS:
            for d in work.documents(kb):
                self.assertLessEqual(len(d.text.encode()),work.UPPER_BYTES)
                self.assertLessEqual(len(kbc.canonical_text(d)),200000)
    def test_native_http_errors_are_closed_and_message_free(self):
        for status in (400,422,500,999):
            e=build.NativeHTTPError(status);self.assertIn(build.error_code(e),build.HTTP_CODES)
            self.assertEqual(str(e),build.error_code(e))

class ClosedRequestDiagnosticTests(unittest.TestCase):
    def test_error_messages_map_only_to_closed_codes(self):
        mapping={'native ingestion status invalid':'NATIVE_STATE_UNRECOGNIZED',
            'invalid native ingestion receipt':'NATIVE_RECEIPT_INVALID',
            'response size exceeded':'NATIVE_RESPONSE_SIZE_EXCEEDED',
            'native response json invalid':'NATIVE_RESPONSE_JSON_INVALID',
            'native response encoding invalid':'NATIVE_RESPONSE_ENCODING_INVALID',
            'native transport failed':'NATIVE_TRANSPORT_FAILED',
            'redirect refused':'NATIVE_REDIRECT_REFUSED',
            'invalid request path':'NATIVE_ROUTE_INVALID'}
        for text,code in mapping.items():
            self.assertEqual(build.error_code(kbc.CandidateError(text)),code)
            self.assertIn(code,build.CODES)
        self.assertEqual(build.error_code(kbc.CandidateError('PUBLIC_CANARY http://secret.invalid private-path')),'REQUEST_FAILED')
    def test_transport_errors_are_classified_without_retry_or_body(self):
        import urllib.error
        cases=[(OSError('PUBLIC_CANARY'),'NATIVE_TRANSPORT_FAILED'),
            (urllib.error.URLError('PUBLIC_CANARY'),'NATIVE_TRANSPORT_FAILED'),
            (TimeoutError('PUBLIC_CANARY'),'BUILD_DEADLINE')]
        t=b.RoutingTransport();t.add_server('cwork-3m',{'kb_id':'public-kb','base_url':'http://127.0.0.1:1','token':'public-token'})
        for error,code in cases:
            opener=Mock();opener.open.side_effect=error
            with patch.object(b.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(kbc.CandidateError) as ctx:t._request('GET','/api/v1/knowledge/public-id',None,1,'cwork-3m')
            self.assertEqual(build.error_code(ctx.exception),code);self.assertNotIn('CANARY',str(ctx.exception));self.assertEqual(opener.open.call_count,1)
    def test_response_shape_encoding_size_remain_bounded(self):
        t=b.RoutingTransport();t.add_server('cwork-3m',{'kb_id':'public-kb','base_url':'http://127.0.0.1:1','token':'public-token'})
        for body,code in [(b'{PUBLIC_CANARY','NATIVE_RESPONSE_JSON_INVALID'),(b'\xff','NATIVE_RESPONSE_ENCODING_INVALID'),(b'x'*(kbc.MAX_RESPONSE_BYTES+1),'NATIVE_RESPONSE_SIZE_EXCEEDED')]:
            from unittest.mock import MagicMock
            opener=MagicMock();response=opener.open.return_value.__enter__.return_value;response.read.return_value=body
            with patch.object(b.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(kbc.CandidateError) as ctx:t._request('GET','/api/v1/knowledge/public-id',None,1,'cwork-3m')
            self.assertEqual(build.error_code(ctx.exception),code);self.assertNotIn('CANARY',str(ctx.exception))
            response.read.assert_called_once_with(kbc.MAX_RESPONSE_BYTES+1);self.assertEqual(opener.open.call_count,1)
