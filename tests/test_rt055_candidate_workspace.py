"""Public synthetic Amendment 6: ownership, OS isolation, teardown, log gate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rt055_candidate_workspace as cw
import rt055_runtime as rt

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/('rt055-'+str(uuid.uuid4()));self.root.mkdir(mode=0o700)
        (self.root/'.rt055-owned').write_text(self.root.name[6:])
        self.wid=str(uuid.uuid4());self.aid=str(uuid.uuid4())
    def make(self,key='a',wid=None,aid=None):
        return cw.create(self.root,wid or self.wid,key,aid or self.aid,synthetic=True)
    def test_exclusive_and_private(self):
        space=self.make();self.assertEqual(space.base,self.root/'candidate-runtime'/self.wid/'a'/self.aid)
        self.assertEqual(space.base.stat().st_mode&0o777,0o700)
        with self.assertRaises(FileExistsError):self.make()
        self.assertTrue(space.ledger.is_file())
    def test_identity_escape_symlink_and_cross_window_rejected(self):
        for wid,key,aid in [('../escape','a',self.aid),(self.wid,'../b',self.aid),(self.wid,'a','../escape')]:
            with self.assertRaises((ValueError,RuntimeError)):cw.create(self.root,wid,key,aid,synthetic=True)
        (self.root/'candidate-runtime').symlink_to(self.tmp.name)
        with self.assertRaises(RuntimeError):self.make()
        (self.root/'candidate-runtime').unlink();s=self.make()
        with self.assertRaises(RuntimeError):cw.validate(s,window_id=str(uuid.uuid4()))
        with self.assertRaises(RuntimeError):cw.file(s,'logs','../escape')
        (s.base/'logs').rmdir();(s.base/'logs').symlink_to(self.tmp.name)
        with self.assertRaises(RuntimeError):cw.file(s,'logs','a.log')
    def test_reuse_rejected_even_after_cleanup(self):
        s=self.make();cw.cleanup(s)
        with self.assertRaises(FileExistsError):self.make()
    def test_cleanup_exact_owner_preserves_sibling_and_audit(self):
        a=self.make();b=self.make('b');other=self.make(wid=str(uuid.uuid4()),aid=str(uuid.uuid4()))
        cw.file(a,'logs','public.log').write_text('public startup')
        cw.file(a,'data','owned.bin').write_bytes(b'public')
        original=a.ledger.read_bytes();row=cw.cleanup(a)
        self.assertTrue(row['complete']);self.assertFalse(a.base.exists())
        self.assertTrue(b.base.exists() and other.base.exists());self.assertEqual(a.ledger.read_bytes(),original)
        self.assertEqual(len(list(a.archive.rglob('public.log'))),1)
        cw.cleanup(b);cw.cleanup(other);self.assertFalse((self.root/'candidate-runtime').exists())
    def test_ownership_tampering_and_symlink_cleanup_rejected(self):
        s=self.make();v=json.loads(s.ledger.read_text());v['attempt_id']=str(uuid.uuid4());s.ledger.write_text(json.dumps(v))
        with self.assertRaises(RuntimeError):cw.cleanup(s)
        self.assertTrue(s.base.exists())
    def test_log_leak_fail_closed_no_private_strings_in_receipt(self):
        s=self.make();p=cw.file(s,'logs','public.log');p.write_text('PUBLIC SECRET CANARY')
        corpus={'libraries':{'public':[{'title':'PUBLIC SECRET CANARY','body':'PUBLIC body','doc_id':'PUBLIC-id','filename':'PUBLIC.md'}]}}
        cases=[type('Case',(),{'query':'PUBLIC question','expected_doc_ids':frozenset({'PUBLIC-id'})})()]
        needles=cw.needles(corpus,cases)
        with self.assertRaisesRegex(RuntimeError,'candidate_log_privacy_failed'):cw.scan(s,needles)
        receipt=json.loads((s.ledger.parent/'log-scan.json').read_text());self.assertFalse(receipt['passed'])
        self.assertNotIn('CANARY',json.dumps(receipt));self.assertEqual(receipt['hit_files'],1)
    def test_log_no_hit_pass_and_missing_logs_fail(self):
        s=self.make()
        with self.assertRaises(RuntimeError):cw.scan(s,['PUBLIC CANARY'])
        # Different scan receipt because failures are append-only.
        other=self.make(aid=str(uuid.uuid4()));cw.file(other,'logs','public.log').write_text('service ready')
        self.assertTrue(cw.scan(other,['PUBLIC CANARY'])['passed'])
    def test_jvm_config_copy_redirects_only_diagnostics_and_rejects_drift(self):
        s=self.make();conf=self.root/'opensearch/config';conf.mkdir(parents=True)
        original='-Xms1g\n-Xmx1g\n-Xlog:gc*,gc+age=trace,safepoint:file=logs/gc.log:utctime,pid,tags:filecount=32,filesize=64m\n-XX:ErrorFile=logs/hs_err_pid%p.log\n-XX:HeapDumpPath=data\n'
        (conf/'jvm.options').write_text(original);(conf/'opensearch.yml').write_text('public: true')
        copied=cw.search_config(self.root,s,'a-public',create=True);text=(copied/'jvm.options').read_text()
        self.assertIn('-Xms1g\n-Xmx1g',text);self.assertNotIn('file=logs/gc.log',text)
        self.assertIn('file='+str(s.base/'logs/gc-a-public.log'),text)
        self.assertEqual((conf/'jvm.options').read_text(),original)
        self.assertEqual((copied/'opensearch.yml').read_text(),'public: true')
        (copied/'jvm.options').write_text(original)
        with self.assertRaises(RuntimeError):cw.search_config(self.root,s,'a-public')
        with self.assertRaises(RuntimeError):cw.jvm_config(original,s,'../../foreign')

    def test_actual_bash_here_string_denied_but_owned_stdin_passes(self):
        s=self.make();binary=self.root/'opensearch/bin/opensearch';binary.parent.mkdir(parents=True)
        source='/bin/cat <<<"$KEYSTORE_PASSWORD"; /bin/cat <<<"$KEYSTORE_PASSWORD"'
        binary.write_text(source);argv,stdin=cw.search_launcher(self.root,s,'a-public',create=True)
        env=rt.clean_env(s.base);env['RT055_KEYSTORE_STDIN']=str(stdin)
        policy=cw.policy(s,rt.sandbox_text(self.root,'inbound-only'))
        old=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,'/bin/bash','-c',source],env=env,capture_output=True)
        self.assertNotEqual(old.returncode,0);self.assertIn(b'temp file',old.stderr)
        new=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,*argv],env=env,capture_output=True)
        self.assertEqual(new.returncode,0);self.assertEqual(new.stdout,b'\n\n')
        self.assertEqual(binary.read_text(),source)
        stdin.write_text('FOREIGN')
        with self.assertRaises(RuntimeError):cw.search_launcher(self.root,s,'a-public')

    def test_realpath_traversal_allowed_but_parent_listing_and_sibling_data_denied(self):
        s=self.make();other=self.make('b');target=cw.file(s,'data','public');target.write_text('PUBLIC')
        foreign=cw.file(other,'data','public');foreign.write_text('PUBLIC')
        program="""import os,sys,json
