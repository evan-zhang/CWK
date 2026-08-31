# RT-026 script evolution migration — stage 08

- Owner: `RT-026`
- Target: `scripts/cwk_nightly_pipeline.py`
- Ordinal: `1`
- Reason: move the nightly wiki-compile defaults onto the model pair that was validated by live probe on 2026-08-30, and off the pair whose repair channel was failing on billing.

The genesis pipeline defaulted `wiki_model` to `newapi/BD-MiniMax` and
`wiki_repair_model` to `newapi/BD-glm`. Two independent operational problems made
that pair unusable for unattended nightly batches. The MiniMax primary had a high
failure rate on bulk summary generation — 138 documents were retried without the
run converging. The BD-glm repair channel, which is only invoked when the primary
returns unparseable or off-contract JSON, began returning billing failures on the
internal gateway, so the fallback path could not discharge the primary's errors.

Stage 08 changes only the two default model identifiers in
`cwk_nightly_pipeline.py`: the primary becomes `evan-openai/glm-5.3-flash` and the
repair channel becomes `deepseek/deepseek-v4-flash`, moving repair onto an external
official channel so a single gateway's billing state cannot disable both roles at
once. The 2026-08-30 live probe recorded 100% quote fidelity and 22–29 seconds per
document on the new primary.

No contract, schema, CLI surface, exit code or directory layout changes. The
`config_value` precedence chain is untouched: an explicit command-line argument
still outranks the run config, which still outranks these defaults, and the
`CWK_CLOUD_WIKI_MODEL` / `CWK_CLOUD_WIKI_REPAIR_MODEL` environment variables still
override the shipped literals. Both new identifiers are inside the
`CWK_ALLOWED_MODELS` allowlist, so the pre-call model gate in `cwk_ai_common` is
unchanged and still fails closed on anything outside it. The evolution does not
grant the pipeline new tools, credentials, network scope or write access, and it
does not touch raw sources.

## Provenance — read before citing this receipt as RT-026 work

This receipt is recorded honestly and is **not** a claim that RT-026 performed this
work. The byte change it covers originated in commit `dc96c28` ("Switch wiki
compile primary model to glm-5.3-flash", 2026-08-30), an ordinary maintenance
change authored nine days **after** the script-evolution policy was frozen on
2026-08-21. RT-026 ("试点准备、影子切换与回滚工具") is still `planned` and has not
started; this change is unrelated to its pilot/shadow/rollback scope.

Policy v1 pre-declares exactly one evolution slot for
`scripts/cwk_nightly_pipeline.py` (stage 8, `max_ordinal` 1, owner `RT-026`), and
the receipt schema constrains `owner_rt` to the policy-declared value. There is
therefore no representable way to attribute this change to another RT without
amending the frozen policy — which would require re-signing the two already issued
receipts, their two independent acceptance reports, the security gate registry
constant and the human-review pin in the guard helper. RT-029 chose the smaller,
non-destructive option: record the true `from`/`to` hash transition in the declared
slot and disclose the real provenance here.

An earlier interrupted attempt at this convergence took the opposite route and
reverted `cwk_nightly_pipeline.py` back to its exact genesis hash, which silently
undid this evidence-backed product decision in order to make the guard pass without
a receipt. That route was rejected: reverting a validated product change to satisfy
a governance check inverts what the check is for.

Consequence, tracked in `RT/_deferred-items.md`: RT-026's only pre-declared
evolution slot for this file is now consumed. When RT-026 actually implements pilot
shadow-switching and needs to change `cwk_nightly_pipeline.py`, it will require a
policy amendment at that point.
