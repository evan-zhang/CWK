#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [ ! -f cwk-mirror.local.json ]; then
  cp skill/templates/CONFIG.example.json cwk-mirror.local.json
  echo "Created cwk-mirror.local.json from template. For the default personal mirror, provide only CWORK_APP_KEY or app_key."
else
  echo "cwk-mirror.local.json already exists; leaving it unchanged."
fi

python3 -m py_compile scripts/*.py
make smoke

echo "Install check complete. Smoke output is under runs/ci-smoke."
