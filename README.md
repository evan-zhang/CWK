# CWK

CWK is a read-only CWork / 工作协同 knowledge mirror workflow.

It turns work-collaboration messages, todos, handled items, and reply chains into:

- raw local evidence files
- structured extraction JSON
- event and entity candidates
- daily Markdown digests
- standalone daily HTML reading pages
- incremental-link review reports
- optional DocDB / knowledge-base mirror sync

## Status

Internal/private v0.1.0. This repository intentionally excludes real raw evidence, real run outputs, secrets, and personal knowledge-base data.

## Quick Start

1. Clone the private repository:

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
```

2. Run install check:

```bash
./install.sh
```

This creates `cwk-mirror.local.json` if missing, compiles scripts, and runs the sanitized smoke test.

3. Check local requirements for live operation:

```bash
python3 --version
gh auth status
```

For live CWork and DocDB sync, the runtime Agent also needs local `cms-cwork-workflow`, `cms-docdb`, and either `CWORK_APP_KEY` or a private `app_key` in the config.

4. If you skipped `install.sh`, copy the config template:

```bash
cp skill/templates/CONFIG.example.json cwk-mirror.local.json
```

5. Fill `docdb_project_id` and `docdb_root_file_id`.

6. Provide CWork auth through `CWORK_APP_KEY` or a private local config.

7. Run a no-publish smoke test:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

The smoke test should create `runs/nightly-smoke-*`. It may report `overall_pass=false` because the sample fixture is intentionally tiny; that is acceptable for smoke. The goal is command and rendering connectivity.

8. Run a live read-only pass:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

A production-ready live run should report `overall_pass=true` and generate both `digest-human-v4.md` and `digest-human-v4.html`.

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
- `docdb_project_id` and `docdb_root_file_id` point to the target user's mirror folder.
- Live run reports `overall_pass=true`.
- Daily Markdown and HTML are generated.
- No CWork mutating command appears in the run manifest.
- Nightly cron is enabled only after one successful live read-only run.

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
