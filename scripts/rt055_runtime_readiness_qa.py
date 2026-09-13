#!/usr/bin/env python3
"""PUBLIC synthetic regressions and behavior mutations, never OPS or queries."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import tempfile
import rt055_local_qa as qa

ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=(
 ('FREEZE_READINESS_DISCONNECTED','rt055_freeze.py',"policy_files=readiness.freeze_files(root,window_id,privacy_migration_id,before_status['started_at'])","policy_files=()"),
 ('SPAWN_BINDING_DISCONNECTED','rt055_runtime_readiness.py',"need(all(frozen.get('private_files',{}).get(p)==ops.sha_file(root/p) for p in paths))","pass"),
 ('CANONICAL_POLICY_CHECK_DISCONNECTED','rt055_runtime_readiness.py',"need(p.is_file() and p.read_text()==runtime.sandbox_text(root,kind))","pass"),
 ('NETWORK_GATE_CHECK_DISCONNECTED','rt055_runtime_readiness.py',"need(ops.read_json(base/'network-gate.json')==NETWORK)","pass"),
 ('READINESS_IDENTITY_DISCONNECTED','rt055_runtime_readiness.py',"need(row==expected)","pass"),
 ('SOURCE_BINDING_DISCONNECTED','rt055_runtime_readiness.py',"need(all(ops.sha_file(window.checked_path(root,'impl',n))==h for n,h in v['source_files'].items()))","pass"),
 ('BEFORE_ORDER_CHECK_DISCONNECTED','rt055_runtime_readiness.py',"need(row['ready_at']<before_started)","pass"),
 ('PREPARATION_AFTER_BEFORE_ALLOWED','rt055_runtime_readiness.py',"need(not w.exists())","pass"),
 ('EXPOSURE_MIGRATION_ALLOWED','rt055_runtime_readiness.py',"need(window.holdout_unexposed(root))","pass"),
 ('SYNTHETIC_ROOT_ALLOWED','rt055_runtime_readiness.py',"need(not (root/'audit/protected-root.json').exists())","pass"),
 ('RECEIPT_OMITTED_FROM_FREEZE','rt055_runtime_readiness.py',"return tuple(sorted((*row['files'],str((directory(root,wid)/'receipt.json').relative_to(root)))))","return tuple(sorted(row['files']))"),
 ('EXPOSURE_SCOPE_PROTECTION_REMOVED','rt055_runtime.py',"'builder','verifier','consumption','exposure','void-prequery',","'builder','verifier','consumption','void-prequery',"),
)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--red-log',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result={'schema':'cwk.rt055.runtime-policy-tests.v1','data_class':'PUBLIC_SYNTHETIC_ONLY_NOT_OPS',
            'baseline_commit':'518147a88f25d8f46f3f8d5a0f1ed6afd3b4b45e',
            'red':qa.stats(args.red_log.read_text(),1),'green':qa.run(ROOT),'mutations':[],
            'ops_formal_queries':0,'ops_candidate_processes':0,'ops_builder_runs':0,'ops_verifier_runs':0}
    if result['green']['exit_code']:raise RuntimeError('regression_failed')
    with tempfile.TemporaryDirectory(prefix='rt055-policy-mutations-') as td:
        for index,(name,filename,old,new) in enumerate(MUTATIONS):
            root=Path(td)/str(index)
            for folder in ('scripts','tests','RT/RT-055/contracts'):
                shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            p=root/'scripts'/filename;text=p.read_text()
            if text.count(old)!=1:raise RuntimeError('mutation_span_not_unique_'+name)
            p.write_text(text.replace(old,new));ast.parse(p.read_text())
            measured=qa.run(root,'test_rt055_runtime_readiness')
            detected=measured['exit_code']!=0 and measured['failures']+measured['errors']>0
            result['mutations'].append({'mutation':name,**measured,'detected':detected})
            print(name,'DETECTED' if detected else 'MISSED',flush=True)
            if not detected:raise RuntimeError('mutation_not_detected_'+name)
    result['restored_green']=qa.run(ROOT)
    if result['restored_green']['exit_code']:raise RuntimeError('restored_regression_failed')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print('SYNTHETIC_QA_PASS')

if __name__=='__main__':main()
