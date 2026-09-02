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
- DocDB sync manifest with no failed command.
- For a scheduled mirror, `scripts/cwk_doctor.py` reports `activation: active`
  and the wizard reports `healthy: true`. A nightly run whose activation record
  is `needs_reconfirmation`, `degraded`, or `unreadable` is running without
  valid consent.

## Common Failures

- `CWK script package is incomplete`: the commands are running outside the CWK
  project root. Change into the directory that holds `scripts/cwk_doctor.py`
  (`$CWK_PROJECT_DIR`, else `/workspace/CWK`, else the user's clone). A copied
  Skill directory does not contain the script package.
- `SKILL_ROOT_NOT_WRITABLE`: the Skill root is a protected read-only mount. The
  core install already succeeded (`CWK_CORE_READY`); only the integration step
  failed. Re-run with `--integration host-skill` to hand registration to an
  operator, or `--integration router` for a self-service sandbox. Never change
  mount permissions or security policy to work around this.
- `AGENTS_ROUTER_CONFLICT`: the target `AGENTS.md` has an unbalanced or
  duplicated CWK marker pair. Fix it by hand; the installer refuses to guess
  which block is authoritative and leaves the file untouched.
- `AGENTS_ROUTER_TEMPLATE_INVALID`: the rendered router block does not carry
  exactly one BEGIN/END pair. The router template is broken or missing; repair
  the template instead of editing `AGENTS.md`.
- `AGENTS_ROUTER_UNREADABLE`: the target `AGENTS.md` is not valid UTF-8. The
  installer refuses to rewrite it and leaves it byte-for-byte intact. Report it;
  do not transcode someone else's file to make the install pass.
- `OPENCLAW_INTEGRATION_CONFLICT`: this Agent already has the other integration
  mode (`CWK_EXISTING_INTEGRATION=AGENTS_ROUTER` or `FORMAL_SKILL`). One Agent
  runs exactly one mode. Nothing was written. `--force` does not bypass this and
  the installer never removes the other mode for you; the user decides which
  mode to keep and removes the other one deliberately.
- `Refusing to overwrite an existing Skill target`: something else already owns
  that directory. Inspect it before deciding; `--force` replaces it (same mode
  only).
- Missing `CWORK_APP_KEY`: resolve auth before live collection. Doctor reads the
  project `.env` safely, so a key written only into `.env` still reports
  `configured`; never `source` or print the file to make a check pass. `.env`
  accepts plain `KEY=value` only — an `export KEY=value` line is ignored by both
  the nightly pipeline and doctor, so a key written that way reports `missing`.
- Empty daily HTML: verify `digest-human-v4.md` exists.
- Too many suspected links: review `incremental-link-preview-v1.md`, keep strong merge disabled.
- DocDB server busy: rerun sync; raw evidence can use physical-file upload.
- Scheduled model rejected: use an allowlisted model such as `newapi-anthropic-vip/MiniMax-latest-cloud`.
- `activation: unreadable`, or the wizard reporting `healthy: false`: the private
  activation record exists but cannot be validated, so any scheduled run is
  untrusted until it is resolved. Do not delete the state and re-`init` over it —
  that manufactures consent nobody gave. Tell the user the integrity reason and
  let them disable the host-side task first.
- `check-drift` exits 5: config or contract changed, so the confirmations voided
  themselves. The host task keeps firing until someone disables it; say that
  plainly, then re-walk from the step `next_step` names.
- `schedule-handoff` refused because the config sits outside the project: move
  the config into the project and retry. Never substitute an absolute host path
  the handoff deliberately omitted.

Full activation procedure: `references/activation.md`.

## Safety Rules

- Never mark CWork items as read.
- Never reply, approve, reject, delete, or complete CWork tasks.
- Raw evidence must cite source IDs.
- Markdown is the durable source; HTML is the human reading surface.
- `CWORK_APP_KEY` may be accepted in chat on the vetted client channel and is stored only via `scripts/setup_app_key.py`; no other credential is collected in chat. Never create, modify, or delete a scheduled task.
