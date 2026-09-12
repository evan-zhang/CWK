#!/usr/bin/env python3
"""Public full A/B startup on main window policy BEFORE before, never scoring.

No corpus/case parsing, candidate attempt claims, arms, exposure, or query calls.
Only the parent owner binds the observed terminal receipt into a later freeze.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import random
import sys
import time
import uuid
import rt055_log_firewall as firewall
import rt055_opslib as ops
import rt055_window as window
import rt055_candidate_workspace as cw
import rt055_runtime as runtime
import rt055_runtime_readiness as ready

SCHEMA='cwk.rt055.candidate-startup-readiness.v1'


def verify(root,wid,mid):
    row=ops.read_json(ready.directory(root,wid)/'workspace-readiness.json')
    policy=ready.verify(root,wid,mid)
    expected={'schema':SCHEMA,**window.envelope(root,wid,'candidate-startup-readiness'),
              'migration_id':mid,'source_commit':policy['source_commit'],'source_files':policy['source_files'],
              'policy_files':policy['files'],'policy_receipt_sha256':ops.sha_file(ready.directory(root,wid)/'receipt.json'),
              'status':'PASS','a_started':3,'b_started':3,'sidecars_started':3,'private_reads':0,
              'formal_attempts':0,'formal_queries':0,'cleanup_failures':0,'remaining_runtime':0,
              'started_at':row.get('started_at'),'finished_at':row.get('finished_at'),'leases':row.get('leases')}
    if (row!=expected or not 0<row['started_at']<=row['finished_at']<=time.time()
            or not isinstance(row['leases'],list) or len(row['leases'])!=2):raise RuntimeError('candidate_startup_readiness_invalid')
    keys=[]
    for item in row['leases']:
        if set(item)!= {'candidate','attempt_id','owner_sha256','scan_sha256','cleanup_sha256'}:raise RuntimeError('candidate_startup_lease_invalid')
        s=cw.Workspace(root,wid,item['candidate'],item['attempt_id'],True,mid);cw._identity(s)
        for key,name in [('owner_sha256','workspace.json'),('scan_sha256','log-scan.json'),('cleanup_sha256','workspace-cleanup.json')]:
            if ops.sha_file(s.ledger.parent/name)!=item[key]:raise RuntimeError('candidate_startup_receipt_drift')
        owner=ops.read_json(s.ledger);clean=ops.read_json(s.ledger.parent/'workspace-cleanup.json');scan=ops.read_json(s.ledger.parent/'log-scan.json')
        if any(owner.get(k)!=v or clean.get(k)!=v for k,v in s.identity().items()):raise RuntimeError('candidate_startup_identity_drift')
        if scan.get('passed') is not True or scan.get('hit_files')!=0 or not clean.get('complete') or clean.get('remaining_owned_runtime')!=0 or s.base.exists():raise RuntimeError('candidate_startup_cleanup_invalid')
        keys.append(s.candidate)
    if sorted(keys)!=['a','b']:raise RuntimeError('candidate_startup_candidates_invalid')
    return row


def run(root,wid,mid):
    import rt055_run_a as a
    import rt055_run_b as b
    from rt055_confidentiality import free_port,load_private_bank
    os.umask(0o077);policy=ready.verify(root,wid,mid)
    if window.directory(root,wid).exists():raise RuntimeError('candidate_startup_after_before')
    base=ready.directory(root,wid)
    window.write_once(base/'workspace-startup.claim',window.envelope(root,wid,'public-startup'))
    state={'schema':SCHEMA,**window.envelope(root,wid,'candidate-startup-readiness'),
           'migration_id':mid,'source_commit':policy['source_commit'],'source_files':policy['source_files'],
           'policy_files':policy['files'],'policy_receipt_sha256':ops.sha_file(base/'receipt.json'),
           'status':'RUNNING','a_started':0,'b_started':0,'sidecars_started':0,'private_reads':0,
           'formal_attempts':0,'formal_queries':0,'cleanup_failures':0,'remaining_runtime':0,
           'started_at':time.time(),'leases':[]}
    bank,_=load_private_bank(root)
    values=list(bank.values)+['RT055_PUBLIC_STARTUP_NEEDLE_NOT_IN_LOG']
    def deny_private(event,args):
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            p=Path(os.path.realpath(os.fsdecode(args[0])))
            if any(p.is_relative_to(root/scope) for scope in runtime.PROTECTED_SCOPES if scope!='runtime-policy-versions'):
                state['private_reads']+=1;raise PermissionError('public_startup_private_read_denied')
    sys.addaudithook(deny_private)
    spaces=[];processes=[]
    a.ROOT=b.ROOT=root;a.HERE=b.HERE=root/'impl'
    error=None
    try:
        for key in ('a','b'):
            s=cw.create(root,wid,key,str(uuid.uuid4()),synthetic=True,migration_id=mid);spaces.append(s)
            firewall.bind(s,values)
            for kb in ops.LIBRARIES:
                if key=='a':
                    proc,info=a.launch_opensearch(kb,free_port(),cw.file(s,'logs','opensearch-'+kb+'.log'),'smoke',workspace=s)
                    processes.append(proc);a.verify_icu(info['base_url']);state['a_started']+=1
                else:
                    port=free_port();proc=b.launch_sidecar(port,root/'sidecar/hf',cw.file(s,'logs','sidecar-'+kb+'.log'),workspace=s)
                    processes.append(proc);state['sidecars_started']+=1
                    server=b.setup_instance(kb,free_port(),port,cw.file(s,'data','b-'+kb),cw.file(s,'logs','native-'+kb+'.log'),random.Random(),workspace=s)
                    processes.append(server['proc']);state['b_started']+=1
            for proc in reversed(processes):ops.stop_process(proc)
            processes=[]
    except Exception as exc:error=type(exc).__name__
    finally:
        for proc in reversed(processes):ops.stop_process(proc)
        for s in spaces:
            try:firewall.finalize(s);cw.scan(s,values)
            except Exception:state['cleanup_failures']+=1
            try:cw.cleanup(s)
            except Exception:state['cleanup_failures']+=1
            state['remaining_runtime']+=int(s.base.exists())
            if all((s.ledger.parent/n).is_file() for n in ('workspace.json','log-scan.json','workspace-cleanup.json')):
                state['leases'].append({'candidate':s.candidate,'attempt_id':s.attempt_id,
                     'owner_sha256':ops.sha_file(s.ledger),'scan_sha256':ops.sha_file(s.ledger.parent/'log-scan.json'),
                     'cleanup_sha256':ops.sha_file(s.ledger.parent/'workspace-cleanup.json')})
        state['finished_at']=time.time();state['status']='PASS' if not error else 'FAILED'
        if error:state['error_kind']=error
        window.write_once(base/'workspace-startup-terminal.json',state)
        if not error and state['a_started']==state['b_started']==state['sidecars_started']==3 and not state['cleanup_failures'] and not state['private_reads'] and not state['remaining_runtime']:
            window.write_once(base/'workspace-readiness.json',state);verify(root,wid,mid)
        else:raise RuntimeError('candidate_startup_readiness_failed')
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--window-id',required=True);parser.add_argument('--migration-id',required=True)
    args=parser.parse_args();run(Path(__file__).resolve().parent.parent,args.window_id,args.migration_id);return 0
if __name__=='__main__':raise SystemExit(main())