out=[]
for kind,p in json.loads(sys.argv[1]):
 try:
  if kind=='realpath':os.path.realpath(p,strict=True)
  elif kind=='listing':os.listdir(p)
  else:open(p).read()
  out.append('ALLOWED')
 except PermissionError:out.append('DENIED')
print(json.dumps(out))
"""
        probes=[('realpath',str(target)),('listing',str(s.base.parent)),('read',str(foreign))]
        for kind in ('loopback','inbound-only'):
            p=subprocess.run(['/usr/bin/sandbox-exec','-p',cw.policy(s,rt.sandbox_text(self.root,kind)),sys.executable,'-c',program,json.dumps(probes)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0);self.assertEqual(json.loads(p.stdout),['ALLOWED','DENIED','DENIED'])

    def test_native_config_is_copied_without_changes_and_drift_is_rejected(self):
        s=self.make('b');source=self.root/'weknora/config';source.mkdir(parents=True)
        (source/'config.yaml').write_text('PUBLIC CONFIG')
        data=cw.file(s,'data','b-public');data.mkdir(mode=0o700)
        target=cw.native_config(self.root,s,data,create=True)
        self.assertEqual((target/'config.yaml').read_text(),'PUBLIC CONFIG')
        self.assertEqual((source/'config.yaml').read_text(),'PUBLIC CONFIG')
        (target/'config.yaml').write_text('DRIFT')
        with self.assertRaises(RuntimeError):cw.native_config(self.root,s,data)
        with self.assertRaises(RuntimeError):cw.native_config(self.root,s,self.root/'outside',create=True)

    def test_native_fallback_log_is_scanned_and_archived_not_database_content(self):
        s=self.make('b');data=cw.file(s,'data','b-public');data.mkdir()
        (data/'logs').mkdir();(data/'logs/fallback.log').write_text('PUBLIC LEAK CANARY')
        (data/'app.db').write_text('DATABASE DOCUMENT CONTENT')
        with self.assertRaises(RuntimeError):cw.scan(s,['PUBLIC LEAK CANARY'])
        self.assertEqual(len(cw.log_files(s)),1);cw.cleanup(s)
        self.assertEqual((s.archive/'data/b-public/logs/fallback.log').read_text(),'PUBLIC LEAK CANARY')
        self.assertFalse(list(s.archive.rglob('app.db')))

    def test_formal_without_claim_rejected(self):
        with self.assertRaises((RuntimeError,OSError)):cw.create(self.root,self.wid,'a',self.aid)
        self.assertFalse((self.root/'candidate-runtime').exists())
    def test_formal_launch_without_workspace_rejected_before_spawn(self):
        import rt055_run_a as a
        with patch.object(a,'ROOT',self.root),patch.object(rt,'spawn') as spawn:
            with self.assertRaises(RuntimeError):a.launch_opensearch('public',39101,self.root/'bad.log',window_id=self.wid)
            spawn.assert_not_called()
    def test_real_sandbox_old_denied_new_write_and_ledger_isolation(self):
        s=self.make();other=self.make('b');protected=self.root/'formal-windows'/self.wid
        protected.mkdir(parents=True);(protected/'public-ledger').write_text('PUBLIC')
        old=protected/'runtime-logs';old.mkdir();owned=cw.file(s,'logs','public.log')
        program='''import pathlib,sys,json
out=[]
for operation,path in json.loads(sys.argv[1]):
 try:
  p=pathlib.Path(path)
  if operation=='read':p.read_bytes()
  else:p.write_text('PUBLIC')
  out.append('ALLOWED')
 except OSError as e:out.append('DENIED' if e.errno in (1,13) else 'OTHER')
print(json.dumps(out))
'''
        (other.base/'logs/public.log').write_text('PUBLIC')
        probes=[('write',str(old/'new.log')),('write',str(owned)),('read',str(protected/'public-ledger')),('read',str(other.base/'logs/public.log')),('write',str(other.base/'logs/new.log'))]
        for kind in ('loopback','inbound-only'):
            policy=cw.policy(s,rt.sandbox_text(self.root,kind))
            p=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,sys.executable,'-c',program,json.dumps(probes)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout),['DENIED','ALLOWED','DENIED','DENIED','DENIED'])

class StartupBindingTests(unittest.TestCase):
    def setUp(self):
        import test_rt055_formal_window as fixture
        self.f=fixture.WindowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.w=self.f.freeze_fixture()
    def test_missing_or_tampered_workspace_binding_blocks_freeze(self):
        import rt055_freeze as f
        import rt055_runtime_readiness as ready
        p=ready.directory(self.f.root,self.f.wid)/'workspace-readiness.json';original=p.read_bytes()
        variants=[('a_started',0),('b_started',0),('sidecars_started',0),('formal_queries',1),('private_reads',1),('cleanup_failures',1),('remaining_runtime',1),('source_commit','0'*40),('leases',[])]
        for key,value in variants:
            row=json.loads(original);row[key]=value;p.write_text(json.dumps(row))
            with self.assertRaises((RuntimeError,OSError)):f.create(self.f.root,self.f.wid,self.f.mid)
            self.assertFalse((self.w/'status/freeze.claim').exists())
        p.unlink()
        with self.assertRaises((RuntimeError,OSError)):f.create(self.f.root,self.f.wid,self.f.mid)
    def test_probe_refuses_after_before_and_main_synthetic_bypass(self):
        with self.assertRaises(RuntimeError):cw.create(self.f.root,self.f.wid,'a',str(uuid.uuid4()),synthetic=True,migration_id=self.f.mid)
        (self.f.root/'verifier/private-verified.json').write_text('{}')
        with self.assertRaises(RuntimeError):cw.create(self.f.root,str(uuid.uuid4()),'a',str(uuid.uuid4()),synthetic=True)

if __name__=='__main__':unittest.main()
