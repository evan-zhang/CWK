#!/usr/bin/env python3
"""RT-043: the unified ingest pipeline for a KB (拉取 → originals → 格式工厂 → raw).

Usage::

    python3 scripts/kb_ingest.py plan --source cwork-mirror --root <dir> \\
        --kb-root <kb> [--since 2026-06-01] > plan.json
    python3 scripts/kb_ingest.py run --plan plan.json [--yes]
    python3 scripts/kb_ingest.py status --kb-root <kb>
    python3 scripts/kb_ingest.py reconcile --kb-root <kb>

Every subcommand prints exactly one JSON object on stdout — success and
failure alike — so a caller never has to parse prose to find out what
happened.  Exit codes: ``0`` ok, ``1`` the check or batch went red,
``2`` refused before doing anything (usage, missing source, bad plan).

Design contracts this file implements (RT/RT-043/references/):
DOCDB-INGEST-DESIGN v1.3 §I–§VI, INGEST-AND-TAXONOMY v1.1, KB-PARAMETERS B
表 #2/#2c/#27/#28/#29.  The storage floor is RT-042: every byte goes through
a :class:`kb_storage.StorageBackend` and every write through
:func:`kb_ledger.record_write`, so a backend that accepts writes and stores
nothing fails at the call that lied instead of at acceptance.

Two identity rules carry the whole design and are worth stating up front:

**lineage_id = ``<source>:<源内稳定ID>`` and never carries a rev or a seq.**
A snapshot number is not an identity: keying the index by ``fileId@rev``
would make every new revision a new document, and the version chain — the
thing citations pin to — would never form.  :func:`assert_lineage_key`
refuses a key that smuggles one in.

**A path is a cache, never a key.**  Reconciliation compares
``lineage_id + sha256``; ``originals/`` paths are derived from
``(source, stable_id, sha256)`` alone so that re-ingesting the same bytes
lands on the same path no matter what the file's mtime says today.  Deriving
the archive path from a timestamp is how "write-once" quietly becomes
"write-once-per-mtime-drift" and the idempotence criterion goes green while
the archive doubles.

Credential rule: DocDB calls read ``XG_BIZ_API_KEY`` / ``XG_APP_KEY`` from
the environment and nothing else; NAS credentials stay in ``CWK_NAS_KB_*``
and are read by :mod:`kb_storage`.  No flag on this CLI accepts a secret.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_ledger import (  # noqa: E402
    iso,
    read_json,
    utc_now,
)
from kb_storage import (  # noqa: E402
    NotFound,
    StorageBackend,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
    normalize_path,
)

PLAN_SCHEMA = "cwk.kb.ingest-plan.v1"

# The adapter name the operator types → the lineage source label that goes
# into the index key.  They differ on purpose: "cwork-mirror" names *how* we
# reach the data today (a local mirror directory), "cwork" names *whose*
# data it is.  Swapping the local mirror for a live API later must not
# rewrite every lineage_id in the index.
ADAPTERS: Dict[str, str] = {"cwork-mirror": "cwork", "docdb": "docdb"}

# DOCDB-INGEST-DESIGN §III: 路由智能缺省, not a platform-wide constant.
# cwork mirror items are high-frequency short reports → a time line reads
# better; docdb items are project documents → a classification tree does.
# The confirmation card carries the value so the operator can flip it.
DEFAULT_ROUTE: Dict[str, str] = {"cwork-mirror": "timeline", "docdb": "classify"}
ROUTE_MODES = ("timeline", "classify")

# 稳定ID for the cwork mirror is the report id: the leading digit run of the
# file name.  The threshold exists so that a file named ``2026-08-14-周报.md``
# is not mistaken for report ``2026`` — a four-digit "id" would collide with
# every other file written that year and silently merge two lineages.
MIN_STABLE_ID_DIGITS = 8
_STABLE_ID_PREFIX = re.compile(r"^(\d{%d,})" % MIN_STABLE_ID_DIGITS)

_DAY_IN_PATH = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH_DIR = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Extensions the format factory recognises (DOCDB-INGEST-DESIGN §V).
PASSTHROUGH_EXTS = (".md", ".markdown", ".txt")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif")
ARCHIVE_SKIP_EXTS = (".rar", ".7z")

# The docx converter is a host binary, addressed by environment variable so
# a machine without it degrades to a placeholder instead of failing the run.
ENV_DOCX_CONVERTER = "KB_DOCX_CONVERTER"
DEFAULT_DOCX_CONVERTER = "/opt/homebrew/bin/md2md"

# DocDB browse scripts live in the cms-docdb skill; the path is an env var so
# nothing here hardcodes a home directory.
ENV_DOCDB_SKILL_DIR = "CMS_DOCDB_SKILL_DIR"
DEFAULT_DOCDB_SKILL_DIR = Path.home() / ".agents" / "skills" / "cms-docdb"
DOCDB_KEY_ENVS = ("XG_BIZ_API_KEY", "XG_APP_KEY")

# Substrings that mark a DocDB failure as worth retrying.  Everything else is
# permanent: retrying a 403 four times turns a clear error into a slow one.
# 5xx is deliberately *in* the retry set and still ends red after the last
# attempt — J5 requires a source fault to fail the batch, not to be absorbed.
DOCDB_TRANSIENT_MARKERS = (
    "服务器繁忙", "请求太过频繁", "频繁", "timeout", "timed out",
    "temporarily unavailable", "connection reset", "429", "500", "502",
    "503", "504",
)

MAX_SLUG_CHARS = 40
UNDATED_BUCKET = "_undated"


class IngestError(Exception):
    """A refusal or a failure the CLI reports as JSON and a non-zero exit."""


class SourceError(IngestError):
    """The source could not be read.  J5: never absorbed into a green run."""


# ── lineage identity ────────────────────────────────────────────────────────


def lineage_id(source: str, stable_id: str) -> str:
    key = f"{source}:{stable_id}"
    assert_lineage_key(key)
    return key


def assert_lineage_key(key: str) -> str:
    """Refuse an index key that carries a revision, a sequence or a path.

    This is the guard behind J6.  ``docdb:1234@7`` and ``cwork:99~2`` look
    harmless until the second revision arrives: they create a *new* entry
    instead of extending the version chain, so ``supersedes`` never forms and
    a citation pinned to version 1 can never be resolved.  The split-item
    anchor ``#slug`` from DOCDB-INGEST-DESIGN §II is allowed — it names a
    part of a document, not a moment in its history.
    """
    if not isinstance(key, str) or key.count(":") != 1:
        raise IngestError(f"lineage_id 必须是 <source>:<稳定ID> 形式，收到 {key!r}")
    source, stable = key.split(":", 1)
    if source not in ADAPTERS.values():
        raise IngestError(f"未知 lineage 源 {source!r}（可选 {sorted(set(ADAPTERS.values()))}）")
    if not stable:
        raise IngestError(f"lineage_id 缺少稳定ID：{key!r}")
    body = stable.split("#", 1)[0]
    for marker in ("@", "~", "/", "\\"):
        if marker in body:
            raise IngestError(
                f"lineage_id 不得含版本/序号/路径分隔符 {marker!r}：{key!r}。"
                "快照号不是身份——把 rev 写进键会让同一份文档的第二版另开条目，"
                "版本链和引文钉版本都会失效。"
            )
    if re.search(r"[.]v\d+$", body):
        raise IngestError(f"lineage_id 不得以 .v<数字> 结尾（那是版本，不是身份）：{key!r}")
    return key


def slugify(text: str) -> str:
    """Deterministic, path-safe slug.  CJK survives; everything odd becomes '-'."""
    kept = []
    for char in text:
        if char.isalnum() or "一" <= char <= "鿿":
            kept.append(char)
        else:
            kept.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(kept)).strip("-")
    slug = slug[:MAX_SLUG_CHARS].strip("-")
    return slug or "untitled"


def split_name(name: str) -> Tuple[str, str]:
    """Return ``(stem, lowercased extension)`` for a source file name."""
    suffix = Path(name).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        return name, ""
    return name[: -len(suffix)], suffix.lower()


def stable_id_from_name(name: str) -> Optional[str]:
    """The cwork report id: the leading digit run of the file name."""
    match = _STABLE_ID_PREFIX.match(name)
    return match.group(1) if match else None


def path_parts(rel_path: str) -> List[str]:
    return [part for part in Path(rel_path).parts if part not in ("/", "")]


def derive_date(rel_path: str, mtime: Optional[float]) -> Optional[str]:
    """Return ``YYYY-MM-DD`` for an item, or ``None`` when nothing says so.

    Order matters: a date that is *written down* (in a directory name or the
    file name) beats a file-system timestamp, because copying a mirror
    rewrites every mtime and would otherwise re-file the whole archive under
    the day of the copy.
    """
    matches = _DAY_IN_PATH.findall(rel_path)
    if matches:
        year, month, day = matches[-1]
        return f"{year}-{month}-{day}"
    for part in reversed(path_parts(rel_path)):
        month = _MONTH_DIR.match(part)
        if month:
            return f"{month.group(1)}-{month.group(2)}-01"
    if mtime is not None:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def assert_iso_day(value: Optional[str], flag: str) -> Optional[str]:
    if value is None:
        return None
    if not _ISO_DAY.match(value):
        raise IngestError(f"{flag} 需要 YYYY-MM-DD 形式，收到 {value!r}")
    return value


# ── format factory: the decision half (DOCDB-INGEST-DESIGN §V) ──────────────


@dataclass(frozen=True)
class FormatDecision:
    """What the factory will do with one item, and what that should produce."""

    format: str          # markdown | docx | xlsx | pptx | image | zip | archive | unknown
    handling: str        # passthrough | docx-convert | xlsx-csv | placeholder | skip
    expected_status: str  # converted | placeholder | skipped
    reason: str

    def as_dict(self) -> dict:
        return {
            "format": self.format,
            "handling": self.handling,
            "expected_status": self.expected_status,
            "reason": self.reason,
        }


def docx_converter_path(env: Optional[Dict[str, str]] = None) -> str:
    source = os.environ if env is None else env
    return source.get(ENV_DOCX_CONVERTER) or DEFAULT_DOCX_CONVERTER


def docx_converter_available(env: Optional[Dict[str, str]] = None) -> bool:
    path = docx_converter_path(env)
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def openpyxl_available() -> bool:
    """True when xlsx sheets can be exported.  Optional import, never required.

    The pipeline is standard-library only; openpyxl is a host capability the
    same way the docx converter is.  Absent, xlsx degrades to a placeholder
    and the state ledger says so — it does not fail the batch.
    """
    try:  # pragma: no cover - exercised through decide_format
        import openpyxl  # noqa: F401
    except Exception:
        return False
    return True


def decide_format(
    name: str,
    *,
    env: Optional[Dict[str, str]] = None,
    has_openpyxl: Optional[bool] = None,
) -> FormatDecision:
    """Classify one item.  Host capabilities are inputs, not global state."""
    _, ext = split_name(name)
    if ext in PASSTHROUGH_EXTS:
        return FormatDecision("markdown", "passthrough", "converted", "md/txt 直通，不做转换")
    if ext == ".docx":
        if docx_converter_available(env):
            return FormatDecision(
                "docx", "docx-convert", "converted",
                f"本机转换器可用：{docx_converter_path(env)}",
            )
        return FormatDecision(
            "docx", "placeholder", "placeholder",
            f"未找到可执行的 {ENV_DOCX_CONVERTER}（默认 {DEFAULT_DOCX_CONVERTER}），降级占位",
        )
    if ext == ".xlsx":
        available = openpyxl_available() if has_openpyxl is None else has_openpyxl
        if available:
            return FormatDecision("xlsx", "xlsx-csv", "converted", "每个 sheet 导出一个 CSV")
        return FormatDecision(
            "xlsx", "placeholder", "placeholder", "openpyxl 不可导入，降级占位"
        )
    if ext == ".pptx":
        return FormatDecision("pptx", "placeholder", "placeholder", "pptx 内容转换列 v1.1")
    if ext in IMAGE_EXTS:
        return FormatDecision(
            "image", "placeholder", "placeholder",
            "图片按文件名/路径可检索，内容不可检索（v1 口径）",
        )
    if ext == ".zip":
        return FormatDecision("zip", "placeholder", "placeholder", "占位 + 中央目录清单，不解压")
    if ext in ARCHIVE_SKIP_EXTS:
        return FormatDecision("archive", "skip", "skipped", f"{ext} v1 不承诺解包，跳过并记状态")
    return FormatDecision(
        "unknown", "placeholder", "placeholder", f"未知扩展名 {ext or '(无)'}，走通用占位路径"
    )


# ── source items ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceItem:
    """One thing worth ingesting, as the adapter saw it.

    ``locator`` is how :func:`fetch_bytes` gets the bytes back later, from a
    plan file, without rescanning the source.  It never holds a credential.
    """

    stable_id: str
    name: str
    group: str
    date: Optional[str]
    size: Optional[int]
    locator: dict

    def as_dict(self) -> dict:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "group": self.group,
            "date": self.date,
            "size": self.size,
            "locator": self.locator,
        }


# ── adapter: cwork-mirror (local directory scan) ────────────────────────────


def scan_cwork_mirror(root: Path, *, since: Optional[str] = None) -> Tuple[List[SourceItem], List[str]]:
    """Walk ``root`` and return ``(items, unidentified)``.

    ``unidentified`` holds the relative paths whose file name has no report
    id.  They are *reported*, never dropped: "静默丢件" is the failure mode
    DOCDB-INGEST-DESIGN §VI exists to prevent, and a scanner that quietly
    ignores what it cannot name is the purest form of it.
    """
    root = Path(root)
    if not root.is_dir():
        raise SourceError(f"源目录不存在或不是目录：{root}")
    items: List[SourceItem] = []
    unidentified: List[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "@eaDir")
        for name in sorted(files):
            if name.startswith(".") or name in ("Thumbs.db",):
                continue
            absolute = Path(current) / name
            rel = absolute.relative_to(root).as_posix()
            stable = stable_id_from_name(name)
            if stable is None:
                unidentified.append(rel)
                continue
            try:
                stat = absolute.stat()
            except OSError as exc:
                raise SourceError(f"源文件不可读：{rel}（{exc}）") from exc
            date = derive_date(rel, stat.st_mtime)
            if since and date and date < since:
                continue
            parent = absolute.parent
            group = parent.name if parent != root else root.name
            items.append(
                SourceItem(
                    stable_id=stable,
                    name=name,
                    group=group,
                    date=date,
                    size=stat.st_size,
                    locator={"kind": "file", "path": str(absolute)},
                )
            )
    items.sort(key=lambda item: (item.date or "", item.stable_id))
    return items, sorted(unidentified)


# ── adapter: docdb (via the cms-docdb skill's browse scripts) ───────────────


def docdb_skill_dir(env: Optional[Dict[str, str]] = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get(ENV_DOCDB_SKILL_DIR) or DEFAULT_DOCDB_SKILL_DIR)


def docdb_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build the child environment for a DocDB skill call.

    The app key comes from the environment and only from the environment
    (KB-PARAMETERS §F.5).  There is no ``--key`` flag to forget to remove and
    no fallback that would resolve a key by making another network call.
    """
    source = dict(os.environ if env is None else env)
    key = next((source[name] for name in DOCDB_KEY_ENVS if source.get(name)), "")
    if not key:
        raise SourceError(
            "缺少 DocDB 凭据环境变量：" + " / ".join(DOCDB_KEY_ENVS) + "。"
            "凭据只从环境变量读，不接受命令行明文。"
        )
    source["XG_BIZ_API_KEY"] = key
    return source


