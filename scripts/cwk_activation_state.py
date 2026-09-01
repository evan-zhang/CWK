#!/usr/bin/env python3
"""RT-032 激活状态机与私有持久化（确定性层）。

本模块只负责「现在处于哪个激活阶段、谁授权过什么、下一步是什么」。
它不读取 CWork、不访问网络、不创建任何计划任务，也不解释自然语言。
对话由 Skill/AI 负责；授权记录只能由本模块写入，且必须与被确认对象的
哈希绑定，因此一次自然语言对话本身永远不构成授权。

安全不变量（由代码强制，不是文档约定）：

1. **闭合 schema**：状态里每一个字符串叶子都必须命中枚举、64 位十六进制摘要、
   RFC3339 UTC 秒级时间戳或一条固定 ID 正则。没有任何自由文本字段，
   因此凭据、原文正文、业务语句在结构上无法被写进状态文件。
2. **确认与哈希绑定**：确认记录保存的是 `bound_sha256`，由 `gate` 域分隔后
   对「被确认的具体对象」求哈希。`gate` 进入原像，所以第一道确认的哈希
   在密码学上无法充当第二道确认——两道门不可互相顶替。
3. **漂移即失效**：配置、画像或执行合同一变，重算出的绑定哈希就对不上，
   已有确认自动作废，只能进入 NEEDS_RECONFIRMATION。
4. **fail closed**：非法跳转、缺确认、状态文件损坏或被手工篡改时一律拒绝，
   绝不「尽力而为」地继续。

持久化默认落在仓库私有目录 `state/activation/`（`/state/` 已在 .gitignore），
目录 0700、文件 0600，写入走 `cwk_atomic_file` 的原子写与 compare-and-swap，
并在同一把排他锁下完成，避免两个并发向导互相覆盖决定。

Refs: RT-032
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "scripts"))

from cwk_atomic_file import (  # noqa: E402
    DIRECTORY_MODE,
    FILE_MODE,
    cas_write,
    child_exists,
    exclusive_lock,
    open_dir_nofollow,
    read_file,
    recover_orphans,
)
from cwk_pr001_contracts import canonical_json_bytes  # noqa: E402

# ── 常量 ────────────────────────────────────────────────────────────────────

ACTIVATION_STATE_SCHEMA = "cwk.activation_state.v1"

STATE_FILE = "activation.json"
LOCK_FILE = "activation.lock"
DEFAULT_STATE_DIR = _PROJECT / "state" / "activation"

MAX_HISTORY = 512
MAX_STRING_LEN = 128

UNINITIALIZED = "UNINITIALIZED"

STATES = (
    "INSTALLED",
    "READY_FOR_DISCOVERY",
    "PROFILE_PROPOSED",
    "PROFILE_CONFIRMED",
    "PILOT_PASSED",
    "ACTIVE",
    "PAUSED",
    "DEGRADED",
    "NEEDS_RECONFIRMATION",
)

GATES = ("discovery", "profile", "activation")

# 每道门的绑定原像字段。gate 本身也进原像，所以跨门替换不可能。
GATE_BINDING_FIELDS = {
    "discovery": ("discovery_scope_sha256",),
    "profile": ("discovery_receipt_sha256", "profile_sha256"),
    "activation": ("contract_sha256", "profile_sha256", "pilot_receipt_sha256"),
}

EVENTS = (
    "init",
    "confirm-discovery",
    "record-discovery",
    "propose-profile",
    "confirm-profile",
    "record-pilot-pass",
    "record-pilot-fail",
    "confirm-activation",
    "record-schedule",
    "pause",
    "resume",
    "flag-drift",
)

AUTHORIZATIONS = (
    "none",
    "human_confirmation_discovery",
    "human_confirmation_profile",
    "human_confirmation_activation",
    "system_receipt",
)

NEXT_STEPS = (
    "confirm_discovery_scope",
    "run_discovery",
    "propose_profile",
    "confirm_profile",
    "run_pilot",
    "confirm_activation",
    "emit_scheduler_handoff",
    "record_external_schedule",
    "rerun_pilot",
    "reconfirm_contract",
    "resume_or_reconfirm",
    "none",
)

DEGRADED_REASON_CODES = (
    "pilot_failed",
    "contract_drift",
    "schedule_id_unknown",
    "state_unparseable",
    "state_schema_unknown",
    "state_schema_invalid",
)

SCHEDULE_STATUSES = ("enabled", "paused")

# (当前状态, 事件) -> 目标状态。表里没有的组合一律非法。
TRANSITIONS: dict[tuple[str, str], str] = {
    (UNINITIALIZED, "init"): "INSTALLED",
    ("INSTALLED", "confirm-discovery"): "READY_FOR_DISCOVERY",
    ("READY_FOR_DISCOVERY", "record-discovery"): "READY_FOR_DISCOVERY",
    ("READY_FOR_DISCOVERY", "propose-profile"): "PROFILE_PROPOSED",
    ("PROFILE_PROPOSED", "propose-profile"): "PROFILE_PROPOSED",
    ("PROFILE_PROPOSED", "confirm-profile"): "PROFILE_CONFIRMED",
    ("PROFILE_CONFIRMED", "record-pilot-pass"): "PILOT_PASSED",
    ("PROFILE_CONFIRMED", "record-pilot-fail"): "DEGRADED",
    ("PROFILE_CONFIRMED", "flag-drift"): "NEEDS_RECONFIRMATION",
    ("PILOT_PASSED", "confirm-activation"): "PILOT_PASSED",
    # 重跑一次通过的试跑是允许的，但它产出新的回执，因而作废旧的第二道确认。
    ("PILOT_PASSED", "record-pilot-pass"): "PILOT_PASSED",
    ("PILOT_PASSED", "record-schedule"): "ACTIVE",
    ("PILOT_PASSED", "record-pilot-fail"): "DEGRADED",
    ("PILOT_PASSED", "flag-drift"): "NEEDS_RECONFIRMATION",
    ("ACTIVE", "pause"): "PAUSED",
    ("ACTIVE", "flag-drift"): "NEEDS_RECONFIRMATION",
    ("PAUSED", "resume"): "ACTIVE",
    ("PAUSED", "flag-drift"): "NEEDS_RECONFIRMATION",
    ("DEGRADED", "record-pilot-pass"): "PILOT_PASSED",
    ("DEGRADED", "record-pilot-fail"): "DEGRADED",
    ("NEEDS_RECONFIRMATION", "propose-profile"): "PROFILE_PROPOSED",
    ("NEEDS_RECONFIRMATION", "record-pilot-pass"): "PILOT_PASSED",
    ("NEEDS_RECONFIRMATION", "record-pilot-fail"): "DEGRADED",
}

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_ACTIVATION_ID = re.compile(r"\Aact_[0-9a-f]{32}\Z")
_CONFIRMATION_ID = re.compile(r"\Acnf_[0-9a-f]{32}\Z")
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_EXTERNAL_TASK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_EXTERNAL_SYSTEM = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")

_BINDING_DOMAIN = b"cwk-activation-binding-v1\x00"


# ── 异常 ────────────────────────────────────────────────────────────────────


class ActivationError(Exception):
    """本模块所有失败的基类。"""

    exit_code = 2


class ActivationContractError(ActivationError):
    """状态文件不符合闭合 schema。"""

    exit_code = 2


class IllegalTransition(ActivationError):
    """(状态, 事件) 不在迁移表里。"""

    exit_code = 3


class AuthorizationMissing(ActivationError):
    """缺少与当前对象绑定的有效确认。"""

    exit_code = 3


class ScheduleConflict(ActivationError):
    """外部调度任务标识冲突或未知。"""

    exit_code = 8


class StateIntegrityError(ActivationError):
    """状态文件损坏、被手工改写或 schema 未知。"""

    exit_code = 2


# ── 绑定哈希 ────────────────────────────────────────────────────────────────


def compute_binding_sha256(*, gate: str, activation_id: str, **fields: str) -> str:
    """算出「这次确认到底确认了什么」的绑定哈希。

    ``gate`` 进入原像做域分隔，因此 discovery 门的哈希不可能等于 activation
    门的哈希——两道确认的隔离是密码学的，不是策略上的。
    ``activation_id`` 进入原像，因此确认无法跨安装复用。
    """

    if gate not in GATE_BINDING_FIELDS:
        raise ActivationContractError(f"unknown gate: {gate!r}")
    required = set(GATE_BINDING_FIELDS[gate])
    got = set(fields)
    if got != required:
        missing = sorted(required - got)
        extra = sorted(got - required)
        raise ActivationContractError(
            f"gate {gate!r} binding fields mismatch missing={missing} extra={extra}"
        )
    if not _ACTIVATION_ID.match(activation_id or ""):
        raise ActivationContractError("invalid activation_id")
    for name, value in fields.items():
        if not isinstance(value, str) or not _HEX64.match(value):
            raise ActivationContractError(f"binding field {name} must be sha256 hex")
    body = {"activation_id": activation_id, "gate": gate}
    body.update(fields)
    return hashlib.sha256(_BINDING_DOMAIN + canonical_json_bytes(body)).hexdigest()


def current_binding(state: dict, gate: str) -> Optional[str]:
    """按状态里的当前事实重算某道门的绑定哈希；缺料则返回 None。"""

    if gate not in GATE_BINDING_FIELDS:
        raise ActivationContractError(f"unknown gate: {gate!r}")
    fields = {name: state.get(name) for name in GATE_BINDING_FIELDS[gate]}
    if any(not isinstance(v, str) for v in fields.values()):
        return None
    return compute_binding_sha256(
        gate=gate, activation_id=state["activation_id"], **fields
    )


def grant_is_valid(state: dict, gate: str) -> bool:
    """该门是否存在一条仍然对得上当前事实的确认。"""

    confirmation = (state.get("confirmations") or {}).get(gate)
    if not isinstance(confirmation, dict):
        return False
    expected = current_binding(state, gate)
    if expected is None:
        return False
    return hmac.compare_digest(str(confirmation.get("bound_sha256")), expected)


def invalidate_stale_confirmations(state: dict) -> list[str]:
    """作废所有与当前事实对不上的确认，返回被作废的门。"""

    dropped: list[str] = []
    confirmations = state.get("confirmations") or {}
    for gate in GATES:
        if confirmations.get(gate) is None:
            continue
        if not grant_is_valid(state, gate):
            confirmations[gate] = None
            dropped.append(gate)
    return dropped


# ── 闭合 schema 校验 ────────────────────────────────────────────────────────


def _require_keys(obj: Any, keys: tuple[str, ...], where: str) -> None:
    if not isinstance(obj, dict):
        raise ActivationContractError(f"{where} must be an object")
    got = set(obj)
    want = set(keys)
    if got != want:
        missing = sorted(want - got)
        extra = sorted(got - want)
        raise ActivationContractError(f"{where} keys mismatch missing={missing} extra={extra}")


def _check_enum(value: Any, allowed: tuple[str, ...], where: str) -> None:
    if value not in allowed:
        raise ActivationContractError(f"{where} must be one of {allowed}, got {value!r}")


def _check_opt_hex(value: Any, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _HEX64.match(value):
        raise ActivationContractError(f"{where} must be null or sha256 hex")


def _check_timestamp(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        raise ActivationContractError(f"{where} must be RFC3339 UTC seconds (…Z)")


def _check_no_long_strings(node: Any, where: str) -> None:
    """兜底：任何字符串叶子都不得超长或含控制字符。

    闭合 schema 已经逐字段限死取值，这里是第二道防线——即使将来有人加字段，
    也无法把一段业务正文或长凭据塞进状态文件。
    """

    if isinstance(node, str):
        if len(node) > MAX_STRING_LEN:
            raise ActivationContractError(f"{where}: string exceeds {MAX_STRING_LEN} chars")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in node):
            raise ActivationContractError(f"{where}: control characters are not allowed")
    elif isinstance(node, dict):
        for key, value in node.items():
            _check_no_long_strings(key, f"{where}.<key>")
            _check_no_long_strings(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_no_long_strings(value, f"{where}[{index}]")


_STATE_KEYS = (
    "schema",
    "activation_id",
    "state",
    "revision",
    "created_at",
    "updated_at",
    "discovery_scope_sha256",
    "discovery_receipt_sha256",
    "profile_sha256",
    "contract_sha256",
    "pilot_receipt_sha256",
    "schedule_handoff_sha256",
    "degraded_reason_code",
    "confirmations",
    "schedule",
    "history",
)

_CONFIRMATION_KEYS = ("confirmation_id", "gate", "bound_sha256", "granted_at")

_SCHEDULE_KEYS = (
    "external_system",
    "external_task_id",
    "bound_contract_sha256",
    "handoff_sha256",
    "recorded_at",
    "status",
)

_HISTORY_KEYS = (
    "at",
    "from_state",
    "to_state",
    "event",
    "authorization",
    "input_receipt_sha256",
    "next_step",
    "revision",
)


def validate_state(state: Any) -> dict:
    """闭合校验。任何不认识的字段、类型或取值都直接判失败。"""

    _require_keys(state, _STATE_KEYS, "state")
    if state["schema"] != ACTIVATION_STATE_SCHEMA:
        raise ActivationContractError(
            f"schema must be {ACTIVATION_STATE_SCHEMA}, got {state['schema']!r}"
        )
    if not isinstance(state["activation_id"], str) or not _ACTIVATION_ID.match(
        state["activation_id"]
    ):
        raise ActivationContractError("activation_id must match act_<32 hex>")
    _check_enum(state["state"], STATES, "state.state")
    if not isinstance(state["revision"], int) or isinstance(state["revision"], bool):
        raise ActivationContractError("revision must be an int")
    if state["revision"] < 1:
        raise ActivationContractError("revision must be >= 1")
    _check_timestamp(state["created_at"], "state.created_at")
    _check_timestamp(state["updated_at"], "state.updated_at")
    for name in (
        "discovery_scope_sha256",
        "discovery_receipt_sha256",
        "profile_sha256",
        "contract_sha256",
        "pilot_receipt_sha256",
        "schedule_handoff_sha256",
    ):
        _check_opt_hex(state[name], f"state.{name}")
    if state["degraded_reason_code"] is not None:
        _check_enum(
            state["degraded_reason_code"], DEGRADED_REASON_CODES, "state.degraded_reason_code"
        )

    _require_keys(state["confirmations"], GATES, "state.confirmations")
    for gate in GATES:
        confirmation = state["confirmations"][gate]
        if confirmation is None:
            continue
        _require_keys(confirmation, _CONFIRMATION_KEYS, f"confirmations.{gate}")
        if not isinstance(confirmation["confirmation_id"], str) or not _CONFIRMATION_ID.match(
            confirmation["confirmation_id"]
        ):
            raise ActivationContractError(f"confirmations.{gate}.confirmation_id invalid")
        if confirmation["gate"] != gate:
            raise ActivationContractError(f"confirmations.{gate}.gate mismatch")
        _check_opt_hex(confirmation["bound_sha256"], f"confirmations.{gate}.bound_sha256")
        if confirmation["bound_sha256"] is None:
            raise ActivationContractError(f"confirmations.{gate}.bound_sha256 required")
        _check_timestamp(confirmation["granted_at"], f"confirmations.{gate}.granted_at")

    schedule = state["schedule"]
    if schedule is not None:
        _require_keys(schedule, _SCHEDULE_KEYS, "state.schedule")
        if not isinstance(schedule["external_system"], str) or not _EXTERNAL_SYSTEM.match(
            schedule["external_system"]
        ):
            raise ActivationContractError("schedule.external_system invalid")
        if not isinstance(schedule["external_task_id"], str) or not _EXTERNAL_TASK_ID.match(
            schedule["external_task_id"]
        ):
            raise ActivationContractError("schedule.external_task_id invalid")
        _check_opt_hex(schedule["bound_contract_sha256"], "schedule.bound_contract_sha256")
        if schedule["bound_contract_sha256"] is None:
            raise ActivationContractError("schedule.bound_contract_sha256 required")
        _check_opt_hex(schedule["handoff_sha256"], "schedule.handoff_sha256")
        _check_timestamp(schedule["recorded_at"], "schedule.recorded_at")
        _check_enum(schedule["status"], SCHEDULE_STATUSES, "schedule.status")

    history = state["history"]
    if not isinstance(history, list) or not history:
        raise ActivationContractError("history must be a non-empty list")
    if len(history) > MAX_HISTORY:
        raise ActivationContractError(f"history exceeds {MAX_HISTORY} entries")
    for index, entry in enumerate(history):
        where = f"history[{index}]"
        _require_keys(entry, _HISTORY_KEYS, where)
        _check_timestamp(entry["at"], f"{where}.at")
        _check_enum(entry["from_state"], STATES + (UNINITIALIZED,), f"{where}.from_state")
        _check_enum(entry["to_state"], STATES, f"{where}.to_state")
        _check_enum(entry["event"], EVENTS, f"{where}.event")
        _check_enum(entry["authorization"], AUTHORIZATIONS, f"{where}.authorization")
        _check_opt_hex(entry["input_receipt_sha256"], f"{where}.input_receipt_sha256")
        _check_enum(entry["next_step"], NEXT_STEPS, f"{where}.next_step")
        if not isinstance(entry["revision"], int) or isinstance(entry["revision"], bool):
            raise ActivationContractError(f"{where}.revision must be an int")

    _check_no_long_strings(state, "state")
    return state


# ── 构造与迁移 ──────────────────────────────────────────────────────────────


def new_activation_id() -> str:
    return "act_" + secrets.token_hex(16)


def new_confirmation_id() -> str:
    return "cnf_" + secrets.token_hex(16)


def default_state(*, activation_id: str, now: str) -> dict:
    state = {
        "schema": ACTIVATION_STATE_SCHEMA,
        "activation_id": activation_id,
        "state": "INSTALLED",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "discovery_scope_sha256": None,
        "discovery_receipt_sha256": None,
        "profile_sha256": None,
        "contract_sha256": None,
        "pilot_receipt_sha256": None,
        "schedule_handoff_sha256": None,
        "degraded_reason_code": None,
        "confirmations": {gate: None for gate in GATES},
        "schedule": None,
        "history": [
            {
                "at": now,
                "from_state": UNINITIALIZED,
                "to_state": "INSTALLED",
                "event": "init",
                "authorization": "none",
                "input_receipt_sha256": None,
                "next_step": "confirm_discovery_scope",
                "revision": 1,
            }
        ],
    }
    return validate_state(state)


def next_state_for(current: str, event: str) -> str:
    """查迁移表；查不到就是非法跳转，直接 fail closed。"""

    if event not in EVENTS:
        raise IllegalTransition(f"unknown event {event!r}")
    key = (current, event)
    if key not in TRANSITIONS:
        raise IllegalTransition(
            f"illegal transition: {current} --{event}--> ? "
            f"(allowed from {current}: "
            f"{sorted(e for (s, e) in TRANSITIONS if s == current)})"
        )
    return TRANSITIONS[key]


def apply_transition(
    state: dict,
    *,
    event: str,
    now: str,
    authorization: str = "none",
    input_receipt_sha256: Optional[str] = None,
    next_step: str = "none",
) -> dict:
    """就地推进状态并追加一条迁移记录。"""

    _check_enum(authorization, AUTHORIZATIONS, "authorization")
    _check_enum(next_step, NEXT_STEPS, "next_step")
    from_state = state["state"]
    to_state = next_state_for(from_state, event)
    state["revision"] = int(state["revision"]) + 1
    state["state"] = to_state
    state["updated_at"] = now
    state["history"].append(
        {
            "at": now,
            "from_state": from_state,
            "to_state": to_state,
            "event": event,
            "authorization": authorization,
            "input_receipt_sha256": input_receipt_sha256,
            "next_step": next_step,
            "revision": state["revision"],
        }
    )
    if len(state["history"]) > MAX_HISTORY:
        del state["history"][: len(state["history"]) - MAX_HISTORY]
    return state


def next_step_for(state: dict) -> str:
    """给 AI 用的下一步提示。只回枚举 token，不回自由文本。"""

    current = state["state"]
    if current == "INSTALLED":
        return "confirm_discovery_scope"
    if current == "READY_FOR_DISCOVERY":
        return "propose_profile" if state.get("discovery_receipt_sha256") else "run_discovery"
    if current == "PROFILE_PROPOSED":
        return "confirm_profile"
    if current == "PROFILE_CONFIRMED":
        return "run_pilot"
    if current == "PILOT_PASSED":
        if not grant_is_valid(state, "activation"):
            return "confirm_activation"
        if not state.get("schedule_handoff_sha256"):
            return "emit_scheduler_handoff"
        return "record_external_schedule"
    if current == "ACTIVE":
        return "none"
    if current == "PAUSED":
        return "resume_or_reconfirm"
    if current == "DEGRADED":
        return "rerun_pilot"
    if current == "NEEDS_RECONFIRMATION":
        return "reconfirm_contract"
    return "none"


# ── 持久化 ──────────────────────────────────────────────────────────────────


def serialize_state(state: dict) -> bytes:
    """状态文件的唯一序列化方式（CAS 的哈希以它为准）。"""

    return (
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate keys in JSON object")
    return dict(pairs)


def open_state_dir(state_dir: Path | str = DEFAULT_STATE_DIR, *, create: bool = False) -> int:
    """打开状态目录并返回 dirfd；创建时使用 0700。"""

    path = Path(state_dir)
    if create:
        path.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ActivationError(f"activation state dir does not exist: {path}")
    return open_dir_nofollow(path)


def read_state(dir_fd: int) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """读取状态。

    返回 ``(state, raw_sha256, integrity_reason)``：

    - 文件不存在 → ``(None, None, None)``，等价于 UNINITIALIZED；
    - 损坏/未知 schema/校验失败 → ``(None, raw_sha256, 原因码)``，
      磁盘内容保持原样以便取证，调用方只能 fail closed。
    """

    if not child_exists(dir_fd, STATE_FILE):
        return None, None, None
    raw = read_file(dir_fd, STATE_FILE)
    raw_sha = hashlib.sha256(raw).hexdigest()
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (ValueError, UnicodeDecodeError):
        return None, raw_sha, "state_unparseable"
    if not isinstance(parsed, dict):
        return None, raw_sha, "state_unparseable"
    if parsed.get("schema") != ACTIVATION_STATE_SCHEMA:
        return None, raw_sha, "state_schema_unknown"
    try:
        validate_state(parsed)
    except ActivationContractError:
        return None, raw_sha, "state_schema_invalid"
    return parsed, raw_sha, None


def commit_state(dir_fd: int, state: dict, expected_sha256: Optional[str]) -> str:
    """校验后原子落盘；用 CAS 保证并发写不会互相覆盖。返回新的 sha256。"""

    validate_state(state)
    data = serialize_state(state)
    receipt = cas_write(
        dir_fd, STATE_FILE, data, expected_previous_sha256=expected_sha256, mode=FILE_MODE
    )
    return receipt.sha256


class ActivationSession:
    """一次「加锁 → 读 → 改 → CAS 写」的会话。"""

    def __init__(self, dir_fd: int, state: Optional[dict], raw_sha: Optional[str], reason: Optional[str]):
        self.dir_fd = dir_fd
        self.state = state
        self.raw_sha = raw_sha
        self.integrity_reason = reason

    @property
    def current_state_name(self) -> str:
        if self.state is None:
            return UNINITIALIZED
        return self.state["state"]

    def require_healthy(self) -> dict:
        if self.integrity_reason is not None:
            raise StateIntegrityError(
                f"activation state is unusable ({self.integrity_reason}); "
                "the file was left untouched for forensics"
            )
        if self.state is None:
            raise IllegalTransition("activation state is not initialised; run `init` first")
        return self.state

    def commit(self) -> str:
        assert self.state is not None
        self.raw_sha = commit_state(self.dir_fd, self.state, self.raw_sha)
        return self.raw_sha


def session(state_dir: Path | str = DEFAULT_STATE_DIR, *, create: bool = False) -> Iterator[ActivationSession]:
    """上下文管理器：排他锁 + 孤儿临时文件清理 + 读状态。"""

    from contextlib import contextmanager

    @contextmanager
    def _run() -> Iterator[ActivationSession]:
        dir_fd = open_state_dir(state_dir, create=create)
        try:
            with exclusive_lock(dir_fd, LOCK_FILE, blocking=False):
                recover_orphans(dir_fd)
                state, raw_sha, reason = read_state(dir_fd)
                yield ActivationSession(dir_fd, state, raw_sha, reason)
        finally:
            import os as _os

            _os.close(dir_fd)

    return _run()
