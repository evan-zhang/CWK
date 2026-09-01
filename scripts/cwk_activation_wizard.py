#!/usr/bin/env python3
"""CWK 激活向导的确定性命令行。

对话由 Skill 里的 AI 负责，**判定不归 AI 管**。这个脚本拥有全部会改变
「系统是否可以自动运行」的事实：状态机迁移、两道人工确认的绑定哈希、
只读发现报告、每日执行合同与其漂移、试跑门禁、以及调度交接与回执。

安全姿态一律 fail closed：

- 迁移表里没有的 (状态, 事件) 组合直接拒绝，不存在「大概可以」；
- 每道门的确认都绑定当时的事实哈希，事实一变确认自动失效；
- 状态文件是闭合 schema，没有任何自由文本字段，凭据与业务正文写不进去；
- 状态目录 0700、文件 0600，写入走 CAS + 原子重命名；
- 本仓库**不创建、不修改、不删除任何定时任务**，也不调用 OpenClaw/Gateway/cron
  接口。调度只产出一张交接单，由用户在宿主侧执行后回填外部任务标识。

每个子命令向 stdout 输出**一个** JSON 对象，供 Skill 直接消费。
退出码见 ``EXIT_*``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "scripts"))

from cwk_activation_contract import (  # noqa: E402
    build_discovery_report,
    build_execution_contract,
    build_scheduler_handoff,
    compute_contract_sha256,
    compute_profile_sha256,
    compute_scope_sha256,
    contract_drift,
    detect_schedule_drift,
    evaluate_pilot,
    render_contract_markdown,
    validate_schedule_receipt,
)
from cwk_activation_state import (  # noqa: E402
    ActivationContractError,
    ActivationError,
    ActivationSession,
    AuthorizationMissing,
    DEFAULT_STATE_DIR,
    GATES,
    IllegalTransition,
    ScheduleConflict,
    StateIntegrityError,
    UNINITIALIZED,
    apply_transition,
    compute_binding_sha256,
    current_binding,
    default_state,
    grant_is_valid,
    invalidate_stale_confirmations,
    new_activation_id,
    new_confirmation_id,
    next_step_for,
    session,
)
from cwk_atomic_file import (  # noqa: E402
    FILE_MODE,
    AtomicFileError,
    LockUnavailable,
    write_atomic,
)

# ── 退出码 ──────────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_USAGE = 2
"""用法错误、输入缺失、schema 违约、状态文件损坏。"""
EXIT_REFUSED = 3
"""非法迁移或缺少有效的人工确认。"""
EXIT_PILOT_FAILED = 4
"""试跑判定为 FAIL；状态已落到 DEGRADED。"""
EXIT_DRIFT = 5
"""检测到合同或调度漂移；确认已作废。"""
EXIT_SCHEDULE_CONFLICT = 8
"""调度回执与交接单/合同对不上。"""

# ── 产物文件名（固定，全部落在 0700 状态目录内）─────────────────────────────

DISCOVERY_REPORT_FILE = "discovery-report.json"
EXECUTION_CONTRACT_FILE = "execution-contract.json"
EXECUTION_CONTRACT_MD_FILE = "execution-contract.md"
PILOT_RECEIPT_FILE = "pilot-receipt.json"
SCHEDULER_HANDOFF_FILE = "scheduler-handoff.json"

DEFAULT_RUN_AT_LOCAL = "02:30"
DEFAULT_TIMEZONE = "Asia/Shanghai"

_RUN_AT = re.compile(r"\A(?:[01]\d|2[0-3]):[0-5]\d\Z")
_TIMEZONE = re.compile(r"\A[A-Za-z][A-Za-z0-9+_-]{0,31}(?:/[A-Za-z0-9+_-]{1,32}){0,2}\Z")
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class WizardError(ActivationError):
    """CLI 层的输入错误。"""

    exit_code = EXIT_USAGE


# ── 小工具 ──────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_now(value: Optional[str]) -> str:
    if value is None:
        return utc_now()
    if not _TIMESTAMP.match(value):
        raise WizardError("--now must look like 2026-09-02T01:23:45Z")
    return value


def _require_json(path: Optional[str], label: str) -> dict:
    if not path:
        raise WizardError(f"{label} is required")
    resolved = Path(path)
    if not resolved.is_file():
        raise WizardError(f"{label} not found: {resolved}")
    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WizardError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise WizardError(f"{label} must be a JSON object")
    return parsed


def _optional_json(path: Optional[str], label: str) -> Optional[dict]:
    if not path:
        return None
    return _require_json(path, label)


def _schedule_intent(config: dict) -> tuple[str, str]:
    """执行合同里的调度意图**只**来自配置文件。

    这样合同就是「配置 + 环境」的纯函数，漂移检测才有意义——否则换个命令行
    参数就能让同一份配置算出不同摘要。
    """

    run_at = config.get("schedule_run_at_local", DEFAULT_RUN_AT_LOCAL)
    tz = config.get("schedule_timezone", DEFAULT_TIMEZONE)
    if not isinstance(run_at, str) or not _RUN_AT.match(run_at):
        raise WizardError("config.schedule_run_at_local must be HH:MM (24h)")
    if not isinstance(tz, str) or not _TIMEZONE.match(tz):
        raise WizardError("config.schedule_timezone looks invalid")
    return run_at, tz


def _write_artifact(dir_fd: int, name: str, payload: Any) -> str:
    if isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
    receipt = write_atomic(dir_fd, name, data, mode=FILE_MODE)
    return receipt.sha256


def _read_artifact(state_dir: Path, name: str, label: str) -> dict:
    path = Path(state_dir) / name
    if not path.is_file():
        raise WizardError(f"{label} not found; run the previous step first ({path})")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WizardError(f"{label} is corrupt: {exc}") from exc
    if not isinstance(parsed, dict):
        raise WizardError(f"{label} must be a JSON object")
    return parsed


def _grant(state: dict, gate: str, now: str) -> dict:
    """给某道门盖章。绑定哈希由当前事实重算，AI 无法自带。"""

    binding = current_binding(state, gate)
    if binding is None:
        raise AuthorizationMissing(
            f"gate {gate!r} cannot be confirmed yet: its bound facts are incomplete"
        )
    confirmation = {
        "confirmation_id": new_confirmation_id(),
        "gate": gate,
        "bound_sha256": binding,
        "granted_at": now,
    }
    state["confirmations"][gate] = confirmation
    return confirmation


def _require_grant(state: dict, gate: str) -> None:
    if not grant_is_valid(state, gate):
        raise AuthorizationMissing(
            f"no valid human confirmation for gate {gate!r}; "
            "the facts it was bound to have changed or it was never granted"
        )


def _gate_report(state: dict) -> dict:
    return {
        gate: {
            "granted": state["confirmations"].get(gate) is not None,
            "valid": grant_is_valid(state, gate),
        }
        for gate in GATES
    }


def _snapshot(state: dict, **extra: Any) -> dict:
    payload = {
        "activation_id": state["activation_id"],
        "state": state["state"],
        "revision": state["revision"],
        "next_step": next_step_for(state),
        "confirmations": _gate_report(state),
        "degraded_reason_code": state["degraded_reason_code"],
        "schedule": state["schedule"],
        "hashes": {
            name: state[name]
            for name in (
                "discovery_scope_sha256",
                "discovery_receipt_sha256",
                "profile_sha256",
                "contract_sha256",
                "pilot_receipt_sha256",
                "schedule_handoff_sha256",
            )
        },
    }
    payload.update(extra)
    return payload


def _prepare(sess: ActivationSession) -> tuple[dict, list[str]]:
    """载入健康状态，并顺手作废与当前事实对不上的确认。"""

    state = sess.require_healthy()
    dropped = invalidate_stale_confirmations(state)
    return state, dropped


# ── 子命令 ──────────────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    with session(args.state_dir, create=True) as sess:
        if sess.integrity_reason is not None:
            raise StateIntegrityError(
                f"activation state is unusable ({sess.integrity_reason}); "
                "the file was left untouched for forensics"
            )
        if sess.state is not None:
            return EXIT_OK, _snapshot(sess.state, created=False, already_initialised=True)
        state = default_state(activation_id=new_activation_id(), now=now)
        sess.state = state
        sess.commit()
        return EXIT_OK, _snapshot(state, created=True, already_initialised=False)


def cmd_status(args: argparse.Namespace) -> tuple[int, dict]:
    with session(args.state_dir) as sess:
        if sess.integrity_reason is not None:
            return EXIT_USAGE, {
                "state": UNINITIALIZED,
                "healthy": False,
                "integrity_reason": sess.integrity_reason,
                "note": "state file left untouched for forensics; nothing was repaired",
            }
        if sess.state is None:
            return EXIT_OK, {
                "state": UNINITIALIZED,
                "healthy": True,
                "next_step": "init",
            }
        state = sess.state
        dropped = invalidate_stale_confirmations(state)
        if dropped:
            sess.commit()
        return EXIT_OK, _snapshot(state, healthy=True, invalidated_gates=dropped)


def cmd_confirm_discovery(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    scope = _require_json(args.scope_file, "--scope-file")
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        state["discovery_scope_sha256"] = compute_scope_sha256(scope)
        invalidate_stale_confirmations(state)
        confirmation = _grant(state, "discovery", now)
        apply_transition(
            state,
            event="confirm-discovery",
            now=now,
            authorization="human_confirmation_discovery",
            input_receipt_sha256=confirmation["bound_sha256"],
            next_step="run_discovery",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=dropped,
            confirmed_gate="discovery",
            bound_sha256=confirmation["bound_sha256"],
        )


def cmd_record_discovery(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    scope = _require_json(args.scope_file, "--scope-file")
    collect = _optional_json(args.collect_manifest, "--collect-manifest")
    nightly = _optional_json(args.nightly_manifest, "--nightly-manifest")
    acceptance = _optional_json(args.acceptance, "--acceptance")
    catalog = _optional_json(args.entity_catalog, "--entity-catalog")
    registry = _optional_json(args.entity_registry, "--entity-registry")

    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        _require_grant(state, "discovery")
        if compute_scope_sha256(scope) != state["discovery_scope_sha256"]:
            raise AuthorizationMissing(
                "the scope passed to record-discovery is not the scope the user confirmed"
            )
        report = build_discovery_report(
            scope=scope,
            collect_manifest=collect,
            nightly_manifest=nightly,
            acceptance=acceptance,
            entity_catalog=catalog,
            entity_registry=registry,
            generated_at=now,
        )
        _write_artifact(sess.dir_fd, DISCOVERY_REPORT_FILE, report)
        state["discovery_receipt_sha256"] = report["report_sha256"]
        invalidate_stale_confirmations(state)
        apply_transition(
            state,
            event="record-discovery",
            now=now,
            authorization="system_receipt",
            input_receipt_sha256=report["report_sha256"],
            next_step="propose_profile",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=dropped,
            discovery_report=report,
            artifact=DISCOVERY_REPORT_FILE,
        )


def cmd_propose_profile(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    profile = _require_json(args.profile_file, "--profile-file")
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        if state["discovery_receipt_sha256"] is None:
            raise IllegalTransition("cannot propose a profile before discovery has been recorded")
        state["profile_sha256"] = compute_profile_sha256(profile)
        invalidate_stale_confirmations(state)
        apply_transition(
            state,
            event="propose-profile",
            now=now,
            authorization="system_receipt",
            input_receipt_sha256=state["profile_sha256"],
            next_step="confirm_profile",
        )
        sess.commit()
        return EXIT_OK, _snapshot(state, invalidated_gates=dropped)


def cmd_confirm_profile(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        confirmation = _grant(state, "profile", now)
        apply_transition(
            state,
            event="confirm-profile",
            now=now,
            authorization="human_confirmation_profile",
            input_receipt_sha256=confirmation["bound_sha256"],
            next_step="run_pilot",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=dropped,
            confirmed_gate="profile",
            bound_sha256=confirmation["bound_sha256"],
        )


def cmd_render_contract(args: argparse.Namespace) -> tuple[int, dict]:
    """只读：把每日执行合同复述成人话，不改状态。"""

    now = _resolve_now(args.now)
    config = _require_json(args.config, "--config")
    run_at, tz = _schedule_intent(config)
    with session(args.state_dir) as sess:
        state = sess.require_healthy()
        if state["profile_sha256"] is None:
            raise IllegalTransition("cannot render a contract before a profile exists")
        contract = build_execution_contract(
            config=config,
            env=os.environ,
            profile_sha256=state["profile_sha256"],
            run_at_local=run_at,
            timezone=tz,
            generated_at=now,
        )
        drift = contract_drift(contract, state["contract_sha256"])
        return EXIT_OK, _snapshot(
            state,
            contract=contract,
            contract_markdown=render_contract_markdown(contract),
            drift=drift,
        )


def cmd_record_pilot(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    config = _require_json(args.config, "--config")
    run_at, tz = _schedule_intent(config)
    nightly = _optional_json(args.nightly_manifest, "--nightly-manifest")
    acceptance = _optional_json(args.acceptance, "--acceptance")
    collect = _optional_json(args.collect_manifest, "--collect-manifest")

    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        _require_grant(state, "profile")
        contract = build_execution_contract(
            config=config,
            env=os.environ,
            profile_sha256=state["profile_sha256"],
            run_at_local=run_at,
            timezone=tz,
            generated_at=now,
        )
        contract_sha = contract["contract_sha256"]
        receipt = evaluate_pilot(
            nightly_manifest=nightly,
            acceptance=acceptance,
            collect_manifest=collect,
            bound_contract_sha256=contract_sha,
            generated_at=now,
        )
        _write_artifact(sess.dir_fd, EXECUTION_CONTRACT_FILE, contract)
        _write_artifact(sess.dir_fd, EXECUTION_CONTRACT_MD_FILE, render_contract_markdown(contract))
        _write_artifact(sess.dir_fd, PILOT_RECEIPT_FILE, receipt)

        state["contract_sha256"] = contract_sha
        state["pilot_receipt_sha256"] = receipt["receipt_sha256"]
        invalidate_stale_confirmations(state)

        passed = receipt["result"] == "PASS"
        if passed:
            state["degraded_reason_code"] = None
            apply_transition(
                state,
                event="record-pilot-pass",
                now=now,
                authorization="system_receipt",
                input_receipt_sha256=receipt["receipt_sha256"],
                next_step="confirm_activation",
            )
        else:
            state["degraded_reason_code"] = "pilot_failed"
            apply_transition(
                state,
                event="record-pilot-fail",
                now=now,
                authorization="system_receipt",
                input_receipt_sha256=receipt["receipt_sha256"],
                next_step="rerun_pilot",
            )
        sess.commit()
        payload = _snapshot(
            state,
            invalidated_gates=dropped,
            pilot_receipt=receipt,
            contract_sha256=contract_sha,
        )
        return (EXIT_OK if passed else EXIT_PILOT_FAILED), payload


def cmd_confirm_activation(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        confirmation = _grant(state, "activation", now)
        apply_transition(
            state,
            event="confirm-activation",
            now=now,
            authorization="human_confirmation_activation",
            input_receipt_sha256=confirmation["bound_sha256"],
            next_step="emit_scheduler_handoff",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=dropped,
            confirmed_gate="activation",
            bound_sha256=confirmation["bound_sha256"],
        )


def cmd_schedule_handoff(args: argparse.Namespace) -> tuple[int, dict]:
    """产出交接单。**本仓库不创建任何定时任务。**"""

    now = _resolve_now(args.now)
    config = _require_json(args.config, "--config")
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        _require_grant(state, "activation")
        if state["state"] != "PILOT_PASSED":
            raise IllegalTransition(
                f"scheduler handoff requires PILOT_PASSED, current state is {state['state']}"
            )
        contract = _read_artifact(args.state_dir, EXECUTION_CONTRACT_FILE, "execution contract")
        if compute_contract_sha256(contract) != state["contract_sha256"]:
            raise IllegalTransition(
                "stored execution contract does not match the confirmed contract hash"
            )
        handoff = build_scheduler_handoff(
            contract=contract,
            contract_sha256=state["contract_sha256"],
            profile_sha256=state["profile_sha256"],
            pilot_receipt_sha256=state["pilot_receipt_sha256"],
            config_path=str(args.config),
            generated_at=now,
        )
        _write_artifact(sess.dir_fd, SCHEDULER_HANDOFF_FILE, handoff)
        # 产出交接单不是状态迁移（仍停在 PILOT_PASSED），所以只推进 revision，
        # 不写 history。这一步的可审计性由交接单自身的哈希承担：它落进
        # schedule_handoff_sha256，并在随后的 record-schedule 里作为
        # input_receipt_sha256 进入历史。
        state["schedule_handoff_sha256"] = handoff["handoff_sha256"]
        state["revision"] = int(state["revision"]) + 1
        state["updated_at"] = now
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=dropped,
            handoff=handoff,
            artifact=SCHEDULER_HANDOFF_FILE,
            repository_created_a_task=False,
        )


def cmd_record_schedule(args: argparse.Namespace) -> tuple[int, dict]:
    """记录用户在宿主侧**自己**建好的任务。这里只校验，不创建。"""

    now = _resolve_now(args.now)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        _require_grant(state, "activation")
        handoff = _read_artifact(args.state_dir, SCHEDULER_HANDOFF_FILE, "scheduler handoff")
        if handoff.get("handoff_sha256") != state["schedule_handoff_sha256"]:
            raise ScheduleConflict("stored handoff does not match the one recorded in state")
        check = validate_schedule_receipt(
            handoff=handoff,
            contract_sha256=state["contract_sha256"],
            external_task_id=args.external_task_id,
        )
        if not check["ok"]:
            raise ScheduleConflict(f"schedule receipt rejected: {check['problems']}")
        state["schedule"] = {
            "external_system": args.external_system,
            "external_task_id": args.external_task_id,
            "bound_contract_sha256": state["contract_sha256"],
            "handoff_sha256": state["schedule_handoff_sha256"],
            "recorded_at": now,
            "status": "enabled",
        }
        apply_transition(
            state,
            event="record-schedule",
            now=now,
            authorization="human_confirmation_activation",
            input_receipt_sha256=state["schedule_handoff_sha256"],
            next_step="none",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state, invalidated_gates=dropped, repository_created_a_task=False
        )


def _simple_transition(args: argparse.Namespace, event: str, next_step: str) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        if event == "resume":
            _require_grant(state, "activation")
        if state["schedule"] is not None:
            state["schedule"]["status"] = "paused" if event == "pause" else "enabled"
        apply_transition(
            state,
            event=event,
            now=now,
            authorization="human_confirmation_activation",
            next_step=next_step,
        )
        sess.commit()
        return EXIT_OK, _snapshot(state, invalidated_gates=dropped)


def cmd_pause(args: argparse.Namespace) -> tuple[int, dict]:
    return _simple_transition(args, "pause", "resume_or_reconfirm")


def cmd_resume(args: argparse.Namespace) -> tuple[int, dict]:
    return _simple_transition(args, "resume", "none")


def cmd_check_drift(args: argparse.Namespace) -> tuple[int, dict]:
    """比对当前配置算出的合同与状态记录，并核对外部调度标识。

    发现未知任务只如实报告，绝不代替用户删除或覆盖。
    """

    now = _resolve_now(args.now)
    config = _require_json(args.config, "--config")
    run_at, tz = _schedule_intent(config)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        if state["profile_sha256"] is None:
            raise IllegalTransition("cannot check drift before a profile exists")
        contract = build_execution_contract(
            config=config,
            env=os.environ,
            profile_sha256=state["profile_sha256"],
            run_at_local=run_at,
            timezone=tz,
            generated_at=now,
        )
        drift = contract_drift(contract, state["contract_sha256"])
        schedule = detect_schedule_drift(
            state_schedule=state["schedule"],
            observed_task_id=args.observed_task_id,
            contract_sha256=state["contract_sha256"] or "",
        )
        drifted = bool(drift["drifted"] or schedule["drifted"])
        flagged = False
        if drifted:
            state["degraded_reason_code"] = (
                "contract_drift" if drift["drifted"] else "schedule_id_unknown"
            )
            try:
                apply_transition(
                    state,
                    event="flag-drift",
                    now=now,
                    authorization="system_receipt",
                    input_receipt_sha256=drift["current_contract_sha256"],
                    next_step="reconfirm_contract",
                )
                flagged = True
            except IllegalTransition:
                # 已经在 DEGRADED / NEEDS_RECONFIRMATION 这类状态里，无需再降级。
                flagged = False
            sess.commit()
        elif dropped:
            sess.commit()
        return (EXIT_DRIFT if drifted else EXIT_OK), _snapshot(
            state,
            invalidated_gates=dropped,
            contract_drift=drift,
            schedule_drift=schedule,
            flagged=flagged,
            destructive_action_taken=False,
        )


# ── 参数解析 ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cwk_activation_wizard",
        description="CWK 激活向导的确定性后端（状态机 / 确认 / 合同 / 试跑 / 调度交接）",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="激活状态目录（默认 state/activation，0700）",
    )
    parser.add_argument("--now", default=None, help="固定时间戳（用于可复现测试）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化激活状态").set_defaults(func=cmd_init)
    sub.add_parser("status", help="查看当前状态与下一步").set_defaults(func=cmd_status)

    p = sub.add_parser("confirm-discovery", help="第一道人工确认：授权只读发现")
    p.add_argument("--scope-file", required=True, help="授权可见范围的 JSON")
    p.set_defaults(func=cmd_confirm_discovery)

    p = sub.add_parser("record-discovery", help="用既有只读回执生成发现报告")
    p.add_argument("--scope-file", required=True)
    p.add_argument("--collect-manifest")
    p.add_argument("--nightly-manifest")
    p.add_argument("--acceptance")
    p.add_argument("--entity-catalog")
    p.add_argument("--entity-registry")
    p.set_defaults(func=cmd_record_discovery)

    p = sub.add_parser("propose-profile", help="提交业务画像草案（草案不是结论）")
    p.add_argument("--profile-file", required=True)
    p.set_defaults(func=cmd_propose_profile)

    sub.add_parser("confirm-profile", help="用户认领业务画像").set_defaults(
        func=cmd_confirm_profile
    )

    p = sub.add_parser("render-contract", help="只读复述每日执行合同")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_render_contract)

    p = sub.add_parser("record-pilot", help="判定一次只读试跑")
    p.add_argument("--config", required=True)
    p.add_argument("--nightly-manifest")
    p.add_argument("--acceptance")
    p.add_argument("--collect-manifest")
    p.set_defaults(func=cmd_record_pilot)

    sub.add_parser(
        "confirm-activation", help="第二道人工确认：接受试跑结果并允许排期"
    ).set_defaults(func=cmd_confirm_activation)

    p = sub.add_parser("schedule-handoff", help="产出调度交接单（本仓库不建任务）")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_schedule_handoff)

    p = sub.add_parser("record-schedule", help="登记宿主侧已建好的任务标识")
    p.add_argument("--external-system", required=True, help="例如 openclaw / launchd / cron")
    p.add_argument("--external-task-id", required=True)
    p.set_defaults(func=cmd_record_schedule)

    sub.add_parser("pause", help="暂停自动运行").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="恢复自动运行").set_defaults(func=cmd_resume)

    p = sub.add_parser("check-drift", help="检查合同与调度漂移")
    p.add_argument("--config", required=True)
    p.add_argument("--observed-task-id", default=None)
    p.set_defaults(func=cmd_check_drift)

    return parser


_EXIT_FOR_EXCEPTION: tuple[tuple[type, int], ...] = (
    (ScheduleConflict, EXIT_SCHEDULE_CONFLICT),
    (AuthorizationMissing, EXIT_REFUSED),
    (IllegalTransition, EXIT_REFUSED),
    (StateIntegrityError, EXIT_USAGE),
    (ActivationContractError, EXIT_USAGE),
    (WizardError, EXIT_USAGE),
    (ActivationError, EXIT_USAGE),
)


def _exit_code_for(exc: Exception) -> int:
    for kind, code in _EXIT_FOR_EXCEPTION:
        if isinstance(exc, kind):
            return code
    return EXIT_USAGE


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload = args.func(args)
    except LockUnavailable:
        payload = {
            "ok": False,
            "error": "another activation command is already running (state dir is locked)",
            "error_kind": "LockUnavailable",
        }
        code = EXIT_USAGE
    except AtomicFileError as exc:
        # CAS 冲突 / 容器越界等：一律不重试、不覆盖，交回给调用方。
        payload = {"ok": False, "error": str(exc), "error_kind": type(exc).__name__}
        code = EXIT_USAGE
    except ActivationError as exc:
        code = _exit_code_for(exc)
        payload = {"ok": False, "error": str(exc), "error_kind": type(exc).__name__}
    else:
        payload = {"ok": code == EXIT_OK, "command": args.command, **payload}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
