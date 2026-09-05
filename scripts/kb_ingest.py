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
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from kb_ledger import (  # noqa: E402
    CHANGED_PATHS_REL,
    RAW_MANIFEST_REL,
    batch_id_for,
    dumps,
    iso,
    read_json,
    record_changed_paths,
    record_write,
    refresh_manifest,
    utc_now,
)
from kb_storage import (  # noqa: E402
    NotFound,
    StorageBackend,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
    normalize_path,
    sha256_bytes,
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

# The three accounts of DOCDB-INGEST-DESIGN §II plus the state ledger of §VI.
# ``raw-index.prev.json`` is the 前代备份 the same section asks for: it is
# written *before* the new index, so an interrupted publish leaves both a
# complete old index and a complete copy of it.
RAW_INDEX_REL = "_system/raw-index.json"
RAW_INDEX_PREV_REL = "_system/raw-index.prev.json"
INGEST_STATE_REL = "_system/ingest-state.json"
PROVENANCE_REL = "_system/provenance.json"
PROVENANCE_CHAIN_REL = "_system/provenance.jsonl"

RAW_INDEX_SCHEMA = "cwk.kb.raw-index.v1"
INGEST_STATE_SCHEMA = "cwk.kb.ingest-state.v1"
PROVENANCE_SCHEMA = "cwk.kb.provenance.v1"
CONFIRM_SCHEMA = "cwk.kb.ingest-confirm.v1"
RUN_SCHEMA = "cwk.kb.ingest-run.v1"

# Recorded on every index entry so a later model or rule change can re-run a
# defined subset instead of the whole library (DOCDB-INGEST-DESIGN §III).
# v1 has no AI routing turn, and says so rather than leaving the field out.
RULE_VERSION = "ingest-v1"
MODEL_VERSION = "none"

TERMINAL_STATUSES = ("converted", "placeholder", "skipped")
ALL_STATUSES = ("pending",) + TERMINAL_STATUSES + ("failed",)

# docx 质量门 (DOCDB-INGEST-DESIGN §V): a conversion that "succeeded" into an
# empty or near-empty document is a failure that hashes green.  Below this
# many characters the artefact becomes a placeholder with a stated reason.
MIN_DOCX_CHARS = 20
MAX_PLACEHOLDER_LISTING = 200


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
        # `_system` is the platform's own ledger area inside a mirror (B 表
        # #2c): timeline reply-chain events and snapshots the nightly
        # pipeline writes for its own bookkeeping.  It is never source
        # content — sweeping it in turned 451 real reports into a 3827-item
        # plan whose extra items were the platform reading its own diary
        # (observed 2026-09-05, Case 1).
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in ("@eaDir", "_system")
        )
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

_DIGIT_NOTE = """Digit-leading folder names: this DSM refuses ``2026-06`` and
``1-交付包-…`` (digit run + hyphen) with code=400 while accepting ``2026x``
and ``2026_06``; letter-prefixed names always pass (observed 2026-09-05)."""


def _device_safe_dir(slug: str) -> str:
    """Prefix digit-leading slugs — see _DIGIT_NOTE."""
    return f"c-{slug}" if slug[:1].isdigit() else slug


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
        # d- prefix: this DSM refuses digits-and-hyphens-only folder names
        # (code=400; observed 2026-09-05 when raw/2026-06 killed the batch
        # mid-ingest), and a date directory is nothing but digits and hyphens.
        return f"raw/d-{date[:7]}/d-{date}/{leaf}"
    if route_mode == "classify":
        # A source folder like「1. 」slugifies to a bare digit — the same DSM
        # refusal — so numeric slugs carry a letter prefix too.
        return f"raw/classify/{_device_safe_dir(slugify(group))}/{leaf}"
    raise IngestError(f"未知 route 模式 {route_mode!r}（可选 {ROUTE_MODES}）")


def originals_path_for(source: str, stable_id: str, digest: str, name: str) -> str:
    """``originals/<source>/id-<stable_id>/<sha256><ext>`` — path from identity only.

    Not from a date and not from a counter.  See the module docstring: an
    archive path that depends on wall-clock time re-writes the same bytes
    under a second name the moment an mtime shifts, and the write-once
    criterion goes green while the archive silently doubles.  The ``id-``
    prefix on the stable-id directory is a device constraint, not style:
    this DSM 7.x refuses CreateFolder for a purely-numeric name with
    code=400 (observed 2026-09-05, Case 1 item 1), and a stable id is
    nothing but digits.
    """
    _, ext = split_name(name)
    return f"originals/{source}/id-{stable_id}/{digest}{ext}"


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
            else "originals/<source>/id-<稳定ID>/<sha256><ext>（摄取时按原件哈希定址；id- 前缀避开 DSM 纯数字目录名限制）"
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


# ── format factory: the execution half ──────────────────────────────────────


