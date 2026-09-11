"""Local-only adversarial closeout checks; all documents and processes synthetic."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import jsonschema
import kb_retrieval_decision as decision
import rt055_builder as builder
import rt055_verifier as verifier
import rt055_exclusion as exclusion
import rt055_tiers as tiers
import rt055_freeze as freeze
import rt055_role_runner as roles
from test_rt055_amendment3 import adapted, defer, DOCDB, SPBP, counts
from test_rt055_exclusion import document, authority


def capacity_document(number):
    doc=document(number)
    marker=chr(0x4e30+number)
    doc['body']=f'独有{marker}甲乙丙丁戊己庚辛壬癸\nSYN-{number:03d}\n| 合成表格{marker}专属 | 一百 |\nshared common neighbours'
    return doc


def validator():
    return jsonschema.Draft202012Validator(json.loads(
        (ROOT / 'RT/RT-055/contracts/aggregate-report.schema.json').read_text()))


class ClosedContractTests(unittest.TestCase):
    def test_version_migration_is_explicit_not_silent_v2_reinterpretation(self):
        r = adapted()
        self.assertEqual(decision.SCHEMA, 'cwk.rt055.retrieval-decision.aggregate.v3')
        r['schema'] = 'cwk.rt055.retrieval-decision.aggregate.v2'
        with self.assertRaisesRegex(decision.ReportError, 'LEGACY_V2_REQUIRES_NEW_VERIFIED_RUN'):
            decision.decide(r)
        self.assertFalse(validator().is_valid(r))
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'synthetic-v2.json';p.write_text(json.dumps(r))
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=decision.main([str(p)])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(output.getvalue())['error_code'],'LEGACY_V2_REQUIRES_NEW_VERIFIED_RUN')

    def test_schema_rejects_valid_capacity_disguised_as_deferred(self):
        r = adapted(); defer(r)
        for row in r['library_validity']['spbp-2027']['trace']:
            row.update(category_counts=DOCDB, total_count=31, floor_pass=False)
        self.assertFalse(validator().is_valid(r))
        with self.assertRaises(decision.ReportError): decision.decide(r)

    def test_schema_rejects_exact_one_disguised_as_passed_trace(self):
        r = adapted()
        r['library_validity']['cwork-3m']['trace'][0].update(category_counts=SPBP, total_count=16)
        self.assertFalse(validator().is_valid(r))

    def test_schema_requires_ordered_partition(self):
        r = adapted(); r['participating_libraries'].reverse()
        self.assertFalse(validator().is_valid(r))

    def test_schema_and_decision_close_all_partition_and_metric_shapes(self):
        mutations = [lambda r:r['participating_libraries'].append('cwork-3m'),
                     lambda r:r['deferred_libraries'].append('cwork-3m'),
                     lambda r:r['library_validity'].update(unknown={}),
                     lambda r:r['library_validity'].pop('cwork-3m'),
                     lambda r:r['candidates'][decision.CANDIDATE_A]['libraries'].pop('cwork-3m'),
                     lambda r:r['candidates'][decision.CANDIDATE_A]['libraries']['cwork-3m'].pop('p95_ms')]
        for mutation in mutations:
            r=adapted(); mutation(r)
            with self.subTest(mutation=mutation):
                self.assertFalse(validator().is_valid(r))
                with self.assertRaises(decision.ReportError):decision.decide(r)

    def test_every_library_can_be_the_only_participant_without_special_results(self):
        for participant in decision.LIBRARIES:
            r=adapted()
            for kb in decision.LIBRARIES:
                if kb!=participant:defer(r,kb)
            validator().validate(r)
            self.assertEqual(decision.decide(r)['decision'],'A')
            for c in r['candidates'].values():
                c['libraries'][participant].update(exact_hits=2,exact=2/3)
            self.assertEqual(decision.decide(r)['decision'],'NO-GO')

    def test_cli_all_deferred_invalid_without_private_exception(self):
        r=adapted()
        for kb in decision.LIBRARIES:defer(r,kb)
        self.assertFalse(validator().is_valid(r))
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'synthetic.json';p.write_text(json.dumps(r))
            output=io.StringIO()
            with contextlib.redirect_stdout(output): code=decision.main([str(p)])
        self.assertEqual(code,2)
        out=json.loads(output.getvalue())
        self.assertEqual(out['decision'],'INVALID')
        self.assertEqual(out['deferred_libraries'],list(decision.LIBRARIES))
        self.assertNotIn('error',out)

    def test_deferred_metric_variants_fail_both_gates(self):
        for payload in (None,0,{}, {'status':'NOT_RUN_DEFERRED','p95_ms':0}, {'status':'PASS'}):
            r=adapted();defer(r)
            r['candidates'][decision.CANDIDATE_B]['libraries']['spbp-2027']=payload
            self.assertFalse(validator().is_valid(r))
            with self.assertRaises(decision.ReportError):decision.decide(r)

    def test_role_levels_and_audit_are_strict(self):
        for level in (None,'SINGLE_UID',0,True,{},[]):
            r=adapted();r['verifier_attestations']['role_separation_level']=level
            self.assertFalse(validator().is_valid(r))
            with self.assertRaises(decision.ReportError):decision.decide(r)
        for level in tiers.ROLE_LEVELS:
            r=adapted();r['verifier_attestations']['role_separation_level']=level
            validator().validate(r);decision.decide(r)
        for value in (None,False,1):
            r=adapted();r['verifier_attestations']['role_audit_verified']=value
            self.assertFalse(validator().is_valid(r))
            with self.assertRaises(decision.ReportError):decision.decide(r)

    def test_identity_booleans_cannot_be_integer_one(self):
        r=adapted();r['candidates'][decision.CANDIDATE_A]['identity']['doc_collapse']=1
        self.assertFalse(validator().is_valid(r))
        with self.assertRaises(decision.ReportError):decision.decide(r)

    def test_boolean_cleanup_count_is_not_zero(self):
        r=adapted();r['cleanup']['cleanup_failures']=False
        self.assertFalse(validator().is_valid(r))
        with self.assertRaises(decision.ReportError):decision.decide(r)

    def test_false_and_unknown_invariants_are_recordable_never_pass(self):
        for value in (False,None):
            r=adapted();r['production_invariants']['nas_unchanged']=value
            validator().validate(r)
            with self.assertRaises(decision.ReportError):decision.decide(r)


class ReplayTests(unittest.TestCase):
    def test_each_individual_floor_fails_and_minimum_complete_pool_passes(self):
        self.assertTrue(tiers.floor_pass(dict(tiers.FLOORS)))
        for category in tiers.CATEGORIES:
            n=dict(tiers.TARGETS);n[category]=tiers.FLOORS[category]-1
            self.assertFalse(tiers.floor_pass(n))
        self.assertFalse(tiers.floor_pass(counts((3,1,3,3,3,3))))

    def test_count_unknown_fields_not_silently_discarded(self):
        with self.assertRaises(ValueError):tiers.trace_row('T3',dict(DOCDB,unknown=1))

    def test_independent_replay_must_not_share_builder_selector(self):
        docs=[capacity_document(i) for i in range(1,12)];r=authority()
        g=builder.derive_adaptive_library('cwork-3m',docs,'synthetic',exclusion=r)
        # Disconnect only the builder-side selector. Verifier must still replay.
        with patch.object(tiers,'select_once',side_effect=AssertionError('builder_selector_called')):
            checked=verifier.verify_adaptive_library('cwork-3m',docs,g,'synthetic',exclusion=r)
        self.assertTrue(checked['selection_verified'])

    def test_empty_and_insufficient_corpus_are_honest_deferred(self):
        for docs in ([],[document(1)]):
            g=builder.derive_adaptive_library('cwork-3m',docs,'synthetic',exclusion=authority())
            self.assertEqual(g['status'],'DEFERRED');self.assertEqual(g['cases'],[])
            checked=verifier.verify_adaptive_library('cwork-3m',docs,g,'synthetic',exclusion=authority())
            self.assertTrue(checked['selection_verified']);self.assertTrue(checked['exclusion_verified'])

    def test_actual_valid_pool_cannot_be_forged_deferred(self):
        docs=[capacity_document(i) for i in range(1,12)];r=authority()
        g=builder.derive_adaptive_library('cwork-3m',docs,'synthetic',exclusion=r)
        self.assertEqual(g['status'],'PARTICIPATING')
        g.update(status='DEFERRED',tier=None,cases=[],category_counts=dict.fromkeys(tiers.CATEGORIES,0))
        g['trace']=[dict(tier=t,category_counts=SPBP,total_count=16,floor_pass=False) for t in tiers.TIERS]
        self.assertFalse(verifier.verify_adaptive_library('cwork-3m',docs,g,'synthetic',exclusion=r)['selection_verified'])

    def test_verifier_rejects_missing_case_extra_case_mixed_seed_and_ledger(self):
        docs=[capacity_document(i) for i in range(1,12)];r=authority('doc_ids',docs[0]['doc_id'])
        good=builder.derive_adaptive_library('cwork-3m',docs,'synthetic',exclusion=r)
        mutations=[lambda g:g['cases'].pop(),lambda g:g['cases'].append(g['cases'][0]),
                   lambda g:g['cases'][0].update(seed='other'),lambda g:g['excluded_sources'].clear(),
                   lambda g:g['excluded_sources'].append(g['excluded_sources'][0]),
                   lambda g:g['trace'][0].update(total_count=0)]
        for mutation in mutations:
            g=copy.deepcopy(good);mutation(g)
            self.assertFalse(verifier.verify_adaptive_library('cwork-3m',docs,g,'synthetic',exclusion=r)['selection_verified'])

    def test_low_tier_reports_relaxed_intersections_truthfully(self):
        docs=[document(i) for i in range(1,12)]
        r=authority('tokens','syn-1')
        # The R member originates outside this synthetic current snapshot.
        r['members'][0]['source_ids']=['synthetic-absent-origin']
        g=builder.derive_cases_for_library('cwork-3m',docs,'synthetic',exclusion=r,tier='T2')
        proof=exclusion.verify_exclusion(docs,r,g,tier='T2')
        self.assertTrue(proof['verified']);self.assertGreater(proof['overlap_counts']['tokens'],0)
        self.assertFalse(exclusion.verify_exclusion(docs,r,g,tier='T3')['verified'])


class RunnerBoundaryTests(unittest.TestCase):
    def test_role_matrix_blocks_code_symlinks_and_impl_private_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            for role in roles.ROLES:(root/role).mkdir()
            alias=root/'impl/renamed.txt';alias.symlink_to(root/'builder/rt055_builder.py')
            self.assertTrue(roles.forbidden(root,'impl',alias))
            self.assertTrue(roles.forbidden(root,'verifier',root/'builder/rt055_builder.py'))
            self.assertFalse(roles.forbidden(root,'verifier',root/'builder/private-candidates.json'))
            self.assertTrue(roles.forbidden(root,'builder',root/'impl/kb_retrieval_candidates.py'))

    def test_role_audit_rereads_live_workspace_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'status').mkdir()
            for i,role in enumerate(roles.ROLES):
                (root/role).mkdir(mode=0o700)
                row=dict(role_id=roles.ROLES[role],status='PASS',process_id=1000+i,uid=os.getuid(),
                         workspace_mode=0o700,cwd=str(root/role),workspace=str(root/role),
                         forbidden_reads=0,denial_probes=1,started_at=10+i*2,finished_at=11+i*2)
                (root/'status'/(role+'.json')).write_text(json.dumps(row))
            self.assertTrue(freeze.role_audit(root)['verified'])
            (root/'builder').chmod(0o755)
            self.assertFalse(freeze.role_audit(root)['verified'])

    def test_actual_single_uid_three_process_audit_and_duplicate_claim_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();root.chmod(0o700);(root/'status').mkdir(mode=0o700)
            shutil.copyfile(ROOT/'scripts/rt055_role_runner.py',root/'rt055_role_runner.py')
            for role,filename in (('builder','rt055_builder.py'),('verifier','rt055_verifier.py'),('impl','rt055_prepare.py')):
                folder=root/role;folder.mkdir(mode=0o700)
                (folder/filename).write_text('raise SystemExit(0)\n')
            for role in ('impl','builder','verifier'):
                proc=subprocess.run([sys.executable,str(root/'rt055_role_runner.py'),role],
                    env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=20)
                self.assertEqual(proc.returncode,0)
            proof=freeze.role_audit(root)
            self.assertTrue(proof['verified'])
            self.assertEqual(proof['role_separation_level'],'PROCESS_LEVEL_SEPARATION_SINGLE_UID')
            prior=(root/'status/builder.json').read_bytes()
            proc=subprocess.run([sys.executable,str(root/'rt055_role_runner.py'),'builder'],capture_output=True,timeout=20)
            self.assertNotEqual(proc.returncode,0);self.assertEqual(prior,(root/'status/builder.json').read_bytes())

    def test_baseline_import_is_inert(self):
        # No command invocation, network access, or claims allowed during import.
        program="import sys;sys.path.insert(0,sys.argv[1]);import rt055_baseline"
        proc=subprocess.run([sys.executable,'-c',program,str(ROOT/'scripts')],
            env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=20)
        self.assertEqual(proc.returncode,0)

    def test_freeze_rejects_empty_artifact_manifest_before_external_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'freeze').mkdir()
            (root/'freeze/freeze-receipt.json').write_text(json.dumps({'run_order':['a','b'],'private_files':{},'candidates':{}}))
            with patch.object(freeze,'artifact_plan',return_value={}),patch.object(freeze,'role_audit',return_value={'verified':True}),patch.object(freeze,'upstream',return_value={'verified_on_ops':True}):
                self.assertFalse(freeze.verify_artifacts(root))


if __name__ == '__main__':unittest.main()
