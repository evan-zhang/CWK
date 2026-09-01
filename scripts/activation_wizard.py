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
- 试跑必须同时出示 nightly manifest、验收回执和采集回执，缺一不可；
- 本仓库**不创建、不修改、不删除任何定时任务**，也不调用 OpenClaw/Gateway/cron
  接口。调度只产出一张交接单，由用户在宿主侧执行后回填外部任务标识。

每个子命令向 stdout 输出**一个** JSON 对象，供 Skill 直接消费；失败也一样，
包括输入/持久化的 I/O 失败，**任何路径都不会以 traceback 形式漏出去**。
错误消息统一经 ``redact_message`` 抹掉路径样式片段并截断，因此不会夹带
绝对路径、凭据或文件正文。退出码见 ``EXIT_*``。
连 stdout 自身写不出去（下游关了管道、盘满）也只是安静地返回非零：不谎报成功、
不留 traceback、也不会退化成解释器的 120。

**输入/持久化失败与降级是两件事。** 读不到输入文件、写不进状态目录属于**用法
失败**：命令中止，激活状态原样不动，既不进 `DEGRADED` 也不写任何迁移回执——
因为压根没有产生过试跑判定或业务结论，没有什么可降级的。只有真的算出了一张
FAIL 的试跑回执，才会落到 `DEGRADED`。
"""

from __future__ import annotations

import argparse
import errno
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

from activation_contract import (  # noqa: E402
    ConfigLocatorError,
    NightlyConfigError,
    ProjectEnvironmentError,
    ScheduledEnvironmentMismatch,
    ScopeSchemaError,
    UnschedulableNightlySetting,
    build_discovery_report,
    build_execution_contract,
    build_scheduler_handoff,
    compute_contract_sha256,
    compute_profile_sha256,
    compute_scope_sha256,
    contract_drift,
    detect_schedule_drift,
    evaluate_pilot,
    normalize_scope,
    render_contract_markdown,
    validate_schedule_receipt,
)
from activation_state import (  # noqa: E402
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
    can_transition,
    compute_binding_sha256,
    current_binding,
    default_state,
    grant_is_valid,
    invalidate_stale_confirmations,
    new_activation_id,
    new_confirmation_id,
    next_step_for,
    read_regular_at,
    read_regular_path,
    record_gate_revocation,
    session,
)
from cwk_atomic_file import (  # noqa: E402
    FILE_MODE,
    AtomicFileError,
    ContainmentError,
    LockUnavailable,
    write_atomic,
)
from cwk_pr001_contracts import ContractError  # noqa: E402

# ── 退出码 ──────────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_USAGE = 2
"""用法错误、输入缺失、schema 违约、状态文件损坏、输入/持久化 I/O 失败。"""
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

# 第一道：凡是「以 /、~/、./、../ 开头的一段」都当路径抹掉。前面是单词字符的
# 斜杠（read/write 这种）不算路径，不会被误伤。
_PATH_LIKE = re.compile(r"(?<![A-Za-z0-9_])(?:~|\.{1,2})?/[^\s'\",;)\]]*")

# 第二道：不带前导斜杠的相对多段路径，例如 state/activation/activation.json、
# client/secret.json。仅靠「有斜杠」判定会把 read/write、and/or、input/output
# 这类词组一并抹掉，所以判据必须更窄——见 _looks_like_relative_path。
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._+-]*"
_RELATIVE_PATH_LIKE = re.compile(rf"(?<![A-Za-z0-9_./~-])(?:{_SEGMENT}/)+{_SEGMENT}")

# 字母开头的扩展名。要求字母是为了不把 1/2.5 这种分数当成带扩展名的路径。
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}\Z")
# 「不像自然语言词」的信号：数字、点、下划线、加号、连字符。
_PATHY_SEGMENT = re.compile(r"[0-9._+-]")

MAX_ERROR_CHARS = 240


class WizardError(ActivationError):
    """CLI 层的输入错误。"""

    exit_code = EXIT_USAGE


class InputIOError(WizardError):
    """读输入或写状态时的 I/O 失败（权限、类型、断链等）。"""

    exit_code = EXIT_USAGE


# ── 小工具 ──────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def errno_name(exc: OSError) -> str:
    """把 OSError 归一成 errno 名字。它是稳定的机器可读符号，且不含路径。"""

    code = getattr(exc, "errno", None)
    if isinstance(code, int):
        return errno.errorcode.get(code, f"E{code}")
    return "EUNKNOWN"


def _looks_like_relative_path(token: str) -> bool:
    """判断一个含斜杠的词到底是路径，还是 read/write 这种词组。

    两条判据，满足其一即当路径：

    1. 最后一段带**字母**扩展名——client/secret.json、state/foo/bar.md；
    2. 至少三段，且其中某一段含数字、点、下划线、加号或连字符——
       home/alice/cwk-mirror。

    纯词组两条都不满足：read/write 只有两段且无扩展名；and/or/but 虽有三段，
    但每一段都是纯字母。宁可漏掉一个长得像散文的路径，也不要把错误消息抹成
    看不懂的东西——真正危险的绝对路径由第一道规则无条件拦下。
    """

    parts = token.split("/")
    if _EXTENSION.search(parts[-1]):
        return True
    return len(parts) >= 3 and any(_PATHY_SEGMENT.search(part) for part in parts)


def _redact_relative(match: "re.Match[str]") -> str:
    token = match.group(0)
    trailing = ""
    while token and token[-1] in ".,;:":
        trailing = token[-1] + trailing
        token = token[:-1]
    if not _looks_like_relative_path(token):
        return token + trailing
    return "<redacted-path>" + trailing


def redact_message(text: Any) -> str:
    """错误消息的最后一道闸。

    错误文本会原样进入给 Agent 的 JSON，因此绝不能夹带绝对路径、文件内容
    或超长片段。这里统一抹掉路径样式的片段、压掉控制字符并截断长度；
    上游消息即使将来写得不小心，也不会把私有路径漏出去。
    """

    raw = str(text)
    cleaned = _PATH_LIKE.sub("<redacted-path>", raw)
    cleaned = _RELATIVE_PATH_LIKE.sub(_redact_relative, cleaned)
    cleaned = "".join(" " if (ord(ch) < 0x20 or ord(ch) == 0x7F) else ch for ch in cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_ERROR_CHARS:
        cleaned = cleaned[: MAX_ERROR_CHARS - 1] + "…"
    return cleaned


def _resolve_now(value: Optional[str]) -> str:
    if value is None:
        return utc_now()
    if not _TIMESTAMP.match(value):
        raise WizardError("--now must look like 2026-09-02T01:23:45Z")
    return value


def _read_text(path: Path, label: str) -> str:
    """读一个调用方给的文件。

    路径由调用方提供，可能指向目录、断链、无权限或已被移走的文件；这些都是
    **输入错误**，不是程序缺陷，所以在这里就地转成向导自己的错误类型，
    带 errno 名字但不带路径、不带文件内容。

    走 `read_regular_path` 而不是 `is_file()` + `read_text()`：后者要两次系统
    调用才读到内容，而这两次之间那个名字可以被换成 FIFO，于是 `read_text` 停在
    `open` 上再也不返回。向导是被 Skill 和脚本以「一定会返回」为前提调用的，
    永久挂住比报错难查得多。`read_regular_path` 只 open 一次并在同一个描述符上
    判定，非常规文件当场拒绝，语义上仍然跟随符号链接（用户的输入文件放成软链
    是他自己的事）。
    """

    try:
        raw = read_regular_path(path)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ContainmentError) as exc:
        raise WizardError(f"{label} not found or is not a regular file") from exc
    except (OSError, AtomicFileError) as exc:
        raise InputIOError(f"{label} could not be read ({errno_name(exc)})") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WizardError(f"{label} is not valid UTF-8 text") from exc


def _parse_json_object(text: str, label: str) -> dict:
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        # JSONDecodeError 只报位置，不回显内容；仍走一遍 redact。
        raise WizardError(f"{label} is not valid JSON: {redact_message(exc)}") from exc
    if not isinstance(parsed, dict):
        raise WizardError(f"{label} must be a JSON object")
    return parsed


def _require_json(path: Optional[str], label: str) -> dict:
    if not path:
        raise WizardError(f"{label} is required")
    return _parse_json_object(_read_text(Path(path), label), label)


def _optional_json(path: Optional[str], label: str) -> Optional[dict]:
    if not path:
        return None
    return _require_json(path, label)


def _require_scope(path: Optional[str], label: str = "--scope-file") -> dict:
    """读入范围文件并**立刻**收敛成闭合 schema。

    第一道门确认的就是这个对象，而它随后会被写进发现报告、再由 AI 念给用户听。
    先归一再使用，意味着：多余的键、自由文本的 subject_ref、不认识的 lane、
    `read_only: false` 都在算哈希之前被拒，绝不会有「用户确认过一份包含任意
    字段的对象」这种事。归一是幂等的，所以下游拿到的和这里算哈希的是同一份。
    """

    raw = _require_json(path, label)
    try:
        return normalize_scope(raw)
    except ScopeSchemaError as exc:
        raise WizardError(f"{label} rejected: {exc}") from exc


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


def _read_artifact(dir_fd: int, name: str, label: str) -> dict:
    """读回自己写在状态目录里的产物。

    读法必须与写法对称。产物是 `_write_artifact` 用 dirfd + 原子重命名写进 0700
    目录的，读的时候却用普通 `Path` 打开，就等于承认「写的时候防符号链接、读的
    时候不防」——而**读**才是决定内容的一环：交接单和执行合同读出来什么，
    随后就按什么去比对哈希、去生成给宿主执行的 argv。所以这里同样锚在 dirfd 上，
    经 `read_regular_at` 的 O_NOFOLLOW + O_NONBLOCK + fstat 检查；名字被换成符号
    链接、目录、FIFO、设备或带第二条硬链接的文件时当场拒读，而不是顺着链接去读
    别处的内容、更不会停在一个永远等不到写端的 FIFO 上。

    文件名是固定常量，路径不进错误消息。
    """

    try:
        raw = read_regular_at(dir_fd, name)
    except FileNotFoundError as exc:
        raise WizardError(f"{label} not found; run the previous step first") from exc
    except ContainmentError as exc:
        # 目录里确实有个东西占着这个名字，但它不是我们写下的那个常规文件。
        # 这不是「还没生成」——是有人动过。fail closed，不跟随、不重写、不删除。
        raise WizardError(
            f"{label} is not the regular file this step wrote "
            "(symlink, directory, pipe, device or extra hard link); refusing to read it"
        ) from exc
    except AtomicFileError as exc:
        raise InputIOError(f"{label} could not be read: {redact_message(exc)}") from exc
    except OSError as exc:
        raise InputIOError(f"{label} could not be read ({errno_name(exc)})") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WizardError(f"{label} is corrupt: not valid UTF-8 text") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise WizardError(f"{label} is corrupt: {redact_message(exc)}") from exc
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


def _revoke_gate(state: dict, gate: str) -> list[str]:
    """显式吊销一道门的确认，返回实际被吊销的门（没有就是空表）。

    与 `invalidate_stale_confirmations` 的区别值得说清楚：那个函数处理的是
    「绑定的事实变了，所以确认自动对不上」；这个函数处理的是「事实没变，但我们
    刚刚发现这份确认不该再算数」——漂移就是这种情况。合同漂移时状态里记的
    `contract_sha256` 仍是旧的那个，绑定哈希照样对得上，所以自动失效那条路
    根本不会触发；不显式吊销，就会出现「下一步要求重新确认，同一份负载却说
    第二道确认依然有效」的自相矛盾。
    """

    confirmations = state.get("confirmations") or {}
    if confirmations.get(gate) is None:
        return []
    confirmations[gate] = None
    return [gate]


def _commit_without_transition(sess: ActivationSession, state: dict, now: str) -> None:
    """没有状态迁移、但确实改动了持久内容时，唯一允许的落盘方式。

    「磁盘上的语义变了、revision 却没动」是不可接受的：外部只能靠 revision 和
    history 判断自己看到的是不是最新的事实，一次无声的改写会让所有基于 revision
    的比较得出错误结论。没有迁移可写进 history 时，至少要推进 revision 和
    updated_at，让这次改动留下痕迹。
    """

    state["revision"] = int(state["revision"]) + 1
    state["updated_at"] = now
    sess.commit()


def _commit_after_gate_loss(
    sess: ActivationSession,
    state: dict,
    now: str,
    gates: list[str],
    *,
    contract_sha256: Optional[str] = None,
) -> None:
    """落盘一次「第二道门没了，但没有迁移可写」的改动。

    这是 `_commit_without_transition` 唯一该被替代的场合。撤销 activation 意味着
    「用户答应过的每晚自动运行现在不作数了」，是本状态机最重的改动之一；只留
    revision+1 和 updated_at，事后翻 history 会以为什么都没发生过，而这条记录恰恰
    是「为什么又要重新确认一次」的唯一凭据。

    只在没有迁移时补。有迁移的路径（`flag-drift`、`record-pilot-fail`）那条迁移
    本身就是记录，两边都写就成了重复记账。

    `contract_sha256` 传「相对哪份合同失效」的摘要：漂移时是**新算出来的**那份，
    绑定过期时是状态里记着的那份。
    """

    if "activation" in gates:
        record_gate_revocation(
            state,
            gate="activation",
            now=now,
            input_receipt_sha256=contract_sha256 or state.get("contract_sha256"),
        )
        sess.commit()
    else:
        _commit_without_transition(sess, state, now)


def _merge_gates(*groups: list[str]) -> list[str]:
    """合并同一条命令里多批被作废的门，输出去重且顺序稳定的清单。

    一条命令可能作废两次确认：进门时先清掉本来就过期的，写入新事实之后再清掉
    刚刚被这条命令弄过期的。回报给 Agent 的必须是这条命令**总共**作废了哪些门，
    否则用户会看到「什么都没失效」，而下一步却在要求重新确认。
    """

    seen: set[str] = set()
    for group in groups:
        seen.update(group)
    return [gate for gate in GATES if gate in seen]


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
    now = _resolve_now(args.now)
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
            # `status` 名义上只读，但它确实会作废绑定原像已经对不上的确认，而那是
            # 一次落盘的语义变更。第二道门被这样清掉时同样要留下可读的痕迹，
            # 否则用户只会看到 next_step 又变回去了，却翻不出是哪一步、为什么。
            _commit_after_gate_loss(sess, state, now, dropped)
        return EXIT_OK, _snapshot(state, healthy=True, invalidated_gates=dropped)


def cmd_confirm_discovery(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    scope = _require_scope(args.scope_file)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        state["discovery_scope_sha256"] = compute_scope_sha256(scope)
        # 与 propose-profile / record-pilot 同一种写法：写入新事实之后再收一次，
        # 并把两批合并回报。当前转移表下这一批恒为空（confirm-discovery 只在
        # INSTALLED 合法，那时还没有任何确认），保持同构是为了让「写入新事实后
        # 必须复查确认」成为这个文件里唯一的一种写法，而不是靠个案记忆。
        dropped_by_scope = invalidate_stale_confirmations(state)
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
            invalidated_gates=_merge_gates(dropped, dropped_by_scope),
            confirmed_gate="discovery",
            bound_sha256=confirmation["bound_sha256"],
        )


def cmd_record_discovery(args: argparse.Namespace) -> tuple[int, dict]:
    now = _resolve_now(args.now)
    scope = _require_scope(args.scope_file)
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
        # 同上：新回执会动摇绑定在旧回执上的 profile 门。当前转移表下这一批恒为空
        # （record-discovery 只在 READY_FOR_DISCOVERY 合法，那时还没有 profile 确认）。
        dropped_by_receipt = invalidate_stale_confirmations(state)
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
            invalidated_gates=_merge_gates(dropped, dropped_by_receipt),
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
        # 新画像会让绑定在旧画像上的 profile/activation 确认失效。这一批必须并进
        # 回报里：从 NEEDS_RECONFIRMATION 重新提画像时，activation 门本来还是有效
        # 的，正是被这条命令作废的。漏报会让用户以为「什么都没失效」。
        dropped_by_profile = invalidate_stale_confirmations(state)
        apply_transition(
            state,
            event="propose-profile",
            now=now,
            authorization="system_receipt",
            input_receipt_sha256=state["profile_sha256"],
            next_step="confirm_profile",
        )
        sess.commit()
        return EXIT_OK, _snapshot(
            state,
            invalidated_gates=_merge_gates(dropped, dropped_by_profile),
        )


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
        # 新的合同/试跑回执可能刚刚让第二道确认失效。这一批必须并进回报，
        # 否则成功负载会说「没有确认被作废」，而 next_step 同时要求重新确认。
        dropped_by_receipt = invalidate_stale_confirmations(state)

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
            invalidated_gates=_merge_gates(dropped, dropped_by_receipt),
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
    run_at, tz = _schedule_intent(config)
    with session(args.state_dir) as sess:
        state, dropped = _prepare(sess)
        _require_grant(state, "activation")
        if state["state"] != "PILOT_PASSED":
            raise IllegalTransition(
                f"scheduler handoff requires PILOT_PASSED, current state is {state['state']}"
            )
        contract = _read_artifact(sess.dir_fd, EXECUTION_CONTRACT_FILE, "execution contract")
        if compute_contract_sha256(contract) != state["contract_sha256"]:
            raise IllegalTransition(
                "stored execution contract does not match the confirmed contract hash"
            )
        # 交接单上写的是 `--config <这个路径>`，也就是说**被排期的那份配置是
        # 现在传进来的这个**，不是当初确认时读的那个。所以光比对「盘上的合同
        # 是否等于确认过的哈希」不够：那两者可以都对，而 `--config` 指向另一份
        # 完全不同的配置。于是就地重算一次并要求相等——被确认的那句话，必须就是
        # 今晚真会跑的那句话。
        #
        # 这一步顺带把「配置本身根本跑不起来」挡在门外：`cloud_first` 之类的
        # 设置会在这里抛 UnschedulableNightlySetting，因为固定的 argv 递不出
        # 对应的 --experimental-* 解锁。
        current = build_execution_contract(
            config=config,
            env=os.environ,
            profile_sha256=state["profile_sha256"],
            run_at_local=run_at,
            timezone=tz,
            generated_at=now,
        )
        if compute_contract_sha256(current) != state["contract_sha256"]:
            raise IllegalTransition(
                "the config named on the command line does not resolve to the confirmed "
                "contract; re-run check-drift and reconfirm before scheduling anything"
            )
        handoff = build_scheduler_handoff(
            contract=contract,
            contract_sha256=state["contract_sha256"],
            profile_sha256=state["profile_sha256"],
            pilot_receipt_sha256=state["pilot_receipt_sha256"],
            config_path=args.config,
            project_root=_PROJECT,
            generated_at=now,
        )
        _write_artifact(sess.dir_fd, SCHEDULER_HANDOFF_FILE, handoff)
        # 产出交接单不是状态迁移（仍停在 PILOT_PASSED），所以只推进 revision，
        # 不写 history。这一步的可审计性由交接单自身的哈希承担：它落进
        # schedule_handoff_sha256，并在随后的 record-schedule 里作为
        # input_receipt_sha256 进入历史。
        state["schedule_handoff_sha256"] = handoff["handoff_sha256"]
        _commit_without_transition(sess, state, now)
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
        handoff = _read_artifact(sess.dir_fd, SCHEDULER_HANDOFF_FILE, "scheduler handoff")
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

    真的漂移了就一定会做两件事，且顺序固定：

    1. **吊销第二道确认**。漂移意味着「用户当初点头同意的那件事」和「今晚实际会
       发生的那件事」已经不是同一件；此时状态里记的 `contract_sha256` 还是旧的，
       绑定哈希照样对得上，自动失效那条路不会触发，所以必须显式吊销——否则
       负载会一边说 `next_step: reconfirm_contract`，一边说 activation 门依然
       有效。这两句话不能同时为真。
    2. **能降级就降级，不能降级就什么都不改**。已经在 DEGRADED /
       NEEDS_RECONFIRMATION 里时 `flag-drift` 不合法，那就不写迁移；也**不**
       顺手改 `degraded_reason_code`——那会是一条既没有迁移记录、也没人授权的
       语义变更。凡是落盘的改动都走 `_commit_without_transition`，至少带上
       revision 与 updated_at 的痕迹。
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
        revoked: list[str] = []
        if drifted:
            # 先问再改：迁移合不合法必须在动状态之前就知道，否则「改了再回滚」
            # 会在异常路径上留下半截改动，而它照样会被下面的 commit 写进磁盘。
            can_flag = can_transition(state["state"], "flag-drift")
            revoked = _revoke_gate(state, "activation")
            if can_flag:
                state["degraded_reason_code"] = (
                    "contract_drift" if drift["drifted"] else "schedule_id_unknown"
                )
                apply_transition(
                    state,
                    event="flag-drift",
                    now=now,
                    authorization="system_receipt",
                    input_receipt_sha256=drift["current_contract_sha256"],
                    next_step="reconfirm_contract",
                )
                flagged = True
                sess.commit()
            elif revoked or dropped:
                # 已经在 DEGRADED / NEEDS_RECONFIRMATION：不再降级，也不改降级原因；
                # 只有确认被吊销这一件事需要落盘，并且要带上可读的痕迹。
                # 上面 can_flag 那条分支已经用 flag-drift 迁移记过同一件事，
                # 所以只在这里补，不两边都记。
                _commit_after_gate_loss(
                    sess,
                    state,
                    now,
                    _merge_gates(dropped, revoked),
                    contract_sha256=drift["current_contract_sha256"],
                )
        elif dropped:
            _commit_after_gate_loss(sess, state, now, dropped)
        return (EXIT_DRIFT if drifted else EXIT_OK), _snapshot(
            state,
            invalidated_gates=_merge_gates(dropped, revoked),
            contract_drift=drift,
            schedule_drift=schedule,
            flagged=flagged,
            activation_authorization_revoked=bool(revoked),
            destructive_action_taken=False,
        )


# ── 参数解析 ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="activation_wizard",
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
    # 三份证据都是试跑门的必要输入。故意不设 argparse required：少给证据不是
    # 用法错误，而是**这次试跑不通过**——它必须留下一张写明缺什么的 FAIL 回执，
    # 而不是一句用法提示。
    p.add_argument("--nightly-manifest", help="nightly 运行回执（缺则试跑判 FAIL）")
    p.add_argument("--acceptance", help="验收回执（缺则试跑判 FAIL）")
    p.add_argument("--collect-manifest", help="采集回执（缺则试跑判 FAIL）")
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


def _silence_broken_stdout() -> None:
    """stdout 已经写不动之后，让解释器退出时的那次 flush 无事可做。

    CPython 在退出时会再 flush 一次 ``sys.stdout``。管道已断时那次 flush 会失败，
    结果是 stderr 上出现 ``Exception ignored in: <_io.TextIOWrapper …>`` 并把进程
    退出码改写成 120——既不是本模块承诺的任何一个 ``EXIT_*``，也违背了「失败也只
    输出一个 JSON 对象、不留 traceback」的约定。

    所以按 CPython 文档对 SIGPIPE 的建议，把 stdout 的文件描述符改指 os.devnull：
    退出时的 flush 落进空洞，退出码保持 ``main`` 的返回值。**只**动 stdout 自己的
    描述符；stdout 没有描述符（比如测试里换成了内存缓冲）就什么也不做。
    这里吞掉异常是有意的：此时已经没有任何可用的报告渠道了。
    """

    try:
        fileno = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, fileno)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _failure(command: str, message: str, kind: str, **extra: Any) -> dict:
    """错误负载的唯一构造点：消息一律过 redact，形状对 Agent 稳定。"""

    return {
        "ok": False,
        "command": command,
        "error": redact_message(message),
        "error_kind": kind,
        **extra,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload = args.func(args)
    except LockUnavailable:
        payload = _failure(
            args.command,
            "another activation command is already running (state dir is locked)",
            "LockUnavailable",
        )
        code = EXIT_USAGE
    except AtomicFileError as exc:
        # CAS 冲突 / 容器越界等：一律不重试、不覆盖，交回给调用方。
        payload = _failure(args.command, exc, type(exc).__name__)
        code = EXIT_USAGE
    except ActivationError as exc:
        code = _exit_code_for(exc)
        payload = _failure(args.command, exc, type(exc).__name__)
    except ConfigLocatorError as exc:
        # 配置位置无法在不泄露宿主绝对路径的前提下表述。fail closed：不出交接单，
        # 也不退化成把绝对路径塞进负载。用户改配置位置后重跑即可。
        payload = _failure(args.command, exc, "ConfigLocatorError")
        code = EXIT_REFUSED
    except ScheduledEnvironmentMismatch as exc:
        # 合同里有一项取值来自当前 shell 的环境变量，而定时任务拿不到它。
        # 出交接单就等于承诺一件今晚不会发生的事，所以宁可拒绝：把值搬进配置
        # 文件、重算合同、重跑试跑，再来要交接单。
        payload = _failure(args.command, exc, "ScheduledEnvironmentMismatch")
        code = EXIT_REFUSED
    except UnschedulableNightlySetting as exc:
        # 配置开了一条被排期的命令走不通的路径（cloud_first /
        # publish_cloud_query_catalog 需要 --experimental-* 解锁，而交接单的
        # argv 给不出）。这不是「用法写错了」，是这条路现在不能被排期——
        # 与其让用户确认一份每晚定时失败的自动化，不如当场拒绝。
        payload = _failure(args.command, exc, "UnschedulableNightlySetting")
        code = EXIT_REFUSED
    except ProjectEnvironmentError as exc:
        # 项目根有一个 `.env`，但读不准。上游在 import 阶段就会读它，读失败的形态
        # 是「nightly 根本起不来」甚至「永远停在 open 上」。这不是用法错误，是这套
        # 配置现在描述不了：与其猜一份不含它的合同（那正是这次要修的盲区），
        # 不如当场拒绝。消息里只有固定的 `.env` 三个字，没有路径、没有正文。
        payload = _failure(args.command, exc, "ProjectEnvironmentError")
        code = EXIT_REFUSED
    except NightlyConfigError as exc:
        # 配置或环境里的 nightly 取值本身不合法（不是整数、超出范围、控制字符），
        # 或出现了登记表不认识的键。合同必须逐字复述今晚会发生什么，
        # 有一项讲不清就整份不写。
        payload = _failure(args.command, exc, "NightlyConfigError")
        code = EXIT_USAGE
    except ScopeSchemaError as exc:
        # 授权可见范围不符合闭合 schema。绝不把调用方给的任意对象当成
        # 「用户确认过的范围」转发下去。
        payload = _failure(args.command, exc, "ScopeSchemaError")
        code = EXIT_USAGE
    except ContractError as exc:
        # 输入 JSON 无法规范化（超安全范围的数字、非字符串键、NFC 键冲突等）：
        # 这是调用方给的数据不合格，不是程序缺陷。
        payload = _failure(
            args.command,
            f"input JSON cannot be canonicalised: {exc}",
            "ContractError",
        )
        code = EXIT_USAGE
    except OSError as exc:
        # 输入文件或状态目录的 I/O 失败（权限、目录/断链、磁盘、ENOSPC…）。
        # 只报 errno 名字：不报路径、不报文件内容。状态写入是 CAS + 原子重命名，
        # 因此这里失败意味着状态没有被推进。
        payload = _failure(
            args.command,
            f"input or state I/O failed ({errno_name(exc)}); "
            "the command was aborted and the activation state was not advanced",
            "IOFailure",
            errno=errno_name(exc),
        )
        code = EXIT_USAGE
    else:
        payload = {"ok": code == EXIT_OK, "command": args.command, **payload}
    try:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except OSError:
        # 下游先关掉管道、或者盘满：已经没有渠道报告这件事了。
        # 但绝不留 traceback、绝不谎报成功——输出丢了就是失败，退出码非零。
        # 状态早已在上面 CAS 落盘，这里只是没能把回执讲给调用方听。
        _silence_broken_stdout()
        return EXIT_USAGE
    return code


if __name__ == "__main__":
    raise SystemExit(main())
