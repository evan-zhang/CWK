# CWK Mirror Operations

## Activation and Scheduling

A nightly job exists only because a person answered two separate questions: may
CWK read this scope read-only, and — much later, after seeing a scored
read-only pilot — may it run every night. Both answers live in the private
activation record and are bound to the hash of what was actually shown.

This repository never creates, modifies, or deletes a scheduled task. It emits a
handoff describing one; the host creates it, and the operator records the
identifier the host assigned:

```bash
python3 scripts/activation_wizard.py status
python3 scripts/activation_wizard.py check-drift --config cwk-mirror.local.json
```

`check-drift` is the operational command. Config or contract changed ⇒ the
confirmations void themselves and the state becomes `NEEDS_RECONFIRMATION`; the
nightly job may still be firing at the host, so re-walk the dialogue or disable
the task. A scheduled task the record does not know about is reported, never
deleted.

`pause` and `resume` change CWK's own posture only. Neither touches the external
task, so a paused mirror still needs the host-side task disabled if the run is
meant to actually stop.

The full state-by-state procedure is in
[the activation reference](../skill/references/activation.md); it is not
repeated here.

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
- `python3 scripts/cwk_wiki_query.py --lint` reports `overall=PASS`: every summary resolves to raw, evidence quotes validate, and topic/entity summary links are not dangling.
- `python3 scripts/cwk_doctor.py` reports `activation: active`, and the wizard reports `healthy: true`. A scheduled mirror whose activation record is `needs_reconfirmation`, `degraded`, or `unreadable` is running without valid consent.

## Wiki Question Retrieval

Use the local evidence retriever before answering questions from the mirror:

```bash
python3 scripts/cwk_wiki_query.py "<question>" --top-k 8
```

The retriever is intentionally model-free. Topics and entities improve recall, summaries rank candidate reports, and raw files provide the final quotes. `confidence=none` means the Agent must refuse or broaden the query; navigation-only text is never final evidence.

## AI Pilot and Bounded Wiki Refinement

Keep record understanding and event-clustering pilots side-by-side. Wiki source refinement may run in the production nightly only with a bounded page budget, a zero-tool reviewer, and best-effort failure handling:

```bash
python3 scripts/cwk_nightly_pipeline.py ... \
  --wiki-refine-fallbacks --wiki-limit 8 --wiki-max-parallel 4 \
  --wiki-model newapi/BD-glm --wiki-repair-model newapi/BD-glm \
  --wiki-timeout-seconds 30
```

Fallback pages remain queryable and their final evidence is always read from `raw/`. Sensitive pages are withheld from the model. Failed pages stop automatic retries after three attempts.

For other AI pilots, run manually or from a separate isolated schedule:

Before the first real call, provision a dedicated `cwk-ai-reviewer` Agent according to `docs/AI-PILOT.md`. CWK rejects an Agent unless its tool profile is `minimal`, all tool allow lists are empty, `deny=["*"]`, its sandbox is `mode=off`, and its workspace exactly matches the fixed project-local `.cwk-ai-runtime` directory. This removes Docker from the AI path without granting host tools.

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
- Cron model rejected: use the reviewed allowlist in `MODEL_ROLES.md` (`newapi/BD-MiniMax` or `newapi/BD-glm`).
- AI stage missing model: set all three `CWK_AI_*_MODEL` values or use `CWK_AI_DRY_RUN=true` for orchestration tests.
- AI output invalid JSON/evidence: inspect the stage error in the nightly manifest; keep the rules digest as the published baseline.
- Credential-like text in a CWork source must pass through unchanged. If a run skips, redacts, quarantines, or blocks on such text, treat that as a pipeline defect.
- `activation: unreadable`, or the wizard reporting `healthy: false`: the private activation record exists but cannot be validated, so every scheduled run is untrusted until it is resolved. Do not delete the state and re-`init` over it — that manufactures consent nobody gave. Disable the host-side task first, then investigate.
- `check-drift` exits 5: the confirmations are void. The host task keeps firing until someone disables it; treat that gap as the operational risk, not the exit code.
- `schedule-handoff` refuses because the config sits outside the project: move the config into the project and retry. Do not hand the host an absolute path that the handoff deliberately omitted.

## Safety Rules

- Never mark CWork items as read.
- Never reply, approve, reject, delete, or complete CWork tasks.
- Raw evidence must cite source IDs.
- Markdown is the durable source; HTML is the human reading surface.
- Never collect a credential in a conversation, and never create, modify, or delete a scheduled task from this repository.
