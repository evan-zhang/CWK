#!/usr/bin/env python3
"""Detached formal sequence: one baseline/freeze/order, finally cleanup+after."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parent

def main():
    os.umask(0o077);fd=os.open(ROOT/'status/formal.claim',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    state={'status':'RUNNING','phase':'BEFORE','process_id':os.getpid(),'started_at':time.time(),'completed':[]}
    def save():
        p=ROOT/'status/formal.json';tmp=p.with_suffix('.next');tmp.write_text(json.dumps(state));os.replace(tmp,p)
    def run(name,script,*args):
        state['phase']=name
        with (ROOT/'audit'/(name.lower()+'.log')).open('wb') as log:
            p=subprocess.Popen([sys.executable,str(ROOT/'impl'/script),*args],cwd=ROOT/'impl',stdout=log,stderr=subprocess.STDOUT)
            state['child_process_id']=p.pid;save();code=p.wait();state['child_process_id']=0
        if code:state['failed_phase']=name;state['exit_code']=code;save();return False
        state['completed'].append(name);save();return True
    failed=False
    try:
        save()
        checks=json.loads((ROOT/'verifier/case-verification.json').read_text())
        if not checks['verified']:raise RuntimeError('cases_invalid')
        for phase,script,args in (('BEFORE','rt055_baseline.py',['before']),('FREEZE','rt055_freeze.py',['create']),('VERIFY_FREEZE','rt055_freeze.py',['verify'])):
            if not run(phase,script,*args):failed=True;break
        if not failed:
            receipt=json.loads((ROOT/'freeze/freeze-receipt.json').read_text())
            for key in receipt['run_order']:
                if not run('RUN_'+key.upper(),'rt055_run_'+key+'.py','--mode','run'):failed=True;break
    except Exception as e:state.update(error_kind=type(e).__name__);failed=True
    finally:
        cleaned=run('CLEANUP','rt055_cleanup.py')
        after=run('AFTER','rt055_baseline.py','after') if (ROOT/'audit/production-before.json').exists() else False
        aggregated=run('AGGREGATE','rt055_aggregate.py') if not failed and cleaned and after else False
        state.update(status='COMPLETE' if aggregated else 'INVALID',phase='COMPLETE',finished_at=time.time(),child_process_id=0);save()
    return 0 if state['status']=='COMPLETE' else 3
if __name__=='__main__':raise SystemExit(main())
