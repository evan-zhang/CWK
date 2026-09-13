"""Synthetic actual-builder/verifier pipeline; all OPS I/O replaced in-process."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rt055_builder as builder
import rt055_verifier as verifier
import rt055_exclusion as exclusion
import rt055_tiers as tiers
from test_rt055_exclusion import authority
from test_rt055_amendment3_closeout import capacity_document


def capacity_docs(n):
    docs=[]
    for i in range(1,n+1):
        d=capacity_document(i)
        if n==3 and i>1:d['body']=d['body'].replace(f'SYN-{i:03d}','plain')
        if n==10:
            if i>3:
                d['body']=d['body'].replace(f'SYN-{i:03d}','plain')
                d['body']='\n'.join(line for line in d['body'].splitlines() if '|' not in line)
            if i>5:d['body']='plain'
        docs.append(d)
    return docs


class ActualTierPipelineTests(unittest.TestCase):
    def test_actual_docdb_shaped_31_t3_pool_verifies_for_any_library(self):
        for kb in builder.ops.LIBRARIES:
            docs=capacity_docs(10)
            g=builder.derive_adaptive_library(kb,docs,'synthetic',exclusion=authority())
            self.assertEqual(g['tier'],'T3');self.assertEqual(len(g['cases']),31)
            checked=verifier.verify_adaptive_library(kb,docs,g,'synthetic',exclusion=authority())
            self.assertTrue(checked['selection_verified']);self.assertEqual(checked['case_total'],31)
            self.assertTrue(all(row['ok'] for row in checked['category_coverage'].values()))

    def test_actual_spbp_shaped_16_t3_automatically_evaluates_t2_and_t1(self):
        docs=capacity_docs(3)
        with patch.object(builder,'derive_cases_for_library',wraps=builder.derive_cases_for_library) as derive:
            g=builder.derive_adaptive_library('spbp-2027',docs,'synthetic',exclusion=authority())
        self.assertEqual([call.kwargs['tier'] for call in derive.call_args_list],['T3','T2','T1'])
        self.assertTrue(all(call.args[2]=='synthetic' for call in derive.call_args_list))
        self.assertEqual(g['status'],'DEFERRED');self.assertEqual(g['cases'],[])
        self.assertTrue(all(row['total_count']==16 and not row['floor_pass'] for row in g['trace']))
        self.assertTrue(verifier.verify_adaptive_library('spbp-2027',docs,g,'synthetic',exclusion=authority())['selection_verified'])

    def test_real_t2_relaxation_replays_without_mixed_pool(self):
        docs=capacity_docs(3)+[capacity_document(4)]
        r=authority('tokens','syn-004',origin='synthetic-absent-origin')
        g=builder.derive_adaptive_library('spbp-2027',docs,'synthetic',exclusion=r)
        self.assertEqual(g['tier'],'T2')
        self.assertEqual([row['floor_pass'] for row in g['trace']],[False,True])
        self.assertEqual(g['cases'],builder.derive_cases_for_library('spbp-2027',docs,'synthetic',exclusion=r,tier='T2')['cases'])
        checked=verifier.verify_adaptive_library('spbp-2027',docs,g,'synthetic',exclusion=r)
        self.assertTrue(checked['selection_verified']);self.assertTrue(checked['exclusion_verified'])

    def test_real_t1_relaxation_replays_without_mixed_pool(self):
        docs=capacity_docs(3)+[capacity_document(4)]
        r=authority('queries',exclusion.normalize(docs[-1]['title']),origin='synthetic-absent-origin')
        g=builder.derive_adaptive_library('spbp-2027',docs,'synthetic',exclusion=r)
        self.assertEqual(g['tier'],'T1')
        self.assertEqual([row['floor_pass'] for row in g['trace']],[False,False,True])
        self.assertEqual(g['cases'],builder.derive_cases_for_library('spbp-2027',docs,'synthetic',exclusion=r,tier='T1')['cases'])
        self.assertTrue(verifier.verify_adaptive_library('spbp-2027',docs,g,'synthetic',exclusion=r)['selection_verified'])

    def roundtrip(self,all_deferred=False,tamper_claim=False):
        with tempfile.TemporaryDirectory(prefix='rt055-synthetic-') as td, contextlib.ExitStack() as stack:
            root=Path(td);root.chmod(0o700)
            for role in ('builder','verifier'):(root/role).mkdir(mode=0o700)
            (root/'.rt055-owned').touch()
            docs={kb:capacity_docs(10) if i==0 and not all_deferred else [] for i,kb in enumerate(builder.ops.LIBRARIES)}
            stack.enter_context(patch.object(builder,'HERE',root/'builder'))
            stack.enter_context(patch.object(builder,'ROOT',root))
            stack.enter_context(patch.object(verifier,'HERE',root/'verifier'))
            stack.enter_context(patch.object(verifier,'ROOT',root))
            stack.enter_context(patch.object(builder.ops,'find_gateway_processes',return_value=[]))
            stack.enter_context(patch.object(builder.ops,'gateway_health',return_value={}))
            stack.enter_context(patch.object(builder.ops,'gateway_source_envs',return_value={kb:{} for kb in docs}))
            stack.enter_context(patch.object(builder.ops,'nas_metadata_summary',return_value={}))
            snapshot=stack.enter_context(patch.object(builder.ops,'snapshot_libraries',side_effect=lambda *_:(copy.deepcopy(docs),{kb:{} for kb in docs})))
            stack.enter_context(patch.object(exclusion,'reconstruct',return_value=authority()))
            stack.enter_context(patch.object(sys,'argv',['synthetic-builder']))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(builder.main(),0)
            self.assertEqual(snapshot.call_count,1)
            before=(root/'builder/private-candidates.json').read_bytes()
            with self.assertRaises(FileExistsError):builder.main()
            self.assertEqual(snapshot.call_count,1)
            self.assertEqual(before,(root/'builder/private-candidates.json').read_bytes())
            if tamper_claim:
                p=root/'builder/single-build-claim.json';claim=json.loads(p.read_text());claim['single_build_claimed']=False;p.write_text(json.dumps(claim))
            code=verifier.mode_verify_cases()
            self.assertEqual(snapshot.call_count,2)
            with self.assertRaises(FileExistsError):verifier.mode_verify_cases()
            self.assertEqual(snapshot.call_count,2)
            result=json.loads((root/'verifier/case-verification.json').read_text())
            self.assertEqual(code,3 if all_deferred or tamper_claim else 0)
            self.assertEqual(result['verified'],not (all_deferred or tamper_claim))
            return result

    def test_single_claim_seed_builder_and_verifier_mixed_empty_libraries(self):
        result=self.roundtrip()
        self.assertEqual(result['participating_libraries'],['cwork-3m'])
        self.assertEqual(result['deferred_libraries'],['docdb-touqian','spbp-2027'])

    def test_all_deferred_pipeline_invalid(self):
        self.assertFalse(self.roundtrip(all_deferred=True)['at_least_one_participating'])

    def test_forged_build_claim_invalid(self):
        self.assertFalse(self.roundtrip(tamper_claim=True)['single_build_seed_verified'])


if __name__=='__main__':unittest.main()
