"""Amendment 7 public synthetic input contract; no OPS data/network."""
from types import SimpleNamespace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import kb_retrieval_candidates as kbc

class ScoringInputTests(unittest.TestCase):
    def test_distinct_trials_same_query_same_semantics_are_both_scored(self):
        cases=[SimpleNamespace(kb_id='cwork-3m',query=q,expected=frozenset(e),exact=x,trial_id=i)
               for i,(q,e,x) in enumerate([('PUBLIC same',['public-doc'],True),('PUBLIC same',['public-doc'],True),('PUBLIC absent',[],False)])]
        candidate=Mock();candidate.search.return_value=[]
        before=Mock()
        metrics=kbc.score_library_cases(candidate,cases,'cwork-3m',before_first_search=before)
        self.assertEqual(metrics['cwork-3m']['total_count'],3)
        self.assertEqual(metrics['cwork-3m']['exact_count'],2)
        self.assertEqual(candidate.search.call_count,3);before.assert_called_once()


import copy
import dataclasses
import json
import uuid
from unittest.mock import patch
import rt055_scoring_input as scoring
import rt055_window as window
import rt055_freeze as freeze
import rt055_runtime as runtime


def public_verified():
    rows=[{'kb_id':kb,'ordinal':i,'category':cat,'category_ordinal':0,
           'query':q,'expected_doc_id':'PUBLIC-DOC','expected_outcome':out,'exact':exact}
          for kb in runtime.ops.LIBRARIES
          for i,(cat,q,out,exact) in enumerate([('title','PUBLIC same','hit',True),
                       ('filename','PUBLIC same','hit',True),('no_answer_mutation','PUBLIC absent','no_evidence',False)])]
    return {'libraries':{kb:{'cases':[r for r in rows if r['kb_id']==kb]} for kb in runtime.ops.LIBRARIES}}


class TrialContractTests(unittest.TestCase):
    def cases(self):return scoring.load_cases(public_verified())

    def rejected(self,cases):
        candidate=Mock();before=Mock()
        with self.assertRaises((kbc.CandidateError,RuntimeError)):
            kbc.score_cases(candidate,cases,before_first_search=before)
        candidate.search.assert_not_called();before.assert_not_called()

    def test_private_identity_not_in_repr(self):
        c=kbc.Case('cwork-3m','PUBLIC-QUERY',frozenset({'PUBLIC-DOC'}),True,543219876)
        self.assertNotIn('543219876',repr(c));self.assertNotIn('PUBLIC',repr(c))

    def test_expected_exact_answerability_conflict_preexposure(self):
        for change in ({'expected':frozenset({'PUBLIC-OTHER'})},{'exact':False},
                       {'expected':frozenset(),'exact':False}):
            with self.subTest(change=change):
                cases=self.cases();cases[1]=dataclasses.replace(cases[1],**change);self.rejected(cases)

    def test_duplicate_missing_illegal_identity_preexposure(self):
        for identity in (0,None,-1,True,'1',1.0,(),[],{}):
            with self.subTest(kind=type(identity).__name__):
                cases=self.cases();cases[1]=dataclasses.replace(cases[1],trial_id=identity);self.rejected(cases)

    def test_legacy_duplicate_stays_rejected(self):
        self.rejected([dataclasses.replace(c,trial_id=None) for c in self.cases()])

    def test_legacy_unique_allowed_and_all_rows_count(self):
        cases=[dataclasses.replace(c,query=c.query+str(i),trial_id=None) for i,c in enumerate(self.cases())]
        candidate=Mock();candidate.search.return_value=[]
        metrics=kbc.score_cases(candidate,cases)
        self.assertEqual(candidate.search.call_count,9)
        self.assertTrue(all(v['total_count']==3 for v in metrics.values()))

    def test_strict_full_validation_bad_query_expected_exact_categories(self):
        for change in ({'query':''},{'query':' '},{'query':None},{'expected':{'PUBLIC'}},
                       {'expected':frozenset({''})},{'expected':frozenset({' '})},{'exact':1},
                       {'expected':frozenset()}):
            cases=self.cases();cases[-2]=dataclasses.replace(cases[-2],**change);self.rejected(cases)
        self.rejected(self.cases()[:-1])

    def test_both_loaders_preserve_every_ordinal_and_strict_flags(self):
        import rt055_run_a as a,rt055_run_b as b
        v=public_verified()
        for runner in (a,b):
            cases=runner.load_cases(v)
            self.assertEqual([c.trial_id for c in cases],[0,1,2]*3)
            self.assertEqual(kbc.validate_cases(cases,require_trial_identity=True)['cwork-3m']['total_count'],3)
            for field,bad in [('ordinal',None),('ordinal',True),('ordinal',-1),('exact',1),
                              ('expected_outcome','bad'),('expected_doc_id',''),('kb_id','wrong')]:
                value=copy.deepcopy(v);value['libraries']['cwork-3m']['cases'][0][field]=bad
                with self.assertRaises(RuntimeError):runner.load_cases(value)
            value=copy.deepcopy(v);del value['libraries']['cwork-3m']['cases'][0]['ordinal']
            with self.assertRaises(RuntimeError):runner.load_cases(value)

    def test_atomic_callback_failure_still_zero_search(self):
        candidate=Mock()
        with self.assertRaises(RuntimeError):
            kbc.score_cases(candidate,self.cases(),before_first_search=Mock(side_effect=RuntimeError('DENY')))
        candidate.search.assert_not_called()


