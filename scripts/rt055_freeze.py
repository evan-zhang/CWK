#!/usr/bin/env python3
"""OPS freeze creator/re-reader. Candidate and private digests never conflated."""
from __future__ import annotations
import argparse
import inspect
from pathlib import Path
import secrets
import subprocess
import sys
import time
import rt055_opslib as ops
import rt055_tiers as tiers
PINNED='8d7298fb5d759973cb1e481cadc5ecdf16dca599'
SHARED=('kb_retrieval_candidates.py','kb_retrieval_decision.py','kb_stage_b_poc.py','kb_stage_b_opensearch_benchmark.py','rt055_runtime.py','rt055_opslib.py','rt055_tiers.py')

def upstream(root):
    def git(*args):return subprocess.check_output(['git','-C',str(root/'weknora'),*args],stderr=subprocess.DEVNULL,text=True).strip()
    clean=not git('status','--porcelain');head=git('rev-parse','HEAD')==PINNED
    official=git('remote','get-url','origin') in ('https://github.com/Tencent/WeKnora.git','https://github.com/Tencent/WeKnora')
    reachable=subprocess.run(['git','-C',str(root/'weknora'),'merge-base','--is-ancestor',PINNED,'origin/main'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    return {'receipt_id':'ops-rt055-weknora-upstream','repository_id':'github.com/Tencent/WeKnora','commit':PINNED,'commit_reachable':reachable and official,'verified_on_ops':clean and head and reachable and official,'head_matches_commit':head,'tree_clean':clean}

def role_audit(root):
    rows={role:ops.read_json(root/'status'/(role+'.json')) for role in ('builder','verifier','impl')}
    ROLES = {'builder':'ops-rt055-builder', 'verifier':'ops-rt055-verifier',
             'impl':'cwk-rt055-candidate-implementer'}
    verified=(all(r['status']=='PASS' and r['role_id']==ROLES[role]
                  and (root/role).is_dir() and not (root/role).is_symlink()
                  and (root/role).stat().st_mode & 0o777 == 0o700
                  and (root/role).stat().st_uid == r['uid']
                  and r['workspace_mode']==0o700 and r['cwd']==str((root/role).resolve())
                  and r['workspace']==r['cwd'] and r['forbidden_reads']==0
                  and r['denial_probes']>=1 for role,r in rows.items())
              and len({r['process_id'] for r in rows.values()})==3
              and rows['builder']['finished_at']<=rows['verifier']['started_at'])
    uids={r['uid'] for r in rows.values()}
    level='PROCESS_LEVEL_SEPARATION_SINGLE_UID' if len(uids)==1 else 'OS_UID_SEPARATION' if len(uids)==3 else None
    return {'verified':verified and level is not None,'role_separation_level':level,'rows':rows}

def artifact_plan(root):
    import kb_retrieval_candidates as candidate
    from kb_stage_b_opensearch_benchmark import mapping_for
    a_config={'runtime':'opensearch-3.3.2','jdk':'21','bind':'loopback','security_plugin':False,'heap':'distribution-default','top_k':10,'warmup':6,'timeout_seconds':30,'empty_data_plane':True,'body_excluded':True}
    b_config={'commit':PINNED,'build_tags':'sqlite_fts5','db_driver':'sqlite','retrieve_driver':'sqlite','redis':False,'bind':'loopback','gin_mode':'release','log_level':'fatal','log_path':'devnull','langfuse':False,'otel':False,'model_egress':'loopback-only-sandbox','model':'BAAI-bge-m3','dimension':1024,'top_k':10,'warmup':6,'timeout_seconds':30,'empty_data_plane':True}
    parent={'mappings':{'dynamic':'strict','properties':{'tenant_id':{'type':'keyword'},'kb_id':{'type':'keyword'},'doc_id':{'type':'keyword'},'text':{'type':'text','index':False}}},'settings':{'number_of_shards':1,'number_of_replicas':0}}
    return {'a-config.json':a_config,'b-config.json':b_config,'a-mapping.json':{'child':mapping_for('analysis_icu',False),'parent':parent},'b-mapping.json':{'engine':'sqlite','vector_enabled':True,'keyword_enabled':True,'wiki_enabled':False,'graph_enabled':False},'a-query-plan.json':{'implementation':inspect.getsource(candidate.OpenSearchCandidate.search)},'b-query-plan.json':{'implementation':inspect.getsource(candidate.WeKnoraCandidate.search)}}

def dependencies(root,key):
    if key=='a':
        files=[p for base in ('jdk','opensearch/lib','opensearch/plugins','opensearch/modules') for p in (root/base).rglob('*') if p.is_file()]
    else:
        files=[root/'sidecar/requirements-freeze.txt']+[p for p in (root/'sidecar/hf').rglob('*') if p.is_file() and '.lock' not in p.name]
    return sorted(files)

def create(root):
    import os
    fd=os.open(root/'status/freeze.claim',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    checks=ops.read_json(root/'verifier/case-verification.json')
    if not checks['verified']:raise RuntimeError('cases_invalid')
    roles=role_audit(root)
    if not roles['verified']:raise RuntimeError('roles_invalid')
    if not ops.read_json(root/'audit/confidentiality-gate.json')['passed']:raise RuntimeError('confidentiality_invalid')
    if not ops.read_json(root/'status/baseline-before.json')['status']=='PASS':raise RuntimeError('before_missing')
    freeze=root/'freeze';freeze.mkdir(mode=0o700,exist_ok=True)
    for name,value in artifact_plan(root).items():ops.write_private_json(freeze/name,value)
    ops.write_private_json(freeze/'role-audit.json',roles)
    receipt={'schema':'cwk.rt055.ops-freeze.amendment3','run_id':root.name.removeprefix('rt055-'),'created_at':time.time(),'run_order':['a','b'] if secrets.randbits(1) else ['b','a'],
             'participating_libraries':checks['participating_libraries'],'deferred_libraries':checks['deferred_libraries'],
             'private_files':{p:ops.sha_file(root/p) for p in ('builder/private-corpus.json','verifier/private-verified.json','verifier/case-verification.json','audit/production-before.json')},'candidates':{}}
    for key in ('a','b'):
        extra=('rt055_run_a.py',) if key=='a' else ('rt055_run_b.py','rt055_embed_sidecar.py')
        code_files=['impl/'+f for f in SHARED+extra]
        image='downloads/opensearch.tar.gz' if key=='a' else 'bin/weknora-server'
        deps=dependencies(root,key)
        if not deps:raise RuntimeError('dependencies_empty')
        dependency_paths=[str(p.relative_to(root)) for p in deps]
        artifact_paths={'image_digest':image,'config_digest':f'freeze/{key}-config.json','mapping_digest':f'freeze/{key}-mapping.json','query_plan_digest':f'freeze/{key}-query-plan.json'}
        block={'candidate_id':'cwk-opensearch-dual-channel-v1' if key=='a' else 'weknora-native-'+PINNED,'receipt_id':'ops-rt055-freeze-candidate-'+key,'code_files':code_files,'artifact_paths':artifact_paths,'dependency_paths':dependency_paths,'frozen_before_run':True,'digests':{'code_digest':ops.file_manifest([root/p for p in code_files],base=root),**{k:ops.sha_file(root/p) for k,p in artifact_paths.items()},'dependency_digest':ops.file_manifest(deps+[root/'impl/rt055_runbooks.json'],base=root)}}
        if key=='b':block.update(upstream_receipt=upstream(root),native_config=True,core_modified=False)
        receipt['candidates'][key]=block
    ops.write_private_json(freeze/'freeze-receipt.json',receipt)

def verify_artifacts(root):
    receipt=ops.read_json(root/'freeze/freeze-receipt.json')
    if receipt.get('run_order') not in (['a','b'],['b','a']):return False
    required_private = {'builder/private-corpus.json', 'verifier/private-verified.json',
                        'verifier/case-verification.json', 'audit/production-before.json'}
    if (set(receipt.get('private_files', {})) != required_private
            or set(receipt.get('candidates', {})) != {'a', 'b'}):return False
    checks = ops.read_json(root/'verifier/case-verification.json')
    if (checks.get('verified') is not True or not checks.get('participating_libraries')
            or receipt.get('participating_libraries') != checks['participating_libraries']
            or receipt.get('deferred_libraries') != checks['deferred_libraries']):return False
    for key, block in receipt['candidates'].items():
        extra = ('rt055_run_a.py',) if key == 'a' else ('rt055_run_b.py', 'rt055_embed_sidecar.py')
        if block.get('code_files') != ['impl/'+f for f in SHARED+extra]:return False
        artifact_paths = {'image_digest': 'downloads/opensearch.tar.gz' if key == 'a' else 'bin/weknora-server',
                          'config_digest': f'freeze/{key}-config.json',
                          'mapping_digest': f'freeze/{key}-mapping.json',
                          'query_plan_digest': f'freeze/{key}-query-plan.json'}
        if block.get('artifact_paths') != artifact_paths:return False
        expected_deps = [str(p.relative_to(root)) for p in dependencies(root,key)]
        if not expected_deps or block.get('dependency_paths') != expected_deps:return False
        if set(block.get('digests', {})) != set(artifact_paths) | {'code_digest','dependency_digest'}:return False
        if block.get('frozen_before_run') is not True:return False
    if not all(ops.sha_file(root/p)==h for p,h in receipt['private_files'].items()):return False
    if not all(ops.read_json(root/'freeze'/p)==v for p,v in artifact_plan(root).items()):return False
    if not role_audit(root)['verified']:return False
    upstream_checked=upstream(root)
    if not upstream_checked['verified_on_ops']:return False
    for key,block in receipt['candidates'].items():
        d=block['digests']
        if d['code_digest']!=ops.file_manifest([root/p for p in block['code_files']],base=root):return False
        if any(d[k]!=ops.sha_file(root/p) for k,p in block['artifact_paths'].items()):return False
        if d['dependency_digest']!=ops.file_manifest([root/p for p in block['dependency_paths']]+[root/'impl/rt055_runbooks.json'],base=root):return False
    return True

def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('create','verify'));args=parser.parse_args()
    root=Path(__file__).resolve().parent.parent
    if args.mode=='create':create(root);return 0
    result=verify_artifacts(root)
    ops.write_private_json(root/'verifier/freeze-verification.json',{'verified':result,'verified_before_run':not (root/'consumption').exists(),'process_id':__import__('os').getpid(),'observed_at':time.time()})
    return 0 if result else 3
if __name__=='__main__':raise SystemExit(main())
