# CWK

CWK is a read-only CWork / 工作协同 knowledge mirror workflow.

It turns work-collaboration messages, todos, handled items, and reply chains into:

- a local authoritative raw-evidence mirror
- structured extraction JSON
- event and entity candidates
- daily Markdown digests
- standalone daily HTML reading pages
- incremental-link review reports
- optional DocDB / knowledge-base mirror sync

## Status

Internal/private stable baseline. The read-only source mirror, business-date raw truth source, evidence-backed Wiki query, source/raw/summary completeness gate, bounded AI refinement, and DocDB version-sync paths are implemented. This repository intentionally excludes real raw evidence, real run outputs, secrets, prompts, AI outputs, and personal knowledge-base data.

**Production profile (2026-08-18): Local-First.** Cloud-First persistence and
`cloud`/`shadow` query modes are paused. The complete local mirror is the
authoritative store and production queries use `--mode local`. DocDB remains
enabled only for derived Wiki/index/run-receipt backup plus daily Markdown/HTML
publishing; raw evidence is not published. See [runtime status](docs/RUNTIME_STATUS.md).

Primary documentation:

- [Design specification](docs/DESIGN.md)
- [Cloud-First v2 target design and re-audit](docs/CLOUD_FIRST_V2_DESIGN.md)
- [User guide](docs/USER_GUIDE.md)
- [Operations guide](docs/OPERATIONS.md)
- [AI runtime policy](docs/AI-PILOT.md)
- [Model role matrix](MODEL_ROLES.md)

## Quick Start

1. Clone the private repository:

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
```

2. Run the portable install check (Python 3.10+; Python 3.11 recommended):

```bash
PYTHON=python3.11 ./install.sh
```

This creates `cwk-mirror.local.json` if missing, compiles scripts, and runs the sanitized smoke test.

3. For an internal OpenClaw installation, use the explicit Skill-link option:

```bash
PYTHON=python3.11 ./install.sh --install-skill
```

This does not collect business data, write DocDB, create cron jobs, or modify
an Agent. See [internal distribution](docs/INTERNAL_DISTRIBUTION.md) for the
per-user authorization and trial procedure.

4. Check local requirements for live operation:

```bash
python3.11 --version
gh auth status
```

For live CWork and DocDB sync, the runtime Agent also needs local `cms-cwork-workflow`, `cms-docdb`, and either `CWORK_APP_KEY` or a private `app_key` in the config.

## What Each User Must Provide

In the default personal-mirror deployment, one 工作协同 appKey is enough for authentication and destination discovery.

- CWork appKey: `CWORK_APP_KEY` environment variable. This lets the collector read 工作协同 records and todos for the authorized user. The same key is also passed to DocDB sync as `XG_BIZ_API_KEY`. It is a secret and must not be committed. A private local `app_key` in `cwk-mirror.local.json` is still supported, but environment variables are preferred.
- DocDB project ID: `CWK_DOCDB_PROJECT_ID`, or `docdb_project_id` in local config. Optional. Leave empty to use the authorized user's personal knowledge base.
- DocDB root folder/file ID: `CWK_DOCDB_ROOT_FILE_ID`, or `docdb_root_file_id` in local config. Optional. Leave empty to find or create the default `工作协同镜像` folder.
- Local Agent capabilities: the Agent/machine must have `cms-cwork-workflow`, `cms-docdb`, and auth helper access. These are runtime tools, not config IDs.
- Optional account routing: `CWK_SENDER_ID` and `CWK_ACCOUNT_ID` are only needed when resolving auth through `cms-auth-skills` instead of providing `CWORK_APP_KEY`.
- CWork relationship API: `CWK_RELATION_API_PATH` (plus optional base URL and timeout). The Work Report backend must resolve the current user from AppKey and return authoritative report relationships in batches. Until that endpoint is deployed, daily views show `关系待确认`; CWK deliberately does not infer `与我无关` from local people lists.

`K` numbers are not setup credentials. They are archive/report identifiers produced by other knowledge workflows, and CWK users do not need to provide a `K` number to install or run this project.

4. Create a private environment file if you want shell-based configuration:

```bash
cp .env.example .env
```

Then fill `CWORK_APP_KEY` plus the matching `CWK_OWNER_EMP_ID` / `CWK_OWNER_NAME`. Keep `CWK_DOCDB_PROJECT_ID` and `CWK_DOCDB_ROOT_FILE_ID` empty unless you are intentionally writing to a specific shared knowledge-base folder.

`cwk_nightly_pipeline.py` automatically loads the gitignored project `.env` and never overrides variables already exported by the parent process.

For one-off shell usage, exporting the key is enough:

```bash
export CWORK_APP_KEY=***
```

5. If you skipped `install.sh`, copy the config template:

```bash
cp skill/templates/CONFIG.example.json cwk-mirror.local.json
```

6. For the default personal mirror, leave `docdb_project_id` and `docdb_root_file_id` empty. Fill them only when writing to a specific team/shared knowledge-base folder.

7. Provide CWork auth through `CWORK_APP_KEY` or a private local config.

8. Run a no-publish smoke test:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

The smoke test should create `runs/nightly-smoke-*`. It may report `overall_pass=false` because the sample fixture is intentionally tiny; that is acceptable for smoke. The goal is command and rendering connectivity.

9. Run a live read-only pass:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

A production-ready live run should report `overall_pass=true` and generate both `digest-human-v4.md` and `digest-human-v4.html`.

Live nightly runs also execute a complete `search-list` pass for that business
date, promote staged reports into the local `raw/YYYY-MM/YYYY-MM-DD/` truth
source, compile missing Wiki summaries, and enforce
`source IDs = raw IDs = summary IDs`.  The bounded inbox/todo collector remains
responsible for the human digest; it is no longer treated as proof of complete
source capture. Generic sync denies raw. The paused Cloud-First implementation
is retained only as experimental migration code and requires an additional
explicit unlock; it is not part of the production runtime profile.

## AI Quality Pilot

The stable nightly remains rules-only by default. RT-001 adds a side-by-side AI pipeline without replacing the rules digest:

- per-report structured understanding with evidence quotes
- cross-report event clustering and priority ranking
- `digest-ai-enhanced.md/html`
- `quality-review.json/md`
- graceful degradation when a model or stage fails

First verify the complete AI orchestration without calling a model:

```bash
CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true make smoke-ai
```

For a real local pilot, configure three models in the private `.env`:

```bash
CWK_AI_ENABLED=true
CWK_AI_DRY_RUN=false
CWK_AI_RECORD_MODEL=newapi/BD-MiniMax
CWK_AI_CLUSTER_MODEL=newapi/BD-glm
CWK_AI_QUALITY_MODEL=newapi/BD-glm
```

**Hard constraint**: the CWK pipeline only permits `newapi/BD-MiniMax` (MiniMax M3) and `newapi/BD-glm` (GLM 5.2). Any other model ID is rejected at startup. See `MODEL_ROLES.md` for the full role matrix.

Then run a side-by-side read-only pass:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name ai-pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

Real AI calls use `openclaw agent --json` through a dedicated `CWK_AI_AGENT_ID` and do not use `--deliver`. The configured Agent is an unsandboxed **zero-tool** transformer: `tools.profile=minimal`, empty allow lists, `deny=["*"]`, `skills=[]`, and `sandbox.mode=off`. OpenClaw reads the temporary message file before the model turn, so the reviewer needs no filesystem tool and Docker is not a runtime dependency. CWK refuses any policy drift. Temporary prompt files are deleted after each call. See `docs/AI-PILOT.md` for the runtime policy.

For cloud wiki source compilation, `scripts/cwk_cloud_wiki_compile.py` defaults to `newapi/BD-MiniMax`. Override with `--model` or `CWK_CLOUD_WIKI_MODEL` only when a cheaper or more reliable reviewed model is intentionally selected.
If the primary response is not valid contract JSON, one bounded repair call uses
`newapi/BD-glm` by default (`--repair-model` / `CWK_CLOUD_WIKI_REPAIR_MODEL`).

Summary coverage and summary quality are separate manifest states. `compiled_report_ids` means a navigable summary exists; `ai_refined_report_ids` and `fallback_report_ids` report the quality split. Nightly compiles missing summaries but does not block on historical fallback refinement. Run the latter as a bounded quality job only:

```bash
REFINE_FALLBACKS=true LIMIT=5 MAX_PARALLEL=4 MAX_BATCHES=1 scripts/cwk_wiki_batch_driver.sh
```

The batch driver is local-only by default. Set `SYNC_WIKI=true` only when the
reviewed pages should be version-synced to DocDB after each batch.

Model failures keep the old fallback page and increment a bounded attempt
counter. After three failed attempts the page becomes
`fallback_terminal_error`. Content readable from CWork is treated as authorized
knowledge-source material: credential-like strings are neither withheld nor
rewritten. The trusted query output still verifies every factual quote against raw.

Nightly can also spend a bounded compile budget on historical quality debt:

```bash
CWK_WIKI_REFINE_FALLBACKS=true CWK_WIKI_LIMIT=5 CWK_WIKI_MAX_PARALLEL=4 \
  python3 scripts/cwk_nightly_pipeline.py ...
