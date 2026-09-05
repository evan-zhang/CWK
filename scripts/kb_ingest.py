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
import importlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
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

# 质量门 (DOCDB-INGEST-DESIGN §V, RT-046 反空转): a conversion that
# "succeeded" into an empty or near-empty document is a failure that hashes
# green.  Below this many characters the artefact becomes a placeholder with
# a stated reason and the receipt records the conversion as *not ok* — the
# lesson bd-eval-loop filed as DI-006 ("转成功但 0 字且静默").
MIN_DOCX_CHARS = 20
MIN_CONVERTED_CHARS = MIN_DOCX_CHARS
MAX_PLACEHOLDER_LISTING = 200

# RT-046 converter table: the versions this RT tested against.  A pin is a
# statement about what was tried, never a substitute for probing what is
# actually installed — the receipt always carries the *detected* version.
ANYDOC_PIN = "0.1.9"
MARKITDOWN_PIN = "0.1.7"

# What a receipt says when the host cannot tell us a version.  Written down
# rather than left out: "unknown" is a fact about this host, an absent field
# is a fact about our code.
UNKNOWN_VERSION = "unknown"

# In-tree converters (passthrough, the PDF text extractor) have no version of
# their own; they move with the rule set, so that is what the receipt shows.
BUILTIN_CONVERTER_VERSION = RULE_VERSION

# Placeholder reason codes that carry meaning downstream.  ``unknown-format``
# is the one the sniffing path is allowed to end on: it means "the bytes did
# not say", not "we did not look".
CODE_UNKNOWN_FORMAT = "unknown-format"
CODE_CONVERTER_MISSING = "converter-missing"


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

    format: str          # markdown | docx | xlsx | pdf | pptx | image | zip | archive | unknown
    handling: str        # passthrough | docx-convert | xlsx-csv | module-text | pdf-text | placeholder | skip
    expected_status: str  # converted | placeholder | skipped
    reason: str
    converter: str = "none"        # the chosen converter's receipt name
    code: str = ""                 # machine-readable placeholder reason
    sniffed: Optional[str] = None  # suffix the magic bytes yielded, if sniffed

    def as_dict(self) -> dict:
        return {
            "format": self.format,
            "handling": self.handling,
            "expected_status": self.expected_status,
            "reason": self.reason,
            "converter": self.converter,
            "code": self.code,
            "sniffed": self.sniffed,
        }


def docx_converter_path(env: Optional[Dict[str, str]] = None) -> str:
    source = os.environ if env is None else env
    return source.get(ENV_DOCX_CONVERTER) or DEFAULT_DOCX_CONVERTER


def docx_converter_available(env: Optional[Dict[str, str]] = None) -> bool:
    path = docx_converter_path(env)
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


# ── the converter table (RT-046) ────────────────────────────────────────────
#
# v1 bucketed suffixes inside one if-chain.  v1.1 makes the mapping a table
# because the question stopped being "which bucket" and became "which
# converter, and is it on this host" — the discipline ported from
# bd-eval-loop RT-108 ``TEXT_CONVERTER_BY_SUFFIX``: **no converter is best at
# every format**, so the choice is per format, written down, and reviewable.
#
# Three properties the table exists to keep:
#
# 1. Every suffix is claimed by exactly one row (:func:`index_format_table`
#    refuses a duplicate at import), so a format cannot be quietly handled in
#    two places with two verdicts.
# 2. A converter that is not installed changes the *outcome* (placeholder,
#    reason ``converter-missing:<format>``, recorded in both ledgers) and
#    never the *floor*: with nothing optional installed the pipeline is still
#    standard-library only and still green.
# 3. The chain is ordered and the first *available* converter wins, so a host
#    that gains a converter starts using it without an edit here — and the
#    receipt says which one actually ran.


@dataclass(frozen=True)
class Converter:
    """One way to turn original bytes into text, plus how to detect it."""

    name: str          # receipt name — stable across hosts
    handling: str      # what materialise() dispatches on
    kind: str          # builtin | host-binary | python-module
    module: str = ""   # import name (kind=python-module)
    dist: str = ""     # distribution name for importlib.metadata
    pin: str = ""      # the version RT-046 tested against ("" = no pin)


