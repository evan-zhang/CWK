"""Amendment 4: public synthetic scoring and irreversible exposure ledger."""
import copy
import json
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import kb_retrieval_candidates as kbc
import rt055_window as window
import rt055_freeze as freeze
import rt055_runtime as runtime
import rt055_scoring_input as scoring
import test_rt055_formal_window as fixtures


def cases(kb):
    return [kbc.Case(kb,'public exact',frozenset({'public-doc'}),True),
            kbc.Case(kb,'public answer',frozenset({'public-doc'})),
            kbc.Case(kb,'public absent',frozenset())]


class ScorerTests(unittest.TestCase):
    def test_each_library_passes_its_own_categories_and_only_returns_itself(self):
        for kb in runtime.ops.LIBRARIES:
            seen=[]
            class Candidate:
                def search(self,q,library,**kw):seen.append(library);return []
            value=kbc.score_library_cases(Candidate(),cases(kb),kb)
            self.assertEqual(set(value),{kb});self.assertEqual(seen,[kb]*3)
            self.assertEqual(value[kb]['total_count'],3)

    def test_default_all_library_semantics_preserved(self):
        c=type('Candidate',(),{'search':lambda *a,**kw:[]})()
        with self.assertRaises(kbc.CandidateError):kbc.score_cases(c,cases(runtime.ops.LIBRARIES[0]))
        self.assertEqual(set(kbc.score_cases(c,[c for kb in runtime.ops.LIBRARIES for c in cases(kb)])),set(runtime.ops.LIBRARIES))

    def test_missing_category_cross_library_or_duplicate_refused_before_hook(self):
        kb=runtime.ops.LIBRARIES[0];called=[]
        c=type('Candidate',(),{'search':lambda *a,**kw:called.append('query')})()
        for rows in (cases(kb)[:-1],cases(kb)[1:],cases(kb)+cases(kb),cases(runtime.ops.LIBRARIES[1])):
            with self.assertRaises(kbc.CandidateError):kbc.score_library_cases(c,rows,kb,before_first_search=lambda:called.append('hook'))
        self.assertEqual(called,[])

    def test_exposure_failure_is_not_scored_or_swallowed(self):
        called=[]
        def deny():raise RuntimeError('exposure_denied')
        c=type('Candidate',(),{'search':lambda *a,**kw:called.append(1)})()
        with self.assertRaisesRegex(RuntimeError,'exposure_denied'):
            kbc.score_library_cases(c,cases(runtime.ops.LIBRARIES[0]),runtime.ops.LIBRARIES[0],before_first_search=deny)
        self.assertEqual(called,[])


