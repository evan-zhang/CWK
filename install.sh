#!/usr/bin/env bash
# CWK installer.
#
# Contract (RT-031):
#   1. Core installation always runs first and never depends on the OpenClaw CLI.
#   2. OpenClaw integration is an explicit, separate choice. Nothing is selected
#      silently: without --integration the installer stops at the core install.
#
# The installer never collects CWork data, never writes DocDB, never creates
# cron jobs, never reads or prints credential values, and never changes an Agent,
# a Gateway, or a host control plane.
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PYTHON="${PYTHON:-python3}"
SKILL_SOURCE="$PROJECT_DIR/skill"
SKILL_NAME="cwk-mirror-workflow"
ROUTER_TEMPLATE="$PROJECT_DIR/prompts/CWK_AGENTS_ROUTER.md"

INTEGRATION=""
INTEGRATION_SET=false
LEGACY_INSTALL_SKILL=false
WORKSPACE_DIR="${CWK_WORKSPACE_DIR:-}"
SKILLS_DIR=""
SKILLS_DIR_SET=false
AGENTS_FILE=""
FORCE=false

usage() {
  cat <<'EOF'
Usage: ./install.sh [--integration MODE] [options]

Core install (always runs, never needs the OpenClaw CLI):
  - creates missing private templates in this project only (never overwrites)
  - tightens new .env / cwk-mirror.local.json to mode 0600
  - compiles the scripts and runs the sanitized smoke test
  - prints CWK_CORE_READY on success

OpenClaw integration modes (--integration MODE), choose exactly one:
  none            Core install only. This is the default.
  host-skill      Do not touch protected Skill roots. Print the Skill source
                  path and SKILL_REGISTRATION_REQUIRES_HOST_ADMIN so an operator
                  can register the Skill for one Agent from the host control
                  plane that matches the Gateway version.
  workspace-skill Copy the public skill/ directory into <workspace>/skills/
                  cwk-mirror-workflow, only when that root is really writable.
                  Refuses to overwrite an existing target unless --force.
  router          Maintain a marked CWK router block inside <workspace>/AGENTS.md
                  so the Agent reads the Skill on demand. Idempotent; refuses to
                  touch a file with a broken or duplicated marker pair.

One Agent runs exactly one CWK integration. When a Workspace already holds the
other self-managed mode, the installer stops with
OPENCLAW_INTEGRATION_CONFLICT and changes nothing: remove the previous mode by
hand first. --force does not override that check.

Options:
  --workspace PATH   Workspace root (default: $CWK_WORKSPACE_DIR, else /workspace).
  --skills-dir PATH  Skill root override inside that Workspace
                     (default: <workspace>/skills).
  --agents-file PATH AGENTS.md path inside that Workspace for router mode
                     (default: <workspace>/AGENTS.md).
  --force            Allow replacing an existing workspace Skill target. It does
                     not override the one-integration-per-Agent check.
  --install-skill    Deprecated compatibility entry for the legacy symlink
                     install; see the migration note it prints.
  --help, -h         Show this message.

Detection only ever reports a recommendation. It never selects a mode for you.
EOF
}

die() {
  echo "$1" >&2
  exit "${2:-1}"
}

# Every staged artifact is registered here before it exists on disk, so an
# ordinary failure, a rejected precondition, or a signal can never leave a
# half-written file or a partial Skill directory behind.
CWK_TEMP_PATHS=()

track_temp_path() {
  CWK_TEMP_PATHS+=("$1")
}

cleanup_temp_paths() {
  local path
  for path in ${CWK_TEMP_PATHS[@]+"${CWK_TEMP_PATHS[@]}"}; do
    [ -n "$path" ] || continue
    rm -rf -- "$path" 2>/dev/null || true
  done
  CWK_TEMP_PATHS=()
}

# Re-raise the signal after cleaning up, so the caller still observes a normal
# signal death instead of a fabricated exit code.
cleanup_and_reraise() {
  cleanup_temp_paths
  trap - "$1"
  kill -"$1" $$
}

trap cleanup_temp_paths EXIT
trap 'cleanup_and_reraise INT' INT
trap 'cleanup_and_reraise TERM' TERM
trap 'cleanup_and_reraise HUP' HUP

