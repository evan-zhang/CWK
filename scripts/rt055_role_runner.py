#!/usr/bin/env python3
"""OPS detached role wrapper: actual cwd/mode/uid/pid and denied-read probes."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import time

ROLES={'builder':'ops-rt055-builder','verifier':'ops-rt055-verifier','impl':'cwk-rt055-candidate-implementer'}

def forbidden(root,role,value):
    if not isinstance(value,(str,bytes,os.PathLike)):return False
    path=Path(os.path.realpath(os.fsdecode(value)))
    for other in ROLES:
        if other==role:continue
        folder=root/other
        if path.is_relative_to(folder):
            if role=='verifier' and other=='builder' and path.suffix not in ('.py','.pyc'):return False
            return True
    if role=='impl' and any(path.is_relative_to(root/x) for x in ('freeze','consumption','run-a','run-b')):return True
    return False

def main():
    root=Path(__file__).resolve().parent;role=sys.argv[1];assert role in ROLES
    folder=root/role;assert folder.is_dir() and not folder.is_symlink() and folder.stat().st_mode & 0o077==0
    os.umask(0o077);os.chdir(folder)
    claim=root/'status'/(role+'.claim');fd=os.open(claim,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    state={'role_id':ROLES[role],'process_id':os.getpid(),'parent_process_id':os.getppid(),'uid':os.getuid(),'workspace':str(folder.resolve()),'workspace_mode':folder.stat().st_mode & 0o777,'cwd':str(Path.cwd()),'started_at':time.time(),'status':'RUNNING','forbidden_reads':0,'denial_probes':0}
    def save():
        tmp=root/'status'/(role+'.next');tmp.write_text(json.dumps(state));os.replace(tmp,root/'status'/(role+'.json'))
    probe=False
    def audit(event,args):
        if event=='open' and forbidden(root,role,args[0]):
            state['denial_probes' if probe else 'forbidden_reads']+=1
            raise PermissionError('role_input_denied')
        if event=='import' and role!='impl' and args[0] in ('kb_retrieval_candidates','rt055_run_a','rt055_run_b'):
            state['forbidden_reads']+=1;raise PermissionError('candidate_import_denied')
    save();sys.addaudithook(audit)
    probe=True
    target=root/('verifier/private-verified.json' if role=='builder' else 'builder/rt055_builder.py' if role=='verifier' else 'builder/private-candidates.json')
    try:target.read_bytes();raise RuntimeError('denied_read_probe_failed')
    except PermissionError:pass
    probe=False
    filename={'builder':'rt055_builder.py','verifier':'rt055_verifier.py','impl':'rt055_prepare.py'}[role]
    sys.argv=[str(folder/filename)]+(['--mode','verify-cases'] if role=='verifier' else [])
    code=2
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        try:runpy.run_path(sys.argv[0],run_name='__main__');code=0
        except SystemExit as e:code=e.code if isinstance(e.code,int) else 2
        except Exception as e:state['error_kind']=type(e).__name__
    state.update(status='PASS' if code==0 else 'FAIL',exit_code=code,finished_at=time.time());save();return code

if __name__=='__main__':raise SystemExit(main())