```

## Trusted Wiki Query

`scripts/cwk_wiki_query.py` is the read-only question retrieval entrypoint. In
production use `--mode local`: it ranks summaries/topics/entities against the
complete local mirror, then validates every returned quote against local raw.
The retained `cloud` and `shadow` providers are paused experimental paths and
require a second explicit unlock.

```bash
python3 scripts/cwk_wiki_query.py \
  "7 月 10 日 OpenClaw Token 异常用户是谁" \
  --top-k 6
```

For a bounded period or person:

```bash
python3 scripts/cwk_wiki_query.py \
  "Token 消耗异常" \
  --from-date 2026-07-15 --to-date 2026-07-31 \
  --format json

python3 scripts/cwk_wiki_query.py \
  "最近参与了哪些事项" --writer 李文俏
```

Run the Wiki integrity gate before relying on it:

```bash
python3 scripts/cwk_wiki_query.py --lint
```

The command returns an evidence packet, not an unverified prose answer. The OpenClaw Agent must answer only from `evidence_status=verified` items, cite `report_id` and the raw path, surface conflicts, and abstain when `confidence=none`.

## Repository Layout

```text
skill/       Agent skill entrypoint, references, and config template
scripts/     Deterministic execution layer
docs/        Migration and operations docs
examples/    Sanitized examples only
tests/       Smoke fixtures only
```

## Production Checklist

- `make test` passes.
- `cwk-mirror.local.json` is private and not committed.
- Default personal knowledge-base discovery succeeds, or `docdb_project_id` and `docdb_root_file_id` point to an explicit shared mirror folder.
- Live run reports `overall_pass=true`.
- Daily Markdown and HTML are generated.
- No CWork mutating command appears in the run manifest.
- Nightly cron is enabled only after one successful live read-only run.
- AI pilot output is side-by-side; it does not replace the rules baseline.
- Every AI event and priority contains valid source `report_id` values.
- `degraded=false` is required before treating an AI pilot as technically healthy.

## Safety

CWK is read-only against CWork by default.

Forbidden unless separately authorized:

- mark read
- reply
- approve or reject
- complete todos
- delete CWork records
- upload real raw evidence to a shared repo

See `SECURITY.md` and `docs/MIGRATION.md`.
