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

import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat as stat_module
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "scripts"))

from cwk_atomic_file import (  # noqa: E402
    DIRECTORY_MODE,
    FILE_MODE,
    AtomicFileError,
    ContainmentError,
    LockUnavailable,
    cas_write,
    child_exists,
    recover_orphans,
)
from cwk_pr001_contracts import canonical_json_bytes  # noqa: E402

# ── 不阻塞的只读原语 ────────────────────────────────────────────────────────
#
# 为什么不直接用 `cwk_atomic_file` 的 `read_file` / `open_dir_nofollow` /
# `exclusive_lock`：它们都在 `os.open()` 里**先打开、后 fstat**，而且没有
# `O_NONBLOCK`。若状态目录里的 `activation.json`（或某个产物、或锁文件）被换成
# 一个 FIFO，`os.open(name, O_RDONLY|O_NOFOLLOW)` 会一直等一个永远不会出现的
# 写端——安装器、doctor、向导会**永久挂住**。挂住比报错更糟：它没有失败，所以
# 没人会去看；而 install / doctor 是被别的脚本以「一定会返回」为前提调用的。
#
# 先 lstat 再 open 不能解决：lstat 与 open 之间那一瞬就是攻击窗口，而且窗口里
# 的失败模式恰好是「永久阻塞」，不是「打开了错的东西」。唯一可靠的顺序是
# **带 O_NONBLOCK 打开、再用 fstat 判定、不对就关掉**——FIFO 在 O_NONBLOCK 下
# 立刻返回，字符设备不会在 open 里等载波，随后 fstat 会当场否掉它。
#
# `scripts/cwk_atomic_file.py` 是 PR-001 `managed_script_inventory` 里的
# legacy_frozen_files（带 sha256 pin），RT-032 改它一个字节都属于越权，所以这里
# 实现一份**只服务激活路径**的等价原语，并让异常类型与上游一致
# （ContainmentError / FileNotFoundError），调用方的 except 分支不用改。
MAX_ACTIVATION_FILE_BYTES = 4 * 1024 * 1024

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _validate_activation_leaf(name: str) -> None:
    """叶子名必须是一个普通文件名，不是路径。"""

    if not name or "/" in name or name in (".", "..") or "\x00" in name:
        raise ContainmentError("invalid leaf name")


def _reject_nonregular(fd: int, st: os.stat_result) -> None:
    if not stat_module.S_ISREG(st.st_mode):
        os.close(fd)
        # 不说它究竟是 FIFO 还是设备还是目录：调用方要做的事一样（拒绝），
        # 而具体类型是攻击者可控的信息，没必要回显。
        raise ContainmentError("child is not a regular file; refusing to use it")


def open_activation_dir_fd(path: Path | str) -> int:
    """以 O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK 打开目录并 fstat 复核。

    与上游 `open_dir_nofollow` 的差别只有 `O_NONBLOCK`：即使内核没有在
    `O_DIRECTORY` 上提前拒绝一个 FIFO，这里也不会停在 open 上。
    """

    flags = os.O_RDONLY | _NOFOLLOW | _DIRECTORY | _NONBLOCK | _CLOEXEC
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ContainmentError("directory is a symlink; refusing to follow") from exc
        if exc.errno == errno.ENOTDIR:
            raise ContainmentError("path is not a directory") from exc
        if exc.errno == errno.ENOENT:
            raise ContainmentError("directory does not exist") from exc
        raise AtomicFileError(f"cannot open directory ({exc.errno})", code="open") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise AtomicFileError(f"cannot stat directory ({exc.errno})", code="fstat") from exc
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise ContainmentError("opened target is not a directory")
    return fd


