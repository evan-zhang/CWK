#!/usr/bin/env python3
"""Amendment 7 OPS-private input readiness: pure validation, no search/launch.

Receipts are append-only under the already-protected window policy directory.
Only a separate closed aggregate projection may leave OPS; all input/source
file digests, private ordinals and row values stay inside this private receipt.
"""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
import time
import kb_retrieval_candidates as kbc
import rt055_opslib as ops
import rt055_window as window
import rt055_runtime_readiness as runtime_ready

SCHEMA = 'cwk.rt055.scoring-input-readiness.v1'
INPUTS = ('verifier/private-verified.json', 'verifier/case-verification.json', 'builder/private-corpus.json')


def need(value):
    if not value:
        raise RuntimeError('scoring_input_readiness_invalid')


def load_cases(verified):
    """Strict shared A/B loader; never coerce exact/ordinal or invent identity."""
    result = []; semantics = {}
    for kb, lib in verified['libraries'].items():
        for row in lib['cases']:
            need(row.get('kb_id') == kb and type(row.get('ordinal')) is int and row['ordinal'] >= 0)
            need(row.get('expected_outcome') in ('hit', 'no_evidence'))
            need(isinstance(row.get('expected_doc_id'), str) and row['expected_doc_id'].strip())
            need(type(row.get('exact')) is bool)
            expected = frozenset(() if row['expected_outcome'] == 'no_evidence' else (row['expected_doc_id'],))
            kbc._query_scope(row['query'], kb)
            key = (kb,row['query']); meaning = (row['expected_doc_id'],row['exact'],row['expected_outcome'])
            need(key not in semantics or semantics[key] == meaning)
            semantics[key] = meaning
            result.append(kbc.Case(kb, row['query'], expected, row['exact'], row['ordinal']))
    return result


def inspect_inputs(root):
    """Only controller reads verified rows; no builder/verifier execution."""
    for name in INPUTS:
        window.checked_path(root, name)
    before = {name: ops.sha_file(root/name) for name in INPUTS}
    verified = ops.read_json(root/INPUTS[0]); checks = ops.read_json(root/INPUTS[1])
    need(checks.get('verified') is True and set(verified.get('libraries', {})) == set(ops.LIBRARIES))
    need(checks.get('participating_libraries') == list(ops.LIBRARIES) and checks.get('deferred_libraries') == [])
    cases = load_cases(verified)
    from rt055_candidate_workspace import needles
    from rt055_log_firewall import Filter
    corpus=ops.read_json(root/'builder/private-corpus.json')
    Filter(needles(corpus,cases)+needles(verified,[])+list(ops.WARMUP_QUERIES))
    metrics = kbc.validate_cases(cases, require_trial_identity=True)
    summary = {}
    for kb in ops.LIBRARIES:
        m = metrics[kb]; rows = [c for c in cases if c.kb_id == kb]
        need(m['total_count'] == checks['library_validity'][kb]['total_count'])
        groups = Counter(c.query for c in rows)
        summary[kb] = {'total_count': m['total_count'],
                       'duplicate_groups': sum(n > 1 for n in groups.values()),
                       'duplicate_extra_rows': sum(n-1 for n in groups.values()),
                       'semantics_consistent': True, 'ordinal_unique': True,
                       'positive_category_denominators': True}
    need(before == {name: ops.sha_file(root/name) for name in INPUTS})
    return summary, before


def directory(root, wid):
    return window.checked_path(root, str(runtime_ready.directory(root, wid).relative_to(root)), 'scoring-input')


def verify(root, wid, mid=None):
    base = directory(root, wid)
    for name in ('claim.json', 'receipt.json'):
        window.checked_path(root, str((base/name).relative_to(root)))
    row = ops.read_json(base/'receipt.json'); mid = mid or row['migration_id']
    dp, dep = runtime_ready.deployment(root, mid)
    summary, files = inspect_inputs(root)
    files[str(dp.relative_to(root))] = ops.sha_file(dp)
    need(ops.read_json(base/'claim.json') == {**window.envelope(root,wid,'scoring-input-readiness'), 'migration_id':mid})
    files[str((base/'claim.json').relative_to(root))] = ops.sha_file(base/'claim.json')
    expected = {'schema':SCHEMA, **window.envelope(root,wid,'scoring-input-readiness'),
                'migration_id':mid, 'source_commit':dep['code_commit'], 'source_files':dep['source_files'],
                'root':str(root.resolve()), 'files':files, 'libraries':summary,
                'validation':'PURE_INPUT_ONLY_NO_CANDIDATE_NO_QUERY', 'status':'PASS',
                'created_at':row.get('created_at'), 'ready_at':row.get('ready_at')}
    need(row == expected)
    need(type(row['created_at']) in (int,float) and type(row['ready_at']) in (int,float)
         and 0 < row['created_at'] <= row['ready_at'] <= time.time())
    return row


def prepare(root, wid, mid):
    base = directory(root,wid); window.require_open(root,wid)
    need(not window.directory(root,wid).exists() and window.holdout_unexposed(root))
    dp, dep = runtime_ready.deployment(root,mid)
    # Main runtime/startup readiness is independently required by freeze. It is
    # not a substitute for this real private-input preflight (nor vice versa).
    base.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    base.mkdir(mode=0o700,exist_ok=False)
    start = time.time()
    window.write_once(base/'claim.json',{**window.envelope(root,wid,'scoring-input-readiness'),'migration_id':mid})
    summary, files = inspect_inputs(root)
    files[str(dp.relative_to(root))] = ops.sha_file(dp)
    files[str((base/'claim.json').relative_to(root))] = ops.sha_file(base/'claim.json')
    window.write_once(base/'receipt.json',{'schema':SCHEMA,**window.envelope(root,wid,'scoring-input-readiness'),
        'migration_id':mid,'source_commit':dep['code_commit'],'source_files':dep['source_files'],
        'root':str(root.resolve()),'files':files,'libraries':summary,
        'validation':'PURE_INPUT_ONLY_NO_CANDIDATE_NO_QUERY','status':'PASS','created_at':start,'ready_at':time.time()})
    return verify(root,wid,mid)


def before_precheck(root,wid):
    window.require_open(root,wid)
    need(window.holdout_unexposed(root))
    row=verify(root,wid)
    from rt055_workload_readiness import verify as workload_verify
    workload_verify(root,row['migration_id'])
    return row


def freeze_files(root,wid,mid,before_started):
    row = verify(root,wid,mid)
    need(row['ready_at'] < before_started)
    return tuple(sorted((*row['files'],str((directory(root,wid)/'receipt.json').relative_to(root)))))


def coordinator_precheck(root,wid,mid):
    """Must precede even controller/candidate attempt claims; never Popen."""
    from rt055_freeze import verify_artifacts
    window.require_open(root,wid)
    need(window.holdout_unexposed(root))
    verify(root,wid,mid); window.verification(root,wid)
    need(window.receipt(root,wid)['privacy_migration_id'] == mid and verify_artifacts(root,wid,no_popen=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window-id',required=True);parser.add_argument('--migration-id',required=True)
    args=parser.parse_args();prepare(Path(__file__).resolve().parent.parent,args.window_id,args.migration_id)
    return 0

if __name__=='__main__':raise SystemExit(main())
