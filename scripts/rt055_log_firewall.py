"""Controller-only byte firewall. No patterns, digests or raw output receipts.

64 MiB input/output per stream, 512 MiB shared bank, 64 MiB longest leaf.
Drain into fixed controller memory; EOF exact leftmost/longest then write.
Factored <=16-byte prefix index, complete comparisons only at candidates.
12 raw buffers/controller; serialized EOF scrub; fixed work limits fail closed.
No line decoder, disk spool, queue, or subprocess receives the needle set.
"""
from __future__ import annotations
import os
import re
from types import MappingProxyType
from pathlib import Path
import select
import signal
import subprocess
import threading
import time
import rt055_window as window

CAP=64*1024*1024
PATTERN_CAP=512*1024*1024
MAX_LEAF=64*1024*1024
PATTERN_COUNT_CAP=16384
PREFIX=16
CANDIDATE_CAP=1024*1024
COMPARE_CAP=8*1024*1024*1024
_RAW_SLOTS=threading.BoundedSemaphore(12)
_CLEAN_LOCK=threading.Lock()
CHUNK=65536
REPLACEMENT=b'[RT055_REDACTED]'
ERRORS={'NONE','CAPACITY','DRAIN_ERROR','MISSING_EOF','CLOSE_ERROR','CHILD_NONZERO','NOT_STOPPED','UNVERIFIED','WORK_LIMIT'}
class FirewallError(RuntimeError):
    def __init__(self,code):
        self.code=code if code in ERRORS else 'UNVERIFIED'
        super().__init__('log_firewall_'+self.code.lower())

def _bytes(value):
    if isinstance(value,bytes):return value
    if isinstance(value,str):return value.encode('utf-8')
    raise FirewallError('UNVERIFIED')


def _prefix_regex(keys):
    # Only fixed <=16-byte prefixes enter the regex, never full long needles.
    # Factoring the trie bounds the index by O(PREFIX * pattern_count).
    trie={}
    for key in keys:
        node=trie
        for c in key:node=node.setdefault(c,{})
        node[None]={}
    def emit(node):
        parts=[re.escape(bytes([c]))+emit(child) for c,child in node.items() if c is not None]
        if None in node:parts.append(b'')
        return parts[0] if len(parts)==1 else b'(?:'+b'|'.join(parts)+b')'
    return re.compile(emit(trie))


class PatternValues:
    """Compatibility view: lazy strings, no second persistent plaintext bank."""
    __slots__=('bank',)
    def __init__(self,bank):self.bank=bank
    def __len__(self):return len(self.bank.patterns)
    def __iter__(self):return (p.decode('utf-8') for p in self.bank.patterns)
    def __contains__(self,value):return _bytes(value) in self.bank.patterns


