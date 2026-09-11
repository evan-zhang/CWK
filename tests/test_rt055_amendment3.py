"""Amendment 3 synthetic behavior; no OPS inputs or connections."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rt055_builder as builder
import rt055_verifier as verifier
import rt055_exclusion as exclusion
import kb_retrieval_decision as decision
from test_rt055_retrieval_decision import valid_report
from test_rt055_exclusion import document, authority

CATS = ('title_filename','exact_identifier_date','body_only_rare_phrase','table_row','no_answer_mutation','near_neighbour')
FLOORS = dict(zip(CATS,(3,2,3,3,3,3)))
TARGETS = dict(zip(CATS,(10,8,10,4,5,5)))
def counts(values): return dict(zip(CATS,values))
DOCDB = counts((10,3,5,3,5,5))
SPBP = counts((3,1,3,3,3,3))
def pool(tier, n):
    return {'category_counts':n, 'cases':[{'synthetic_tier':tier,'ordinal':i} for i in range(sum(n.values()))], 'seed':'synthetic-seed'}
def selection(n=DOCDB):
    return {'status':'PARTICIPATING','tier':'T3','category_counts':dict(n),'total_count':sum(n.values()),
            'floors':dict(FLOORS),'total_floor':16,'targets':dict(TARGETS),
            'trace':[{'tier':'T3','category_counts':dict(n),'total_count':sum(n.values()),'floor_pass':True}],
            'same_build_and_seed':True,'complete_pool_verified':True}
def adapted():
    r=valid_report()
    r['participating_libraries']=list(decision.LIBRARIES);r['deferred_libraries']=[]
    r['library_validity']={kb:selection() for kb in decision.LIBRARIES}
    r['verifier_attestations'].update(role_separation_level='PROCESS_LEVEL_SEPARATION_SINGLE_UID',role_audit_verified=True)
    for c in r['candidates'].values():
        for row in c['libraries'].values():
            row.update(total_count=31,answerable_count=26,recall_hits_at_10=26,exact_count=3,exact_hits=3,no_answer_count=5,no_answer_correct=5,recall_at_10=1,exact=1,no_answer=1)
    return r

def defer(r,kb='spbp-2027'):
    r['participating_libraries'].remove(kb); r['deferred_libraries'].append(kb)
    v=r['library_validity'][kb];v.update(status='DEFERRED',tier=None,category_counts=counts((0,0,0,0,0,0)),total_count=0,
        trace=[{'tier':t,'category_counts':dict(SPBP),'total_count':16,'floor_pass':False} for t in ('T3','T2','T1')])
    for c in r['candidates'].values(): c['libraries'][kb]={'status':'NOT_RUN_DEFERRED'}

class TierSelectionTests(unittest.TestCase):
    def run_select(self, values):
        seen=[]
        def derive(kb,docs,seed,**kw):
            seen.append((kw['tier'],seed)); return pool(kw['tier'],values[kw['tier']])
        with patch.object(builder,'derive_cases_for_library',side_effect=derive):
            result=builder.derive_adaptive_library('spbp-2027',[],'synthetic-seed',exclusion=authority(),eligible_ids=set())
        return result,seen
    def test_docdb_31_t3_directly_passes_floor_not_target(self):
        r,seen=self.run_select({'T3':DOCDB});self.assertEqual(seen,[('T3','synthetic-seed')]);self.assertEqual(r['tier'],'T3');self.assertEqual(len(r['cases']),31)
    def test_exact_one_requires_next_tier_same_build_seed_no_mixing(self):
        r,seen=self.run_select({'T3':SPBP,'T2':DOCDB});self.assertEqual(seen,[('T3','synthetic-seed'),('T2','synthetic-seed')]);self.assertEqual(r['tier'],'T2');self.assertTrue(all(c['synthetic_tier']=='T2' for c in r['cases']))
    def test_t1_only_after_t2_failure(self):
        r,seen=self.run_select({'T3':SPBP,'T2':SPBP,'T1':DOCDB});self.assertEqual([x[0] for x in seen],['T3','T2','T1']);self.assertEqual(r['tier'],'T1')
    def test_t1_insufficient_deferred_empty_consumable_pool(self):
        r,seen=self.run_select(dict.fromkeys(('T3','T2','T1'),SPBP));self.assertEqual(r['status'],'DEFERRED');self.assertIsNone(r['tier']);self.assertEqual(r['cases'],[]);self.assertEqual(len(seen),3)
    def test_actual_exclusion_dimensions_and_bad_tier(self):
        d=document();r=authority('tokens','syn-1')
        self.assertEqual(builder.derive_cases_for_library('cwork-3m',[d],'s',exclusion=r,tier='T3')['cases'],[])
        self.assertTrue(builder.derive_cases_for_library('cwork-3m',[d],'s',exclusion=r,tier='T2')['cases'])
        r=authority('queries',d['title'].casefold())
        self.assertEqual(builder.derive_cases_for_library('cwork-3m',[d],'s',exclusion=r,tier='T2')['cases'],[])
        self.assertTrue(builder.derive_cases_for_library('cwork-3m',[d],'s',exclusion=r,tier='T1')['cases'])
        with self.assertRaises(ValueError):builder.derive_cases_for_library('cwork-3m',[d],'s',exclusion=r,tier='T0')
    def test_verifier_rejects_forged_deferred_or_tier_trace(self):
        docs=[document(i) for i in range(1,12)];r=authority()
        generated=builder.derive_adaptive_library('cwork-3m',docs,'s',exclusion=r)
        good=verifier.verify_adaptive_library('cwork-3m',docs,generated,'s',exclusion=r)
        self.assertTrue(good['selection_verified'])
        for mutate in (lambda x:x.update(tier='T0'),lambda x:x['trace'][0].update(floor_pass=not x['trace'][0]['floor_pass']),lambda x:x.update(seed='different'),lambda x:x['floors'].update(exact_identifier_date=1)):
            bad=copy.deepcopy(generated);mutate(bad)
            self.assertFalse(verifier.verify_adaptive_library('cwork-3m',docs,bad,'s',exclusion=r)['selection_verified'])

class AmendmentHarnessTests(unittest.TestCase):
    def check(self,r):
        decision.validate_report(r)
        import jsonschema
        s=json.loads((Path(__file__).resolve().parents[1]/'RT/RT-055/contracts/aggregate-report.schema.json').read_text())
        jsonschema.Draft202012Validator(s).validate(r)
    def test_participating_and_deferred_are_schema_valid(self):
        r=adapted();defer(r);self.check(r);v=decision.decide(r);self.assertEqual(v['decision'],'A');self.assertEqual(v['deferred_libraries'],['spbp-2027']);self.assertEqual(v['production_candidate_libraries'],['cwork-3m','docdb-touqian'])
    def test_deferred_cannot_hide_an_eligible_tier(self):
        r=adapted();defer(r);r['library_validity']['spbp-2027']['trace'][0].update(category_counts=DOCDB,total_count=31)
        with self.assertRaises(decision.ReportError):decision.validate_report(r)
    def test_wrong_tier_floor_sequence_null_role_and_partition_rejected(self):
        mutations=[lambda r:r['library_validity']['cwork-3m'].update(tier='T2'),lambda r:r['library_validity']['cwork-3m']['floors'].update(exact_identifier_date=1),lambda r:r['library_validity']['cwork-3m']['trace'].append(r['library_validity']['cwork-3m']['trace'][0]),lambda r:r['verifier_attestations'].update(role_separation_level=None),lambda r:r['participating_libraries'].pop(),lambda r:r['deferred_libraries'].append('unknown')]
        for m in mutations:
            r=adapted();m(r)
            with self.subTest(m=m),self.assertRaises(decision.ReportError):decision.validate_report(r)
    def test_all_deferred_invalid(self):
        r=adapted()
        for kb in decision.LIBRARIES:defer(r,kb)
        with self.assertRaises(decision.ReportError):decision.validate_report(r)
    def test_deferred_metrics_zero_null_or_extra_rejected(self):
        for payload in (None,0,{'status':'NOT_RUN_DEFERRED','total_count':0},adapted()['candidates'][decision.CANDIDATE_A]['libraries']['spbp-2027']):
            r=adapted();defer(r);r['candidates'][decision.CANDIDATE_A]['libraries']['spbp-2027']=payload
            with self.assertRaises(decision.ReportError):decision.validate_report(r)
    def test_participating_denominator_matches_frozen_pool(self):
        r=adapted()
        for c in r['candidates'].values():
            c['libraries']['cwork-3m'].update(total_count=32,no_answer_count=6,no_answer_correct=6)
        with self.assertRaises(decision.ReportError):decision.validate_report(r)
    def test_participating_quality_and_utility_unchanged(self):
        r=adapted();defer(r)
        for c in r['candidates'].values():c['libraries']['docdb-touqian'].update(exact_hits=2,exact=2/3)
        self.assertEqual(decision.decide(r)['decision'],'NO-GO')
        r=adapted();defer(r);a,b=(r['candidates'][k] for k in (decision.CANDIDATE_A,decision.CANDIDATE_B));b['operations']=a['operations'].copy()
        for kb in r['participating_libraries']:
            for f in decision.RESOURCE_FIELDS:b['libraries'][kb][f]=a['libraries'][kb][f]*.7
        self.assertEqual(decision.decide(r)['decision'],'B')
    def test_external_drift_honest_schema_value_but_invalid_decision(self):
        r=adapted();r['production_invariants']['nas_unchanged']=False
        with self.assertRaises(decision.ReportError):decision.decide(r)

if __name__=='__main__':unittest.main()