while [ "$#" -gt 0 ]; do
  case "$1" in
    --integration)
      shift
      [ "$#" -gt 0 ] || die "--integration requires a mode" 2
      INTEGRATION="$1"
      INTEGRATION_SET=true
      ;;
    --workspace)
      shift
      [ "$#" -gt 0 ] || die "--workspace requires a path" 2
      WORKSPACE_DIR="$1"
      ;;
    --skills-dir)
      shift
      [ "$#" -gt 0 ] || die "--skills-dir requires a path" 2
      SKILLS_DIR="$1"
      SKILLS_DIR_SET=true
      ;;
    --agents-file)
      shift
      [ "$#" -gt 0 ] || die "--agents-file requires a path" 2
      AGENTS_FILE="$1"
      ;;
    --force)
      FORCE=true
      ;;
    --install-skill)
      LEGACY_INSTALL_SKILL=true
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1" 2
      ;;
  esac
  shift
done

if [ "$LEGACY_INSTALL_SKILL" = true ] && [ "$INTEGRATION_SET" = true ]; then
  die "--install-skill is the deprecated compatibility entry; do not combine it with --integration" 2
fi

if [ "$INTEGRATION_SET" = false ]; then
  INTEGRATION="none"
fi

case "$INTEGRATION" in
  none|host-skill|workspace-skill|router) ;;
  *) die "Unknown integration mode: $INTEGRATION (expected none, host-skill, workspace-skill, or router)" 2 ;;
esac

# Resolve the workspace only for the modes that actually need one, so a plain
# core install never fails just because this machine has no /workspace.
resolve_workspace() {
  if [ -n "$WORKSPACE_DIR" ]; then
    echo "$WORKSPACE_DIR"
    return 0
  fi
  if [ -d /workspace ]; then
    echo "/workspace"
    return 0
  fi
  return 1
}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  die "Python interpreter not found: $PYTHON"
fi

canonical_workspace() {
  # canonical_workspace <candidate> <project_dir>
  "$PYTHON" - "$1" "$2" <<'PY'
import sys
from pathlib import Path

candidate = Path(sys.argv[1]).expanduser()
project = Path(sys.argv[2])
try:
    resolved = candidate.resolve(strict=True)
except (OSError, RuntimeError) as exc:
    print("cannot resolve workspace %s: %s" % (candidate, exc), file=sys.stderr)
    raise SystemExit(2)
if not resolved.is_dir():
    print("workspace directory does not exist: %s" % candidate, file=sys.stderr)
    raise SystemExit(2)
# The workspace is the containment boundary for every self-managed write.
# The filesystem root would make that boundary meaningless: any absolute path
# would then count as "inside the workspace".
if resolved == Path(resolved.anchor):
    print(
        "refusing to use the filesystem root as a workspace: %s. "
        "A self-managed install needs a real Agent Workspace directory, so that "
        "'inside the workspace' still means something." % resolved,
        file=sys.stderr,
    )
    raise SystemExit(2)
# Installing CWK's own checkout into itself would make the source tree and the
# installed target the same directory.
try:
    project_resolved = project.resolve()
except (OSError, RuntimeError):
    project_resolved = project
if resolved == project_resolved:
    print(
        "refusing to use the CWK project root as a workspace: %s. "
        "Pass the Agent Workspace with --workspace, or use --integration host-skill "
        "when the Skill root is managed elsewhere." % resolved,
        file=sys.stderr,
    )
    raise SystemExit(2)
print(resolved)
PY
}

workspace_relative_path() {
  # workspace_relative_path <workspace> <path>; prints only when path is inside.
  "$PYTHON" - "$1" "$2" <<'PY'
import sys
from pathlib import Path

try:
    workspace = Path(sys.argv[1]).expanduser().resolve(strict=True)
    candidate = Path(sys.argv[2]).expanduser().resolve(strict=True)
    print(candidate.relative_to(workspace).as_posix())
except (OSError, RuntimeError, ValueError):
    raise SystemExit(1)
PY
}

