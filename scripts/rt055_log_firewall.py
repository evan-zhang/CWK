"""Controller-only byte firewall. No patterns, digests or raw output receipts.

64 MiB total input per stream, 64 MiB aggregate patterns, 8 MiB longest leaf.
Leftmost, longest-at-that-position replacement; retain an undecidable suffix.
No line decoder, disk spool, queue, or subprocess receives the needle set.
"""
from __future__ import annotations
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time
import rt055_window as window

CAP=64*1024*1024
MAX_LEAF=8*1024*1024
CHUNK=65536
REPLACEMENT=b'[RT055_REDACTED]'
ERRORS={'NONE','CAPACITY','DRAIN_ERROR','MISSING_EOF','CLOSE_ERROR','CHILD_NONZERO','NOT_STOPPED','UNVERIFIED'}
class FirewallError(RuntimeError):
    def __init__(self,code):
        self.code=code if code in ERRORS else 'UNVERIFIED'
        super().__init__('log_firewall_'+self.code.lower())

class Filter:
    def __init__(self,values,cap=CAP):
        patterns=tuple(sorted({v.encode('utf-8') for v in values if v},key=lambda b:(-len(b),b)))
        if not patterns or sum(map(len,patterns))>CAP or len(patterns[0])>MAX_LEAF:
            raise FirewallError('CAPACITY')
        if not 0<cap<=CAP:raise FirewallError('CAPACITY')
        self.patterns=patterns;self.maximum=len(patterns[0]);self.cap=cap
        self.buffer=b'';self.input_bytes=0;self.output_bytes=0;self.redactions=0
    def feed(self,data,eof=False):
        if len(data)>CHUNK or self.input_bytes+len(data)>self.cap:raise FirewallError('CAPACITY')
        self.input_bytes+=len(data);self.buffer+=data
        limit=len(self.buffer) if eof else max(0,len(self.buffer)-self.maximum+1)
        pos=0;out=[]
        while pos<limit:
            best=None
            for pattern in self.patterns:
                at=self.buffer.find(pattern,pos)
                if at>=0 and at<limit and (best is None or (at,-len(pattern))<(best[0],-len(best[1]))):best=(at,pattern)
            if best is None:out.append(self.buffer[pos:limit]);pos=limit;break
            at,pattern=best;out.extend((self.buffer[pos:at],REPLACEMENT));pos=at+len(pattern);self.redactions+=1
        self.buffer=self.buffer[pos:];result=b''.join(out);self.output_bytes+=len(result);return result

class Stream:
    def __init__(self,path,values,cap=CAP):
        self.path=Path(path);self.filter=Filter(values,cap)
        self.error='NONE';self.eof=False;self.closed=False;self.finalized=False
        self.stop=threading.Event();self.proc=None;self.thread=None;self.pipe=None
        self.sink=self.path.open('xb',buffering=0);os.chmod(self.path,0o600)
    def fail(self,code):
        if self.error=='NONE':self.error=code
        self.stop.set()
        # Only this controller-created, new-session process group is eligible.
        if self.proc is not None and self.proc.poll() is None:
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
                clean=self.filter.feed(data,eof=not data)
                if clean:
                    view=memoryview(clean)
                    while view:
                        written=self.sink.write(view)
                        if not written:raise OSError("short_write")
                        view=view[written:]
                if not data:self.eof=True;break
        except FirewallError as exc:self.fail(exc.code)
        except BaseException:self.fail('DRAIN_ERROR')
        finally:
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
    def finish(self,timeout=5):
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
        return {'input_bytes':self.filter.input_bytes,'output_bytes':self.filter.output_bytes,
                'redaction_count':self.filter.redactions,'overflow':self.error=='CAPACITY',
                'error':self.error,'eof':self.eof,'closed':self.closed,
                'verified':self.finalized and self.eof and self.closed and self.error=='NONE'}

# Parent memory only, never serialize this registry or include it in repr.
_CONTEXTS={}
class Firewall:
    def __init__(self,space,values):
        self.space=space;self.values=tuple(values);Filter(self.values)
        self.streams={};self.finalized=False
    def spawn(self,argv,path,kwargs):
        from rt055_candidate_workspace import require_path
        require_path(self.space,path,'logs')
        if self.finalized or path in self.streams:raise FirewallError('UNVERIFIED')
        if any(k in kwargs for k in ('stdout','stderr','text','encoding','errors')):raise FirewallError('UNVERIFIED')
        stream=Stream(path,self.values);self.streams[path]=stream
        try:
            proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True,**kwargs)
            proc._rt055_firewall=stream;stream.attach(proc);return proc
        except BaseException:
            stream.fail('UNVERIFIED');stream.finish();raise FirewallError('UNVERIFIED') from None
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
            [setattr(s.filter,'patterns',()) for s in self.streams.values()]
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
        row['post_scan_hits']=sum(v.encode() in Path(path).read_bytes() for v in values if v)
        window.write_once(Path(str(path)+'.firewall.json'),row)
    if not row['verified'] or row['post_scan_hits']:return code or 3
    return code
