#!/usr/bin/env python3
"""Pre-freeze PUBLIC synthetic native privacy gate. Never opens a holdout.

Use a fresh 0700 synthetic child workspace with public impl/tool copies. Keep
failed receipts/logs immutable. The OPS owner separately reconciles retained
bytes and publishes a recovery receipt; this command never freezes or scores.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import rt055_opslib as ops
import rt055_runtime as runtime
import rt055_candidate_workspace as cw
import uuid

NORMAL_QUERY = 'rt055publicnormalqueryz9'
NORMAL_TITLE = 'RT055 PUBLIC NORMAL TITLE 78213'
ERROR_QUERY = 'RT055_PUBLIC_ERROR_QUERY_98217'
ERROR_TITLE = 'RT055 PUBLIC ERROR TITLE 39217'
NEEDLES = (NORMAL_QUERY, NORMAL_TITLE, ERROR_QUERY, ERROR_TITLE)
KINDS = {'search', 'native', 'sidecar'}
ENV_NAMES = ('LANGFUSE_ENABLED','LANGFUSE_HOST','LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY',
             'LANGFUSE_BASE_URL','OTEL_SDK_DISABLED','OTEL_EXPORTER_OTLP_ENDPOINT',
             'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT','SERVER_HOST','LOG_LEVEL','LOG_PATH',
             'SSRF_WHITELIST','JIEBA_DICT_DIR','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','OPENSEARCH_JAVA_OPTS')


def assert_synthetic_root(root):
    if (root.is_symlink() or not root.is_dir() or not root.name.startswith('rt055-')
            or root.stat().st_mode & 0o077 or not (root/'.rt055-owned').is_file()):
        raise RuntimeError('synthetic_root_invalid')
    if any((root/p).is_file() for p in ('freeze/freeze-receipt.json','run-a/result.json','run-b/result.json')):
        raise RuntimeError('formal_state_present')
    if any(p.is_file() for name in ('builder','verifier','consumption','exposure','void-prequery','zero-exposure-migrations','formal-windows') for p in (root/name).rglob('*')):
        raise RuntimeError('private_or_consumed_input_present')


def scan_logs(root, needles):
    paths=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
    return len(paths), sum(any(n in p.read_text(errors='replace') for n in needles) for p in paths)


def evaluate(o):
    """Derive gates from observed events; zero unexecuted logs cannot pass."""
    startup=all(o.get(k) is True for k in ('search_started','native_started','sidecar_started'))
    normal=startup and o.get('search_normal_calls',0)>0 and o.get('native_normal_calls',0)>0
    error=(o.get('authenticated_session_verified') is True
           and o.get('authenticated_query_error_code') in (400,404,422,500)
           and o.get('authenticated_title_error_code') in (400,404,422,500))
    env=o.get('runtime_environment_verified') is True
    traffic=(o.get('embedding_calls',0)>0 and o.get('embedding_successes',0)>0
             and o.get('model_endpoint_verified') is True)
    telemetry=(env and traffic and o.get('trace_header_observations')==0
               and o.get('native_trace_response_headers')==0)
    sockets=(set(o.get('observed_kinds',[]))==KINDS
             and set(o.get('loopback_listener_kinds',[]))==KINDS
             and o.get('socket_samples',0)>0 and o.get('external_socket_observations')==0
             and o.get('observer_errors')==0)
    network=(o.get('external_probes_denied') is True and o.get('network_policy_verified') is True
             and o.get('nonloopback_model_rejected') is True)
    logs=normal and error and o.get('log_files_scanned',0)>0 and o.get('log_canary_hits')==0
    result={'normal_native_canary':normal,'authenticated_error_canaries':error,
            'native_log_canary_absent':logs,'langfuse_runtime_disabled':telemetry,
            'otel_runtime_disabled':telemetry,'loopback_models_only':traffic and sockets,
            'external_egress_denied':network,'forbidden_reads_zero':o.get('forbidden_reads')==0}
    result['passed']=all(result.values())
    return result


class Observer:
    def __init__(self,root):
        self.root=root;self.rows={};self.errors=0;self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)

    def sample(self):
        for receipt in (self.root/'resources').glob('process-*.json'):
            record=ops.read_json(receipt);pid=record['pid'];argv=record['argv']
            kind=('native' if any(str(a).endswith('/weknora-server') for a in argv)
                  else 'sidecar' if any(str(a).endswith('/rt055_embed_sidecar.py') for a in argv)
                  else 'search')
            ps=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True,timeout=10)
            if not ps.stdout.strip() or str(self.root) not in ps.stdout:continue
            if kind=='search' and 'org.opensearch.bootstrap.OpenSearch' not in ps.stdout:continue
            row=self.rows.setdefault(pid,{'kind':kind,'samples':0,'env_observed':False,
                                         'env_valid':True,'listeners':0,'external':0})
            try:
                env=ops.process_environment(pid,ENV_NAMES)
                if 'LANGFUSE_ENABLED' in env and 'OTEL_SDK_DISABLED' in env:
                    valid=True
                    try:runtime.validate_environment(env)
                    except ValueError:valid=False
                    if kind=='native':
                        valid &= (env.get('SERVER_HOST')=='127.0.0.1' and env.get('LOG_LEVEL')=='fatal'
                                  and env.get('LOG_PATH')=='/dev/null' and env.get('SSRF_WHITELIST')=='127.0.0.1'
                                  and env.get('JIEBA_DICT_DIR')==str(self.root/'jieba'))
                    if kind=='sidecar':valid &= env.get('HF_HUB_OFFLINE')=='1' and env.get('TRANSFORMERS_OFFLINE')=='1'
                    if kind=='search':valid &= '-Djava.net.preferIPv4Stack=true' in ps.stdout.split()
                    row['env_observed']=True;row['env_valid'] &= valid
                p=subprocess.run(['/usr/sbin/lsof','-nP','-a','-p',str(pid),'-i','-Fn'],capture_output=True,text=True,timeout=15)
                if p.returncode not in (0,1):raise RuntimeError('socket_observer_failed')
                for line in p.stdout.splitlines():
                    if not line.startswith('n'):continue
                    ends=line[1:].split('->')
                    local=all(re.fullmatch(r'(?:127\.0\.0\.1|\[::1\]):\d+(?: \(\w+\))?',part) for part in ends)
                    if not local:row['external']+=1
                    elif len(ends)==1:row['listeners']+=1
                row['samples']+=1
            except subprocess.CalledProcessError:
                # A process exiting between ps and ps eww is not missing live evidence.
                if subprocess.run(['/bin/ps','-p',str(pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:self.errors+=1
            except Exception:self.errors+=1

    def run(self):
        while not self.stop.is_set():
            try:self.sample()
            except Exception:self.errors+=1
            self.stop.wait(1)

    def finish(self):
        self.stop.set();self.thread.join(timeout=60)
        rows=list(self.rows.values())
        return {'observed_kinds':sorted({r['kind'] for r in rows}),
                'loopback_listener_kinds':sorted({r['kind'] for r in rows if r['listeners']>0}),
                'runtime_environment_verified':bool(rows) and all(r['env_observed'] and r['env_valid'] for r in rows),
                'socket_samples':sum(r['samples'] for r in rows),
                'external_socket_observations':sum(r['external'] for r in rows),
                'observer_errors':self.errors+int(self.thread.is_alive())}


def java_probe(root):
    """JDK Socket and NIO must both reject active external connect, not config."""
    source=root/'tmp/PrivacyJdkProbe.java'
    source.write_text('''import java.net.*;import java.nio.channels.*;
class PrivacyJdkProbe {public static void main(String[] a) throws Exception {
 int denied=0; boolean bind=false;
 try(ServerSocketChannel s=ServerSocketChannel.open()){s.bind(new InetSocketAddress("127.0.0.1",0));bind=true;}
 try(Socket s=new Socket()){s.connect(new InetSocketAddress("1.1.1.1",443),3000);}catch(SocketException e){if(e.getMessage().contains("Operation not permitted"))denied++;}
 try(SocketChannel s=SocketChannel.open()){s.connect(new InetSocketAddress("1.1.1.1",443));}catch(SocketException e){if(e.getMessage().contains("Operation not permitted"))denied++;}
 System.out.println("{\\"bind\\":"+bind+",\\"denied\\":"+denied+"}");
}}''')
    p=subprocess.run(['/usr/bin/sandbox-exec','-f',str(root/'runtime-policy/search-network.sb'),
                      str(root/'jdk/Contents/Home/bin/java'),'-Djava.net.preferIPv4Stack=true',str(source)],
                     env=runtime.clean_env(root),capture_output=True,timeout=90)
    if p.returncode:return False
    try:return json.loads(p.stdout)=={'bind':True,'denied':2}
    except ValueError:return False


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def run(root,protected_root=None):
    import rt055_run_a as search
    import rt055_run_b as native
    import kb_retrieval_candidates as candidate
    import kb_stage_b_poc as projection
    assert_synthetic_root(root);os.umask(0o077)
    for name in ('audit','status','resources','runtime-logs'):(root/name).mkdir(mode=0o700,exist_ok=True)
    claim=root/'status/confidentiality.claim'
    fd=os.open(claim,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    if protected_root:
        ops.write_private_json(root/'audit/protected-root.json',{'root':str(protected_root.resolve())})
    protected=[root]+([protected_root.resolve()] if protected_root else [])
    o={'forbidden_reads':0};probing=False;denial_probes=0
    def audit(event,args):
        nonlocal denial_probes
        if event!='open' or not isinstance(args[0],(str,bytes,os.PathLike)):return
        p=Path(os.path.realpath(os.fsdecode(args[0])))
        if any(p.is_relative_to(parent/scope) for parent in protected for scope in runtime.PROTECTED_SCOPES):
            if probing:denial_probes+=1
            else:o['forbidden_reads']+=1
            raise PermissionError('synthetic_private_read_denied')
    sys.addaudithook(audit)
    probing=True
    try:(protected[-1]/'builder/private-candidates.json').read_bytes();raise RuntimeError('denied_read_probe_failed')
    except PermissionError:pass
    probing=False
    state={'status':'RUNNING','phase':'NETWORK','pid':os.getpid()}
    def phase(value):state['phase']=value;ops.write_private_json(root/'status/confidentiality.json',state)
    processes=[];spaces=[];search_adapter=None;observer=Observer(root);observer.thread.start()
    search.ROOT=native.ROOT=root;search.HERE=native.HERE=root/'impl'
    try:
        phase('NETWORK');net=runtime.network_probe(root)
        o['network_policy_verified']=net['passed']
        o['java_bind_and_egress_probe']=java_probe(root)
        if not o['java_bind_and_egress_probe']:raise RuntimeError('java_network_policy_unproven')
        phase('SEARCH_START');search.ensure_icu_plugin()
        synthetic_window=str(uuid.uuid4())
        a=cw.create(root,synthetic_window,'a',str(uuid.uuid4()),synthetic=True);spaces.append(a)
        proc,info=search.launch_opensearch('privacy',free_port(),cw.file(a,'logs','search.log'),'smoke',workspace=a)
        processes.append(proc);o['search_started']=proc.poll() is None
        search.verify_icu(info['base_url'])
        kb='cwork-3m'
        doc=projection.SourceDocument(kb,'synthetic-public-001',NORMAL_TITLE,'synthetic-public.md',
                                      NORMAL_QUERY+' public alpha body 内容验证。\n'+('public synthetic line\n'*80),{})
        search_adapter=candidate.OpenSearchCandidate(candidate.LoopbackJSON(info['base_url']),'rt055privacy')
        search_adapter.build([doc],timeout=120)
        hits=search_adapter.search(NORMAL_QUERY,kb,timeout=30)
        o['search_normal_calls']=int(any(h.doc_id==doc.doc_id for h in hits))
        phase('SIDECAR_START');side_port=free_port()
        bspace=cw.create(root,synthetic_window,'b',str(uuid.uuid4()),synthetic=True);spaces.append(bspace)
        side=native.launch_sidecar(side_port,root/'sidecar/hf',cw.file(bspace,'logs','sidecar.log'),privacy_probe=True,workspace=bspace)
        processes.append(side);o['sidecar_started']=side.poll() is None
        phase('NATIVE_START')
        server=native.setup_instance('privacy',free_port(),side_port,cw.file(bspace,'data','privacy-native'),cw.file(bspace,'logs','native.log'),random.Random(),workspace=bspace)
        processes.append(server['proc']);o['native_started']=server['proc'].poll() is None
        auth=native.api_call(server['base_url'],'GET','/api/v1/knowledge-bases',None,server['token'])
        o['authenticated_session_verified']=auth.get('success') is True
        try:
            native.api_call(server['base_url'],'POST','/api/v1/models',
                            {'parameters':{'base_url':'http://1.1.1.1:443/v1'}},server['token'])
            o['nonloopback_model_rejected']=False
        except ValueError:o['nonloopback_model_rejected']=True
        # Native persisted model endpoint, not the submitted configuration alone.
        models=native.api_call(server['base_url'],'GET','/api/v1/models',None,server['token'])
        entries=models.get('data',[])
        if isinstance(entries,dict):entries=entries.get('models',[])
        urls=[m.get('parameters',{}).get('base_url') for m in entries if isinstance(m,dict)]
        o['model_endpoint_verified']=bool(urls) and all(u==f'http://127.0.0.1:{side_port}/v1' for u in urls)
        phase('NATIVE_NORMAL_CANARY')
        routing=native.RoutingTransport();routing.add_server(kb,server)
        b=candidate.WeKnoraCandidate(routing,{kb:server['kb_id']})
        deadline=time.monotonic()+1800
        try:b.build([doc],timeout=120)
        except candidate.CandidateError:
            if not b._import_complete:raise
        while not b.ready and time.monotonic()<deadline:
            time.sleep(3)
            try:b.check_ready(timeout=30)
            except candidate.CandidateError:continue
        if not b.ready:raise RuntimeError('synthetic_import_not_ready')
        hits=b.search(NORMAL_QUERY,kb,timeout=90)
        o['native_normal_calls']=int(any(h.doc_id==doc.doc_id for h in hits))
        phase('AUTHENTICATED_ERROR_CANARIES')
        # Both use a real authenticated, accessible KB. Bad typed/validated
        # fields force the handler's parsed request error path, not a 401.
        probes=[('authenticated_query_error_code','/hybrid-search',{'query_text':ERROR_QUERY,'match_count':'INVALID_PUBLIC_INTEGER'}),
                ('authenticated_title_error_code','/knowledge/manual',{'title':ERROR_TITLE,'content':{'public_invalid_type':NORMAL_QUERY},'status':'publish'})]
        for name,suffix,payload in probes:
            try:
                native.api_call(server['base_url'],'POST','/api/v1/knowledge-bases/'+server['kb_id']+suffix,payload,server['token']);o[name]=200
            except urllib.error.HTTPError as exc:o[name]=exc.code;exc.read();exc.close()
        # Send an incoming trace context through a successful native model call;
        # inspect response and sidecar request headers after it completes.
        req=urllib.request.Request(server['base_url']+'/api/v1/knowledge-bases/'+server['kb_id']+'/hybrid-search',
             data=json.dumps({'query_text':NORMAL_QUERY,'match_count':10}).encode(),
             headers={'Content-Type':'application/json','Authorization':'Bearer '+server['token'],
                      'traceparent':'00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),native._NoRedirect())
        with opener.open(req,timeout=90) as resp:
            resp.read();o['native_trace_response_headers']=sum(bool(resp.headers.get(k)) for k in ('traceparent','x-langfuse-trace-id','x-trace-id'))
        with opener.open(f'http://127.0.0.1:{side_port}/health',timeout=10) as resp:stats=json.load(resp)
        o.update(embedding_calls=stats['embedding_calls'],embedding_successes=stats['embedding_successes'],trace_header_observations=stats['trace_headers'])
        with opener.open(f'http://127.0.0.1:{side_port}/privacy-probe',timeout=10) as resp:probe=json.load(resp)
        o['sidecar_external_errno']=probe['errno']
        o['external_probes_denied']=net['external_denied'] and o['java_bind_and_egress_probe'] and probe['denied'] is True
        phase('RUNTIME_OBSERVATION');time.sleep(5)
    except Exception as exc:
        o['execution_error_kind']=type(exc).__name__
    finally:
        phase('STOPPING_SYNTHETIC')
        if search_adapter:
            try:search_adapter.close()
            except Exception:o['cleanup_error']=True
        for proc in reversed(processes):ops.stop_process(proc)
        o.update(observer.finish());o['denial_probes']=denial_probes
        count=hits=0
        for space in spaces:
            paths=cw.log_files(space);count+=len(paths)
            hits+=sum(any(n in p.read_text(errors='replace') for n in NEEDLES) for p in paths)
            try:cw.scan(space,NEEDLES)
            except Exception:o['cleanup_error']=True
            try:cw.cleanup(space)
            except Exception:o['cleanup_error']=True
        o['candidate_workspace_cleanup_zero']=not (root/'candidate-runtime').exists()
        o['candidate_workspace_candidates']=sorted(s.candidate for s in spaces)
        other_count,other_hits=scan_logs(root/'weknora/logs',NEEDLES)
        o['log_files_scanned']=count+other_count;o['log_canary_hits']=hits+other_hits
        result=evaluate(o)
        if o.get('cleanup_error') or 'execution_error_kind' in o:result['passed']=False
        ops.write_private_json(root/'audit/confidentiality-observations.json',o)
        ops.write_private_json(root/'audit/confidentiality-gate.json',result)
        state.update(status='PASS' if result['passed'] else 'RECOVERABLE_EXECUTION_FAILURE',phase='COMPLETE',finished_at=time.time())
        ops.write_private_json(root/'status/confidentiality.json',state)
    return 0 if result['passed'] else 3


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path)
    parser.add_argument('--protected-root',type=Path)
    args=parser.parse_args()
    root=args.root or Path(__file__).resolve().parent.parent
    return run(root,args.protected_root)

if __name__=='__main__':raise SystemExit(main())
