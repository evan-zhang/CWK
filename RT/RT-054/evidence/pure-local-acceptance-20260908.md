# RT-054 pure-local bounded-read acceptance

This evidence is local-only and contains no host, path, credential, or other
environment value. It does not make any claim about external cleanup.

The only acceptance command is `make rt054-pure-local`. It accepts no test
selection or variable-selected test list. Make overrides its shell flags and
derives its own realpath rather than trusting caller `CURDIR`; it invokes only
fixed `/bin/sh` and a checked-in launcher by shell-quoted absolute path. The
launcher accepts only `/usr/bin/python3`, rejecting Homebrew, `/usr/local`,
PATH selection, and every candidate symlink. Before `exec`, its shell helper
checks the candidate/target and each component from `/` through the target:
each must be root-owned and not group- or other-writable; the target must be a
regular executable. It uses fixed `/usr/bin/stat` syntax selected only by the
host's Darwin/Linux identity. Any failed check has no fallback and exits 127.
The first Python process is `-I -S`; the launcher does not use the caller's
shell or PATH to choose an interpreter. The runner checks those
isolation flags before any repository import, then starts a child with a fixed
bootstrap and fixed test argv. The child has only `CWK_RT054_PURE_LOCAL=1`,
fixed C locale, and a fresh mode-0700 local temporary root for HOME and
TMPDIR. It receives no operator PATH, HOME, TMP, locale, Python import
configuration or credential.

Before loading repository tests, the bootstrap installs an audit hook for
`open` events whose basename is `.env`, then covers the Python standard open
routes: `builtins.open`, `io.open`, `Path.open`/`read_text`, and `os.open`.
Each negative route fails with a redacted guard error and increments the same
counter exactly once. This is deliberately a standard-Python-open guarantee,
not a claim to cover non-Python readers. It wraps only Python
`socket.socket.connect`, `socket.create_connection`, and
`socket.socket.connect_ex`, plus `FileStationBackend.from_env` and default-
transport FileStation writes. LocalFS and fake/injected transports remain
available to the unit tests. This is not a process-level sandbox and does not
prove that arbitrary syscalls or child processes have zero network activity.

## 2026-09-08 result

- Fixed bounded-read + storage + P0 + guard run: `Ran 99 tests` / `OK
  (skipped=1)` / exit 0.
- The verbose result named `NasSmokeTests.test_probe_directory_round_trip` and
  recorded `SKIP-reason: RT-054 pure-local gate`.
- The bootstrap completion marker was `RT054_PURE_LOCAL_BOOTSTRAP=1` with
  `socket=0`, `filestation_from_env=0`, `filestation_write=0`, and `dotenv=0`.
  The `socket` count covers only the three named Python connection APIs above.
- A local fake-tree negative check rejects both an operator-owned target and a
  symlink; a separate check accepts the current root-owned `/usr/bin/python3`.
- A second run uses direct assignments and MAKEFLAGS for shell, cwd,
  interpreter and PATH canaries, plus a parent `PYTHONPATH` local
  `sitecustomize`/`.pth` marker. It has the same test result and marker; the
  marker does not execute and no canary appears in the recipe or output.

The earlier 87- and 90-test combinations are revoked as pure-local evidence:
they did not provide this fixed outer launch, isolated interpreter, temporary
home, dotenv trap and runtime I/O guard. This record makes no external cleanup
claim and does not record any external host, path or credential. The temporary
canary is a private, non-project, non-NAS temporary write used solely to test
that `.env` reads are blocked; it is not a claim of literal zero writes.
