"""Public SQLite collision reproduction and serialized native import contract."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import kb_retrieval_candidates as kbc
import kb_stage_b_poc as poc
import rt055_run_b as runner
import rt055_build_readiness as build
import rt055_log_firewall as fw
KB='cwork-3m'
def docs():
    return [poc.SourceDocument(KB,'public-'+str(i),'Public '+str(i),'public-'+str(i)+'.md','Public synthetic solar energy '+str(i),{}) for i in range(2)]
class SQLiteJobs:
    """Real UNIQUE constraint; jobs allocate before asynchronously committing.

    GET advances each pending job, making interleaving reproducible without
    timing luck. This is not WeKnora; the real OPS workload is separate evidence.
    """
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE chunks(seq_id INTEGER UNIQUE)')
        self.jobs={};self.events=[];self.collisions=0;self.max_inflight=0
    def __call__(self,method,path,payload,timeout):
        if method=='POST':
            i='public-'+str(len(self.jobs));seq=self.db.execute('SELECT COALESCE(MAX(seq_id),0)+1 FROM chunks').fetchone()[0]
            self.jobs[i]=[seq,'pending',0];self.events.append('POST');self.max_inflight=max(self.max_inflight,sum(x[1]=='pending' for x in self.jobs.values()))
        else:
            i=path.rsplit('/',1)[-1];job=self.jobs[i];job[2]+=1
            if job[2]>1 and job[1]=='pending':
                try:self.db.execute('INSERT INTO chunks VALUES (?)',(job[0],));self.db.commit();job[1]='completed'
                except sqlite3.IntegrityError:self.collisions+=1;job[1]='failed'
            self.events.append(job[1])
        return {'success':True,'data':{'id':i,'knowledge_base_id':'public-kb','parse_status':self.jobs[i][1]}}
class SerializedTests(unittest.TestCase):
    def test_real_sqlite_serialization_prevents_unique_collision(self):
        t=SQLiteJobs();self.addCleanup(t.db.close);c=kbc.WeKnoraCandidate(t,{KB:'public-kb'})
        try:c.build(docs(),timeout=2)
        except kbc.CandidatePending:c.check_ready(timeout=2)
        self.assertTrue(c.ready);self.assertEqual(t.collisions,0);self.assertEqual(t.max_inflight,1)
        posts=[i for i,x in enumerate(t.events) if x=='POST'];self.assertEqual(len(posts),2)
        self.assertEqual(t.events[posts[1]-1],'completed')
    def test_concurrent_schedule_reproduces_sqlite_unique(self):
        t=SQLiteJobs();self.addCleanup(t.db.close)
        for d in docs():t('POST','/manual',{},1)
        for i in range(2):
            for _ in range(2):t('GET','/knowledge/public-'+str(i),None,1)
        self.assertEqual(t.collisions,1);self.assertEqual(t.max_inflight,2)
    def transport(self):
        t=runner.RoutingTransport();t.add_server(KB,{'kb_id':'public-kb'});return t
    def instrumented_request(self,t):
        def request(method,path,payload,timeout,kb):
            if method=='POST':data={'data':{'id':'public-'+str(t.post_count[kb])}}
            else:data={'data':{'parse_status':'completed'}}
            t._instrument(method,path,payload,data,kb);return data
        return request
    def post(self,t,value):return t('POST','/api/v1/knowledge-bases/public-kb/knowledge/manual',{'content':value},1)
    def test_second_post_before_completed_rejected_before_io(self):
        t=self.transport()
        with patch.object(t,'_request',side_effect=self.instrumented_request(t)) as request:
            self.post(t,'first')
            with self.assertRaises(kbc.CandidateImportContractError):self.post(t,'second')
            self.assertEqual(request.call_count,1)
            t('GET','/api/v1/knowledge/public-1',None,1);self.post(t,'second')
            self.assertEqual(t.max_inflight[KB],1);self.assertEqual(t.post_count[KB],2);self.assertEqual(t.completed_before_next_post[KB],1)
    def test_duplicate_post_even_after_completion_rejected_before_io(self):
        t=self.transport()
        with patch.object(t,'_request',side_effect=self.instrumented_request(t)) as request:
            self.post(t,'same');t('GET','/api/v1/knowledge/public-1',None,1)
            with self.assertRaises(kbc.CandidateImportContractError):self.post(t,'same')
            self.assertEqual(request.call_count,2)
    def test_ambiguous_post_is_never_retried(self):
        t=self.transport()
        with patch.object(t,'_request',side_effect=kbc.CandidateTimeout('public')) as request:
            with self.assertRaises(kbc.CandidateTimeout):self.post(t,'same')
            with self.assertRaises(kbc.CandidateImportContractError):self.post(t,'same')
            self.assertEqual(request.call_count,1)
    def test_terminal_failed_aborts_without_sleep_or_second_post(self):
        def request(method,path,payload,timeout):
            return {'success':True,'data':{'id':'public-1','knowledge_base_id':'public-kb','parse_status':'failed'}}
        r=Mock(side_effect=request);c=kbc.WeKnoraCandidate(r,{KB:'public-kb'})
        with patch.object(kbc.time,'sleep') as sleep:
            with self.assertRaises(kbc.CandidateBuildFailed):c.build(docs())
            sleep.assert_not_called()
        self.assertEqual([x.args[0] for x in r.call_args_list],['POST','GET'])
        with self.assertRaises(kbc.CandidateError):c.check_ready()
        with self.assertRaises(kbc.CandidateImportContractError):c.build(docs())
        self.assertEqual(r.call_count,2)
    def test_pending_timeout_single_post_no_deadline_reset(self):
        def request(method,path,payload,timeout):
            return {'success':True,'data':{'id':'public-1','knowledge_base_id':'public-kb','parse_status':'pending'}}
        r=Mock(side_effect=request);c=kbc.WeKnoraCandidate(r,{KB:'public-kb'});start=time.monotonic()
        with self.assertRaises(kbc.CandidateTimeout):c.build(docs(),timeout=.02)
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(sum(x.args[0]=='POST' for x in r.call_args_list),1)
        with self.assertRaises(kbc.CandidateError):c.check_ready()
    def test_total_budget_includes_serial_waits(self):
        t=SQLiteJobs();self.addCleanup(t.db.close);c=kbc.WeKnoraCandidate(t,{KB:'public-kb'})
        now=[0.0]
        with patch.object(kbc.time,'monotonic',side_effect=lambda:now[0]),patch.object(kbc.time,'sleep',side_effect=lambda n:now.__setitem__(0,now[0]+n)):
            with self.assertRaises(kbc.CandidateTimeout):c.build(docs(),timeout=.3)
        self.assertEqual(t.events.count('POST'),2);self.assertFalse(c.ready)
    def test_firewall_exception_prevents_network(self):
        t=self.transport();t.observer=Mock(side_effect=fw.FirewallError('DRAIN_ERROR'))
        with patch.object(t,'_request') as request:
            with self.assertRaises(fw.FirewallError):self.post(t,'public')
            request.assert_not_called()
        self.assertEqual(t.post_count[KB],0)
    def test_lock_rejects_overlapping_transport_requests(self):
        t=self.transport();t._import_locks[KB].acquire()
        try:
            with patch.object(t,'_request') as request:
                with self.assertRaises(kbc.CandidateImportContractError):self.post(t,'public')
                request.assert_not_called()
        finally:t._import_locks[KB].release()
    def test_private_receipt_counts_and_phase_no_id_value_hash(self):
        with tempfile.TemporaryDirectory() as td:
            t=self.transport();t._request=self.instrumented_request(t)
            c=Mock();c.build.side_effect=kbc.CandidateBuildFailed('SECRET_PUBLIC_CANARY')
            path=Path(td)/'status.json'
            with self.assertRaises(kbc.CandidateBuildFailed):build.build_b(c,[],t,KB,path,Mock())
            row=json.loads(path.read_text());self.assertEqual(row['error'],'NATIVE_TERMINAL_FAILED')
            self.assertEqual(set(row['libraries'][KB]),{'imported','completed','pending','failed'})
            self.assertNotIn('SECRET_PUBLIC_CANARY',path.read_text());self.assertNotIn('public-kb',path.read_text())
    def test_finalize_failure_path_populates_verified_stream_summary(self):
        import rt055_workload_readiness as work
        from types import SimpleNamespace
        receipt={'verified':True,'logs':[{'eof':True,'closed':True,'overflow':False,'input_bytes':5,'output_bytes':4,'redaction_count':1,'error':'NONE'}]}
        with tempfile.TemporaryDirectory() as td:
            space=SimpleNamespace(ledger=Path(td)/'workspace.json');row={}
            with patch.object(work.fw,'finalize',return_value=receipt),patch.object(work.cw,'scan',return_value={'passed':True,'hit_files':0}):
                work.finalize_streams(space,row,['public'])
            self.assertTrue(row['firewall_verified']);self.assertTrue(row['eof_all']);self.assertEqual(row['redactions'],1)
            receipt['logs'][0]['overflow']=True
            with patch.object(work.fw,'finalize',return_value=receipt),patch.object(work.cw,'scan',return_value={'passed':True,'hit_files':0}):
                with self.assertRaises(fw.FirewallError):work.finalize_streams(space,{},['public'])

class FinalizingContractTests(unittest.TestCase):
    """Pinned native finalizing is not completed; reproduce scheduling without private input."""
    def test_finalizing_waits_for_completed_before_next_post(self):
        events=[];states=iter(('processing','finalizing','finalizing','completed','finalizing','completed'));current=[0]
        def request(method,path,payload,timeout):
            if method=='POST':current[0]+=1;state='pending'
            else:state=next(states)
            events.append((method,state))
            return {'success':True,'data':{'id':'public-'+str(current[0]),'knowledge_base_id':'public-kb','parse_status':state}}
        c=kbc.WeKnoraCandidate(request,{KB:'public-kb'})
        with patch.object(kbc.time,'sleep'):c.build(docs(),timeout=2)
        self.assertTrue(c.ready)
        posts=[i for i,x in enumerate(events) if x[0]=='POST'];self.assertEqual(len(posts),2)
        self.assertEqual(events[posts[1]-1],('GET','completed'))
        self.assertEqual(sum(x==('GET','finalizing') for x in events),3)
    def test_finalizing_never_becomes_ready_at_deadline(self):
        calls=[]
        def request(method,path,payload,timeout):
            calls.append(method)
            return {'success':True,'data':{'id':'public-1','knowledge_base_id':'public-kb','parse_status':'finalizing'}}
        c=kbc.WeKnoraCandidate(request,{KB:'public-kb'})
        with self.assertRaises(kbc.CandidateTimeout):c.build(docs(),timeout=.01)
        self.assertFalse(c.ready);self.assertEqual(calls.count('POST'),1)
    def test_finalizing_then_failed_aborts_no_reimport(self):
        states=iter(('pending','finalizing','failed'));calls=[]
        def request(method,path,payload,timeout):
            calls.append(method)
            return {'success':True,'data':{'id':'public-1','knowledge_base_id':'public-kb','parse_status':next(states)}}
        c=kbc.WeKnoraCandidate(request,{KB:'public-kb'})
        with patch.object(kbc.time,'sleep'):
            with self.assertRaises(kbc.CandidateBuildFailed):c.build(docs(),timeout=2)
        self.assertEqual(calls,['POST','GET','GET']);self.assertFalse(c.ready)
    def test_unrecognized_state_remains_closed_failure(self):
        with self.assertRaises(kbc.CandidateError) as ctx:kbc.native_import_state('PUBLIC_UNRECOGNIZED_CANARY')
        self.assertEqual(build.error_code(ctx.exception),'NATIVE_STATE_UNRECOGNIZED')
        self.assertNotIn('CANARY',str(ctx.exception))
