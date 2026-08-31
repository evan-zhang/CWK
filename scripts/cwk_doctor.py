#!/usr/bin/env python3
"""Validate a CWK installation without collecting or publishing business data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = Path("knowledge") / "工作协同镜像"
DEFAULT_CWORK_SKILL = Path.home() / ".openclaw" / "skills" / "cms-cwork-workflow"
DEFAULT_DOCDB_SKILL = Path.home() / ".agents" / "skills" / "cms-docdb"
DEFAULT_AUTH_SKILL = Path.home() / ".agents" / "skills" / "cms-auth-skills"
MIN_PYTHON = (3, 10)


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


def run_checks(config: dict[str, Any], require_live: bool, require_docdb: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

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
    scripts_ok = all((PROJECT / "scripts" / name).is_file() for name in required_scripts)
    checks.append({"name": "project_scripts", "ok": scripts_ok, "value": str(PROJECT)})
    if not scripts_ok:
        errors.append("CWK script package is incomplete")

    raw_mirror = str(config.get("mirror_root") or os.environ.get("CWK_MIRROR_ROOT") or DEFAULT_MIRROR)
    mirror_path = Path(raw_mirror).expanduser()
    if not mirror_path.is_absolute():
        mirror_path = PROJECT / mirror_path
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

    cwork_skill = resolve_path(os.environ.get("CMS_CWORK_WORKFLOW_DIR", ""), DEFAULT_CWORK_SKILL)
    docdb_skill = resolve_path(os.environ.get("CMS_DOCDB_SKILL_DIR", ""), DEFAULT_DOCDB_SKILL)
    auth_skill = resolve_path(os.environ.get("CMS_AUTH_SKILL_DIR", ""), DEFAULT_AUTH_SKILL)
    if require_live:
        cwork_ok = (cwork_skill / "scripts" / "cwork-query-report.py").is_file()
        auth_ok = (auth_skill / "scripts" / "auth" / "login.py").is_file() or bool(os.environ.get("CMS_AUTH_LOGIN"))
        auth_input_ok = bool(os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or os.environ.get("CWK_SENDER_ID"))
        checks.extend([
            {"name": "cms_cwork_workflow", "ok": cwork_ok, "value": str(cwork_skill)},
            {"name": "cms_auth_skills", "ok": auth_ok, "value": str(auth_skill)},
            {"name": "live_auth_configured", "ok": auth_input_ok, "value": "configured" if auth_input_ok else "missing"},
        ])
        if not cwork_ok:
            errors.append("cms-cwork-workflow is unavailable; set CMS_CWORK_WORKFLOW_DIR or install the company Skill")
        if not auth_ok:
            errors.append("cms-auth-skills is unavailable; set CMS_AUTH_SKILL_DIR or CMS_AUTH_LOGIN")
        if not auth_input_ok:
            errors.append("live collection needs CWORK_APP_KEY, XG_BIZ_API_KEY, or CWK_SENDER_ID routing")
    else:
        warnings.append("live CWork and auth checks skipped; pass --require-live before a real collection")

    if require_docdb:
        docdb_ok = (docdb_skill / "scripts" / "browse" / "get-personal-project-id.py").is_file()
        checks.append({"name": "cms_docdb", "ok": docdb_ok, "value": str(docdb_skill)})
        if not docdb_ok:
            errors.append("cms-docdb is unavailable; set CMS_DOCDB_SKILL_DIR or install the company Skill")
    else:
        warnings.append("DocDB write checks skipped; pass --require-docdb before enabling --sync-docdb")

    return {
        "schema_version": "cwk.doctor.v1",
        "project": str(PROJECT),
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "next_step": "run a no-publish smoke test" if not errors else "resolve the listed errors and rerun doctor",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a portable CWK installation without reading business data.")
    parser.add_argument("--config", default="cwk-mirror.local.json")
    parser.add_argument("--check-only", action="store_true", help="Validate only local package and Python requirements.")
    parser.add_argument("--require-live", action="store_true", help="Also validate live CWork and auth prerequisites.")
    parser.add_argument("--require-docdb", action="store_true", help="Also validate DocDB prerequisite paths.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()
    try:
        config = read_config(args.config) if Path(args.config).expanduser().exists() else {}
        result = run_checks(config, args.require_live and not args.check_only, args.require_docdb and not args.check_only)
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
