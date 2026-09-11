#!/usr/bin/env python3
"""Single detached pool launch; never retries builder/verifier after a claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parent

def main():
    os.umask(0o077)
    fd=os.open(ROOT/'status/pool.claim',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    state={'phase':'STARTING','status':'RUNNING','process_id':os.getpid(),'builder_runs':0,'verifier_runs':0}
    def save():
        p=ROOT/'status/pool.json';tmp=p.with_suffix('.next');tmp.write_text(json.dumps(state));os.replace(tmp,p)
    try:
        save()
        for role in ('impl','builder','verifier'):
            state.update(phase=role.upper())
            if role!='impl':state[role+'_runs']=1
            proc=subprocess.Popen([sys.executable,str(ROOT/'rt055_role_runner.py'),role],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            state['child_process_id']=proc.pid;save();code=proc.wait()
            if code:state.update(status='FAIL',exit_code=code,child_process_id=0);break
        else:state.update(phase='VERIFIED',status='PASS',exit_code=0,child_process_id=0)
    except Exception:state.update(status='FAIL',exit_code=2,child_process_id=0)
    finally:state['finished_at']=time.time();save()
    return state['exit_code']
if __name__=='__main__':raise SystemExit(main())