class ItemFailure(IngestError):
    """One item failed.  The batch goes red; the items around it still land."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{('：' + detail) if detail else ''}")
        self.reason = reason
        self.detail = detail


@dataclass
class Artefact:
    """What the factory produced for one item, ready to be written."""

    data: bytes
    kind: str                    # document | placeholder
    status: str                  # converted | placeholder
    converter: str
    placeholder_reason: Optional[str] = None
    extras: Dict[str, bytes] = field(default_factory=dict)


def placeholder_body(
    *,
    lineage: str,
    name: str,
    origin_sha: str,
    origin_size: int,
    reason: str,
    listing: Optional[Sequence[str]] = None,
) -> bytes:
    """A占位件 with no wall-clock in it.

    Determinism is not cosmetic here: J1 compares the whole tree between two
    runs, and a placeholder stamped with "generated at" would differ on every
    pass and turn the idempotence criterion into noise.
    """
    lines = [
        "---",
        f"lineage_id: {lineage}",
        "artifact_kind: placeholder",
        f"placeholder_reason: {reason}",
        f"origin_name: {name}",
        f"origin_sha256: {origin_sha}",
        f"origin_size: {origin_size}",
        f"rule_version: {RULE_VERSION}",
        "---",
        "",
        f"# {name}（占位）",
        "",
        f"本件在摄取 v1 未做内容转换：{reason}。",
        "原件字节已 write-once 存档在 originals/ 下，可按 lineage_id + sha256 定位。",
        "占位件不进精编引用集，也不计入 classify 成功率（DOCDB-INGEST-DESIGN §III）。",
    ]
    if listing is not None:
        lines += ["", f"## 内容清单（{len(listing)} 项，不解压）", ""]
        for entry in list(listing)[:MAX_PLACEHOLDER_LISTING]:
            lines.append(f"- {entry}")
        if len(listing) > MAX_PLACEHOLDER_LISTING:
            lines.append(f"- …… 另有 {len(listing) - MAX_PLACEHOLDER_LISTING} 项")
    return ("\n".join(lines) + "\n").encode("utf-8")


def zip_listing(data: bytes) -> List[str]:
    """Read a zip's central directory without extracting anything.

    A zip whose directory cannot be read is a hard failure, not a placeholder
    (DOCDB-INGEST-DESIGN §V: 清单读取失败必红).  Degrading it would file an
    unreadable archive next to the readable ones with the same green status.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return sorted(info.filename for info in archive.infolist())
    except Exception as exc:  # noqa: BLE001 - any malformed zip
        raise ItemFailure("zip-directory-unreadable", str(exc)) from exc