class LedgerTests(fixtures.WindowTests):
    # Reuse fixture helpers without copying inherited test methods into discovery.
    def ready(self,key='a'):
        w=self.freeze_fixture()
        with patch.object(freeze.secrets,'randbits',return_value=1 if key=='a' else 0):freeze.create(self.root,self.wid,self.mid)
        freeze.verify_once(self.root,self.wid)
        return w,runtime.claim_candidate(self.root,key,self.wid)

    def test_arm_does_not_consume_exposure_is_before_first_search_both_arms(self):
        for key in ('a','b'):
            if key=='b':self.setUp()
            w,attempt=self.ready(key);kb=runtime.ops.LIBRARIES[0];observed=[]
            root=self.root
            class Candidate:
                def search(self,*a,**kw):
                    observed.append(json.loads((root/'exposure'/key/(kb+'.json')).read_text()))
                    return []
            metrics=window.score_library(self.root,self.wid,key,kb,attempt,Candidate(),cases(kb))
            self.assertEqual(len(observed),3)
            self.assertEqual(len({o['exposure_id'] for o in observed}),1)
            self.assertEqual(observed[0]['window_id'],self.wid)
            self.assertEqual(observed[0]['attempt_id'],attempt)
            self.assertTrue((w/'arms'/key/(kb+'.json')).is_file())
            window.library_scored(self.root,self.wid,key,kb,metrics[kb])
            window.library_complete(self.root,self.wid,key,kb,{'metrics':metrics[kb]})
            self.assertEqual(window.library_result(self.root,self.wid,key,kb)['metrics'],metrics[kb])

    def test_failed_prequery_categories_leave_arm_but_zero_exposure(self):
        w,attempt=self.ready();kb=runtime.ops.LIBRARIES[0]
        with self.assertRaises(kbc.CandidateError):window.score_library(self.root,self.wid,'a',kb,attempt,object(),cases(kb)[:-1])
        self.assertTrue((w/'arms/a'/(kb+'.json')).exists())
        self.assertFalse((self.root/'exposure/a'/(kb+'.json')).exists())
        self.assertIsNone(window.library_result(self.root,self.wid,'a',kb))

    def test_crash_after_exposure_and_cross_window_are_not_replayable(self):
        w,attempt=self.ready();kb=runtime.ops.LIBRARIES[0]
        window.library_arm(self.root,self.wid,'a',kb,attempt)
        self.assertTrue(window.holdout_unexposed(self.root))
        window.library_expose(self.root,self.wid,'a',kb,attempt)
        self.assertFalse(window.holdout_unexposed(self.root))
        with self.assertRaises(RuntimeError):window.library_result(self.root,self.wid,'a',kb)
        for wid in (self.wid,str(uuid.uuid4())):
            with self.assertRaises((RuntimeError,FileExistsError,FileNotFoundError)):
                window.library_arm(self.root,wid,'a',kb,attempt)
        with self.assertRaises(FileExistsError):window.library_expose(self.root,self.wid,'a',kb,attempt)

    def test_legacy_claim_without_verified_void_cannot_arm_or_freeze(self):
        w,attempt=self.ready();kb=runtime.ops.LIBRARIES[0]
        window.write_once(self.root/'consumption/a'/(kb+'.claim'),{'window_id':self.wid})
        with self.assertRaises(RuntimeError):window.library_arm(self.root,self.wid,'a',kb,attempt)
        with self.assertRaises(RuntimeError):window.holdout_unexposed(self.root)
        with self.assertRaises(RuntimeError):window.library_result(self.root,self.wid,'a',kb)

    def test_result_binds_exposure_score_window_and_freeze(self):
        w,attempt=self.ready();kb=runtime.ops.LIBRARIES[0]
        window.library_arm(self.root,self.wid,'a',kb,attempt);window.library_expose(self.root,self.wid,'a',kb,attempt)
        with self.assertRaises(FileNotFoundError):window.library_complete(self.root,self.wid,'a',kb,{'metrics':{}})
        window.library_scored(self.root,self.wid,'a',kb,{'total_count':3})
        window.library_complete(self.root,self.wid,'a',kb,{'metrics':{'total_count':3}})
        p=w/'run-a'/(kb+'.json');old=p.read_bytes()
        for field,value in [('window_id',str(uuid.uuid4())),('exposure_id',str(uuid.uuid4())),('receipt_sha256','0'*64),('score_sha256','0'*64)]:
            row=json.loads(old);row[field]=value;p.write_text(json.dumps(row))
            with self.assertRaises(RuntimeError):window.library_result(self.root,self.wid,'a',kb)
        p.write_bytes(old)
        self.assertEqual(window.library_result(self.root,self.wid,'a',kb)['metrics']['total_count'],3)

    def second_valid_window(self):
        other=str(uuid.uuid4());self.prepare_policy(other);scoring.prepare(self.root,other,self.mid);self.collect(wid=other)
        with patch.object(freeze.secrets,'randbits',return_value=1):freeze.create(self.root,other,self.mid)
        freeze.verify_once(self.root,other)
        return other,runtime.claim_candidate(self.root,'a',other)

    def test_second_valid_frozen_window_cannot_replay_global_exposure(self):
        w,attempt=self.ready();other,second=self.second_valid_window();kb=runtime.ops.LIBRARIES[0]
        window.library_arm(self.root,self.wid,'a',kb,attempt)
        window.library_expose(self.root,self.wid,'a',kb,attempt)
        self.assertTrue(freeze.verify_artifacts(self.root,other))
        with self.assertRaisesRegex(RuntimeError,'already_exposed_no_replay'):
            window.library_arm(self.root,other,'a',kb,second)
        with self.assertRaisesRegex(RuntimeError,'exposure_without_completion_no_replay'):
            window.library_result(self.root,other,'a',kb)

    def test_two_armed_windows_race_allows_exactly_one_search_and_truncation_blocks(self):
        import concurrent.futures
        import threading
        w,attempt=self.ready();other,second=self.second_valid_window();kb=runtime.ops.LIBRARIES[0]
        for wid,aid in ((self.wid,attempt),(other,second)):window.library_arm(self.root,wid,'a',kb,aid)
        barrier=threading.Barrier(2);observed=[]
        def invoke(pair):
            wid,aid=pair;barrier.wait()
            try:window.library_expose(self.root,wid,'a',kb,aid)
            except FileExistsError:return 'DENIED_BEFORE_SEARCH'
            # This is the first candidate search; it sees a complete durable row.
            row=json.loads((self.root/'exposure/a'/(kb+'.json')).read_text())
            observed.append(row['window_id'])
            return 'SEARCH_EXECUTED'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(invoke,[(self.wid,attempt),(other,second)]))
        self.assertEqual(sorted(results),['DENIED_BEFORE_SEARCH','SEARCH_EXECUTED'])
        self.assertEqual(len(observed),1)
        # Interrupted/empty exposure is still a global consumed boundary.
        (self.root/'exposure/a'/(kb+'.json')).write_bytes(b'')
        self.assertFalse(window.holdout_unexposed(self.root))
        with self.assertRaisesRegex(RuntimeError,'already_exposed_no_replay'):
            window.library_arm(self.root,other,'a',kb,second)

    def test_closed_old_window_permanently_refuses_new_attempt(self):
        w,attempt=self.ready();self.collect('after')
        with self.assertRaises(RuntimeError):runtime.claim_candidate(self.root,'a',self.wid)


