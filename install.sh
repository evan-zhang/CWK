#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [ ! -f cwk-mirror.local.json ]; then
  cp skill/templates/CONFIG.example.json cwk-mirror.local.json
  echo "Created cwk-mirror.local.json from template. For the default personal mirror, prefer CWORK_APP_KEY in your shell or .env."
else
  echo "cwk-mirror.local.json already exists; leaving it unchanged."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from template. Fill CWORK_APP_KEY locally; .env is gitignored."
else
  echo ".env already exists; leaving it unchanged."
fi

python3 -m py_compile scripts/*.py
make smoke

echo "Install check complete. Smoke output is under runs/ci-smoke."
