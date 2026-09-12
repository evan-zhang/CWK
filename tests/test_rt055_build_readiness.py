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