resolve_inside_workspace() {
  # resolve_inside_workspace <workspace> <candidate> <label>
  # Relative candidates are rooted at the workspace. Existing leaf symlinks
  # are rejected, and every resolved target must remain inside the workspace.
  "$PYTHON" - "$1" "$2" "$3" <<'PY'
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
candidate = Path(sys.argv[2]).expanduser()
label = sys.argv[3]
if not candidate.is_absolute():
    candidate = workspace / candidate
if candidate.is_symlink():
    print("%s must not be a symlink: %s" % (label, candidate), file=sys.stderr)
    raise SystemExit(3)
try:
    resolved = candidate.resolve(strict=False)
    resolved.relative_to(workspace)
except (OSError, RuntimeError, ValueError):
    print("%s must stay inside workspace %s: %s" % (label, workspace, candidate), file=sys.stderr)
    raise SystemExit(3)
print(resolved)
PY
}

assert_single_integration_mode() {
  # assert_single_integration_mode <workspace> <skills_dir|""> <agents_file|""> <mode>
  # One Agent runs exactly one CWK integration. Creating the second one would
  # double-trigger the Skill, so this refuses in both directions. It is a
  # read-only probe: it never deletes, migrates, or auto-switches an existing
  # install, and --force deliberately does not apply to it.
  "$PYTHON" - "$1" "$2" "$3" "$4" "$SKILL_NAME" <<'PY'
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
skills_raw = sys.argv[2]
agents_raw = sys.argv[3]
mode = sys.argv[4]
skill_name = sys.argv[5]

BEGIN = b"<!-- BEGIN CWK ROUTER (managed by CWK install.sh) -->"


def rooted(raw: str, default: str) -> Path:
    candidate = Path(raw).expanduser() if raw else workspace / default
    return candidate if candidate.is_absolute() else workspace / candidate


skill_target = rooted(skills_raw, "skills") / skill_name
agents_file = rooted(agents_raw, "AGENTS.md")

formal_present = False
router_present = False
try:
    formal_present = (skill_target / "SKILL.md").is_file()
except OSError:
    formal_present = False
try:
    # Bytes, not text: an AGENTS.md this installer cannot decode must still be
    # recognized as carrying a router block.
    router_present = agents_file.is_file() and BEGIN in agents_file.read_bytes()
except OSError:
    router_present = False

if mode == "workspace-skill" and router_present:
    print("OPENCLAW_INTEGRATION_CONFLICT")
    print("CWK_EXISTING_INTEGRATION=AGENTS_ROUTER")
    print(
        "This Workspace already has a CWK router block in %s. Installing a formal "
        "Skill as well would give one Agent two CWK entry points. Remove the router "
        "block by hand first (or pick a different Workspace); the installer will not "
        "delete it for you, and --force does not apply to this check." % agents_file,
        file=sys.stderr,
    )
    raise SystemExit(3)
if mode == "router" and formal_present:
    print("OPENCLAW_INTEGRATION_CONFLICT")
    print("CWK_EXISTING_INTEGRATION=FORMAL_SKILL")
    print(
        "This Workspace already has a formal CWK Skill at %s. Adding a router block "
        "as well would give one Agent two CWK entry points. Remove that Skill "
        "directory by hand first (or pick a different Workspace); the installer will "
        "not delete it for you, and --force does not apply to this check." % skill_target,
        file=sys.stderr,
    )
    raise SystemExit(3)
PY
}

# Validate integration arguments before mutating anything, so a bad invocation
# fails fast instead of half-installing. Self-service integration is confined
# to one explicit Workspace; host-skill is the only handoff for protected roots.
WORKSPACE_RESOLVED=""
if [ "$INTEGRATION" = "workspace-skill" ] || [ "$INTEGRATION" = "router" ]; then
  if ! workspace_input="$(resolve_workspace)"; then
    die "--integration $INTEGRATION needs a workspace; pass --workspace PATH or set CWK_WORKSPACE_DIR" 2
  fi
  if ! WORKSPACE_RESOLVED="$(canonical_workspace "$workspace_input" "$PROJECT_DIR")"; then
    exit 2
  fi

  if [ "$INTEGRATION" = "workspace-skill" ]; then
    if [ "$SKILLS_DIR_SET" != true ]; then
      SKILLS_DIR="$WORKSPACE_RESOLVED/skills"
    fi
    if ! SKILLS_DIR="$(resolve_inside_workspace "$WORKSPACE_RESOLVED" "$SKILLS_DIR" "Skill root")"; then
      exit 3
    fi
  fi
  if [ "$INTEGRATION" = "router" ]; then
    if [ -z "$AGENTS_FILE" ]; then
      AGENTS_FILE="$WORKSPACE_RESOLVED/AGENTS.md"
    fi
    if ! AGENTS_FILE="$(resolve_inside_workspace "$WORKSPACE_RESOLVED" "$AGENTS_FILE" "AGENTS.md target")"; then
      exit 3
    fi
  fi

  if ! assert_single_integration_mode \
      "$WORKSPACE_RESOLVED" "$SKILLS_DIR" "$AGENTS_FILE" "$INTEGRATION"; then
    echo "OPENCLAW_INTEGRATION=FAILED"
    exit 3
  fi
