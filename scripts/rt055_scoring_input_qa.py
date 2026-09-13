#!/usr/bin/env python3
"""Local public-only contract regression and source-copy behavior mutations."""
from pathlib import Path
import argparse
import json
import shutil
import tempfile
import py_compile
import rt055_local_qa as qa
ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=(
 ('DUPLICATE_IDENTITY_ALLOWED','kb_retrieval_candidates.py','        if identity in seen:', '        if False:'),
 ('CONFLICTING_QUERY_ALLOWED','kb_retrieval_candidates.py','        if query_key in queries and (not explicit or queries[query_key] != semantics):','        if False:'),
 ('IDENTITY_LOST_BY_LOADER','rt055_scoring_input.py',"row['exact'], row['ordinal']))", "row['exact']))"),
 ('EXACT_COERCED','rt055_scoring_input.py',"            need(type(row.get('exact')) is bool)","            row['exact'] = bool(row['exact'])"),
 ('REPR_DISCLOSES_IDENTITY','kb_retrieval_candidates.py','trial_id: int | None = field(default=None, repr=False)','trial_id: int | None = field(default=None)'),
 ('NO_EVIDENCE_ORIGIN_CONFLICT_ALLOWED','rt055_scoring_input.py','            need(key not in semantics or semantics[key] == meaning)','            pass'),
 ('MAIN_BEFORE_GATE_DISCONNECTED','rt055_baseline.py','        before_precheck(ROOT,args.window_id)','        pass'),
 ('COORDINATOR_GATE_DISCONNECTED','rt055_formal_coordinator.py','        coordinator_precheck(ROOT,args.window_id,args.privacy_migration_id)','        pass'),
 ('FREEZE_READINESS_BINDING_LOST','rt055_freeze.py','+policy_files+scoring_files','+policy_files'),
 ('BEFORE_ORDERING_IGNORED','rt055_scoring_input.py',"    need(row['ready_at'] < before_started)",'    pass'),
 ('INPUT_TOTALS_IGNORED','rt055_scoring_input.py',"        need(m['total_count'] == checks['library_validity'][kb]['total_count'])",'        pass'),
)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result={'schema':'cwk.rt055.scoring-input-tests.v1','data_class':'PUBLIC_SYNTHETIC_ONLY',
            'red':{'tests':1,'errors':1,'failure':'DUPLICATE_QUERY_PREEXPOSURE'},
            'green':qa.run(ROOT),'mutations':[],'formal_queries':0,'ops_calls':0}
    if result['green']['exit_code']:raise RuntimeError('regression_failed')
    with tempfile.TemporaryDirectory(prefix='rt055-input-mutation-') as tmp:
        base=Path(tmp)
        for name,filename,old,new in MUTATIONS:
            target=base/name
            shutil.copytree(ROOT/'scripts',target/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT/'tests',target/'tests',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT/'RT/RT-055',target/'RT/RT-055')
            path=target/'scripts'/filename;text=path.read_text()
            if text.count(old)!=1:raise RuntimeError('mutation_span_invalid')
            path.write_text(text.replace(old,new));py_compile.compile(str(path),doraise=True)
            stats=qa.run(target,'test_rt055_scoring_input')
            detected=stats['exit_code']!=0 and stats['failures']+stats['errors']>0
            result['mutations'].append({'name':name,'detected':detected,**stats})
            if not detected:raise RuntimeError('mutation_not_detected_'+name)
    result['restored_green']=qa.run(ROOT)
    if result['restored_green']['exit_code']:raise RuntimeError('restored_regression_failed')
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0
if __name__=='__main__':raise SystemExit(main())
