"""OPS-only Amendment 4 void verifier. No holdout parsing or candidate queries.

Only the known, frozen unconditional prequery defect is eligible. Archive and
hash original evidence before deployment; preserve every old claim/window byte.
A void is not a quality judgement, and an exposure is never voidable.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import rt055_opslib as ops
import rt055_window as window

REASON='PER_LIBRARY_RUNNER_VS_ALL_LIBRARY_SCORER_PREQUERY_CONFLICT'
BASIS='FROZEN_CONTROL_FLOW_UNCONDITIONAL_PREQUERY_REJECTION_FOR_SINGLE_LIBRARY'
# Public source digests from 1af1362, not private input/evidence digests.
LEGACY_PUBLIC_SOURCES={
    'kb_retrieval_candidates.py':'523c6d8e300acf6d54ea16719ff71f2ee52a9e5faf3952e0ac46c3d3549234ba',
    'rt055_run_a.py':'64c8cf093ac57a92be0eb1a9648cdb7eb3a23187a13cba2c6563434e2fa98c8b',
    'rt055_run_b.py':'e8ac4f930c181cccdbe7a257db6c32134a7d0b240e05fcfa564c9133f9916c13'}


def _need(condition):
    if not condition:raise RuntimeError('zero_exposure_evidence_invalid_no_replay')


def _checked_path(root,*parts):
    p=root.joinpath(*parts)
    for node in (p,*p.parents):
        _need(not node.is_symlink())
        if node==root:break
    _need(p.is_relative_to(root))
    return p


def _write_once(path,value):
    window.write_once(path,value)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _path(root,relative):
    _need(isinstance(relative,str) and relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts)
    return _checked_path(root,relative)


def _dependency_path(root,relative):
    # HuggingFace snapshot files are links to immutable local blobs. Preserve
    # their original relative identity and byte hash, but permit no cache escape.
    p=root/relative
    if (isinstance(relative,str) and relative.startswith('sidecar/hf/')
            and not Path(relative).is_absolute() and '..' not in Path(relative).parts
            and p.is_symlink()):
        cache=_checked_path(root,'sidecar','hf')
        _checked_path(root,str(p.parent.relative_to(root)))
        resolved=p.resolve(strict=True)
        _need(resolved.is_relative_to(cache) and resolved.is_file())
        return p
    return _path(root,relative)


def _files(base):
    paths=[]
    for p in base.rglob('*'):
        _need(not p.is_symlink())
        if p.is_file():paths.append(p)
    return sorted(paths)


def _counts(root,wid):
    w=window.directory(root,wid)
    return {key:{'claims':len(_files(root/'consumption'/key)),
                 'attempts':len(list((w/('run-'+key)/'attempts').glob('*/claim.json'))),
                 'scores':len(_files(w/('run-'+key)/'scores')),
                 'results':len(list((w/('run-'+key)).glob('*.json')))} for key in ('a','b')}


def _no_processes(root):
    lines=subprocess.check_output(['/bin/ps','-axo','pid=,command='],text=True).splitlines()
    terms=('org.opensearch.bootstrap.OpenSearch','weknora-server','rt055_embed_sidecar.py',
           'rt055_formal_coordinator.py','rt055_run_a.py --mode run','rt055_run_b.py --mode run')
    return not any(str(root) in line and any(x in line for x in terms) for line in lines)


def _raw_report(value):
    _need(value.get('reason')==REASON and value.get('zero_calls_basis')==BASIS
          and value.get('window_decision')=='INVALID'
          and type(value.get('formal_query_calls')) is int and value['formal_query_calls']==0
          and type(value.get('formal_score_receipts')) is int and value['formal_score_receipts']==0
          and value.get('private_holdout_reopened_for_diagnosis') is False
          and value.get('freeze_source_recomputed') is True
          and value.get('privacy_source_binding_recomputed') is True
          and value.get('candidate_attempts')=={'a':1,'b':0}
          and value.get('consumption_claims')=={'a':1,'b':0})


def _reconciliation(value):
    _need(value.get('classification')==REASON and value.get('zero_calls_basis')==BASIS
          and value.get('actual_private_holdout_reopened_for_probe') is False
          and type(value.get('formal_candidate_search_calls')) is int and value['formal_candidate_search_calls']==0
          and type(value.get('score_receipts')) is int and value['score_receipts']==0
          and value.get('frozen_source_recomputed') is True and value.get('privacy_binding_recomputed') is True
          and value.get('candidate_b_started') is False and value.get('candidate_processes_remaining')==0)


def _proof(archive):
    # Reproduce only public cases in a separate interpreter. No holdout can open.
    program='''import sys,json
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root/'impl'))
import kb_retrieval_candidates as c
forbidden=0
def audit(event,args):
 global forbidden
 if event=='open' and isinstance(args[0],(str,bytes)):
  p=Path(args[0]).resolve()
  if any(p.is_relative_to(root/n) for n in ('builder','verifier','consumption')):
   forbidden+=1;raise PermissionError('private_read_denied')
sys.addaudithook(audit)
rows=[]
for kb in c.decision.LIBRARIES:
 class Candidate:
  calls=0
  def search(self,*a,**kw):self.calls+=1;raise RuntimeError('unreachable')
 candidate=Candidate();failure=None
 try:c.score_cases(candidate,[c.Case(kb,'public exact',frozenset({'public-doc'}),True),c.Case(kb,'public no answer',frozenset())])
 except Exception as e:failure=type(e).__name__
 rows.append({'calls':candidate.calls,'failure':failure})
print(json.dumps({'rows':rows,'forbidden_reads':forbidden}))
'''
    env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1','LANG':'C','LC_ALL':'C'}
    p=subprocess.run([sys.executable,'-c',program,str(archive)],env=env,capture_output=True,text=True,timeout=30)
    _need(p.returncode==0)
    value=json.loads(p.stdout)
    _need(value=={'rows':[{'calls':0,'failure':'CandidateError'}]*3,'forbidden_reads':0})
    return value


def capture(root,wid,migration_id):
    """Call with old deployed source still intact; writes only a new OPS archive."""
    import rt055_freeze as freeze
    import rt055_runtime as runtime
    window.identifier(migration_id);w=window.directory(root,wid)
    _need(_no_processes(root) and not any((root/n).exists() for n in ('data-run','data-smoke')))
    _need(freeze.verify_artifacts(root,wid) and runtime.privacy_passed(root,window.receipt(root,wid)['privacy_migration_id']))
    _need(ops.read_json(w/'status/formal.json')['status']=='INVALID')
    window.baseline(root,wid,'after');window.comparison(root,wid)
    counts=_counts(root,wid)
    _need(counts=={'a':{'claims':1,'attempts':1,'scores':0,'results':0},'b':{'claims':0,'attempts':0,'scores':0,'results':0}})
    _need(not _files(root/'exposure'))
    claims=list((root/'consumption/a').glob('*.claim'));_need(len(claims)==1)
    claim_path=claims[0];claim=ops.read_json(claim_path);kb=claim.get('library')
    _need(kb in ops.LIBRARIES and claim_path.name==kb+'.claim' and claim.get('candidate')=='a' and claim.get('window_id')==wid)
    attempts=list((w/'run-a/attempts').glob('*/claim.json'));attempt=attempts[0].parent.name
    _need(ops.read_json(attempts[0].parent/'failure.json').get('error')=='CandidateError')
    reports=[p for p in (w/'audit').glob('*.json') if ops.read_json(p).get('reason')==REASON and 'formal_query_calls' in ops.read_json(p)]
    reconciliations=[p for p in (w/'audit').glob('*.json') if ops.read_json(p).get('classification')==REASON and 'formal_candidate_search_calls' in ops.read_json(p)]
    _need(len(reports)==len(reconciliations)==1)
    _raw_report(ops.read_json(reports[0]));_reconciliation(ops.read_json(reconciliations[0]))
    retained=[]
    for p in (root/'audit').glob('*.json'):
        v=ops.read_json(p)
        if isinstance(v,dict) and len(v)==96 and all(isinstance(h,str) and len(h)==64 for h in v.values()):retained.append(p)
    _need(len(retained)==1)
    materials=ops.read_json(retained[0]);_need(all(ops.sha_file(_path(root,p))==h for p,h in materials.items()))
    m=_checked_path(root,'zero-exposure-migrations',migration_id);m.mkdir(parents=True,mode=0o700,exist_ok=False)
    receipt=window.receipt(root,wid);mid=receipt['privacy_migration_id'];pm=runtime.migration_directory(root,mid)
    binding=ops.read_json(pm/'privacy-receipt.json');deployment=ops.read_json(pm/'deployment.json')
    selected=set(_files(w)+_files(root/'consumption'))
    selected.update(_path(root,p) for p in materials)
    selected.update(_path(root,p) for p in receipt['private_files'])
    selected.update(root/'impl'/n for n in deployment['source_files'])
    selected.update([retained[0],pm/'deployment.json',pm/'privacy-receipt.json',root/'audit/confidentiality-recovery.json',root/'impl/rt055_runbooks.json'])
    t=pm/('rt055-synthetic-%03d'%binding['attempt'])
    selected.update(t/'impl'/n for n in deployment['source_files'])
    selected.update(t/p for p in ('audit/confidentiality-observations.json','status/confidentiality.json','audit/synthetic-cleanup.json'))
    selected.update(root/'status'/(role+'.json') for role in ('builder','verifier','impl'))
    manifest={}
    for p in sorted(selected):
        relative=str(p.relative_to(root));dest=_path(m/'archive',relative);dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with dest.open('xb') as f:f.write(p.read_bytes());f.flush();os.fsync(f.fileno())
        os.chmod(dest,0o600);manifest[relative]=ops.sha_file(p)
        _need(ops.sha_file(dest)==manifest[relative])
    value={'schema':'cwk.rt055.zero-exposure-private-evidence.v1','run_id':receipt['run_id'],
           'old_window_id':wid,'migration_id':migration_id,'candidate':'a','library':kb,'attempt_id':attempt,
           'claim_path':str(claim_path.relative_to(root)),'report_path':str(reports[0].relative_to(root)),
           'reconciliation_path':str(reconciliations[0].relative_to(root)),
           'retention_path':str(retained[0].relative_to(root)),'archive_files':manifest,
           'proof':_proof(m/'archive'),'observed_counts':counts,'created_at':time.time()}
    _write_once(m/'evidence.json',value)
    validate_evidence(root,m,value,live_source=True)
    return m


def validate_evidence(root,m,e,live_source=False):
    from rt055_confidentiality import evaluate
    _need(e.get('schema')=='cwk.rt055.zero-exposure-private-evidence.v1' and e.get('run_id')==root.name.removeprefix('rt055-'))
    wid=e['old_window_id'];w=window.directory(root,wid);a=m/'archive';aw=window.directory(a,wid)
    _need(e['candidate']=='a' and e['library'] in ops.LIBRARIES and e['migration_id']==m.name)
    _need(e['observed_counts']=={'a':{'claims':1,'attempts':1,'scores':0,'results':0},'b':{'claims':0,'attempts':0,'scores':0,'results':0}})
    _need(e['proof']=={'rows':[{'calls':0,'failure':'CandidateError'}]*3,'forbidden_reads':0})
    _need(_counts(root,wid)==e['observed_counts'])
    # Exposure can NEVER be reclassified by a void. This check is for the old
    # window; later replacement exposure remains globally irreversible.
    for p in _files(root/'exposure'):
        row=ops.read_json(p);_need(row.get('window_id')!=wid)
    for relative,h in e['archive_files'].items():
        archived=_path(a,relative);_need(archived.is_file() and ops.sha_file(archived)==h)
        # Public impl changes are the purpose of migration. All historical
        # windows, claims, private bytes and old privacy evidence remain exact.
        if live_source or not relative.startswith('impl/'):
            _need(ops.sha_file(_path(root,relative))==h)
    for name,digest in LEGACY_PUBLIC_SOURCES.items():_need(ops.sha_file(a/'impl'/name)==digest)
    _need(_proof(a)==e['proof'])
    report=ops.read_json(_path(a,e['report_path']));_raw_report(report)
    recon=ops.read_json(_path(a,e['reconciliation_path']));_reconciliation(recon)
    _need(report.get('window_id')==wid and recon.get('window_id')==wid)
    claim=ops.read_json(_path(a,e['claim_path']));fr=ops.read_json(aw/'freeze/freeze-receipt.json')
    _need(e['claim_path']==str(Path('consumption/a')/(e['library']+'.claim')))
    _need(claim.get('candidate')=='a' and claim.get('library')==e['library'] and claim.get('window_id')==wid
          and claim.get('receipt_sha256')==ops.sha_file(aw/'freeze/freeze-receipt.json'))
    attempt=aw/'run-a/attempts'/window.identifier(e['attempt_id'])
    ac=ops.read_json(attempt/'claim.json');failure=ops.read_json(attempt/'failure.json')
    _need(ac.get('candidate')=='a' and ac.get('window_id')==wid and failure.get('error')=='CandidateError')
    _need(ac['time']<=claim['time'] and ops.read_json(w/'status/formal.json').get('status')=='INVALID')
    fv=ops.read_json(aw/'verifier/freeze-verification.json')
    _need(fv.get('verified') is True and fv.get('verified_before_run') is True
          and fv.get('receipt_sha256')==ops.sha_file(aw/'freeze/freeze-receipt.json'))
    _need(fr.get('window_id')==wid and fr.get('run_id')==e['run_id'])
    for p,h in fr['private_files'].items():_need(ops.sha_file(_path(a,p))==h and ops.sha_file(_path(root,p))==h)
    for block in fr['candidates'].values():
        _need(block['digests']['code_digest']==ops.file_manifest([_path(a,p) for p in block['code_files']],base=a))
        for k,p in block['artifact_paths'].items():
            source=_path(a,p) if p.startswith('formal-windows/') else _path(root,p)
            _need(ops.sha_file(source)==block['digests'][k])
        _need(block['digests']['dependency_digest']==ops.file_manifest([_dependency_path(root,p) for p in block['dependency_paths']]+[root/'impl/rt055_runbooks.json'],base=root))
    pm=Path('executioner-migrations')/fr['privacy_migration_id'];binding=ops.read_json(a/pm/'privacy-receipt.json')
    dep=ops.read_json(a/pm/'deployment.json');t=pm/('rt055-synthetic-%03d'%binding['attempt'])
    _need(dep['source_files']==binding['source_files'] and binding['deployment_sha256']==ops.sha_file(a/pm/'deployment.json'))
    for n,h in binding['source_files'].items():_need(ops.sha_file(a/'impl'/n)==h and ops.sha_file(a/t/'impl'/n)==h)
    for key,p in [('observations_sha256',t/'audit/confidentiality-observations.json'),('status_sha256',t/'status/confidentiality.json'),('cleanup_sha256',t/'audit/synthetic-cleanup.json'),('historical_recovery_sha256',Path('audit/confidentiality-recovery.json'))]:_need(binding[key]==ops.sha_file(a/p))
    obs=ops.read_json(a/t/'audit/confidentiality-observations.json')
    _need(evaluate(obs)['passed'] and not obs.get('cleanup_error') and 'execution_error_kind' not in obs)
    _need(ops.read_json(a/t/'status/confidentiality.json').get('status')=='PASS')
    _need(ops.read_json(a/t/'audit/synthetic-cleanup.json')=={'complete':True,'failures':0,'remaining_processes':0,'remaining_data_planes':0})
    materials=ops.read_json(_path(a,e['retention_path']));_need(len(materials)==96)
    _need(all(ops.sha_file(_path(root,p))==h for p,h in materials.items()))
    _need(len(list((root/'builder').glob('single-build-claim*')))==len(list((root/'verifier').glob('single-verify-claim*')))==1)
    return True


def append_void(root,m):
    e=ops.read_json(m/'evidence.json');validate_evidence(root,m,e,live_source=True)
    _need(_no_processes(root) and not _files(root/'exposure'))
    receipt={'schema':'cwk.rt055.zero-exposure-void.v1','status':'VOID_PREQUERY_NO_EXPOSURE',
             'run_id':e['run_id'],'old_window_id':e['old_window_id'],'candidate':e['candidate'],'library':e['library'],
             'attempt_id':e['attempt_id'],'migration_id':e['migration_id'],
             'claim_sha256':ops.sha_file(_path(root,e['claim_path'])),
             'freeze_sha256':ops.sha_file(window.directory(root,e['old_window_id'])/'freeze/freeze-receipt.json'),
             'evidence_sha256':ops.sha_file(m/'evidence.json'),'voided_at':time.time()}
    _write_once(_checked_path(root,'void-prequery',e['candidate'],e['library']+'.json'),receipt)
    validate_void(root,e['candidate'],e['library'])
    return receipt


def validate_void(root,key,kb):
    try:
        p=_checked_path(root,'void-prequery',key,kb+'.json');v=ops.read_json(p)
        _need(v.get('schema')=='cwk.rt055.zero-exposure-void.v1' and v.get('status')=='VOID_PREQUERY_NO_EXPOSURE'
              and v.get('candidate')==key and v.get('library')==kb and v.get('run_id')==root.name.removeprefix('rt055-'))
        m=_checked_path(root,'zero-exposure-migrations',window.identifier(v['migration_id']))
        e=ops.read_json(m/'evidence.json');_need(v['evidence_sha256']==ops.sha_file(m/'evidence.json'))
        _need(all(v[k]==e[k] for k in ('old_window_id','candidate','library','attempt_id','migration_id')))
        _need(v['claim_sha256']==ops.sha_file(_path(root,e['claim_path']))
              and v['freeze_sha256']==ops.sha_file(window.directory(root,e['old_window_id'])/'freeze/freeze-receipt.json'))
        validate_evidence(root,m,e)
        return v
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise RuntimeError('strict_zero_exposure_void_missing_or_invalid') from exc