fi

if [ "$INTEGRATION" = "router" ] && [ ! -f "$ROUTER_TEMPLATE" ]; then
  die "router template is missing: $ROUTER_TEMPLATE"
fi

# ---------------------------------------------------------------------------
# Core installation
# ---------------------------------------------------------------------------

# --project-dir pins the check to the checkout this script belongs to. Without
# it an inherited CWK_PROJECT_DIR pointing at another valid checkout would make
# the doctor validate that other tree and report it as this install's result.
"$PYTHON" scripts/cwk_doctor.py --check-only --project-dir "$PROJECT_DIR" --config cwk-mirror.local.json

create_private_file() {
  # create_private_file <target> <template> <message>
  #
  # Rendered into a same-directory temp file first, then activated with a single
  # link(2). A missing or unreadable template, a full disk, or a signal
  # therefore leaves no target at all -- never a 0-byte file that later installs
  # would faithfully preserve as "already exists".
  local target="$1" template="$2" message="$3"
  if [ -e "$target" ] || [ -L "$target" ]; then
    echo "$target already exists; leaving its content and permissions unchanged."
    return 0
  fi
  if [ ! -f "$template" ]; then
    die "cannot create $target: template is missing: $template"
  fi

  local target_dir target_name staged
  target_dir="$(dirname "$target")"
  target_name="$(basename "$target")"
  if ! staged="$(umask 077 && mktemp "$target_dir/.$target_name.cwk-install.XXXXXX")"; then
    die "cannot create a staging file for $target in $target_dir"
  fi
  track_temp_path "$staged"
  chmod 600 "$staged"
  if ! cat "$template" > "$staged"; then
    rm -f -- "$staged" 2>/dev/null || true
    die "cannot create $target: failed to read template $template"
  fi

  # link(2) fails when the target already exists, so a target created after the
  # check above is preserved exactly instead of being clobbered.
  if ! ln "$staged" "$target" 2>/dev/null; then
    rm -f -- "$staged" 2>/dev/null || true
    if [ -e "$target" ] || [ -L "$target" ]; then
      echo "$target already exists; leaving its content and permissions unchanged."
      return 0
    fi
    die "cannot create $target from $template"
  fi
  rm -f -- "$staged" 2>/dev/null || true
  echo "$message"
}

create_private_file \
  "cwk-mirror.local.json" \
  "skill/templates/CONFIG.example.json" \
  "Created cwk-mirror.local.json (mode 0600) from template. For the default personal mirror, prefer CWORK_APP_KEY in your shell or .env."

create_private_file \
  ".env" \
  ".env.example" \
  "Created .env (mode 0600) from template. Fill CWORK_APP_KEY locally; .env is gitignored."

