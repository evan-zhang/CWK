#!/usr/bin/env python3
"""Detached/resumable same-window sequence. Never repeats completed scoring.

Before/freeze/verification/results/cleanup/after/aggregate remain append-only.
A transport loss does not cancel the detached child. A failed execution attempt
with no ambiguous consumption stops for reconciliation, not a new experiment.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import rt055_opslib as ops
import rt055_window as window
import rt055_runtime as runtime
import rt055_freeze as freeze
ROOT=Path(__file__).resolve().parent.parent


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window-id',required=True)
    parser.add_argument('--privacy-migration-id',required=True)
    parser.add_argument('--closeout-invalid',action='store_true')
    args=parser.parse_args(argv)
    os.umask(0o077);w=window.directory(ROOT,args.window_id)
    if not args.closeout_invalid:
        from rt055_scoring_input import coordinator_precheck
        coordinator_precheck(ROOT,args.window_id,args.privacy_migration_id)
    (w/'status').mkdir(parents=True,mode=0o700,exist_ok=True)
    lock=(w/'status/controller.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    attempt=str(uuid.uuid4())
    window.write_once(w/'controllers'/attempt/'claim.json',window.envelope(ROOT,args.window_id,'controller'))
    state={**window.envelope(ROOT,args.window_id,'formal'),'status':'RUNNING','phase':'PRECHECK',
           'process_id':os.getpid(),'started_at':time.time(),'completed':[],'controller_attempt':attempt}
    def save():ops.write_private_json(w/'status/formal.json',state)
    def run(name,script,*extra):
        state['phase']=name;save()
        from rt055_log_firewall import run_command
        from rt055_candidate_workspace import needles
        values=needles(ops.read_json(ROOT/'builder/private-corpus.json'),[])+needles(ops.read_json(ROOT/'verifier/private-verified.json'),[])
        code=run_command([sys.executable,str(ROOT/'impl'/script),*extra],
            w/'controllers'/attempt/(name.lower()+'.log'),values,cwd=ROOT/'impl',stdin=subprocess.DEVNULL)
        state['child_process_id']=0
        window.write_once(w/'controllers'/attempt/(name.lower()+'.json'),{'phase':name,'exit_code':code,'finished_at':time.time()})
        if code:state.update(failed_phase=name,exit_code=code)
        else:state['completed'].append(name)
        save();return code
    def after_and_cleanup():
        if not (w/'audit/cleanup.json').exists():
            run('CLEANUP','rt055_cleanup.py','--window-id',args.window_id)
        if not (w/'status/baseline-after.claim').exists():
            window.baseline(ROOT,args.window_id,'before')
            run('AFTER','rt055_baseline.py','after','--window-id',args.window_id)
        window.comparison(ROOT,args.window_id)
    try:
        save()
        if args.closeout_invalid:
            after_and_cleanup();state.update(status='INVALID',phase='COMPLETE');return 3
        checks=ops.read_json(ROOT/'verifier/case-verification.json')
        pool=ops.read_json(ROOT/'status/pool.json')
        if not checks['verified'] or pool['builder_runs']!=1 or pool['verifier_runs']!=1:
            raise RuntimeError('roles_or_cases_invalid')
        if not runtime.privacy_passed(ROOT,args.privacy_migration_id):raise RuntimeError('privacy_migration_invalid')
        if not (w/'status/baseline-before.claim').exists():
            if run('BEFORE','rt055_baseline.py','before','--window-id',args.window_id):raise RuntimeError('before_incomplete')
        window.baseline(ROOT,args.window_id,'before')
        if not (w/'status/freeze.claim').exists():
            if run('FREEZE','rt055_freeze.py','create','--window-id',args.window_id,'--privacy-migration-id',args.privacy_migration_id):raise RuntimeError('freeze_create_incomplete')
        if not (w/'status/freeze-verification.claim').exists():
            if run('VERIFY_FREEZE','rt055_freeze.py','verify','--window-id',args.window_id):raise RuntimeError('freeze_verify_incomplete')
        window.verification(ROOT,args.window_id)
        receipt=window.receipt(ROOT,args.window_id)
        if receipt['privacy_migration_id']!=args.privacy_migration_id:raise RuntimeError('privacy_receipt_mismatch')
        for key in receipt['run_order']:
            result=w/('run-'+key)/'result.json'
            if result.exists():
                value=window.validate_run(ROOT,args.window_id,key,checks['participating_libraries'])
                if value.get('status')!='OK' or value.get('window_id')!=args.window_id:raise RuntimeError('completed_result_invalid')
                continue
            runtime.verify_ready(ROOT,key,args.window_id)
            if run('RUN_'+key.upper(),'rt055_run_'+key+'.py','--mode','run','--window-id',args.window_id):
                # Completion artifacts survive SSH failures. An unmatched consumption
                # is never replayed. Only the parent may classify a real hard gate.
                for kb in checks['participating_libraries']:window.library_result(ROOT,args.window_id,key,kb)
                scans=list(w.glob('run-*/attempts/*/log-scan.json'))
                if any(ops.read_json(p).get('passed') is not True for p in scans):
                    after_and_cleanup();state.update(status='INVALID',phase='COMPLETE',reason='CANDIDATE_LOG_PRIVACY_FAILED');return 3
                state.update(status='WAITING_RECONCILIATION',phase='EXECUTION_ATTEMPT_FAILED')
                return 4
        after_and_cleanup()
        if not (w/'aggregate/decision.json').exists():
            code=run('AGGREGATE','rt055_aggregate.py','--window-id',args.window_id)
            if code not in (0,2):raise RuntimeError('aggregate_incomplete')
        result=ops.read_json(w/'aggregate/decision.json')
        state.update(status='COMPLETE',phase='COMPLETE',decision=result['decision'])
        return 0
    except Exception as exc:
        # Do not turn an executor/transport fault into terminal INVALID. Preserve
        # state, safely stop at the boundary, and require explicit reconciliation.
        state.update(status='WAITING_RECONCILIATION',error_kind=type(exc).__name__)
        return 4
    finally:
        state.update(finished_at=time.time(),child_process_id=0);save()
        window.write_once(w/'controllers'/attempt/'terminal.json',state)
        lock.close()

if __name__=='__main__':raise SystemExit(main())
