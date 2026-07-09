# v0.1.0-internal

This is the first internal production package for CWK.

## What Is Included

- Agent Skill entrypoint in `skill/SKILL.md`
- Deterministic execution scripts in `scripts/`
- Private config template in `skill/templates/CONFIG.example.json`
- Migration and operations docs
- Read-only safety boundary
- Daily Markdown and HTML digest generation
- Optional DocDB sync
- Smoke fixture and CI

## Install

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
./install.sh
```

Then edit `cwk-mirror.local.json` and run a live read-only pass:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

## Safety

The default workflow is read-only against CWork. It must not mark read, reply, approve, reject, delete, or complete tasks unless a separate mutating workflow is explicitly approved.