def read_regular_at(dir_fd: int, name: str) -> bytes:
    """读 ``dir_fd`` 下的一个常规文件，永不阻塞、永不跟随链接。

    - ``FileNotFoundError``：名字不存在（与上游 `read_file` 同）；
    - ``ContainmentError``：符号链接、目录、FIFO、设备、套接字、多条硬链接，
      或超过 :data:`MAX_ACTIVATION_FILE_BYTES`。
    """

    _validate_activation_leaf(name)
    flags = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(name) from exc
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ContainmentError("child is a symlink; refusing to follow") from exc
        if exc.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV):
            # 套接字、或没有写端的只写设备：open 直接拒绝，这已经证明它不是
            # 我们写下的那个常规文件。
            raise ContainmentError("child is not a regular file; refusing to use it") from exc
        raise
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise AtomicFileError(f"cannot stat child ({exc.errno})", code="fstat") from exc
    _reject_nonregular(fd, st)
    try:
        if st.st_nlink != 1:
            raise ContainmentError("child has more than one hard link; refusing to read")
        if st.st_size > MAX_ACTIVATION_FILE_BYTES:
            raise ContainmentError("child is larger than the activation read limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                buf = os.read(fd, 65536)
            except InterruptedError:
                continue
            except BlockingIOError:
                # 常规文件不会给出 EAGAIN；真给了就说明它已经不是常规文件了。
                raise ContainmentError("child is not a regular file; refusing to use it")
            if not buf:
                break
            total += len(buf)
            if total > MAX_ACTIVATION_FILE_BYTES:
                raise ContainmentError("child is larger than the activation read limit")
            chunks.append(buf)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_regular_path(path: Path | str) -> bytes:
    """读一个**调用方给的**路径，永不阻塞。

    和 :func:`read_regular_at` 的分工：那个守的是我们自己的私有状态目录，所以
    连符号链接和第二条硬链接都不接受；这个守的是命令行上传进来的输入文件
    （``--config`` / ``--pilot-report`` / ``--collection-receipt`` / ``--scope``），
    用户把它放成软链或与别处同 inode 都是他自己的事，拒绝反而是我们越界。

    因此这里**保留跟随符号链接**的既有语义，只关掉那个真正致命的性质：阻塞。
    原实现是 ``path.is_file()`` 之后 ``path.read_text()``——两次系统调用之间
    就是攻击窗口，而窗口里的失败模式恰好是「``open`` 永远不返回」。先 stat 再
    open 挡不住这个，所以改成**只 open 一次**：带 ``O_NONBLOCK`` 打开，用同一个
    描述符 ``fstat``，不是常规文件就当场关掉。判定和读取因此作用在同一个对象上，
    中间没有可以被替换的缝。

    - ``FileNotFoundError``：路径不存在或断链；
    - ``ContainmentError``：FIFO、设备、套接字、目录，或超过读取上限；
    - 其余 ``OSError``（权限等）原样抛出，由调用方转成脱敏输入错误。
    """

    flags = os.O_RDONLY | _NONBLOCK | _CLOEXEC
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV):
            # 套接字，或没有对端的只写设备：open 自己就否了，不必等到 fstat。
            raise ContainmentError("input is not a regular file; refusing to use it") from exc
        raise
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise AtomicFileError(f"cannot stat input ({exc.errno})", code="fstat") from exc
    _reject_nonregular(fd, st)
    try:
        if st.st_size > MAX_ACTIVATION_FILE_BYTES:
            raise ContainmentError("input is larger than the activation read limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                buf = os.read(fd, 65536)
            except InterruptedError:
                continue
            except BlockingIOError:
                raise ContainmentError("input is not a regular file; refusing to use it")
            if not buf:
                break
            total += len(buf)
            if total > MAX_ACTIVATION_FILE_BYTES:
                raise ContainmentError("input is larger than the activation read limit")
            chunks.append(buf)
        return b"".join(chunks)
    finally:
        os.close(fd)


@contextmanager
def activation_lock(dir_fd: int, name: str, *, blocking: bool = False) -> Iterator[int]:
    """激活专用的排他锁：先确保锁文件是常规文件，再 flock。

    上游 `exclusive_lock` 用 ``O_RDWR|O_CREAT|O_NOFOLLOW``（无 ``O_EXCL``、无
    ``O_NONBLOCK``）。锁名被换成 FIFO 时那次 open 的行为随平台而定，最坏是永久
    阻塞——而且是在**拿锁**这一步，连超时都没有。这里补上 ``O_NONBLOCK`` 并在
    flock 之前 fstat 复核。
    """

    _validate_activation_leaf(name)
    flags = os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK | _CLOEXEC
    try:
        fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ContainmentError("lock file is a symlink; refusing to follow") from exc
        if exc.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV):
            raise ContainmentError("lock file is not a regular file") from exc
        raise AtomicFileError(f"cannot open lock ({exc.errno})", code="lock") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise AtomicFileError(f"cannot stat lock ({exc.errno})", code="lock") from exc
    _reject_nonregular(fd, st)
    try:
        op = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, op)
        except BlockingIOError as exc:
            raise LockUnavailable("lock is held by another process") from exc
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError:
            pass
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

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
    # 撤销一道确认，但**状态不变**。存在的理由：迁移表里没有
    # (DEGRADED, flag-drift) 与 (NEEDS_RECONFIRMATION, flag-drift)，所以在这两个
    # 状态下检测到漂移时，第二道门被撤销了却没有任何迁移可记。只靠 revision+1 和
    # updated_at 留证，等于让「用户的排期授权被作废」这件事只体现在两个计数器上；
    # 事后没人能从 history 里看出**哪道门、为什么、当时绑的是哪份回执**。
    # 这是一条自环事件：from_state == to_state，不进 TRANSITIONS，只进 history。
    "revoke-activation",
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


