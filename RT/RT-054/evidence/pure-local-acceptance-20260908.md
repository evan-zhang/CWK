# RT-054 pure-local bounded-read acceptance

This evidence is local-only and contains no host, path, credential, or other
environment value. It does not make any claim about external cleanup.

The only acceptance command is `make rt054-pure-local`. It accepts no test
selection or variable-selected test list. Make starts the runner in a fixed
outer environment; the runner then resolves its current interpreter to an
absolute path and starts a child with `-I -S`, a fixed bootstrap and fixed
test argv. The child has only `CWK_RT054_PURE_LOCAL=1`, fixed C locale, and a
fresh mode-0700 local temporary root for HOME and TMPDIR. It receives no
operator PATH, HOME, TMP, locale, Python import configuration or credential.

Before loading tests, the bootstrap blocks socket connection attempts,
`FileStationBackend.from_env`, and FileStation writes that use a default
network transport. It also plants a canary `.env` in the temporary root and
raises if any `.env` is opened. LocalFS and fake/injected transports remain
available to the unit tests.

## 2026-09-08 result

- Fixed bounded-read + storage + P0 + guard run: `Ran 93 tests` / `OK
  (skipped=1)` / exit 0.
- The verbose result named `NasSmokeTests.test_probe_directory_round_trip` and
  recorded `SKIP-reason: RT-054 pure-local gate`.
- The bootstrap completion marker was `RT054_PURE_LOCAL_BOOTSTRAP=1` with
  `socket=0`, `filestation_from_env=0`, `filestation_write=0`, and `dotenv=0`.
- A second run uses a parent carrying synthetic host, user, password, share,
  certificate, token, app key, secret, private-key, PATH, TMP/TEMP,
  PYTHONPATH/PYTHONHOME/user-site and sitecustomize values. It has the same
  test result and marker; no synthetic value appears in argv, child env or
  output.

The earlier 87- and 90-test combinations are revoked as pure-local evidence:
they did not provide this fixed outer launch, isolated interpreter, temporary
home, dotenv trap and runtime I/O guard. This record makes no external cleanup
claim and does not record any external host, path or credential.
