"""Immutable formal-window identity and append-only private artifacts.

Legacy generic paths are never a fallback for a formal window. All hashes in
this module remain on OPS; only the random window/migration IDs may be exported.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
import uuid
import rt055_opslib as ops


def identifier(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value or uuid.UUID(value).version!=4:
        raise ValueError('explicit_random_uuid_required')
    return value


def directory(root,window_id):
    identifier(window_id)
    base=root/'formal-windows';dest=base/window_id
    if root.is_symlink() or base.is_symlink() or dest.is_symlink():
        raise RuntimeError('window_symlink')
    return dest


def write_once(path,value):
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('artifact_symlink')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,sort_keys=True,ensure_ascii=False,allow_nan=False)
        f.write('\n');f.flush();os.fsync(f.fileno())


def envelope(root,window_id,mode):
    return {'run_id':root.name.removeprefix('rt055-'),'window_id':identifier(window_id),'mode':mode}


def baseline(root,window_id,mode):
    if mode not in ('before','after'):raise ValueError('baseline_mode_invalid')
    w=directory(root,window_id)
    status_path=w/'status'/f'baseline-{mode}.json'
    artifact=w/'audit'/f'production-{mode}.json'
    claim=w/'status'/f'baseline-{mode}.claim'
    for p in (status_path,artifact,claim):
        if p.is_symlink() or not p.is_file():raise RuntimeError('window_baseline_missing')
    status=ops.read_json(status_path);value=ops.read_json(artifact);claimed=ops.read_json(claim)
    expected=envelope(root,window_id,mode)
    if any(any(row.get(k)!=v for k,v in expected.items()) for row in (status,value,claimed)):
        raise RuntimeError('window_baseline_identity_invalid')
    if (status.get('status')!='PASS' or status.get('artifact_sha256')!=ops.sha_file(artifact)
            or status.get('artifact')!=str(artifact.relative_to(root))
            or value.get('observed_at',0)<=0 or status.get('finished_at',0)<value['observed_at']):
        raise RuntimeError('window_baseline_not_verified')
    return value,status,artifact


def comparison(root,window_id):
    before,_,bp=baseline(root,window_id,'before');after,_,ap=baseline(root,window_id,'after')
    p=directory(root,window_id)/'audit/production-comparison.json'
    value=ops.read_json(p)
    if (any(value.get(k)!=v for k,v in envelope(root,window_id,'comparison').items())
            or value.get('before_sha256')!=ops.sha_file(bp) or value.get('after_sha256')!=ops.sha_file(ap)
            or after['observed_at']<before['observed_at']):
        raise RuntimeError('window_comparison_invalid')
    from rt055_baseline import compare_values
    expected={**compare_values(before,after),**envelope(root,window_id,'comparison'),
              'before_sha256':ops.sha_file(bp),'after_sha256':ops.sha_file(ap)}
    if value!=expected:raise RuntimeError('window_comparison_recompute_failed')
    return value


def receipt(root,window_id):
    w=directory(root,window_id)
    r=ops.read_json(w/'freeze/freeze-receipt.json')
    if r.get('window_id')!=window_id or r.get('run_id')!=root.name.removeprefix('rt055-'):
        raise RuntimeError('freeze_window_mismatch')
    return r


def verification(root,window_id):
    r=receipt(root,window_id);w=directory(root,window_id)
    v=ops.read_json(w/'verifier/freeze-verification.json')
    if (v.get('window_id')!=window_id or v.get('run_id')!=r['run_id']
            or v.get('verified') is not True or v.get('verified_before_run') is not True
            or v.get('receipt_sha256')!=ops.sha_file(w/'freeze/freeze-receipt.json')):
        raise RuntimeError('freeze_verification_binding_invalid')
    return v


def library_claim(root,window_id,key,kb):
    if key not in ('a','b') or kb not in ops.LIBRARIES:raise ValueError('consumption_identity_invalid')
    verification(root,window_id)
    # Global run scope: another window can never consume this candidate/library.
    p=root/'consumption'/key/(kb+'.claim')
    write_once(p,{**envelope(root,window_id,'consumption'),'candidate':key,'library':kb,'pid':os.getpid(),'time':time.time(),
                  'receipt_sha256':ops.sha_file(directory(root,window_id)/'freeze/freeze-receipt.json')})


def library_scored(root,window_id,key,kb,metrics):
    claim=ops.read_json(root/'consumption'/key/(kb+'.claim'))
    if claim.get('window_id')!=window_id:raise RuntimeError('consumption_window_mismatch')
    write_once(directory(root,window_id)/('run-'+key)/'scores'/(kb+'.json'),
               {**envelope(root,window_id,'scored'),'candidate':key,'library':kb,'metrics':metrics,'scored_at':time.time()})


def library_complete(root,window_id,key,kb,value):
    claim=ops.read_json(root/'consumption'/key/(kb+'.claim'))
    if claim.get('window_id')!=window_id:raise RuntimeError('consumption_window_mismatch')
    write_once(directory(root,window_id)/('run-'+key)/(kb+'.json'),
               {**value,**envelope(root,window_id,'library-result'),'candidate':key,'library':kb,'completed_at':time.time()})


def library_result(root,window_id,key,kb):
    p=directory(root,window_id)/('run-'+key)/(kb+'.json')
    if not p.exists():
        if (root/'consumption'/key/(kb+'.claim')).exists():raise RuntimeError('consumption_without_completion_no_replay')
        return None
    v=ops.read_json(p);c=ops.read_json(root/'consumption'/key/(kb+'.claim'))
    if (any(v.get(k)!=x for k,x in envelope(root,window_id,'library-result').items())
            or any(c.get(k)!=x for k,x in envelope(root,window_id,'consumption').items())
            or v.get('candidate')!=key or v.get('library')!=kb or c.get('candidate')!=key or c.get('library')!=kb
            or c.get('receipt_sha256')!=ops.sha_file(directory(root,window_id)/'freeze/freeze-receipt.json')
            or not (v.get('completed_at',0)>=c.get('time',0)>=verification(root,window_id)['observed_at'])):
        raise RuntimeError('library_result_identity_invalid')
    return v


def validate_run(root,window_id,key,participating):
    row=ops.read_json(directory(root,window_id)/('run-'+key)/'result.json')
    if (row.get('schema')!='cwk.rt055.run-'+key+'.result.v1' or row.get('mode')!='run'
            or row.get('status')!='OK' or row.get('window_id')!=window_id
            or set(row.get('libraries',{}))!=set(participating)):
        raise RuntimeError('formal_run_identity_invalid')
    saved=[library_result(root,window_id,key,kb) for kb in participating]
    for kb,v in zip(participating,saved):
        if v is None:raise RuntimeError('formal_run_consumption_binding_invalid')
        scored=ops.read_json(directory(root,window_id)/('run-'+key)/'scores'/(kb+'.json'))
        if (any(scored.get(k)!=x for k,x in envelope(root,window_id,'scored').items())
                or any(v['metrics'].get(k)!=x for k,x in scored['metrics'].items())
                or scored['scored_at']>v['completed_at']):
            raise RuntimeError('formal_score_completion_binding_invalid')
    if any(v is None or v['metrics']!=row['libraries'][kb] for kb,v in zip(participating,saved)):
        raise RuntimeError('formal_run_consumption_binding_invalid')
    gates=saved[0]['gateway_readiness']
    expected={k:all(v['gateway_readiness'][k] for v in saved) for k in gates}
    if row.get('gateway_readiness')!=expected:raise RuntimeError('formal_run_gateway_binding_invalid')
    return row
