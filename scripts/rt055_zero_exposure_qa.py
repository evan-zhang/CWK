#!/usr/bin/env python3
"""Reproducible PUBLIC synthetic Amendment 4 regression and behavior mutations."""
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import rt055_local_qa as qa

ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=(
 ('SINGLE_LIBRARY_SCOPE_DISCONNECTED','kb_retrieval_candidates.py','expected_libraries=(library,),','expected_libraries=None,'),
 ('MISSING_CATEGORY_ACCEPTED','kb_retrieval_candidates.py',"if any(min(m['answerable_count'], m['exact_count'], m['no_answer_count']) <= 0 for m in metrics.values()):","if False:"),
 ('EXPOSURE_HOOK_DISCONNECTED','kb_retrieval_candidates.py','            before_first_search()','            pass'),
 ('LEGACY_VOID_GUARD_DISCONNECTED','rt055_window.py',"    if p.exists():\n        from rt055_zero_exposure import validate_void","    if False:\n        from rt055_zero_exposure import validate_void"),
 ('EXPOSURE_WRITE_DISCONNECTED','rt055_window.py',"    write_once(checked_path(root,'exposure',key,kb+'.json'),row)","    pass"),
 ('INCOMPLETE_EXPOSURE_REPLAY_ALLOWED','rt055_window.py',"if checked_path(root,'exposure',key,kb+'.json').exists():raise RuntimeError('exposure_without_completion_no_replay')","if False:raise RuntimeError('exposure_without_completion_no_replay')"),
 ('CLOSED_WINDOW_REOPENED','rt055_window.py',"if ((w/'status/baseline-after.claim').exists() or (w/'superseded-by-zero-exposure-migration.json').exists()):","if False:"),
 ('RESULT_CHAIN_DISCONNECTED','rt055_window.py',"if (any(v.get(k)!=x for k,x in expected.items())\n            or any(v['metrics'].get(k)!=x for k,x in score['metrics'].items())\n            or v['completed_at']<score['scored_at']):raise RuntimeError('library_result_identity_invalid')","if False:raise RuntimeError('library_result_identity_invalid')"),
 ('ONE_QUERY_VOID_ALLOWED','rt055_zero_exposure.py',"value['formal_query_calls']==0","value['formal_query_calls']>=0"),
 ('ONE_SCORE_VOID_ALLOWED','rt055_zero_exposure.py',"value['formal_score_receipts']==0","value['formal_score_receipts']>=0"),
 ('OPENED_HOLDOUT_VOID_ALLOWED','rt055_zero_exposure.py',"value.get('private_holdout_reopened_for_diagnosis') is False","True"),
 ('RAW_CALL_RECONCILIATION_IGNORED','rt055_zero_exposure.py',"value['formal_candidate_search_calls']==0","value['formal_candidate_search_calls']>=0"),
 ('LIBRARY_RESULT_COUNT_IGNORED','rt055_zero_exposure.py',"'results':len(list((w/('run-'+key)).glob('*.json')))","'results':0"),
 ('ARCHIVE_DIGEST_BINDING_IGNORED','rt055_zero_exposure.py',"_need(v['evidence_sha256']==ops.sha_file(m/'evidence.json'))","pass"),
)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--red-log',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result={'schema':'cwk.rt055.zero-exposure-scorer-tests.v1','data_class':'PUBLIC_SYNTHETIC_ONLY_NOT_OPS',
            'baseline_commit':'1af1362b57e639de87a35a9e5c2ce1af7f296da1',
            'red':qa.stats(args.red_log.read_text(),1),'green':qa.run(ROOT),'mutations':[],
            'formal_queries':0,'builder_runs':0,'verifier_runs':0}
    if result['green']['exit_code']:raise RuntimeError('regression_failed')
    with tempfile.TemporaryDirectory(prefix='rt055-zero-mutations-') as td:
        for index,(name,filename,old,new) in enumerate(MUTATIONS):
            root=Path(td)/str(index)
            for folder in ('scripts','tests','RT/RT-055/contracts'):
                shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            p=root/'scripts'/filename;s=p.read_text()
            if s.count(old)!=1:raise RuntimeError('mutation_span_not_unique_'+name)
            p.write_text(s.replace(old,new));ast.parse(p.read_text())
            measured=qa.run(root,'test_rt055_zero_exposure')
            detected=measured['exit_code']!=0 and measured['failures']+measured['errors']>0
            result['mutations'].append({'mutation':name,**measured,'detected':detected})
            print(name,'DETECTED' if detected else 'MISSED',flush=True)
            if not detected:raise RuntimeError('mutation_not_detected_'+name)
    result['restored_green']=qa.run(ROOT)
    if result['restored_green']['exit_code']:raise RuntimeError('restored_regression_failed')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print('SYNTHETIC_QA_PASS')

if __name__=='__main__':main()
