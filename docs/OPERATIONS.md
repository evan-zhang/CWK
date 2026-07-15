# CWK Mirror Operations

## Daily Run

The nightly job should call:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config /path/to/cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

## Expected Outputs

Local run:

```text
runs/nightly-YYYYMMDD-HHMM/
  run.json
  ACCEPTANCE-RESULT.md
  digest-human-v4.md
  digest-human-v4.html
  incremental-link-preview-v1.md
  nightly-pipeline-manifest.json
```

When `CWK_AI_ENABLED=true`, the run also contains:

```text
ai-understanding/*.json
ai-record-summary.json
ai-events.json
ai-daily-priorities.json
digest-ai-enhanced.md
digest-ai-enhanced.html
quality-review.json
quality-review.md
```

Mirror:

```text
knowledge/工作协同镜像/
  daily/YYYY-MM/YYYY-MM-DD.md
  daily/YYYY-MM/YYYY-MM-DD.html
  runs/YYYY-MM-DD-<run>-acceptance.md
  runs/YYYY-MM-DD-<run>-incremental-link-preview.md
```

## Health Check

A healthy run has:

- `overall_pass=true` in `run.json`.
- A daily Markdown file and daily HTML file.
- Incremental-link counts in `incremental-link-preview-v1.md`.
- Live collection is stateful. `state/collection-state.json` stores successful fingerprints, overflow, and historical backfill cursors; it remains local and gitignored.
- Nightly collection prioritizes new and changed records, then a bounded set of unresolved continuations. `detail_cap` is a ceiling, not a fixed sample size.
- Historical inbox, outbox, pending-report, pending-todo, and completed-todo pages are backfilled round-robin with a separate cap, so history cannot consume the daily incremental allowance.
- The human digest reports new, updated, continuation, and historical-backfill counts separately.
- `cwk_materialize_safe.py` incrementally writes redacted `history/`, `events/`, `entities/`, and `_index/` pages. Raw evidence is never copied by this stage.
- DocDB sync is allowlisted to `daily/`, `runs/`, `history/`, `events/`, `entities/`, and `_index/`; `raw/` remains excluded.
- DocDB sync manifest with no failed command.
- For an AI pilot, `ai.degraded=false`, all AI stages are completed, and quality evidence IDs resolve to raw reports.

## AI Pilot Run

Keep the scheduled production job rules-only. Run AI pilots manually or from a separate isolated schedule:

Before the first real call, provision a dedicated `cwk-ai-reviewer` Agent according to `docs/AI-PILOT.md`. CWK rejects an Agent unless its tool profile is `minimal`, its only additional tool is `read`, its sandbox is `mode=all/scope=agent/workspaceAccess=ro`, and its workspace exactly matches the fixed project-local `.cwk-ai-runtime` directory.

```bash
CWK_AI_ENABLED=true \
CWK_AI_RECORD_MODEL=provider/model \
CWK_AI_CLUSTER_MODEL=provider/model \
CWK_AI_QUALITY_MODEL=provider/model \
python3 scripts/cwk_nightly_pipeline.py \
  --config /path/to/cwk-mirror.local.json \
  --run-name ai-pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

The runner creates temporary prompt files under the ignored `.cwk-ai-runtime/prompts/` directory and removes them after each model call. OpenClaw Agent calls are never delivered to a chat channel. A failed AI stage sets `degraded=true` but does not fail or remove the rules digest.

## Common Failures

- Missing `CWORK_APP_KEY`: resolve auth before live collection.
- Empty daily HTML: verify `digest-human-v4.md` exists.
- Too many suspected links: review `incremental-link-preview-v1.md`, keep strong merge disabled.
- DocDB server busy: rerun sync; raw evidence can use physical-file upload.
- Cron model rejected: use an allowlisted model such as `newapi-anthropic-vip/MiniMax-latest-cloud`.
- AI stage missing model: set all three `CWK_AI_*_MODEL` values or use `CWK_AI_DRY_RUN=true` for orchestration tests.
- AI output invalid JSON/evidence: inspect the stage error in the nightly manifest; keep the rules digest as the published baseline.
- `skipped_sensitive_count > 0`: the source was withheld before model invocation. Rotate real credentials at the issuer and review existing raw replicas before deleting them.

## Safety Rules

- Never mark CWork items as read.
- Never reply, approve, reject, delete, or complete CWork tasks.
- Raw evidence must cite source IDs.
- Markdown is the durable source; HTML is the human reading surface.