class PatternBank:
    """One immutable exact-byte bank shared by all controller workspaces/streams."""
    __slots__=('patterns','maximum','total','index','short','matcher','_sealed')
    def __setattr__(self,name,value):
        if getattr(self,'_sealed',False):raise AttributeError('immutable_pattern_bank')
        object.__setattr__(self,name,value)
    def __init__(self,values):
        unique=set();total=0
        for value in values:
            p=_bytes(value)
            if not p:continue
            if len(p)>MAX_LEAF:raise FirewallError('CAPACITY')
            if p in unique:continue
            if total+len(p)>PATTERN_CAP or len(unique)>=PATTERN_COUNT_CAP:raise FirewallError('CAPACITY')
            unique.add(p);total+=len(p)
        if not unique:raise FirewallError('CAPACITY')
        # Sorting references only; no stream ever copies these byte strings.
        self.patterns=tuple(sorted(unique,key=lambda p:(-len(p),p)))
        self.maximum=len(self.patterns[0]);self.total=total
        index={};short={}
        for p in self.patterns:
            if len(p)>=PREFIX:index.setdefault(p[:PREFIX],[]).append(p)
            else:short.setdefault(p[0],[]).append(p)
        self.index=MappingProxyType({k:tuple(v) for k,v in index.items()})
        self.short=MappingProxyType({k:tuple(v) for k,v in short.items()})
        self.matcher=(_prefix_regex([*index,*(p for ps in short.values() for p in ps)]),)
        self._sealed=True
    def extend(self,values):
        # Compile only genuinely additional public/derived needles. Full bank
        # bytes and its compiled matcher remain shared, including current checks.
        if len(self.matcher)!=1:raise FirewallError('UNVERIFIED')
        existing=set(self.patterns);extra=set();total=self.total
        for value in values:
            p=_bytes(value)
            if not p or p in existing or p in extra:continue
            if len(p)>MAX_LEAF or total+len(p)>PATTERN_CAP or len(existing)+len(extra)>=PATTERN_COUNT_CAP:
                raise FirewallError('CAPACITY')
            extra.add(p);total+=len(p)
        if not extra:return self
        additional=PatternBank(extra)
        for name in ('index','short'):
            combined=dict(getattr(self,name))
            for key,rows in getattr(additional,name).items():
                combined[key]=tuple(sorted(combined.get(key,())+rows,key=lambda p:-len(p)))
            object.__setattr__(additional,name,MappingProxyType(combined))
        object.__setattr__(additional,'patterns',tuple(sorted(self.patterns+additional.patterns,key=lambda p:-len(p))))
        object.__setattr__(additional,'maximum',max(self.maximum,additional.maximum))
        object.__setattr__(additional,'total',self.total+additional.total)
        object.__setattr__(additional,'matcher',self.matcher+additional.matcher)
        return additional
    @property
    def values(self):return PatternValues(self)
    def clean(self,raw,end,cap):
        pos=0;copied=0;out=bytearray();redactions=0;candidates=0;compared=0
        def append(value):
            if len(out)+len(value)>cap:raise FirewallError('CAPACITY')
            out.extend(value)
        while pos<end:
            matches=[m for r in self.matcher if (m:=r.search(raw,pos,end)) is not None]
            match=min(matches,key=lambda m:m.start()) if matches else None
            if match is None:break
            at=match.start();candidates+=1
            if candidates>CANDIDATE_CAP:raise FirewallError('WORK_LIMIT')
            found=None
            # Long bucket and short closed set are both checked. Regex branch
            # choice cannot hide a longer full match at this same position.
            for group in (self.index.get(bytes(raw[at:at+PREFIX]),()),self.short.get(raw[at],())):
                for p in group:
                    if at+len(p)>end:continue
                    compared+=len(p)
                    if compared>COMPARE_CAP:raise FirewallError('WORK_LIMIT')
                    if raw.startswith(p,at,end):found=p;break
                if found is not None:break
            if found is None:
                pos=at+1  # failed full comparison must not skip overlapping prefixes
                continue
            append(memoryview(raw)[copied:at]);append(REPLACEMENT)
            redactions+=1;pos=at+len(found);copied=pos
        append(memoryview(raw)[copied:end])
        return out,redactions


def as_bank(values):
    if isinstance(values,PatternBank):return values
    if isinstance(values,PatternValues):return values.bank
    return PatternBank(values)


def byte_patterns(values):
    if isinstance(values,(PatternBank,PatternValues)):return as_bank(values).patterns
    return tuple({_bytes(v) for v in values if v})


class Filter:
    def __init__(self,values,cap=CAP,*,patterns=None):
        if not 0<cap<=CAP:raise FirewallError('CAPACITY')
        self.bank=as_bank(patterns if patterns is not None else values)
        self.patterns=self.bank.patterns;self.maximum=self.bank.maximum;self.cap=cap
        self._raw=None;self._slot=None;self.input_bytes=0;self.output_bytes=0;self.redactions=0;self.done=False
    @property
    def buffer(self):return memoryview(self._raw)[:self.input_bytes] if self._raw is not None else b''
    def discard(self):
        self._raw=None
        if getattr(self,'_slot',None) is not None:self._slot.release();self._slot=None
    def __del__(self):self.discard()
    def feed(self,data,eof=False):
        try:
            if self.done:raise FirewallError('UNVERIFIED')
            if len(data)>CHUNK or self.input_bytes+len(data)>self.cap:raise FirewallError('CAPACITY')
            if data and self._raw is None:
                if not _RAW_SLOTS.acquire(blocking=False):raise FirewallError('CAPACITY')
                self._slot=_RAW_SLOTS;self._raw=bytearray(self.cap)
            if data:self._raw[self.input_bytes:self.input_bytes+len(data)]=data
            self.input_bytes+=len(data)
            if not eof:return b''
            self.done=True
            # Drains keep reading independently. Serialize only EOF scrubbing,
            # so at most one <=64MiB sanitized output allocation exists here.
            with _CLEAN_LOCK:
                result,self.redactions=self.bank.clean(self._raw or bytearray(),self.input_bytes,self.cap)
            self.output_bytes=len(result);self.discard();return result
        except BaseException:
            self.done=True;self.discard();raise

