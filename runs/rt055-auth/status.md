# RT-055 auth status

- Milestone: final QA complete; commit `d84f358` on `feature/rt055-auth`.
- Verified: synthetic auth suite 6/6; existing RAG+retrieval suites 24/24; combined 30/30 green.
- Full repository discovery was started but hit the 300s command timeout; no failure was emitted.
- `git diff --check` and `compileall` passed; no deployment/runtime process touched.
- Auth remains dormant by default (`RAG_AUTH_ENABLED=false`); no real corpus or credential output.
- Internal call chain fix: `RetrievalHTTPRetriever` reads optional `RAG_AUTH_TOKEN`; non-empty sends `X-KB-Token`, empty preserves no-header behavior.
- Enable sequence at gray GO: use `kb_token issue` to sign one internal-service token with all three KB IDs, write its plaintext to the rag-answer container's `RAG_AUTH_TOKEN`, then set `RAG_AUTH_ENABLED=true` and provide the registry.
- `kb_token` supports multi-KB scope: `issue_token(..., kb_ids=...)` validates and preserves a non-empty list, so no single-bank design decision is currently required.
