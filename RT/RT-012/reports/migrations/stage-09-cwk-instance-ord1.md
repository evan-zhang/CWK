# RT-012 script evolution migration — stage 09

- Owner: `RT-012`
- Target: `scripts/cwk_instance.py`
- Ordinal: `1`
- Reason: close the instance-root ancestor-symlink and post-open replacement gap before the first reproducible PR-001 checkpoint.

The genesis implementation validated only the final `CWK_INSTANCE_ROOT` leaf with `lstat`, then reopened the textual path for every operation. On macOS/POSIX an ancestor symlink or a rename-and-replace sequence could therefore retarget an existing `InstanceLayout` to a different inode.

Stage 09 changes the compatibility surface without changing public tenant, registry, CLI, schema, exit-code, or directory-layout contracts. `InstanceLayout.open()` now walks every canonical absolute-path component using `lstat`-equivalent `os.stat(..., follow_symlinks=False)`, `openat(O_DIRECTORY|O_NOFOLLOW)`, and `fstat`; it records the complete device/inode chain and retains the final root FD as an anchor. Every `root_fd()` replays and compares that chain. A race can yield the original inode or a stable `InstanceRootError`, never the replacement tree.

The layout now has explicit idempotent `close()` and context-manager lifecycle. `root_fd()` duplicates the anchor while holding the lifecycle lock, then releases that lock before rewalking and yielding, so RT-013+ dirfd transactions remain concurrent. A close may complete while a previously yielded FD stays valid; every new access after close fails. Copy and deepcopy deliberately return the same lifecycle handle; pickle is rejected so anchor-FD ownership cannot be duplicated. Hosts without the required dir-fd and nofollow capabilities fail closed. Raw `/`, dot/dotdot, duplicate separators, and trailing separators are rejected.

Compatibility fixtures that used macOS `/var` through its `/private/var` symlink now explicitly pass `Path(tmp).resolve()`; production code does not call `realpath` and does not follow user-supplied symlinks. The sole exception is the hash-pinned legacy VG-A helper: its bytes remain unchanged because the immutable VG-A receipt may never be rewritten or rotated. Supplemental VG-A regression on this candidate therefore runs with an explicitly canonical `TMPDIR` (for example `/private/tmp`) and is new evidence, not a re-signing of the historical receipt. The historical RT-012 acceptance report is not rewritten and does not establish PASS for this remediation. Independent acceptance must rerun against the final checkpoint.
