#!/usr/bin/env python3
"""Shared runtime and validation helpers for the optional CWK AI stages."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
RECORD_SCHEMA = "cwk.ai_record_understanding.v1"
EVENTS_SCHEMA = "cwk.ai_events.v1"
PRIORITIES_SCHEMA = "cwk.ai_daily_priorities.v1"
QUALITY_SCHEMA = "cwk.ai_quality_review.v1"
_SAFE_AGENTS: set[str] = set()
AI_ENV_ALLOWLIST = {"OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_PASSWORD"}

# ── Model allowlist ──────────────────────────────────────────────
# CWK business pipeline permits ONLY these models.
# See projects/CWK/MODEL_ROLES.md for the full rationale.
CWK_ALLOWED_MODELS: set[str] = {
    "newapi/BD-MiniMax",
    "newapi/BD-glm",
    "evan-openai/glm-5.3-flash",
    "deepseek/deepseek-v4-flash",
}
TEMPORARY_GPT56_BATCH_MODELS = {
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
}


def allowed_cwk_models() -> set[str]:
    """Return the normal allowlist plus an explicit, time-boxed batch override.

    GPT-5.6 is intentionally unavailable to ordinary CWK runs.  A human must
    set ``CWK_TEMP_GPT56_BATCH=1`` for a one-off refinement sprint; this keeps
    the historical MiniMax/GLM production contract intact by default.
    """
    models = set(CWK_ALLOWED_MODELS)
    if os.environ.get("CWK_TEMP_GPT56_BATCH") == "1":
        models.update(TEMPORARY_GPT56_BATCH_MODELS)
    return models


def assert_cwk_model(model: str) -> None:
    """Reject models outside the CWK allowlist before any AI call."""
    allowed_models = allowed_cwk_models()
    if not model:
        raise ValueError(
            "CWK AI model is empty. Set CWK_AI_*_MODEL to one of: "
            + ", ".join(sorted(allowed_models))
        )
    if model not in allowed_models:
        raise ValueError(
            f"CWK pipeline rejects model {model!r}. "
            f"Allowed models: {', '.join(sorted(allowed_models))}. "
            "See projects/CWK/MODEL_ROLES.md."
        )
def ai_agent_workspace() -> Path:
    workspace = PROJECT.resolve() / ".cwk-ai-runtime"
    if workspace.is_symlink():
        raise RuntimeError("CWK AI runtime workspace must not be a symlink")
    return workspace


@contextmanager
def ai_runtime_guard():
    """Prevent concurrent AI pilots and clear prompt remnants from interrupted runs."""
    workspace = ai_agent_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = workspace / ".pilot.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another CWK AI pilot is already using the runtime workspace") from exc
        prompt_dir = workspace / "prompts"
        if prompt_dir.exists():
            for path in prompt_dir.iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[end + 4 :].lstrip("\n")


def clean_evidence(value: str, limit: int = 180) -> str:
    value = re.sub(r"```.*?```", " ", value or "", flags=re.S)
    value = re.sub(r"<[^>]+>|&nbsp;", " ", value)
    value = re.sub(r"[*_`#|]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -:：,，;；")
    return value[:limit]


def first_readable_sentence(body: str, fallback: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", body, flags=re.S)
    lines = []
    for raw_line in cleaned.splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        line = clean_evidence(raw_line)
        if len(line) < 8 or line.startswith(("report id", "title", "reference_")):
            continue
        if line.startswith(("{", "[", '"nodeName"', '"content"')):
            continue
        lines.append(line)
    return (lines[0] if lines else clean_evidence(fallback))[:180]


def to_shanghai(value: str) -> str:
    if not value:
        return ""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        return parsed.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19]


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract one JSON object from plain text or an OpenClaw JSON envelope."""
    text = text.strip()
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    candidates.extend(fenced)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            nested = _find_nested_json(value)
            return nested or value
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                nested = _find_nested_json(value)
                return nested or value
    raise ValueError("model output did not contain a JSON object")