def run_json(
    cmd: Sequence[str],
    env: Dict[str, str],
    *,
    cwd: Path,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[..., object] = subprocess.run,
) -> dict:
    """Run a skill script and return its last JSON line (cwk_sync 的 run_json 模式).

    Transient failures are retried; a permanent one is raised immediately.
    Either way the *last* attempt's failure becomes a :class:`SourceError`
    rather than an empty result — J5 turns on this call never returning
    something that looks like "no items".
    """
    last_error = ""
    for attempt in range(max(1, retries)):
        proc = runner(list(cmd), cwd=str(cwd), env=env, text=True, capture_output=True)
        if proc.returncode == 0:
            lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
            if not lines:
                last_error = f"命令没有输出 JSON：{' '.join(cmd)}\n{proc.stdout.strip()[:400]}"
            else:
                try:
                    payload = json.loads(lines[-1])
                except json.JSONDecodeError as exc:
                    last_error = f"JSON 解析失败：{exc}"
                    payload = None
                if isinstance(payload, dict):
                    message = str(payload.get("resultMsg") or "")
                    if payload.get("resultCode") == 1:
                        return payload
                    if not any(
                        marker.lower() in message.lower() for marker in DOCDB_TRANSIENT_MARKERS
                    ):
                        raise SourceError(f"DocDB 调用失败（永久错误）：{message or payload}")
                    last_error = message
        else:
            last_error = (
                f"命令退出码 {proc.returncode}：{' '.join(cmd)}\n"
                f"{proc.stderr.strip()[:400]}\n{proc.stdout.strip()[:400]}"
            )
        if attempt < max(1, retries) - 1:
            sleep(1 + attempt)
    raise SourceError(f"DocDB 调用失败（重试 {max(1, retries)} 次后仍失败）：{last_error[:600]}")


