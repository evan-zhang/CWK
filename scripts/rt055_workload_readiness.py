"""A/B public synthetic 42/31/42 documents and trials, never formal scoring.

Public text is authored here, not sampled from private strings. One upper-bound
512 KiB document per library plus 8 KiB documents. Both candidates use the exact
same documents, counts and 7200 second per-library build deadline.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import threading
import time
import uuid
import urllib.error
import rt055_opslib as ops
import rt055_runtime as runtime
import rt055_window as window
import rt055_candidate_workspace as cw
import rt055_log_firewall as fw
import rt055_build_readiness as build
import kb_retrieval_candidates as kbc
import kb_stage_b_poc as poc

SCHEMA='cwk.rt055.public-workload.v2'
COUNTS=dict(zip(ops.LIBRARIES,(42,31,42)))
UPPER_BYTES=524288
SQL_CANARY='RT055_PUBLIC_NATIVE_SQL_FIREWALL_CANARY_雪'

def documents(kb):
    result=[]
    for i in range(COUNTS[kb]):
        target=UPPER_BYTES if i==0 else 8192
        prefix=f'Public synthetic reference-{i:03d}. Public synthetic solar panels electricity. '+SQL_CANARY+'\n'
        line='公开合成资料：白天由太阳能板发电，夜间由储能电池供电。\n'
        text=prefix+(line*((target-len(prefix.encode()))//len(line.encode())))
        if len(text)>199000:raise RuntimeError('public_native_character_limit')
        result.append(poc.SourceDocument(kb,f'public-workload-{i:03d}',f'{SQL_CANARY} public document {i:03d}',f'public-workload-{i:03d}.md',text,{}))
    return result

def safe_result(row):
    return (row.get('schema')==SCHEMA and row.get('status')=='PASS' and row.get('counts')==COUNTS
        and row.get('timeout_seconds')==7200 and row.get('document_byte_upper_bound')==UPPER_BYTES
        and row.get('private_reads')==row.get('formal_queries')==row.get('formal_attempts')==0
        and row.get('cleanup_failures')==row.get('remaining_runtime')==0
        and set(row.get('candidates',{}))=={'a','b'}
        and all(set(v.get('libraries',{}))==set(COUNTS) and v.get('firewall_verified') is True
            and v.get('post_scan_hits')==0 and v.get('searches')==3
            and all(x.get('imported')==x.get('completed')==COUNTS[k] and x.get('pending')==x.get('failed')==0
                and 0<x.get('build_seconds',0)<=7200 for k,x in v['libraries'].items()) for v in row['candidates'].values())
        and all(v.get('max_inflight')==1 and v.get('post_count')==COUNTS[k]
                and v.get('completed_before_next_post')==COUNTS[k]-1
                for k,v in row['candidates']['b']['libraries'].items())
        and all(v.get('firewall_streams',0)==(3 if key=='a' else 6) and v.get('eof_all') is True
                and v.get('closed_all') is True and v.get('caps_pass') is True
                and v.get('firewall_errors')==0 for key,v in row['candidates'].items())
        and row['candidates']['b'].get('sql_canary_redactions',0)>0
        and row.get('native_sql_errorpath_injected') is True)

def verify(root,mid):
    m=runtime.migration_directory(root,mid);dep=ops.read_json(m/'deployment.json')
    row=ops.read_json(m/'public-workload.json')
    if not safe_result(row) or row['source_commit']!=dep['code_commit'] or row['migration_id']!=mid:
        raise RuntimeError('public_workload_not_verified')
    if not all(ops.sha_file(root/'impl'/n)==h for n,h in dep['source_files'].items()):raise RuntimeError('workload_source_drift')
    return row

def freeze_files(root,mid,before_started):
    row=verify(root,mid)
    if row['finished_at']>=before_started:raise RuntimeError('public_workload_after_before')
    return (str((runtime.migration_directory(root,mid)/'public-workload.json').relative_to(root)),)

def finalize_streams(space, current, values):
    """Final drain result is collected on both success and failure, before cleanup."""
    receipt = fw.finalize(space)
    current.update(firewall_verified=receipt['verified'],
        firewall_streams=len(receipt['logs']),
        eof_all=all(x['eof'] for x in receipt['logs']),
        closed_all=all(x['closed'] for x in receipt['logs']),
        caps_pass=all(not x['overflow'] and x['input_bytes']<=fw.CAP
                      and x['output_bytes']<=fw.CAP for x in receipt['logs']),
        firewall_errors=sum(x['error']!='NONE' for x in receipt['logs']),
        input_bytes=sum(x['input_bytes'] for x in receipt['logs']),
        output_bytes=sum(x['output_bytes'] for x in receipt['logs']),
        redactions=sum(x['redaction_count'] for x in receipt['logs']))
    scan_path=space.ledger.parent/'log-scan.json'
    scan=ops.read_json(scan_path) if scan_path.exists() else cw.scan(space,values)
    current['post_scan_hits']=scan['hit_files']
    if not receipt['verified'] or not scan['passed'] or not current['caps_pass']:
        raise fw.FirewallError('UNVERIFIED')
    return receipt

def run(root,mid,source_commit):
    import rt055_run_a as a,rt055_run_b as b
    from rt055_confidentiality import assert_synthetic_root,free_port
    assert_synthetic_root(root);os.umask(0o077)
    row={'schema':SCHEMA,'data_class':'PUBLIC_SYNTHETIC_ONLY_NOT_FORMAL_SCORES','status':'RUNNING',
         'migration_id':mid,'source_commit':source_commit,'counts':COUNTS,'trial_counts':COUNTS,
         'timeout_seconds':7200,'document_byte_upper_bound':UPPER_BYTES,'ordinary_document_bytes':8192,
         'private_reads':0,'formal_queries':0,'formal_attempts':0,'cleanup_failures':0,'remaining_runtime':0,
         'native_sql_errorpath_injected':False,'candidates':{},'started_at':time.time()}
    protected=[root]
    binding=root/'audit/protected-root.json'
    if binding.exists():protected.append(Path(ops.read_json(binding)['root']))
    from rt055_confidentiality import load_private_bank
    if len(protected)!=2:raise RuntimeError('full_private_bank_protected_root_required')
    full_bank,_=load_private_bank(protected[-1])
    def deny(event,args):
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            p=Path(os.path.realpath(os.fsdecode(args[0])))
            if any(p.is_relative_to(r/scope) for r in protected for scope in runtime.PROTECTED_SCOPES):
                row['private_reads']+=1;raise PermissionError('synthetic_private_read_denied')
    sys.addaudithook(deny)
    a.ROOT=b.ROOT=root;a.HERE=b.HERE=root/'impl'
    spaces=[];processes=[];adapters=[];current=None;error=None
    window.write_once(root/'status/workload.claim',{'mode':'PUBLIC_ONLY','pid':os.getpid()})
    def status(phase):
        row['phase']=phase;ops.write_private_json(root/'status/workload.json',row)
    try:
        for key in ('a','b'):
            status('START_'+key.upper());space=cw.create(root,str(uuid.uuid4()),key,str(uuid.uuid4()),synthetic=True);spaces.append(space)
            docs={kb:documents(kb) for kb in COUNTS}
            trials=[kbc.Case(kb,f'Public synthetic reference-{i:03d}',frozenset({d.doc_id}) if i%3!=2 else frozenset(),i%3==0,i+1)
                    for kb,ds in docs.items() for i,d in enumerate(ds)]
            totals=kbc.validate_cases(trials,require_trial_identity=True)
            if {kb:v['total_count'] for kb,v in totals.items()}!=COUNTS:raise RuntimeError('public_trial_counts_invalid')
            values=cw.needles({kb:[vars(d) for d in ds] for kb,ds in docs.items()},trials)+[SQL_CANARY]+list(full_bank.values)
            firewall=fw.bind(space,values)
            current={'libraries':{},'searches':0,'firewall_verified':False,'post_scan_hits':0};row['candidates'][key]=current
            transport=b.RoutingTransport()
            services={}
            for kb in COUNTS:
                if key=='a':
                    proc,info=a.launch_opensearch(kb,free_port(),cw.file(space,'logs','a-'+kb+'.log'),'smoke',workspace=space);processes.append(proc)
                    a.verify_icu(info['base_url']);adapter=kbc.OpenSearchCandidate(kbc.LoopbackJSON(info['base_url']),'rt055workload');adapters.append(adapter);services[kb]=adapter
                else:
                    port=free_port();proc=b.launch_sidecar(port,root/'sidecar/hf',cw.file(space,'logs','sidecar-'+kb+'.log'),workspace=space);processes.append(proc)
                    info=b.setup_instance(kb,free_port(),port,cw.file(space,'data','b-'+kb),cw.file(space,'logs','native-'+kb+'.log'),random.Random(),workspace=space);processes.append(info['proc']);transport.add_server(kb,info)
                    services[kb]=kbc.WeKnoraCandidate(transport,{kb:info['kb_id']})
            for kb in COUNTS:
                status('BUILD_'+key.upper());adapter=services[kb];started=time.monotonic()
                if key=='a':adapter.build(docs[kb],timeout=build.TIMEOUT)
                else:
                    build.build_b(adapter,docs[kb],transport,kb,space.ledger.parent/'build-status.json',firewall)
                elapsed=time.monotonic()-started
                current['libraries'][kb]={'imported':COUNTS[kb],'completed':COUNTS[kb],'pending':0,'failed':0,'build_seconds':round(elapsed,3)}
                if key=='b':
                    current['libraries'][kb].update(build.counts(transport)[kb],
                        max_inflight=transport.max_inflight[kb],post_count=transport.post_count[kb],
                        completed_before_next_post=transport.completed_before_next_post[kb])
                status('SEARCH_'+key.upper());firewall.health()
                # At least one real public native search per library; no scorer.
                hits=adapter.search('Public synthetic solar panels electricity',kb,timeout=30)
                if not hits:raise RuntimeError('public_search_empty')
                current['searches']+=1
                if key=='b':
                    # A controlled GORM INSERT error writes SQL through the
                    # real native stdout logger. An owned SQLite write lock
                    # guarantees this EXTRA probe cannot persist a document.
                    # Run ONLY AFTER normal build/search, never before lazy native initialization.
                    db=sqlite3.connect(str(transport.servers[kb]['data_dir']/'app.db'))
                    db.execute('BEGIN IMMEDIATE');rejected=False
                    try:
                        info=transport.servers[kb]
                        try:b.api_call(info['base_url'],'POST','/api/v1/knowledge-bases/'+info['kb_id']+'/knowledge/manual',
                            {'title':SQL_CANARY,'content':SQL_CANARY,'status':'publish'},info['token'],timeout=30)
                        except urllib.error.HTTPError as exc:
                            rejected=exc.code==500;exc.close()
                    finally:db.rollback();db.close()
                    if not rejected:raise RuntimeError('public_sql_probe_not_rejected')
                    row['native_sql_errorpath_injected']=True
            for adapter in adapters:adapter.close()
            adapters=[]
            for proc in reversed(processes):ops.stop_process(proc)
            processes=[]
            receipt=finalize_streams(space,current,values)
            current['redactions']=sum(x['redaction_count'] for x in receipt['logs'])
            if key=='b':
                current['sql_canary_redactions']=sum(x['redaction_count'] for x in receipt['logs'] if x['log_identity'].startswith('logs/native-'))
                # Count-only proof of the forced canary + native SQL INSERT on the
                # same sanitized stream. Never open unsanitized native output.
                current['native_sql_sanitized_observed']=all(b'INSERT INTO' in p.read_bytes() and fw.REPLACEMENT in p.read_bytes() for p in firewall.streams if p.name.startswith('native-'))
                if not current['native_sql_sanitized_observed']:raise RuntimeError('native_sql_firewall_unproven')
            cw.cleanup(space);spaces.remove(space)
        row['status']='PASS'
    except Exception as exc:
        row['status']='BLOCKED';row['error']=build.error_code(exc);row['error_kind']=type(exc).__name__;error=True
    finally:
        for adapter in adapters:
            try:adapter.close()
            except Exception:row['cleanup_failures']+=1
        for proc in reversed(processes):ops.stop_process(proc)
        for space in spaces:
            try:finalize_streams(space,row['candidates'][space.candidate],fw.get(space).values or [SQL_CANARY])
            except Exception:
                row['status']='BLOCKED';row['error']='FIREWALL_FAILED'
                row['candidates'][space.candidate]['firewall_verified']=False
            try:cw.cleanup(space)
            except Exception:row['cleanup_failures']+=1
        row['remaining_runtime']=int((root/'candidate-runtime').exists());row['finished_at']=time.time()
        row['phase']='COMPLETE';ops.write_private_json(root/'status/workload.json',row)
        window.write_once(root/'audit/public-workload.json',row)
    return row

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--migration-id',required=True);p.add_argument('--source-commit',required=True)
    a=p.parse_args();r=run(a.root,a.migration_id,a.source_commit);return 0 if safe_result(r) else 3
if __name__=='__main__':raise SystemExit(main())