def convert_docx(
    data: bytes,
    *,
    env: Optional[Dict[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
) -> Tuple[Optional[str], str]:
    """Run the host docx converter.  Returns ``(markdown or None, reason)``."""
    converter = docx_converter_path(env)
    child_env = dict(os.environ if env is None else env)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "input.docx"
        source.write_bytes(data)
        proc = runner(
            [converter, str(source)], cwd=tmp, env=child_env, text=True, capture_output=True
        )
    code = getattr(proc, "returncode", 1)
    text = (getattr(proc, "stdout", "") or "").strip()
    if code != 0:
        return None, f"docx-convert-failed:exit{code}"
    if not text:
        return None, "docx-convert-empty"
    if len(text) < MIN_DOCX_CHARS:
        return None, "docx-convert-too-short"
    return text, "docx-convert-ok"


def convert_xlsx(data: bytes) -> Dict[str, str]:
    """Return ``{sheet name: CSV text}``.  openpyxl is an optional import."""
    import openpyxl  # noqa: PLC0415 - optional host capability, never required

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets: Dict[str, str] = {}
    try:
        for sheet in workbook.worksheets:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            for row in sheet.iter_rows(values_only=True):
                writer.writerow(["" if cell is None else str(cell) for cell in row])
            sheets[str(sheet.title)] = buffer.getvalue()
    finally:
        workbook.close()
    return sheets


def sheet_dir_for(raw_path: str) -> str:
    """Sibling directory that holds one CSV per sheet."""
    return raw_path[: -len(".md")] + ".sheets" if raw_path.endswith(".md") else raw_path + ".sheets"


def materialise(
    *,
    lineage: str,
    name: str,
    raw_path: str,
    origin: bytes,
    decision: FormatDecision,
    env: Optional[Dict[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
) -> Optional[Artefact]:
    """Turn original bytes into the raw artefact.  ``None`` means skipped."""
    digest = sha256_bytes(origin)
    if decision.handling == "skip":
        return None
    if decision.handling == "passthrough":
        return Artefact(data=origin, kind="document", status="converted", converter="passthrough")
    if decision.handling == "docx-convert":
        text, reason = convert_docx(origin, env=env, runner=runner)
        if text is None:
            return Artefact(
                data=placeholder_body(
                    lineage=lineage, name=name, origin_sha=digest,
                    origin_size=len(origin), reason=reason,
                ),
                kind="placeholder", status="placeholder",
                converter="docx:" + docx_converter_path(env), placeholder_reason=reason,
            )
        return Artefact(
            data=(text + "\n").encode("utf-8"), kind="document", status="converted",
            converter="docx:" + docx_converter_path(env),
        )
    if decision.handling == "xlsx-csv":
        try:
            sheets = convert_xlsx(origin)
        except Exception as exc:  # noqa: BLE001 - any openpyxl failure
            reason = "xlsx-unreadable"
            return Artefact(
                data=placeholder_body(
                    lineage=lineage, name=name, origin_sha=digest,
                    origin_size=len(origin), reason=f"{reason}:{exc}"[:200],
                ),
                kind="placeholder", status="placeholder",
                converter="xlsx:openpyxl", placeholder_reason=reason,
            )
        directory = sheet_dir_for(raw_path)
        extras = {
            f"{directory}/{slugify(title)}.csv": text.encode("utf-8")
            for title, text in sheets.items()
        }
        lines = [
            "---",
            f"lineage_id: {lineage}",
            "artifact_kind: document",
            f"origin_name: {name}",
            f"origin_sha256: {digest}",
            f"rule_version: {RULE_VERSION}",
            "---",
            "",
            f"# {name}",
            "",
            f"表格按 sheet 导出为 CSV（共 {len(sheets)} 个）：",
            "",
        ]
        for title in sorted(sheets):
            lines.append(f"- {title} → `{directory}/{slugify(title)}.csv`")
        return Artefact(
            data=("\n".join(lines) + "\n").encode("utf-8"), kind="document",
            status="converted", converter="xlsx:openpyxl", extras=extras,
        )
    listing = zip_listing(origin) if decision.format == "zip" else None
    reason = decision.format + "-not-converted-in-v1"
    return Artefact(
        data=placeholder_body(
            lineage=lineage, name=name, origin_sha=digest, origin_size=len(origin),
            reason=reason, listing=listing,
        ),
        kind="placeholder", status="placeholder", converter="placeholder",
        placeholder_reason=reason,
    )


# ── fetching original bytes ─────────────────────────────────────────────────


def fetch_bytes(
    locator: dict,
    *,
    env: Optional[Dict[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Get an item's original bytes back from what the plan recorded."""
    kind = (locator or {}).get("kind")
    if kind == "file":
        path = Path(str(locator.get("path") or ""))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SourceError(f"源文件读不到：{path}（{exc}）") from exc
    if kind == "docdb":
        skill_dir = docdb_skill_dir(env)
        script = skill_dir / "scripts" / "query" / "download-file.py"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "payload.bin"
            run_json(
                [sys.executable, str(script), str(locator.get("file_id")),
                 "--output", str(output)],
                docdb_env(env), cwd=skill_dir, retries=retries, sleep=sleep, runner=runner,
            )
            if not output.is_file():
                raise SourceError(
                    f"DocDB 下载脚本声称成功但没有产出文件：fileId={locator.get('file_id')}"
                )
            return output.read_bytes()
    raise IngestError(f"计划里的 locator 无法解析：{locator!r}")


# ── originals: write-once (J1) ──────────────────────────────────────────────


def archive_original(
    backend: StorageBackend, *, source: str, stable_id: str, name: str, data: bytes
) -> Tuple[str, str, bool]:
    """Archive the original bytes.  Returns ``(path, sha256, wrote)``.

    Write-once means exactly this: the path is content-addressed, so bytes we
    already hold are recognised by *identity*, not by a timestamp comparison,
    and the second ingest of the same item writes nothing at all.  A file
    already sitting at that path whose digest disagrees is corruption, not a
    new version — the path encodes the digest, so the two cannot legitimately
    differ.
    """
    digest = sha256_bytes(data)
    path = originals_path_for(source, stable_id, digest, name)
    if backend.exists(path):
        if backend.sha256(path) != digest:
            raise ItemFailure(
                "originals-sha-mismatch",
                f"{path} 的字节与其内容寻址不符——存档层被改写过，拒绝覆盖",
            )
        return path, digest, False
    record_write(backend, path, data)
    return path, digest, True


def versioned_path(raw_path: str, version: int) -> str:
    """``raw/…/x.md`` → ``raw/…/x.v2.md`` for a timeline-mode new version."""
    if version <= 1:
        return raw_path
    stem, ext = (raw_path[: -len(".md")], ".md") if raw_path.endswith(".md") else (raw_path, "")
    return f"{stem}.v{version}{ext}"


# ── the three accounts + the state ledger ───────────────────────────────────


class Accounts:
    """raw-index (#28), provenance (#27) and ingest-state (#29) as one unit.

    They are published together, in a fixed order, after every item: index
    first, then the provenance chain, then the state.  The order is the
    recovery contract — state is what a resumed run trusts, so it must be the
    *last* thing that becomes true.  Publishing state first and dying would
    tell the next run "已完成" about an artefact no account can locate.
    """

    def __init__(self, backend: StorageBackend, kb_code: str) -> None:
        self.backend = backend
        self.kb_code = kb_code
        self.index = self._load(RAW_INDEX_REL, {"schema": RAW_INDEX_SCHEMA, "entries": {}})
        self.index.setdefault("entries", {})
        self.state = self._load(
            INGEST_STATE_REL,
            {"schema": INGEST_STATE_SCHEMA, "items": {}, "unidentified": [], "batches": []},
        )
        for key, default in (("items", {}), ("unidentified", []), ("batches", [])):
            self.state.setdefault(key, default)
        try:
            self.chain = self.backend.read(PROVENANCE_CHAIN_REL)
        except NotFound:
            self.chain = b""
        self.pending_records: List[dict] = []
        self.written: List[str] = []
        self.dirty = False

    def _load(self, path: str, default: dict) -> dict:
        try:
            return read_json(self.backend, path)
        except NotFound:
            return dict(default)

    # ── reads ───────────────────────────────────────────────────────────
    def entry(self, lineage: str) -> Optional[dict]:
        return self.index["entries"].get(lineage)

    def item_state(self, lineage: str) -> Optional[dict]:
        return self.state["items"].get(lineage)

    def already_done(self, lineage: str, origin_sha: str) -> bool:
        """True when this exact item, these exact bytes, are already landed.

        J1 and J4 are the same question asked twice.  ``failed`` and
        ``pending`` are deliberately *not* terminal, so a resumed run picks
        them up; and a terminal item whose raw artefact has since vanished is
        not "done" either, or the pipeline would happily leave a hole that
        only reconcile would ever notice.
        """
        row = self.item_state(lineage)
        if not row or row.get("status") not in TERMINAL_STATUSES:
            return False
        if row.get("origin_sha256") != origin_sha:
            return False
        if row.get("status") == "skipped":
            return True
        entry = self.entry(lineage)
        if not entry or entry.get("origin_sha256") != origin_sha:
            return False
        return all(self.backend.exists(path) for path in entry.get("artifacts") or [])

    # ── writes ──────────────────────────────────────────────────────────
    def upsert(
        self,
        lineage: str,
        *,
        source: str,
        stable_id: str,
        route_mode: str,
        raw_path: str,
        artefact: Artefact,
        artefact_sha: str,
        artefact_size: int,
        artifacts: Sequence[str],
        origin_sha: str,
        originals_path: str,
        at: str,
    ) -> dict:
        """Extend the lineage's version chain (J6) and point it at the newest."""
        previous = self.entry(lineage)
        if previous is None:
            version, supersedes, versions = 1, None, []
        else:
            version = int(previous.get("version") or 1) + 1
            supersedes = int(previous.get("version") or 1)
            versions = list(previous.get("versions") or [])
        versions.append(
            {
                "version": version,
                "supersedes": supersedes,
                "origin_sha256": origin_sha,
                "sha256": artefact_sha,
                "path": raw_path,
                "originals": originals_path,
                "at": at,
            }
        )
        entry = {
            "lineage_id": lineage,
            "source": source,
            "stable_id": stable_id,
            "route_mode": route_mode,
            "path": raw_path,
            "artifacts": sorted(set(artifacts)),
            "size": artefact_size,
            "sha256": artefact_sha,
            "origin_sha256": origin_sha,
            "originals": originals_path,
            "artifact_kind": artefact.kind,
            "placeholder_reason": artefact.placeholder_reason,
            "status": "ok" if artefact.kind == "document" else "placeholder",
            "version": version,
            "versions": versions,
            "model_version": MODEL_VERSION,
            "rule_version": RULE_VERSION,
            "updated_at": at,
        }
        self.index["entries"][lineage] = entry
        self.dirty = True
        return entry

    def set_state(
        self,
        lineage: str,
        *,
        status: str,
        at: str,
        reason: Optional[str] = None,
        origin_sha: Optional[str] = None,
        originals_path: Optional[str] = None,
        raw_path: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> dict:
        if status not in ALL_STATUSES:
            raise IngestError(f"未知摄取状态 {status!r}（可选 {ALL_STATUSES}）")
        previous = self.state["items"].get(lineage) or {}
        row = {
            "lineage_id": lineage,
            "status": status,
            "reason": reason,
            "origin_sha256": origin_sha or previous.get("origin_sha256"),
            "originals": originals_path or previous.get("originals"),
            "raw_path": raw_path if raw_path is not None else previous.get("raw_path"),
            "attempts": int(previous.get("attempts") or 0) + 1,
            "batch_id": batch_id,
            "updated_at": at,
        }
        self.state["items"][lineage] = row
        self.dirty = True
        return row

    def add_provenance(self, record: dict) -> None:
        self.pending_records.append(record)
        self.dirty = True

    def note_unidentified(self, paths: Sequence[str]) -> None:
        merged = sorted(set(self.state.get("unidentified") or []) | set(paths))
        if merged != (self.state.get("unidentified") or []):
            self.state["unidentified"] = merged
            self.dirty = True

    def add_batch(self, summary: dict) -> None:
        self.state["batches"].append(summary)
        self.dirty = True

    def publish(self) -> List[str]:
        """Write the accounts.  No-op when nothing changed (the J1 zero-write).

        The index goes out through a previous-generation copy first, so a
        publish that dies half way leaves both a complete old index and a
        complete backup of it — the tmp+fsync+rename the local backend does
        underneath protects the file, this protects the *generation*.
        """
        if not self.dirty:
            return []
        written: List[str] = []
        self.index["schema"] = RAW_INDEX_SCHEMA
        self.index["kb_code"] = self.kb_code
        self.index["entry_count"] = len(self.index["entries"])

        try:
            current = self.backend.read(RAW_INDEX_REL)
        except NotFound:
            current = None
        if current is not None:
            record_write(self.backend, RAW_INDEX_PREV_REL, current)
            written.append(RAW_INDEX_PREV_REL)
        record_write(self.backend, RAW_INDEX_REL, dumps(self.index))
        written.append(RAW_INDEX_REL)

        if self.pending_records:
            appended = b"".join(
                (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                for record in self.pending_records
            )
            self.chain = self.chain + appended
            record_write(self.backend, PROVENANCE_CHAIN_REL, self.chain)
            written.append(PROVENANCE_CHAIN_REL)
            self.pending_records = []
            record_write(
                self.backend,
                PROVENANCE_REL,
                dumps(
                    {
                        "schema": PROVENANCE_SCHEMA,
                        "kb_code": self.kb_code,
                        "chain": PROVENANCE_CHAIN_REL,
                        "record_count": self.chain.count(b"\n"),
                        "note": (
                            "来源账是 append-only JSONL；本文件只是它的指针与计数，"
                            "不复制内容（复制出来的第二份账迟早会和链对不上）。"
                        ),
                    }
                ),
            )
            written.append(PROVENANCE_REL)

        # State last: it is what a resumed run believes.
        self.state["schema"] = INGEST_STATE_SCHEMA
        self.state["kb_code"] = self.kb_code
        self.state["counts"] = status_counts(self.state["items"])
        record_write(self.backend, INGEST_STATE_REL, dumps(self.state))
        written.append(INGEST_STATE_REL)

        self.dirty = False
        for path in written:
            if path not in self.written:
                self.written.append(path)
        return written

    def owned_paths(self) -> List[str]:
        """Every path the accounts can explain.

        This is what the root-manifest refresh is allowed to re-sign.  A file
        under ``raw/`` or ``originals/`` that no account mentions stays
        unexplained and still trips the ledger — which is the point: the
        exemption is "我记得写过它", not "它在我的目录里".
        """
        owned = {
            RAW_INDEX_REL, RAW_INDEX_PREV_REL, INGEST_STATE_REL,
            PROVENANCE_REL, PROVENANCE_CHAIN_REL, RAW_MANIFEST_REL,
        }
        for entry in self.index["entries"].values():
            owned.update(entry.get("artifacts") or [])
            if entry.get("originals"):
                owned.add(entry["originals"])
            for version in entry.get("versions") or []:
                if version.get("path"):
                    owned.add(version["path"])
                if version.get("originals"):
                    owned.add(version["originals"])
        for row in self.state["items"].values():
            for key in ("originals", "raw_path"):
                if row.get(key):
                    owned.add(row[key])
        return sorted(owned)


def status_counts(items: Dict[str, dict]) -> Dict[str, int]:
    counts = {status: 0 for status in ALL_STATUSES}
    for row in items.values():
        status = str(row.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def refresh_raw_manifest(backend: StorageBackend, kb_code: str, at: str) -> List[str]:
    """Rebuild B #3 after writing raw, so ``kb_doctor verify --raw`` stays true."""
    entries = {}
    for path in backend.walk_files("raw"):
        if path == RAW_MANIFEST_REL:
            continue
        data = backend.read(path)
        entries[path] = {"sha256": sha256_bytes(data), "size": len(data)}
    record_write(
        backend,
        RAW_MANIFEST_REL,
        dumps(
            {
                "schema": "cwk.kb.raw-manifest.v1",
                "kb_code": kb_code,
                "generated_at": at,
                "entry_count": len(entries),
                "entries": entries,
            }
        ),
    )
    return [RAW_MANIFEST_REL]


# ── run ─────────────────────────────────────────────────────────────────────


def confirmation_summary(plan: dict) -> dict:
    """What ``run`` prints when ``--yes`` is absent.  Reads only."""
    return {
        "schema": CONFIRM_SCHEMA,
        "applied": False,
        "confirm_required": True,
        "adapter": plan.get("adapter"),
        "source": plan.get("source"),
        "kb_root": plan.get("kb_root"),
        "route": plan.get("route"),
        "item_count": len(plan.get("items") or []),
        "expected_status_counts": plan.get("expected_status_counts"),
        "unidentified": plan.get("unidentified") or [],
        "cards": [row.get("confirmation") for row in plan.get("items") or []],
        "note": "确认卡可改（route_mode / proposed_raw_path）。确认后加 --yes 执行；本次零写入。",
    }


def execute_plan(
    backend: StorageBackend,
    plan: dict,
    *,
    kb_code: str,
    env: Optional[Dict[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    now: Optional[datetime] = None,
    has_openpyxl: Optional[bool] = None,
) -> dict:
    """Ingest every item in ``plan``.  Returns the run report; never raises
    for a *single* item's failure — that item goes to ``failed`` with a reason
    and the batch ends red (J5)."""
    at = iso(now or utc_now())
    rows = list(plan.get("items") or [])
    source = str(plan.get("source") or "")
    batch = batch_id_for([str((row.get("confirmation") or {}).get("lineage_id")) for row in rows])
    accounts = Accounts(backend, kb_code)
    accounts.note_unidentified(plan.get("unidentified") or [])

    results: List[dict] = []
    touched: List[str] = []
    for row in rows:
        card = row.get("confirmation") or {}
        lineage = assert_lineage_key(str(card.get("lineage_id") or ""))
        stable_id = str(row.get("stable_id") or lineage.split(":", 1)[1])
        name = str(row.get("name") or stable_id)
        route_mode = str(card.get("route_mode"))
        try:
            target = normalize_path(str(card.get("proposed_raw_path") or ""))
            origin = fetch_bytes(
                row.get("locator") or {}, env=env, runner=runner, retries=retries, sleep=sleep
            )
            origin_sha = sha256_bytes(origin)
            if accounts.already_done(lineage, origin_sha):
                results.append({"lineage_id": lineage, "status": "unchanged", "wrote": False})
                continue
            originals_path, origin_sha, wrote_original = archive_original(
                backend, source=source, stable_id=stable_id, name=name, data=origin
            )
            if wrote_original:
                touched.append(originals_path)
            # Re-decided here, not read off the card: the card records what
            # the host could do when the plan was made, and a converter that
            # has since appeared or vanished must change the outcome, not be
            # overruled by a stale expectation.
            decision = decide_format(name, env=env, has_openpyxl=has_openpyxl)
            artefact = materialise(
                lineage=lineage, name=name, raw_path=target, origin=origin,
                decision=decision, env=env, runner=runner,
            )
            if artefact is None:
                accounts.set_state(
                    lineage, status="skipped", at=at, reason=decision.reason,
                    origin_sha=origin_sha, originals_path=originals_path,
                    raw_path=None, batch_id=batch,
                )
                accounts.add_provenance(
                    {
                        "schema": PROVENANCE_SCHEMA, "at": at, "batch_id": batch,
                        "lineage_id": lineage, "event": "skipped",
                        "originals": originals_path, "origin_sha256": origin_sha,
                        "derived": [], "converter": "skip", "reason": decision.reason,
                    }
                )
                accounts.publish()
                results.append({"lineage_id": lineage, "status": "skipped", "wrote": True})
                continue

            previous = accounts.entry(lineage)
            version = 1 if previous is None else int(previous.get("version") or 1) + 1
            # raw 只增不改 for a timeline library: a second version lands
            # beside the first instead of over it.  A classify library is the
            # 活文档 model of §II, where the current file *is* the document
            # and the chain remembers what it used to be.
            raw_path = versioned_path(target, version) if route_mode == "timeline" else target
            raw_path = normalize_path(raw_path)

            artefact_sha = record_write(backend, raw_path, artefact.data)
            touched.append(raw_path)
            artifacts = [raw_path]
            for extra_path, extra_data in sorted(artefact.extras.items()):
                safe = normalize_path(extra_path)
                record_write(backend, safe, extra_data)
                artifacts.append(safe)
                touched.append(safe)

            accounts.upsert(
                lineage, source=source, stable_id=stable_id, route_mode=route_mode,
                raw_path=raw_path, artefact=artefact, artefact_sha=artefact_sha,
                artefact_size=len(artefact.data), artifacts=artifacts,
                origin_sha=origin_sha, originals_path=originals_path, at=at,
            )
            accounts.set_state(
                lineage, status=artefact.status, at=at,
                reason=artefact.placeholder_reason, origin_sha=origin_sha,
                originals_path=originals_path, raw_path=raw_path, batch_id=batch,
            )
            accounts.add_provenance(
                {
                    "schema": PROVENANCE_SCHEMA, "at": at, "batch_id": batch,
                    "lineage_id": lineage, "event": "ingested", "version": version,
                    "supersedes": None if version == 1 else version - 1,
                    "originals": originals_path, "origin_sha256": origin_sha,
                    "derived": artifacts, "artifact_sha256": artefact_sha,
                    "converter": artefact.converter,
                    "artifact_kind": artefact.kind,
                }
            )
            accounts.publish()
            results.append(
                {
                    "lineage_id": lineage, "status": artefact.status, "version": version,
                    "raw_path": raw_path, "wrote": True,
                }
            )
        except IngestError as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            accounts.set_state(
                lineage, status="failed", at=at, reason=f"{reason}: {exc}"[:400],
                batch_id=batch,
            )
            accounts.publish()
            results.append({"lineage_id": lineage, "status": "failed", "reason": str(exc)})

    failed = [row for row in results if row["status"] == "failed"]
    summary = {
        "batch_id": batch,
        "at": at,
        "source": source,
        "item_count": len(rows),
        "counts": {
            status: sum(1 for row in results if row["status"] == status)
            for status in ("converted", "placeholder", "skipped", "unchanged", "failed")
        },
        "failed": [row["lineage_id"] for row in failed],
    }
    if touched or accounts.dirty or failed:
        accounts.add_batch(summary)
        accounts.publish()
    if touched:
        touched += refresh_raw_manifest(backend, kb_code, at)
        record_changed_paths(backend, touched, reason=f"ingest:{batch}", at=now or utc_now())
    if accounts.written:
        # Re-sign the root ledger for *any* run that wrote, including a batch
        # where every item failed: the state ledger changed, and a manifest
        # left describing the tree from before it is a ledger that lies.
        allowed = sorted(set(accounts.owned_paths()) | set(touched) | {CHANGED_PATHS_REL})
        refresh_manifest(
            backend, kb_code=kb_code, generated_at=now or utc_now(),
            allow_new=allowed, allow_replaced=allowed,
        )

    return {
        "schema": RUN_SCHEMA,
        "ok": not failed,
        "applied": True,
        **summary,
        "results": results,
        "written_paths": sorted(set(touched) | set(accounts.written)),
    }


# ── status + coverage reconciliation (DOCDB-INGEST-DESIGN §VI) ──────────────


STATUS_SCHEMA = "cwk.kb.ingest-status.v1"
RECONCILE_SCHEMA = "cwk.kb.ingest-reconcile.v1"

# Every list here means "件对不上". They are separate fields rather than one
# bag because they need different repairs: a missing raw artefact is re-run,
# a hash mismatch is a hand-edited raw file (§IV says report *that*, not
# "文件丢失"), an orphan original is a batch that died before its accounts.
RECONCILE_RED_FIELDS = (
    "missing_raw",
    "raw_modified_by_hand",
    "missing_originals",
    "missing_index",
    "orphan_originals",
    "orphan_raw",
    "failed_items",
    "pending_items",
)


def dedupe_rows(rows: Sequence) -> List:
    """Stable de-duplication for rows that may be dicts (unhashable)."""
    seen: List[str] = []
    out: List = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.append(key)
        out.append(row)
    return out


def load_accounts_readonly(backend: StorageBackend) -> Tuple[dict, dict]:
    def read(path: str, default: dict) -> dict:
        try:
            return read_json(backend, path)
        except NotFound:
            return default

    index = read(RAW_INDEX_REL, {"entries": {}})
    state = read(INGEST_STATE_REL, {"items": {}})
    index.setdefault("entries", {})
    state.setdefault("items", {})
    return index, state


def ingest_status(backend: StorageBackend) -> dict:
    """Summarise ingest-state.  A report, not a gate — the gate is reconcile."""
    index, state = load_accounts_readonly(backend)
    items = state["items"]
    counts = status_counts(items)
    failed = [
        {
            "lineage_id": lineage,
            "reason": row.get("reason"),
            "attempts": row.get("attempts"),
            "updated_at": row.get("updated_at"),
        }
        for lineage, row in sorted(items.items())
        if row.get("status") == "failed"
    ]
    batches = state.get("batches") or []
    return {
        "schema": STATUS_SCHEMA,
        "ok": not failed and counts.get("pending", 0) == 0,
        "kb_code": state.get("kb_code") or index.get("kb_code"),
        "item_count": len(items),
        "indexed_count": len(index["entries"]),
        "counts": counts,
        "failed": failed,
        "unidentified": state.get("unidentified") or [],
        "batch_count": len(batches),
        "last_batch": batches[-1] if batches else None,
        "note": "status 只汇总状态账；覆盖率判定在 reconcile（缺件 exit 1）。",
    }


def reconcile_coverage(backend: StorageBackend) -> dict:
    """originals ↔ index ↔ raw 三方对账.

    §VI exists because the three accounts can each be internally consistent
    while an item is missing from all of them.  So this walks the *bytes* on
    both ends — every originals file must be explained by an account, and
    every account entry must resolve to bytes — instead of comparing the
    accounts with each other and calling the agreement proof.
    """
    index, state = load_accounts_readonly(backend)
    entries = index["entries"]
    items = state["items"]

    report: Dict[str, List] = {name: [] for name in RECONCILE_RED_FIELDS}
    referenced_raw: set = set()
    referenced_originals: set = set()

    for lineage, entry in sorted(entries.items()):
        artifacts = list(entry.get("artifacts") or ([entry["path"]] if entry.get("path") else []))
        for path in artifacts:
            referenced_raw.add(path)
            if not backend.exists(path):
                report["missing_raw"].append({"lineage_id": lineage, "path": path})
        primary = entry.get("path")
        if primary and backend.exists(primary) and entry.get("sha256"):
            if backend.sha256(primary) != entry["sha256"]:
                # §IV 红线：raw 可移动、可重命名，不可编辑内容。
                report["raw_modified_by_hand"].append(
                    {"lineage_id": lineage, "path": primary,
                     "recorded_sha256": entry["sha256"],
                     "actual_sha256": backend.sha256(primary)}
                )
        for version in entry.get("versions") or []:
            if version.get("path"):
                referenced_raw.add(version["path"])
            if version.get("originals"):
                referenced_originals.add(version["originals"])
        original = entry.get("originals")
        if original:
            referenced_originals.add(original)
            if not backend.exists(original):
                report["missing_originals"].append({"lineage_id": lineage, "path": original})

    for lineage, row in sorted(items.items()):
        status = row.get("status")
        original = row.get("originals")
        if original:
            referenced_originals.add(original)
            if not backend.exists(original):
                report["missing_originals"].append({"lineage_id": lineage, "path": original})
        if status == "failed":
            report["failed_items"].append({"lineage_id": lineage, "reason": row.get("reason")})
        elif status == "pending":
            report["pending_items"].append(lineage)
        elif status in ("converted", "placeholder") and lineage not in entries:
            # 这正是 §VI 的静默丢件：状态账说做完了，定位账里没有。
            report["missing_index"].append({"lineage_id": lineage, "status": status})

    for path in backend.walk_files("originals"):
        if path not in referenced_originals:
            report["orphan_originals"].append(path)
    for path in backend.walk_files("raw"):
        if path.startswith("raw/_system/"):
            continue
        if path not in referenced_raw:
            report["orphan_raw"].append(path)

    # The same missing original can be reached from the index and from the
    # state; report it once so a count of the list is a count of the problem.
    deduped = {name: dedupe_rows(rows) for name, rows in report.items()}
    ok = all(not deduped[name] for name in RECONCILE_RED_FIELDS)
    return {
        "schema": RECONCILE_SCHEMA,
        "ok": ok,
        "kb_code": state.get("kb_code") or index.get("kb_code"),
        "checked": {
            "index_entries": len(entries),
            "state_items": len(items),
            "originals_files": len(backend.walk_files("originals")),
            "raw_files": len([p for p in backend.walk_files("raw")
                              if not p.startswith("raw/_system/")]),
        },
        **deduped,
        "unidentified": state.get("unidentified") or [],
        "unidentified_note": "无稳定ID 的源文件：从未摄取，不计入缺件，但也不许消失。",
    }


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

    run = sub.add_parser("run", help="按计划执行摄取；没有 --yes 时只打确认卡")
    run.add_argument("--plan", required=True, help="plan 子命令产出的计划文件")
    run.add_argument("--kb-root", help="覆盖计划里记的库根")
    run.add_argument(
        "--yes",
        action="store_true",
        help="确认后执行。缺省只输出确认卡并退出，本次零写入。",
    )
    run.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
    run.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")

    for name, help_text in (
        ("status", "读 ingest-state 汇总"),
        ("reconcile", "覆盖率对账；缺件 exit 1 并输出差异 JSON"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--kb-root", help="库根（local 后端必填）")
        node.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
        node.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
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


def cmd_run(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan).expanduser())
    if not args.yes:
        # Reads only.  The confirmation gate has to be *provably* inert, so
        # it returns before a backend is even opened.
        emit(confirmation_summary(plan))
        return 0
    kb_root = args.kb_root or plan.get("kb_root")
    backend = None
    try:
        backend = open_backend(args, kb_root)
        report = execute_plan(backend, plan, kb_code=read_kb_code(backend))
    finally:
        close_backend(backend)
    emit(report)
    return 0 if report["ok"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    backend = None
    try:
        backend = open_backend(args, args.kb_root)
        payload = ingest_status(backend)
    finally:
        close_backend(backend)
    emit(payload)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    backend = None
    try:
        backend = open_backend(args, args.kb_root)
        payload = reconcile_coverage(backend)
    finally:
        close_backend(backend)
    emit(payload)
    return 0 if payload["ok"] else 1


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "plan": cmd_plan,
    "run": cmd_run,
    "status": cmd_status,
    "reconcile": cmd_reconcile,
}


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
