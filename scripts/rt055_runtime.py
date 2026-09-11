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

# OpenSearch is single-node and serves only inbound HTTP. It needs no outbound
# socket. This also avoids relying on JVM-specific connect enforcement.
SEARCH_SANDBOX = '''(version 1)
(allow default)
(deny network*)
(allow network-inbound (local ip "localhost:*"))
'''


def require_loopback_url(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.username or parsed.password
            or parsed.fragment or parsed.query):
        raise ValueError('model_or_native_endpoint_not_loopback')
    return value


def validate_environment(env):
    if env.get('LANGFUSE_ENABLED') != 'false' or env.get('OTEL_SDK_DISABLED') != 'true':
        raise ValueError('tracing_not_disabled')
    for name, value in env.items():
        if name.startswith('LANGFUSE') and name != 'LANGFUSE_ENABLED' and value:
            raise ValueError('langfuse_configuration_present')
        if name.startswith('OTEL_') and name != 'OTEL_SDK_DISABLED' and value not in ('', 'none'):
            raise ValueError('otel_exporter_configuration_present')
        if name in ('SERVER_HOST', 'SSRF_WHITELIST') and value != '127.0.0.1':
            raise ValueError('native_bind_not_loopback')
        if name in ('OPENAI_BASE_URL', 'OPENAI_API_BASE', 'OLLAMA_HOST') and value:
            require_loopback_url(value)


def clean_env(root):
    # No credentials, proxies, tracing endpoints or operator model settings.
    env = {k:os.environ[k] for k in ('PATH','LANG','LC_ALL') if k in os.environ}
    for name in ('home','tmp'):(root/name).mkdir(mode=0o700,exist_ok=True)
    env.update(HOME=str(root/'home'), TMPDIR=str(root/'tmp'),
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               OTEL_SDK_DISABLED='true', LANGFUSE_ENABLED='false', PYTHONDONTWRITEBYTECODE='1')
    return env


def sandbox_text(root, network_policy):
    if network_policy not in ('loopback','inbound-only'):
        raise RuntimeError('unsupported_network_policy')
    text = SANDBOX if network_policy == 'loopback' else SEARCH_SANDBOX
    # OS-level write confinement, including descendants, not just cwd convention.
    # JSON quotes are also valid SBPL string literals.
    text += '(deny file-write*)\n'
    text += '(allow file-write* (subpath %s) (literal "/dev/null"))\n' % json.dumps(str(root.resolve()))
    protected = [root]
    binding = root/'audit/protected-root.json'
    if binding.exists():protected.append(Path(ops.read_json(binding)['root']))
    for parent in protected:
        for scope in ('builder','verifier','consumption'):
            text += '(deny file-read* (subpath %s))\n' % json.dumps(str((parent/scope).resolve()))
            text += '(deny file-write* (subpath %s))\n' % json.dumps(str((parent/scope).resolve()))
    return text


def spawn(root, argv, network_policy='loopback', **kwargs):
    receipt = root/'audit/execution-network-gate.json'
    if not receipt.is_file():raise RuntimeError('network_gate_missing')
    if not ops.read_json(receipt)['passed']:raise RuntimeError('network_gate_failed')
    profile = root/'runtime-policy'/('network.sb' if network_policy=='loopback' else 'search-network.sb')
    if profile.is_symlink() or not profile.is_file() or profile.read_text()!=sandbox_text(root,network_policy):
        raise RuntimeError('network_profile_drift')
    validate_environment(kwargs.get('env', {}))
    if any(str(a).endswith('/opensearch') for a in argv):
        if (network_policy != 'inbound-only' or 'network.host=127.0.0.1' not in argv
                or 'transport.host=127.0.0.1' not in argv
                or kwargs['env'].get('OPENSEARCH_JAVA_OPTS') != '-Djava.net.preferIPv4Stack=true'):
            raise ValueError('search_execution_boundary_invalid')
    proc = subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(profile),*argv],**kwargs)
    folder=root/'resources';folder.mkdir(mode=0o700,exist_ok=True)
    ops.write_private_json(folder/f'process-{proc.pid}.json',
        {'pid':proc.pid,'argv':argv,'started':time.time(),
         'cwd':str(kwargs.get('cwd',root)), 'network_policy':network_policy})
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

