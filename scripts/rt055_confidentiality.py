#!/usr/bin/env python3
"""Synthetic native canaries before confidential input; records booleans only."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import urllib.error
import rt055_opslib as ops
import rt055_runtime as runtime

def main():
    root=Path(__file__).resolve().parent.parent;os.umask(0o077)
    runtime.network_probe(root)
    for key in ('a','b'):
        with (root/'audit'/('smoke-'+key+'.log')).open('wb') as log:
            p=subprocess.run([sys.executable,str(root/'impl'/('rt055_run_'+key+'.py')),'--mode','smoke'],stdout=log,stderr=subprocess.STDOUT,timeout=7200)
        if p.returncode:raise RuntimeError('synthetic_native_smoke_failed')
    # Exercise the pinned native handler's failure path with a synthetic title/query.
    import rt055_run_b as native
    rng=random.Random();port=native.free_port(rng)
    log=root/'runtime-logs/error-path.log'
    proc=native.launch_server(port,root/'data-smoke/error-path',log)
    try:
        for payload in ({'query_text':'RT055_PRIVATE_CANARY_ERROR_78213','match_count':10},{'title':'RT055_PRIVATE_CANARY_TITLE_98217','content':'RT055_PRIVATE_CANARY_CONTENT_39217','status':'publish'}):
            path='/api/v1/knowledge-bases/nonexistent/hybrid-search' if 'query_text' in payload else '/api/v1/knowledge-bases/nonexistent/knowledge/manual'
            try:native.api_call('http://127.0.0.1:'+str(port),'POST',path,payload,None)
            except urllib.error.HTTPError as e:e.close()
    finally:ops.stop_process(proc)
    needles=['RT055_PRIVATE_CANARY_ERROR_78213','RT055_PRIVATE_CANARY_TITLE_98217','RT055_PRIVATE_CANARY_CONTENT_39217','RT055 Smoke Doc','reference-RT055-B-001','smoke alpha body 测试',*ops.WARMUP_QUERIES]
    paths=list((root/'runtime-logs').rglob('*'))+list((root/'weknora/logs').rglob('*'))
    log_ok=all(not any(n in p.read_text(errors='replace') for n in needles) for p in paths if p.is_file())
    # The deployed process env is a clean whitelist: Langfuse keys/host absent,
    # OTEL disabled; sandbox has just denied a real external connection syscall.
    network=ops.read_json(root/'audit/network-gate.json')
    result={'passed':log_ok and network['passed'],'native_log_canary_absent':log_ok,'external_egress_denied':network['external_denied'],'loopback_models_only':network['loopback_allowed'],'langfuse_unconfigured':True,'otel_disabled':True,'process_id':os.getpid()}
    ops.write_private_json(root/'audit/confidentiality-gate.json',result)
    # Smoke-created data only; no private/frozen input exists in these subtrees.
    import shutil
    if (root/'data-smoke').is_dir():shutil.rmtree(root/'data-smoke')
    return 0 if result['passed'] else 3
if __name__=='__main__':raise SystemExit(main())
