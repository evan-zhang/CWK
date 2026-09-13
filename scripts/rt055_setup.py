#!/usr/bin/env python3
"""OPS private tool clone/setup. No source corpus read, no production writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parent

def main():
    os.umask(0o077)
    fd=os.open(ROOT/'status/setup.claim',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    state={'status':'RUNNING','phase':'CLONE_TOOLCHAIN','process_id':os.getpid(),'started_at':time.time()}
    def save():
        tmp=ROOT/'status/setup.next';tmp.write_text(json.dumps(state));os.replace(tmp,ROOT/'status/setup.json')
    def run(argv,**kwargs):
        with (ROOT/'audit/setup-private.log').open('ab') as out:subprocess.run(argv,stdout=out,stderr=subprocess.STDOUT,check=True,**kwargs)
    try:
        save();base=ROOT.parent
        for name in ('jdk','opensearch','bin','downloads'):
            if (ROOT/name).exists():raise RuntimeError('tool_destination_exists')
            run(['/bin/cp','-cR',str(base/name),str(ROOT/name)],timeout=600)
        # New checkout; never execute from the shared, previously used checkout.
        run(['git','clone','--no-hardlinks','--no-checkout',str(base/'weknora'),str(ROOT/'weknora')],timeout=600)
        run(['git','-C',str(ROOT/'weknora'),'remote','set-url','origin','https://github.com/Tencent/WeKnora.git'],timeout=30)
        run(['git','-C',str(ROOT/'weknora'),'checkout','--detach','8d7298fb5d759973cb1e481cadc5ecdf16dca599'],timeout=60)
        # Native distribution may need relative logs even with explicit path.logs.
        (ROOT/'opensearch/logs').mkdir(exist_ok=True)
        for name in ('data','data-run','logs'):
            # Do not retain old native data from a tool distribution clone.
            path=ROOT/'opensearch'/name
            if path.exists() and name=='data':shutil.rmtree(path)
        state['phase']='PYTHON_DEPENDENCIES';save()
        (ROOT/'sidecar').mkdir(mode=0o700,exist_ok=True)
        run([sys.executable,'-m','venv',str(ROOT/'sidecar/venv')],timeout=120)
        run([str(ROOT/'sidecar/venv/bin/python'),'-m','pip','install','-r',str(base/'sidecar/requirements-freeze.txt')],timeout=1800)
        shutil.copyfile(base/'sidecar/requirements-freeze.txt',ROOT/'sidecar/requirements-freeze.txt')
        state['phase']='PUBLIC_MODEL_WEIGHTS';save()
        env=dict(os.environ);env.update(HF_HOME=str(ROOT/'sidecar/hf'),HF_ENDPOINT='https://hf-mirror.com')
        # Only public model download. Actual candidate service is offline and sandboxed.
        run([str(ROOT/'sidecar/venv/bin/python'),'-c','from huggingface_hub import snapshot_download; snapshot_download("BAAI/bge-m3", ignore_patterns=["onnx/*","openvino/*"])'],env=env,timeout=7200)
        state.update(status='PASS',phase='READY',finished_at=time.time());save();return 0
    except Exception as e:state.update(status='FAIL',error_kind=type(e).__name__,finished_at=time.time());save();return 2
if __name__=='__main__':raise SystemExit(main())
