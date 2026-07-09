# CWK Mirror Migration

This document explains how to move the CWork mirror workflow to another Agent, machine, or colleague.

## Migration Unit

Move the project script package and create a new config:

```text
cwk-mirror-workflow/
  scripts/
  CONFIG.example.json
  MIGRATION.md
  OPERATIONS.md
```

Do not copy secrets. Do not copy Evan's raw evidence into a colleague's mirror unless that colleague is explicitly allowed to see it.

## Required Capabilities

- `cms-cwork-workflow` available locally.
- `cms-auth-skills` or a valid `CWORK_APP_KEY`.
- `cms-docdb` available when syncing to a knowledge base.
- Write access to a personal or team knowledge-base folder named `工作协同镜像`.
- Cron support for nightly execution.

## Setup

1. Create a target knowledge-base folder for the user or team.
2. Copy `CONFIG.example.json` to a local config file outside shared source control.
3. Fill:
   - `docdb_project_id`
   - `docdb_root_file_id`
   - `history_run_name` if a baseline exists
   - `detail_cap`
4. Keep `sync_docdb=false` until smoke passes. Use `--sync-docdb` explicitly for live sync.
5. Provide `CWORK_APP_KEY` through the environment, or set `app_key` in the local config only when the file is private.
6. Run a no-publish smoke test using existing raw samples.
7. Run one live read-only pass with `--sync-docdb`.
8. Enable nightly cron only after the live pass succeeds.

## Commands

Smoke test:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config /path/to/cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

Live read-only run:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config /path/to/cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

## Privacy Boundary

- Personal mirrors are private to the owner.
- Team mirrors should not include raw private evidence by default.
- Shared output should prefer daily HTML summaries and confirmed event pages.
- Suspected relations are review hints, not facts.