def _find_nested_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if "schema_version" in value or _looks_like_summary_payload(value):
            return value
        preferred_keys = ("text", "content", "message", "output", "response", "reply")
        for key in preferred_keys:
            child = value.get(key)
            if isinstance(child, str):
                try:
                    parsed = extract_json_object(child)
                except ValueError:
                    continue
                if "schema_version" in parsed or _looks_like_summary_payload(parsed):
                    return parsed
        for child in value.values():
            found = _find_nested_json(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested_json(child)
            if found:
                return found
    return None


def _looks_like_summary_payload(value: dict[str, Any]) -> bool:
    """Recognize a model payload even when it omitted deterministic identity keys.

    Some otherwise valid MiniMax/GLM responses omit ``schema_version`` and
    ``report_id``.  The caller already owns those deterministic values, so the
    JSON extractor should still unwrap the semantic payload instead of
    returning the outer OpenClaw envelope.  Validation remains responsible for
    rejecting a conflicting identity.
    """
    if not isinstance(value.get("summary"), str) or not value.get("summary", "").strip():
        return False
    structural_keys = {
        "key_facts",
        "decisions",
        "action_items",
        "risks",
        "topics",
        "entities",
        "evidence_refs",
    }
    return bool(structural_keys.intersection(value))


def invoke_openclaw_json(
    prompt: str,
    *,
    model: str,
    stage: str,
    timeout_seconds: int,
    prompt_dir: Path,
) -> dict[str, Any]:
    if not model:
        raise ValueError(f"model is required for real AI stage {stage}")
    assert_cwk_model(model)
    runtime_workspace = ai_agent_workspace()
    agent_id = os.environ.get("CWK_AI_AGENT_ID", "cwk-ai-reviewer")
    assert_safe_ai_agent(agent_id)
    thinking = os.environ.get("CWK_AI_THINKING", "high")
    instruction = (
        "You are a JSON-only responder. Follow the instructions below exactly. "
        "Return only the requested JSON object — no prose, no markdown fences.\n\n"
        + prompt
    )
    attempts = max(1, int(os.environ.get("CWK_AI_CALL_RETRIES", "3")))
    last_error = "unknown model failure"
    for attempt in range(attempts):
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path: Path | None = None
        session_id = str(uuid.uuid4())
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=prompt_dir, prefix=f"{stage}-", suffix=".txt", delete=False) as handle:
                handle.write(instruction)
                prompt_path = Path(handle.name)
            # Start a new process group.  The OpenClaw CLI can leave its
            # gateway child alive after its own timeout; killing only the CLI
            # then leaves this caller blocked in ``communicate`` forever.
            proc = subprocess.Popen(
                [
                    "openclaw",
                    "agent",
                    "--local",
                    "--agent",
                    agent_id,
                    "--session-id",
                    session_id,
                    "--model",
                    model,
                    "--message-file",
                    str(prompt_path),
                    "--thinking",
                    thinking,
                    "--timeout",
                    str(timeout_seconds),
                    "--json",
                ],
                cwd=str(runtime_workspace),
                env=sanitized_ai_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds + 30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()
                raise
            if proc.returncode == 0:
                result = extract_json_object(stdout)
                if "error" in result and "schema_version" not in result:
                    raise RuntimeError(f"agent returned error: {compact(result.get('error'), 400)}")
                return result
            last_error = clean_evidence(stderr or stdout, 500)
        except subprocess.TimeoutExpired:
            last_error = f"model call timed out after {timeout_seconds + 30}s"
        finally:
            if prompt_path and prompt_path.exists():
                prompt_path.unlink()
            # Reviewer calls are one-shot transforms. Keeping their transcripts
            # indefinitely bloats the gateway session index and delays user turns.
            try:
                subprocess.run(
                    [
                        "openclaw",
                        "gateway",
                        "call",
                        "sessions.delete",
                        "--params",
                        json.dumps(
                            {
                                "key": f"agent:{agent_id}:explicit:{session_id}",
                                "agentId": agent_id,
                                "deleteTranscript": True,
                            }
                        ),
                    ],
                    cwd=str(runtime_workspace),
                    env=sanitized_ai_environment(),
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if attempt + 1 < attempts:
            time.sleep(2**attempt)
    raise RuntimeError(f"OpenClaw model call failed after {attempts} attempts: {last_error}")


def sanitized_ai_environment() -> dict[str, str]:
    """Keep runtime settings while withholding unrelated application/provider secrets."""
    sanitized = {}
    for key, value in os.environ.items():
        upper = key.upper()
        looks_secret = upper.endswith(("_API_KEY", "_APP_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        if looks_secret and upper not in AI_ENV_ALLOWLIST:
            continue
        sanitized[key] = value
    sanitized.pop("CWORK_APP_KEY", None)
    sanitized.pop("XG_BIZ_API_KEY", None)
    return sanitized


def safe_agent_policy(agent: dict[str, Any]) -> tuple[bool, str]:
    if agent.get("skills") != []:
        return False, "skills must be an explicit empty list"
    declared_workspace = Path(str(agent.get("workspace", ""))).expanduser()
    if not declared_workspace.is_absolute() or declared_workspace.is_symlink():
        return False, "workspace must be a non-symlink absolute path"
    workspace = Path(os.path.abspath(str(declared_workspace)))
    if workspace != ai_agent_workspace():
        return False, "workspace must match the fixed private CWK AI runtime workspace"
    sandbox = agent.get("sandbox") or {}
    if sandbox.get("mode") != "off":
        return False, "sandbox must use mode=off; CWK reviewers are zero-tool message transformers"
    tools = agent.get("tools") or {}
    if tools.get("profile") != "minimal":
        return False, "tools.profile must be minimal"
    allow = tools.get("allow") or []
    also_allow = tools.get("alsoAllow") or []
    deny = tools.get("deny") or []
    if not isinstance(allow, list) or not isinstance(also_allow, list) or not isinstance(deny, list):
        return False, "tool allow/deny lists must be arrays"
    if allow or also_allow:
        return False, "CWK reviewer must not allow any tools"
    if set(deny) != {"*"}:
        return False, "CWK reviewer must deny all tools with wildcard '*'"
    return True, "ok"


def assert_safe_ai_agent(agent_id: str) -> None:
    expected_workspace = ai_agent_workspace()
    cache_key = f"{agent_id}:{expected_workspace}"
    if cache_key in _SAFE_AGENTS:
        return
    proc = subprocess.run(
        ["openclaw", "config", "get", "agents.list", "--json"],
        cwd=str(PROJECT),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError("could not inspect OpenClaw agent policy")
    agents = json.loads(proc.stdout)
    agent = next((item for item in agents if item.get("id") == agent_id), None)
    if not agent:
        raise RuntimeError(f"CWK AI agent {agent_id!r} is not configured")
    safe, reason = safe_agent_policy(agent)
    if not safe:
        raise RuntimeError(f"CWK AI agent {agent_id!r} is unsafe: {reason}")
    _SAFE_AGENTS.add(cache_key)


def _document_type(title: str) -> str:
    mapping = [
        ("会议纪要", "meeting_minutes"),
        ("周报", "weekly_report"),
        ("日报", "daily_report"),
        ("合同", "contract_legal"),
        ("法务", "contract_legal"),
        ("申请", "request"),
        ("方案", "technical_plan"),
    ]
    return next((kind for token, kind in mapping if token in title), "other")


def fallback_record(raw_path: Path, extracted: dict[str, Any], status: str = "dry_run") -> dict[str, Any]:
    text = raw_path.read_text(encoding="utf-8", errors="ignore")
    meta, body = parse_frontmatter(text)
    report_id = str(meta.get("report_id") or (extracted.get("source_ids") or [raw_path.stem])[0])
    title = meta.get("title") or extracted.get("title") or raw_path.stem
    evidence = first_readable_sentence(body, title)
    actions = [clean_evidence(item) for item in extracted.get("actions", []) if clean_evidence(item)]
    risks = [clean_evidence(item) for item in extracted.get("risks", []) if clean_evidence(item)]
    decisions = [clean_evidence(item) for item in extracted.get("decision_points", []) if clean_evidence(item)]
    raw_entities = extracted.get("entities", {})
    entities = {
        "people": raw_entities.get("people", []),
        "teams": raw_entities.get("orgs", []),
        "systems": raw_entities.get("systems", []),
        "products": raw_entities.get("products", []),
        "projects": raw_entities.get("projects", []),
    }
    attention = extracted.get("attention_type", "")
    priority = {"requires_action": "must_read", "optional_review": "review", "awareness_only": "FYI"}.get(attention, "archive")
    return {
        "schema_version": RECORD_SCHEMA,
        "ai_status": status,
        "report_id": report_id,
        "title": title,
        "writer": meta.get("writer", ""),
        "created_at_shanghai": to_shanghai(meta.get("create_time", "")),
        "source_lane": meta.get("source_lane") or extracted.get("source_lane", "unknown"),
        "document_type": _document_type(title),
        "event_anchor": extracted.get("event_anchor") or title[:30],
        "event_anchor_confidence": 0.55,
        "summary": evidence,
        "background": evidence,
        "decisions": [{"text": item, "evidence": evidence} for item in decisions[:5]],
        "action_items": [
            {"task": item, "owner": None, "due_date": None, "status": "unknown", "evidence": evidence}
            for item in actions[:8]
        ],
        "risks": [{"risk": item, "severity": "unknown", "evidence": evidence} for item in risks[:5]],
        "entities": entities,
        "priority_hint": priority,
        "noise_flags": [],
        "evidence_refs": [{"report_id": report_id, "quote": evidence}],
    }


def _quote_in_source(quote: str, source: str) -> bool:
    normalized_quote = re.sub(r"\s+", "", quote or "")
    normalized_source = re.sub(r"\s+", "", source or "")
    return bool(normalized_quote) and normalized_quote in normalized_source


def validate_record(payload: dict[str, Any], report_id: str, evidence_source: str | None = None) -> list[str]:
    errors = []
    required = ("title", "summary", "event_anchor", "entities", "action_items", "risks", "evidence_refs")
    if payload.get("schema_version") != RECORD_SCHEMA:
        errors.append("invalid schema_version")
    if str(payload.get("report_id")) != str(report_id):
        errors.append("report_id mismatch")
    for key in required:
        if key not in payload:
            errors.append(f"missing {key}")
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list) or not any(str(ref.get("report_id")) == str(report_id) and ref.get("quote") for ref in refs if isinstance(ref, dict)):
        errors.append("missing traceable evidence_refs")
    if evidence_source is not None:
        for ref in refs or []:
            if isinstance(ref, dict) and ref.get("quote") and not _quote_in_source(str(ref["quote"]), evidence_source):
                errors.append("evidence_ref quote not found in source")
        for collection, evidence_key in (("decisions", "evidence"), ("action_items", "evidence"), ("risks", "evidence")):
            for item in payload.get(collection, []):
                evidence = item.get(evidence_key, "") if isinstance(item, dict) else ""
                if not _quote_in_source(str(evidence), evidence_source):
                    errors.append(f"{collection} item missing exact source evidence")
    return errors


def normalize_record(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Fill non-evidentiary structural gaps from the deterministic extraction."""
    normalized = dict(fallback)
    normalized.update(payload)
    filled = []
    for key in (
        "title",
        "writer",
        "created_at_shanghai",
        "source_lane",
        "document_type",
        "event_anchor",
        "event_anchor_confidence",
        "background",
        "entities",
        "priority_hint",
        "noise_flags",
    ):
        if key not in payload or payload.get(key) in (None, ""):
            normalized[key] = fallback.get(key)
            filled.append(key)
    normalized["schema_version"] = RECORD_SCHEMA
    normalized["report_id"] = fallback["report_id"]
    for key in ("title", "writer", "created_at_shanghai", "source_lane"):
        if normalized.get(key) != fallback.get(key):
            filled.append(f"deterministic_{key}")
        normalized[key] = fallback.get(key)
    normalized["normalization_flags"] = sorted(set(payload.get("normalization_flags", []) + filled))
    return normalized


def sanitize_record_evidence(payload: dict[str, Any], fallback: dict[str, Any], evidence_source: str) -> dict[str, Any]:
    """Prune model items whose evidence cannot be traced verbatim to the source."""
    sanitized = dict(payload)
    pruned = False
    for collection in ("decisions", "action_items", "risks"):
        items = payload.get(collection, [])
        kept = [
            item
            for item in items
            if isinstance(item, dict) and _quote_in_source(str(item.get("evidence", "")), evidence_source)
        ]
        pruned = pruned or len(kept) != len(items)
        sanitized[collection] = kept
    refs = [
        ref
        for ref in payload.get("evidence_refs", [])
        if isinstance(ref, dict)
        and str(ref.get("report_id")) == str(fallback["report_id"])
        and _quote_in_source(str(ref.get("quote", "")), evidence_source)
    ]
    if not refs:
        refs = fallback.get("evidence_refs", [])
        pruned = True
    sanitized["evidence_refs"] = refs
    if pruned:
        sanitized["normalization_flags"] = sorted(set(sanitized.get("normalization_flags", []) + ["untraceable_evidence_pruned"]))
    return sanitized


def validate_events(payload: dict[str, Any], valid_ids: set[str]) -> list[str]:
    errors = []
    if payload.get("schema_version") != EVENTS_SCHEMA:
        errors.append("invalid events schema_version")
    events = payload.get("events")
    if not isinstance(events, list):
        return errors + ["events must be a list"]
    for index, event in enumerate(events):
        for key in ("event_id", "event_title", "event_type", "status", "priority", "merged_summary", "why_it_matters"):
            if key not in event or event.get(key) in (None, ""):
                errors.append(f"event {index} missing {key}")
        ids = {str(item) for item in event.get("record_ids", [])}
        if not ids or not ids.issubset(valid_ids):
            errors.append(f"event {index} has invalid record_ids")
        if event.get("status") not in {"new", "continuing", "updated", "blocked", "closed", "unknown"}:
            errors.append(f"event {index} has invalid status")
        if event.get("priority") not in {"P0", "P1", "P2", "FYI"}:
            errors.append(f"event {index} has invalid priority")
    return errors


def validate_priorities(payload: dict[str, Any], valid_ids: set[str]) -> list[str]:
    errors = []
    if payload.get("schema_version") != PRIORITIES_SCHEMA:
        errors.append("invalid priorities schema_version")
    priorities = payload.get("priorities")
    if not isinstance(priorities, list):
        return errors + ["priorities must be a list"]
    for index, item in enumerate(priorities):
        for key in ("rank", "event_id", "title", "priority", "status", "summary", "why_it_matters"):
            if key not in item or item.get(key) in (None, ""):
                errors.append(f"priority {index} missing {key}")
        ids = {str(value) for value in item.get("record_ids", [])}
        if not ids or not ids.issubset(valid_ids):
            errors.append(f"priority {index} has invalid record_ids")
        if item.get("priority") not in {"P0", "P1", "P2", "FYI"}:
            errors.append(f"priority {index} has invalid priority")
    return errors