class Stream:
    def __init__(self,path,values,cap=CAP,*,patterns=None):
        self.path=Path(path);self.filter=Filter(values,cap,patterns=patterns)
        self.cap=cap;self.input_bytes=0;self.output_bytes=0
        self.error='NONE';self.eof=False;self.closed=False;self.finalized=False
        self.stop=threading.Event();self.proc=None;self.thread=None;self.pipe=None
        self.sink=self.path.open('xb',buffering=0);os.chmod(self.path,0o600)
    def fail(self,code):
        if self.error=='NONE':self.error=code
        already_stopping=self.stop.is_set();self.stop.set()
        # Only this controller-created, new-session process group is eligible.
        if not already_stopping and self.proc is not None and self.proc.poll() is None:
            try:os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
    def attach(self,proc):
        self.proc=proc;self.pipe=proc.stdout
        if self.pipe is None:raise FirewallError('UNVERIFIED')
        os.set_blocking(self.pipe.fileno(),False)
        self.thread=threading.Thread(target=self.drain,name='rt055-log-drain',daemon=True)
        try:self.thread.start()
        except BaseException:
            self.thread=None;self.fail('DRAIN_ERROR')
            for f in (self.pipe,self.sink):
                try:f.close()
                except BaseException:pass
            self.closed=self.pipe.closed and self.sink.closed
            raise FirewallError('DRAIN_ERROR') from None
    def drain(self):
        try:
            while not self.stop.is_set():
                if not select.select([self.pipe],[],[],.1)[0]:continue
                data=os.read(self.pipe.fileno(),CHUNK)
                self.input_bytes+=len(data)
                if not data:self.eof=True
                clean=self.filter.feed(data,eof=not data)
                if self.output_bytes+len(clean)>self.cap:raise FirewallError('CAPACITY')
                if clean:
                    view=memoryview(clean)
                    while view:
                        written=self.sink.write(view)
                        if not written:raise OSError("short_write")
                        self.output_bytes+=written
                        view=view[written:]
                if not data:self.eof=True;break
        except FirewallError as exc:self.fail(exc.code)
        except BaseException:self.fail('DRAIN_ERROR')
        finally:
            self.filter.discard()
            for f in (self.pipe,self.sink):
                if f is not None:
                    try:
                        if f is self.sink:os.fsync(f.fileno())
                    except BaseException:self.fail('CLOSE_ERROR')
                    try:f.close()
                    except BaseException:
                        self.fail('CLOSE_ERROR')
                        try:os.close(f.fileno())
                        except BaseException:pass
            self.closed=all(f is None or f.closed for f in (self.pipe,self.sink))
    def finish(self,timeout=90):
        if not self.finalized:
            if self.proc is None:
                self.fail('UNVERIFIED')
                try:self.sink.close()
                except BaseException:self.fail('CLOSE_ERROR')
                self.closed=True
            else:
                if self.proc.poll() is None:self.fail('NOT_STOPPED')
                if self.thread:self.thread.join(timeout)
                if self.thread and self.thread.is_alive():
                    self.fail('MISSING_EOF');self.thread.join(1)
                if not self.eof and self.error=='NONE':self.fail('MISSING_EOF')
                if self.proc.poll() not in (0,None) and not getattr(self.proc,'_rt055_controller_stop',False):self.fail('CHILD_NONZERO')
            if not self.closed:self.fail('CLOSE_ERROR')
            self.finalized=True
        return self.receipt()
    def receipt(self):
        return {'input_bytes':self.input_bytes,'output_bytes':self.output_bytes,
                'redaction_count':self.filter.redactions,'overflow':self.error=='CAPACITY',
                'error':self.error,'eof':self.eof,'closed':self.closed,
                'verified':self.finalized and self.eof and self.closed and self.error=='NONE'}

