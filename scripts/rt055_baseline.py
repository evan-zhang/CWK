#!/usr/bin/env python3
"""OPS-private read-only production baseline. Never prints identifiers/digests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'verifier'))
import rt055_opslib as ops
import rt055_window as window
import kb_storage
STATUS = None
state = {}

def save():
    ops.write_private_json(STATUS, state)


def command(*args):
    p = subprocess.run(args, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError('readonly_command_failed')
    return p.stdout.decode(errors='replace')


def http(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=30) as response:
        data = response.read()
        assert response.status == 200
    return data


def fingerprint(value):
    return ops.sha_text(ops.canonical_json(value))


def local_state():
    rows = ops.find_gateway_processes()
    assert len(rows) == 3
    commands = {int(parts[0]): parts[1] for line in command('/bin/ps', '-axo', 'pid=,command=').splitlines()
                if len(parts := line.strip().split(None, 1)) == 2}
    gateway, configs, health = [], {}, {}
    for row in rows:
        argv = shlex.split(commands[row['pid']])
        cwd_lines = command('/usr/sbin/lsof', '-a', '-p', str(row['pid']), '-d', 'cwd', '-Fn').splitlines()
        cwd = Path(next(line[1:] for line in cwd_lines if line.startswith('n')))
        files = [cwd / arg for arg in argv if arg.endswith('.py')]
        for flag in ('--tokens-file', '--config'):
            if flag in argv:
                files.append(cwd / argv[argv.index(flag) + 1])
        for optional in (cwd / '.env', cwd / 'cwk-mirror.local.json'):
            if optional.is_file():
                files.append(optional)
        assert files and all(path.is_file() for path in files)
        configs[str(row['port'])] = {'files': {str(path): ops.sha_file(path) for path in files},
            'environment_sha256': fingerprint(ops.process_environment(row['pid'], (*ops.ENV_NAMES, argv[argv.index('--admin-key-env')+1])))}
        gateway.append(row)
        raw = http('http://127.0.0.1:%d/health' % row['port'])
        payload = json.loads(raw)
        assert payload.get('ok') is True and payload.get('read_only') is True
        # Store raw response fingerprint, compare all fields except the declared
        # observation timestamp. Both before/after use this exact projection.
        canonical = dict(payload)
        canonical.pop('at', None)
        health[str(row['port'])] = {'raw_sha256': ops.sha_bytes(raw), 'stable_sha256': fingerprint(canonical), 'http_200': True}
    docker = shutil.which('docker')
    fallback = Path('/Applications/Docker.app/Contents/Resources/bin/docker')
    if not docker and fallback.is_file():
        docker = str(fallback)
    if not docker:
        raise RuntimeError('container_inventory_unmeasured')
    ids = command(docker, 'ps', '-a', '--no-trunc', '--format', '{{.ID}}').splitlines()
    inspect = json.loads(command(docker, 'inspect', *ids)) if ids else []
    containers, indices = [], {}
    for item in inspect:
        containers.append({'id': item['Id'], 'name': item['Name'], 'image': item['Image'],
            'running': item['State']['Running'], 'started_at': item['State']['StartedAt'],
            'restart_count': item['RestartCount'], 'config_sha256': fingerprint(item['Config']),
            'host_config_sha256': fingerprint(item['HostConfig']), 'mounts_sha256': fingerprint(item['Mounts'])})
        image = item.get('Config', {}).get('Image', '').casefold()
        if 'opensearch' in image or 'elasticsearch' in image:
            ports = item['NetworkSettings']['Ports'].get('9200/tcp')
            if not ports:
                raise RuntimeError('existing_index_endpoint_unmeasured')
            port = int(ports[0]['HostPort'])
            base = 'http://127.0.0.1:' + str(port)
            listing = json.loads(http(base + '/_cat/indices?format=json&bytes=b'))
            stats = json.loads(http(base + '/_stats/store,docs'))
            indices[item['Id']] = {
                'aliases': json.loads(http(base + '/_alias')),
                'settings': json.loads(http(base + '/_settings')),
                'mappings': json.loads(http(base + '/_mapping')),
                'indices': sorted([{'index': row['index'], 'uuid': row['uuid'], 'status': row['status'],
                                    'docs.count': row.get('docs.count'), 'docs.deleted': row.get('docs.deleted'),
                                    'pri.store.size': row.get('pri.store.size')} for row in listing], key=lambda row: row['index']),
                'statistics': {name: {'uuid': values['uuid'], 'primaries': values['primaries']}
                               for name, values in stats['indices'].items()}}
    # A native search process without a separately inventoried endpoint fails
    # closed instead of treating the container list as the complete index set.
    native = [pid for pid, text in commands.items() if 'org.opensearch.bootstrap.OpenSearch' in text]
    if native:
        raise RuntimeError('native_index_endpoint_unmeasured')
    volume_names = command(docker,'volume','ls','-q').splitlines()
    volumes = json.loads(command(docker,'volume','inspect',*volume_names)) if volume_names else []
    service_lines = command('launchctl', 'list').splitlines()[1:]
    services = sorted(line.split(None, 2)[2] for line in service_lines if len(line.split(None, 2)) == 3)
    return {'gateways': gateway, 'configuration': configs, 'health': health,
            'containers': sorted(containers, key=lambda row: row['id']), 'services': services,
            'existing_search_indices': indices, 'native_search_processes': native, 'volumes':volumes}


def complete_nas_listing(backend):
    stack=[''];visited=set();files=[]
    while stack:
        current=stack.pop()
        if current in visited:raise RuntimeError('nas_directory_repeated')
        visited.add(current);offset=0;names=set();total=None
        while total is None or offset<total:
            payload=backend._get('entry.cgi',{'api':'SYNO.FileStation.List','version':'2','method':'list',
                'folder_path':json.dumps(backend._remote(current) if current else backend._remote_root()),
                'offset':offset,'limit':1000,'additional':json.dumps(['type'])})
            rows=payload.get('files');reported=payload.get('total')
            if not isinstance(rows,list) or type(reported) is not int or (total is not None and total!=reported):raise RuntimeError('nas_pagination_invalid')
            total=reported
            if not rows and offset<total:raise RuntimeError('nas_pagination_empty')
            for row in rows:
                name=row['name']
                if not isinstance(name,str) or name in names or '/' in name or name in ('.','..'):raise RuntimeError('nas_entry_invalid')
                names.add(name);relative=f'{current}/{name}' if current else name
                if row['isdir']:stack.append(relative)
                else:files.append(relative)
            offset+=len(rows)
        if offset!=total:raise RuntimeError('nas_pagination_overrun')
    if len(files)!=len(set(files)):raise RuntimeError('nas_file_repeated')
    return sorted(files),sorted(visited)


def nas_state():
    envs = ops.gateway_source_envs()
    result = {}
    for kb in ops.LIBRARIES:
        state['phase'] = 'NAS'
        save()
        backend = kb_storage.FileStationBackend.from_env(envs[kb], prefix=kb, timeout=120)
        try:
            files, directories = complete_nas_listing(backend)
            assert files and len(files) == len(set(files))
            metadata = {}
            for start in range(0, len(files), 100):
                batch = files[start:start + 100]
                remote = {backend._remote(rel): rel for rel in batch}
                payload = backend._get('entry.cgi', {'api': 'SYNO.FileStation.List', 'version': '2', 'method': 'getinfo',
                    'path': json.dumps(list(remote)), 'additional': json.dumps(['size','time','type'])})
                rows = payload.get('files')
                assert isinstance(rows, list) and len(rows) == len(batch)
                for row in rows:
                    rel = remote[row['path']]
                    additional = row['additional']
                    times = additional['time']
                    assert 'mtime' in times and 'size' in additional
                    metadata[rel] = {'size': additional['size'], 'mtime': times['mtime'], 'isdir': bool(row['isdir'])}
            assert set(metadata) == set(files)
            # Existing lexical/raw index names AND their statistics are measured
            # directly, in addition to the native search-container index list.
            result[kb] = {'files': metadata, 'directories':directories, 'scope_complete':True, 'metadata_sha256': fingerprint(metadata),
                          'index_content_fingerprints':{rel:ops.sha_bytes(backend.read(rel)) for rel in files if rel.startswith('_system/') and any(x in rel.lower() for x in ('index','alias','config','readiness'))},
                          'existing_indices': {rel: row for rel,row in metadata.items() if rel.startswith('_system/') and 'index' in rel},
                          'configuration_sha256': ops.sha_bytes(backend.read('kb.json'))}
        finally:
            backend.logout()
        state['libraries_measured'] += 1
        save()
    return result


def compare_values(before,value):
    a, b = before['local'], value['local']
    health_equal = all(a['health'][port]['stable_sha256'] == b['health'][port]['stable_sha256'] and b['health'][port]['http_200'] for port in a['health'])
    comparison = {'nas_unchanged': before['nas'] == value['nas'],
        'gateway_unchanged': a['gateways'] == b['gateways'] and health_equal,
        'gateway_health_content_unchanged': health_equal,
        'existing_indices_unchanged': a['existing_search_indices'] == b['existing_search_indices'] and all(before['nas'][kb]['existing_indices'] == value['nas'][kb]['existing_indices'] and before['nas'][kb]['index_content_fingerprints']==value['nas'][kb]['index_content_fingerprints'] for kb in ops.LIBRARIES),
        'production_config_unchanged': a['configuration'] == b['configuration'] and all(before['nas'][kb]['configuration_sha256'] == value['nas'][kb]['configuration_sha256'] for kb in ops.LIBRARIES),
        'containers_unchanged': a['containers'] == b['containers'], 'volumes_unchanged':a['volumes']==b['volumes'], 'services_unchanged': a['services'] == b['services'],
        'three_gateways_healthy': all(row['http_200'] for row in b['health'].values()), 'all_items_measured': False}
    # This collector has complete NAS metadata, but not all file bytes,
    # every production dependency/process, volume contents, or an owner-
    # enumerated registry of ALL search endpoints. Preserve measured drift;
    # an equal projection cannot attest the stronger complete invariant.
    for field in ('nas_unchanged', 'existing_indices_unchanged', 'production_config_unchanged'):
        comparison[field + '_measured_projection'] = comparison[field]
        comparison[field] = False if comparison[field] is False else None
    if not all(comparison[k] for k in ('containers_unchanged', 'volumes_unchanged', 'services_unchanged')):
        comparison['production_config_unchanged'] = False
    return comparison


def main(argv=None):
    import argparse
    global STATUS, state
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('before', 'after'))
    parser.add_argument('--window-id')
    args = parser.parse_args(argv); mode = args.mode
    w = window.directory(ROOT,args.window_id) if args.window_id else ROOT
    binding = window.envelope(ROOT,args.window_id,mode) if args.window_id else {}
    if args.window_id and mode=='before':
        from rt055_scoring_input import before_precheck
        before_precheck(ROOT,args.window_id)
    (w/'status').mkdir(parents=True,mode=0o700,exist_ok=True)
    os.umask(0o077)
    STATUS = w / 'status' / ('baseline-' + mode + '.json')
    state = {'status': 'RUNNING', 'phase': 'LOCAL', 'process_id': os.getpid(), 'libraries_measured': 0, 'started_at':time.time(), **binding}
    claim=w/'status'/('baseline-'+mode+'.claim')
    window.write_once(claim,binding)
    save()
    try:
        if args.window_id and mode=='after':window.baseline(ROOT,args.window_id,'before')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            value = {'local': local_state(), 'nas': nas_state()}
            value.update(binding);value['observed_at'] = time.time()
            artifact=w / 'audit' / ('production-' + mode + '.json')
            window.write_once(artifact, value)
            if args.window_id:state.update(artifact=str(artifact.relative_to(ROOT)),artifact_sha256=ops.sha_file(artifact))
        if mode == 'after':
            before = window.baseline(ROOT,args.window_id,'before')[0] if args.window_id else ops.read_json(ROOT / 'audit/production-before.json')
            comparison=compare_values(before,value)
            if args.window_id:
                comparison.update(window.envelope(ROOT,args.window_id,'comparison'),before_sha256=ops.sha_file(w/'audit/production-before.json'),after_sha256=ops.sha_file(artifact))
            window.write_once(w / 'audit/production-comparison.json', comparison)
        state.update(status='PASS', phase='COMPLETE', finished_at=time.time())
    except Exception as exc:
        state.update(status='FAIL', error_kind=type(exc).__name__, finished_at=time.time())
    save()
    return 0 if state['status'] == 'PASS' else 3


if __name__ == '__main__':
    raise SystemExit(main())
