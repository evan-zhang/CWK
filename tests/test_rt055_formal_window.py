"""Append-only formal-window wiring: public synthetic data, no OPS calls."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rt055_baseline as baseline
import rt055_freeze as freeze
import rt055_runtime as runtime
import test_rt055_privacy_recovery as privacy_tests


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='rt055-window-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/('rt055-'+str(uuid.uuid4()));self.root.mkdir(mode=0o700)
        self.wid=str(uuid.uuid4());self.mid=str(uuid.uuid4())
        (self.root/'.rt055-owned').write_text(self.root.name.removeprefix('rt055-'))
        for name in ('status','audit','impl','builder','verifier','downloads','bin','sidecar','jieba','jdk'):
            (self.root/name).mkdir()
        self.old={name:b'{"historical":"unchanged"}' for name in ('status/baseline-before.claim','audit/production-before.json','audit/confidentiality-recovery.json')}
        for name,data in self.old.items():(self.root/name).write_bytes(data)
        self.local={'health':{'1':{'stable_sha256':'synthetic','http_200':True}},'gateways':[],
                    'existing_search_indices':{},'configuration':{},'containers':[],'volumes':[],'services':[]}
        self.nas={kb:dict(existing_indices={},index_content_fingerprints={},configuration_sha256='synthetic') for kb in runtime.ops.LIBRARIES}

    def collect(self,mode='before',wid=None):
        with patch.object(baseline,'ROOT',self.root),patch.object(baseline,'local_state',return_value=copy.deepcopy(self.local)),patch.object(baseline,'nas_state',return_value=copy.deepcopy(self.nas)):
            return baseline.main([mode,'--window-id',wid or self.wid])

    def test_new_window_before_and_after_leave_history_unchanged(self):
        self.assertEqual(self.collect(),0)
        w=self.root/'formal-windows'/self.wid
        before=(w/'audit/production-before.json').read_bytes()
        self.assertEqual(self.collect('after'),0)
        comparison=json.loads((w/'audit/production-comparison.json').read_text())
        self.assertEqual(comparison['window_id'],self.wid)
        self.assertEqual(comparison['mode'],'comparison')
        self.assertIsNone(comparison['nas_unchanged'])
        self.assertEqual((w/'audit/production-before.json').read_bytes(),before)
        for name,data in self.old.items():self.assertEqual((self.root/name).read_bytes(),data)

    def test_second_before_claim_rejected_without_overwrite(self):
        self.assertEqual(self.collect(),0)
        w=self.root/'formal-windows'/self.wid
        before={str(p):p.read_bytes() for p in w.rglob('*') if p.is_file()}
        with self.assertRaises(FileExistsError):self.collect()
        self.assertTrue(all(Path(p).read_bytes()==v for p,v in before.items()))

    def test_after_without_same_window_before_rejected(self):
        self.assertNotEqual(self.collect('after'),0)

    def migration(self):
        import rt055_window as window
        for name in runtime.MIGRATION_SOURCE_FILES:
            p=self.root/'impl'/name
            if name=='aggregate-report.schema.json':p.write_bytes((Path(__file__).parents[1]/'RT/RT-055/contracts'/name).read_bytes())
            else:p.write_text('public synthetic '+name)
        attempt=self.root/'executioner-migrations'/self.mid/'rt055-synthetic-001'
        for name in ('audit','status','impl'):(attempt/name).mkdir(parents=True,exist_ok=True)
        (attempt/'.rt055-owned').write_text(self.root.name.removeprefix('rt055-'))
        for name in runtime.MIGRATION_SOURCE_FILES:(attempt/'impl'/name).write_bytes((self.root/'impl'/name).read_bytes())
        obs=privacy_tests.GateTests().fixture()
        obs.update(candidate_workspace_cleanup_zero=True,candidate_workspace_candidates=['a','b'])
        window.write_once(attempt/'audit/confidentiality-observations.json',obs)
        window.write_once(attempt/'status/confidentiality.json',{'status':'PASS','phase':'COMPLETE'})
        window.write_once(attempt/'audit/synthetic-cleanup.json',{'complete':True,'failures':0,'remaining_processes':0,'remaining_data_planes':0})
        manifest={n:runtime.ops.sha_file(self.root/'impl'/n) for n in runtime.MIGRATION_SOURCE_FILES}
        window.write_once(self.root/'executioner-migrations'/self.mid/'deployment.json',{'schema':'cwk.rt055.executioner-deployment.v1','run_id':self.root.name.removeprefix('rt055-'),'migration_id':self.mid,'code_commit':'1'*40,'source_files':manifest})
        runtime.bind_privacy_migration(self.root,self.mid,1)
        return attempt

    def prepare_policy(self,wid=None):
        from types import SimpleNamespace
        import rt055_runtime_readiness as readiness
        original_run=runtime.subprocess.run
        def probe(args,**kwargs):
            if args[0]!='/usr/bin/sandbox-exec':return original_run(args,**kwargs)
            kind='inbound-only' if str(args[2]).endswith('search-network.sb') else 'loopback'
            return SimpleNamespace(returncode=0,stdout=json.dumps(readiness.NETWORK['policy_observations'][kind]))
        with patch.object(runtime.subprocess,'run',side_effect=probe):
            readiness.prepare(self.root,wid or self.wid,self.mid)
        self.workspace_fixture(wid or self.wid)

    def workspace_fixture(self,wid):
        # Unit fixture only. OPS evidence is produced by the real startup CLI.
        import time
        import rt055_candidate_workspace as cw
        import rt055_candidate_startup as startup
        import rt055_runtime_readiness as readiness
        import rt055_window as window
        r=readiness.verify(self.root,wid,self.mid);base=readiness.directory(self.root,wid)
        row={'schema':startup.SCHEMA,**window.envelope(self.root,wid,'candidate-startup-readiness'),
             'migration_id':self.mid,'source_commit':r['source_commit'],'source_files':r['source_files'],
             'policy_files':r['files'],'policy_receipt_sha256':runtime.ops.sha_file(base/'receipt.json'),
             'status':'PASS','a_started':3,'b_started':3,'sidecars_started':3,'private_reads':0,'formal_attempts':0,
             'formal_queries':0,'cleanup_failures':0,'remaining_runtime':0,'started_at':time.time(),'leases':[]}
        for key in ('a','b'):
            space=cw.create(self.root,wid,key,str(uuid.uuid4()),synthetic=True,migration_id=self.mid)
            cw.file(space,'logs','public.log').write_text('service ready')
            cw.scan(space,['PUBLIC CANARY']);cw.cleanup(space)
            row['leases'].append({'candidate':key,'attempt_id':space.attempt_id,
                  'owner_sha256':runtime.ops.sha_file(space.ledger),'scan_sha256':runtime.ops.sha_file(space.ledger.parent/'log-scan.json'),
                  'cleanup_sha256':runtime.ops.sha_file(space.ledger.parent/'workspace-cleanup.json')})
        row['finished_at']=time.time();window.write_once(base/'workspace-readiness.json',row)


    def freeze_fixture(self,policy=True):
        self.migration()
        if policy:self.prepare_policy();self.collect()
        checks={'verified':True,'participating_libraries':list(runtime.ops.LIBRARIES),'deferred_libraries':[]}
        for name,value in {'builder/private-corpus.json':{},'verifier/private-verified.json':{},'verifier/case-verification.json':checks}.items():runtime.ops.write_private_json(self.root/name,value)
        for name in ('downloads/opensearch.tar.gz','bin/weknora-server','jdk/public','sidecar/requirements-freeze.txt'):(self.root/name).write_text('public')
        for name in runtime.JIEBA_FILES:(self.root/'jieba'/name).write_text('public')
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(freeze,'role_audit',return_value={'verified':True,'role_separation_level':'PROCESS_LEVEL_SEPARATION_SINGLE_UID'}))
        stack.enter_context(patch.object(freeze,'upstream',return_value={'verified_on_ops':True,'head_matches_commit':True,'tree_clean':True,'receipt_id':'ops-rt055-weknora-upstream','repository_id':'github.com/Tencent/WeKnora','commit':freeze.PINNED,'commit_reachable':True}))
        return self.root/'formal-windows'/self.wid

    def test_fresh_window_freeze_and_reverify(self):
        w=self.freeze_fixture();freeze.create(self.root,self.wid,self.mid)
        self.assertTrue(freeze.verify_artifacts(self.root,self.wid))
        receipt=json.loads((w/'freeze/freeze-receipt.json').read_text())
        self.assertEqual(receipt['window_id'],self.wid)
        self.assertEqual(receipt['privacy_migration_id'],self.mid)
        self.assertIn(f'formal-windows/{self.wid}/audit/production-before.json',receipt['private_files'])
        self.assertNotIn('audit/production-before.json',receipt['private_files'])
        self.assertFalse(freeze.verify_artifacts(self.root,str(uuid.uuid4())))
        old=(w/'freeze/freeze-receipt.json').read_bytes()
        with self.assertRaises(FileExistsError):freeze.create(self.root,self.wid,self.mid)
        self.assertEqual((w/'freeze/freeze-receipt.json').read_bytes(),old)

    def test_wrong_mode_old_before_missing_before_and_wrong_window_rejected(self):
        w=self.freeze_fixture();p=w/'audit/production-before.json';original=p.read_bytes()
        for change in ('after','wrong-window','old','missing'):
            with self.subTest(change=change):
                value=json.loads(original)
                if change=='after':value['mode']='after'
                elif change=='wrong-window':value['window_id']=str(uuid.uuid4())
                elif change=='old':value=json.loads(self.old['audit/production-before.json'])
                if change=='missing':p.unlink()
                else:p.write_text(json.dumps(value))
                with self.assertRaises((RuntimeError,ValueError,OSError)):freeze.create(self.root,self.wid,self.mid)
                p.write_bytes(original)

    def test_privacy_requires_new_observations_and_current_sources(self):
        attempt=self.migration()
        self.assertTrue(runtime.privacy_passed(self.root,self.mid))
        with self.assertRaises(FileExistsError):runtime.bind_privacy_migration(self.root,self.mid,1)
        p=attempt/'audit/confidentiality-observations.json';old=p.read_bytes();p.unlink()
        self.assertFalse(runtime.privacy_passed(self.root,self.mid));p.write_bytes(old)
        (self.root/'impl/rt055_freeze.py').write_text('drift')
        self.assertFalse(runtime.privacy_passed(self.root,self.mid))
        self.assertEqual((self.root/'audit/confidentiality-recovery.json').read_bytes(),self.old['audit/confidentiality-recovery.json'])

    def test_changed_observations_cannot_be_approved_by_only_rehashing_receipt(self):
        w=self.freeze_fixture();m=self.root/'executioner-migrations'/self.mid
        p=m/'rt055-synthetic-001/audit/confidentiality-observations.json'
        obs=json.loads(p.read_text());obs['log_canary_hits']=1;p.write_text(json.dumps(obs))
        receipt=m/'privacy-receipt.json';row=json.loads(receipt.read_text());row['observations_sha256']=runtime.ops.sha_file(p);receipt.write_text(json.dumps(row))
        self.assertFalse(runtime.privacy_passed(self.root,self.mid))
        with self.assertRaises(RuntimeError):freeze.create(self.root,self.wid,self.mid)

    def test_frozen_private_input_drift_rejected_without_export(self):
        self.freeze_fixture();freeze.create(self.root,self.wid,self.mid)
        (self.root/'builder/private-corpus.json').write_text('{"synthetic":"changed"}')
        self.assertFalse(freeze.verify_artifacts(self.root,self.wid))

    def test_freeze_rejects_missing_migration_and_source_drift(self):
        w=self.freeze_fixture()
        with self.assertRaises(RuntimeError):freeze.create(self.root,self.wid,str(uuid.uuid4()))
        freeze.create(self.root,self.wid,self.mid)
        (self.root/'impl/rt055_baseline.py').write_text('drift')
        self.assertFalse(freeze.verify_artifacts(self.root,self.wid))

    def test_rehashed_after_cannot_impersonate_before(self):
        import rt055_window as window
        w=self.freeze_fixture();p=w/'audit/production-before.json'
        value=json.loads(p.read_text());value['mode']='after';p.write_text(json.dumps(value))
        status=w/'status/baseline-before.json';s=json.loads(status.read_text());s['artifact_sha256']=runtime.ops.sha_file(p);status.write_text(json.dumps(s))
        with self.assertRaises(RuntimeError):window.baseline(self.root,self.wid,'before')

    def test_uuid_and_symlink_window_rejected(self):
        import rt055_window as window
        for bad in ('../old','formal','',None,'1'*36):
            with self.assertRaises((ValueError,RuntimeError,TypeError)):window.directory(self.root,bad)
        base=self.root/'formal-windows';base.mkdir();(base/self.wid).symlink_to(self.root/'audit')
        with self.assertRaises(RuntimeError):window.directory(self.root,self.wid)

    def test_verification_claim_is_append_only_and_binds_receipt_bytes(self):
        import rt055_window as window
        w=self.freeze_fixture();freeze.create(self.root,self.wid,self.mid)
        self.assertEqual(freeze.verify_once(self.root,self.wid),0)
        self.assertTrue(window.verification(self.root,self.wid)['verified'])
        old=(w/'verifier/freeze-verification.json').read_bytes()
        with self.assertRaises(FileExistsError):freeze.verify_once(self.root,self.wid)
        self.assertEqual((w/'verifier/freeze-verification.json').read_bytes(),old)
        p=w/'freeze/freeze-receipt.json';r=json.loads(p.read_text());r['run_order'].reverse();p.write_text(json.dumps(r))
        with self.assertRaises(RuntimeError):window.verification(self.root,self.wid)

    def expose_fixture(self,key,kb):
        import rt055_window as window
        attempt=str(uuid.uuid4())
        window.write_once(window.directory(self.root,self.wid)/('run-'+key)/'attempts'/attempt/'claim.json',
            {**window.envelope(self.root,self.wid,'execution-attempt'),'candidate':key})
        window.library_arm(self.root,self.wid,key,kb,attempt)
        window.library_expose(self.root,self.wid,key,kb,attempt)

    def test_each_library_consumed_once_and_ambiguous_claim_not_replayed(self):
        import rt055_window as window
        w=self.freeze_fixture();freeze.create(self.root,self.wid,self.mid);freeze.verify_once(self.root,self.wid)
        kb=runtime.ops.LIBRARIES[0]
        self.expose_fixture('a',kb)
        with self.assertRaises(RuntimeError):window.library_result(self.root,self.wid,'a',kb)
        window.library_scored(self.root,self.wid,'a',kb,{'total_count':42})
        window.library_complete(self.root,self.wid,'a',kb,{'metrics':{'total_count':42}})
        self.assertEqual(window.library_result(self.root,self.wid,'a',kb)['metrics']['total_count'],42)
        with self.assertRaises(RuntimeError):self.expose_fixture('a',kb)
        with self.assertRaises(FileExistsError):window.library_complete(self.root,self.wid,'a',kb,{'metrics':{'total_count':42}})

    def test_after_comparison_recomputed_and_wrong_window_refused(self):
        import rt055_window as window
        self.collect();self.collect('after')
        p=window.directory(self.root,self.wid)/'audit/production-comparison.json'
        self.assertIsNone(window.comparison(self.root,self.wid)['nas_unchanged'])
        old=p.read_bytes();v=json.loads(old);v['nas_unchanged']=True;p.write_text(json.dumps(v))
        with self.assertRaises(RuntimeError):window.comparison(self.root,self.wid)
        v=json.loads(old);v['window_id']=str(uuid.uuid4());p.write_text(json.dumps(v))
        with self.assertRaises(RuntimeError):window.comparison(self.root,self.wid)

    def test_v3_formal_binding_rejects_cross_window_missing_privacy_and_legacy(self):
        import test_rt055_retrieval_decision as fixture
        import kb_retrieval_decision as decision
        import jsonschema
        good=fixture.valid_report();decision.validate_report(good)
        schema=json.loads((Path(fixture.__file__).parents[1]/'RT/RT-055/contracts/aggregate-report.schema.json').read_text())
        for field in ('before_window_id','after_window_id','freeze_window_id','verification_window_id'):
            bad=copy.deepcopy(good);bad['formal_window'][field]=str(uuid.uuid4())
            with self.assertRaises(decision.ReportError):decision.validate_report(bad)
        for field in ('privacy_revalidation_verified','before_verified','after_comparison_verified'):
            bad=copy.deepcopy(good);bad['formal_window'][field]=False
            with self.assertRaises(decision.ReportError):decision.validate_report(bad)
            with self.assertRaises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
        bad=copy.deepcopy(good);del bad['formal_window']
        with self.assertRaises(decision.ReportError):decision.validate_report(bad)
        with self.assertRaises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
        bad=copy.deepcopy(good);bad['candidates'][decision.CANDIDATE_A]['freeze_receipt']['window_id']=str(uuid.uuid4())
        with self.assertRaises(decision.ReportError):decision.validate_report(bad)

    def test_synthetic_or_old_results_cannot_substitute_formal_consumption(self):
        import rt055_window as window
        import test_rt055_retrieval_decision as fixture
        w=self.freeze_fixture();freeze.create(self.root,self.wid,self.mid);freeze.verify_once(self.root,self.wid)
        c=fixture.candidate_a();p=w/'run-a/result.json'
        value={'schema':'cwk.rt055.run-a.result.v1','mode':'smoke','status':'OK','window_id':self.wid,'libraries':c['libraries'],'gateway_readiness':c['gateway_readiness']}
        window.write_once(p,value)
        with self.assertRaises(RuntimeError):window.validate_run(self.root,self.wid,'a',list(runtime.ops.LIBRARIES))
        value['mode']='run';p.write_text(json.dumps(value))
        with self.assertRaises(RuntimeError):window.validate_run(self.root,self.wid,'a',list(runtime.ops.LIBRARIES))
        for kb in runtime.ops.LIBRARIES:
            self.expose_fixture('a',kb)
            window.library_scored(self.root,self.wid,'a',kb,c['libraries'][kb])
            window.library_complete(self.root,self.wid,'a',kb,{'metrics':c['libraries'][kb],'gateway_readiness':c['gateway_readiness']})
        self.assertEqual(window.validate_run(self.root,self.wid,'a',list(runtime.ops.LIBRARIES))['mode'],'run')
        value['libraries'][runtime.ops.LIBRARIES[0]]['recall_at_10']=0;p.write_text(json.dumps(value))
        with self.assertRaises(RuntimeError):window.validate_run(self.root,self.wid,'a',list(runtime.ops.LIBRARIES))

    def runner_entrypoint(self,key,leak=False):
        import importlib
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock
        import rt055_window as window
        import test_rt055_retrieval_decision as fixture
        runner=importlib.import_module('rt055_run_'+key)
        w=self.freeze_fixture();(self.root/'.rt055-owned').touch()
        with patch.object(freeze.secrets,'randbits',return_value=1 if key=='a' else 0):freeze.create(self.root,self.wid,self.mid)
        freeze.verify_once(self.root,self.wid)
        frozen_plan=freeze.artifact_plan(self.root)
        import kb_retrieval_candidates as kbc
        cases=[c for kb in runtime.ops.LIBRARIES for c in (kbc.Case(kb,'public exact',frozenset({'public-doc'}),True),kbc.Case(kb,'public absent',frozenset()))];calls=[]
        root=self.root;outer=self
        gateway=fixture.candidate_a()['gateway_readiness']
        class Candidate:
            ready=True;child_index='public-child';parent_index='public-parent'
            def __init__(self,*a,**kw):pass
            def build(self,*a,**kw):time.sleep(.005)
            def search(self,q,kb,**kw):
                if q.startswith('public '):
                    outer.assertTrue((root/'exposure'/key/(kb+'.json')).is_file())
                    calls.append(kb)
                return []
            def close(self):pass
        def server(kb,port,side,data,log,rng,*,window_id=None,workspace=None):
            outer.assertEqual(window_id,outer.wid)
            outer.assertIn('candidate-runtime',str(log))
            log.write_text(cases[0].query if leak else 'public service started')
            return {'kb_id':kb,'proc':Mock(pid=123),'data_dir':data,'base_url':'http://127.0.0.1:41101'}
        def sidecar(port,hf,log,*,window_id=None,workspace=None):
            outer.assertEqual(window_id,outer.wid)
            log.write_text('public sidecar started');return Mock(pid=124)
        def search_start(kb,port,log,mode,*,window_id=None,workspace=None):
            outer.assertEqual(window_id,outer.wid)
            outer.assertIn('candidate-runtime',str(log));log.write_text(cases[0].query if leak else 'service started')
            return Mock(pid=123),{'base_url':'http://127.0.0.1:41101'}
        class Response:
            def __init__(self,url):self.url=url
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'{"_all":{"primaries":{"store":{"size_in_bytes":1000}}}}' if '/_stats/store' in self.url else b'[]'
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(freeze,'artifact_plan',return_value=frozen_plan))
            for name,value in (('ROOT',self.root),('load_documents',lambda *a:[]),('load_cases',lambda *a:cases),('free_port',lambda *a:41101)):
                stack.enter_context(patch.object(runner,name,value))
            stack.enter_context(patch.object(sys,'argv',['runner','--mode','run','--window-id',self.wid]))
            stack.enter_context(patch.object(runtime,'gateway_probe',return_value=gateway))
            stack.enter_context(patch.object(runtime,'data_bytes',return_value=1000))
            stack.enter_context(patch.object(runner.ops,'RssSampler',return_value=Mock(stop=Mock(return_value=10000))))
            stack.enter_context(patch.object(runner.ops,'stop_process'))
            if key=='a':
                stack.enter_context(patch.object(runner,'launch_opensearch',side_effect=search_start))
                stack.enter_context(patch.object(runner,'verify_icu'))
                stack.enter_context(patch.object(runner.kbc,'OpenSearchCandidate',Candidate))
                stack.enter_context(patch.object(runner.urllib.request,'urlopen',side_effect=lambda url,**kw:Response(url)))
            else:
                stack.enter_context(patch.object(runner,'launch_sidecar',side_effect=sidecar))
                stack.enter_context(patch.object(runner,'setup_instance',side_effect=server))
                stack.enter_context(patch.object(runner.kbc,'WeKnoraCandidate',Candidate))
            if leak:
                self.assertEqual(runner.main(),2)
                self.assertFalse((w/('run-'+key)/'result.json').exists())
                self.assertFalse(list(w.glob('run-'+key+'/complete/*.json')))
                self.assertFalse((root/'candidate-runtime').exists())
                scans=list(w.glob('run-'+key+'/attempts/*/log-scan.json'))
                self.assertEqual(len(scans),1);self.assertFalse(runtime.ops.read_json(scans[0])['passed'])
                return
            self.assertEqual(runner.main(),0)
            self.assertEqual(calls,[kb for kb in runtime.ops.LIBRARIES for _ in range(2)])
            actual=window.validate_run(self.root,self.wid,key,list(runtime.ops.LIBRARIES))
            self.assertTrue(all(v['build_seconds']>0 and v['peak_rss_bytes']>0 and v['index_bytes']>0 for v in actual['libraries'].values()))
            old=(w/('run-'+key)/'result.json').read_bytes()
            self.assertNotEqual(runner.main(),0)
            self.assertEqual(calls,[kb for kb in runtime.ops.LIBRARIES for _ in range(2)])
            self.assertEqual((w/('run-'+key)/'result.json').read_bytes(),old)

    def test_a_actual_runner_claims_before_score_and_never_replays(self):self.runner_entrypoint('a')
    def test_b_actual_runner_claims_before_score_and_never_replays(self):self.runner_entrypoint('b')
    def test_a_private_log_leak_blocks_result_and_completion(self):self.runner_entrypoint('a',leak=True)
    def test_b_private_log_leak_blocks_result_and_completion(self):self.runner_entrypoint('b',leak=True)

    def test_real_aggregate_uses_only_same_window_after_and_current_privacy(self):
        import rt055_aggregate as aggregate
        import rt055_window as window
        import test_rt055_retrieval_decision as fixture
        import subprocess
        w=self.freeze_fixture();public=fixture.valid_report()
        checks=runtime.ops.read_json(self.root/'verifier/case-verification.json')
        checks.update(library_validity=public['library_validity'])
        for k in ('selection_verified','single_build_seed_verified','at_least_one_participating','category_coverage_verified','rt054_pool_excluded','input_disjoint_verified','denominators_nonzero_all_categories'):checks[k]=True
        runtime.ops.write_private_json(self.root/'verifier/case-verification.json',checks)
        freeze.create(self.root,self.wid,self.mid);freeze.verify_once(self.root,self.wid)
        for key,cid in (('a',aggregate.CANDIDATE_A),('b',aggregate.CANDIDATE_B)):
            c=public['candidates'][cid]
            for kb in public['participating_libraries']:
                self.expose_fixture(key,kb)
                window.library_scored(self.root,self.wid,key,kb,c['libraries'][kb])
                window.library_complete(self.root,self.wid,key,kb,{'metrics':c['libraries'][kb],'gateway_readiness':c['gateway_readiness']})
            window.write_once(w/('run-'+key)/'result.json',{'schema':'cwk.rt055.run-'+key+'.result.v1','mode':'run','status':'OK','window_id':self.wid,'libraries':c['libraries'],'gateway_readiness':c['gateway_readiness']})
        self.collect('after')
        window.write_once(w/'audit/cleanup.json',{**public['cleanup'],'window_id':self.wid})
        (self.root/'contracts').mkdir()
        (self.root/'contracts/aggregate-report.schema.json').write_bytes((Path(fixture.__file__).parents[1]/'RT/RT-055/contracts/aggregate-report.schema.json').read_bytes())
        # Actual JSON Schema subprocess, with only interpreter location adapted.
        real_run=subprocess.run
        def run(cmd,**kw):return real_run([sys.executable,*cmd[1:]],**kw)
        with patch.object(aggregate,'ROOT',self.root),patch.object(aggregate,'role_audit',return_value={'verified':True,'role_separation_level':'PROCESS_LEVEL_SEPARATION_SINGLE_UID'}),patch.object(sys,'argv',['aggregate','--window-id',self.wid]),patch.object(aggregate.subprocess,'run',side_effect=run):
            # Missing/foreign after cannot be substituted by a legacy generic PASS.
            cp=w/'audit/production-comparison.json';old=cp.read_bytes();cp.unlink()
            with self.assertRaises(OSError):aggregate.main()
            cp.write_bytes(old)
            # Structural PASS + truthful UNKNOWN must still make decision INVALID.
            self.assertEqual(aggregate.main(),2)
            out=json.loads((w/'aggregate/aggregate-report.json').read_text())
            self.assertEqual(out['formal_window']['window_id'],self.wid)
            self.assertTrue(out['formal_window']['privacy_revalidation_verified'])
            self.assertIsNone(out['production_invariants']['nas_unchanged'])
            with self.assertRaises(FileExistsError):aggregate.main()

if __name__=='__main__':unittest.main()
