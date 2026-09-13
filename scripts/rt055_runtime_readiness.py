#!/usr/bin/env python3
"""OPS-private Amendment 5: exclusive main-root policy preparation before before.

Only public TCP probes run here, never candidate launches or holdout parsing.
Legacy policies and receipts remain untouched. A window selects its own version.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import time
import rt055_opslib as ops
import rt055_window as window
import rt055_runtime as runtime

SCHEMA='cwk.rt055.runtime-policy-readiness.v1'
KINDS={'loopback':'network.sb','inbound-only':'search-network.sb'}
NETWORK={'passed':True,'external_denied':True,'loopback_allowed':True,
         'policy_observations':{'loopback':{'external':'DENIED','loopback':'CONNECTED'},
                                'inbound-only':{'external':'DENIED','loopback':'DENIED'}}}


def need(value):
    if not value:raise RuntimeError('formal_runtime_policy_readiness_invalid')


def main_root(root):
    root=Path(root)
    window.identifier(root.name.removeprefix('rt055-'))
    need(root.name.startswith('rt055-'))
    window.checked_path(root,'.rt055-owned')
    need((root/'.rt055-owned').read_text().strip()==root.name.removeprefix('rt055-'))
    # Synthetic roots cannot assert readiness for their protected parent.
    need(not (root/'audit/protected-root.json').exists())
    return root


def directory(root,wid):
    main_root(root);window.identifier(wid)
    return window.checked_path(root,'runtime-policy-versions',wid)


def deployment(root,mid):
    p=window.checked_path(root,'executioner-migrations',window.identifier(mid),'deployment.json')
    v=ops.read_json(p)
    need(v.get('schema')=='cwk.rt055.executioner-deployment.v1'
         and v.get('run_id')==root.name.removeprefix('rt055-') and v.get('migration_id')==mid
         and re.fullmatch('[0-9a-f]{40}',v.get('code_commit',''))
         and set(v.get('source_files',{}))==set(runtime.MIGRATION_SOURCE_FILES))
    need(all(ops.sha_file(window.checked_path(root,'impl',n))==h for n,h in v['source_files'].items()))
    return p,v


def artifacts(root,wid,mid):
    base=directory(root,wid);dp,dep=deployment(root,mid)
    paths=[base/'claim.json',base/'network-gate.json',dp]
    for kind,name in KINDS.items():
        p=window.checked_path(root,str((base/name).relative_to(root)))
        need(p.is_file() and p.read_text()==runtime.sandbox_text(root,kind))
        paths.append(p)
    for p in paths:window.checked_path(root,str(p.relative_to(root)))
    need(ops.read_json(base/'network-gate.json')==NETWORK)
    claim=ops.read_json(base/'claim.json')
    need(claim=={**window.envelope(root,wid,'runtime-policy-readiness'),'migration_id':mid})
    return dep,{str(p.relative_to(root)):ops.sha_file(p) for p in paths}


def verify(root,wid,mid=None):
    base=directory(root,wid)
    window.checked_path(root,str((base/'receipt.json').relative_to(root)))
    row=ops.read_json(base/'receipt.json');mid=mid or row['migration_id']
    dep,files=artifacts(root,wid,mid)
    expected={'schema':SCHEMA,**window.envelope(root,wid,'runtime-policy-readiness'),
              'migration_id':mid,'source_commit':dep['code_commit'],
              'source_files':dep['source_files'],'root':str(root.resolve()),
              'protected_roots':[str(root.resolve())], 'protected_scopes':list(runtime.PROTECTED_SCOPES),
              'files':files,'status':'PASS','probe_class':'PUBLIC_SOCKET_ONLY_NO_CANDIDATE',
              'created_at':row.get('created_at'),'ready_at':row.get('ready_at')}
    need(row==expected)
    need(type(row['created_at']) in (int,float) and type(row['ready_at']) in (int,float)
         and 0<row['created_at']<=row['ready_at']<=time.time())
    return row


def freeze_files(root,wid,mid,before_started):
    row=verify(root,wid,mid)
    need(row['ready_at']<before_started)
    from rt055_candidate_startup import verify as verify_workspace
    workspace=verify_workspace(root,wid,mid)
    need(workspace['finished_at']<before_started)
    return tuple(sorted((*row['files'],str((directory(root,wid)/'receipt.json').relative_to(root)),
                         str((directory(root,wid)/'workspace-readiness.json').relative_to(root)))))


def prepare(root,wid,mid):
    base=directory(root,wid);w=window.directory(root,wid)
    window.require_open(root,wid)
    need(not w.exists())
    need(window.holdout_unexposed(root))
    deployment(root,mid)
    need(runtime.privacy_passed(root,mid))
    # Exclusive directory AND claim: a failed probe is never silently retried.
    base.parent.mkdir(mode=0o700,exist_ok=True)
    base.mkdir(mode=0o700,exist_ok=False)
    start=time.time()
    window.write_once(base/'claim.json',{**window.envelope(root,wid,'runtime-policy-readiness'),'migration_id':mid})
    runtime.network_probe(root,policy_directory=base,gate_path=base/'network-gate.json')
    dep,files=artifacts(root,wid,mid)
    row={'schema':SCHEMA,**window.envelope(root,wid,'runtime-policy-readiness'),
         'migration_id':mid,'source_commit':dep['code_commit'],'source_files':dep['source_files'],
         'root':str(root.resolve()),'protected_roots':[str(root.resolve())],
         'protected_scopes':list(runtime.PROTECTED_SCOPES),'files':files,'status':'PASS',
         'probe_class':'PUBLIC_SOCKET_ONLY_NO_CANDIDATE','created_at':start,'ready_at':time.time()}
    window.write_once(base/'receipt.json',row)
    return verify(root,wid,mid)


def spawn_profile(root,wid,kind):
    need(kind in KINDS)
    window.require_open(root,wid);window.verification(root,wid)
    frozen=window.receipt(root,wid);mid=frozen['privacy_migration_id']
    _,bs,_=window.baseline(root,wid,'before')
    paths=freeze_files(root,wid,mid,bs['started_at'])
    need(all(frozen.get('private_files',{}).get(p)==ops.sha_file(root/p) for p in paths))
    return directory(root,wid)/KINDS[kind]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window-id',required=True);parser.add_argument('--migration-id',required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parent.parent
    prepare(root,args.window_id,args.migration_id)
    return 0

if __name__=='__main__':raise SystemExit(main())
