---
name: cwk-mirror-workflow
description: Use when asked to build, migrate, operate, or troubleshoot a read-only CWork/工作协同 knowledge mirror with daily Markdown and HTML digests, event/entity linking, DocDB sync, or nightly cron.
---

# CWK Mirror Workflow

Use this skill to create or operate a portable `工作协同镜像` for a person or team.

## Safety Boundary

Allowed:
- Read CWork records in read-only modes.
- Write local run artifacts and knowledge mirror files.
- Optionally publish derived Wiki and daily Markdown/HTML files into the configured knowledge-base folder.
- Schedule cron after a live read-only run succeeds.

Forbidden unless the user explicitly asks for that separate action:
- Mark CWork items as read.
- Reply, approve, reject, delete, or complete CWork tasks.
- Send CWork messages.
- Mix one user's private raw evidence into another user's mirror.

## Required Inputs

Before running live collection, identify:
- target user/team and whether this is a personal mirror or team mirror
- CWork auth source: `CWORK_APP_KEY` preferred
- optional derived-page publishing target: default personal knowledge base, unless a team/shared `docdb_project_id` and `docdb_root_file_id` is explicitly required
- local script package path

For migration, read `references/migration.md`. For operation and failures, read `references/operations.md`.

## Standard Workflow

1. Locate the script package and inspect the local config.
2. Create a private config from `templates/CONFIG.example.json`.
3. Run a no-publish smoke test.
4. Run one live read-only Local-First pass; add `--sync-docdb` only when derived Wiki/HTML publishing is desired.
5. Inspect `run.json`, `ACCEPTANCE-RESULT.md`, and `incremental-link-preview-v1.md`.
6. Enable or update cron only after the live pass succeeds.
7. Report concise results: run name, processed count, pass/fail, MD/HTML paths, link statistics, and sync status.

## Commands

Smoke test:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

Live read-only run:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --no-cloud-first \
  --no-publish-cloud-query-catalog
```

To publish only derived Wiki/HTML copies, add `--sync-docdb --sync-wiki`. Raw
evidence remains local and authoritative.

## Quality Review

Warning signs:
- `overall_pass=false` on live runs
- missing daily HTML
- too many suspected links
- any CWork mutating command appears in manifests
- raw evidence from different users appears in a shared/team mirror without approval

Markdown is the durable source; HTML is the human reading surface.