def docdb_children(payload: dict) -> List[dict]:
    """Normalise the several shapes browse.py answers with."""
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows: List[dict] = []
        for key in ("folders", "files", "list", "rows", "items", "children"):
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
        return rows
    return []


def _docdb_id(row: dict) -> Optional[str]:
    for key in ("fileId", "id", "folderId"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    return None


def _docdb_is_folder(row: dict) -> bool:
    return str(row.get("type")) in ("1", "folder")


def _docdb_date(row: dict) -> Optional[str]:
    for key in ("updateTime", "gmtModified", "createTime", "gmtCreate"):
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value)
        match = _DAY_IN_PATH.search(text)
        if match:
            return "-".join(match.groups())
        if text.isdigit() and len(text) >= 10:
            seconds = int(text[:13]) / 1000 if len(text) >= 13 else int(text)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def scan_docdb(
    root_id: str,
    *,
    since: Optional[str] = None,
    recursive: bool = True,
    env: Optional[Dict[str, str]] = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[..., object] = subprocess.run,
    max_depth: int = 8,
) -> Tuple[List[SourceItem], List[str]]:
    """List a DocDB folder tree through ``scripts/browse/browse.py``."""
    skill_dir = docdb_skill_dir(env)
    browse = skill_dir / "scripts" / "browse" / "browse.py"
    child_env = docdb_env(env)
    items: List[SourceItem] = []
    unidentified: List[str] = []
    queue: List[Tuple[str, str, int]] = [(str(root_id), str(root_id), 0)]
    seen: set = set()
    while queue:
        folder_id, folder_name, depth = queue.pop(0)
        if folder_id in seen:
            continue
        seen.add(folder_id)
        payload = run_json(
            [sys.executable, str(browse), str(folder_id)],
            child_env,
            cwd=skill_dir,
            retries=retries,
            sleep=sleep,
            runner=runner,
        )
        for row in docdb_children(payload):
            row_id = _docdb_id(row)
            name = str(row.get("name") or "")
            if not row_id or not name:
                unidentified.append(f"{folder_name}/{name or '(无名)'}")
                continue
            if _docdb_is_folder(row):
                if recursive and depth + 1 <= max_depth:
                    queue.append((row_id, name, depth + 1))
                continue
            date = _docdb_date(row)
            if since and date and date < since:
                continue
            size = row.get("size")
            items.append(
                SourceItem(
                    stable_id=row_id,
                    name=name,
                    group=folder_name,
                    date=date,
                    size=int(size) if isinstance(size, (int, str)) and str(size).isdigit() else None,
                    locator={"kind": "docdb", "file_id": row_id},
                )
            )
    items.sort(key=lambda item: (item.date or "", item.stable_id))
    return items, sorted(set(unidentified))