CONVERTERS: Dict[str, Converter] = {
    converter.name: converter
    for converter in (
        # 直通不是"没有转换器"：它是一个明确的选择（md/txt 转一道只会有损），
        # 所以它在表里有名字、进回执，和别的转换器一样。
        Converter("passthrough", "passthrough", "builtin"),
        # 本机 docx 转换器（RT-043 起就是这条），仍排在 anydoc 前面：它是这
        # 台机器上已验证的通路，装了 anydoc 也不该无声改变既有产物。
        Converter("docx-host", "docx-convert", "host-binary"),
        Converter("anydoc", "module-text", "python-module",
                  module="anydoc", dist="firecrawl-anydoc", pin=ANYDOC_PIN),
        # xlsx 首选 openpyxl(data_only=True)：RT-108 选 markitdown 的理由是
        # anydoc 会连公式的**缓存值**一起丢，而缓存值恰恰是结论（总额/CAGR/
        # 峰值）。openpyxl 的 data_only=True 读的正是那个缓存值，同一条不变
        # 量已由 test_xlsx_keeps_the_cached_formula_value 钉住；markitdown 作
        # 为没有 openpyxl 时的备选，而不是替换。
        Converter("openpyxl", "xlsx-csv", "python-module", module="openpyxl", dist="openpyxl"),
        Converter("markitdown", "module-text", "python-module",
                  module="markitdown", dist="markitdown", pin=MARKITDOWN_PIN),
        # PDF 走仓内纯标准库抽取（zlib + 内容流扫描）：能抽多少算多少，抽不
        # 出就占位并说明原因，不假装有正文。
        Converter("pdf-text", "pdf-text", "builtin"),
    )
}

# Handlings whose product is one markdown/text body (as opposed to the
# sheet-per-CSV shape).  Kept as a set so materialise() dispatches on the
# table instead of on a chain of format names.
TEXT_HANDLINGS = ("docx-convert", "module-text", "pdf-text")


@dataclass(frozen=True)
class FormatRow:
    """One format: which suffixes are it, and who may convert it."""

    format: str
    suffixes: Tuple[str, ...]
    chain: Tuple[str, ...] = ()    # ordered candidates; first available wins
    fallback: str = "placeholder"  # placeholder | skip, when the chain is empty/absent
    note: str = ""                 # why, in the operator's language


FORMAT_TABLE: Tuple[FormatRow, ...] = (
    FormatRow("markdown", PASSTHROUGH_EXTS, ("passthrough",), note="md/txt 直通，不做转换"),
    FormatRow("docx", (".docx",), ("docx-host", "anydoc")),
    FormatRow("xlsx", (".xlsx", ".xlsm"), ("openpyxl", "markitdown")),
    FormatRow("pdf", (".pdf",), ("pdf-text",)),
    FormatRow("pptx", (".pptx",), (), note="pptx 正文转换未列入 v1.1"),
    FormatRow("image", IMAGE_EXTS, (),
              note="图片按文件名/路径可检索，内容不可检索（v1 口径）"),
    FormatRow("zip", (".zip",), (), note="占位 + 中央目录清单，不解压"),
    FormatRow("archive", ARCHIVE_SKIP_EXTS, (), fallback="skip",
              note="v1 不承诺解包，跳过并记状态"),
)


def index_format_table(table: Sequence[FormatRow] = FORMAT_TABLE) -> Dict[str, FormatRow]:
    """``suffix → row``, refusing a suffix that two rows both claim.

    A duplicate would make the verdict depend on row order — i.e. on nothing
    a reader can see — which is exactly the failure mode the table replaced.
    """
    index: Dict[str, FormatRow] = {}
    for row in table:
        for suffix in row.suffixes:
            if suffix in index:
                raise IngestError(
                    f"格式表冲突：后缀 {suffix} 同时被 {index[suffix].format} 和 {row.format} 认领"
                )
            index[suffix] = row
    return index


FORMAT_BY_SUFFIX: Dict[str, FormatRow] = index_format_table()


def module_available(module: str) -> bool:
    """True when ``import <module>`` would work — without importing it.

    ``sys.modules`` is consulted first so a test can inject a stub converter
    and drive the whole path (probe → convert → receipt) on a host where the
    real optional package is absent.  That is the only way the degradation
    *and* the success branch can both be covered on a bare CI runner.
    """
    if not module:
        return False
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # namespace oddities, __spec__ = None
        return False


def openpyxl_available() -> bool:
    """True when xlsx sheets can be exported.  Optional import, never required.

    The pipeline is standard-library only; openpyxl is a host capability the
    same way the docx converter is.  Absent, xlsx tries the next converter in
    the table and then degrades to a placeholder the state ledger explains —
    it does not fail the batch.
    """
    return module_available("openpyxl")


def module_version(converter: Converter) -> str:
    """The *installed* version of an optional converter, or ``"unknown"``.

    Never read off ``pin``: a receipt that says 0.1.9 because the table says
    0.1.9 proves nothing about the bytes that were produced.  ``unknown`` is
    a legitimate answer and is recorded as one.
    """
    for dist in (converter.dist, converter.module):
        if dist:
            try:
                return str(importlib_metadata.version(dist))
            except Exception:  # noqa: BLE001 - not installed / no metadata
                pass
    module = sys.modules.get(converter.module)
    version = getattr(module, "__version__", "") if module is not None else ""
    return str(version) if version else UNKNOWN_VERSION