# Only the new methods run here; original fixture tests still run in their module.
for _name in tuple(dir(fixtures.WindowTests)):
    if _name.startswith('test_') and _name not in LedgerTests.__dict__:
        setattr(LedgerTests,_name,None)


class InProcessProofTests(unittest.TestCase):
    def test_actual_frozen_public_source_no_popen_no_socket_no_private_reads(self):
        import rt055_zero_exposure as zero
        import socket, subprocess, builtins, io
        archive=Path(__file__).parent/'fixtures/rt055-legacy-proof'
        opened=[]; original=io.open
        def checked(file,*args,**kwargs):
            if isinstance(file,(str,Path)):
                self.assertTrue(Path(file).resolve().is_relative_to(archive.resolve()))
                opened.append(Path(file).name)
            return original(file,*args,**kwargs)
        with patch.object(subprocess,'Popen',side_effect=AssertionError('NO_POPEN')),patch.object(socket.socket,'connect',side_effect=AssertionError('NO_QUERY')),patch.object(io,'open',side_effect=checked),patch.object(builtins,'open',side_effect=AssertionError('NO_BUILTIN_OPEN')):
            self.assertEqual(zero._proof(archive),{'rows':[{'calls':0,'failure':'CandidateError'}]*3,'forbidden_reads':0})
        self.assertIn('kb_retrieval_candidates.py',opened)

    def test_source_tamper_is_rejected_before_execution(self):
        import tempfile,shutil
        import rt055_zero_exposure as zero
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder); (archive/'impl').mkdir()
            for p in (Path(__file__).parent/'fixtures/rt055-legacy-proof/impl').glob('*.py'):shutil.copyfile(p,archive/'impl'/p.name)
            p=archive/'impl/kb_retrieval_candidates.py';p.write_text(p.read_text()+'\nraise RuntimeError("tampered")\n')
            with self.assertRaisesRegex(RuntimeError,'zero_exposure_evidence_invalid_no_replay'):zero._proof(archive)


    def test_changed_dependency_literals_fail_closed(self):
        import tempfile,shutil
        import rt055_zero_exposure as zero
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder);shutil.copytree(Path(__file__).parent/'fixtures/rt055-legacy-proof/impl',archive/'impl')
            p=archive/'impl/kb_retrieval_decision.py';p.write_text(p.read_text().replace('"spbp-2027")','"forged")'))
            with self.assertRaises(RuntimeError):zero._proof(archive)

    def test_removed_original_category_gate_cannot_produce_proof(self):
        import tempfile,shutil,hashlib,ast
        import rt055_zero_exposure as zero
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder);shutil.copytree(Path(__file__).parent/'fixtures/rt055-legacy-proof/impl',archive/'impl')
            p=archive/'impl/kb_retrieval_candidates.py';tree=ast.parse(p.read_text())
            scorer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='score_cases')
            scorer.body=[n for n in scorer.body if not (isinstance(n,ast.If) and 'missing scoring category' in ast.unparse(n))]
            p.write_text(ast.unparse(tree))
            # Mutation bypasses ONLY public source authentication to exercise
            # behavioral verification: a changed scorer cannot forge zero calls.
            with patch.dict(zero.LEGACY_PUBLIC_SOURCES,{p.name:hashlib.sha256(p.read_bytes()).hexdigest()}):
                with self.assertRaises((RuntimeError,NameError)):zero._proof(archive)


