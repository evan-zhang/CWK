"""Public-only logger reproducer and streaming boundary regressions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

CANARY='RT055_PUBLIC_SQL_CANARY_Unicode雪'
class RedReproducer(unittest.TestCase):
    def test_old_direct_file_is_not_privacy_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'legacy.log'
            program="import os,sys; b=sys.argv[1].encode(); os.write(1,b[:11]); os.write(1,b[11:])"
            with p.open('wb') as f:subprocess.run([sys.executable,'-c',program,CANARY],stdout=f,check=True)
            # The old post-scan detects the leak only AFTER it is on disk.
            self.assertIn(CANARY.encode(),p.read_bytes())
if __name__=='__main__':unittest.main()

import json
import threading
import time
import uuid
from unittest.mock import patch
import rt055_log_firewall as fw
import rt055_candidate_workspace as cw
import rt055_opslib as ops
class StreamingTests(unittest.TestCase):
    def test_every_boundary_and_longest_overlap(self):
        values=[CANARY,'prefix','prefix-suffix','fix-suffix','雪☃','line\n"quoted"']
        values=cw.needles({'values':values},[])
        for value in values:
            b=value.encode()
            for cut in range(len(b)+1):
                f=fw.Filter(values);out=f.feed(b[:cut])+f.feed(b[cut:])+f.feed(b'',True)
                self.assertEqual(out,fw.REPLACEMENT)
        f=fw.Filter(['ab','abc','bc']);self.assertEqual(f.feed(b'abcabc',True),fw.REPLACEMENT*2)
    def test_long_no_newline_bounded_and_cap(self):
        f=fw.Filter([CANARY]);out=b''
        for _ in range(1024):
            out=f.feed(b'x'*fw.CHUNK);self.assertEqual(out,b'');self.assertLessEqual(len(f.buffer),fw.CAP)
        self.assertEqual(f.input_bytes,64*1024*1024)
        with self.assertRaises(fw.FirewallError):f.feed(b'x')
        self.assertNotIn(CANARY.encode(),out)
        with self.assertRaises(fw.FirewallError):fw.Filter(['x'*(fw.MAX_LEAF+1)])
    def make(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)/('rt055-'+str(uuid.uuid4()));root.mkdir(mode=0o700);(root/'.rt055-owned').touch()
        s=cw.create(root,str(uuid.uuid4()),'b',str(uuid.uuid4()),synthetic=True)
        self.addCleanup(fw.release,s);return s
    def spawn(self,s,program,values=None):
        f=fw.bind(s,values or [CANARY]);p=cw.file(s,'logs','native.log')
        proc=f.spawn([sys.executable,'-c',program],p,{'env':{'PATH':os.environ['PATH']}})
        return f,p,proc
    def test_raw_not_on_disk_sanitized_scan_archive(self):
        s=self.make();b=CANARY.encode()
        program=f'import os; b={b!r}; os.write(1,b[:7]); os.write(2,b[7:]); os.write(1,b" ready")'
        f,p,proc=self.spawn(s,program);proc.wait(5);row=f.finalize()
        self.assertTrue(row['verified']);self.assertEqual(row['logs'][0]['redaction_count'],1)
        self.assertIn(fw.REPLACEMENT,p.read_bytes());self.assertTrue(cw.scan(s,[CANARY])['passed'])
        for item in s.base.rglob('*'):
            if item.is_file():self.assertNotIn(b,item.read_bytes())
        self.assertTrue(cw.cleanup(s)['logs_retained']);self.assertFalse(s.base.exists())
    def test_child_nonzero_hard_failure(self):
        s=self.make();f,p,proc=self.spawn(s,'raise SystemExit(7)');proc.wait(5)
        with self.assertRaises(fw.FirewallError):f.finalize()
        self.assertEqual(f.row['logs'][0]['error'],'CHILD_NONZERO')
    def test_missing_eof_and_fd_threads_reaped(self):
        s=self.make();f,p,proc=self.spawn(s,'import time;time.sleep(20)')
        stream=proc._rt055_firewall
        with patch.object(stream,'drain'):
            pass
        # Independent held pipe with no EOF, no spawned grandchild to leak.
        ops.stop_process(proc);stream.thread.join(5)
        stream.eof=False
        with self.assertRaises(fw.FirewallError):f.finalize()
        self.assertTrue(stream.pipe.closed and stream.sink.closed)
        self.assertFalse(stream.thread.is_alive())
    def test_drain_failure_kills_child_no_raw_spool(self):
        s=self.make();f=fw.bind(s,[CANARY]);p=cw.file(s,'logs','native.log')
        with patch.object(fw.Filter,'feed',side_effect=OSError('PUBLIC')):
            proc=f.spawn([sys.executable,'-c','import os,time;os.write(1,b"public");time.sleep(20)'],p,{})
            proc.wait(5)
            with self.assertRaises(fw.FirewallError):f.finalize()
        stream=proc._rt055_firewall;self.assertEqual(stream.error,'DRAIN_ERROR');self.assertFalse(stream.thread.is_alive())
        self.assertTrue(stream.pipe.closed and stream.sink.closed)
    def test_direct_bypass_and_residual_refused(self):
        s=self.make();f,p,proc=self.spawn(s,'print("ready")');proc.wait(5);f.finalize()
        p.write_text(CANARY)
        with self.assertRaises(RuntimeError):cw.scan(s,[CANARY])
        self.assertEqual(ops.read_json(s.ledger.parent/'log-scan.json')['hit_files'],1)
        self.assertFalse(cw.cleanup(s)['logs_retained']);self.assertEqual(list(s.archive.rglob('*.log')),[])
        s=self.make();cw.file(s,'logs','direct.log').write_text('no matches')
        with self.assertRaises(RuntimeError):cw.scan(s,[CANARY])
    def test_repeated_process_fd_and_thread_cleanup(self):
        before=len(list(Path('/dev/fd').iterdir()));threads=threading.active_count()
        for _ in range(10):
            s=self.make();f,p,proc=self.spawn(s,'print("ready")');proc.wait(5);f.finalize();cw.scan(s,[CANARY]);cw.cleanup(s)
        self.assertLessEqual(len(list(Path('/dev/fd').iterdir())),before+1)
        self.assertLessEqual(threading.active_count(),threads)
    def test_real_held_pipe_missing_eof(self):
        from unittest.mock import Mock
        s=self.make();path=cw.file(s,'logs','held.log');stream=fw.Stream(path,[CANARY]);r,w=os.pipe()
        proc=Mock(stdout=os.fdopen(r,'rb',buffering=0));proc.poll.return_value=0
        try:
            stream.attach(proc);row=stream.finish(.02)
            self.assertEqual(row['error'],'MISSING_EOF');self.assertFalse(row['verified']);self.assertFalse(stream.thread.is_alive())
            self.assertTrue(stream.pipe.closed and stream.sink.closed)
        finally:os.close(w)
    def test_stream_overflow_and_fsync_failure_stop_and_close(self):
        s=self.make();path=cw.file(s,'logs','cap.log');stream=fw.Stream(path,[CANARY],cap=100)
        proc=subprocess.Popen([sys.executable,'-c','import os,time;os.write(1,b"x"*101);time.sleep(20)'],stdout=subprocess.PIPE,start_new_session=True)
        stream.attach(proc);proc.wait(5);row=stream.finish();self.assertTrue(row['overflow']);self.assertFalse(row['verified'])
        self.assertTrue(stream.pipe.closed and stream.sink.closed)
        s=self.make();f=fw.bind(s,[CANARY]);path=cw.file(s,'logs','close.log')
        with patch.object(fw.os,'fsync',side_effect=OSError('PUBLIC')):
            proc=f.spawn([sys.executable,'-c','print("ready")'],path,{});proc.wait(5);proc._rt055_firewall.thread.join(5)
        with self.assertRaises(fw.FirewallError):f.finalize()
        stream=proc._rt055_firewall;self.assertEqual(stream.error,'CLOSE_ERROR');self.assertTrue(stream.pipe.closed and stream.sink.closed)
    def test_parent_pattern_bank_is_shared_and_bounded(self):
        s=self.make();f=fw.bind(s,[CANARY]);p1=cw.file(s,'logs','one.log');p2=cw.file(s,'logs','two.log')
        procs=[f.spawn([sys.executable,'-c','print("ready")'],p,{}) for p in (p1,p2)]
        for p in procs:p.wait(5)
        self.assertIs(procs[0]._rt055_firewall.filter.patterns,procs[1]._rt055_firewall.filter.patterns)
        self.assertEqual(fw.PATTERN_CAP,512*1024*1024);self.assertEqual(fw.CAP,64*1024*1024)
        f.finalize();cw.scan(s,[CANARY]);cw.cleanup(s)
        with patch.object(fw,'PATTERN_CAP',5):
            with self.assertRaises(fw.FirewallError):fw.Filter([CANARY])
    def test_output_expansion_cap_and_actual_byte_accounting(self):
        s=self.make();path=cw.file(s,'logs','expansion.log');stream=fw.Stream(path,['X'],cap=100)
        proc=subprocess.Popen([sys.executable,'-c','import os,time;os.write(1,b"X"*10)'],stdout=subprocess.PIPE,start_new_session=True)
        stream.attach(proc);proc.wait(5);row=stream.finish()
        self.assertTrue(row['overflow']);self.assertEqual(row['input_bytes'],10);self.assertEqual(row['output_bytes'],0);self.assertEqual(path.read_bytes(),b'')


class Amendment14Tests(unittest.TestCase):
    def test_eof_only_and_shared_across_workspaces(self):
        bank=fw.MemoryBank({'text':'PUBLIC_PRIVATE_雪'},['abc','abcdef'])
        a=fw.Filter(bank);b=fw.Filter(bank.values)
        self.assertIs(a.bank,b.bank);self.assertIs(a.patterns,b.patterns)
        self.assertEqual(a.feed(b'abcdef'),b'')
        self.assertEqual(a.feed(b'',True),fw.REPLACEMENT)
        with self.assertRaises(fw.FirewallError):a.feed(b'new')
        s1=StreamingTests.make(self);s2=StreamingTests.make(self)
        f1=fw.bind(s1,bank);f2=fw.bind(s2,bank)
        self.assertIs(f1.bank,f2.bank)
        self.assertNotIn('PUBLIC_PRIVATE',repr(bank))

    def test_overlap_safe_failed_prefix_and_short_closed_set(self):
        values=['a'*16+'end','a'*17+'end','aba','ab','babc','雪🙂','x\n"quoted"']
        raw=('a'*17+'end'+'abababc'+'雪🙂'+'x\n"quoted"').encode()
        def oracle(body,patterns):
            out=b'';pos=0
            while pos<len(body):
                hit=next((p for p in sorted(patterns,key=lambda p:-len(p)) if body.startswith(p,pos)),None)
                if hit:out+=fw.REPLACEMENT;pos+=len(hit)
                else:out+=body[pos:pos+1];pos+=1
            return out
        expected=oracle(raw,[v.encode() for v in values])
        for cut in range(len(raw)+1):
            f=fw.Filter(values,cap=4096)
            self.assertEqual(f.feed(raw[:cut])+f.feed(raw[cut:])+f.feed(b'',True),expected)
        # First fixed prefix is a false full match; the next overlaps it.
        f=fw.Filter(['a'*16+'b'],cap=1024)
        self.assertEqual(f.feed(b'a'*17+b'b',True),b'a'+fw.REPLACEMENT)

    def test_randomized_exact_reference(self):
        import random
        rng=random.Random(1414)
        for _ in range(100):
            patterns={bytes(rng.choices(b'abc',k=rng.randint(1,25))) for _ in range(20)}
            raw=bytes(rng.choices(b'abc',k=300));out=bytearray();pos=0
            while pos<len(raw):
                matches=[p for p in patterns if raw.startswith(p,pos)]
                if matches:p=max(matches,key=len);out.extend(fw.REPLACEMENT);pos+=len(p)
                else:out.append(raw[pos]);pos+=1
            f=fw.Filter(patterns,cap=8192)
            self.assertEqual(f.feed(raw,True),out)

    def test_index_resource_limits_and_immutable_bank(self):
        bank=fw.PatternBank(['public-one','public-two'])
        with self.assertRaises((AttributeError,TypeError)):bank.patterns=()
        with self.assertRaises((AttributeError,TypeError)):bank.index[b'new']=()
        with patch.object(fw,'PATTERN_COUNT_CAP',1):
            with self.assertRaises(fw.FirewallError):fw.PatternBank(['public-one','public-two'])
        with patch.object(fw,'MAX_LEAF',4):
            with self.assertRaises(fw.FirewallError):fw.PatternBank(['12345'])
        with patch.object(fw,'PATTERN_CAP',7):
            with self.assertRaises(fw.FirewallError):fw.PatternBank(['1234','5678'])
        with patch.object(fw,'CANDIDATE_CAP',1):
            f=fw.Filter(['a'*16+'b'],cap=128)
            with self.assertRaises(fw.FirewallError):f.feed(b'a'*20,True)
        with patch.object(fw,'COMPARE_CAP',1):
            f=fw.Filter(['a'*16+'b'],cap=128)
            with self.assertRaises(fw.FirewallError):f.feed(b'a'*16+b'b',True)
        with patch.object(fw,'_RAW_SLOTS',threading.BoundedSemaphore(1)):
            f=fw.Filter(['public'],cap=128);g=fw.Filter(['public'],cap=128)
            f.feed(b'x')
            with self.assertRaises(fw.FirewallError):g.feed(b'y')
            f.discard()

    def test_child_output_stays_empty_until_eof(self):
        s=StreamingTests.make(self);f=fw.bind(s,['PUBLIC_SECRET'])
        path=cw.file(s,'logs','eof.log')
        proc=f.spawn([sys.executable,'-c','import os,time;os.write(1,b"PUBLIC_SECRET");time.sleep(.4)'],path,{})
        deadline=time.monotonic()+3
        while proc._rt055_firewall.input_bytes==0 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(path.read_bytes(),b'')
        proc.wait(5);f.finalize();self.assertEqual(path.read_bytes(),fw.REPLACEMENT)

    def test_thread_start_failure_and_no_raw(self):
        s=StreamingTests.make(self);f=fw.bind(s,['PUBLIC_SECRET']);path=cw.file(s,'logs','thread.log')
        with patch.object(threading.Thread,'start',side_effect=RuntimeError('public')):
            with self.assertRaises(fw.FirewallError):f.spawn([sys.executable,'-c','print("PUBLIC_SECRET")'],path,{})
        stream=f.streams[path]
        if stream.proc:stream.proc.wait(5)
        self.assertFalse(stream.receipt()['verified']);self.assertEqual(path.read_bytes(),b'')

    def test_extra_matcher_never_recompiles_full_bank(self):
        bank=fw.MemoryBank({'text':'a'*20},['ab']);receipt=bank.receipt()
        with patch.object(fw,'_prefix_regex',wraps=fw._prefix_regex) as compile:
            derived=bank.extend(['a'*20,'a'*21,'abc'])
            self.assertEqual(compile.call_count,1)
            self.assertEqual(set(compile.call_args.args[0]),{b'a'*16,b'abc'})
        self.assertIs(derived.matcher[0],bank.matcher[0])
        self.assertEqual(bank.receipt(),receipt)
        self.assertIs(derived.patterns[1],bank.patterns[0])
        f=fw.Filter(derived,cap=256)
        self.assertEqual(f.feed(b'a'*21+b'abc',True),fw.REPLACEMENT*2)
        self.assertIs(bank.extend(bank.values),bank)

    def test_private_bank_load_and_multiple_spaces_compile_once(self):
        import rt055_confidentiality as privacy
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('builder','verifier'):(root/name).mkdir()
            for name in ('builder/private-corpus.json','verifier/private-verified.json'):
                (root/name).write_text('{"value":"PUBLIC_COMPLETE_雪"}')
            with patch.object(fw,'_prefix_regex',wraps=fw._prefix_regex) as compile:
                first,_=privacy.load_private_bank(root);second,_=privacy.load_private_bank(root)
                self.assertIs(first,second)
                s1=StreamingTests.make(self);s2=StreamingTests.make(self)
                self.assertIs(privacy.bind_current_logs(s1,first).bank,privacy.bind_current_logs(s2,second).bank)
                self.assertEqual(compile.call_count,1)
