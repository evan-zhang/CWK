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
- Write access to the authorized user's personal knowledge base, or to an explicit team knowledge-base folder.
- Cron support for nightly execution.

## Required IDs and Permissions

Do not treat all setup values as one credential. The default personal deployment has one authentication secret and auto-discovers the destination:

- CWork appKey: provide `CWORK_APP_KEY` as an environment variable, or set private `app_key` in the local config. This authorizes read-only collection from 工作协同 and is also passed to DocDB sync as `XG_BIZ_API_KEY`.
- DocDB destination: leave `docdb_project_id` and `docdb_root_file_id` empty to use the authorized user's personal knowledge base and auto-create or reuse `工作协同镜像`.
- Shared/team destination: provide `docdb_project_id` and `docdb_root_file_id` only when writing to an explicit shared folder.

The runtime Agent also needs local access to `cms-cwork-workflow` and `cms-docdb`. If auth is resolved through `cms-auth-skills` instead of `CWORK_APP_KEY`, set `CWK_SENDER_ID` and `CWK_ACCOUNT_ID` for the target user/account.

`scripts/cwk_doctor.py` searches several Skill roots (`$HOME/.openclaw/skills`,
`$HOME/.agents/skills`, `<workspace>/skills`, `<workspace>/.agents/skills`, and
OpenClaw's read-only sandbox materialization root
`<workspace>/.openclaw/sandbox-skills/skills`) and works with `HOME=/`. When the
CWK clone is outside the Agent Workspace, set `CWK_WORKSPACE_DIR` to that
Workspace. Point `CWK_SKILL_ROOTS` at an extra root, or set
`CMS_CWORK_WORKFLOW_DIR` /
`CMS_AUTH_SKILL_DIR` /
`CMS_DOCDB_SKILL_DIR` for one exact directory. These are directory paths, never
credentials. Doctor reads the project `.env` with a minimal dotenv parser: it
never executes the file and reports only `configured` / `missing`, so never run
`source .env` or `cat .env`.

`K` numbers are not required for CWK setup. They are archive/report identifiers from separate knowledge workflows.

## Setup

1. Default: use the target user's personal knowledge base. Optional: create a team/shared target folder.
2. Copy `CONFIG.example.json` to a local config file outside shared source control.
3. Fill `app_key` only if not using the `CWORK_APP_KEY` environment variable.
4. Optional: fill `docdb_project_id` and `docdb_root_file_id` only for a shared/team destination.
5. Optional: fill `history_run_name` if a baseline exists, and tune `detail_cap`.
6. Keep `sync_docdb=false` until smoke passes. Use `--sync-docdb` explicitly for live sync.
7. Run a no-publish smoke test using existing raw samples.
8. Run one live read-only pass with `--sync-docdb`.
9. Enable nightly cron only after the live pass succeeds.

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
