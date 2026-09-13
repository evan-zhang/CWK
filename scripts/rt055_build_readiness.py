"""Count-only native build observation; terminal failures never poll to deadline."""
from __future__ import annotations
import time
import kb_retrieval_candidates as kbc
import rt055_opslib as ops
from rt055_log_firewall import FirewallError
TIMEOUT=7200.0
HTTP_CODES=('NATIVE_HTTP_400','NATIVE_HTTP_401','NATIVE_HTTP_403','NATIVE_HTTP_404','NATIVE_HTTP_409','NATIVE_HTTP_413','NATIVE_HTTP_422','NATIVE_HTTP_429','NATIVE_HTTP_500','NATIVE_HTTP_503','NATIVE_HTTP_OTHER')
class NativeHTTPError(kbc.CandidateError):
    def __init__(self,status):
        value='NATIVE_HTTP_'+str(status)
        self.code=value if value in HTTP_CODES else 'NATIVE_HTTP_OTHER'
        super().__init__(self.code)

# Exact public exception constants only; unmatched text is never exported.
REQUEST_CODES={
    'native ingestion status invalid':'NATIVE_STATE_UNRECOGNIZED',
    'invalid native ingestion receipt':'NATIVE_RECEIPT_INVALID',
    'response size exceeded':'NATIVE_RESPONSE_SIZE_EXCEEDED',
    'native response json invalid':'NATIVE_RESPONSE_JSON_INVALID',
    'native response encoding invalid':'NATIVE_RESPONSE_ENCODING_INVALID',
    'native transport failed':'NATIVE_TRANSPORT_FAILED',
    'redirect refused':'NATIVE_REDIRECT_REFUSED',
    'invalid request path':'NATIVE_ROUTE_INVALID',
}
CODES=HTTP_CODES+tuple(REQUEST_CODES.values())+('NONE','NATIVE_PENDING','NATIVE_TERMINAL_FAILED','BUILD_DEADLINE','REQUEST_FAILED','SCOPE_INVALID','FIREWALL_FAILED','UNKNOWN_EXECUTION_FAILURE','IMPORT_CONTRACT_REJECTED')
def error_code(exc):
    if isinstance(exc,kbc.CandidateImportContractError):return 'IMPORT_CONTRACT_REJECTED'
    if isinstance(exc,NativeHTTPError):return exc.code
    if isinstance(exc,FirewallError):return 'FIREWALL_FAILED'
    if isinstance(exc,kbc.CandidateBuildFailed):return 'NATIVE_TERMINAL_FAILED'
    if isinstance(exc,kbc.CandidatePending):return 'NATIVE_PENDING'
    if isinstance(exc,kbc.CandidateTimeout):return 'BUILD_DEADLINE'
    if isinstance(exc,kbc.CandidateLeak):return 'SCOPE_INVALID'
    if isinstance(exc,kbc.CandidateError):return REQUEST_CODES.get(str(exc),'REQUEST_FAILED')
    return 'UNKNOWN_EXECUTION_FAILURE'
def counts(transport):
    return {kb:{'imported':len(transport.imported[kb]),'completed':len(transport.completed[kb]),
                'failed':len(transport.failed[kb]),'pending':len(transport.imported[kb]-transport.completed[kb]-transport.failed[kb])}
            for kb in transport.servers}
def build_b(candidate,documents,transport,kb,path,firewall,timeout=TIMEOUT,poll_seconds=5):
    start=time.monotonic();deadline=start+timeout
    phase='IMPORT';code='NONE'
    def observe():
        firewall.health()
        ops.write_private_json(path,{'schema':'cwk.rt055.build-status.v1','candidate':'b','library':kb,
            'deadline_phase':phase,'error':code,'timeout_seconds':timeout,'elapsed_seconds':round(time.monotonic()-start,3),
            'libraries':counts(transport)})
    transport.observer=observe
    try:
        observe()
        try:candidate.build(documents,timeout=timeout)
        except kbc.CandidatePending:pass
        phase='READY_POLL'
        while not candidate.ready:
            code='NATIVE_PENDING';observe();remaining=deadline-time.monotonic()
            if remaining<=0:raise kbc.CandidateTimeout('build deadline')
            time.sleep(min(poll_seconds,remaining));remaining=deadline-time.monotonic()
            if remaining<=0:raise kbc.CandidateTimeout('build deadline')
            try:candidate.check_ready(min(120,remaining))
            except kbc.CandidatePending:continue
        phase='COMPLETE';code='NONE';observe()
    except Exception as exc:
        code=error_code(exc)
        # Failure observation must survive a failed firewall without raw error text.
        ops.write_private_json(path,{'schema':'cwk.rt055.build-status.v1','candidate':'b','library':kb,
            'deadline_phase':phase,'error':code,'timeout_seconds':timeout,'elapsed_seconds':round(time.monotonic()-start,3),
            'libraries':counts(transport)})
        raise
    finally:transport.observer=None
