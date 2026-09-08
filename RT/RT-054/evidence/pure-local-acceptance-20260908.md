# RT-054 pure-local bounded-read acceptance

This evidence is local-only and contains no host, path, credential, or other
environment value. It does not make any claim about external cleanup.

The only acceptance command is `make rt054-pure-local`. Its runner constructs
the child environment from a fixed minimal whitelist (path, locale, and local
temporary-directory variables), then sets `CWK_RT054_PURE_LOCAL=1`. It does
not pass NAS, application-key, token, password, secret, or key variables from
the parent.

## 2026-09-08 result

- Full bounded-read + storage + P0 + guard run: `Ran 90 tests` / `OK
  (skipped=1)` / exit 0.
- The verbose result named `NasSmokeTests.test_probe_directory_round_trip` and
  recorded `SKIP-reason: RT-054 pure-local gate`.
- Bounded-read single module: `Ran 21 tests` / `OK` / exit 0.
- Full `make test` under the same reconstructed parent environment: `Ran 2453
  tests` / `OK (skipped=9)` / exit 0; its three local smoke stages completed.
- Canary regression injects fake NAS and credential-shaped parent variables;
  its runner child still reports the forced skip. A patched
  `FileStationBackend.from_env` trap is not called while the skipped suite
  runs, proving the smoke backend is not instantiated and therefore performs
  no network or write operation.

The earlier 87-test combination was capable of selecting NAS smoke in its
parent environment. It is not pure-local evidence and is retained only as a
safety-category record.