class InputReadinessTests(unittest.TestCase):
    def setUp(self):
        import test_rt055_formal_window as fixtures
        self.f=fixtures.WindowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.wid=self.f.wid;self.mid=self.f.mid

    def fixture(self):
        self.w=self.f.freeze_fixture();self.base=scoring.directory(self.root,self.wid)

    def frozen(self):
        self.fixture();freeze.create(self.root,self.wid,self.mid)
        self.assertEqual(freeze.verify_once(self.root,self.wid),0)

    def test_before_missing_receipt_fails_before_claim(self):
        with self.assertRaises((RuntimeError,OSError)):
            import rt055_baseline as baseline
            with patch.object(baseline,'ROOT',self.root):baseline.main(['before','--window-id',self.wid])
        self.assertFalse(window.directory(self.root,self.wid).exists())

    def test_counts_binding_and_pure_full_input_preflight(self):
        self.frozen()
        with patch.object(runtime.subprocess,'Popen',side_effect=AssertionError('NO POPEN')) as p:
            row=scoring.verify(self.root,self.wid,self.mid)
            self.assertEqual([v['total_count'] for v in row['libraries'].values()],[3]*3)
            self.assertEqual([v['duplicate_groups'] for v in row['libraries'].values()],[1]*3)
            p.assert_not_called()
        self.assertIn(str((self.base/'receipt.json').relative_to(self.root)),window.receipt(self.root,self.wid)['private_files'])
        self.assertTrue(freeze.verify_artifacts(self.root,self.wid))

    def test_after_before_closed_exposed_or_duplicate_prepare_rejected(self):
        self.fixture()
        with self.assertRaises(RuntimeError):scoring.prepare(self.root,self.wid,self.mid)
        wid=str(uuid.uuid4());window.write_once(self.root/'exposure/a/cwork-3m.json',{})
        with self.assertRaises(RuntimeError):scoring.prepare(self.root,wid,self.mid)
        self.assertFalse(scoring.directory(self.root,wid).exists())
        window.write_once(self.w/'status/baseline-after.claim',{})
        with self.assertRaises(RuntimeError):scoring.before_precheck(self.root,self.wid)

    def test_duplicate_readiness_directory_is_never_overwritten(self):
        self.f.migration();self.f.input_fixture();scoring.prepare(self.root,self.wid,self.mid)
        before=(scoring.directory(self.root,self.wid)/'receipt.json').read_bytes()
        with self.assertRaises(FileExistsError):scoring.prepare(self.root,self.wid,self.mid)
        self.assertEqual((scoring.directory(self.root,self.wid)/'receipt.json').read_bytes(),before)

    def test_synthetic_cannot_impersonate_main(self):
        self.f.migration()
        t=self.root/('rt055-'+str(uuid.uuid4()));t.mkdir();(t/'.rt055-owned').write_text(t.name[6:])
        window.write_once(t/'audit/protected-root.json',{'root':str(self.root)})
        with self.assertRaises(RuntimeError):scoring.prepare(t,self.wid,self.mid)

    def test_receipt_identity_source_files_counts_timing_rejected(self):
        self.fixture();p=self.base/'receipt.json';original=p.read_bytes()
        bads=[('window_id',str(uuid.uuid4())),('migration_id',str(uuid.uuid4())),('run_id',str(uuid.uuid4())),
              ('source_commit','2'*40),('source_files',{}),('files',{}),('libraries',{}),('status','BAD'),
              ('ready_at',window.baseline(self.root,self.wid,'before')[0]['observed_at'])]
        for field,value in bads:
            with self.subTest(field=field):
                row=json.loads(original);row[field]=value;p.write_text(json.dumps(row))
                with self.assertRaises((RuntimeError,OSError)):freeze.create(self.root,self.wid,self.mid)
                self.assertFalse((self.w/'status/freeze.claim').exists())
        p.write_bytes(original)
        checks=self.root/'verifier/case-verification.json';row=json.loads(checks.read_text())
        row['library_validity']['cwork-3m']['total_count']=4;checks.write_text(json.dumps(row))
        with self.assertRaises(RuntimeError):scoring.inspect_inputs(self.root)
        with self.assertRaises(RuntimeError):scoring.verify(self.root,self.wid,self.mid)

    def test_private_rows_or_source_drift_rejected_even_rehash(self):
        self.frozen()
        p=self.root/'verifier/private-verified.json';row=json.loads(p.read_text())
        row['libraries']['cwork-3m']['cases'][1]['expected_doc_id']='PUBLIC-CONFLICT';p.write_text(json.dumps(row))
        rp=self.base/'receipt.json';r=json.loads(rp.read_text());r['files'][str(p.relative_to(self.root))]=runtime.ops.sha_file(p);rp.write_text(json.dumps(r))
        with self.assertRaises((RuntimeError,kbc.CandidateError)):scoring.verify(self.root,self.wid,self.mid)
        self.assertFalse(freeze.verify_artifacts(self.root,self.wid))

    def test_formal_coordinator_bad_receipt_source_after_exposure_zero_claims_popen(self):
        import rt055_formal_coordinator as coordinator
        self.frozen();receipt=self.base/'receipt.json';original=receipt.read_bytes()
        source=self.root/'impl/rt055_scoring_input.py';src=source.read_bytes()
        after=self.w/'status/baseline-after.claim';exposure=self.root/'exposure/a/cwork-3m.json'
        for bad in ('receipt','source','after','exposure'):
            with self.subTest(bad=bad):
                if bad=='receipt':receipt.write_bytes(original+b' ')
                if bad=='source':source.write_bytes(src+b' ')
                if bad=='after':window.write_once(after,{})
                if bad=='exposure':window.write_once(exposure,{})
                with patch.object(coordinator,'ROOT',self.root),patch.object(coordinator.subprocess,'Popen') as p:
                    with self.assertRaises((RuntimeError,OSError)):
                        coordinator.main(['--window-id',self.wid,'--privacy-migration-id',self.mid])
                    p.assert_not_called()
                self.assertFalse((self.w/'controllers').exists());self.assertFalse(list(self.w.glob('run-*/attempts/*/claim.json')))
                receipt.write_bytes(original);source.write_bytes(src)
                if after.exists():after.unlink()
                if exposure.exists():exposure.unlink()


class RawSemanticsTests(unittest.TestCase):
    def test_no_evidence_origin_conflict_also_rejected(self):
        v=public_verified();rows=v['libraries']['cwork-3m']['cases']
        rows.append({**rows[-1],'ordinal':3,'expected_doc_id':'PUBLIC-OTHER'})
        with self.assertRaises(RuntimeError):scoring.load_cases(v)

if __name__=='__main__':unittest.main()
