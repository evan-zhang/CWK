#!/usr/bin/env python3
"""Precise finally cleanup. Only this UUID and recorded created process IDs."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid
import rt055_opslib as ops
import rt055_window as window
import rt055_candidate_workspace as cw

def cleanup(root,window_id=None):
    w=window.directory(root,window_id) if window_id else root
    assert not root.is_symlink() and root.stat().st_mode & 0o077==0
    uuid.UUID(root.name.removeprefix('rt055-'))
    assert (root/'.rt055-owned').is_file()
    failures=0;processes=0
    for record in sorted((root/'resources').glob('process-*.json')):
        row=ops.read_json(record);pid=row['pid']
        if window_id:
            identity=row.get('workspace',{})
            if identity.get('window_id')!=window_id:continue
            space=cw.Workspace(root,window_id,identity.get('candidate'),identity.get('attempt_id'))
            if identity!=space.identity():failures+=1;continue
            if not space.base.exists():continue
            try:cw.validate(space)
            except (OSError,RuntimeError,ValueError):failures+=1;continue
        observed=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout.strip()
        if not observed:continue
        # PID reuse fails closed. Never terminate by scan/substring alone.
        if str(space.base if window_id else root) not in observed:continue  # unrelated PID reuse
        try:
            os.kill(pid,signal.SIGTERM)
            for _ in range(20):
                time.sleep(.5)
                if subprocess.run(['/bin/ps','-p',str(pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:break
            else:os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass
    # Each subtree was created only in this exclusive UUID root. Never touch
    # shared parent tooling, old pools, NAS or an index outside this data plane.
    for space in cw.owned(root,window_id) if window_id else []:
        try:cw.cleanup(space)
        except (OSError,RuntimeError):failures+=1
    # Legacy cleanup is available only outside a formal window. New formal
    # cleanup must not remove unowned/historical data trees.
    for name in (() if window_id else ('data-run','data-smoke')):
        path=root/name
        if path.is_symlink():failures+=1;continue
        if path.exists():
            try:shutil.rmtree(path)
            except OSError:failures+=1
    raw=subprocess.check_output(['/bin/ps','-axo','pid=,command='],text=True)
    for line in raw.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)==2 and int(parts[0])!=os.getpid() and str(root) in parts[1] and any(x in parts[1] for x in ('org.opensearch.bootstrap.OpenSearch','weknora-server','rt055_embed_sidecar.py')):processes+=1
    docker='/Applications/Docker.app/Contents/Resources/bin/docker'
    resources=0
    if Path(docker).is_file():
        for args in (['ps','-a','--format','{{.Names}}'],['volume','ls','--format','{{.Name}}'],['network','ls','--format','{{.Name}}']):
            p=subprocess.run([docker,*args],capture_output=True,text=True)
            if p.returncode:failures+=1
            else:resources+=sum(root.name in line for line in p.stdout.splitlines())
    owned_remaining=len(cw.owned(root,window_id)) if window_id else 0
    payload={'private_holdout_retained_on_ops':(root/'builder/private-corpus.json').is_file() and (root/'verifier/private-verified.json').is_file(),
             'temporary_indices_zero':not (root/'data-run').exists() and owned_remaining==0,'temporary_services_zero':processes==0,'temporary_containers_zero':resources==0,'cleanup_failures':failures,
             'related_processes':processes,'uuid_container_volume_network_resources':resources,'private_artifacts_retained':True}
    if window_id:
        payload.update(window.envelope(root,window_id,'cleanup'))
        window.write_once(w/'audit/cleanup.json',payload)
    else:
        window.write_once(root/'audit/cleanup.json',payload)
    return payload

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--window-id',required=True);args=parser.parse_args()
    result=cleanup(Path(__file__).resolve().parent.parent,args.window_id)
    return 0 if all(result[k] for k in ('temporary_indices_zero','temporary_services_zero','temporary_containers_zero')) and not result['cleanup_failures'] else 3
if __name__=='__main__':raise SystemExit(main())
