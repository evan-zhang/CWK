# RT-032 activation fixtures

Every file in this directory is **synthetic, sanitized test data**. None of it
came from a real CWork or DocDB instance, none of it describes a real person,
and none of it contains a credential — `config.json` carries the *name* of an
environment variable and a placeholder that is self-evidently not a key.

Most fixtures say so in their own `_comment` field. `scope.json` cannot: the
authorized visible scope is a **closed schema** (`normalize_scope` in
`scripts/cwk_activation_contract.py`) that accepts exactly four keys and
rejects any fifth, including a comment.

That is not an oversight to route around. The scope object is written verbatim
into `discovery-report.json` and read back to the user as the authoritative
statement of what they are authorising; a free-text field there is a channel
straight into the sentence their consent rests on. So the schema stays closed,
the fixture stays comment-free, and the provenance note lives here instead.

`scope.json` is recognisable as synthetic from its content:
`subject_ref: "fixture-user-a"`.

`tests/test_rt032_activation_contract.py` pins this arrangement — it asserts
that the shipped fixture is already in normal form, so if the schema ever
changed such that the documented example needed fixing, that would fail here
rather than being silently patched in the fixture.