class VoidTests(LedgerTests):
    def legacy_fixture(self):
        import rt055_zero_exposure as zero
        import hashlib
        import time
        w=self.freeze_fixture(policy=False)
        # Public synthetic control-flow model; actual OPS requires the three
        # fixed full-source digests, tested independently against the baseline.
        old='''from dataclasses import dataclass
class CandidateError(Exception):pass
class decision:LIBRARIES=('cwork-3m','docdb-touqian','spbp-2027')
@dataclass
class Case:
 kb_id:str
 query:str
 expected:frozenset
 exact:bool=False
def score_cases(candidate,cases,timeout=30):
 counts={kb:0 for kb in decision.LIBRARIES}
 for c in cases:counts[c.kb_id]+=1
 if min(counts.values())==0:raise CandidateError('missing scoring category')
 for c in cases:candidate.search(c.query,c.kb_id,timeout=timeout)
'''
        hf=self.root/'sidecar/hf';(hf/'blobs').mkdir(parents=True);(hf/'snapshots').mkdir()
        (hf/'blobs/public-model').write_text('public model bytes')
        (hf/'snapshots/public-model').symlink_to('../blobs/public-model')
        (self.root/'impl/kb_retrieval_candidates.py').write_text(old)
        (self.root/'impl/kb_retrieval_decision.py').write_bytes((Path(__file__).parent/'fixtures/rt055-legacy-proof/impl/kb_retrieval_decision.py').read_bytes())
        pm=runtime.migration_directory(self.root,self.mid);t=pm/'rt055-synthetic-001'
        (t/'impl/kb_retrieval_candidates.py').write_text(old)
        (t/'impl/kb_retrieval_decision.py').write_bytes((self.root/'impl/kb_retrieval_decision.py').read_bytes())
        dep=runtime.ops.read_json(pm/'deployment.json');dep['source_files']['kb_retrieval_decision.py']=runtime.ops.sha_file(self.root/'impl/kb_retrieval_decision.py');dep['source_files']['kb_retrieval_candidates.py']=runtime.ops.sha_file(self.root/'impl/kb_retrieval_candidates.py')
        (pm/'deployment.json').write_text(json.dumps(dep))
        (pm/'privacy-receipt.json').write_text(json.dumps(runtime.migration_evidence(self.root,self.mid,1)))
        self.prepare_policy();scoring.prepare(self.root,self.wid,self.mid);self.collect()
        hashes={n:runtime.ops.sha_file(self.root/'impl'/n) for n in zero.LEGACY_PUBLIC_SOURCES}
        self.addCleanup(patch.stopall)
        patch.object(zero,'LEGACY_PUBLIC_SOURCES',hashes).start()
        with patch.object(freeze.secrets,'randbits',return_value=1):freeze.create(self.root,self.wid,self.mid)
        freeze.verify_once(self.root,self.wid);attempt=runtime.claim_candidate(self.root,'a',self.wid)
        kb=runtime.ops.LIBRARIES[0];window.library_claim(self.root,self.wid,'a',kb)
        window.write_once(w/'run-a/attempts'/attempt/'failure.json',{'error':'CandidateError'})
        window.write_once(w/'status/formal.json',{'status':'INVALID'})
        self.collect('after')
        report={'reason':zero.REASON,'zero_calls_basis':zero.BASIS,'window_id':self.wid,'window_decision':'INVALID',
                'formal_query_calls':0,'formal_score_receipts':0,'private_holdout_reopened_for_diagnosis':False,
                'freeze_source_recomputed':True,'privacy_source_binding_recomputed':True,
                'candidate_attempts':{'a':1,'b':0},'consumption_claims':{'a':1,'b':0}}
        reconciliation={'classification':zero.REASON,'zero_calls_basis':zero.BASIS,'window_id':self.wid,
                        'actual_private_holdout_reopened_for_probe':False,'formal_candidate_search_calls':0,
                        'score_receipts':0,'frozen_source_recomputed':True,'privacy_binding_recomputed':True,
                        'candidate_b_started':False,'candidate_processes_remaining':0}
        window.write_once(w/'audit/report.json',report);window.write_once(w/'audit/reconciliation.json',reconciliation)
        retained={}
        for i in range(96):
            p=self.root/'synthetic-retained'/str(i);p.parent.mkdir(exist_ok=True);p.write_text('public')
            retained[str(p.relative_to(self.root))]=runtime.ops.sha_file(p)
        window.write_once(self.root/'audit/retained.json',retained)
        (self.root/'builder/single-build-claim.json').write_text('{}')
        (self.root/'verifier/single-verify-claim').write_text('{}')
        # The frozen historical verifier predates void_paths; emulate only that
        # contract difference, retaining all live source/private/dependency checks.
        original_verify=freeze.verify_artifacts;old_wid=self.wid
        def legacy_verify(root,wid):
            if wid==old_wid:
                with patch.object(window,'void_paths',return_value=[]):return original_verify(root,wid)
            return original_verify(root,wid)
        patch.object(freeze,'verify_artifacts',side_effect=legacy_verify).start()
        for role in ('builder','verifier','impl'):window.write_once(self.root/'status'/(role+'.json'),{'status':'PASS'})
        self.archive_id=str(uuid.uuid4())
        self.original={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        return zero,w,kb

    def test_strict_zero_void_allows_new_window_without_deleting_claim(self):
        zero,w,kb=self.legacy_fixture();m=zero.capture(self.root,self.wid,self.archive_id)
        zero.append_void(self.root,m)
        import subprocess,socket
        with patch.object(subprocess,'Popen',side_effect=AssertionError('NO_POPEN')),patch.object(socket.socket,'connect',side_effect=AssertionError('NO_QUERY')):
            zero.validate_void(self.root,'a',kb)
            self.assertTrue(window.holdout_unexposed(self.root))
        self.assertTrue(all((self.root/p).read_bytes()==b for p,b in self.original.items()))
        self.wid=str(uuid.uuid4());self.prepare_policy();scoring.prepare(self.root,self.wid,self.mid);self.collect()
        freeze.create(self.root,self.wid,self.mid);freeze.verify_once(self.root,self.wid)
        attempt=str(uuid.uuid4());window.write_once(window.directory(self.root,self.wid)/'run-a/attempts'/attempt/'claim.json',{**window.envelope(self.root,self.wid,'execution-attempt'),'candidate':'a'})
        window.library_arm(self.root,self.wid,'a',kb,attempt)
        window.library_expose(self.root,self.wid,'a',kb,attempt)
        self.assertFalse(window.holdout_unexposed(self.root))
        with self.assertRaises((RuntimeError,FileExistsError)):zero.append_void(self.root,m)
        self.assertTrue(all((self.root/p).read_bytes()==b for p,b in self.original.items()))

    def test_one_query_score_result_source_drift_or_missing_evidence_denies_void(self):
        zero,w,kb=self.legacy_fixture()
        for target,field,value in [('audit/report.json','formal_query_calls',1),('audit/report.json','formal_score_receipts',1),
                                   ('audit/report.json','private_holdout_reopened_for_diagnosis',True),
                                   ('audit/reconciliation.json','formal_candidate_search_calls',1)]:
            p=w/target;old=p.read_bytes();v=json.loads(old);v[field]=value;p.write_text(json.dumps(v))
            with self.assertRaises(RuntimeError):zero.capture(self.root,self.wid,str(uuid.uuid4()))
            p.write_bytes(old)
        for target in ('run-a/scores/cwork-3m.json','run-a/cwork-3m.json','run-b/attempts/public/claim.json'):
            p=w/target;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
            with self.assertRaises(RuntimeError):zero.capture(self.root,self.wid,str(uuid.uuid4()))
            p.unlink()
        p=self.root/'impl/kb_retrieval_candidates.py';old=p.read_bytes();p.write_text('drift')
        with self.assertRaises(RuntimeError):zero.capture(self.root,self.wid,str(uuid.uuid4()))
        p.write_bytes(old)
        p=w/'audit/report.json';old=p.read_bytes();p.unlink()
        with self.assertRaises(RuntimeError):zero.capture(self.root,self.wid,str(uuid.uuid4()))
        p.write_bytes(old)
        self.assertFalse((self.root/'void-prequery/a'/(kb+'.json')).exists())

    def test_archived_evidence_hash_source_or_raw_record_tamper_is_rejected(self):
        zero,w,kb=self.legacy_fixture();m=zero.capture(self.root,self.wid,self.archive_id);zero.append_void(self.root,m)
        for p in [m/'evidence.json',m/'archive/impl/kb_retrieval_candidates.py',w/'audit/report.json',self.root/'consumption/a'/(kb+'.claim')]:
            old=p.read_bytes()
            if p==m/'evidence.json':
                changed=json.loads(old);changed['created_at']+=1;p.write_text(json.dumps(changed))
            else:p.write_text('{}')
            with self.assertRaises(RuntimeError):zero.validate_void(self.root,'a',kb)
            p.write_bytes(old)
        zero.validate_void(self.root,'a',kb)

    def test_public_model_link_cannot_escape_or_impersonate_private_input(self):
        zero,w,kb=self.legacy_fixture()
        link=self.root/'sidecar/hf/snapshots/public-model'
        self.assertEqual(zero._dependency_path(self.root,str(link.relative_to(self.root))),link)
        with self.assertRaises(RuntimeError):zero._path(self.root,str(link.relative_to(self.root)))
        link.unlink();link.symlink_to(self.root/'builder/private-corpus.json')
        with self.assertRaises(RuntimeError):zero._dependency_path(self.root,str(link.relative_to(self.root)))

    def test_forged_void_boolean_without_original_evidence_is_rejected(self):
        zero,w,kb=self.legacy_fixture()
        window.write_once(self.root/'void-prequery/a'/(kb+'.json'),{'status':'VOID_PREQUERY_NO_EXPOSURE','passed':True})
        with self.assertRaises(RuntimeError):zero.validate_void(self.root,'a',kb)

for _name in tuple(dir(LedgerTests)):
    if _name.startswith('test_') and _name not in VoidTests.__dict__:setattr(VoidTests,_name,None)

if __name__=='__main__':unittest.main()
