#!/usr/bin/env python3
"""RT-053: export, import, revoke and rotate one-KB shared access files.

This is the management/file face, never an HTTP management endpoint. Export,
rotate and revoke require filesystem write access to the protected OPS token
registry. Import writes only the receiving Agent's private local access store.
No command prints the bearer token.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_ledger import dumps, iso, loads, parse_iso, utc_now  # noqa: E402
from kb_storage import assert_no_plaintext_credential_flags  # noqa: E402
from kb_token import (  # noqa: E402
    DEFAULT_TTL_DAYS,
    TokenError,
    UsageError,
    issue_shared_token,
    load_registry,
    public_view,
    revoke_token,
    rotate_shared_token,
    save_registry,
    validate_kb_ids,
)

ACCESS_FILE_SCHEMA = "cwk.kb.access-file.v1"
EXPORT_SCHEMA = "cwk.kb.access-export.v1"
IMPORT_SCHEMA = "cwk.kb.access-import.v1"
REVOKE_SCHEMA = "cwk.kb.access-revoke.v1"
ROTATE_SCHEMA = "cwk.kb.access-rotate.v1"
ERROR_SCHEMA = "cwk.kb.access-error.v1"

ACCESS_DIR_MODE = 0o700
ACCESS_FILE_MODE = 0o600
MAX_ACCESS_FILE_BYTES = 16 * 1024
DEFAULT_ACCESS_DIR = Path("~/.openclaw/cwk/access").expanduser()
CURRENT_HTTP_GATEWAY_HOSTS = frozenset({"192.168.91.72"})
REQUIRED_FIELDS = frozenset(
    {"schema", "kb_id", "gateway_url", "token", "token_id", "issued_at", "expires_at"}
)
_TOKEN_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_ID_RE = re.compile(r"\Atok-[0-9a-f]{16}\Z")


class AccessFileError(TokenError):
    kind = "access_file"


class UnsafePath(AccessFileError):
    kind = "unsafe_path"


class InvalidAccessFile(AccessFileError):
    kind = "invalid_access_file"


class AccessConflict(AccessFileError):
    kind = "conflict"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _absolute_no_follow(path: Path | str) -> Path:
    """Make a path absolute without resolving its final symlink."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _assert_regular_file(path: Path, *, allow_missing: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise AccessFileError(f"授权文件不存在：{path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafePath(f"授权文件路径必须是普通文件且不能是符号链接：{path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafePath(f"授权文件不属于当前用户：{path}")
    if info.st_nlink != 1:
        raise UnsafePath(f"授权文件存在硬链接，拒绝使用：{path}")


def ensure_access_dir(path: Path | str) -> Path:
    target = _absolute_no_follow(path)
    target.mkdir(parents=True, exist_ok=True, mode=ACCESS_DIR_MODE)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"授权目录必须是普通目录且不能是符号链接：{target}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafePath(f"授权目录不属于当前用户：{target}")
    os.chmod(target, ACCESS_DIR_MODE)
    if _mode(target) != ACCESS_DIR_MODE:
        raise UnsafePath(f"授权目录权限必须是 0700：{target}")
    return target


def access_filename(kb_id: str) -> str:
    kb = validate_kb_ids([kb_id])[0]
    digest = hashlib.sha256(kb.encode("utf-8")).hexdigest()[:24]
    return f"kb-{digest}.json"


def access_path_for_kb(kb_id: str, access_dir: Path | str = DEFAULT_ACCESS_DIR) -> Path:
    directory = _absolute_no_follow(access_dir)
    return directory / access_filename(kb_id)


def validate_gateway_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidAccessFile("gateway_url 必须是非空字符串")
    if any(ord(ch) < 32 for ch in value):
        raise InvalidAccessFile("gateway_url 含控制字符")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InvalidAccessFile("gateway_url 不得含账号、查询参数或片段")
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in ("http", "https"):
        raise InvalidAccessFile("gateway_url 必须是绝对 HTTP(S) URL")
    if parsed.scheme == "http" and host not in CURRENT_HTTP_GATEWAY_HOSTS:
        raise InvalidAccessFile("明文 HTTP 只允许当前受控内网 Gateway")
    return value.strip().rstrip("/")


def validate_access_payload(value: object, *, expected_kb_id: str = "") -> dict:
    if not isinstance(value, dict):
        raise InvalidAccessFile("授权文件必须是 JSON 对象")
    keys = frozenset(value)
    if keys != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - keys)
        extra = sorted(keys - REQUIRED_FIELDS)
        detail = []
        if missing:
            detail.append("缺少字段 " + ",".join(missing))
        if extra:
            detail.append("拒绝额外字段 " + ",".join(extra))
        raise InvalidAccessFile("；".join(detail))
    if value.get("schema") != ACCESS_FILE_SCHEMA:
        raise InvalidAccessFile("授权文件 schema 不受支持")
    kb_id = validate_kb_ids([value.get("kb_id", "")])[0]
    if expected_kb_id and kb_id != validate_kb_ids([expected_kb_id])[0]:
        raise InvalidAccessFile("授权文件的 kb_id 与请求目标不一致")
    gateway_url = validate_gateway_url(value.get("gateway_url"))
    token = value.get("token")
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise InvalidAccessFile("token 格式无效")
    token_id = value.get("token_id")
    if not isinstance(token_id, str) or not _TOKEN_ID_RE.fullmatch(token_id):
        raise InvalidAccessFile("token_id 格式无效")
    try:
        issued_at = parse_iso(str(value.get("issued_at") or ""))
        expires_at = parse_iso(str(value.get("expires_at") or ""))
    except ValueError:
        raise InvalidAccessFile("issued_at/expires_at 必须是合法 UTC 时间") from None
    if expires_at <= issued_at:
        raise InvalidAccessFile("expires_at 必须晚于 issued_at")
    return {
        "schema": ACCESS_FILE_SCHEMA,
        "kb_id": kb_id,
        "gateway_url": gateway_url,
        "token": token,
        "token_id": token_id,
        "issued_at": iso(issued_at),
        "expires_at": iso(expires_at),
    }


def read_access_file(path: Path | str, *, expected_kb_id: str = "") -> dict:
    source = _absolute_no_follow(path)
    _assert_regular_file(source)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise AccessFileError(f"授权文件读取失败：{exc}") from exc
    if len(raw) > MAX_ACCESS_FILE_BYTES:
        raise InvalidAccessFile("授权文件超过大小上限")
    try:
        value = loads(raw)
    except Exception:
        raise InvalidAccessFile("授权文件不是合法 JSON") from None
    return validate_access_payload(value, expected_kb_id=expected_kb_id)


def _validate_target(path: Path, *, replace: bool) -> None:
    try:
        _assert_regular_file(path)
    except FileNotFoundError:
        return
    except AccessFileError:
        if not path.exists() and not path.is_symlink():
            return
        raise
    if not replace:
        raise AccessConflict(f"授权文件已存在：{path}；显式 replace 才会覆盖")


def write_access_file(path: Path | str, payload: Mapping[str, object], *, replace: bool = False) -> Path:
    clean = validate_access_payload(dict(payload))
    target = _absolute_no_follow(path)
    parent = ensure_access_dir(target.parent)
    _validate_target(target, replace=replace)
    handle, temp_name = tempfile.mkstemp(dir=str(parent), prefix=".kb-access-")
    try:
        os.fchmod(handle, ACCESS_FILE_MODE)
        with os.fdopen(handle, "wb") as stream:
            stream.write(dumps(clean))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        os.chmod(target, ACCESS_FILE_MODE)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def make_payload(record: Mapping[str, object], token: str, gateway_url: str) -> dict:
    return validate_access_payload(
        {
            "schema": ACCESS_FILE_SCHEMA,
            "kb_id": list(record.get("kb_ids", []))[0],
            "gateway_url": gateway_url,
            "token": token,
            "token_id": record.get("token_id", ""),
            "issued_at": record.get("created_at", ""),
            "expires_at": record.get("expires_at", ""),
        }
    )


def export_access(
    registry: Path | str,
    *,
    kb_id: str,
    gateway_url: str,
    output: Path | str,
    rotate: bool = False,
    replace: bool = False,
    ttl_days: int = DEFAULT_TTL_DAYS,
    actor: str = "",
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict:
    moment = now or utc_now()
    original = load_registry(registry)
    data = copy.deepcopy(original)
    target = _absolute_no_follow(output)
    parent = ensure_access_dir(target.parent)
    _validate_target(target, replace=replace)
    if rotate:
        record, token, superseded = rotate_shared_token(
            data, kb_id=kb_id, ttl_days=ttl_days, now=moment, actor=actor, reason=reason
        )
        schema = ROTATE_SCHEMA
    else:
        record, token = issue_shared_token(
            data, kb_id=kb_id, ttl_days=ttl_days, now=moment, actor=actor, reason=reason
        )
        superseded = []
        schema = EXPORT_SCHEMA
    payload = make_payload(record, token, validate_gateway_url(gateway_url))

    handle, temp_name = tempfile.mkstemp(dir=str(parent), prefix=".kb-access-export-")
    try:
        os.fchmod(handle, ACCESS_FILE_MODE)
        with os.fdopen(handle, "wb") as stream:
            stream.write(dumps(payload))
            stream.flush()
            os.fsync(stream.fileno())
        save_registry(registry, data, now=moment)
        try:
            os.replace(temp_name, target)
            os.chmod(target, ACCESS_FILE_MODE)
        except BaseException:
            save_registry(registry, original, now=moment)
            raise
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return {
        "schema": schema,
        "ok": True,
        "kb_id": payload["kb_id"],
        "token_id": payload["token_id"],
        "generation": record["generation"],
        "access_file": str(target),
        "mode": "0600",
        "superseded_token_ids": superseded,
        "at": iso(moment),
    }


def import_access(
    source: Path | str,
    *,
    access_dir: Path | str = DEFAULT_ACCESS_DIR,
    replace: bool = False,
) -> dict:
    payload = read_access_file(source)
    target = access_path_for_kb(payload["kb_id"], access_dir)
    write_access_file(target, payload, replace=replace)
    return {
        "schema": IMPORT_SCHEMA,
        "ok": True,
        "kb_id": payload["kb_id"],
        "token_id": payload["token_id"],
        "installed_file": str(target),
        "mode": "0600",
    }


def load_installed_access(kb_id: str, access_dir: Path | str = DEFAULT_ACCESS_DIR) -> dict:
    return read_access_file(access_path_for_kb(kb_id, access_dir), expected_kb_id=kb_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="单库共享授权文件：export/import/revoke/rotate")
    sub = parser.add_subparsers(dest="command", required=True)

    def management(name: str, help_text: str) -> argparse.ArgumentParser:
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--registry", required=True)
        node.add_argument("--kb-id", required=True)
        node.add_argument("--gateway-url", required=True)
        node.add_argument("--out", required=True)
        node.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
        node.add_argument("--actor", default="")
        node.add_argument("--reason", default="")
        node.add_argument("--replace", action="store_true")
        return node

    management("export", "首次生成单库共享授权文件")
    management("rotate", "换代并使旧授权文件全部失效")

    revoke = sub.add_parser("revoke", help="按 token_id 即刻吊销")
    revoke.add_argument("--registry", required=True)
    revoke.add_argument("--token-id", required=True)
    revoke.add_argument("--actor", default="")
    revoke.add_argument("--reason", default="")

    install = sub.add_parser("import", help="导入接收的授权文件")
    install.add_argument("--file", required=True)
    install.add_argument("--access-dir", default=str(DEFAULT_ACCESS_DIR))
    install.add_argument("--replace", action="store_true")
    return parser


def run(args: argparse.Namespace, *, now: Optional[datetime] = None) -> dict:
    moment = now or utc_now()
    if args.command in ("export", "rotate"):
        return export_access(
            args.registry,
            kb_id=args.kb_id,
            gateway_url=args.gateway_url,
            output=args.out,
            rotate=args.command == "rotate",
            replace=args.replace,
            ttl_days=args.ttl_days,
            actor=args.actor,
            reason=args.reason,
            now=moment,
        )
    if args.command == "revoke":
        data = load_registry(args.registry)
        record = revoke_token(
            data, token_id=args.token_id, actor=args.actor, reason=args.reason, now=moment
        )
        save_registry(args.registry, data, now=moment)
        return {
            "schema": REVOKE_SCHEMA,
            "ok": True,
            "token_id": record["token_id"],
            "effective": "immediate",
            "at": iso(moment),
        }
    if args.command == "import":
        return import_access(args.file, access_dir=args.access_dir, replace=args.replace)
    raise UsageError("未知子命令")


def main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        sys.stdout.write(dumps(run(args, now=now)).decode("utf-8"))
        return 0
    except TokenError as exc:
        payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": exc.kind, "message": str(exc)}}
        sys.stdout.write(dumps(payload).decode("utf-8"))
        print(f"kb_access_file 失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - JSON CLI boundary
        payload = {"schema": ERROR_SCHEMA, "ok": False, "error": {"kind": type(exc).__name__, "message": str(exc)}}
        sys.stdout.write(dumps(payload).decode("utf-8"))
        print(f"kb_access_file 失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
