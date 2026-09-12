"""Public synthetic executioner recovery; no OPS/private inputs."""
import copy
import json
import os
import subprocess
import socket
import urllib.request
import urllib.error
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rt055_runtime as runtime
import rt055_run_a as search
import rt055_run_b as native
import rt055_confidentiality as gate
import rt055_candidate_workspace as cw
import uuid

def public_workspace(root,key):
    root.chmod(0o700);(root/".rt055-owned").write_text("PUBLIC")
    return cw.create(root,str(uuid.uuid4()),key,str(uuid.uuid4()),synthetic=True)


class ExecutionerTests(unittest.TestCase):
    def test_search_launch_forces_ipv4_and_no_outbound_and_cleans_failed_start(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'runtime-logs').mkdir();space=public_workspace(root,'a')
            conf=root/'opensearch/config';conf.mkdir(parents=True);(conf/'jvm.options').write_text('-Xlog:gc:file=logs/gc.log')
            (root/'opensearch/bin').mkdir();(root/'opensearch/bin/opensearch').write_text('cat <<<"$KEYSTORE_PASSWORD"; cat <<<"$KEYSTORE_PASSWORD"')
            p = Mock()
            with patch.object(search, 'ROOT', root), patch.object(runtime, 'spawn', return_value=p) as spawn, patch.object(search.ops, 'wait_for_http', side_effect=RuntimeError('not_ready')), patch.object(search.ops, 'stop_process') as stop:
                with self.assertRaises(RuntimeError):
                    search.launch_opensearch('synthetic', 39101, cw.file(space,'logs','a.log'), 'smoke',workspace=space)
            self.assertIn('-Djava.net.preferIPv4Stack=true', spawn.call_args.kwargs['env'].get('OPENSEARCH_JAVA_OPTS', ''))
            self.assertEqual(spawn.call_args.kwargs.get('network_policy'), 'inbound-only')
            self.assertIn('transport.host=127.0.0.1', spawn.call_args.args[1])
            stop.assert_called_once_with(p)

    def test_external_native_api_rejected_before_network(self):
        with patch.object(native.urllib.request, 'build_opener') as opener:
            for url in ('http://1.1.1.1:443', 'http://example.com', 'http://127.0.0.1.evil.test', 'http://user@127.0.0.1:41101'):
                with self.assertRaises((ValueError, RuntimeError)):
                    native.api_call(url, 'GET', '/api/v1/knowledge-bases', None, None)
            opener.assert_not_called()

    def test_loopback_endpoint_contract(self):
        runtime.require_loopback_url('http://127.0.0.1:41101/v1')
        for url in ('https://example.com/v1', 'http://0.0.0.0:41101/v1', 'http://127.0.0.1:41101/v1#x'):
            with self.assertRaises(ValueError):runtime.require_loopback_url(url)

    def test_environment_rejects_tracing_or_external_model_address(self):
        with tempfile.TemporaryDirectory() as td:
            env = runtime.clean_env(Path(td))
            runtime.validate_environment(env)
            for key, value in (('LANGFUSE_ENABLED', 'true'), ('LANGFUSE_HOST', 'http://127.0.0.1:41101'), ('OTEL_SDK_DISABLED', 'false'), ('OTEL_EXPORTER_OTLP_ENDPOINT', 'https://example.com'), ('SERVER_HOST', '0.0.0.0'), ('OPENAI_BASE_URL', 'https://example.com/v1')):
                bad = {**env, key: value}
                with self.assertRaises(ValueError):runtime.validate_environment(bad)

    def test_modified_sandbox_rejected_before_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'audit').mkdir(); (root/'runtime-policy').mkdir()
            (root/'audit/execution-network-gate.json').write_text(json.dumps({'passed': True}))
            (root/'runtime-policy/network.sb').write_text(runtime.SANDBOX + '\n(allow network*)\n')
            with patch.object(runtime.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'network_profile_drift'):runtime.spawn(root, ['synthetic'], env=runtime.clean_env(root))
                popen.assert_not_called()

    def test_failed_native_setup_stops_launched_process(self):
        p=Mock()
        with patch.object(native, 'launch_server', return_value=p), patch.object(native, 'api_call', side_effect=RuntimeError('synthetic_setup_failed')), patch.object(native.ops, 'stop_process') as stop:
            with self.assertRaises(RuntimeError):native.setup_instance('synthetic',41101,41102,Path('synthetic'),Path('synthetic.log'),None)
            stop.assert_called_once_with(p)

    def test_native_dictionary_configuration_is_required_and_used(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'runtime-logs').mkdir();space=public_workspace(root,'b')
            (root/'weknora/config').mkdir(parents=True);(root/'weknora/config/config.yaml').write_text('public: true')
            (root/'weknora/migrations/sqlite').mkdir(parents=True);(root/'weknora/migrations/sqlite/0001.sql').write_text('SELECT 1;')
            with patch.object(native,'ROOT',root),patch.object(runtime,'spawn') as spawn:
                with self.assertRaisesRegex(RuntimeError,'native_dictionary_assets_missing'):
                    native.launch_server(41101,cw.file(space,'data','native'),cw.file(space,'logs','native.log'),workspace=space)
                spawn.assert_not_called()
                (root/'jieba').mkdir()
                for name in runtime.JIEBA_FILES:(root/'jieba'/name).write_text('public synthetic')
                with patch.object(native.ops,'wait_for_http'):
                    native.launch_server(41101,cw.file(space,'data','native'),cw.file(space,'logs','native.log'),workspace=space)
                self.assertEqual(spawn.call_args.kwargs['env']['JIEBA_DICT_DIR'],str(root/'jieba'))

    def test_required_dictionary_bytes_enter_the_dependency_manifest(self):
        import rt055_freeze as freeze
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'sidecar').mkdir();(root/'sidecar/requirements-freeze.txt').write_text('public==1')
            with self.assertRaisesRegex(RuntimeError,'native_dictionary_assets_missing'):freeze.dependencies(root,'b')
            (root/'jieba').mkdir()
            for name in runtime.JIEBA_FILES:(root/'jieba'/name).write_text('public synthetic')
            paths=freeze.dependencies(root,'b');self.assertEqual(len(paths),6)
            before=runtime.ops.file_manifest(paths,base=root)
            (root/'jieba'/runtime.JIEBA_FILES[0]).write_text('changed')
            self.assertNotEqual(before,runtime.ops.file_manifest(paths,base=root))

    def test_exited_service_wait_fails_without_sleep(self):
        p=Mock();p.poll.return_value=1
        with patch.object(search.ops.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'service_process_exited'):
                search.ops.wait_for_http('http://127.0.0.1:39101', 300, process=p)
            sleep.assert_not_called()

    def test_external_model_registration_is_refused_without_http(self):
        with patch.object(native.urllib.request, 'build_opener') as opener:
            with self.assertRaises(ValueError):
                native.api_call('http://127.0.0.1:41101','POST','/api/v1/models',
                                {'parameters':{'base_url':'https://example.com/v1'}},'synthetic')
            opener.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS integration')
    def test_actual_syscall_network_and_write_confinement(self):
        with tempfile.TemporaryDirectory(prefix='rt055-') as td:
            root=Path(td);(root/'audit').mkdir();(root/'builder').mkdir()
            (root/'builder/public-probe').write_text('unchanged')
            self.assertTrue(runtime.network_probe(root)['passed'])
            program="""import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);(root/'owned-output').write_text('public')
denied=False
try:(root.parent/'outside-rt055-public-probe').write_text('public')
except PermissionError:denied=True
private_denied=False
try:(root/'builder/public-probe').write_text('mutated')
except PermissionError:private_denied=True
print(json.dumps({'outside_write_denied':denied,'private_write_denied':private_denied}))
"""
            p=subprocess.run(['/usr/bin/sandbox-exec','-f',str(root/'runtime-policy/network.sb'),sys.executable,'-c',program,str(root)],capture_output=True,env=runtime.clean_env(root),timeout=30)
            self.assertEqual(p.returncode,0);self.assertTrue(json.loads(p.stdout)['outside_write_denied'])
            self.assertEqual((root/'owned-output').read_text(),'public')
            self.assertTrue(json.loads(p.stdout)['private_write_denied'])
            self.assertEqual((root/'builder/public-probe').read_text(),'unchanged')

    @unittest.skipUnless(sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS integration')
    def test_sidecar_real_requests_record_calls_traces_and_denied_egress(self):
        with tempfile.TemporaryDirectory(prefix='rt055-') as td:
            root=Path(td);(root/'audit').mkdir();runtime.network_probe(root)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            program="""import runpy,sys,types
m=types.ModuleType('sentence_transformers')
class Model:
 def __init__(self,*a,**k):pass
 def get_sentence_embedding_dimension(self):return 2
 def encode(self,texts,**k):return [[0.0,1.0] for _ in texts]
m.SentenceTransformer=Model;sys.modules['sentence_transformers']=m
script=sys.argv[1];sys.argv=sys.argv[1:];runpy.run_path(script,run_name='__main__')
"""
            script=Path(native.__file__).parent/'rt055_embed_sidecar.py'
            with (root/'sidecar.log').open('wb') as log:
                p=runtime.spawn(root,[sys.executable,'-c',program,str(script),'--port',str(port),'--model','synthetic','--hf-home',str(root/'hf'),'--privacy-probe'],env=runtime.clean_env(root),stdout=log,stderr=subprocess.STDOUT)
            base=f'http://127.0.0.1:{port}'
            try:
                native.ops.wait_for_http(base+'/health',30,process=p)
                req=urllib.request.Request(base+'/v1/embeddings',data=b'{"input":"PUBLIC_SIDECAR_NORMAL"}',headers={'Content-Type':'application/json','traceparent':'public-trace'})
                with urllib.request.urlopen(req,timeout=10) as resp:self.assertEqual(resp.status,200)
                with urllib.request.urlopen(base+'/health',timeout=10) as resp:stats=json.load(resp)
                self.assertEqual(stats['embedding_calls'],1);self.assertEqual(stats['embedding_successes'],1);self.assertEqual(stats['trace_headers'],1)
                with urllib.request.urlopen(base+'/privacy-probe',timeout=10) as resp:self.assertTrue(json.load(resp)['denied'])
            finally:native.ops.stop_process(p)
            self.assertNotIn('PUBLIC_SIDECAR_NORMAL',(root/'sidecar.log').read_text())


class ObserverTests(unittest.TestCase):
    def test_bootstrap_is_not_native_and_effective_jvm_flag_is_observed(self):
        with tempfile.TemporaryDirectory(prefix='rt055-') as td:
            root=Path(td);(root/'resources').mkdir()
            (root/'resources/process-123.json').write_text(json.dumps({'pid':123,'argv':[str(root/'opensearch/bin/opensearch')]}))
            o=gate.Observer(root)
            def row(text):return subprocess.CompletedProcess([],0,stdout=text)
            with patch.object(gate.subprocess,'run',return_value=row(f'/bin/bash {root}/opensearch/bin/opensearch')):
                o.sample()
            self.assertEqual(o.rows,{})
            env={**runtime.clean_env(root),'OPENSEARCH_JAVA_OPTS':''}
            cmd=f'{root}/jdk/java -Djava.net.preferIPv4Stack=true org.opensearch.bootstrap.OpenSearch'
            with patch.object(gate.subprocess,'run',side_effect=[row(cmd),row('n127.0.0.1:41101')]),patch.object(gate.ops,'process_environment',return_value=env):o.sample()
            self.assertTrue(o.rows[123]['env_valid']);self.assertEqual(o.rows[123]['listeners'],1)
            with patch.object(gate.subprocess,'run',side_effect=[row(cmd.replace('-Djava.net.preferIPv4Stack=true','')),row('n127.0.0.1:41101')]),patch.object(gate.ops,'process_environment',return_value=env):o.sample()
            self.assertFalse(o.rows[123]['env_valid'])




class GateTests(unittest.TestCase):
    def fixture(self):
        return {'search_normal_calls': 1, 'native_normal_calls': 1,
                'authenticated_query_error_code': 400, 'authenticated_title_error_code': 400,
                'authenticated_session_verified': True, 'embedding_calls': 2,
                'embedding_successes': 2, 'trace_header_observations': 0,
                'runtime_environment_verified': True, 'observed_kinds': ['search','native','sidecar'],
                'loopback_listener_kinds': ['search','native','sidecar'],
                'socket_samples': 3, 'external_socket_observations': 0, 'observer_errors': 0,
                'external_probes_denied': True, 'network_policy_verified': True,'nonloopback_model_rejected':True,
                'log_files_scanned': 3, 'log_canary_hits': 0, 'forbidden_reads': 0,
                'search_started': True, 'native_started': True, 'sidecar_started': True,
                'model_endpoint_verified': True, 'native_trace_response_headers': 0}

    def test_real_evidence_required_for_every_gate(self):
        good=self.fixture();self.assertTrue(gate.evaluate(good)['passed'])
        for key,value in (('search_normal_calls',0),('native_normal_calls',0),('authenticated_session_verified',False),('authenticated_query_error_code',401),('authenticated_title_error_code',200),('embedding_calls',0),('embedding_successes',0),('trace_header_observations',1),('runtime_environment_verified',False),('external_socket_observations',1),('observer_errors',1),('external_probes_denied',False),('network_policy_verified',False),('nonloopback_model_rejected',False),('log_files_scanned',0),('log_canary_hits',1),('forbidden_reads',1),('search_started',False),('native_started',False),('sidecar_started',False),('socket_samples',0),('model_endpoint_verified',False),('native_trace_response_headers',1)):
            bad=copy.deepcopy(good);bad[key]=value
            with self.subTest(key=key):self.assertFalse(gate.evaluate(bad)['passed'])
        for key in ('observed_kinds','loopback_listener_kinds'):
            bad=copy.deepcopy(good);bad[key]=['search'];self.assertFalse(gate.evaluate(bad)['passed'])

    def test_legacy_true_without_runtime_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'audit').mkdir()
            (root/'audit/confidentiality-gate.json').write_text('{"passed":true}')
            self.assertFalse(runtime.privacy_passed(root))

    def test_recovery_binds_actual_sources_and_preserves_failed_receipt(self):
        with tempfile.TemporaryDirectory(prefix='rt055-') as td:
            root=Path(td);(root/'audit').mkdir();(root/'impl').mkdir()
            old=b'{"passed":false}'
            (root/'audit/confidentiality-gate.json').write_bytes(old)
            obs=root/'audit/confidentiality-recovery-observations.json';obs.write_text(json.dumps(self.fixture()))
            for name in runtime.PRIVACY_SOURCE_FILES:(root/'impl'/name).write_text('public synthetic')
            binding={'schema':'cwk.rt055.privacy-recovery-binding.v1','run_id':root.name.removeprefix('rt055-'),
                     'status':'READY_TO_FREEZE','source_files':{n:runtime.ops.sha_file(root/'impl'/n) for n in runtime.PRIVACY_SOURCE_FILES},
                     'observations_sha256':runtime.ops.sha_file(obs)}
            (root/'audit/confidentiality-recovery.json').write_text(json.dumps(binding))
            self.assertTrue(runtime.privacy_passed(root))
            (root/'impl/rt055_runtime.py').write_text('drift')
            self.assertFalse(runtime.privacy_passed(root))
            self.assertEqual((root/'audit/confidentiality-gate.json').read_bytes(),old)

    def test_running_logs_not_empty_list_and_both_canaries_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertEqual(gate.scan_logs(root, ['normal-canary','error-canary']), (0,0))
            (root/'native.log').write_text('service started\n')
            self.assertEqual(gate.scan_logs(root,['normal-canary','error-canary']), (1,0))
            for needle in ('normal-canary','error-canary'):
                (root/'native.log').write_text(needle)
                self.assertEqual(gate.scan_logs(root,['normal-canary','error-canary']), (1,1))

    def test_pre_freeze_recovery_refuses_any_consumption_or_freeze(self):
        with tempfile.TemporaryDirectory(prefix='rt055-') as td:
            root=Path(td);root.chmod(0o700);(root/'.rt055-owned').touch()
            gate.assert_synthetic_root(root)
            for name in ('freeze/freeze-receipt.json','consumption/a.claim','builder/private-candidates.json','verifier/private-verified.json'):
                p=root/name;p.parent.mkdir(exist_ok=True);p.touch()
                with self.assertRaises(RuntimeError):gate.assert_synthetic_root(root)
                p.unlink()


if __name__ == '__main__':unittest.main()