def converter_available(
    name: str,
    *,
    env: Optional[Dict[str, str]] = None,
    has_openpyxl: Optional[bool] = None,
) -> bool:
    """Can this host run the named converter right now?"""
    converter = CONVERTERS.get(name)
    if converter is None:
        raise IngestError(f"转换器表里没有 {name!r}（有 {sorted(CONVERTERS)}）")
    if converter.kind == "builtin":
        return True
    if converter.kind == "host-binary":
        return docx_converter_available(env)
    if converter.name == "openpyxl" and has_openpyxl is not None:
        return bool(has_openpyxl)  # explicit capability from the caller wins
    return module_available(converter.module)


def converter_version(name: str) -> str:
    """The version that goes into the receipt for the named converter."""
    converter = CONVERTERS.get(name)
    if converter is None:
        return UNKNOWN_VERSION
    if converter.kind == "builtin":
        return BUILTIN_CONVERTER_VERSION
    if converter.kind == "host-binary":
        # A host binary answers no version question we can trust (it may not
        # take --version at all), so the receipt says so and carries the path
        # in converter_label instead of inventing a number.
        return UNKNOWN_VERSION
    return module_version(converter)


def converter_label(decision: FormatDecision, env: Optional[Dict[str, str]] = None) -> str:
    """The human-facing converter string kept on the artefact (``xlsx:openpyxl``)."""
    if decision.converter == "docx-host":
        return "docx:" + docx_converter_path(env)
    if decision.converter in ("none", "passthrough"):
        return decision.converter
    return f"{decision.format}:{decision.converter}"


def converter_reason(row: FormatRow, converter: Converter, env: Optional[Dict[str, str]]) -> str:
    if converter.kind == "host-binary":
        return f"本机转换器可用：{docx_converter_path(env)}"
    if converter.kind == "builtin":
        return row.note or f"{row.format} 走仓内 {converter.name} 转换器"
    return f"{row.format} → {converter.name}（表内 pin {converter.pin or '无'}）"


# ── magic-byte sniffing (RT-046 自补，蓝本未覆盖) ────────────────────────────
#
# 投前库里三份关键流程文档根本没有后缀。v1 把它们判成 unknown 并占位，正文
# 因此不可问答。规则很短，也很克制：只有**名字没说**的件才嗅探，判据只有
# magic bytes，判不出就是 unknown-format（占位），绝不按文件名或大小猜。

