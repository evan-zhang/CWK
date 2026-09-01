#!/usr/bin/env python3
"""Validate a CWK installation without collecting or publishing business data.

Security boundary (RT-026 owner surface, extended by RT-031):

* The project ``.env`` is parsed with a minimal dotenv reader. Its content is
  never executed as shell, never printed, and never echoed back in any form --
  not the value, not a prefix, not a hash, not a reversible fragment. Only
  ``configured`` / ``missing`` is reported.
* That reader accepts exactly what ``cwk_nightly_pipeline.py`` accepts. A
  readiness answer that the runtime would contradict is a false answer, so the
  two parsers must stay aligned; see ``parse_env_file``.
* Nothing here reads CWork, writes DocDB, creates cron jobs, or mutates an Agent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_MIRROR = Path("knowledge") / "工作协同镜像"
MIN_PYTHON = (3, 10)
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Credential-bearing names. Their presence may be reported; their content is not.
SECRET_ENV_KEYS = (
    "CWORK_APP_KEY",
    "XG_BIZ_API_KEY",
    "XG_APP_KEY",
    "CWK_SENDER_ID",
)

# Company Skills CWK can consume, with the marker file that proves a real
# installation rather than an empty directory.
COMPANY_SKILLS = {
    "cms-cwork-workflow": Path("scripts") / "cwork-query-report.py",
    "cms-auth-skills": Path("scripts") / "auth" / "login.py",
    "cms-docdb": Path("scripts") / "browse" / "get-personal-project-id.py",
}

SKILL_DIR_ENV = {
    "cms-cwork-workflow": "CMS_CWORK_WORKFLOW_DIR",
    "cms-auth-skills": "CMS_AUTH_SKILL_DIR",
    "cms-docdb": "CMS_DOCDB_SKILL_DIR",
}


def safe_home() -> Path | None:
    """Return the home directory, tolerating HOME=/ and an unset HOME.

    ``Path.home()`` raises when HOME is missing and there is no passwd entry,
    which is exactly the sandbox case this must survive.
    """
    raw = os.environ.get("HOME")
    if raw:
        return Path(raw)
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def find_project_root(explicit: str | None = None) -> Path:
    """Locate the CWK project root without assuming a fixed layout.

    Order: explicit argument, CWK_PROJECT_DIR, the directory this file lives in,
    then /workspace/CWK. A candidate only counts when it actually holds the
    script package, so a stale variable cannot silently redirect the checks.
    """
    marker = Path("scripts") / "cwk_doctor.py"
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_dir = os.environ.get("CWK_PROJECT_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(__file__).resolve().parents[1])
    candidates.append(Path("/workspace/CWK"))

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if (resolved / marker).is_file():
            return resolved
    return Path(__file__).resolve().parents[1]


PROJECT = find_project_root()


def parse_env_file(text: str) -> dict[str, str]:
    """Minimal dotenv parser. It never executes shell and never expands anything.

    This deliberately mirrors ``load_local_env`` in ``cwk_nightly_pipeline.py``,
    including what it rejects: ``export KEY=value`` yields the key ``"export
    KEY"``, which fails the key pattern and is dropped. Accepting it here would
    make the doctor report ``configured`` for a line the runtime ignores, which
    is worse than reporting ``missing`` -- the user would discover the real
    state only during a live collection.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    try:
        if not path.is_file():
            return {}
        return parse_env_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def build_env(project: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Merge the project .env under the process environment.

    The process environment always wins, mirroring how cwk_nightly_pipeline.py
    loads .env with setdefault. Nothing is written back into os.environ, so
    reading the doctor's view can never leak a value into a child process.
    """
    env: dict[str, str] = dict(load_env_file(project / ".env"))
    env.update(dict(os.environ) if base_env is None else base_env)
    return env


def skill_roots(env: dict[str, str], project: Path) -> list[Path]:
    """Candidate Skill roots, in priority order, deduplicated.

    Covers per-user roots, workspace roots, and materialized roots that are
    readable but not writable inside a sandbox. CWK_SKILL_ROOTS lets an operator
    name a root this list does not know about.
    """
    roots: list[Path] = []

    for item in env.get("CWK_SKILL_ROOTS", "").split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item))

    workspace = env.get("CWK_WORKSPACE_DIR", "").strip()
    workspace_dirs = [Path(workspace)] if workspace else []
    workspace_dirs.append(Path("/workspace"))
    for base in workspace_dirs:
        roots.append(base / "skills")
        roots.append(base / ".agents" / "skills")
        # OpenClaw materializes eligible managed, bundled, and plugin Skills in
        # this generated read-only root when sandbox workspaceAccess is "rw".
        roots.append(base / ".openclaw" / "sandbox-skills" / "skills")
        # Retain the older generic candidate for deployments that explicitly
        # mirror Skills there; it is not the current OpenClaw sandbox path.
        roots.append(base / ".openclaw" / "skills")

    home = safe_home()
    if home is not None:
        roots.append(home / ".openclaw" / "skills")
        roots.append(home / ".agents" / "skills")

    roots.append(project.parent / "skills")

    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        try:
            key = str(root.expanduser())
        except (OSError, RuntimeError):
            continue
        if key not in seen:
            seen.add(key)
            ordered.append(Path(key))
    return ordered


def find_company_skill(
    name: str,
    env: dict[str, str],
    roots: list[Path],
) -> tuple[bool, str]:
    """Locate one company Skill. Returns (found, non-secret location label)."""
    marker = COMPANY_SKILLS[name]

    override = env.get(SKILL_DIR_ENV[name], "").strip()
    if override:
        # An explicit directory is authoritative: if it is wrong, say so rather
        # than silently falling back to some other root.
        candidate = Path(override).expanduser()
        return (candidate / marker).is_file(), str(candidate)

    for root in roots:
        candidate = root / name
        if (candidate / marker).is_file():
            return True, str(candidate)
    return False, "not found in %d candidate Skill roots" % len(roots)


def detect_integration(env: dict[str, str], project: Path, roots: list[Path]) -> dict[str, Any]:
    """Report which OpenClaw integrations are visible. Never selects one."""
    formal: list[str] = []
    for root in roots:
        candidate = root / "cwk-mirror-workflow"
        if (candidate / "SKILL.md").is_file():
            formal.append(str(candidate))

    router_files: list[str] = []
    begin = "<!-- BEGIN CWK ROUTER (managed by CWK install.sh) -->"
    workspace = env.get("CWK_WORKSPACE_DIR", "").strip()
    agents_candidates = [Path(workspace) / "AGENTS.md"] if workspace else []
    agents_candidates.append(Path("/workspace/AGENTS.md"))
    agents_candidates.append(project.parent / "AGENTS.md")
    for candidate in agents_candidates:
        try:
            if candidate.is_file() and begin in candidate.read_text(encoding="utf-8"):
                router_files.append(str(candidate))
        except (OSError, UnicodeDecodeError):
            continue

    if formal:
        detected = "FORMAL_SKILL"
    elif router_files:
        detected = "AGENTS_ROUTER"
    else:
        detected = "NONE"
    return {
        "detected": detected,
        "formal_skill_paths": formal,
        "router_files": sorted(set(router_files)),
    }


def resolve_path(value: str, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default.expanduser().resolve()


def read_config(path: str) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ValueError("config file does not exist: %s" % config_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config root must be a JSON object: %s" % config_path)
    return payload


def run_checks(
    config: dict[str, Any],
    require_live: bool,
    require_docdb: bool,
    project: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    project = PROJECT if project is None else Path(project)
    env = build_env(project) if env is None else env

    python_ok = sys.version_info[:2] >= MIN_PYTHON
    checks.append({
        "name": "python",
        "ok": python_ok,
        "value": ".".join(str(part) for part in sys.version_info[:3]),
        "required": "Python 3.10+",
    })
    if not python_ok:
        errors.append("Python 3.10+ is required; run this installer with PYTHON=python3.11 or a newer interpreter")

    required_scripts = ["cwk_nightly_pipeline.py", "cwk_collect_live.py", "cwk_doctor.py"]
    scripts_ok = all((project / "scripts" / name).is_file() for name in required_scripts)
    checks.append({"name": "project_scripts", "ok": scripts_ok, "value": str(project)})
    if not scripts_ok:
        errors.append("CWK script package is incomplete")

    # Presence only. The file's content is never read back to the caller.
    env_file = project / ".env"
    checks.append({
        "name": "env_file",
        "ok": True,
        "value": "present" if env_file.is_file() else "absent",
        "path": str(env_file),
    })

    raw_mirror = str(config.get("mirror_root") or env.get("CWK_MIRROR_ROOT") or DEFAULT_MIRROR)
    mirror_path = Path(raw_mirror).expanduser()
    if not mirror_path.is_absolute():
        mirror_path = project / mirror_path
    mirror_path = mirror_path.resolve()
    legacy_path = "CWK-20260708-001" in raw_mirror or "workspace-life" in raw_mirror
    checks.append({
        "name": "mirror_root",
        "ok": not legacy_path,
        "value": str(mirror_path),
        "configured_value": raw_mirror,
    })
    if legacy_path:
        errors.append("mirror_root contains an Evan-specific legacy path; use a relative path such as knowledge/工作协同镜像")

    roots = skill_roots(env, project)
    integration = detect_integration(env, project, roots)
    checks.append({
        "name": "openclaw_integration",
        "ok": True,
        "value": integration["detected"],
        "formal_skill_paths": integration["formal_skill_paths"],
        "router_files": integration["router_files"],
    })
    if integration["formal_skill_paths"] and integration["router_files"]:
        warnings.append(
            "both a formal Skill and an AGENTS.md router block are present; "
            "keep exactly one CWK integration per Agent to avoid double triggering"
        )

    if require_live:
        cwork_ok, cwork_where = find_company_skill("cms-cwork-workflow", env, roots)
        auth_ok, auth_where = find_company_skill("cms-auth-skills", env, roots)
        if not auth_ok and env.get("CMS_AUTH_LOGIN"):
            auth_ok, auth_where = True, "CMS_AUTH_LOGIN"
        # Presence of a credential only. Never the value, prefix, or hash.
        auth_input_ok = any(env.get(key) for key in SECRET_ENV_KEYS)
        checks.extend([
            {"name": "cms_cwork_workflow", "ok": cwork_ok, "value": cwork_where},
            {"name": "cms_auth_skills", "ok": auth_ok, "value": auth_where},
            {"name": "live_auth_configured", "ok": auth_input_ok, "value": "configured" if auth_input_ok else "missing"},
        ])
        if not cwork_ok:
            errors.append("cms-cwork-workflow is unavailable; set CMS_CWORK_WORKFLOW_DIR or install the company Skill")
        if not auth_ok:
            errors.append("cms-auth-skills is unavailable; set CMS_AUTH_SKILL_DIR or CMS_AUTH_LOGIN")
        if not auth_input_ok:
            errors.append("live collection needs CWORK_APP_KEY, XG_BIZ_API_KEY, XG_APP_KEY, or CWK_SENDER_ID routing")
    else:
        warnings.append("live CWork and auth checks skipped; pass --require-live before a real collection")

    if require_docdb:
        docdb_ok, docdb_where = find_company_skill("cms-docdb", env, roots)
        checks.append({"name": "cms_docdb", "ok": docdb_ok, "value": docdb_where})
        if not docdb_ok:
            errors.append("cms-docdb is unavailable; set CMS_DOCDB_SKILL_DIR or install the company Skill")
    else:
        warnings.append("DocDB write checks skipped; pass --require-docdb before enabling --sync-docdb")

    return {
        "schema_version": "cwk.doctor.v1",
        "project": str(project),
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "next_step": "run a no-publish smoke test" if not errors else "resolve the listed errors and rerun doctor",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a portable CWK installation without reading business data.")
    parser.add_argument("--config", default="cwk-mirror.local.json")
    parser.add_argument("--project-dir", default="", help="CWK project root; defaults to CWK_PROJECT_DIR or this script's package.")
    parser.add_argument("--check-only", action="store_true", help="Validate only local package and Python requirements.")
    parser.add_argument("--require-live", action="store_true", help="Also validate live CWork and auth prerequisites.")
    parser.add_argument("--require-docdb", action="store_true", help="Also validate DocDB prerequisite paths.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()
    try:
        project = find_project_root(args.project_dir or None)
        config = read_config(args.config) if Path(args.config).expanduser().exists() else {}
        result = run_checks(
            config,
            args.require_live and not args.check_only,
            args.require_docdb and not args.check_only,
            project=project,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"schema_version": "cwk.doctor.v1", "passed": False, "errors": [str(exc)], "warnings": [], "checks": []}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("CWK doctor: %s" % ("PASS" if result["passed"] else "FAIL"))
        for check in result.get("checks", []):
            print("- [%s] %s: %s" % ("ok" if check["ok"] else "fail", check["name"], check["value"]))
        for warning in result.get("warnings", []):
            print("- warning: %s" % warning)
        for error in result.get("errors", []):
            print("- error: %s" % error)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