# ── routing (DOCDB-INGEST-DESIGN §III) ──────────────────────────────────────


def raw_path_for(
    *,
    route_mode: str,
    lineage: str,
    stable_id: str,
    name: str,
    group: str,
    date: Optional[str],
) -> str:
    """Where the raw artefact lands.  Deterministic given the card's inputs."""
    stem, _ = split_name(name)
    slug = slugify(stem[len(stable_id):] if stem.startswith(stable_id) else stem)
    leaf = f"{stable_id}-{slug}.md"
    if route_mode == "timeline":
        if not date:
            return f"raw/{UNDATED_BUCKET}/{leaf}"
        return f"raw/{date[:7]}/{date}/{leaf}"
    if route_mode == "classify":
        return f"raw/classify/{slugify(group)}/{leaf}"
    raise IngestError(f"未知 route 模式 {route_mode!r}（可选 {ROUTE_MODES}）")


def originals_path_for(source: str, stable_id: str, digest: str, name: str) -> str:
    """``originals/<source>/<stable_id>/<sha256><ext>`` — path from identity only.

    Not from a date and not from a counter.  See the module docstring: an
    archive path that depends on wall-clock time re-writes the same bytes
    under a second name the moment an mtime shifts, and the write-once
    criterion goes green while the archive silently doubles.
    """
    _, ext = split_name(name)
    return f"originals/{source}/{stable_id}/{digest}{ext}"


