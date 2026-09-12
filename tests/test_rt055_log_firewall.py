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
            out=f.feed(b'x'*fw.CHUNK);self.assertLess(len(f.buffer),f.maximum)
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
