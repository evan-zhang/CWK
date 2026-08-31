# RT-022 script evolution migration — stage 06

- Owner: `RT-022`
- Target: `scripts/cwk_wiki_query.py`
- Ordinal: `1`
- Reason: make author/date report listings metadata-scoped, raw-verified, and complete during incremental summary ingestion.

The genesis query path treated generic labels such as “工作汇报” as semantic entity names. When a caller also supplied an author or date filter, entity resolution could bind the generic label to an unrelated catalog entry and then filter every valid report out. This made bounded historical listings unreliable even though the raw source already contained authoritative author and date metadata.

Stage 06 introduces an explicit metadata-listing intent for a closed set of generic report labels. The intent is available only when an author or date bound is present; ordinary semantic queries retain the existing BM25 and entity-resolution path. Listing rows are ranked by date rather than artificial relevance terms, and every returned row is verified against the raw envelope’s author/date metadata.

Incremental ingestion can expose a raw report before its summary enters the semantic index. For author-scoped listings only, the query now scans raw metadata for missing or stale index rows, deduplicates by `report_id`, and includes the raw-only row without adding its body to the semantic index. This preserves listing coverage while maintaining the evidence boundary: the response is a metadata claim, not a semantic fact claim.

The evolution does not widen cloud-query access, bypass entity scoping for factual questions, or weaken raw-source verification. Acceptance tests cover both an indexed report and a raw-only report and require `raw_listing_metadata` evidence with the exact requested author/date scope.

## Provenance — read before citing this receipt as RT-022 work

This receipt is recorded honestly and is **not** a claim that RT-022 performed
this work. The byte change it covers originated in commit `c26c7ad` ("fix:
support author-filtered report listings", 2026-08-30), an ordinary maintenance
fix authored nine days **after** the script-evolution policy was frozen on
2026-08-21. RT-022 ("Query Broker 授权核心") is still `planned` and has not
started; this change is unrelated to its Query Broker scope.

Policy v1 pre-declares exactly one evolution slot for
`scripts/cwk_wiki_query.py` (stage 6, `max_ordinal` 1, owner `RT-022`), and the
receipt schema constrains `owner_rt` to the policy-declared value. There is
therefore no representable way to attribute this change to another RT without
amending the frozen policy — which would require re-signing the two already
issued receipts, their two independent acceptance reports, the security gate
registry constant and the human-review pin in the guard helper. RT-029 chose the
smaller, non-destructive option: record the true `from`/`to` hash transition in
the declared slot and disclose the real provenance here.

Consequence, tracked in `RT/_deferred-items.md`: RT-022's only pre-declared
evolution slot for this file is now consumed. When RT-022 actually implements
the Query Broker and needs to change `cwk_wiki_query.py`, it will require a
policy amendment at that point.
