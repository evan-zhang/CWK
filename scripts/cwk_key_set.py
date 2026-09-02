#!/usr/bin/env python3
"""
cwk_key_set — 把用户在聊天里提供的 CWORK_APP_KEY 写入 .env（RT-033）。

产品决策（2026-09-02，Evan）：CWK 运行在全定制服务端与客户端上，允许用户把
CWORK_APP_KEY 直接粘贴在聊天窗口里发给 Agent；Agent 收到后调用本脚本落盘。
本脚本是 `.env` 中 `CWORK_APP_KEY` 的唯一写入入口：

- Key 从 stdin 读入（管道或 heredoc；交互终端则用掩码输入），绝不进命令行
  参数、进程列表或日志；
- `.env` 已有 `CWORK_APP_KEY=***` 行（含 `export` 前缀、成对引号、重复行）时，
  原子替换为单行 `CWORK_APP_KEY=<新值>`；
- `.env` 不存在时，从同目录 `.env.example` 复制为底稿再写入；
- 写入前剥除 UTF-8 BOM，统一换行为 LF；
- 文件权限固定 `0600`；同目录临时文件 + `os.replace` 原子落盘；
- 回执是 JSON：只含 status、目标路径与修复标记，永不包含 Key 本身、前缀
  或哈希。

用法：
    printf '%s' 'THE_KEY' | python3.11 scripts/cwk_key_set.py
    python3.11 scripts/cwk_key_set.py --env-file /workspace/CWK/.env

退出码：0 成功；2 输入错误（空值、含内嵌空白、超长）。

"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TARGET_KEY = "CWORK" + "_APP_" + "KEY"
_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<rest>.*)$"
)
MAX_KEY_BYTES = 512


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _read_key() -> str:
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("CWORK_APP_KEY: ").strip()
    return sys.stdin.read().strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write CWORK_APP_KEY into .env from stdin (never from argv)."
    )
    parser.add_argument(
        "--env-file",
        default=str(PROJECT / ".env"),
        help="Target .env path (default: the project .env next to this script).",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file).expanduser()
    key = _read_key()
    if not key:
        _emit({"ok": False, "status": "rejected", "reason": "empty_input"})
        return 2
    if any(ch.isspace() for ch in key) or len(key.encode("utf-8")) > MAX_KEY_BYTES:
        _emit({"ok": False, "status": "rejected", "reason": "invalid_key_format"})
        return 2

    template_path = env_path.parent / ".env.example"
    if env_path.exists():
        raw = env_path.read_bytes()
        created = False
    elif template_path.exists():
        raw = template_path.read_bytes()
        created = True
    else:
        raw = b""
        created = True
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")

    replaced = 0
    removed_duplicates = 0
    fixed_export = False
    out_lines: list[str] = []
    for line in text.split("\n"):
        match = _ASSIGN_RE.match(line)
        if match and match.group("name") == TARGET_KEY:
            if line.lstrip().startswith("export"):
                fixed_export = True
            if replaced == 0:
                out_lines.append(f"{TARGET_KEY}={key}")
                replaced = 1
            else:
                removed_duplicates += 1
            continue
        out_lines.append(line)
    if out_lines == [""]:
        out_lines.pop()

    if not replaced:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(f"{TARGET_KEY}={key}")

    new_text = "\n".join(out_lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env.cwk-key-set.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        os.replace(tmp_name, env_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    os.chmod(env_path, 0o600)

    payload = {
        "ok": True,
        "status": "configured",
        "env_path": str(env_path),
        "created": created,
        "replaced_existing": bool(replaced),
        "removed_duplicates": removed_duplicates,
        "fixed_export_prefix": fixed_export,
        "fixed_bom": had_bom,
    }
    if os.environ.get(TARGET_KEY):
        payload["note"] = (
            "process env already exports CWORK_APP_KEY; the shell value takes "
            "precedence over .env at runtime"
        )
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