def confirmation_card(
    item: SourceItem,
    *,
    source_label: str,
    route_mode: str,
    decision: FormatDecision,
    digest: Optional[str] = None,
) -> dict:
    """The 确认卡 for one item: what will happen, and what may be edited.

    ``run`` reads ``route_mode`` and ``proposed_raw_path`` back *from the
    card*, so editing the plan file actually changes where the artefact
    lands.  A card that only described the decision would be decoration.
    """
    lineage = lineage_id(source_label, item.stable_id)
    return {
        "lineage_id": lineage,
        "route_mode": route_mode,
        "proposed_raw_path": raw_path_for(
            route_mode=route_mode,
            lineage=lineage,
            stable_id=item.stable_id,
            name=item.name,
            group=item.group,
            date=item.date,
        ),
        "originals_path": (
            originals_path_for(source_label, item.stable_id, digest, item.name)
            if digest
            else "originals/<source>/<稳定ID>/<sha256><ext>（摄取时按原件哈希定址）"
        ),
        "format": decision.as_dict(),
        "editable": ["route_mode", "proposed_raw_path"],
        "note": "改 route_mode 后 proposed_raw_path 需一并改，run 以卡上的路径为准。",
    }


# ── plan ────────────────────────────────────────────────────────────────────


def build_plan(
    *,
    adapter: str,
    root: str,
    kb_root: str,
    since: Optional[str] = None,
    route_mode: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    has_openpyxl: Optional[bool] = None,
    generated_at: Optional[datetime] = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[..., object] = subprocess.run,
) -> dict:
    """Scan the source and produce the ingest plan with one card per item."""
    if adapter not in ADAPTERS:
        raise IngestError(f"未知源适配器 {adapter!r}（可选 {sorted(ADAPTERS)}）")
    source_label = ADAPTERS[adapter]
    mode = route_mode or DEFAULT_ROUTE[adapter]
    if mode not in ROUTE_MODES:
        raise IngestError(f"未知 route 模式 {mode!r}（可选 {ROUTE_MODES}）")
    assert_iso_day(since, "--since")

    if adapter == "cwork-mirror":
        items, unidentified = scan_cwork_mirror(Path(root).expanduser(), since=since)
    else:
        items, unidentified = scan_docdb(
            root, since=since, env=env, retries=retries, sleep=sleep, runner=runner
        )

    rows: List[dict] = []
    for item in items:
        decision = decide_format(item.name, env=env, has_openpyxl=has_openpyxl)
        row = item.as_dict()
        row["lineage_id"] = lineage_id(source_label, item.stable_id)
        row["confirmation"] = confirmation_card(
            item, source_label=source_label, route_mode=mode, decision=decision
        )
        rows.append(row)

    counts: Dict[str, int] = {}
    for row in rows:
        status = row["confirmation"]["format"]["expected_status"]
        counts[status] = counts.get(status, 0) + 1

    return {
        "schema": PLAN_SCHEMA,
        "generated_at": iso(generated_at or utc_now()),
        "adapter": adapter,
        "source": source_label,
        "root": str(root),
        "kb_root": str(kb_root),
        "since": since,
        "route": {"mode": mode, "default_for_source": DEFAULT_ROUTE[adapter]},
        "item_count": len(rows),
        "expected_status_counts": counts,
        "unidentified": unidentified,
        "unidentified_note": (
            "这些源文件没有稳定ID（文件名缺少 ≥"
            f"{MIN_STABLE_ID_DIGITS} 位数字前缀），本次不摄取；"
            "它们会进 ingest-state 与 reconcile 的 unidentified 清单，不会静默消失。"
        ),
        "items": rows,
    }


