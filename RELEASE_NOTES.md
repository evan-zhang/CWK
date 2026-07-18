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
