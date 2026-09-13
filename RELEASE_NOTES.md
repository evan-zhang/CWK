# v0.3.0-retrieval-beta

First beta of the RT-055 dual-channel retrieval system (candidate `cwk-opensearch-dual-channel-v1`), selected through a controlled bake-off against WeKnora and now under a live production shadow soak on the OPS host.

## Included

- Production retrieval package `adapters/opensearch_retrieval/`: exact-resolver + lexical ICU BM25 dual-channel query engine with parent expansion and no-answer detection; ICU parent-child index pipeline with document collapse; HTTP API (`/query`, `/healthz`, `/readyz`); CLI (`build-index`, `smoke-test`); environment-driven configuration
- Deployment artifacts (`deploy/`): docker-compose stack for OpenSearch + retrieval service (loopback by default), Dockerfiles, Mac/Linux guides covering shadow -> gray -> cutover with acceptance baselines
- Shadow reconciliation runner: side-by-side old/new comparison at 60s cadence for a 24h soak (live on OPS; first rounds 115/115 success, 0 errors)
- Complete RT-055 decision record: frozen exam, scoring evidence, candidate fairness audit, production plan

## Verification

- `make ci` green on the release commit (doctor + compile + 107-module unit suite + smokes + AODW + governance audit over 1074 tracked files)
- First full CI round: 2838 passed / 12 skipped; targeted retrieval + governance suite: 75 passed
- Privacy: no corpus, queries, or credentials in the repository; synthetic fixtures only; credentials via environment variables

## Status & known limitations

- Pre-release: the 24h production shadow soak is in progress; gray and full cutover follow its report
- Compose smoke executed on the deployment host (OPS); build machines carry no local Docker daemon
# v0.2.0-ai-pilot

This internal pilot release adds an optional AI understanding and quality layer while retaining the deterministic read-only CWK pipeline as the default baseline.

## Included

- Evidence-backed per-report AI understanding
- Cross-report event clustering and management priorities
- AI-enhanced Markdown and HTML digests
- Independent AI quality review
- Dedicated read-only AI reviewer runtime
- Sensitive-source quarantine before model calls
- Graceful model failure and deterministic batch recovery
- Three reviewed real-data pilots

## Pilot evidence

- 2026-07-15: AI 84 / rules 68 / evidence coverage 0.82
- 2026-07-16: AI 82 / rules 64 / evidence coverage 0.86
- 2026-07-17: AI 84 / rules 68 / evidence coverage 0.88

## Safety and rollout

AI remains disabled by default. Enable it explicitly with `CWK_AI_ENABLED=true` and use the dedicated `cwk-ai-reviewer` Agent described in `docs/AI-PILOT.md`. This release does not authorize CWork write operations or automatically change the production cron.

To return to the stable deterministic path, disable `CWK_AI_ENABLED`; no CWork or knowledge-base rollback is required.