def load_plan(path: Path) -> dict:
    """Read and structurally validate a plan file before anything is written."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise IngestError(f"读不到计划文件：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise IngestError(f"计划文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PLAN_SCHEMA:
        raise IngestError(f"计划文件 schema 应为 {PLAN_SCHEMA}，收到 {type(payload).__name__}")
    for key in ("adapter", "source", "kb_root", "items"):
        if key not in payload:
            raise IngestError(f"计划文件缺字段：{key}")
    if not isinstance(payload["items"], list):
        raise IngestError("计划文件的 items 必须是数组")
    for row in payload["items"]:
        card = (row or {}).get("confirmation") or {}
        assert_lineage_key(str(card.get("lineage_id") or row.get("lineage_id") or ""))
        mode = card.get("route_mode")
        if mode not in ROUTE_MODES:
            raise IngestError(f"确认卡的 route_mode 非法：{mode!r}")
        # normalize_path is the same gate every backend write goes through;
        # running it here means an edited card is refused before it can put
        # an artefact outside the library root.
        normalize_path(str(card.get("proposed_raw_path") or ""))
    return payload


# ── CLI ─────────────────────────────────────────────────────────────────────


def emit(payload: dict) -> None:
    """One JSON object on stdout.  Every subcommand, success or failure."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def open_backend(args: argparse.Namespace, kb_root: Optional[str]) -> StorageBackend:
    kind = getattr(args, "backend", "local")
    if kind == "local" and not kb_root:
        raise IngestError("local 后端必须给 --kb-root")
    return build_backend(kind, root=kb_root, prefix=getattr(args, "prefix", ""))