def can_transition(current: str, event: str) -> bool:
    """(状态, 事件) 是否在迁移表里。

    存在的意义是让调用方**先问再改**：先改状态再靠捕获 `IllegalTransition` 回滚，
    会在异常路径上留下已经写进内存、随后被一起提交的半截改动——那正是「没有回执
    的语义变更」。问一句就能避免。
    """

    return event in EVENTS and (current, event) in TRANSITIONS


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


def record_gate_revocation(
    state: dict,
    *,
    gate: str,
    now: str,
    input_receipt_sha256: Optional[str] = None,
) -> dict:
    """记一条「撤销了某道确认，但状态没变」的自环回执。

    只在**没有迁移可记**的时候用。有迁移时（例如 ACTIVE 遇到漂移会
    `flag-drift` 进 NEEDS_RECONFIRMATION），那次迁移本身已经把语义变更写进
    history 了，再补一条就是重复记账，会让「撤销发生过几次」这个问题有两个
    答案。

    ``input_receipt_sha256`` 传当时的绑定原像摘要（漂移时是**新的**
    contract_sha256），这样 history 里那条记录自己就说清了「被撤销的授权
    是相对哪份合同失效的」。

    `gate` 只用于校验它确实是一道门；门名不进 history —— history 条目是封闭
    schema，加字段就等于开了个自由文本口子。目前只有 activation 会走到这条
    路径（另两道门的绑定原像不受漂移影响），事件名 `revoke-activation` 已经
    把它说死了。
    """

    _check_enum(gate, GATES, "gate")
    if gate != "activation":
        raise ActivationContractError(
            "revoke-activation only records the activation gate; "
            "other gates have no no-transition revocation path"
        )
    current = state["state"]
    _check_enum(current, STATES, "state.state")
    state["revision"] = int(state["revision"]) + 1
    state["updated_at"] = now
    state["history"].append(
        {
            "at": now,
            "from_state": current,
            "to_state": current,
            "event": "revoke-activation",
            "authorization": "none",
            "input_receipt_sha256": input_receipt_sha256,
            "next_step": next_step_for(state),
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
        # 不回显路径：这条消息会原样进入给 Agent 的 JSON。
        raise ActivationError("activation state dir does not exist or is not a directory")
    return open_activation_dir_fd(path)


def read_state(dir_fd: int) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """读取状态。

    返回 ``(state, raw_sha256, integrity_reason)``：

    - 文件不存在 → ``(None, None, None)``，等价于 UNINITIALIZED；
    - 损坏/未知 schema/校验失败 → ``(None, raw_sha256, 原因码)``，
      磁盘内容保持原样以便取证，调用方只能 fail closed。
    """

    if not child_exists(dir_fd, STATE_FILE):
        return None, None, None
    try:
        raw = read_regular_at(dir_fd, STATE_FILE)
    except FileNotFoundError:
        # `child_exists` 与这次 open 之间名字被删掉了。等价于「没有状态」。
        return None, None, None
    except ContainmentError:
        # 状态文件是符号链接、目录、FIFO、设备、或有第二条硬链接：
        # `read_regular_at` 带 O_NONBLOCK 打开后由 fstat 当场否掉，绝不阻塞。
        # 这不是「没有状态」——磁盘上确实有个东西占着这个名字，只是它
        # 不可信。返回原因码而不是抛，让上层一律 fail closed；文件一个字节都不动。
        return None, None, "state_file_not_contained"
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


# ── 只读探针：给 install / doctor 用 ────────────────────────────────────────

ACTIVATION_READINESS_SCHEMA = "cwk.activation_readiness.v1"

READINESS_STATUSES = (
    "not_started",
    "in_progress",
    "active",
    "paused",
    "needs_reconfirmation",
    "degraded",
    "unreadable",
)

# `integrity_reason` 的封闭词表。安装器与 doctor 会把它原样打印，所以它必须是
# 枚举，不能是异常消息——异常消息里会有路径和 errno 细节。
READINESS_INTEGRITY_REASONS = (
    "state_dir_not_a_directory",
    "state_dir_symlink",
    "state_dir_unreadable",
    "state_file_not_contained",
    "state_unreadable",
    "state_unparseable",
    "state_schema_unknown",
    "state_schema_invalid",
)

_STATE_READINESS = {
    "INSTALLED": "in_progress",
    "READY_FOR_DISCOVERY": "in_progress",
    "PROFILE_PROPOSED": "in_progress",
    "PROFILE_CONFIRMED": "in_progress",
    "PILOT_PASSED": "in_progress",
    "ACTIVE": "active",
    "PAUSED": "paused",
    "NEEDS_RECONFIRMATION": "needs_reconfirmation",
    "DEGRADED": "degraded",
}


def unreadable_readiness(reason: str = "state_unreadable") -> dict:
    """私有状态存在但不可信时唯一的答案形状。

    ``reason`` 必须来自 :data:`READINESS_INTEGRITY_REASONS`；不明来源一律收敛成
    ``state_unreadable``，免得异常文本顺着这条路漏进面向 Agent 的输出。

    对外公开是为了让 doctor 这类只读探针在自己的兜底分支里也能给出**同一个**
    答案，而不用把 `status`/`integrity_reason` 的词表抄第二遍——抄一遍就意味着
    将来会有两份不一致的词表。
    """

    if reason not in READINESS_INTEGRITY_REASONS:
        reason = "state_unreadable"
    return _readiness(
        "unreadable",
        state=None,
        state_present=True,
        healthy=False,
        next_step=None,
        integrity_reason=reason,
    )


def _readiness(status: str, **extra: Any) -> dict:
    payload = {
        "schema": ACTIVATION_READINESS_SCHEMA,
        "status": status,
        "state": UNINITIALIZED,
        "state_present": False,
        "healthy": True,
        "next_step": "init",
        "integrity_reason": None,
    }
    payload.update(extra)
    return payload


def readiness(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict:
    """只读探针：报告激活进度，**不创建目录、不加锁、不写任何东西**。

    安装器和 doctor 需要回答「这台机器上激活走到哪了」，但它们都不该因为被问
    了一句就把私有状态目录创建出来——目录本身的存在就是一条事实，安装不该替
    用户宣称这条事实。所以这里只看、不建。

    不加锁是刻意的：一条正在运行的向导命令不应该让 doctor 变红。写入走的是
    原子重命名，因此读到的要么是上一份完整状态，要么是新的一份完整状态，
    不存在半截文件。

    输出只有枚举、布尔和 `null`：没有路径、没有哈希、没有业务内容，可以原样
    进入面向 Agent 的输出。私有状态存在但读不动/校验不过时报 ``unreadable``
    并 ``healthy=False``——fail closed，绝不退化成「当作没激活过」。
    """

    path = Path(state_dir)
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return _readiness("not_started")
    except OSError:
        # 路径存在与否都问不出来（父目录无权限等）。不猜「没激活过」。
        return unreadable_readiness("state_dir_unreadable")

    if stat_module.S_ISLNK(entry.st_mode):
        # 状态目录是个符号链接。`open_dir_nofollow` 会拒绝跟随，但更重要的是：
        # 这个位置本该是安装时用 0700 建出来的私有目录，被换成链接本身就是一条
        # 「有人动过」的事实。绝不跟过去，也绝不当成「还没开始」——后者是唯一会
        # 让一条已经在跑的排期显得清白的答案。
        return unreadable_readiness("state_dir_symlink")
    if not stat_module.S_ISDIR(entry.st_mode):
        return unreadable_readiness("state_dir_not_a_directory")

    try:
        dir_fd = open_activation_dir_fd(path)
    except (OSError, AtomicFileError):
        # 目录在、但打不开（权限、竞态换成链接/FIFO）。有东西，只是不可信。
        # `open_activation_dir_fd` 抛的是 AtomicFileError，不是 OSError——只
        # catch OSError 会让只读探针把 traceback 打到安装输出里。
        return unreadable_readiness("state_dir_unreadable")
    try:
        state, _raw_sha, reason = read_state(dir_fd)
    except (OSError, AtomicFileError):
        return unreadable_readiness("state_unreadable")
    finally:
        os.close(dir_fd)

    if reason is not None:
        return unreadable_readiness(reason)
    if state is None:
        return _readiness("not_started")

    # 在内存副本上收一遍过期确认，让 next_step 说的是**现在**的实话；
    # 磁盘上的状态一个字节都不动——探针不是命令。
    working = json.loads(json.dumps(state))
    invalidate_stale_confirmations(working)
    return _readiness(
        _STATE_READINESS.get(working["state"], "in_progress"),
        state=working["state"],
        state_present=True,
        next_step=next_step_for(working),
    )


def session(state_dir: Path | str = DEFAULT_STATE_DIR, *, create: bool = False) -> Iterator[ActivationSession]:
    """上下文管理器：排他锁 + 孤儿临时文件清理 + 读状态。"""

    @contextmanager
    def _run() -> Iterator[ActivationSession]:
        dir_fd = open_state_dir(state_dir, create=create)
        try:
            with activation_lock(dir_fd, LOCK_FILE, blocking=False):
                recover_orphans(dir_fd)
                state, raw_sha, reason = read_state(dir_fd)
                yield ActivationSession(dir_fd, state, raw_sha, reason)
        finally:
            os.close(dir_fd)

    return _run()
