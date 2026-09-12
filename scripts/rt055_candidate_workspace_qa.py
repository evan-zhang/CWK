#!/usr/bin/env python3
"""Public-only regression and behavior-mutation evidence for Amendment 6."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import tempfile
import rt055_local_qa as qa
ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=(
 ('CROSS_WINDOW_GUARD_REMOVED','rt055_candidate_workspace.py',"if window_id is not None and s.window_id!=window_id:raise RuntimeError('candidate_workspace_cross_window')","if False:raise RuntimeError('candidate_workspace_cross_window')",'test_rt055_candidate_workspace'),
 ('SYMLINK_GUARD_REMOVED','rt055_candidate_workspace.py',"if node.is_symlink():raise RuntimeError('candidate_workspace_symlink')","if False:raise RuntimeError('candidate_workspace_symlink')",'test_rt055_candidate_workspace'),
 ('OWNERSHIP_GUARD_REMOVED','rt055_candidate_workspace.py',"if value!={**s.identity(),'device':st.st_dev,'inode':st.st_ino,'uid':st.st_uid} or st.st_mode&0o777!=0o700:","if False:",'test_rt055_candidate_workspace'),
 ('BROAD_RUNTIME_DELETION','rt055_candidate_workspace.py','validate(s);shutil.rmtree(s.base)',"validate(s);shutil.rmtree(s.root/'candidate-runtime')",'test_rt055_candidate_workspace'),
 ('LOG_HARD_FAILURE_REMOVED','rt055_candidate_workspace.py',"if not row['passed']:raise RuntimeError('candidate_log_privacy_failed')","if False:raise RuntimeError('candidate_log_privacy_failed')",'test_rt055_candidate_workspace'),
 ('SIBLING_READ_ISOLATION_REMOVED','rt055_candidate_workspace.py',"text+='(deny file-read* (subpath %s))\\n'%json.dumps(str((s.root/'candidate-runtime').resolve()))","text+='\\n'",'test_rt055_candidate_workspace'),
 ('FORMAL_LEDGER_SCOPE_REMOVED','rt055_runtime.py',"'zero-exposure-migrations','formal-windows',","'zero-exposure-migrations',",'test_rt055_candidate_workspace'),
 ('MAIN_STARTUP_SOURCE_CHECK_REMOVED','rt055_candidate_startup.py',"row!=expected or not 0<row['started_at']", "False or not 0<row['started_at']",'test_rt055_candidate_workspace'),
 ('A_LOG_SCAN_DISCONNECTED','rt055_run_a.py','workspace_api.scan(workspace,workspace_api.needles(corpus,cases));log_scanned=True','log_scanned=True','test_rt055_formal_window'),
 ('B_LOG_SCAN_DISCONNECTED','rt055_run_b.py','workspace_api.scan(workspace,workspace_api.needles(corpus,cases));log_scanned=True','log_scanned=True','test_rt055_formal_window'),
)
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result={'schema':'cwk.rt055.candidate-workspace-tests.v1','data_class':'PUBLIC_SYNTHETIC_ONLY_NOT_OPS','green':qa.run(ROOT),'mutations':[],'formal_queries':0}
    if result['green']['exit_code']:raise RuntimeError('regression_failed')
    with tempfile.TemporaryDirectory(prefix='rt055-workspace-mutations-') as td:
        for i,(name,filename,old,new,tests) in enumerate(MUTATIONS):
            root=Path(td)/str(i)
            for folder in ('scripts','tests','RT/RT-055/contracts'):
                shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            target=root/'scripts'/filename;text=target.read_text()
            if text.count(old)!=1:raise RuntimeError('mutation_span_'+name)
            target.write_text(text.replace(old,new));ast.parse(target.read_text())
            row=qa.run(root,tests);detected=row['exit_code']!=0 and row['failures']+row['errors']>0
            result['mutations'].append({'mutation':name,**row,'detected':detected});print(name,detected,flush=True)
            if not detected:raise RuntimeError('mutation_missed_'+name)
    result['restored_green']=qa.run(ROOT)
    if result['restored_green']['exit_code']:raise RuntimeError('restored_failed')
    a.output.write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__':main()