def read_kb_code(backend: StorageBackend) -> str:
    try:
        return str(read_json(backend, "kb.json").get("kb_code") or "")
    except NotFound as exc:
        raise IngestError(
            "库根下没有 kb.json——请先用 scripts/kb_create.py 建库，摄取不负责造库。"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RT-043 摄取管道：plan / run / status / reconcile（输出一律 JSON）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="扫描源，产出带路由确认卡的摄取计划")
    plan.add_argument("--source", required=True, choices=sorted(ADAPTERS), dest="adapter")
    plan.add_argument("--root", required=True, help="cwork-mirror 的目录 / docdb 的根 fileId")
    plan.add_argument("--kb-root", required=True, help="目标库根（local 后端）")
    plan.add_argument("--since", help="只收这一天（含）之后的件，YYYY-MM-DD")
    plan.add_argument("--route", choices=ROUTE_MODES, help="覆盖按源缺省的 route.mode")
    plan.add_argument("--out", help="同时把计划写到这个文件")
    plan.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
    plan.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
    return parser


def cmd_plan(args: argparse.Namespace) -> int:
    backend = None
    try:
        backend = open_backend(args, args.kb_root)
        kb_code = read_kb_code(backend)
    finally:
        close_backend(backend)
    payload = build_plan(
        adapter=args.adapter,
        root=args.root,
        kb_root=args.kb_root,
        since=args.since,
        route_mode=args.route,
    )
    payload["kb_code"] = kb_code
    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    emit(payload)
    return 0


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {"plan": cmd_plan}


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        return COMMANDS[args.command](args)
    except IngestError as exc:
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
