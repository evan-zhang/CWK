"""OPS-only experiment lifecycle helpers; no private values printed/exported."""
from __future__ import annotations
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import rt055_opslib as ops

SANDBOX = '''(version 1)
(allow default)
(deny network*)
(allow network-outbound (remote ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-bind (local ip "localhost:*"))
'''

def clean_env(root):
    # Never inherit NAS, Gateway, tracing or operator credentials into candidates.
    env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TMPDIR') if k in os.environ}
    env.update(HOME=str(root/'home'),HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OTEL_SDK_DISABLED='true',LANGFUSE_ENABLED='false')
    (root/'home').mkdir(mode=0o700,exist_ok=True)
    return env

def spawn(root, argv, **kwargs):
    if not (root/'audit/network-gate.json').is_file(): raise RuntimeError('network_gate_missing')
    if not ops.read_json(root/'audit/network-gate.json')['passed']:raise RuntimeError('network_gate_failed')
    proc=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(root/'freeze/network.sb'),*argv],**kwargs)
    folder=root/'resources';folder.mkdir(mode=0o700,exist_ok=True)
    ops.write_private_json(folder/f'process-{proc.pid}.json',{'pid':proc.pid,'argv':argv,'started':time.time(),'cwd':str(kwargs.get('cwd',root))})
    return proc

def data_bytes(root):
    # Entire data plane, including DB/WAL/cache/object files; not just main DB.
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())

def participating(root):
    v=ops.read_json(root/'verifier/case-verification.json')
    if not v['verified'] or not v['participating_libraries']:raise RuntimeError('case_verification_failed')
    return v['participating_libraries']

def claim_candidate(root, key):
    v=ops.read_json(root/'verifier/freeze-verification.json')
    if not v.get('verified'):raise RuntimeError('freeze_invalid')
    receipt=ops.read_json(root/'freeze/freeze-receipt.json')
    order=receipt['run_order'];assert key in order
    if order.index(key):
        first=ops.read_json(root/('run-'+order[0])/'result.json')
        if first['status']!='OK':raise RuntimeError('run_order_previous_failed')
    folder=root/'consumption';folder.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(folder/(key+'.claim'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as h:json.dump({'candidate':key,'pid':os.getpid(),'time':time.time(),'libraries':participating(root)},h)

def verify_ready(root,key):
    from rt055_freeze import verify_artifacts
    if not verify_artifacts(root):raise RuntimeError('frozen_artifact_drift')
    if not ops.read_json(root/'audit/confidentiality-gate.json')['passed']:raise RuntimeError('confidentiality_gate_failed')

def network_probe(root):
    (root/'freeze').mkdir(mode=0o700,exist_ok=True)
    (root/'freeze/network.sb').write_text(SANDBOX)
    # Actual denied non-loopback syscall; TCP denial, not a health inference.
    program='''import socket,json
out={}
for key,host,port in [('external','1.1.1.1',443),('loopback','127.0.0.1',8787)]:
 s=socket.socket();s.settimeout(8)
 try:s.connect((host,port));out[key]='CONNECTED'
 except OSError as e:out[key]='DENIED' if e.errno in (1,13) else 'UNKNOWN'
 finally:s.close()
print(json.dumps(out))
'''
    proc=subprocess.run(['/usr/bin/sandbox-exec','-f',str(root/'freeze/network.sb'),sys.executable,'-c',program],capture_output=True,timeout=30)
    try:result=json.loads(proc.stdout)
    except Exception:result={}
    passed=proc.returncode==0 and result=={'external':'DENIED','loopback':'CONNECTED'}
    ops.write_private_json(root/'audit/network-gate.json',{'passed':passed,'external_denied':result.get('external')=='DENIED','loopback_allowed':result.get('loopback')=='CONNECTED'})
    if not passed:raise RuntimeError('network_sandbox_unproven')

def gateway_probe(root, key, candidate, libraries):
    """Actual ephemeral HTTPS authorization shell, not existing /health inference.

    Same control-plane contract for A/B. Probe uses neutral synthetic warmup,
    never holdout; public boolean means laboratory capability, not production
    deployed integration. No NAS/search credential is returned to its clients.
    """
    directory=root/'gateway-probe'/key;directory.mkdir(parents=True,mode=0o700,exist_ok=True)
    cert,keyfile=directory/'cert.pem',directory/'key.pem'
    if not cert.exists():
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(keyfile),'-out',str(cert),'-days','1','-subj','/CN=localhost'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)
        keyfile.chmod(0o600)
    grants={secrets.token_hex(24):kb for kb in libraries}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            identity=self.headers.get('Authorization','').removeprefix('Bearer ')
            bound=grants.get(identity)
            try:
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                if not bound or body.get('kb_id')!=bound:
                    code,payload=403,{'error':'DENIED'}
                else:
                    # Test real candidate query route while returning only safe capability metadata.
                    hits=candidate.search(body['query'],bound,timeout=30)
                    code,payload=200,{'identity':bound,'query_api':True,'credentials_returned':False,'result_count':len(hits)}
            except Exception:code,payload=500,{'error':'QUERY_FAILED'}
            data=json.dumps(payload).encode();self.send_response(code);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,keyfile)
    server.socket=ctx.wrap_socket(server.socket,server_side=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    probes=[]
    try:
        trust=ssl.create_default_context(cafile=str(cert))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=trust))
        for token,kb in grants.items():
            for sent,auth,expected in ((kb,token,200),('forbidden-library',token,403),(kb,'invalid-token',403)):
                req=urllib.request.Request('https://localhost:%d/query'%server.server_port,data=json.dumps({'kb_id':sent,'query':ops.WARMUP_QUERIES[0]}).encode(),headers={'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
                try:
                    with opener.open(req,timeout=40) as resp:code=resp.status;payload=json.load(resp)
                except urllib.error.HTTPError as exc:code=exc.code;payload=json.load(exc)
                probes.append({'authorized':expected==200,'status_match':code==expected,'identity_match':payload.get('identity')==kb if expected==200 else True,'no_credentials':set(payload)<= {'identity','query_api','credentials_returned','error','result_count'}})
    finally:server.shutdown();server.server_close();worker.join(timeout=10)
    result={'https_query_api':all(p['status_match'] for p in probes if p['authorized']),
            'per_gateway_identity':all(p['identity_match'] for p in probes),
            'kb_grants_server_side':all(p['status_match'] for p in probes if not p['authorized']),
            'no_direct_nas_or_search_credentials':all(p['no_credentials'] for p in probes)}
    ops.write_private_json(directory/('probe-'+secrets.token_hex(4)+'.json'),{'results':result,'checks':probes})
    return result
