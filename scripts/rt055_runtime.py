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
import rt055_window as window

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


PROTECTED_SCOPES = ('builder','verifier','consumption','exposure','void-prequery',
                    'zero-exposure-migrations','formal-windows',
                    'runtime-policy-versions')


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
        for scope in PROTECTED_SCOPES:
            text += '(deny file-read* (subpath %s))\n' % json.dumps(str((parent/scope).resolve()))
            text += '(deny file-write* (subpath %s))\n' % json.dumps(str((parent/scope).resolve()))
    return text


def spawn_precheck(root, network_policy='loopback', window_id=None):
    if window_id is not None:
        from rt055_runtime_readiness import spawn_profile
        return spawn_profile(root,window_id,network_policy)
    # A main formal root cannot fall back to an old generic/synthetic policy.
    if (root/'verifier/case-verification.json').exists():
        raise RuntimeError('formal_spawn_requires_window_policy')
    receipt = root/'audit/execution-network-gate.json'
    if not receipt.is_file():raise RuntimeError('network_gate_missing')
    if not ops.read_json(receipt)['passed']:raise RuntimeError('network_gate_failed')
    profile = root/'runtime-policy'/('network.sb' if network_policy=='loopback' else 'search-network.sb')
    if profile.is_symlink() or not profile.is_file() or profile.read_text()!=sandbox_text(root,network_policy):
        raise RuntimeError('network_profile_drift')
    return profile


def spawn(root, argv, network_policy='loopback', window_id=None, workspace=None, **kwargs):
    if workspace is not None and workspace.migration_id:
        from rt055_candidate_workspace import probe_profile
        if window_id is not None:raise RuntimeError('candidate_probe_formal_window_argument')
        profile=probe_profile(workspace,network_policy)
    else:profile=spawn_precheck(root,network_policy,window_id)
    validate_environment(kwargs.get('env', {}))
    if any(str(a).endswith('/opensearch') for a in argv):
        if (network_policy != 'inbound-only' or 'network.host=127.0.0.1' not in argv
                or 'transport.host=127.0.0.1' not in argv
                or kwargs['env'].get('OPENSEARCH_JAVA_OPTS') != '-Djava.net.preferIPv4Stack=true'):
            raise ValueError('search_execution_boundary_invalid')
    from rt055_candidate_workspace import validate, policy
    if window_id is not None and workspace is None:raise RuntimeError('candidate_workspace_required')
    if workspace is not None:
        validate(workspace,window_id=window_id)
        if workspace.root!=root:raise RuntimeError('candidate_workspace_root_mismatch')
        sandbox=['/usr/bin/sandbox-exec','-p',policy(workspace,profile.read_text())]
    else:sandbox=['/usr/bin/sandbox-exec','-f',str(profile)]
    proc = subprocess.Popen([*sandbox,*argv],**kwargs)
    folder=root/'resources';folder.mkdir(mode=0o700,exist_ok=True)
    ops.write_private_json(folder/f'process-{proc.pid}.json',
        {'pid':proc.pid,'argv':argv,'started':time.time(),
         'cwd':str(kwargs.get('cwd',root)), 'network_policy':network_policy,
         **({'workspace':workspace.identity()} if workspace else {})})
    return proc

def data_bytes(root):
    # Entire data plane, including DB/WAL/cache/object files; not just main DB.
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())

def participating(root):
    v=ops.read_json(root/'verifier/case-verification.json')
    if not v['verified'] or not v['participating_libraries']:raise RuntimeError('case_verification_failed')
    return v['participating_libraries']

def claim_candidate(root, key, window_id):
    window.require_open(root,window_id)
    window.verification(root,window_id)
    receipt=window.receipt(root,window_id)
    order=receipt['run_order'];assert key in order
    if order.index(key):
        first=window.validate_run(root,window_id,order[0],participating(root))
        if first.get('status')!='OK' or first.get('window_id')!=window_id:
            raise RuntimeError('run_order_previous_failed')
    w=window.directory(root,window_id)
    if (w/('run-'+key)/'result.json').exists():raise FileExistsError('candidate_already_complete')
    # This is an execution-attempt claim, not a holdout-consumption claim.
    # Arm is not consumption; global exposure is persisted before the first private search.
    import uuid
    attempt=str(uuid.uuid4())
    window.write_once(w/('run-'+key)/'attempts'/attempt/'claim.json',
        {**window.envelope(root,window_id,'execution-attempt'),'candidate':key,'pid':os.getpid(),'time':time.time()})
    return attempt

JIEBA_FILES = ('jieba.dict.utf8','hmm_model.utf8','user.dict.utf8','idf.utf8','stop_words.utf8')


PRIVACY_SOURCE_FILES = ('rt055_runtime.py','rt055_confidentiality.py','rt055_opslib.py',
                        'rt055_run_a.py','rt055_run_b.py','rt055_embed_sidecar.py',
                        'rt055_freeze.py','kb_retrieval_candidates.py','kb_stage_b_poc.py',
                        'kb_stage_b_opensearch_benchmark.py')


