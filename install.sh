#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
INSTALL_SKILL=false
SKILLS_DIR="${OPENCLAW_SKILLS_DIR:-$HOME/.openclaw/skills}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-skill)
      INSTALL_SKILL=true
      ;;
    --skills-dir)
      shift
      [ "$#" -gt 0 ] || { echo "--skills-dir requires a path" >&2; exit 2; }
      SKILLS_DIR="$1"
      ;;
    --help|-h)
      cat <<'EOF'
Usage: ./install.sh [--install-skill] [--skills-dir PATH]

Creates private config templates and verifies the local package. It never
collects CWork data, writes DocDB, creates cron jobs, or changes an Agent.
Use --install-skill to create a local symlink for OpenClaw skill discovery.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python interpreter not found: $PYTHON" >&2
  exit 1
fi

"$PYTHON" scripts/cwk_doctor.py --check-only --config cwk-mirror.local.json

if [ "$INSTALL_SKILL" = true ]; then
  target="$SKILLS_DIR/cwk-mirror-workflow"
  source_dir="$(pwd)/skill"
  mkdir -p "$SKILLS_DIR"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "Refusing to overwrite non-link skill path: $target" >&2
    exit 1
  fi
  if [ -L "$target" ] && [ "$(readlink "$target")" != "$source_dir" ]; then
    echo "Refusing to replace an existing skill link: $target" >&2
    exit 1
  fi
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

"$PYTHON" -m py_compile scripts/*.py
make PYTHON="$PYTHON" smoke

if [ "$INSTALL_SKILL" = true ]; then
  ln -sfn "$source_dir" "$target"
  echo "Linked CWK skill: $target -> $source_dir"
fi

echo "Install check complete. Smoke output is under runs/ci-smoke."
