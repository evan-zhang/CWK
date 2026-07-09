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

## Common Failures

- Missing `CWORK_APP_KEY`: resolve auth before live collection.
- Empty daily HTML: verify `digest-human-v4.md` exists.
- Too many suspected links: review `incremental-link-preview-v1.md`, keep strong merge disabled.
- DocDB server busy: rerun sync; raw evidence can use physical-file upload.
- Cron model rejected: use an allowlisted model such as `newapi-anthropic-vip/MiniMax-latest-cloud`.

## Safety Rules

- Never mark CWork items as read.
- Never reply, approve, reject, delete, or complete CWork tasks.
- Raw evidence must cite source IDs.
- Markdown is the durable source; HTML is the human reading surface.