"$PYTHON" -m py_compile scripts/*.py
make PYTHON="$PYTHON" smoke

echo "Core install complete. Smoke output is under runs/ci-smoke."
echo "CWK_CORE_READY"

# ---------------------------------------------------------------------------
# OpenClaw integration adapters
# ---------------------------------------------------------------------------

install_workspace_skill() {
  # install_workspace_skill <skills_dir>
  local skills_dir="$1"
  local target="$skills_dir/$SKILL_NAME"

  local created_skills_dir=false
  if [ ! -d "$skills_dir" ]; then
    created_skills_dir=true
    if ! mkdir -p "$skills_dir" 2>/dev/null; then
      echo "SKILL_ROOT_NOT_WRITABLE"
      echo "OPENCLAW_INTEGRATION=FAILED"
      echo "Cannot create the Skill root: $skills_dir" >&2
      echo "This root is typically a protected read-only mount. Re-run with --integration host-skill to hand registration to an operator, or --integration router to use an AGENTS.md router instead." >&2
      return 3
    fi
  fi

  # A Skill root created under a strict umask would be 0700 and hide the Skill
  # from a runtime on another uid. Only a root this run created is adjusted;
  # an existing root's permissions are the user's business.
  if [ "$created_skills_dir" = true ]; then
    chmod 755 "$skills_dir" 2>/dev/null || true
  fi

  if [ ! -w "$skills_dir" ]; then
    echo "SKILL_ROOT_NOT_WRITABLE"
    echo "OPENCLAW_INTEGRATION=FAILED"
    echo "Skill root is not writable: $skills_dir" >&2
    echo "This root is typically a protected read-only mount. Re-run with --integration host-skill to hand registration to an operator, or --integration router to use an AGENTS.md router instead." >&2
    return 3
  fi

  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$FORCE" != true ]; then
      echo "OPENCLAW_INTEGRATION=FAILED"
      echo "Refusing to overwrite an existing Skill target: $target" >&2
      echo "It may come from another source or hold manual edits. Inspect it, then re-run with --force if replacing it is really what you want." >&2
      return 3
    fi
  fi

  # Stage a complete copy inside the same filesystem before touching the final
  # target. A read-only mount, quota error, or interrupted copy therefore gets
  # a stable failure status instead of leaving a half-installed Skill target.
  local staging
  if ! staging="$(mktemp -d "$skills_dir/.cwk-skill-install.XXXXXX" 2>/dev/null)"; then
    echo "SKILL_INSTALL_FAILED"
    echo "OPENCLAW_INTEGRATION=FAILED"
    echo "Cannot create a staging directory in the Skill root: $skills_dir" >&2
    return 3
  fi
  track_temp_path "$staging"
  if ! cp -R "$SKILL_SOURCE"/. "$staging"/; then
    rm -rf "$staging" 2>/dev/null || true
    echo "SKILL_INSTALL_FAILED"
    echo "OPENCLAW_INTEGRATION=FAILED"
    echo "Failed to copy the public Skill into the staging directory." >&2
    return 3
  fi

  # mktemp -d creates 0700 and cp applies the caller's umask, so a staged copy
  # can end up private to the installing account. The Agent runtime that has to
  # read this Skill may run under a different uid, so restore the public
  # source-tree semantics: traversable and readable, never group/other writable.
  # 'X' only adds execute where it already exists or on directories.
  if ! chmod -R a+rX,go-w "$staging" || ! chmod 755 "$staging"; then
    rm -rf "$staging" 2>/dev/null || true
    echo "SKILL_INSTALL_FAILED"
    echo "OPENCLAW_INTEGRATION=FAILED"
    echo "Failed to make the staged Skill readable for other accounts." >&2
    return 3
  fi

  if [ -e "$target" ] || [ -L "$target" ]; then
    if ! rm -rf "$target"; then
      rm -rf "$staging" 2>/dev/null || true
      echo "SKILL_INSTALL_FAILED"
      echo "OPENCLAW_INTEGRATION=FAILED"
      echo "Failed to replace the existing Skill target: $target" >&2
      return 3
    fi
  fi
  if ! mv "$staging" "$target"; then
    rm -rf "$staging" 2>/dev/null || true
    echo "SKILL_INSTALL_FAILED"
    echo "OPENCLAW_INTEGRATION=FAILED"
    echo "Failed to activate the staged Skill at: $target" >&2
    return 3
  fi

  echo "CWK_SKILL_SOURCE=$SKILL_SOURCE"
  echo "CWK_SKILL_TARGET=$target"
  echo "OPENCLAW_INTEGRATION=FORMAL_SKILL"
  echo "OPENCLAW_DISCOVERY=UNVERIFIED"
  echo "Copied the public skill/ directory. Whether this Agent's runtime actually discovers and loads the Skill from this root is not verified by the installer; confirm it in the Agent before relying on it."
}

apply_router_block() {
  # apply_router_block <agents_file> <skill_doc_path>
  local agents_file="$1" skill_doc="$2"
  local status=0
  set +e
  "$PYTHON" - "$agents_file" "$ROUTER_TEMPLATE" "$skill_doc" <<'PY'
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

agents_path = Path(sys.argv[1])
template_path = Path(sys.argv[2])
skill_doc = sys.argv[3]

BEGIN = "<!-- BEGIN CWK ROUTER (managed by CWK install.sh) -->"
END = "<!-- END CWK ROUTER -->"


def stop(signum, _frame):
    # Turn a signal into SystemExit so the finally block below still removes
    # the temp file instead of leaving it next to the user's AGENTS.md.
    raise SystemExit(128 + signum)


for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, stop)

try:
    body = template_path.read_text(encoding="utf-8").replace("{{CWK_SKILL_DOC}}", skill_doc)
except (OSError, UnicodeDecodeError) as exc:
    print("AGENTS_ROUTER_TEMPLATE_INVALID")
    print("Cannot read the router template %s: %s" % (template_path, exc), file=sys.stderr)
    raise SystemExit(3)
block = "%s\n%s\n%s" % (BEGIN, body.strip("\n"), END)

# The rendered block is what gets written. If the template (or a substituted
# path) carries its own markers, the result would contain several pairs and the
# next run could no longer tell which one it owns. Refuse before writing.
if block.count(BEGIN) != 1 or block.count(END) != 1:
    print("AGENTS_ROUTER_TEMPLATE_INVALID")
    print(
        "The rendered CWK router block must contain exactly one marker pair "
        "(found %d begin, %d end) from template %s; refusing to write."
        % (block.count(BEGIN), block.count(END), template_path),
        file=sys.stderr,
    )
    raise SystemExit(3)

if agents_path.exists():
    if not agents_path.is_file():
        print("AGENTS_ROUTER_CONFLICT")
        print("Router target is not a regular file: %s" % agents_path, file=sys.stderr)
        raise SystemExit(3)
    try:
        original = agents_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Fail closed and stay quiet about the content: rewriting a file this
        # installer cannot decode would risk destroying it.
        print("AGENTS_ROUTER_UNREADABLE")
        print(
            "%s is not valid UTF-8; refusing to rewrite it. Convert the file to UTF-8 "
            "by hand, or point --agents-file at the intended AGENTS.md." % agents_path,
            file=sys.stderr,
        )
        raise SystemExit(3)
    except OSError as exc:
        print("AGENTS_ROUTER_UNREADABLE")
        print("Cannot read %s: %s" % (agents_path, exc), file=sys.stderr)
        raise SystemExit(3)
else:
    original = ""

begins = original.count(BEGIN)
ends = original.count(END)

# Refuse on a broken or duplicated marker pair instead of guessing which block
# is authoritative. Unrelated content is never rewritten.
if begins != ends:
    print("AGENTS_ROUTER_CONFLICT")
    print(
        "Unbalanced CWK router markers in %s (%d begin, %d end). "
        "Fix the file by hand; the installer will not guess." % (agents_path, begins, ends),
        file=sys.stderr,
    )
    raise SystemExit(3)
if begins > 1:
    print("AGENTS_ROUTER_CONFLICT")
    print(
        "Found %d CWK router blocks in %s; expected at most one. "
        "Remove the duplicates by hand; the installer will not guess." % (begins, agents_path),
        file=sys.stderr,
    )
    raise SystemExit(3)

if begins == 1:
    start = original.index(BEGIN)
    end_start = original.index(END)
    if end_start < start:
        print("AGENTS_ROUTER_CONFLICT")
        print(
            "CWK router end marker appears before its begin marker in %s. "
            "Fix the file by hand; the installer will not guess." % agents_path,
            file=sys.stderr,
        )
        raise SystemExit(3)
    stop = end_start + len(END)
    updated = original[:start] + block + original[stop:]
    action = "unchanged" if updated == original else "updated"
else:
    if original and not original.endswith("\n"):
        original += "\n"
    separator = "\n" if original else ""
    updated = original + separator + block + "\n"
    action = "appended"

# Last gate before writing: the file we are about to produce must still own
# exactly one marker pair, whatever the input looked like.
if updated.count(BEGIN) != 1 or updated.count(END) != 1:
    print("AGENTS_ROUTER_CONFLICT")
    print(
        "The rewritten %s would contain %d begin and %d end CWK markers; expected "
        "exactly one pair. Nothing was written." % (agents_path, updated.count(BEGIN), updated.count(END)),
        file=sys.stderr,
    )
    raise SystemExit(3)

if updated != original:
    tmp_name = None
    try:
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(agents_path.stat().st_mode) if agents_path.exists() else 0o644
        fd, tmp_name = tempfile.mkstemp(
            prefix=".%s.cwk-router." % agents_path.name,
            dir=str(agents_path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, agents_path)
        tmp_name = None
    except OSError as exc:
        print("AGENTS_ROUTER_WRITE_FAILED")
        print("Cannot update %s atomically: %s" % (agents_path, exc), file=sys.stderr)
        raise SystemExit(3)
    finally:
        # Runs for OSError, SystemExit, and KeyboardInterrupt alike.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

print("AGENTS_ROUTER_BLOCK=%s" % action)
PY
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    echo "OPENCLAW_INTEGRATION=FAILED"
    return "$status"
  fi
  echo "CWK_ROUTER_FILE=$agents_file"
  echo "OPENCLAW_INTEGRATION=AGENTS_ROUTER"
  echo "AGENTS_ROUTER_ACTIVATION=NEXT_SESSION"
  echo "OPENCLAW_DISCOVERY=UNVERIFIED"
}

if [ "$LEGACY_INSTALL_SKILL" = true ]; then
  legacy_dir="${SKILLS_DIR:-${OPENCLAW_SKILLS_DIR:-$HOME/.openclaw/skills}}"
  target="$legacy_dir/$SKILL_NAME"
  echo "CWK_INSTALL_SKILL_DEPRECATED"
  echo "--install-skill still performs the legacy symlink install and keeps its refusal guards." >&2
  echo "Migrate to an explicit mode: --integration workspace-skill (writable workspace), --integration host-skill (protected/read-only Skill roots), or --integration router (self-service sandbox)." >&2

  mkdir -p "$legacy_dir"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "OPENCLAW_INTEGRATION=FAILED"
    die "Refusing to overwrite non-link skill path: $target" 3
  fi
  if [ -L "$target" ] && [ "$(readlink "$target")" != "$SKILL_SOURCE" ]; then
    echo "OPENCLAW_INTEGRATION=FAILED"
    die "Refusing to replace an existing skill link: $target" 3
  fi
  ln -sfn "$SKILL_SOURCE" "$target"
  echo "Linked CWK skill: $target -> $SKILL_SOURCE"
  echo "CWK_SKILL_SOURCE=$SKILL_SOURCE"
  echo "CWK_SKILL_TARGET=$target"
  echo "OPENCLAW_INTEGRATION=FORMAL_SKILL"
  echo "OPENCLAW_DISCOVERY=UNVERIFIED"
  echo "A symlink existing does not prove this Agent's OpenClaw runtime discovers or loads the Skill; confirm that in the Agent."
  exit 0
fi

case "$INTEGRATION" in
  none)
    echo "OPENCLAW_INTEGRATION=NONE"
    echo "Core program installed without OpenClaw integration. Re-run with --integration host-skill, workspace-skill, or router to connect one Agent."
    ;;
  host-skill)
    echo "CWK_SKILL_SOURCE=$SKILL_SOURCE"
    echo "CWK_SKILL_SOURCE_SCOPE=CURRENT_EXECUTION_ENV"
    if workspace_hint="$(resolve_workspace 2>/dev/null)" && \
       source_relative="$(workspace_relative_path "$workspace_hint" "$SKILL_SOURCE" 2>/dev/null)"; then
      echo "CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE=$source_relative"
    fi
    echo "SKILL_REGISTRATION_REQUIRES_HOST_ADMIN"
    echo "OPENCLAW_INTEGRATION=HOST_SKILL_PENDING"
    echo "Nothing was written to any Skill root. An operator must resolve the corresponding host Agent Workspace path (a /workspace/... source is a sandbox path, not a host path), then register '$SKILL_NAME' for the one intended Agent from the host control plane that matches the Gateway version. Do not copy private config, credentials, or run data."
    ;;
  workspace-skill)
    install_workspace_skill "$SKILLS_DIR"
    ;;
  router)
    apply_router_block "$AGENTS_FILE" "$PROJECT_DIR/skill/SKILL.md"
    ;;
esac