JIEBA_FILES = ('jieba.dict.utf8','hmm_model.utf8','user.dict.utf8','idf.utf8','stop_words.utf8')


PRIVACY_SOURCE_FILES = ('rt055_runtime.py','rt055_confidentiality.py','rt055_opslib.py',
                        'rt055_run_a.py','rt055_run_b.py','rt055_embed_sidecar.py',
                        'rt055_freeze.py','kb_retrieval_candidates.py','kb_stage_b_poc.py',
                        'kb_stage_b_opensearch_benchmark.py')


def privacy_passed(root):
    """An append-only recovery supersedes, but never rewrites, a failed gate."""
    from rt055_confidentiality import evaluate
    receipt = root/'audit/confidentiality-recovery.json'
    try:
        if receipt.exists():
            row=ops.read_json(receipt)
            if (row.get('schema')!='cwk.rt055.privacy-recovery-binding.v1'
                    or row.get('run_id')!=root.name.removeprefix('rt055-')
                    or row.get('status')!='READY_TO_FREEZE'
                    or set(row.get('source_files',{}))!=set(PRIVACY_SOURCE_FILES)):
                return False
            if not all(ops.sha_file(root/'impl'/name)==digest for name,digest in row['source_files'].items()):
                return False
            observations=ops.read_json(root/'audit/confidentiality-recovery-observations.json')
            if ops.sha_file(root/'audit/confidentiality-recovery-observations.json')!=row['observations_sha256']:
                return False
        else:
            observations=ops.read_json(root/'audit/confidentiality-observations.json')
        return evaluate(observations)['passed'] and not observations.get('cleanup_error') and 'execution_error_kind' not in observations
    except (OSError,ValueError,KeyError,TypeError):return False


def verify_ready(root,key):
    from rt055_freeze import verify_artifacts
    if not verify_artifacts(root):raise RuntimeError('frozen_artifact_drift')
    if not privacy_passed(root):raise RuntimeError('confidentiality_gate_failed')

def network_probe(root):
    """Fresh loopback listener + real denied TCP syscalls, no production health."""
    (root/'runtime-policy').mkdir(mode=0o700,exist_ok=True)
    listener = http.server.ThreadingHTTPServer(('127.0.0.1',0), http.server.BaseHTTPRequestHandler)
    port = listener.server_port
    program = '''import socket,json,sys
out={}
for key,host,port in [('external','1.1.1.1',443),('loopback','127.0.0.1',int(sys.argv[1]))]:
 s=socket.socket();s.settimeout(5)
 try:s.connect((host,port));out[key]='CONNECTED'
 except OSError as e:out[key]='DENIED' if e.errno in (1,13) else 'UNKNOWN'
 finally:s.close()
print(json.dumps(out))
'''
    observations={}
    try:
        for kind, filename in (('loopback','network.sb'),('inbound-only','search-network.sb')):
            profile=root/'runtime-policy'/filename
            profile.write_text(sandbox_text(root,kind));profile.chmod(0o600)
            proc=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,
                                 '-c',program,str(port)],capture_output=True,timeout=30,
                                env=clean_env(root))
            try:observations[kind]=json.loads(proc.stdout) if proc.returncode==0 else {}
            except ValueError:observations[kind]={}
    finally:listener.server_close()
    passed=(observations.get('loopback')=={'external':'DENIED','loopback':'CONNECTED'}
            and observations.get('inbound-only')=={'external':'DENIED','loopback':'DENIED'})
    result={'passed':passed,'external_denied':passed,'loopback_allowed':passed,
            'policy_observations':observations}
    ops.write_private_json(root/'audit/execution-network-gate.json',result)
    if not passed:raise RuntimeError('network_sandbox_unproven')
    return result

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
