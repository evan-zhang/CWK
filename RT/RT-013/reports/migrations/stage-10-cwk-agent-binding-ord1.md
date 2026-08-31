# Stage 10 — RT-013 `cwk_agent_binding.py` concurrency remediation

## Scope

This migration evolves `scripts/cwk_agent_binding.py` from the RT-016 genesis
SHA `9122d1ee6a637e811f6fa03c4eb7cf78960e458f2cd71bce5cdcadc8d426fdcd`
to `2d390d6fa1a5b84e1dcc137e64c642f3a1a9cb010e009fa5c7a6e00e076030c4`
under RT-013 ordinal 1. It changes no schema, CLI command, credential boundary,
tenant-state rule, receipt format, or production configuration.

## Defect and fix

`BindingRegistry.bind()` checked for an active/suspended record and later read
the prospective predecessor again before CAS. A concurrent winner could
commit between those reads. The loser then treated the newly visible active
record as if it were a revoked predecessor, copied its SHA, and legitimately
committed `binding_epoch=2` under the lock. High-count reproduction produced
both `ok:1` and `ok:2` in one bind race.

The late predecessor read now accepts only `status == "revoked"`. Any active
or suspended record observed at that point raises `BindingConflictError`
before journal creation or mutation. A genuinely revoked predecessor remains
eligible, so the existing two-step rebind contract is preserved.

## Acceptance evidence

The Stage-10 receipt binds these canonical tests:

- the existing parallel exactly-one-wins regression;
- a deterministic two-thread barrier test that forces the early-check/late-read
  interleaving and proves one success, one conflict, final epoch 1, one receipt,
  and exactly one tenant `auth_epoch` bump;
- same-tenant and cross-tenant duplicate-bind rejection; and
- the existing revoked-prior two-step rebind behavior.

The remediation is local-only. It does not use credentials, contact CWork,
write Cloud/DocDB, change cron, deploy, push, or rewrite the original RT-013
acceptance report. Because RT-013 runtime bytes changed, the legacy report is
historical provenance only; a new independent Stage-10 acceptance report must
bind the eventual candidate commit before G2 or any later release gate may pass.
