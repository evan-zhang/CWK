#!/usr/bin/env python3
"""Public-only full-capacity reproducible benchmark; outputs aggregate JSON only.

No private paths, model calls or child workloads. Generated giants stay in RAM.
"""
import json
import resource
import sys
import time
import rt055_log_firewall as fw


def run():
    started=time.monotonic()
    # Six independent 48MiB leaves plus 6475 short needles = 6481, >285MiB.
    def values():
        for i in range(6):yield 'PUBLIC_%08d_'%i+'z'*(48*1024*1024-16)
        for i in range(6475):yield 'public-small-%08d'%i
    bank=fw.MemoryBank(values(),[])
    compiled=time.monotonic();assert len(bank.patterns)==6481 and bank.total>=285*1024*1024
    assert bank.maximum==48*1024*1024
    clean_seconds=0;redactions=0;input_bytes=0
    for pattern in bank.patterns[:6]:
        f=fw.Filter(bank);assert f.bank is bank
        assert f.feed(b'lead')==b''
        for offset in range(0,len(pattern),fw.CHUNK):assert f.feed(pattern[offset:offset+fw.CHUNK])==b''
        assert f.feed(b'tail')==b''
        t=time.monotonic();out=f.feed(b'',True);clean_seconds+=time.monotonic()-t
        assert out==b'lead'+fw.REPLACEMENT+b'tail'
        assert not any(p in out for p in bank.patterns)
        redactions+=f.redactions;input_bytes+=f.input_bytes
    small=b'|'.join(bank.patterns[6:]);f=fw.Filter(bank)
    for offset in range(0,len(small),fw.CHUNK):assert f.feed(small[offset:offset+fw.CHUNK])==b''
    out=f.feed(b'',True);assert f.redactions==6475 and not any(p in out for p in bank.patterns)
    redactions+=f.redactions;input_bytes+=f.input_bytes
    assert redactions==6481
    # Largest permitted no-newline input, all patterns included, no hit.
    f=fw.Filter(bank)
    for _ in range(fw.CAP//fw.CHUNK):f.feed(b'x'*fw.CHUNK)
    t=time.monotonic();out=f.feed(b'',True);nohit=time.monotonic()-t
    assert len(out)==fw.CAP and f.redactions==0
    del out,f
    # Real 64MiB+1 leaf rejection, and real >512MiB distinct bank rejection.
    limits=0
    try:fw.PatternBank([b'q'*(fw.MAX_LEAF+1)])
    except fw.FirewallError as e:assert e.code=='CAPACITY';limits+=1
    del bank
    def overflow():
        for i in range(11):yield b'PUBLIC_%08d_'%i+b'z'*(48*1024*1024-16)
    try:fw.PatternBank(overflow())
    except fw.FirewallError as e:assert e.code=='CAPACITY';limits+=1
    assert limits==2
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    assert rss<1536*1024*1024 and nohit<10 and clean_seconds<30
    return {'schema':'cwk.rt055.amendment14-capacity-qa.v1','status':'PASS','data_class':'PUBLIC_SYNTHETIC_ONLY',
        'needle_count':6481,'pattern_bytes':6*48*1024*1024+sum(len('public-small-%08d'%i) for i in range(6475)),
        'maximum_needle_bytes':48*1024*1024,'all_needles_exercised':True,'redactions':redactions,
        'input_bytes':input_bytes,'raw_files_written':0,'bank_object_shared':True,
        'actual_hard_cap_rejections':limits,'compile_seconds':round(compiled-started,4),
        'six_giant_clean_seconds':round(clean_seconds,4),'nohit_64mib_seconds':round(nohit,4),
        'elapsed_seconds':round(time.monotonic()-started,4),'peak_rss_bytes':rss,
        'rss_budget_bytes':1536*1024*1024,'bank_cap_bytes':fw.PATTERN_CAP,'leaf_cap_bytes':fw.MAX_LEAF,
        'stream_input_output_cap_bytes':fw.CAP,'raw_buffer_slots':12,'prefix_bytes':fw.PREFIX,
        'comparison_budget_bytes':fw.COMPARE_CAP,'candidate_budget_count':fw.CANDIDATE_CAP}


if __name__=='__main__':print(json.dumps(run(),sort_keys=True))
