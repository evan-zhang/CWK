"""Amendment 3 closed, candidate-independent capacity contract. Pure functions."""
from __future__ import annotations
TIERS = ('T3','T2','T1')
FIELDS = {'T3':('doc_ids','queries','tokens'),'T2':('doc_ids','queries'),'T1':('doc_ids',)}
CATEGORIES = ('title_filename','exact_identifier_date','body_only_rare_phrase','table_row','no_answer_mutation','near_neighbour')
FLOORS = dict(zip(CATEGORIES,(3,2,3,3,3,3)))
TARGETS = dict(zip(CATEGORIES,(10,8,10,4,5,5)))
TOTAL_FLOOR = 16
ROLE_LEVELS = ('PROCESS_LEVEL_SEPARATION_SINGLE_UID','OS_UID_SEPARATION')

def fields(tier):
    if tier not in TIERS: raise ValueError('tier_invalid')
    return FIELDS[tier]

def complete_counts(counts):
    if not isinstance(counts, dict) or not set(counts) <= set(CATEGORIES):
        raise ValueError('category_counts_invalid')
    return {c:counts.get(c,0) for c in CATEGORIES}

def valid_counts(counts):
    return (isinstance(counts,dict) and set(counts)==set(CATEGORIES)
            and all(type(v) is int and 0<=v<=TARGETS[k] for k,v in counts.items()))

def floor_pass(counts):
    return (valid_counts(counts) and sum(counts.values())>=TOTAL_FLOOR
            and all(counts[c]>=FLOORS[c] for c in CATEGORIES))

def trace_row(tier,counts):
    fields(tier)
    counts=complete_counts(counts)
    if not valid_counts(counts): raise ValueError('category_counts_invalid')
    return {'tier':tier,'category_counts':counts,'total_count':sum(counts.values()),'floor_pass':floor_pass(counts)}

def select_once(evaluate):
    """evaluate closes over one claimed build/snapshot/seed; never merges pools."""
    trace=[]
    for tier in TIERS:
        pool=evaluate(tier)
        row=trace_row(tier,pool['category_counts']);trace.append(row)
        if len(pool['cases']) != row['total_count']: raise ValueError('pool_count_mismatch')
        if row['floor_pass']:
            return {**pool,'status':'PARTICIPATING','tier':tier,'category_counts':row['category_counts'],
                    'trace':trace,'floors':dict(FLOORS),'targets':dict(TARGETS),'total_floor':TOTAL_FLOOR}
    # Discard all attempted case payloads. Keep only accounting metadata.
    return {**pool,'status':'DEFERRED','tier':None,'cases':[],
            'category_counts':dict.fromkeys(CATEGORIES,0),'trace':trace,
            'floors':dict(FLOORS),'targets':dict(TARGETS),'total_floor':TOTAL_FLOOR}

def validate_selection(v):
    required={'status','tier','category_counts','total_count','floors','total_floor','targets','trace','same_build_and_seed','complete_pool_verified'}
    if not isinstance(v,dict) or set(v)!=required: raise ValueError('selection_fields_invalid')
    if (v['floors']!=FLOORS or v['targets']!=TARGETS or type(v['total_floor']) is not int or v['total_floor']!=TOTAL_FLOOR
        or v['same_build_and_seed'] is not True or v['complete_pool_verified'] is not True): raise ValueError('selection_contract_invalid')
    trace=v['trace']
    if not isinstance(trace,list) or not 1<=len(trace)<=3: raise ValueError('tier_trace_invalid')
    for i,row in enumerate(trace):
        if (not isinstance(row,dict) or set(row)!={'tier','category_counts','total_count','floor_pass'}
            or row['tier']!=TIERS[i] or not valid_counts(row['category_counts'])
            or type(row['total_count']) is not int or row['total_count']!=sum(row['category_counts'].values())
            or row['floor_pass'] is not floor_pass(row['category_counts'])): raise ValueError('tier_trace_invalid')
        if i<len(trace)-1 and row['floor_pass']: raise ValueError('tier_skipped_first_valid')
    if not valid_counts(v['category_counts']) or type(v['total_count']) is not int or v['total_count']!=sum(v['category_counts'].values()): raise ValueError('final_counts_invalid')
    if v['status']=='PARTICIPATING':
        if (not trace[-1]['floor_pass'] or v['tier']!=trace[-1]['tier'] or v['category_counts']!=trace[-1]['category_counts']): raise ValueError('participation_invalid')
    elif v['status']=='DEFERRED':
        if len(trace)!=3 or trace[-1]['floor_pass'] or v['tier'] is not None or v['total_count']!=0: raise ValueError('deferred_invalid')
    else: raise ValueError('library_status_invalid')

def public_selection(generated, *, verified):
    return {k:generated[k] for k in ('status','tier','category_counts','floors','total_floor','targets','trace')} | {
        'total_count':len(generated['cases']),'same_build_and_seed':verified,'complete_pool_verified':verified}