# Parent memory only, never serialize this registry or include it in repr.
_CONTEXTS={}
class Firewall:
    def __init__(self,space,values):
        self.space=space;self.bank=as_bank(values);self.values=self.bank.values;self.patterns=self.bank.patterns
        self.streams={};self.finalized=False
    def spawn(self,argv,path,kwargs):
        from rt055_candidate_workspace import require_path
        require_path(self.space,path,'logs')
        if self.finalized or path in self.streams:raise FirewallError('UNVERIFIED')
        if any(k in kwargs for k in ('stdout','stderr','text','encoding','errors')):raise FirewallError('UNVERIFIED')
        stream=Stream(path,self.bank);self.streams[path]=stream
        try:
            proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True,**kwargs)
            proc._rt055_firewall=stream;stream.attach(proc);return proc
        except BaseException:
            stream.fail('UNVERIFIED')
            if stream.proc is not None:
                try:stream.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:stream.fail('NOT_STOPPED')
            stream.finish();raise FirewallError('UNVERIFIED') from None
    def health(self):
        if not self.streams:raise FirewallError('UNVERIFIED')
        for stream in self.streams.values():
            if stream.error!='NONE':raise FirewallError(stream.error)
            if stream.proc.poll() is not None:raise FirewallError('CHILD_NONZERO')
    def finalize(self):
        if not self.finalized:
            rows=[{'log_identity':str(p.relative_to(self.space.base)),**s.finish()} for p,s in self.streams.items()]
            self.row={'schema':'cwk.rt055.log-firewall.v1','verified':bool(rows) and all(r['verified'] for r in rows),'logs':rows}
            window.write_once(self.space.ledger.parent/'log-firewall.json',self.row);self.finalized=True
            # Immutable shared bank remains controller-only; release removes workspace ownership.
        if not self.row['verified']:raise FirewallError('UNVERIFIED')
        return self.row

def bind(space,values):
    key=str(space.base)
    if key in _CONTEXTS:raise FirewallError('UNVERIFIED')
    f=Firewall(space,values);_CONTEXTS[key]=f;return f

def get(space):
    try:return _CONTEXTS[str(space.base)]
    except KeyError:raise FirewallError('UNVERIFIED') from None

def finalize(space):return get(space).finalize()

def verified_paths(space):
    f=get(space)
    if not f.finalized or not f.row['verified']:raise FirewallError('UNVERIFIED')
    return set(f.streams)

def release(space):_CONTEXTS.pop(str(space.base),None)

def run_command(argv,path,values,**kwargs):
    """Controller subprocess boundary, using the identical stream implementation."""
    stream=Stream(path,values)
    try:
        proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True,**kwargs)
        proc._rt055_firewall=stream;stream.attach(proc);code=proc.wait()
    finally:
        if stream.proc is not None:
            import rt055_opslib as ops
            ops.stop_process(stream.proc)
        row=stream.finish()
        row={'schema':'cwk.rt055.controller-log-firewall.v1','log_identity':Path(path).name,**row}
        body=Path(path).read_bytes()
        row['post_scan_hits']=sum(p in body for p in byte_patterns(values))
        window.write_once(Path(str(path)+'.firewall.json'),row)
    if not row['verified'] or row['post_scan_hits']:return code or 3
    return code


class MemoryBank(PatternBank):
    """All string leaves and both JSON encodings; only deduplicated bytes persist."""
    __slots__=('__weakref__',)
    def __init__(self, private_json, public_values):
        import json
        def leaves(x):
            if isinstance(x,str):
                if x:yield x
            elif isinstance(x,dict):
                for v in x.values():yield from leaves(v)
            elif not isinstance(x,(bytes,int,float,bool,type(None))):
                for v in x:yield from leaves(v)
        def patterns():
            for x in (private_json,public_values):
                for value in leaves(x):
                    yield value.encode('utf-8')
                    for ascii in (False,True):yield json.dumps(value,ensure_ascii=ascii)[1:-1].encode('utf-8')
        super().__init__(patterns())
    def receipt(self):
        return {'needle_count':len(self.patterns),'pattern_bytes':self.total,
                'maximum_needle_bytes':self.maximum,
                'all_string_leaves':True,'json_escaped_variants':True,
                'controller_memory_only':True,'values_serialized':False}
