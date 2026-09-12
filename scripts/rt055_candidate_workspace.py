"""Amendment 6: controller-owned runtime leases, never holdout or query logic.

Only UUID/window/candidate/attempt constructed paths are writable by children.
Ownership and archived logs live outside the mutable runtime. All receipts and
log scans stay private on OPS; callers export counts/booleans only.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import rt055_opslib as ops
import rt055_window as window

AREAS=('data','logs','home','tmp')

@dataclass(frozen=True)
class Workspace:
    root: Path
    window_id: str
    candidate: str
    attempt_id: str
    synthetic: bool=False
    migration_id: str | None=None
    @property
    def base(self):return self.root/'candidate-runtime'/self.window_id/self.candidate/self.attempt_id
    @property
    def ledger(self):
        if self.migration_id:
            return self.root/'executioner-migrations'/self.migration_id/'main-startup'/self.window_id/self.candidate/self.attempt_id/'workspace.json'
        parent=(self.root/'audit/candidate-workspaces'/self.window_id/self.candidate/self.attempt_id
                if self.synthetic else window.directory(self.root,self.window_id)/('run-'+self.candidate)/'attempts'/self.attempt_id)
        return parent/'workspace.json'
    @property
    def archive(self):return self.ledger.parent/'runtime-log-archive'
    def identity(self):
        return {'schema':'cwk.rt055.candidate-workspace.v1','run_id':self.root.name.removeprefix('rt055-'),
                'window_id':self.window_id,'candidate':self.candidate,'attempt_id':self.attempt_id,'synthetic':self.synthetic,'migration_id':self.migration_id}


def checked(root,path):
    root=Path(root).absolute();path=Path(path).absolute()
    if '..' in path.parts or not path.is_relative_to(root):raise RuntimeError('candidate_workspace_escape')
    for node in (path,*path.parents):
        if node.is_symlink():raise RuntimeError('candidate_workspace_symlink')
        if node==root:break
    return path


def _identity(s):
    window.identifier(s.window_id);window.identifier(s.attempt_id)
    if s.candidate not in ('a','b') or type(s.synthetic) is not bool:raise ValueError('candidate_workspace_identity')
    if s.migration_id:window.identifier(s.migration_id)
    checked(s.root,s.base);checked(s.root,s.ledger)
    if s.root.stat().st_mode&0o077 or not (s.root/'.rt055-owned').is_file():raise RuntimeError('candidate_workspace_private_root')


def create(root,window_id,candidate,attempt_id,*,synthetic=False,migration_id=None):
    s=Workspace(Path(root),window_id,candidate,attempt_id,synthetic,migration_id);_identity(s)
    if migration_id:
        if not synthetic:raise RuntimeError('candidate_probe_requires_synthetic')
        probe_profile(s,'loopback')
    elif synthetic:
        if (s.root/'verifier/private-verified.json').exists():raise RuntimeError('synthetic_workspace_in_formal_root')
    else:
        window.require_open(s.root,window_id)
        claim=ops.read_json(s.ledger.parent/'claim.json')
        if any(claim.get(k)!=v for k,v in {**window.envelope(s.root,window_id,'execution-attempt'),'candidate':candidate}.items()):
            raise RuntimeError('candidate_workspace_claim_mismatch')
    # Reserve outside child write scope BEFORE allocating. A failed allocation
    # and even a completed cleanup can never silently reuse the same identity.
    window.write_once(s.ledger.parent/'workspace-allocation.claim',s.identity())
    for p in (s.root/'candidate-runtime',s.base.parent.parent,s.base.parent):
        checked(s.root,p);p.mkdir(mode=0o700,exist_ok=True)
        if p.stat().st_mode&0o077:raise RuntimeError('candidate_workspace_permissions')
    s.base.mkdir(mode=0o700,exist_ok=False)
    for area in AREAS:(s.base/area).mkdir(mode=0o700)
    st=s.base.stat()
    window.write_once(s.ledger,{**s.identity(),'device':st.st_dev,'inode':st.st_ino,'uid':st.st_uid})
    return validate(s)


def probe_profile(s,kind):
    from rt055_runtime_readiness import verify,directory,KINDS
    if not s.synthetic or not s.migration_id:raise RuntimeError('candidate_probe_identity')
    window.require_open(s.root,s.window_id)
    if window.directory(s.root,s.window_id).exists():raise RuntimeError('candidate_probe_after_before')
    verify(s.root,s.window_id,s.migration_id)
    return directory(s.root,s.window_id)/KINDS[kind]


def validate(s,*,window_id=None):
    _identity(s)
    if window_id is not None and s.window_id!=window_id:raise RuntimeError('candidate_workspace_cross_window')
    value=ops.read_json(s.ledger);st=s.base.stat()
    if value!={**s.identity(),'device':st.st_dev,'inode':st.st_ino,'uid':st.st_uid} or st.st_mode&0o777!=0o700:
        raise RuntimeError('candidate_workspace_ownership')
    if st.st_uid!=os.getuid():raise RuntimeError('candidate_workspace_uid')
    for p in s.base.rglob('*'):
        checked(s.root,p);mode=p.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):raise RuntimeError('candidate_workspace_special_file')
        if p.is_file() and p.stat().st_nlink!=1:raise RuntimeError('candidate_workspace_hardlink')
    return s


def file(s,area,name):
    _identity(s)
    if area not in AREAS or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}',name) or name in ('.','..'):
        raise RuntimeError('candidate_workspace_component')
    p=checked(s.root,s.base/area/name)
    if p.exists() and p.is_file() and p.stat().st_nlink!=1:raise RuntimeError('candidate_workspace_hardlink')
    return p


def search_launcher(root,s,component,*,create=False):
    original=checked(root,root/'opensearch/bin/opensearch')
    text=original.read_text()
    if text.count('<<<')!=2 or text.count('<<<"$KEYSTORE_PASSWORD"')!=2:
        raise RuntimeError('candidate_launcher_stdin_template_drift')
    stdin=file(s,'data',component+'-keystore-stdin')
    if create:
        with stdin.open('xb') as f:f.write(b'\n')
        stdin.chmod(0o600)
    if stdin.read_bytes()!=b'\n':raise RuntimeError('candidate_launcher_stdin_drift')
    # Same empty password + newline as the credential-free original here-string.
    # Bash 3.2's unlinked temporary fd cannot satisfy strict path confinement.
    # Supply an exact owned file instead of granting /tmp or orphan-fd access.
    text=text.replace('<<<"$KEYSTORE_PASSWORD"','<"$RT055_KEYSTORE_STDIN"')
    return ['/bin/bash','-c',text,str(original)],stdin


def jvm_config(text,s,component):
    # JVM opens each -Xlog destination while parsing: a later override cannot
    # undo an earlier denied open. Rewrite only destinations in a private copy.
    gc=file(s,'logs','gc-'+component+'.log')
    fatal=file(s,'logs','hs_err-'+component+'.log')
    heap=file(s,'data','heapdump-'+component+'.hprof')
    if any(c.isspace() for c in str(s.base)):raise RuntimeError('candidate_jvm_path_whitespace')
    if 'file=logs/gc.log' not in text:raise RuntimeError('candidate_jvm_log_template_drift')
    text=text.replace('file=logs/gc.log','file='+str(gc))
    text=text.replace('-XX:ErrorFile=logs/hs_err_pid%p.log','-XX:ErrorFile='+str(fatal))
    text=text.replace('-XX:HeapDumpPath=data','-XX:HeapDumpPath='+str(heap))
    return text


def search_config(root,s,component,*,create=False):
    source=checked(root,root/'opensearch/config');dest=file(s,'data',component+'-config')
    files=sorted(source.rglob('*'))
    if not (source/'jvm.options').is_file():raise RuntimeError('candidate_jvm_config_missing')
    if create:dest.mkdir(mode=0o700,exist_ok=False)
    expected=set()
    for p in files:
        checked(root,p);target=checked(s.root,dest/p.relative_to(source))
        if p.is_dir():
            if create:target.mkdir(mode=0o700,exist_ok=False)
        elif p.is_file():
            data=p.read_bytes()
            if p==source/'jvm.options':data=jvm_config(data.decode(),s,component).encode()
            if create:
                with target.open('xb') as f:f.write(data)
                target.chmod(0o600)
            if target.read_bytes()!=data:raise RuntimeError('candidate_runtime_config_drift')
            expected.add(target)
        else:raise RuntimeError('candidate_runtime_config_special_file')
    if set(p for p in dest.rglob('*') if p.is_file())!=expected:raise RuntimeError('candidate_runtime_config_extra_file')
    return dest


def require_path(s,path,area):
    validate(s)
    if Path(path)!=file(s,area,Path(path).name):raise RuntimeError('candidate_workspace_foreign_path')
    return Path(path)


def policy(s,base_text):
    validate(s)
    # Retain ALL network and ledger denies. Narrow the old root-wide write
    # allowance to this exact lease. No runtime can read a sibling lease.
    allowance='(allow file-write* (subpath %s) (literal "/dev/null"))\n'%json.dumps(str(s.root.resolve()))
    if base_text.count(allowance)!=1:raise RuntimeError('candidate_workspace_policy_template')
    text=base_text.replace(allowance,'(allow file-write* (literal "/dev/null"))\n')
    text+='(deny file-read* (subpath %s))\n'%json.dumps(str((s.root/'candidate-runtime').resolve()))
    # More-specific allowed lease; explicit deny-except prevents an allow from
    # accidentally superseding a broader deny on another SBPL implementation.
    text+='(allow file-read* file-write* (subpath %s))\n'%json.dumps(str(s.base.resolve()))
    # Canonical-path APIs inspect each ancestor. Grant only metadata on these
    # exact known directories; listing, sibling data and all ledgers stay denied.
    for ancestor in (s.base.parent,s.base.parent.parent,s.root/'candidate-runtime'):
        text+='(allow file-read-metadata (literal %s))\n'%json.dumps(str(ancestor.resolve()))
    for area in ('audit','resources','impl','executioner-migrations'):
        p=s.root/area
        if area=='executioner-migrations':
            # Synthetic descendants need their own public dependencies, but
            # main-root candidates cannot inspect source/control migrations.
            if not s.synthetic or s.migration_id:text+='(deny file-read* (subpath %s))\n'%json.dumps(str(p.resolve()))
        else:
            if area!='impl':text+='(deny file-read* (subpath %s))\n'%json.dumps(str(p.resolve()))
        text+='(deny file-write* (subpath %s))\n'%json.dumps(str(p.resolve()))
    return text


def needles(corpus,cases):
    values=set()
    # Every private string field (title/body/filename/id/locator/path/expected),
    # not only warmups. Do not include structural dict keys or emit values.
    def visit(x):
        if isinstance(x,str) and x:values.add(x)
        elif isinstance(x,dict):
            for v in x.values():visit(v)
        elif isinstance(x,(list,tuple,set,frozenset)):
            for v in x:visit(v)
    visit(corpus)
    for case in cases:
        row=case if isinstance(case,dict) else vars(case)
        for k,v in row.items():
            if k not in ('kb_id','library','category','exact','expected_outcome'):visit(v)
    encoded={json.dumps(v,ensure_ascii=ascii)[1:-1] for v in values for ascii in (False,True)}
    return sorted(values|encoded)


def scan(s,values):
    validate(s);paths=[]
    for p in (s.base/'logs').rglob('*'):
        checked(s.root,p)
        if p.is_file():paths.append(p)
    hits=sum(any(n in p.read_text(errors='replace') for n in values if n) for p in paths)
    row={'schema':'cwk.rt055.candidate-log-scan.v1','passed':bool(paths) and bool(values) and hits==0,
         'log_files':len(paths),'hit_files':hits,'needles_present':bool(values),'content_exported':False}
    window.write_once(s.ledger.parent/'log-scan.json',row)
    if not row['passed']:raise RuntimeError('candidate_log_privacy_failed')
    return row


def cleanup(s):
    validate(s)
    for record in (s.root/'resources').glob('process-*.json'):
        row=ops.read_json(record)
        if row.get('workspace')!=s.identity():continue
        observed=subprocess.run(['/bin/ps','-p',str(row['pid']),'-o','command='],capture_output=True,text=True,check=False).stdout
        if str(s.base) in observed:raise RuntimeError('candidate_workspace_process_still_live')
    # Retain private logs, then remove exactly the inode-bound lease. Never
    # delete data-run/data-smoke or traverse another window's runtime tree.
    s.archive.mkdir(mode=0o700,exist_ok=False)
    for p in sorted((s.base/'logs').rglob('*')):
        checked(s.root,p);dest=s.archive/p.relative_to(s.base/'logs')
        if p.is_dir():dest.mkdir(mode=0o700)
        elif p.is_file():
            with dest.open('xb') as f:f.write(p.read_bytes())
            dest.chmod(0o600)
            if ops.sha_file(dest)!=ops.sha_file(p):raise RuntimeError('candidate_log_archive_mismatch')
    validate(s);shutil.rmtree(s.base)
    for p in (s.base.parent,s.base.parent.parent,s.root/'candidate-runtime'):
        try:p.rmdir()
        except OSError:pass  # nonempty siblings are retained, never traversed
    row={**s.identity(),'complete':not s.base.exists(),'remaining_owned_runtime':int(s.base.exists()),'logs_retained':True}
    window.write_once(s.ledger.parent/'workspace-cleanup.json',row)
    return row


def owned(root,window_id):
    window.identifier(window_id);result=[]
    for key in ('a','b'):
        base=window.directory(root,window_id)/('run-'+key)/'attempts'
        for p in sorted(base.glob('*/workspace.json')):
            s=Workspace(root,window_id,key,p.parent.name)
            _identity(s)
            if s.base.exists():result.append(validate(s))
    return result
