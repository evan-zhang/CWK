#!/usr/bin/env python3
"""Candidate implementer process; examines PUBLIC artifacts only, never cases."""
import json
import os
from pathlib import Path
import subprocess
import sys
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(HERE))
import rt055_opslib as ops

def main():
    upstream=ROOT/'weknora'
    def git(*args):return subprocess.check_output(['git','-C',str(upstream),*args],stderr=subprocess.DEVNULL,text=True).strip()
    pinned='8d7298fb5d759973cb1e481cadc5ecdf16dca599'
    assert git('rev-parse','HEAD')==pinned and not git('status','--porcelain')
    assert git('remote','get-url','origin') in ('https://github.com/Tencent/WeKnora.git','https://github.com/Tencent/WeKnora')
    assert subprocess.run(['git','-C',str(upstream),'merge-base','--is-ancestor',pinned,'origin/main'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    files=sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix in ('.py','.json'))
    ops.write_private_json(HERE/'implementation-prepared.json',{'prepared':True,'public_files':{p.name:ops.sha_file(p) for p in files},'upstream_clean':True,'upstream_reachable':True,'core_modified':False})
    return 0
if __name__=='__main__':raise SystemExit(main())
