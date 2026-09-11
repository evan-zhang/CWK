#!/usr/bin/env python3
"""Local synthetic Amendment 3 evidence; never dispatches an OPS runner.

Runs test_rt055_* only with a credential-free child environment. Mutations run
in disposable source copies, never in the working tree. Emits counts/enums,
not stdout, traceback, paths, case data, private digests, or arbitrary strings.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=(
    ('BUILDER_ACCEPTS_FIRST_FAILED_TIER','scripts/rt055_tiers.py',
     "        if row['floor_pass']:","        if True:",
     'test_rt055_amendment3.TierSelectionTests'),
    ('VERIFIER_SELECTION_DISCONNECTED','scripts/rt055_verifier.py',
     "    selection_ok=all(generated.get(k)==replay[k] for k in compare)",
     "    selection_ok=True",'test_rt055_amendment3_closeout.ReplayTests'),
    ('DEFERRED_METRIC_REJECTION_DISCONNECTED','scripts/kb_retrieval_decision.py',
     "            if libraries[kb] != {'status':'NOT_RUN_DEFERRED'}:",
     "            if False:",'test_rt055_amendment3.AmendmentHarnessTests'),
    ('ROLE_ENUM_REJECTION_DISCONNECTED','scripts/kb_retrieval_decision.py',
     "    if attest['role_separation_level'] not in tiers.ROLE_LEVELS or attest['role_audit_verified'] is not True:",
     "    if False:",'test_rt055_amendment3_closeout.ClosedContractTests'),
)


def stats(log,code):
    match=re.search(r'Ran (\d+) tests? in ',log)
    if not match:raise RuntimeError('test_summary_missing')
    def count(name):
        found=re.search(r'\b'+name+r'=(\d+)',log)
        return int(found.group(1)) if found else 0
    return dict(tests=int(match.group(1)),failures=count('failures'),errors=count('errors'),
                skipped=count('skipped'),exit_code=code)


def run(root,test=None):
    env={'PATH':os.environ['PATH'],'LANG':'C','LC_ALL':'C','TMPDIR':tempfile.gettempdir(),
         'PYTHONDONTWRITEBYTECODE':'1'}
    args=[sys.executable,'-m','unittest']
    if test:args.append(test);cwd=root/'tests'
    else:args+=['discover','-s','tests','-p','test_rt055_*.py'];cwd=root
    p=subprocess.run(args,cwd=cwd,env=env,capture_output=True,text=True,timeout=180)
    return stats(p.stdout+p.stderr,p.returncode)


def verify_evidence(value):
    import jsonschema
    schema=json.loads((ROOT/'RT/RT-055/evidence/amendment3-local-tests.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(value)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--red-log',type=Path)
    parser.add_argument('--initial-red-log',type=Path)
    parser.add_argument('--inherited-log',type=Path)
    args=parser.parse_args(argv)
    result={'schema':'cwk.rt055.amendment3-local-tests.v1','data_class':'SYNTHETIC_ONLY_NOT_OPS',
            'predecessor_commit':'45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4',
            'baseline_commit':'c86519425e260a45a11a89917cb0c6600d46a11f',
            'ops_candidate_runs':0,'ops_builder_runs':0,'ops_verifier_runs':0,
            'red_basis':'INHERITED_UNCOMMITTED_IMPLEMENTATION_BEFORE_CLOSEOUT_FIXES',
            'initial_red':stats(args.initial_red_log.read_text(),1) if args.initial_red_log else None,
            'corrected_red':stats(args.red_log.read_text(),1) if args.red_log else None,
            'inherited_regression':stats(args.inherited_log.read_text(),0) if args.inherited_log else None,
            'initial_red_fixture_issue':'TWO_CAPACITY_TESTS_USED_NON_TABLE_FIXTURE_CORRECTED_BEFORE_IMPLEMENTATION',
            'intermediate_green_issue':'INHERITED_MUTABLE_FLOOR_FIXTURE_ALIAS_FIXED_NOT_PRODUCTION_RULE_CHANGE',
            'green':run(ROOT),'mutations':[]}
    if result['green']['exit_code']:raise RuntimeError('green_suite_failed')
    with tempfile.TemporaryDirectory(prefix='rt055-mutation-') as td:
        base=Path(td)
        for index,(name,filename,old,new,test) in enumerate(MUTATIONS):
            target=base/str(index)
            (target/'scripts').mkdir(parents=True);(target/'tests').mkdir()
            for p in (ROOT/'scripts').glob('*.py'):shutil.copy2(p,target/'scripts'/p.name)
            for p in (ROOT/'tests').glob('test_rt055_*.py'):shutil.copy2(p,target/'tests'/p.name)
            shutil.copytree(ROOT/'RT/RT-055/contracts',target/'RT/RT-055/contracts')
            p=target/filename;text=p.read_text()
            if text.count(old)!=1:raise RuntimeError('mutation_span_not_unique')
            p.write_text(text.replace(old,new))
            measurement=run(target,test)
            rejected=measurement['exit_code']!=0 and measurement['failures']>0
            result['mutations'].append({'mutation':name,**measurement,'rejected':rejected})
            if not rejected:raise RuntimeError('mutation_not_detected')
    result['restored_green']=run(ROOT)
    if result['restored_green']['exit_code']:raise RuntimeError('restored_suite_failed')
    verify_evidence(result)
    payload=json.dumps(result,indent=2)+'\n'
    if args.output:args.output.write_text(payload)
    print(payload,end='')
    return 0


if __name__=='__main__':raise SystemExit(main())