# Versioned separately: never change the historical v1 recovery source set.
MIGRATION_SOURCE_FILES = PRIVACY_SOURCE_FILES + (
    'rt055_window.py','rt055_baseline.py','rt055_formal_coordinator.py',
    'rt055_aggregate.py','rt055_cleanup.py','rt055_tiers.py',
    'kb_retrieval_decision.py','rt055_runbooks.json','aggregate-report.schema.json',
    'rt055_zero_exposure.py','rt055_runtime_readiness.py','rt055_candidate_workspace.py','rt055_candidate_startup.py')


def migration_directory(root,migration_id):
    window.identifier(migration_id)
    p=root/'executioner-migrations'/migration_id
    if p.is_symlink() or p.parent.is_symlink():raise RuntimeError('migration_symlink')
    return p


def migration_evidence(root,migration_id,attempt):
    from rt055_confidentiality import evaluate
    import re
    if type(attempt) is not int or not 1<=attempt<=999:raise ValueError('synthetic_attempt_invalid')
    m=migration_directory(root,migration_id);t=m/('rt055-synthetic-%03d'%attempt)
    deployment=ops.read_json(m/'deployment.json')
    if (deployment.get('schema')!='cwk.rt055.executioner-deployment.v1'
            or deployment.get('run_id')!=root.name.removeprefix('rt055-')
            or deployment.get('migration_id')!=migration_id
            or not re.fullmatch('[0-9a-f]{40}',deployment.get('code_commit',''))
            or set(deployment.get('source_files',{}))!=set(MIGRATION_SOURCE_FILES)):
        raise RuntimeError('migration_deployment_invalid')
    for name,digest in deployment['source_files'].items():
        if ops.sha_file(root/'impl'/name)!=digest or ops.sha_file(t/'impl'/name)!=digest:
            raise RuntimeError('migration_source_drift')
    status=ops.read_json(t/'status/confidentiality.json')
    obs=ops.read_json(t/'audit/confidentiality-observations.json')
    clean=ops.read_json(t/'audit/synthetic-cleanup.json')
    if (status.get('status')!='PASS' or status.get('phase')!='COMPLETE'
            or not evaluate(obs)['passed'] or obs.get('cleanup_error') or 'execution_error_kind' in obs
            or obs.get('candidate_workspace_cleanup_zero') is not True
            or obs.get('candidate_workspace_candidates')!=['a','b']
            or clean!={'complete':True,'failures':0,'remaining_processes':0,'remaining_data_planes':0}):
        raise RuntimeError('new_synthetic_privacy_revalidation_required')
    return {'schema':'cwk.rt055.executioner-migration-binding.v1','run_id':deployment['run_id'],
            'migration_id':migration_id,'code_commit':deployment['code_commit'],'attempt':attempt,
            'source_files':deployment['source_files'],'status':'READY_TO_FREEZE',
            'deployment_sha256':ops.sha_file(m/'deployment.json'),
            'observations_sha256':ops.sha_file(t/'audit/confidentiality-observations.json'),
            'status_sha256':ops.sha_file(t/'status/confidentiality.json'),
            'cleanup_sha256':ops.sha_file(t/'audit/synthetic-cleanup.json'),
            'historical_recovery_sha256':ops.sha_file(root/'audit/confidentiality-recovery.json')}


def bind_privacy_migration(root,migration_id,attempt):
    row=migration_evidence(root,migration_id,attempt)
    window.write_once(migration_directory(root,migration_id)/'privacy-receipt.json',row)
    return row


def migration_privacy_passed(root,migration_id):
    try:
        row=ops.read_json(migration_directory(root,migration_id)/'privacy-receipt.json')
        return row==migration_evidence(root,migration_id,row['attempt'])
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):return False


def privacy_passed(root, migration_id=None):
    """An append-only recovery supersedes, but never rewrites, a failed gate."""
    if migration_id is not None:return migration_privacy_passed(root,migration_id)
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


def verify_ready(root,key,window_id):
    window.require_open(root,window_id)
    from rt055_freeze import verify_artifacts
    window.verification(root,window_id)
    if not verify_artifacts(root,window_id):raise RuntimeError('frozen_artifact_drift')
    if not privacy_passed(root,window.receipt(root,window_id)['privacy_migration_id']):raise RuntimeError('confidentiality_gate_failed')

def network_probe(root, *, policy_directory=None, gate_path=None):
    """Fresh loopback listener + real denied TCP syscalls, no production health."""
    if policy_directory is None and (root/'verifier/case-verification.json').exists():
        raise RuntimeError('formal_probe_requires_versioned_readiness')
    policy_directory=policy_directory or root/'runtime-policy'
    gate_path=gate_path or root/'audit/execution-network-gate.json'
    policy_directory.mkdir(mode=0o700,exist_ok=True)
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
            profile=policy_directory/filename
            with profile.open('x') as f:
                f.write(sandbox_text(root,kind));f.flush();os.fsync(f.fileno())
            profile.chmod(0o600)
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
    window.write_once(gate_path,result)
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
