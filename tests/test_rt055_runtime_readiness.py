"""Amendment 5 public-only policy readiness; never launches candidates."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rt055_freeze as freeze
import rt055_runtime as runtime
import rt055_runtime_readiness as ready
import rt055_window as window
import test_rt055_formal_window as fixtures

class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WindowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.wid=self.f.wid;self.mid=self.f.mid

    def fixture(self):
        self.w=self.f.freeze_fixture();self.base=ready.directory(self.root,self.wid)

    def frozen(self):
        self.fixture();freeze.create(self.root,self.wid,self.mid)
        self.assertEqual(freeze.verify_once(self.root,self.wid),0)

    def test_missing_main_readiness_cannot_freeze(self):
        self.fixture();shutil.rmtree(self.base)
        with self.assertRaises((RuntimeError,OSError)):freeze.create(self.root,self.wid,self.mid)
        self.assertFalse((self.w/'status/freeze.claim').exists())

    def test_legacy_stale_policy_no_readiness_cannot_freeze(self):
        self.fixture();shutil.rmtree(self.base)
        old=self.root/'runtime-policy';old.mkdir();(old/'network.sb').write_text(runtime.SANDBOX)
        window.write_once(self.root/'audit/execution-network-gate.json',ready.NETWORK)
        with self.assertRaises((RuntimeError,OSError)):freeze.create(self.root,self.wid,self.mid)

    def test_real_probe_wrapper_preserves_all_legacy_bytes_and_duplicate_claim_denied(self):
        self.f.migration()
        old={'runtime-policy/network.sb':b'old loopback','runtime-policy/search-network.sb':b'old inbound',
             'audit/execution-network-gate.json':b'{"passed":true}'}
        for rel,data in old.items():
            p=self.root/rel;p.parent.mkdir(exist_ok=True);p.write_bytes(data)
        self.f.prepare_policy()
        with self.assertRaises(FileExistsError):self.f.prepare_policy()
        self.assertTrue(all((self.root/p).read_bytes()==b for p,b in old.items()))

    def test_synthetic_root_cannot_impersonate_main(self):
        self.fixture()
        t=self.root/'executioner-migrations'/self.mid/'rt055-synthetic-001'
        with self.assertRaises((RuntimeError,ValueError)):ready.prepare(t,self.wid,self.mid)
        # Even a UUID-shaped child with a forged own marker cannot bind parent.
        t=self.root/('rt055-'+str(uuid.uuid4()));t.mkdir();(t/'.rt055-owned').write_text(t.name[6:])
        window.write_once(t/'audit/protected-root.json',{'root':str(self.root)})
        with self.assertRaises(RuntimeError):ready.main_root(t)

    def test_missing_ledger_network_relaxation_or_policy_drift_rejected(self):
        self.fixture();p=self.base/'network.sb';original=p.read_bytes()
        variants=[runtime.SANDBOX,original.decode()+'(allow network*)\n',
                  '\n'.join(l for l in original.decode().splitlines() if '/exposure' not in l),
                  original.decode()+'\n']
        for text in variants:
            p.write_text(text)
            # Even rehashing the receipt cannot bless a noncanonical policy.
            rp=self.base/'receipt.json';row=json.loads(rp.read_text());row['files'][str(p.relative_to(self.root))]=runtime.ops.sha_file(p);rp.write_text(json.dumps(row))
            with self.assertRaises(RuntimeError):ready.verify(self.root,self.wid,self.mid)
            with self.assertRaises(RuntimeError):freeze.create(self.root,self.wid,self.mid)
        p.write_bytes(original)

    def test_network_receipt_root_scope_source_migration_window_drift_rejected(self):
        self.fixture();p=self.base/'receipt.json';original=p.read_bytes()
        for key,value in [('root',str(self.root/'child')),('protected_roots',[]),('protected_scopes',[]),
                          ('source_commit','2'*40),('source_files',{}),('migration_id',str(uuid.uuid4())),
                          ('window_id',str(uuid.uuid4())),('files',{}),('status','PASS_WITHOUT_PROBE')]:
            row=json.loads(original);row[key]=value;p.write_text(json.dumps(row))
            with self.assertRaises((RuntimeError,OSError)):ready.verify(self.root,self.wid,self.mid)
        p.write_bytes(original)
        gate=self.base/'network-gate.json';g=json.loads(gate.read_text());g['passed']=False;gate.write_text(json.dumps(g))
        row=json.loads(p.read_text());row['files'][str(gate.relative_to(self.root))]=runtime.ops.sha_file(gate);p.write_text(json.dumps(row))
        with self.assertRaises(RuntimeError):ready.verify(self.root,self.wid,self.mid)

    def test_source_file_drift_wrong_migration_and_cross_window_cannot_freeze(self):
        self.fixture()
        with self.assertRaises((RuntimeError,OSError)):freeze.create(self.root,self.wid,str(uuid.uuid4()))
        with self.assertRaises((RuntimeError,OSError)):ready.verify(self.root,str(uuid.uuid4()),self.mid)
        (self.root/'impl/rt055_runtime.py').write_text('changed source')
        with self.assertRaises(RuntimeError):ready.verify(self.root,self.wid,self.mid)

    def test_readiness_after_before_is_refused_even_if_snapshot_is_later(self):
        self.f.freeze_fixture(policy=False);self.f.collect()
        with self.assertRaises(RuntimeError):self.f.prepare_policy()
        # A forged timing receipt cannot circumvent comparison with before start.
        self.setUp();self.fixture();p=self.base/'receipt.json';row=json.loads(p.read_text())
        row['ready_at']=window.baseline(self.root,self.wid,'before')[0]['observed_at'];p.write_text(json.dumps(row))
        with self.assertRaises(RuntimeError):freeze.create(self.root,self.wid,self.mid)

    def test_correct_freeze_binds_all_files_and_spawn_precheck_no_popen(self):
        self.frozen()
        with patch.object(runtime.subprocess,'Popen',side_effect=AssertionError('candidate process forbidden')) as popen:
            for kind in ready.KINDS:
                profile=runtime.spawn_precheck(self.root,kind,self.wid)
                self.assertEqual(profile.read_text(),runtime.sandbox_text(self.root,kind))
            popen.assert_not_called()
        self.assertTrue(freeze.verify_artifacts(self.root,self.wid))
        paths=window.receipt(self.root,self.wid)['private_files']
        self.assertTrue(set(ready.verify(self.root,self.wid)['files'])<=set(paths))
        self.assertIn(str((self.base/'receipt.json').relative_to(self.root)),paths)

    def test_postfreeze_policy_or_receipt_drift_blocks_real_spawn_before_popen(self):
        self.frozen()
        for name in ('network.sb','search-network.sb','network-gate.json','receipt.json','claim.json'):
            p=self.base/name;old=p.read_bytes();p.write_bytes(old+b' ')
            with patch.object(runtime.subprocess,'Popen') as popen:
                with self.assertRaises((RuntimeError,OSError)):
                    runtime.spawn(self.root,['public-not-executed'],window_id=self.wid)
                popen.assert_not_called()
            self.assertFalse(freeze.verify_artifacts(self.root,self.wid))
            p.write_bytes(old)
        self.assertTrue(freeze.verify_artifacts(self.root,self.wid))

    def test_closed_window_or_missing_window_never_falls_back_to_legacy(self):
        self.frozen()
        with self.assertRaises(RuntimeError):runtime.spawn_precheck(self.root)
        self.f.collect('after')
        with self.assertRaises(RuntimeError):runtime.spawn_precheck(self.root,window_id=self.wid)

    def test_exposure_prevents_new_policy_claim(self):
        self.f.migration();window.write_once(self.root/'exposure/a/cwork-3m.json',{})
        with self.assertRaises(RuntimeError):self.f.prepare_policy()
        self.assertFalse(ready.directory(self.root,self.wid).exists())

    def test_failed_socket_probe_is_not_retried_or_claimed_pass(self):
        self.f.migration()
        from types import SimpleNamespace
        with patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{}')):
            with self.assertRaises(RuntimeError):ready.prepare(self.root,self.wid,self.mid)
        b=ready.directory(self.root,self.wid)
        self.assertTrue((b/'claim.json').exists());self.assertFalse((b/'receipt.json').exists())
        with self.assertRaises(FileExistsError):self.f.prepare_policy()

    def test_all_required_ledger_scopes_are_bound_in_generated_policy(self):
        self.fixture()
        expected=('builder','verifier','consumption','exposure','void-prequery',
                  'zero-exposure-migrations','formal-windows','runtime-policy-versions')
        for kind in ready.KINDS:
            lines=runtime.sandbox_text(self.root,kind).splitlines()
            for scope in expected:
                for action in ('file-read*','file-write*'):
                    rule='(deny %s (subpath %s))' % (action,json.dumps(str((self.root/scope).resolve())))
                    self.assertIn(rule,lines)
        self.assertEqual(set(ready.verify(self.root,self.wid)['protected_scopes']),set(expected))

    def test_symlink_policy_or_receipt_refused(self):
        self.fixture()
        for name in ('network.sb','receipt.json'):
            p=self.base/name;data=p.read_bytes();p.unlink();q=self.root/'public-target';q.write_bytes(data);p.symlink_to(q)
            with self.assertRaises(RuntimeError):ready.verify(self.root,self.wid,self.mid)
            p.unlink();p.write_bytes(data)

if __name__=='__main__':unittest.main()