SNIFF_HEAD_BYTES = 16
ZIP_MAGIC = b"PK\x03\x04"
MAGIC_PREFIXES: Tuple[Tuple[bytes, str], ...] = (
    (b"%PDF-", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
# OOXML is a zip whose member names say which application wrote it.
OOXML_CONTENT_TYPES = "[Content_Types].xml"
OOXML_MEMBER_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("word/", ".docx"),
    ("xl/", ".xlsx"),
    ("ppt/", ".pptx"),
)


def sniff_zip_container(data: bytes) -> Optional[str]:
    """``.docx`` / ``.xlsx`` / ``.pptx`` / ``.zip`` from the central directory.

    An unreadable directory returns ``None`` rather than ``.zip``: the name
    never claimed to be an archive, so there is nothing to hold it to.  (A
    file *named* ``.zip`` whose directory will not read is still a hard
    failure — DOCDB-INGEST-DESIGN §V; that claim came from the source.)
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except Exception:  # noqa: BLE001 - any malformed container
        return None
    for prefix, suffix in OOXML_MEMBER_PREFIXES:
        if any(name.startswith(prefix) for name in names):
            return suffix
    if OOXML_CONTENT_TYPES in names:
        return None  # OOXML of a kind this table does not know — 不猜
    return ".zip"


def sniff_suffix(data: bytes) -> Optional[str]:
    """The suffix the bytes imply, or ``None`` when they do not say."""
    head = data[:SNIFF_HEAD_BYTES]
    for magic, suffix in MAGIC_PREFIXES:
        if head.startswith(magic):
            return suffix
    if head.startswith(ZIP_MAGIC):
        return sniff_zip_container(data)
    return None


def decide_format(
    name: str,
    *,
    env: Optional[Dict[str, str]] = None,
    has_openpyxl: Optional[bool] = None,
    data: Optional[bytes] = None,
) -> FormatDecision:
    """Classify one item against the converter table.

    Host capabilities *and* the bytes are inputs, not global state: the same
    name decides differently on a host that has anydoc, and an extension-less
    file decides differently once its bytes are in hand.  ``data`` is absent
    at plan time (the plan does not download) and present at run time, which
    is why :func:`execute_plan` re-decides instead of trusting the card.
    """
    _, ext = split_name(name)
    sniffed: Optional[str] = None
    if not ext:
        if data is None:
            return FormatDecision(
                "unknown", "placeholder", "placeholder",
                "无后缀：计划期没有字节可读，摄取时按 magic bytes 嗅探再定",
                code=CODE_UNKNOWN_FORMAT,
            )
        sniffed = sniff_suffix(data)
        if sniffed is None:
            return FormatDecision(
                "unknown", "placeholder", "placeholder",
                "无后缀且 magic bytes 判不出，走通用占位路径（不猜）",
                code=CODE_UNKNOWN_FORMAT,
            )
        ext = sniffed
    row = FORMAT_BY_SUFFIX.get(ext)
    if row is None:
        return FormatDecision(
            "unknown", "placeholder", "placeholder",
            f"未知扩展名 {ext or '(无)'}，走通用占位路径",
            code=CODE_UNKNOWN_FORMAT, sniffed=sniffed,
        )
    for candidate in row.chain:
        if converter_available(candidate, env=env, has_openpyxl=has_openpyxl):
            converter = CONVERTERS[candidate]
            return FormatDecision(
                row.format, converter.handling, "converted",
                converter_reason(row, converter, env),
                converter=candidate, sniffed=sniffed,
            )
    if row.chain:
        # 反空转：转换器缺失必须降级 *并记账*。静默跳过会让"没转"和"不用转"
        # 在账上长得一模一样，装上转换器后也没人知道该重跑哪些件。
        return FormatDecision(
            row.format, "placeholder", "placeholder",
            f"{row.format} 的转换器都不在本机（表：{'/'.join(row.chain)}），降级占位并记账",
            code=f"{CODE_CONVERTER_MISSING}:{row.format}", sniffed=sniffed,
        )
    if row.fallback == "skip":
        return FormatDecision(
            row.format, "skip", "skipped", f"{ext} {row.note}", sniffed=sniffed
        )
    return FormatDecision(
        row.format, "placeholder", "placeholder", row.note,
        code=f"{row.format}-not-converted-in-v1", sniffed=sniffed,
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
    """What the factory produced for one item, ready to be written.

    ``converter`` is the human label (``docx:/opt/homebrew/bin/md2md``);
    ``converter_name`` / ``converter_version`` are the two receipt fields
    RT-046 requires — a label that mixes both is unusable for "which builds
    of which converter produced the bodies now in this library".
    ``conversion_ok`` is false for every attempt that produced no usable
    text, including the "转出 0 字" case: the item still lands (as a
    placeholder that says why), and the receipt still calls it a failure.
    """

    data: bytes
    kind: str                    # document | placeholder
    status: str                  # converted | placeholder
    converter: str
    placeholder_reason: Optional[str] = None
    extras: Dict[str, bytes] = field(default_factory=dict)
    converter_name: str = "placeholder"
    converter_version: str = BUILTIN_CONVERTER_VERSION
    conversion_ok: bool = True
    conversion_reason: str = ""


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


def convert_with_module(
    data: bytes,
    *,
    converter: Converter,
    suffix: str,
) -> Tuple[Optional[str], str]:
    """Run an optional Python converter (anydoc / markitdown) on the bytes.

    Returns ``(text or None, reason)`` — the same shape :func:`convert_docx`
    uses, so the caller does not care which kind of converter ran.  The bytes
    go to a temporary file with the *right suffix* because both converters
    route on it; handing markitdown a suffix-less path is how a sniffed docx
    would silently come back as plain text.

    Purity: file in, text out, no network and no model.  A conversion that
    an LLM performs cannot be reproduced from the receipt, which would make
    ``converter{name,version} + source sha + output sha`` decorative.
    """
    try:
        module = importlib.import_module(converter.module)
    except Exception as exc:  # noqa: BLE001 - not installed / broken install
        return None, f"{CODE_CONVERTER_MISSING}:{converter.name}:{exc}"[:200]
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / ("input" + (suffix or ""))
        source.write_bytes(data)
        try:
            if converter.name == "markitdown":
                text = module.MarkItDown().convert(str(source)).text_content
            else:
                text = module.to_markdown(str(source))
        except Exception as exc:  # noqa: BLE001 - any converter failure
            return None, f"{converter.name}-convert-failed:{exc}"[:200]
    text = (text or "").strip()
    if not text:
        return None, f"{converter.name}-convert-empty"
    if len(text) < MIN_CONVERTED_CHARS:
        return None, f"{converter.name}-convert-too-short"
    return text, f"{converter.name}-convert-ok"


# ── PDF text: standard library only ─────────────────────────────────────────
#
# 「纯标准库能做多少做多少，做不到就占位并记原因」。做得到的是：Flate 或未压
# 缩的内容流、文本算子（Tj/TJ/'/"）、以及子集字体的 ToUnicode CMap——最后这
# 项不是锦上添花：Word 导出的中文 PDF 一律是 Identity-H 子集字体，没有 CMap
# 就只能抽出 CID 乱码，也就是说「支持 PDF」会退化成「每份中文 PDF 都占位」。
# 做不到的是：扫描件（根本没有文本算子）、没有 ToUnicode 的子集字体、加密件。
# 三者都必须判成失败——一份乱码正文比占位更坏，它会进检索、进引文，而且哈希
# 永远是绿的。

PDF_MAGIC = b"%PDF-"
PDF_MIN_PRINTABLE_RATIO = 0.7
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_PDF_OBJECT = re.compile(rb"(\d+)\s+0\s+obj(.*?)endobj", re.DOTALL)
_PDF_TOUNICODE_REF = re.compile(rb"/ToUnicode\s+(\d+)\s+0\s+R")
_PDF_FONT_RESOURCE = re.compile(rb"/Font\s*<<(.*?)>>", re.DOTALL)
_PDF_FONT_ENTRY = re.compile(rb"/([A-Za-z0-9#+.\-]+)\s+(\d+)\s+0\s+R")
_PDF_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
_PDF_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
_PDF_CMAP_TOKEN = re.compile(rb"<([0-9A-Fa-f]*)>|(\[)|(\])")
_PDF_HEX_ONLY = re.compile(rb"<([0-9A-Fa-f]*)>")
_PDF_CODESPACE = re.compile(rb"begincodespacerange(.*?)endcodespacerange", re.DOTALL)
_PDF_CONTENTS = re.compile(rb"/Contents\s*(?:(\d+)\s+0\s+R|\[([^\]]*)\])")
_PDF_REFERENCE = re.compile(rb"(\d+)\s+0\s+R")
_PDF_OPERATOR_BYTES = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz*'\"")
_PDF_NAME_BYTES = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-.#_")
_PDF_NEWLINE_OPS = (b"Td", b"TD", b"T*", b"'", b'"', b"ET")
_PDF_ESCAPES = {b"n": "\n", b"r": "\r", b"t": "\t", b"b": "\b", b"f": "\f"}
_PDF_UNMAPPED = "�"


def _pdf_literal(stream: bytes, start: int) -> Tuple[bytes, int]:
    """Read a ``(...)`` string, honouring nesting and backslash escapes."""
    out = bytearray()
    depth = 1
    index = start + 1
    while index < len(stream) and depth:
        char = stream[index : index + 1]
        if char == b"\\":
            nxt = stream[index + 1 : index + 2]
            if nxt in _PDF_ESCAPES:
                out.extend(_PDF_ESCAPES[nxt].encode("latin-1"))
                index += 2
                continue
            if nxt.isdigit():
                octal = stream[index + 1 : index + 4]
                digits = bytes(byte for byte in octal if 0x30 <= byte <= 0x37)
                if digits:
                    out.append(int(digits, 8) & 0xFF)
                    index += 1 + len(digits)
                    continue
            out.extend(nxt)
            index += 2
            continue
        if char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if not depth:
                index += 1
                break
        out.extend(char)
        index += 1
    return bytes(out), index


def _pdf_hex(stream: bytes, start: int) -> Tuple[bytes, int]:
    end = stream.find(b">", start)
    if end < 0:
        return b"", len(stream)
    digits = bytes(byte for byte in stream[start + 1 : end] if byte in b"0123456789abcdefABCDEF")
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii")), end + 1
    except ValueError:  # pragma: no cover - filtered above
        return b"", end + 1


@dataclass(frozen=True)
class PdfCMap:
    """A font's ToUnicode table plus the code width its codespace declares.

    The width is read from the CMap, never inferred from the largest code:
    an Identity-H subset font whose glyphs happen to be numbered 1..20 is
    still addressed with two bytes, and decoding it one byte at a time
    yields exactly the kind of mojibake that passes a naive length check.
    """

    width: int
    table: Dict[int, str]


def _pdf_decode(raw: bytes, cmap: Optional[PdfCMap] = None) -> str:
    """Bytes of a shown string → text, through the font's CMap when there is one."""
    if cmap and cmap.table:
        width = cmap.width
        out: List[str] = []
        for start in range(0, len(raw) - width + 1, width):
            code = int.from_bytes(raw[start : start + width], "big")
            # An unmapped code becomes U+FFFD on purpose: it must show up in
            # the printable-ratio gate rather than vanish into a text that
            # looks complete and is not.
            out.append(cmap.table.get(code, _PDF_UNMAPPED))
        return "".join(out)
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("latin-1", "replace")


def _pdf_inflate(raw: bytes, *, plain_ok: bool = False) -> Optional[bytes]:
    """Decompress a stream body, or return it unchanged when it is plain.

    ``plain_ok`` is false for content streams: an undecompressable stream is
    usually an embedded image, and scanning its bytes as if they were text
    is how a JPEG turns into "正文".
    """
    body = raw.strip(b"\r\n")
    try:
        return zlib.decompress(body)
    except zlib.error:
        pass
    try:  # truncated / trailing-garbage flate streams still yield their head
        return zlib.decompressobj().decompress(body)
    except zlib.error:
        pass
    if plain_ok:
        return body
    return body if (b"BT" in body or b"Tj" in body or b"TJ" in body) else None


def _pdf_cmap(obj_body: bytes) -> Optional[PdfCMap]:
    """Parse a ToUnicode CMap object into a :class:`PdfCMap`."""
    match = _PDF_STREAM.search(obj_body)
    stream = _pdf_inflate(match.group(1), plain_ok=True) if match else obj_body
    if not stream:
        return None
    table: Dict[int, str] = {}
    widths: List[int] = []
    for block in _PDF_CODESPACE.findall(stream):
        widths += [max(1, len(token) // 2) for token in _PDF_HEX_ONLY.findall(block)]
    for block in _PDF_BFCHAR.findall(stream):
        codes = _PDF_HEX_ONLY.findall(block)
        for index in range(0, len(codes) - 1, 2):
            source = codes[index]
            widths.append(max(1, len(source) // 2))
            table[int(source, 16) if source else 0] = _pdf_utf16(codes[index + 1])
    for block in _PDF_BFRANGE.findall(stream):
        widths += [max(1, len(token) // 2) for token in _PDF_HEX_ONLY.findall(block)[:1]]
        table.update(_pdf_bfrange(block))
    table = {code: text for code, text in table.items() if text}
    if not table:
        return None
    return PdfCMap(width=max(widths) if widths else 1, table=table)


def _pdf_utf16(digits: bytes) -> str:
    raw = bytes.fromhex((digits + b"0" if len(digits) % 2 else digits).decode("ascii"))
    return raw.decode("utf-16-be", "replace") if len(raw) >= 2 else raw.decode("latin-1")


def _pdf_bfrange(block: bytes) -> Dict[int, str]:
    """``<lo> <hi> <dst>`` and ``<lo> <hi> [<d1> <d2> …]`` forms."""
    cmap: Dict[int, str] = {}
    pending: List[bytes] = []
    array: Optional[List[bytes]] = None
    for hexed, opener, closer in _PDF_CMAP_TOKEN.findall(block):
        if opener:
            array = []
            continue
        if closer:
            if array is not None and len(pending) >= 2:
                low = int(pending[0], 16)
                for offset, item in enumerate(array):
                    cmap[low + offset] = _pdf_utf16(item)
            pending, array = [], None
            continue
        if array is not None:
            array.append(hexed)
            continue
        pending.append(hexed)
        if len(pending) == 3:
            low, high, dst = (int(pending[0], 16), int(pending[1], 16), pending[2])
            base = _pdf_utf16(dst)
            for offset in range(0, max(0, high - low) + 1):
                if base and len(base) == 1:
                    cmap[low + offset] = chr(ord(base) + offset)
                else:
                    cmap[low + offset] = base
            pending = []
    return cmap


def _pdf_objects(data: bytes) -> Dict[int, bytes]:
    return {int(number): body for number, body in _PDF_OBJECT.findall(data)}


def _pdf_content_streams(objects: Dict[int, bytes]) -> List[bytes]:
    """The inflated page content streams, addressed through ``/Contents``.

    Structural, not heuristic, and that is the point: an embedded font file
    is also a Flate stream, and a large one will contain the byte pairs
    ``BT``/``Tj`` by chance.  Scanning every stream would feed those bytes to
    the text scanner and turn a perfectly readable document into mojibake
    that the quality gate then rejects wholesale.
    """
    streams: List[bytes] = []
    for number, body in sorted(objects.items()):
        if b"/Page" not in body:
            continue
        for single, array in _PDF_CONTENTS.findall(body):
            references = [single] if single else _PDF_REFERENCE.findall(array)
            for reference in references:
                target = objects.get(int(reference))
                if target is None:
                    continue
                match = _PDF_STREAM.search(target)
                stream = _pdf_inflate(match.group(1)) if match else None
                if stream:
                    streams.append(stream)
    return streams


def pdf_font_cmaps(data: bytes) -> Dict[str, PdfCMap]:
    """``{resource name (F1…): CMap}`` for every font that declares a ToUnicode.

    Resource names are resolved document-wide.  When one name points at two
    different font objects (possible across pages), the name is dropped
    instead of guessed: mapping CIDs through the wrong subset font produces
    text that reads plausibly and is wrong, which is worse than a placeholder.
    """
    objects = _pdf_objects(data)
    names: Dict[str, Optional[int]] = {}
    for body in objects.values():
        for block in _PDF_FONT_RESOURCE.findall(body):
            for name, number in _PDF_FONT_ENTRY.findall(block):
                key, target = name.decode("latin-1"), int(number)
                if key in names and names[key] != target:
                    names[key] = None
                else:
                    names[key] = target
    cmaps: Dict[str, PdfCMap] = {}
    for key, number in names.items():
        if number is None:
            continue
        reference = _PDF_TOUNICODE_REF.search(objects.get(number) or b"")
        if not reference:
            continue
        cmap = _pdf_cmap(objects.get(int(reference.group(1))) or b"")
        if cmap is not None:
            cmaps[key] = cmap
    return cmaps


def _pdf_stream_text(stream: bytes, fonts: Optional[Dict[str, PdfCMap]] = None) -> str:
    """Text-showing operators of one content stream, in order."""
    fonts = fonts or {}
    out: List[str] = []
    has_blocks = b"BT" in stream
    active = not has_blocks
    last_name = ""
    cmap: Optional[PdfCMap] = None
    index, size = 0, len(stream)
    while index < size:
        char = stream[index : index + 1]
        if char == b"(":
            raw, index = _pdf_literal(stream, index)
            if active:
                out.append(_pdf_decode(raw, cmap))
            continue
        if char == b"<" and stream[index + 1 : index + 2] != b"<":
            raw, index = _pdf_hex(stream, index)
            if active:
                out.append(_pdf_decode(raw, cmap))
            continue
        if char == b"/":
            start = index + 1
            index = start
            while index < size and stream[index] in _PDF_NAME_BYTES:
                index += 1
            last_name = stream[start:index].decode("latin-1")
            continue
        if stream[index] in _PDF_OPERATOR_BYTES:
            start = index
            while index < size and stream[index] in _PDF_OPERATOR_BYTES:
                index += 1
            token = stream[start:index]
            if token == b"Tf":
                cmap = fonts.get(last_name)
            elif token == b"BT":
                active = True
            elif token in _PDF_NEWLINE_OPS:
                if active:
                    out.append("\n")
                if token == b"ET":
                    active = not has_blocks
            continue
        index += 1
    return "".join(out)


def _printable_ratio(text: str) -> float:
    body = [char for char in text if not char.isspace()]
    if not body:
        return 0.0
    good = sum(1 for char in body if char.isprintable() and char != "�")
    return good / len(body)


def extract_pdf_text(data: bytes) -> Tuple[Optional[str], str]:
    """Pure-standard-library PDF text.  Returns ``(text or None, reason)``.

    The quality gate is the point, not the parser: a scanned page yields
    nothing (``pdf-text-empty``) and a subset-font page yields mojibake
    (``pdf-text-unreliable``).  Both must end as placeholders with a reason —
    admitting mojibake would put unreadable "正文" into the retrieval set and
    into citations, and it would hash green forever.
    """
    if not data.startswith(PDF_MAGIC):
        return None, "pdf-not-a-pdf"
    objects = _pdf_objects(data)
    fonts = pdf_font_cmaps(data)
    streams = _pdf_content_streams(objects)
    if not streams:
        # No page ever named its content (object streams, a shape this
        # extractor does not parse).  Falling back to "every stream that
        # looks like text" is deliberate and bounded: whatever it produces
        # still has to pass the quality gate below.
        streams = [
            stream
            for stream in (_pdf_inflate(raw) for raw in _PDF_STREAM.findall(data))
            if stream and b"begincmap" not in stream and b"BT" in stream
        ]
    chunks: List[str] = []
    for stream in streams:
        piece = _pdf_stream_text(stream, fonts)
        if piece.strip():
            chunks.append(piece)
    lines = [line.strip() for line in "\n".join(chunks).splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        return None, "pdf-text-empty"
    if _printable_ratio(text) < PDF_MIN_PRINTABLE_RATIO:
        return None, "pdf-text-unreliable"
    if len(text) < MIN_CONVERTED_CHARS:
        return None, "pdf-text-too-short"
    return text, "pdf-text-ok"


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


def convert_text(
    origin: bytes,
    *,
    name: str,
    decision: FormatDecision,
    env: Optional[Dict[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
) -> Tuple[Optional[str], str]:
    """Run the converter the table chose.  ``(text or None, reason)``."""
    if decision.handling == "docx-convert":
        return convert_docx(origin, env=env, runner=runner)
    if decision.handling == "pdf-text":
        return extract_pdf_text(origin)
    converter = CONVERTERS[decision.converter]
    suffix = decision.sniffed or split_name(name)[1]
    return convert_with_module(origin, converter=converter, suffix=suffix)


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
    version = converter_version(decision.converter)
    label = converter_label(decision, env)

    def degraded(reason: str, *, listing: Optional[Sequence[str]] = None) -> Artefact:
        """A placeholder that says what was tried and why it did not work."""
        return Artefact(
            data=placeholder_body(
                lineage=lineage, name=name, origin_sha=digest,
                origin_size=len(origin), reason=reason, listing=listing,
            ),
            kind="placeholder", status="placeholder", converter=label,
            placeholder_reason=reason, converter_name=decision.converter,
            converter_version=version, conversion_ok=False, conversion_reason=reason,
        )

    if decision.handling == "skip":
        return None
    if decision.handling == "passthrough":
        if not origin.strip():
            # 反空转，直通面：源件本身是空的。放行会在 raw 里留下一个 0 字正文，
            # 它照样进索引、照样对得上哈希，而检索侧永远问不出东西。
            return degraded("passthrough-empty")
        return Artefact(
            data=origin, kind="document", status="converted", converter="passthrough",
            converter_name="passthrough", converter_version=version,
            conversion_reason="passthrough-ok",
        )
    if decision.handling in TEXT_HANDLINGS:
        text, reason = convert_text(
            origin, name=name, decision=decision, env=env, runner=runner
        )
        if text is None:
            return degraded(reason)
        return Artefact(
            data=(text + "\n").encode("utf-8"), kind="document", status="converted",
            converter=label, converter_name=decision.converter,
            converter_version=version, conversion_reason=reason,
        )
    if decision.handling == "xlsx-csv":
        try:
            sheets = convert_xlsx(origin)
        except Exception as exc:  # noqa: BLE001 - any openpyxl failure
            return degraded(f"xlsx-unreadable:{exc}"[:200])
        if not sheets or not any(text.strip() for text in sheets.values()):
            return degraded("xlsx-convert-empty")
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
            status="converted", converter=label, extras=extras,
            converter_name=decision.converter, converter_version=version,
            conversion_reason=f"{decision.converter}-convert-ok",
        )
    # No converter ran: either the table lists none for this format (pptx /
    # image / zip) or none of the listed ones is on this host.  Both end in a
    # placeholder, and ``decision.code`` is what tells them apart in the
    # ledgers — ``converter-missing:docx`` is a re-run candidate the day the
    # converter arrives, ``image-not-converted-in-v1`` is not.
    listing = zip_listing(origin) if decision.format == "zip" else None
    reason = decision.code or (decision.format + "-not-converted-in-v1")
    return Artefact(
        data=placeholder_body(
            lineage=lineage, name=name, origin_sha=digest, origin_size=len(origin),
            reason=reason, listing=listing,
        ),
        kind="placeholder", status="placeholder", converter="placeholder",
        placeholder_reason=reason, converter_name="placeholder",
        converter_version=BUILTIN_CONVERTER_VERSION, conversion_ok=False,
        conversion_reason=reason,
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
            # overruled by a stale expectation.  This is also the first point
            # where the *bytes* exist, so it is where an extension-less item
            # gets sniffed (RT-046) — the plan could only say "无后缀".
            decision = decide_format(
                name, env=env, has_openpyxl=has_openpyxl, data=origin
            )
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
                        "derived": [],
                        "converter": {"name": "skip", "version": BUILTIN_CONVERTER_VERSION},
                        "converter_label": "skip",
                        "reason": decision.reason,
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
                    # RT-046 回执三链：converter{name,version} + 源 sha256 +
                    # 产物 sha256.  The label keeps the host detail (which
                    # binary, which path); the pair keeps the answerable
                    # question "which converter, which build".
                    "converter": {
                        "name": artefact.converter_name,
                        "version": artefact.converter_version,
                    },
                    "converter_label": artefact.converter,
                    "conversion": {
                        "ok": artefact.conversion_ok,
                        "reason": artefact.conversion_reason,
                        "format": decision.format,
                        "handling": decision.handling,
                        "sniffed": decision.sniffed,
                    },
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
