#!/usr/bin/env python3
"""Retrieve traceable evidence from the CWK Wiki and immutable raw layer.

The Wiki is used for navigation and recall.  Every returned evidence quote is
then verified against the linked raw report.  This command is intentionally
model-free and read-only so it can be used by an OpenClaw Agent without Docker,
network access, or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from cwk_ai_common import parse_frontmatter
import cwk_entity_catalog as entity_catalog


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"
SCHEMA = "cwk.wiki_query.v1"
REPORT_ID_RE = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")
SUMMARY_LINK_RE = re.compile(r"\[`?(\d{15,20})`?\]\([^)]*summaries/\1\.md\)")
EVIDENCE_RE = re.compile(r"证据：>\s*(.+?)\s*$", flags=re.M)
ASCII_RE = re.compile(r"[a-z0-9][a-z0-9._/+-]*", re.I)
CJK_RE = re.compile(r"[\u3400-\u9fff]+")
STOP_ASCII = {"the", "a", "an", "of", "to", "is", "are", "and", "or", "in", "on", "for"}
STOP_CJK = {
    "什么", "哪些", "怎么", "怎样", "如何", "一下", "这个", "那个", "我们", "关于",
    "目前", "现在", "最近", "情况", "介绍", "分析", "帮我", "给我", "是否", "有没有",
    "中旬", "月底", "月初", "月中", "月末", "月", "到", "至", "底",
}
_INDEX_CACHE: dict[tuple[str, int, int, int, int], tuple[Any, ...]] = {}

# ---------------------------------------------------------------------------
# Entity intent detection.  Rules are general and documented in
# RT/RT-010/specs/需求契约.md; do not add per-entity keywords here.
# ---------------------------------------------------------------------------
INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "progress": re.compile(r"(进展|进度|status|progress)", re.I),
    "plan": re.compile(r"(规划|计划|next\s*plan|next\s*step|下一步|路线图|后续)", re.I),
    "risk": re.compile(r"(风险|风控|risk|blocker|阻塞|事故|异常)", re.I),
    "owner": re.compile(r"(负责人|owner|负责|归属|承接人|谁负责)", re.I),
}
MANAGEMENT_INTENT_KEYS = tuple(INTENT_PATTERNS.keys())


def _date_sort_key(value: str) -> int:
    """Return a monotonically-comparable int for a ``YYYY-MM-DD`` string."""
    value = (value or "")[:10]
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else 0


@dataclass
class SummaryDoc:
    report_id: str
    title: str
    writer: str
    date: str
    source_lane: str
    summary_path: Path
    raw_path: Path
    body: str
    evidence_quotes: list[str] = field(default_factory=list)
    raw_sha256: str = ""

    @property
    def search_text(self) -> str:
        return f"{self.title}\n{self.title}\n{self.writer}\n{self.date}\n{self.body}"


@dataclass
class NavDoc:
    kind: str
    title: str
    path: Path
    body: str
    report_ids: list[str]

    @property
    def search_text(self) -> str:
        return f"{self.title}\n{self.title}\n{self.body}"


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"\s+", " ", value).strip()


def compact(value: str, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def tokenize(value: str) -> list[str]:
    """Tokenize English plus Chinese 2/3-grams without external dependencies."""
    value = normalize(value)
    tokens: list[str] = []
    for token in ASCII_RE.findall(value):
        token = token.strip("._/+-")
        if len(token) >= 2 and token not in STOP_ASCII:
            tokens.append(token)
    for sequence in CJK_RE.findall(value):
        for stop in sorted(STOP_CJK, key=len, reverse=True):
            sequence = sequence.replace(stop, " ")
        for part in sequence.split():
            if len(part) == 1:
                tokens.append(part)
                continue
            if len(part) <= 8:
                tokens.append(part)
            for size in (2, 3):
                if len(part) >= size:
                    tokens.extend(part[i : i + size] for i in range(len(part) - size + 1))
    return tokens


def resolve_raw_path(summary_path: Path, source: str) -> Path:
    source = str(source or "").strip().strip('"')
    return (summary_path.parent / source).resolve() if source else Path()


def heading_title(body: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", body, flags=re.M)
    return compact(match.group(1), 240) if match else fallback


def metadata_line(body: str, label: str) -> str:
    match = re.search(rf"^-\s*{re.escape(label)}：\s*(.+)$", body, flags=re.M)
    return compact(match.group(1), 120) if match else ""


def load_summaries(mirror: Path) -> list[SummaryDoc]:
    summaries: list[SummaryDoc] = []
    for path in sorted((mirror / "wiki" / "summaries").glob("*.md")):
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        report_id = compact(str(meta.get("report_id") or path.stem), 40)
        source = str(meta.get("source") or "")
        summaries.append(
            SummaryDoc(
                report_id=report_id,
                title=heading_title(body, path.stem),
                writer=metadata_line(body, "发送人"),
                date=metadata_line(body, "时间")[:10],
                source_lane=metadata_line(body, "来源类型").strip("`"),
                summary_path=path.resolve(),
                raw_path=resolve_raw_path(path, source),
                body=body,
                evidence_quotes=[compact(m.group(1), 500) for m in EVIDENCE_RE.finditer(body)],
                raw_sha256=str(meta.get("source_sha256") or ""),
            )
        )
    return summaries


def load_navigation(mirror: Path) -> list[NavDoc]:
    wiki = mirror / "wiki"
    rows: list[NavDoc] = []
    roots = (("topic", wiki / "topics"), ("entity", wiki / "entities"))
    for kind, root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.name == "index.md":
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            rows.append(
                NavDoc(
                    kind=kind,
                    title=heading_title(body, path.stem),
                    path=path.resolve(),
                    body=body,
                    # Only explicit links to source summaries establish graph
                    # edges.  Long employee/file/attachment IDs may appear in
                    # evidence quotes and must not be treated as report IDs.
                    report_ids=list(dict.fromkeys(SUMMARY_LINK_RE.findall(body))),
                )
            )
    return rows


def load_summary_quality(mirror: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Load coverage-vs-quality state without treating it as factual evidence."""
    path = mirror / "wiki" / "_system" / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, {"ai_refined": 0, "fallback_pending": 0, "fallback_terminal_error": 0, "unknown": 0}
    refined = {str(value) for value in payload.get("ai_refined_report_ids", [])}
    fallback = {str(value) for value in payload.get("fallback_report_ids", [])}
    terminal = {
        str(item.get("report_id"))
        for item in payload.get("failure_queue", [])
        if item.get("report_id") and int(item.get("attempts", 1)) >= 3
    }
    quality = {report_id: "ai_refined" for report_id in refined}
    quality.update({report_id: "fallback_pending" for report_id in fallback})
    quality.update({report_id: "fallback_terminal_error" for report_id in fallback & terminal})
    return quality, {
        "ai_refined": len(refined),
        "fallback_pending": len(fallback - terminal),
        "fallback_terminal_error": len(fallback & terminal),
        "unknown": 0,
    }


def load_precomputed_index(
    mirror: Path,
) -> tuple[list[SummaryDoc], list[NavDoc], "BM25", "BM25", dict[str, Any]] | None:
    """Load the persistent index, rejecting stale or internally inconsistent data."""
    compressed = mirror / "wiki" / "_system" / "search-index.json.gz"
    path = compressed if compressed.exists() else mirror / "wiki" / "_system" / "search-index.json"
    meta_path = mirror / "wiki" / "_system" / "index-meta.json"
    try:
        stat = path.stat()
        meta_stat = meta_path.stat() if meta_path.is_file() else None
        cache_key = (
            str(mirror.resolve()), stat.st_mtime_ns, stat.st_size,
            meta_stat.st_mtime_ns if meta_stat else 0,
            meta_stat.st_size if meta_stat else 0,
        )
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "cwk.search_index.v1":
            return None
        claimed_sha = str(payload.get("index_sha256") or "")
        core = {
            key: value for key, value in payload.items()
            if key not in {"index_version", "index_sha256", "generated_at"}
        }
        actual_sha = hashlib.sha256(
            json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not claimed_sha or claimed_sha != actual_sha:
            return None
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(meta.get("index_sha256") or "") != claimed_sha:
                return None
        summary_rows = payload["summary_docs"]
        nav_rows = payload["navigation_docs"]
        stats = payload["statistics"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    summaries: list[SummaryDoc] = []
    navigation: list[NavDoc] = []
    for row in summary_rows:
        summary_path = (mirror / str(row["summary_path"])).resolve()
        raw_path = (mirror / str(row["raw_path"])).resolve()
        summaries.append(
            SummaryDoc(
                report_id=str(row["report_id"]),
                title=str(row.get("title") or row["report_id"]),
                writer=str(row.get("writer") or ""),
                date=str(row.get("date") or ""),
                source_lane=str(row.get("source_lane") or ""),
                summary_path=summary_path,
                raw_path=raw_path,
                body="",
                evidence_quotes=[str(value) for value in row.get("evidence_quotes", [])],
                raw_sha256=str(row.get("raw_sha256") or ""),
            )
        )
    for row in nav_rows:
        navigation.append(
            NavDoc(
                kind=str(row.get("kind") or ""),
                title=str(row.get("title") or ""),
                path=(mirror / str(row["path"])).resolve(),
                body="",
                report_ids=[str(value) for value in row.get("report_ids", [])],
            )
        )
    if len(summaries) != len(summary_rows) or len(navigation) != len(nav_rows):
        return None
    summary_index = BM25.from_serialized(
        [row.get("term_counts", {}) for row in summary_rows],
        [int(row.get("length") or 0) for row in summary_rows],
        stats.get("summary_document_frequency", {}),
    )
    nav_index = BM25.from_serialized(
        [row.get("term_counts", {}) for row in nav_rows],
        [int(row.get("length") or 0) for row in nav_rows],
        stats.get("navigation_document_frequency", {}),
    )
    result = summaries, navigation, summary_index, nav_index, {
        "provider": "persistent_index",
        "index_version": payload.get("index_version"),
        "index_sha256": payload.get("index_sha256"),
        "entity_catalog_sha256": payload.get("entity_catalog_sha256"),
        "entity_catalog_schema": payload.get("entity_catalog_schema"),
        "entity_catalog_registry_version": payload.get("entity_catalog_registry_version"),
        "summary_quality_map": {
            str(row["report_id"]): str(row.get("summary_quality") or "unknown") for row in summary_rows
        },
        "summary_quality_counts": stats.get("summary_quality", {}),
    }
    # Retain only the current immutable generation for this mirror. The key
    # includes nanosecond mtimes and sizes, so any rebuild invalidates it.
    for key in [key for key in _INDEX_CACHE if key[0] == str(mirror.resolve()) and key != cache_key]:
        _INDEX_CACHE.pop(key, None)
    _INDEX_CACHE[cache_key] = result
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BM25:
    def __init__(self, texts: Iterable[str]):
        self.term_counts: list[Counter[str]] = []
        self.lengths: list[int] = []
        document_frequency: Counter[str] = Counter()
        for text in texts:
            counts = Counter(tokenize(text))
            self.term_counts.append(counts)
            length = sum(counts.values())
            self.lengths.append(length)
            document_frequency.update(counts.keys())
        self.n = len(self.term_counts)
        self.avg_len = sum(self.lengths) / max(1, self.n)
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in document_frequency.items()
        }

    @classmethod
    def from_serialized(
        cls,
        term_counts: list[dict[str, int]],
        lengths: list[int],
        document_frequency: dict[str, int],
    ) -> "BM25":
        instance = cls([])
        instance.term_counts = [Counter({str(term): int(value) for term, value in row.items()}) for row in term_counts]
        instance.lengths = [int(value) for value in lengths]
        instance.n = len(instance.term_counts)
        instance.avg_len = sum(instance.lengths) / max(1, instance.n)
        instance.idf = {
            str(term): math.log(1 + (instance.n - int(freq) + 0.5) / (int(freq) + 0.5))
            for term, freq in document_frequency.items()
        }
        return instance

    def score(self, query_tokens: list[str], index: int, k1: float = 1.5, b: float = 0.75) -> float:
        counts = self.term_counts[index]
        length = self.lengths[index]
        score = 0.0
        for term, qtf in Counter(query_tokens).items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            denominator = tf + k1 * (1 - b + b * length / max(1.0, self.avg_len))
            score += self.idf.get(term, 0.0) * (tf * (k1 + 1) / denominator) * min(2, qtf)
        return score

    def coverage(self, query_tokens: list[str], index: int) -> float:
        """IDF-weighted query coverage, including absent terms in denominator."""
        unique = set(query_tokens)
        if not unique:
            return 0.0
        absent_idf = math.log(1 + (self.n + 0.5) / 0.5)
        total = sum(self.idf.get(term, absent_idf) for term in unique)
        matched = sum(
            self.idf.get(term, absent_idf)
            for term in unique
            if term in self.term_counts[index]
        )
        return matched / total if total else 0.0


def normalized_contains(needle: str, haystack: str) -> bool:
    left = re.sub(r"\s+", "", needle or "")
    right = re.sub(r"\s+", "", haystack or "")
    return len(left) >= 4 and left in right


def raw_content(raw_text: str) -> str:
    _, body = parse_frontmatter(raw_text)
    match = re.search(r"<content>\s*(.*?)\s*</content>", body, flags=re.S | re.I)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return body.split("## List Row Metadata", 1)[0].strip()


def raw_evidence_haystack(raw_text: str) -> str:
    """Return the human-authored raw envelope used to verify citations.

    Evidence may legitimately cite the report title, guidance, content,
    opinion chain, or source metadata captured verbatim in raw.
    """
    _, body = parse_frontmatter(raw_text)
    return body.strip()


def excerpt_candidates(content: str) -> list[str]:
    parts = re.split(r"\n\s*\n|(?<=[。！？；])\s*", content)
    rows: list[str] = []
    for part in parts:
        part = part.strip().strip("#>*- ")
        if len(part) < 12:
            continue
        rows.append(compact(part, 500))
    return rows


def evidence_for(
    doc: SummaryDoc,
    question: str,
    max_evidence: int,
    raw_loader: Callable[[SummaryDoc], tuple[str, str]] | None = None,
    *,
    return_raw: bool = False,
):
    """Return ``(evidence_status, evidence, source_ref)``.

    When ``return_raw=True`` the return tuple is extended with the
    raw text and content-substring so callers (the structured entity-
    intent linkage helper) can reason about block-level position
    without re-reading the file.
    """
    if raw_loader:
        try:
            raw_text, source_ref = raw_loader(doc)
        except Exception as exc:
            status = "hash_mismatch" if "hash mismatch" in str(exc).lower() else "missing"
            if return_raw:
                return status, [], "", "", ""
            return status, [], ""
    else:
        if not doc.raw_path or not doc.raw_path.is_file():
            if return_raw:
                return "missing", [], "", "", ""
            return "missing", [], ""
        if doc.raw_sha256 and file_sha256(doc.raw_path) != doc.raw_sha256:
            if return_raw:
                return "hash_mismatch", [], str(doc.raw_path), "", ""
            return "hash_mismatch", [], str(doc.raw_path)
        raw_text = doc.raw_path.read_text(encoding="utf-8", errors="replace")
        source_ref = str(doc.raw_path)
    content = raw_content(raw_text)
    evidence_haystack = raw_evidence_haystack(raw_text)
    verified: list[dict[str, str]] = []
    for quote in doc.evidence_quotes:
        if normalized_contains(quote, evidence_haystack):
            verified.append({"kind": "summary_quote", "quote": quote})
        if len(verified) >= max_evidence:
            break
    if verified:
        if return_raw:
            return "verified", verified, source_ref, raw_text, content
        return "verified", verified, source_ref

    candidates = excerpt_candidates(content)
    if not candidates:
        if return_raw:
            return "missing", [], source_ref, raw_text, content
        return "missing", [], source_ref
    index = BM25(candidates)
    query_tokens = tokenize(question)
    ranked = sorted(
        ((index.score(query_tokens, i), value) for i, value in enumerate(candidates)),
        key=lambda item: (-item[0], len(item[1])),
    )
    excerpts = [
        {"kind": "raw_excerpt", "quote": value}
        for score, value in ranked[:max_evidence]
        if score > 0
    ]
    if not excerpts:
        if return_raw:
            return "unverified", [], source_ref, raw_text, content
        return "unverified", [], source_ref
    if return_raw:
        return "verified", excerpts, source_ref, raw_text, content
    return "verified", excerpts, source_ref


def date_ok(value: str, from_date: str, to_date: str) -> bool:
    if not value:
        return not (from_date or to_date)
    return (not from_date or value >= from_date) and (not to_date or value <= to_date)


def confidence_for(top_score: float, top_coverage: float, evidence_status: str, result_count: int) -> str:
    if result_count == 0 or top_score < 1.2 or top_coverage < 0.15:
        return "none"
    if evidence_status != "verified":
        return "none"
    if top_score >= 14 and top_coverage >= 0.45:
        return "high"
    if top_score >= 5 and top_coverage >= 0.20:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Entity resolution + scope enforcement.  Uses the deterministic catalog
# built by ``cwk_entity_catalog.build_catalog``; see RT-010 specs.
# ---------------------------------------------------------------------------


@dataclass
class EntityResolution:
    status: str  # unscoped | resolved | ambiguous | unknown | resolved_empty
    family_id: str = ""
    family_name: str = ""
    entity_types: list[str] = field(default_factory=list)
    matched_surfaces: list[dict[str, str]] = field(default_factory=list)
    candidate_family_ids: list[str] = field(default_factory=list)
    resolution_kind: str = ""  # exact | approved_alias | none
    reason: str = ""


def detect_intents(question: str) -> list[str]:
    """Return requested management intents in a stable order."""
    hits: list[str] = []
    for key in MANAGEMENT_INTENT_KEYS:
        if INTENT_PATTERNS[key].search(question or ""):
            hits.append(key)
    return hits


def _normalize_query_with_offset_map(question: str) -> tuple[str, list[int]]:
    """Return NFKC/case-folded text plus an offset map.

    ``offset_map[i]`` gives the start index in the original ``question``
    for the ``i``-th character of the normalised string; a trailing
    sentinel ``len(question)`` lets callers translate exclusive end
    offsets too.  Whitespace is **preserved** (as a single ASCII space)
    so that ``ALPHA risk`` normalises to ``alpha risk`` rather than
    ``alpharisk`` – otherwise the ASCII boundary check would treat the
    following ``r`` as a glued word char and reject the match.  Spaced
    acronyms like ``T B S`` are handled by a separate spaced-acronym
    pattern registered by :func:`_find_entity_matches`.
    """
    import unicodedata as _ud
    out_chars: list[str] = []
    offset_map: list[int] = []
    prev_was_space = True  # treat leading whitespace as already collapsed
    for i, ch in enumerate(question or ""):
        nfkc = _ud.normalize("NFKC", ch)
        for out_ch in nfkc.casefold():
            if out_ch.isspace():
                if prev_was_space:
                    continue
                out_chars.append(" ")
                offset_map.append(i)
                prev_was_space = True
            else:
                out_chars.append(out_ch)
                offset_map.append(i)
                prev_was_space = False
    offset_map.append(len(question or ""))
    return "".join(out_chars), offset_map


def _find_entity_matches(
    question: str, catalog: dict[str, Any]
) -> list[tuple[int, int, str, str, dict[str, Any]]]:
    """Scan the question for the longest catalog surface matches.

    Returns ``[(start, end, family_id, surface_display, family_payload)]``
    where ``(start, end)`` are offsets into the ORIGINAL question string.
    Matching is performed on the NFKC-normalised, case-folded,
    whitespace-collapsed representation of the question so that
    ``TBS``, ``ｔｂｓ``, and ``T B S`` all resolve to the same family.
    An offset map converts normalised match spans back to original
    spans for provenance / token removal.
    """
    families_by_id = {family["family_id"]: family for family in catalog.get("families", [])}
    surface_index = catalog.get("surface_index", {}) or {}
    ac = entity_catalog.AhoCorasick()
    ascii_acronym_re = re.compile(r"^[a-z0-9._-]{2,8}$")
    for norm, family_ids in surface_index.items():
        if not norm or len(norm) < 2:
            continue
        for family_id in family_ids:
            family = families_by_id.get(family_id)
            if family is None:
                continue
            surface = next(
                (s for s in family.get("surfaces", []) if s.get("normalized") == norm), None
            )
            display = surface.get("display") if surface else norm
            ac.add(norm, (family_id, norm, display or norm))
            # For short ASCII acronyms, also register a spaced variant
            # (``t b s``) so users typing the acronym with spaces still
            # hit the family.  The variant maps back to the same family
            # payload; overlap suppression keeps the longer match if
            # both fire.
            if ascii_acronym_re.match(norm) and len(norm) >= 2:
                spaced = " ".join(norm)
                if spaced != norm:
                    ac.add(spaced, (family_id, norm, display or norm))
    ac.finalize()
    normalised_question, offset_map = _normalize_query_with_offset_map(question)
    raw_hits: list[tuple[int, int, tuple[str, str, str]]] = []
    for n_start, n_end, payload in ac.find_all(normalised_question):
        surface_norm = payload[1]
        # Boundary is applied on the normalised string (both sides in
        # the same space).  The surface kind is derived from the
        # normalised form for consistency.
        kind = entity_catalog._surface_kind(surface_norm)
        if not entity_catalog._boundary_ok(normalised_question, n_start, n_end, kind):
            continue
        raw_hits.append((n_start, n_end, payload))
    kept = entity_catalog.suppress_shorter_overlaps(raw_hits)
    results: list[tuple[int, int, str, str, dict[str, Any]]] = []
    seen: set[tuple[int, int, str]] = set()
    for n_start, n_end, payload in kept:
        family_id, surface_norm, display = payload
        o_start = offset_map[n_start] if n_start < len(offset_map) else len(question)
        o_end = offset_map[n_end] if n_end < len(offset_map) else len(question)
        key = (o_start, o_end, family_id)
        if key in seen:
            continue
        seen.add(key)
        family = families_by_id.get(family_id)
        if family is None:
            continue
        results.append((o_start, o_end, family_id, display, family))
    return results


_GENERIC_RESIDUAL_RE = re.compile(
    r"(项目|系统|平台|体系|方案|情况|状态|进度|进展|规划|计划|风险|风控|阻塞|事故"
    r"|负责人|负责|owner|下一步|next\s*plan|next\s*step|路线图|后续|哪些|如何|怎么|谁"
    r"|本周|本月|本季度|今天|昨天|最近|现在|目前|团队|大家|各位|同事"
    r"|有|是|吗|呢|了|的|地|得|要|已|将|需|请"
    r"|status|progress|blocker|risk|plan|owner|team|update|report|summary)",
    re.I,
)
_QUOTED_NAME_RE = re.compile(r"[「『\"“《][^」』\"”》]{2,}[」』\"”》]")


def _query_looks_bare_entity(question: str) -> bool:
    """Return True only when the query is a bare ASCII acronym (or a
    bracket-quoted proper noun) after intent + generic scaffolding is
    stripped.  Used by the RT-010 catalog-binding fail-closed check to
    protect bare-entity-only queries (e.g. ``ALPHA``) without touching
    long CJK factual keywords such as ``基础事实``.
    """
    if not question:
        return False
    residual = question
    for pattern in INTENT_PATTERNS.values():
        residual = pattern.sub(" ", residual)
    residual = _GENERIC_RESIDUAL_RE.sub(" ", residual)
    tokens = [tok for tok in re.split(r"[\s，,。.:：；;!！?？]+", residual) if tok]
    tokens = [tok for tok in tokens if tok.lower() not in STOP_ASCII]
    residual = " ".join(tokens).strip()
    if not residual:
        return False
    if re.search(r"[A-Z][A-Z0-9._-]{1,}", residual):
        return True
    if _QUOTED_NAME_RE.search(question):
        return True
    return False


def _query_looks_entity_shaped(question: str) -> bool:
    """Return True only when the query names a specific proper noun.

    Detection strategy after stripping intent verbs and the closed
    generic-scaffolding vocabulary:

    - ASCII acronym (``[A-Z][A-Z0-9._-]+``) survives → entity-shaped;
    - Bracket-quoted proper name anywhere in the original query → true;
    - A CJK residual chunk of ``>=4`` characters after all scaffolding
      is stripped → true (long CJK sequences that survive the closed
      scaffolding list are almost always proper nouns).

    Ordinary management prose such as ``本周项目进展如何`` collapses to
    an empty residual and correctly stays ``False``.
    """
    if not question:
        return False
    residual = question
    for pattern in INTENT_PATTERNS.values():
        residual = pattern.sub(" ", residual)
    residual = _GENERIC_RESIDUAL_RE.sub(" ", residual)
    tokens = [tok for tok in re.split(r"[\s，,。.:：；;!！?？]+", residual) if tok]
    tokens = [tok for tok in tokens if tok.lower() not in STOP_ASCII]
    residual = " ".join(tokens).strip()
    if not residual:
        return False
    if re.search(r"[A-Z][A-Z0-9._-]{1,}", residual):
        return True
    if _QUOTED_NAME_RE.search(question):
        return True
    # Long CJK residual after scaffolding removal is almost certainly a
    # proper noun (e.g. ``霜蓝鲸鱼量子披萨-进展``).  The threshold of 4
    # keeps normal Chinese management prose out — that path already
    # collapses to an empty residual above.
    if re.search(r"[一-鿿]{4,}", residual):
        return True
    return False


def resolve_entity(question: str, catalog: dict[str, Any] | None) -> EntityResolution:
    """Resolve the query to a family (or explain the failure).

    The five states are documented in RT/RT-010/specs/需求契约.md.
    ``resolved_empty`` and ``ambiguous`` and ``unknown`` all return
    empty result sets with ``global_fallback_used=false``.
    """
    if catalog is None:
        # No catalog means we cannot enforce scope; treat as unscoped so
        # ordinary fact queries stay compatible with pre-RT-010 CLI use.
        return EntityResolution(status="unscoped", reason="catalog_unavailable")
    matches = _find_entity_matches(question, catalog)
    if not matches:
        # Fail-closed ONLY for entity-shaped **management-intent**
        # queries (e.g. ``霜蓝鲸鱼量子披萨-进展``, ``TBS 风险``).  A
        # bare factual query like ``API 接口测试`` names an acronym but
        # has no management intent, so it must stay unscoped and use
        # ordinary BM25 recall (RT-010 contract line 10).
        if bool(detect_intents(question)) and _query_looks_entity_shaped(question):
            return EntityResolution(status="unknown", reason="no_family_matched_query")
        return EntityResolution(status="unscoped", reason="no_entity_shape_in_query")
    # Group hits by family_id preserving order of first appearance.
    per_family: dict[str, list[tuple[int, int, str]]] = {}
    for start, end, family_id, display, _payload in matches:
        per_family.setdefault(family_id, []).append((start, end, display))
    family_ids = list(per_family.keys())
    if len(family_ids) > 1:
        # Distinguish two failure modes so downstream tools can react
        # correctly:
        #   * ``ambiguous`` – the SAME span matches multiple families
        #     (typical for same-name cross-type nodes without a registry
        #     entry).
        #   * ``unsupported_multi_entity`` – DIFFERENT spans match
        #     different families in one query.  RT-010 does not compute
        #     multi-entity intersections; we return this reason instead
        #     of silently conflating it with same-span ambiguity.
        spans_per_family = {
            fid: {(s, e) for s, e, _ in per_family[fid]}
            for fid in family_ids
        }
        shared_spans = set.intersection(*spans_per_family.values()) if spans_per_family else set()
        candidate_families = []
        for family_id in family_ids:
            fam = next(f for f in catalog["families"] if f["family_id"] == family_id)
            candidate_families.append(fam)
        status = "ambiguous" if shared_spans else "unsupported_multi_entity"
        reason = (
            "multiple_families_match_same_span"
            if shared_spans
            else "multiple_families_match_different_spans"
        )
        return EntityResolution(
            status=status,
            candidate_family_ids=family_ids,
            matched_surfaces=[
                {"family_id": fam["family_id"], "name": fam.get("canonical_surface", ""),
                 "entity_types": ",".join(fam.get("entity_types", []))}
                for fam in candidate_families
            ],
            reason=reason,
        )
    family_id = family_ids[0]
    family = next(f for f in catalog["families"] if f["family_id"] == family_id)
    matched_surfaces = [
        {"start": str(start), "end": str(end), "display": display}
        for start, end, display in per_family[family_id]
    ]
    surface_norms = {
        entity_catalog.normalize_surface(display) for _s, _e, display in per_family[family_id]
    }
    exact = any(
        s.get("normalized") in surface_norms and s.get("origin") == "declared"
        for s in family.get("surfaces", [])
    )
    if int(family.get("posting_count", 0)) == 0:
        return EntityResolution(
            status="resolved_empty",
            family_id=family_id,
            family_name=family.get("canonical_surface", ""),
            entity_types=list(family.get("entity_types", [])),
            matched_surfaces=matched_surfaces,
            resolution_kind="exact" if exact else "approved_alias",
            reason="no_indexed_reports",
        )
    return EntityResolution(
        status="resolved",
        family_id=family_id,
        family_name=family.get("canonical_surface", ""),
        entity_types=list(family.get("entity_types", [])),
        matched_surfaces=matched_surfaces,
        resolution_kind="exact" if exact else "approved_alias",
        reason="ok",
    )


def _remove_entity_tokens(question: str, resolution: EntityResolution) -> str:
    """Strip the resolved surfaces from the question by original span.

    Because ``_find_entity_matches`` returns spans keyed to the ORIGINAL
    question characters (post NFKC/casefold mapping), we can splice them
    out directly instead of running a regex that would miss full/half
    width or spaced variants.
    """
    spans: list[tuple[int, int]] = []
    for item in resolution.matched_surfaces:
        try:
            start = int(item.get("start") or 0)
            end = int(item.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end))
    if not spans:
        return question
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    pieces: list[str] = []
    cursor = 0
    for s, e in merged:
        pieces.append(question[cursor:s])
        pieces.append(" ")
        cursor = e
    pieces.append(question[cursor:])
    return "".join(pieces)


def _scope_support_for(
    doc: "SummaryDoc",
    family: dict[str, Any],
    *,
    raw_text: str = "",
) -> dict[str, Any]:
    entries = family.get("posting_provenance", {}).get(doc.report_id, [])
    surfaces: list[str] = []
    rules: list[str] = []
    origins: set[str] = set()
    # Collect summary-candidate quotes preserved by the entity catalog
    # so we can raw-verify them here; unverified quotes are dropped
    # rather than silently claimed as auditable evidence.  RT-010
    # hardening (independent audit blocker G): the catalog stored the
    # quote, but the query layer never checked it against raw.  Either
    # verify it or do not surface it.
    candidate_quotes: list[str] = []
    for entry in entries:
        origin = str(entry.get("origin") or "")
        origins.add(origin)
        # v2 compact provenance carries ``surfaces: [...]`` per origin;
        # older single-surface rows carry ``surface: "..."`` — accept
        # both shapes so a stale on-disk catalog does not break query.
        entry_surfaces = entry.get("surfaces") or []
        if not entry_surfaces and entry.get("surface"):
            entry_surfaces = [entry.get("surface")]
        for display in entry_surfaces:
            display = str(display or "")
            if display and display not in surfaces:
                surfaces.append(display)
        rule = str(entry.get("rule") or "")
        if rule and rule not in rules:
            rules.append(rule)
        # Preserve per-entry candidate quotes for raw-verification below.
        # v2 compact provenance stores ``quotes: [...]``; older single-
        # surface rows store ``quote: "..."`` — accept both shapes.
        entry_quotes = entry.get("quotes") or []
        if not entry_quotes and entry.get("quote"):
            entry_quotes = [entry.get("quote")]
        if origin == "summary_candidate":
            for quote in entry_quotes:
                q = str(quote or "").strip()
                if q:
                    candidate_quotes.append(q)
    if not rules and "summary_candidate" in origins:
        rules.append("summary_candidate")
    if "raw_anchor" in origins and "raw_anchor" not in rules:
        rules.append("raw_anchor")
    evidence_source = ""
    if "summary_candidate" in origins and "raw_anchor" in origins:
        evidence_source = "summary_candidate+raw_anchor"
    elif "summary_candidate" in origins:
        evidence_source = "summary_candidate"
    elif "raw_anchor" in origins:
        evidence_source = "raw_anchor"
    # Raw-verify candidate quotes so operators only see quotes that
    # actually appear in the raw envelope.  Without raw text we cannot
    # verify and therefore must drop them from the exposed provenance.
    verified_candidate_quotes: list[str] = []
    if candidate_quotes and raw_text:
        haystack = raw_evidence_haystack(raw_text)
        for quote in candidate_quotes:
            if quote in verified_candidate_quotes:
                continue
            if normalized_contains(quote, haystack):
                verified_candidate_quotes.append(quote)
    # Registry provenance surfaces per-family (authorised bridge) plus
    # any alias-generation rule that promoted the surface (parenthetical
    # acronym, controlled generic suffix, compound alias).
    surface_provenance: list[dict[str, str]] = []
    matched_norms = {
        entity_catalog.normalize_surface(s) for s in surfaces
    }
    for surface_row in family.get("surfaces", []) or []:
        if surface_row.get("normalized") not in matched_norms:
            continue
        for prov in surface_row.get("provenance", []) or []:
            surface_provenance.append(dict(prov))
    registry_provenance = [
        dict(row)
        for row in family.get("approved_family_evidence", []) or []
    ]
    return {
        "family_id": family.get("family_id", ""),
        "surfaces": surfaces,
        "rules": rules,
        "evidence_source": evidence_source,
        "in_scope": bool(surfaces),
        "surface_provenance": surface_provenance,
        "registry_evidence": registry_provenance,
        # ``candidate_quotes`` = raw-verified subset of the summary
        # candidate ``证据：>`` quotes preserved in the catalog.
        # Unverified quotes are intentionally dropped so downstream
        # tools cannot cite them.  ``candidate_quotes_verified`` is
        # the raw-verified count; ``candidate_quotes_total`` is what
        # the catalog stored, so operators can see the drop.
        "candidate_quotes": verified_candidate_quotes,
        "candidate_quotes_verified": len(verified_candidate_quotes),
        "candidate_quotes_total": len(candidate_quotes),
    }


# ---------------------------------------------------------------------------
# Block parsing + structured entity-intent linkage
# ---------------------------------------------------------------------------

# Bounded auditable window for entity/intent co-occurrence when no
# structural block relation applies.  Keep this small; larger windows
# stop being locally verifiable.
_INTENT_LINK_WINDOW = 256

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+·]|\d+[.．、)]|[（(]\d+[)）])\s+(.+)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_BLOCKQUOTE_RE = re.compile(r"^\s*>+\s*(.*)$")
# Structural boundaries that split intent from anchor even inside a
# single-newline neighbourhood: sentence terminators AND any newline
# (a plain ``\n`` is treated as a structural gap, not a continuation).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
# Inline heading marker split preprocessor.  A raw line that carries an
# inline ``## something`` marker without a leading newline (e.g. because
# the source flattener collapsed structure) must be split so the block
# parser can honour the section boundary; otherwise the whole flat line
# becomes a single paragraph and intent/anchor pairs inside it would
# spuriously satisfy ``same_block``.
_INLINE_HEADING_SPLIT_RE = re.compile(r"(?<=\S)\s+(?=#{1,6}\s+\S)")


def parse_raw_blocks(content: str) -> list[dict[str, Any]]:
    """Segment a raw ``<content>`` string into auditable blocks.

    Each block carries a ``kind`` (``heading | paragraph | list_item |
    table_row | blockquote``), the running heading path ancestors
    (``heading_path``), the block text, and start/end character
    offsets inside ``content``.  The parser is deliberately forgiving:

    - Standard Markdown headings ``#…######``.
    - Flattened Markdown fallbacks where a heading marker collapsed to
      just a hash-less line: any line that ends with ``：`` / ``:``
      and is followed by an indented / bulleted line is treated as a
      soft heading at the current depth + 1 so we still keep local
      linkage windows.
    - Blank line separates paragraphs.
    - List items and table rows each become their own block so a
      section-local intent hit does not accidentally leak into a
      neighbouring item.
    - Fenced code blocks are treated as opaque paragraphs.
    """
    blocks: list[dict[str, Any]] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    # Preprocess: insert a newline before any inline ``## `` heading
    # marker that appears mid-line so a flattened source cannot hide a
    # section boundary inside one paragraph.  Offsets after the split
    # shift by one per inserted newline; downstream callers use block
    # boundaries and per-block text, not raw content offsets, so this
    # is safe.  We deliberately do NOT rewrite existing offsets of any
    # external anchor list — surface_hits are always recomputed from
    # the (possibly split) content.
    if _INLINE_HEADING_SPLIT_RE.search(content):
        content = _INLINE_HEADING_SPLIT_RE.sub("\n", content)
    lines = content.splitlines(keepends=True)
    offset = 0
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def flush_paragraph(end_offset: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        if not paragraph_lines:
            return
        text = "".join(paragraph_lines).strip()
        if text:
            blocks.append(
                {
                    "kind": "paragraph",
                    "heading_path": [title for _, title in heading_stack],
                    "text": text,
                    "start": paragraph_start,
                    "end": end_offset,
                }
            )
        paragraph_lines = []

    in_code = False
    for line in lines:
        line_len = len(line)
        raw = line.rstrip("\n")
        stripped = raw.strip()
        # Fenced code block.
        if stripped.startswith("```"):
            if in_code:
                paragraph_lines.append(line)
                flush_paragraph(offset + line_len)
                in_code = False
            else:
                flush_paragraph(offset)
                in_code = True
                paragraph_lines = [line]
                paragraph_start = offset
            offset += line_len
            continue
        if in_code:
            paragraph_lines.append(line)
            offset += line_len
            continue
        # Blank line ⇒ paragraph boundary.
        if not stripped:
            flush_paragraph(offset)
            offset += line_len
            continue
        m_head = _HEADING_LINE_RE.match(raw)
        if m_head:
            flush_paragraph(offset)
            level = len(m_head.group(1))
            title = m_head.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(
                {
                    "kind": "heading",
                    "heading_path": [t for _, t in heading_stack],
                    "text": title,
                    "start": offset,
                    "end": offset + line_len,
                }
            )
            offset += line_len
            continue
        if _TABLE_ROW_RE.match(raw):
            flush_paragraph(offset)
            blocks.append(
                {
                    "kind": "table_row",
                    "heading_path": [t for _, t in heading_stack],
                    "text": stripped,
                    "start": offset,
                    "end": offset + line_len,
                }
            )
            offset += line_len
            continue
        m_bullet = _BULLET_LINE_RE.match(raw)
        if m_bullet:
            flush_paragraph(offset)
            blocks.append(
                {
                    "kind": "list_item",
                    "heading_path": [t for _, t in heading_stack],
                    "text": m_bullet.group(1).strip(),
                    "start": offset,
                    "end": offset + line_len,
                }
            )
            offset += line_len
            continue
        m_quote = _BLOCKQUOTE_RE.match(raw)
        if m_quote:
            flush_paragraph(offset)
            blocks.append(
                {
                    "kind": "blockquote",
                    "heading_path": [t for _, t in heading_stack],
                    "text": m_quote.group(1).strip(),
                    "start": offset,
                    "end": offset + line_len,
                }
            )
            offset += line_len
            continue
        # Flattened heading fallback: a text line ending in ``：`` /
        # ``:`` becomes a soft heading at current depth+1.  We only
        # treat it as a heading when the next non-blank line looks
        # like structured content (bullet / heading / table).  Since
        # we scan forward line-by-line, keep it simple: if the line
        # is short (<= 32 chars) and ends with a colon, treat as soft
        # heading.
        if len(stripped) <= 32 and stripped.endswith(("：", ":")):
            flush_paragraph(offset)
            level = heading_stack[-1][0] + 1 if heading_stack else 2
            title = stripped.rstrip("：:").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(
                {
                    "kind": "heading",
                    "heading_path": [t for _, t in heading_stack],
                    "text": title,
                    "start": offset,
                    "end": offset + line_len,
                }
            )
            offset += line_len
            continue
        # Ordinary paragraph accumulation.
        if not paragraph_lines:
            paragraph_start = offset
        paragraph_lines.append(line)
        offset += line_len
    flush_paragraph(offset)
    return blocks


def _find_surface_hits(
    content: str,
    surface_displays: list[str],
) -> list[tuple[int, int, str]]:
    """Return every ``(start, end, surface_display)`` for approved
    family surfaces inside ``content``, using the catalog's normalised-
    boundary rules and the standard longest-match suppression."""
    if not content or not surface_displays:
        return []
    hits: list[tuple[int, int, tuple[str]]] = []
    for display in surface_displays:
        if not display or len(display) < 2:
            continue
        for offset in entity_catalog.find_exact_anchors(content, display):
            hits.append((offset, offset + len(display), (display,)))
    kept = entity_catalog.suppress_shorter_overlaps(hits)
    return [(s, e, payload[0]) for s, e, payload in kept]


def _locate_block(
    blocks: list[dict[str, Any]], offset: int
) -> dict[str, Any] | None:
    for block in blocks:
        if block["start"] <= offset < block["end"]:
            return block
    return None


def _same_leaf_section(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True iff both blocks share the SAME leaf section – identical
    heading_path AND both are plain paragraph blocks.  List items,
    table rows, blockquotes and headings are structurally distinct
    from a neighbouring paragraph even under the same heading, so we
    intentionally exclude them: a bullet ``- 完成 ALPHA`` and a
    following bullet ``- 风险为 X`` are not the same section for
    linkage purposes.
    """
    if a.get("kind") != "paragraph" or b.get("kind") != "paragraph":
        return False
    pa = list(a.get("heading_path") or [])
    pb = list(b.get("heading_path") or [])
    if not pa and not pb:
        return True
    return pa == pb


def _heading_is_ancestor(anchor: dict[str, Any], intent_block: dict[str, Any]) -> bool:
    """True iff ``anchor`` is a HEADING block whose heading_path is a
    (non-strict) prefix of ``intent_block``'s heading_path.  Because the
    parser records a heading's own title inside its heading_path, the
    intent block that sits directly beneath the heading has the SAME
    heading_path — that counts as descendant here.

    A paragraph anchor never satisfies this rule: the entity-bearing
    block must itself be a heading for report-level heading linkage
    to be authorised.  This closes the leak where a paragraph mention
    of the entity would silently authorise a distant sibling intent.
    """
    if anchor.get("kind") != "heading":
        return False
    ap = list(anchor.get("heading_path") or [])
    ip = list(intent_block.get("heading_path") or [])
    if not ap:
        return False
    if len(ip) < len(ap):
        return False
    return ip[:len(ap)] == ap


def _same_sentence(text: str, a_off: int, b_off: int) -> bool:
    """True iff no sentence terminator AND no newline sits between the
    two offsets.  A plain ``\\n`` is treated as a structural boundary
    – callers combine this with ``same_block`` to keep the
    ``same_sentence`` relation strictly inside one paragraph.
    """
    lo, hi = min(a_off, b_off), max(a_off, b_off)
    fragment = text[lo:hi]
    return not _SENTENCE_SPLIT_RE.search(fragment)


def _no_structural_boundary_between(
    blocks: list[dict[str, Any]], lo: int, hi: int
) -> bool:
    """True iff every block strictly BETWEEN ``lo`` and ``hi`` offsets is
    a paragraph continuation (no heading, list item, table row or
    blockquote interposed).  Used when linking two paragraph blocks that
    share the same leaf section — even under the same heading we refuse
    to cross a bullet or a nested heading."""
    if lo > hi:
        lo, hi = hi, lo
    for block in blocks:
        if block["end"] <= lo:
            continue
        if block["start"] >= hi:
            break
        # A block entirely inside the (lo, hi) range that is not a
        # paragraph counts as a structural boundary.
        if block["start"] >= lo and block["end"] <= hi:
            if block.get("kind") != "paragraph":
                return False
    return True


def _snippet_around(content: str, start: int, end: int, radius: int = 40) -> str:
    lo = max(0, start - radius)
    hi = min(len(content), end + radius)
    snippet = content[lo:hi].replace("\n", " ").strip()
    return snippet[:200]


def compute_entity_intent_links(
    question: str,
    doc: "SummaryDoc",
    scope_family: dict[str, Any] | None,
    raw_text: str,
    raw_sha: str,
) -> tuple[dict[str, bool], dict[str, dict[str, Any]], bool]:
    """Return ``(intent_support, intent_links, has_raw_surface)``.

    Rules (RT-010 hardening):

    - ``same_block``: intent and anchor sit in the same parsed block.
    - ``same_sentence``: same block AND no sentence terminator or
      newline between them (a stricter refinement of ``same_block``).
    - ``heading_ancestor``: the anchor is inside a HEADING block whose
      heading_path is a prefix of the intent block's heading_path
      (i.e. the entity IS the section title, so its subtree may
      legitimately supply intent evidence).
    - ``same_leaf_section``: both are paragraph blocks sharing the
      exact same heading_path AND no structural block (heading, list
      item, table row, blockquote) sits between them AND the raw
      distance is bounded by ``_INTENT_LINK_WINDOW``.

    Every other pairing – sibling headings, adjacent list items, table
    rows, blockquotes, single-newline structural gaps – returns no
    relation, even when the raw distance is under 256 chars.  The old
    unconditional ``bounded_window`` fallback has been removed.

    - ``intent_support[intent]``: ``True`` iff at least one link
      satisfies the rules above.
    - ``intent_links[intent]``: single closest-provenance record with
      surface, anchor/intent offsets and snippets, relation, distance,
      and raw sha256.
    - ``has_raw_surface``: True iff any approved family surface anchor
      was found in raw at all.
    """
    intents = detect_intents(question)
    if not intents:
        return {}, {}, False
    surfaces: list[str] = []
    if scope_family:
        for surface_row in scope_family.get("surfaces", []) or []:
            display = str(surface_row.get("display") or "")
            if display and len(display) >= 2:
                surfaces.append(display)
    content = raw_content(raw_text)
    blocks = parse_raw_blocks(content)
    surface_hits = _find_surface_hits(content, surfaces)
    intent_hits_map: dict[str, list[tuple[int, int]]] = {}
    for intent in intents:
        pattern = INTENT_PATTERNS[intent]
        matches = [(m.start(), m.end()) for m in pattern.finditer(content)]
        intent_hits_map[intent] = matches
    support: dict[str, bool] = {intent: False for intent in intents}
    links: dict[str, dict[str, Any]] = {}
    relation_rank = {
        "same_sentence": 0,
        "same_block": 1,
        "heading_ancestor": 2,
        "same_leaf_section": 3,
    }
    for intent in intents:
        best_key: tuple[int, int] | None = None
        best_payload: dict[str, Any] | None = None
        for intent_start, intent_end in intent_hits_map[intent]:
            intent_block = _locate_block(blocks, intent_start)
            if intent_block is None:
                continue
            for anchor_start, anchor_end, surface_display in surface_hits:
                anchor_block = _locate_block(blocks, anchor_start)
                if anchor_block is None:
                    continue
                distance = abs(intent_start - anchor_start)
                relation: str | None = None
                if intent_block is anchor_block:
                    if _same_sentence(content, intent_start, anchor_start):
                        relation = "same_sentence"
                    else:
                        relation = "same_block"
                elif _heading_is_ancestor(anchor_block, intent_block):
                    relation = "heading_ancestor"
                elif (
                    _same_leaf_section(anchor_block, intent_block)
                    and _no_structural_boundary_between(
                        blocks,
                        min(anchor_start, intent_start),
                        max(anchor_start, intent_start),
                    )
                    and distance <= _INTENT_LINK_WINDOW
                ):
                    relation = "same_leaf_section"
                if relation is None:
                    continue
                candidate_key = (relation_rank[relation], distance)
                if best_key is None or candidate_key < best_key:
                    best_key = candidate_key
                    best_payload = {
                        "anchor_kind": "raw",
                        "surface": surface_display,
                        "anchor_block": anchor_block.get("kind"),
                        "anchor_heading_path": list(
                            anchor_block.get("heading_path") or []
                        ),
                        "anchor_offset": [int(anchor_start), int(anchor_end)],
                        "anchor_snippet": _snippet_around(
                            content, anchor_start, anchor_end
                        ),
                        "intent": intent,
                        "intent_block": intent_block.get("kind"),
                        "intent_heading_path": list(
                            intent_block.get("heading_path") or []
                        ),
                        "intent_offset": [int(intent_start), int(intent_end)],
                        "intent_snippet": _snippet_around(
                            content, intent_start, intent_end
                        ),
                        "relation": relation,
                        "distance": int(distance),
                        "raw_sha256": raw_sha,
                    }
        if best_payload is not None:
            support[intent] = True
            links[intent] = best_payload
    return support, links, bool(surface_hits)


def _extract_raw_h1(raw_text: str) -> str:
    """Return the first H1 heading from raw (post-frontmatter) body.

    We prefer the canonical raw H1 because summary titles are derived
    from the summary body and can drift (or be edited) independently of
    raw.  Using summary title as evidence would allow a forged summary
    to authorise "strong linkage" over an unrelated raw report.
    """
    body = raw_evidence_haystack(raw_text or "")
    match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.M)
    return match.group(1).strip() if match else ""


def _title_contains_family_surface(
    doc: "SummaryDoc",
    scope_family: dict[str, Any] | None,
    raw_text: str = "",
) -> bool:
    """Return True iff the CANONICAL raw title / H1 contains an
    approved family surface using the exact-boundary matcher.

    RT-010 hardening (independent audit blocker B):

    - Never rely on the summary-derived ``doc.title`` alone – a forged
      or drifted summary could steal strong linkage.  We require the
      raw H1, verified against raw text.
    - Never accept a bare ``substring`` – ``AB`` inside ``TABLE`` is
      not a strong-link signal.  We use
      :func:`entity_catalog.find_exact_anchors` which applies the
      approved boundary policy (ASCII word boundary or CJK longest-
      match suppression) exactly the same way the scope catalog does.
    - Only surfaces whose normalised form has length ≥ 2 are eligible;
      ultra-short 1-char surfaces are never a report-level anchor.
    """
    if not scope_family:
        return False
    raw_h1 = _extract_raw_h1(raw_text) if raw_text else ""
    if not raw_h1:
        return False
    # Collect all approved family displays sharing the family, then
    # apply exact-anchor matching in the raw H1.  ``suppress_shorter_
    # overlaps`` keeps the longest-match invariant so a glued shorter
    # surface never wins.
    hits: list[tuple[int, int, tuple[str]]] = []
    for surface_row in scope_family.get("surfaces", []) or []:
        display = str(surface_row.get("display") or "")
        if not display or len(display) < 2:
            continue
        for offset in entity_catalog.find_exact_anchors(raw_h1, display):
            hits.append((offset, offset + len(display), (display,)))
    return bool(entity_catalog.suppress_shorter_overlaps(hits))


def _row_has_strong_entity_linkage(
    doc: "SummaryDoc",
    scope_support: dict[str, Any],
    scope_family: dict[str, Any] | None,
    raw_text: str = "",
) -> bool:
    """Report-level strong linkage: the raw H1 contains an approved
    family surface (verified through the catalog's exact-boundary
    matcher).  All other rows are ``weak`` and must carry a structured
    local entity-intent link (see :func:`compute_entity_intent_links`).
    """
    return _title_contains_family_surface(doc, scope_family, raw_text)


def _row_intent_support(
    question: str,
    evidence: list[dict[str, Any]],
    scope_family: dict[str, Any] | None,
    strong_linkage: bool,
) -> dict[str, bool]:
    """Return ``{intent: verified}`` for the requested management intents.

    Strong-linked rows (see :func:`_row_has_strong_entity_linkage`)
    may claim intent support from any verified evidence quote in the
    report.  Weak-linked rows — the entity is only known via a raw
    substring anchor — must co-occur the intent keyword with an
    approved family surface **in the same evidence quote**, which
    approximates the bounded paragraph / section window that
    ``excerpt_candidates`` already carves the raw layer into.
    """
    intents = detect_intents(question)
    result: dict[str, bool] = {}
    if not intents:
        return result
    surface_cf: list[str] = []
    if scope_family:
        for surface_row in scope_family.get("surfaces", []) or []:
            display = str(surface_row.get("display") or "")
            if display and len(display) >= 2:
                surface_cf.append(display.casefold())
    verified_quotes = [
        str(ev.get("quote") or "")
        for ev in (evidence or [])
        if isinstance(ev, dict) and ev.get("quote")
    ]
    for intent in intents:
        pattern = INTENT_PATTERNS[intent]
        hit = False
        for quote in verified_quotes:
            if not pattern.search(quote):
                continue
            if strong_linkage:
                hit = True
                break
            quote_cf = quote.casefold()
            if any(surf in quote_cf for surf in surface_cf):
                hit = True
                break
        result[intent] = hit
    return result


def _intent_verified_for(
    question: str, verified_evidence_quotes: list[str]
) -> dict[str, bool]:
    """Check whether each requested intent has a structured evidence hit.

    Only quotes that already passed raw verification (either an exact
    summary quote confirmed against raw, or a BM25-selected raw excerpt
    returned in ``evidence``) count.  We deliberately never scan the
    full raw file here – that would let a single generic keyword make
    every result look ``high``.

    Retained for backwards-compatible callers; new scoped-management
    code should use :func:`_row_intent_support`, which additionally
    enforces entity-surface co-occurrence for weak raw-only anchors.
    """
    requested = detect_intents(question)
    verified: dict[str, bool] = {}
    for intent in requested:
        pattern = INTENT_PATTERNS[intent]
        verified[intent] = any(
            pattern.search(quote or "") for quote in verified_evidence_quotes
        )
    return verified


def _safe_empty_payload(
    question: str,
    resolution: EntityResolution,
    *,
    filters_removed_all: bool = False,
    mirror: Path | None = None,
    catalog_bound: bool = True,
    reason_override: str = "",
) -> dict[str, Any]:
    reason = reason_override or resolution.reason or resolution.status
    if filters_removed_all:
        reason = "filters_removed_all"
    return {
        "schema_version": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mirror_root": str(mirror) if mirror else "",
        "query": question,
        "confidence": "none",
        "entity_resolution": {
            "status": resolution.status,
            "family_id": resolution.family_id,
            "family_name": resolution.family_name,
            "entity_types": resolution.entity_types,
            "matched_surfaces": resolution.matched_surfaces,
            "candidate_family_ids": resolution.candidate_family_ids,
            "resolution_kind": resolution.resolution_kind,
            "reason": reason,
        },
        "scope": {
            "applied": bool(resolution.family_id),
            "postings_size": 0,
            "filtered_out": 0,
            "scope_hash": "",
            "filter_reasons": [reason],
        },
        "intents": {
            "requested": detect_intents(question),
            "verified": [],
            "missing": detect_intents(question),
        },
        "support": {
            "source_integrity": "unassessed",
            "scope_integrity": "verified" if resolution.status == "resolved_empty" else "unassessed",
            "query_support": "insufficient",
        },
        "answer_policy": "Entity recognised but no matching reports; abstain."
        if resolution.status == "resolved_empty"
        else "No entity scope could be established; abstain or clarify.",
        "global_fallback_used": False,
        "catalog_bound": catalog_bound,
        "results": [],
    }


def query_mirror(
    mirror: Path,
    question: str,
    *,
    top_k: int = 8,
    max_evidence: int = 3,
    from_date: str = "",
    to_date: str = "",
    writer: str = "",
    kind: str = "all",
    min_score: float = 0.1,
    use_index: bool = True,
    raw_loader: Callable[[SummaryDoc], tuple[str, str]] | None = None,
    require_catalog: bool = False,
) -> dict[str, Any]:
    precomputed = load_precomputed_index(mirror) if use_index else None
    if precomputed:
        summaries, navigation, summary_index, nav_index, index_info = precomputed
        summary_quality = index_info.pop("summary_quality_map", {})
        quality_counts = index_info.pop("summary_quality_counts", {})
    else:
        summaries = load_summaries(mirror)
        navigation = load_navigation(mirror)
        summary_index = BM25(doc.search_text for doc in summaries)
        nav_index = BM25(doc.search_text for doc in navigation)
        index_info = {"provider": "live_scan", "index_version": None, "index_sha256": None}
        summary_quality, quality_counts = load_summary_quality(mirror)
    quality_counts["unknown"] = sum(1 for doc in summaries if doc.report_id not in summary_quality)

    catalog = entity_catalog.load_catalog(mirror)
    expected_sha = str(index_info.get("entity_catalog_sha256") or "")
    actual_sha = str((catalog or {}).get("catalog_sha256") or "")
    catalog_bound = False
    catalog_mismatch = False
    persistent_index = index_info.get("provider") == "persistent_index"
    if catalog is not None:
        if expected_sha:
            catalog_bound = expected_sha == actual_sha
            catalog_mismatch = not catalog_bound
        else:
            # Live-scan callers cannot carry a bound sha; treat the
            # loaded catalog as usable but note that binding is
            # implicit.  A persistent index without a bound sha is
            # from a pre-RT-010 build and is treated as a mismatch so
            # entity-management queries fail closed rather than
            # silently trusting an unbound catalog.
            if persistent_index:
                catalog_mismatch = True
            else:
                catalog_bound = index_info.get("provider") == "live_scan"
    else:
        # Persistent index declared a catalog hash but no catalog is
        # available on disk – this is a mismatch, not a soft-miss.
        # Or the persistent index has no sha at all (old build): still
        # a mismatch for entity-management purposes.
        catalog_mismatch = bool(expected_sha) or persistent_index
    # RT-010 hardening (independent audit blocker E): any explicit
    # entity-resolved query – including a bare entity-only query like
    # ``ALPHA`` – must fail closed when the persistent index is not
    # bound to a compatible catalog sha, so operators cannot get an
    # unscoped BM25 fallback masquerading as a resolved answer.  Plain
    # non-entity fact queries stay on the historical BM25 path so this
    # does not regress the general recall surface.  A bare-entity
    # signal is *strict*: an ASCII acronym or a bracket-quoted proper
    # noun.  Long CJK residuals are ambiguous (a factual keyword can
    # exceed 4 chars) so the strict path deliberately excludes them.
    entity_shaped = _query_looks_entity_shaped(question)
    bare_entity = _query_looks_bare_entity(question)
    entity_management_query = bool(detect_intents(question)) and entity_shaped
    if catalog_mismatch and (require_catalog or entity_management_query or bare_entity):
        resolution = EntityResolution(
            status="unknown", reason="catalog_index_hash_mismatch_or_missing"
        )
        return _safe_empty_payload(
            question, resolution, mirror=mirror, catalog_bound=False,
            reason_override="catalog_index_hash_mismatch_or_missing",
        )
    if require_catalog and not catalog_bound:
        resolution = EntityResolution(
            status="unknown", reason="catalog_unavailable_but_required"
        )
        return _safe_empty_payload(
            question, resolution, mirror=mirror, catalog_bound=False,
            reason_override="catalog_unavailable_but_required",
        )
    resolution = resolve_entity(question, catalog if catalog_bound else None)

    if resolution.status in {"ambiguous", "unknown", "resolved_empty", "unsupported_multi_entity"}:
        payload = _safe_empty_payload(
            question, resolution, mirror=mirror, catalog_bound=catalog_bound
        )
        payload["indexed"] = {
            "summaries": len(summaries),
            "navigation_pages": len(navigation),
            "summary_quality": quality_counts,
            **index_info,
        }
        return payload

    query_tokens = tokenize(question)
    if not query_tokens:
        return {
            "schema_version": SCHEMA,
            "query": question,
            "confidence": "none",
            "reason": "query_has_no_searchable_tokens",
            "entity_resolution": {
                "status": resolution.status,
                "family_id": resolution.family_id,
                "family_name": resolution.family_name,
                "entity_types": resolution.entity_types,
                "resolution_kind": resolution.resolution_kind,
                "reason": resolution.reason,
            },
            "global_fallback_used": False,
            "results": [],
        }

    scope_active = resolution.status == "resolved"
    scope_family: dict[str, Any] | None = None
    scope_postings: set[str] = set()
    scope_hash = ""
    residual_query_empty = False
    if scope_active:
        scope_family = next(
            (f for f in catalog["families"] if f["family_id"] == resolution.family_id), None
        )
        if scope_family is None:
            resolution = EntityResolution(status="unknown", reason="family_missing_from_catalog")
            return _safe_empty_payload(
                question, resolution, mirror=mirror, catalog_bound=catalog_bound
            )
        scope_postings = {str(rid) for rid in scope_family.get("postings", [])}
        scope_hash = str(scope_family.get("scope_hash") or "")
        # Drop the entity tokens so nav/BM25 don't double-count them.
        residual_query = _remove_entity_tokens(question, resolution)
        scoped_tokens = tokenize(residual_query)
        # If the residual is empty (query was entity-only, e.g. ``TBS``)
        # do NOT fall back to entity tokens.  We already know which
        # scope to serve; ranking will fall back to date/quality below.
        residual_query_empty = not scoped_tokens
        query_tokens = scoped_tokens

    nav_bonus: defaultdict[str, float] = defaultdict(float)
    nav_hits: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    if not scope_active and kind in {"all", "topic", "entity"}:
        ranked_nav = sorted(
            ((nav_index.score(query_tokens, i), i) for i in range(len(navigation))),
            reverse=True,
        )[:12]
        for rank, (score, index_value) in enumerate(ranked_nav):
            if score <= 0:
                continue
            nav = navigation[index_value]
            bonus = min(8.0, score) / (rank + 1)
            for report_id in nav.report_ids:
                nav_bonus[report_id] += bonus
                nav_hits[report_id].append(
                    {"kind": nav.kind, "title": nav.title, "path": str(nav.path), "score": round(score, 4)}
                )

    normalized_question = normalize(question)
    requested_ids = set(REPORT_ID_RE.findall(question))
    ranked: list[tuple[float, float, SummaryDoc]] = []
    total_scanned = 0
    scope_filtered_out = 0
    date_or_writer_filtered = 0
    for i, doc in enumerate(summaries):
        if scope_active and doc.report_id not in scope_postings:
            scope_filtered_out += 1
            continue
        if not date_ok(doc.date, from_date, to_date):
            date_or_writer_filtered += 1
            continue
        if writer and normalize(writer) not in normalize(doc.writer):
            date_or_writer_filtered += 1
            continue
        total_scanned += 1
        if scope_active and residual_query_empty:
            # Entity-only query with nothing left to rank on; keep the
            # doc but score it neutrally so we can deterministically
            # order by date/quality below.
            ranked.append((0.0, 0.0, doc))
            continue
        score = summary_index.score(query_tokens, i)
        title_norm = normalize(doc.title)
        if normalized_question and normalized_question in title_norm:
            score += 18.0
        if title_norm and title_norm in normalized_question:
            score += 12.0
        if requested_ids and doc.report_id in requested_ids:
            score += 50.0
        if not scope_active:
            score += nav_bonus.get(doc.report_id, 0.0)
        coverage = summary_index.coverage(query_tokens, i)
        if score >= min_score:
            ranked.append((score, coverage, doc))

    if scope_active and residual_query_empty:
        # Sort deterministically: newest first, then AI-refined before
        # fallback, then report_id ascending.
        quality_rank = {"ai_refined": 0, "fallback_pending": 1,
                        "fallback_terminal_error": 2, "unknown": 3}
        def entity_only_key(item: tuple[float, float, SummaryDoc]) -> tuple[str, int, str]:
            doc = item[2]
            return (
                doc.date or "0000-00-00",
                quality_rank.get(summary_quality.get(doc.report_id, "unknown"), 9),
                doc.report_id,
            )
        ranked.sort(key=lambda item: (
            entity_only_key(item)[0],
            entity_only_key(item)[1],
            entity_only_key(item)[2],
        ), reverse=False)
        # Reverse date to newest-first while keeping quality asc and
        # report_id asc as tie-breakers.
        ranked = sorted(
            ranked,
            key=lambda item: (
                -_date_sort_key(item[2].date),
                quality_rank.get(summary_quality.get(item[2].report_id, "unknown"), 9),
                item[2].report_id,
            ),
        )
    else:
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2].date or "9999-99-99", item[2].report_id))

    if scope_active and not ranked:
        empty_reason = (
            "filters_removed_all" if date_or_writer_filtered else "no_query_evidence_in_scope"
        )
        # Promote the resolver to ``resolved_empty`` so consumers can
        # distinguish an entity that was recognised but has no matching
        # reports (either because filters ate the scope or because no
        # returned doc satisfied the residual query) from a genuinely
        # unknown entity.  The `reason` field carries the more specific
        # sub-code.
        resolution.status = "resolved_empty"
        resolution.reason = empty_reason
        payload = _safe_empty_payload(
            question,
            resolution,
            mirror=mirror,
            catalog_bound=catalog_bound,
            filters_removed_all=empty_reason == "filters_removed_all",
            reason_override=empty_reason,
        )
        payload["indexed"] = {
            "summaries": len(summaries),
            "navigation_pages": len(navigation),
            "summary_quality": quality_counts,
            **index_info,
        }
        payload["scope"] = {
            "applied": True,
            "postings_size": len(scope_postings),
            "filtered_out": scope_filtered_out,
            "scope_hash": scope_hash,
            "filter_reasons": [empty_reason],
        }
        return payload

    intents_requested = detect_intents(question)

    # ----- Iterative bounded evaluation -----
    # A scoped management query with many high-BM25 unverified (or
    # verified-but-no-intent) rows could hide a raw-verified intent-
    # supporting row past any single fixed bounded pool.  Evaluate in
    # deterministic batches of ``max(top_k * 4, 32)`` inside the scoped
    # candidate list until we either have enough verified rows to fill
    # ``top_k`` AND at least one intent-supporting row, or the scoped
    # candidate list is fully exhausted.  For plain / unscoped queries
    # we keep the historical ``ranked[:top_k]`` cheap path so latency
    # does not regress.
    is_scoped_mgmt = (
        scope_active and resolution.status == "resolved" and bool(intents_requested)
    )
    batch_size = max(top_k * 4, 32) if is_scoped_mgmt else min(len(ranked), top_k)

    def _evaluate_row(score: float, coverage: float, doc: SummaryDoc) -> dict[str, Any]:
        # Load raw text once so we can share it between evidence and
        # linkage helpers.  Falls back to the legacy 3-tuple when the
        # extended return is not available (e.g. under a mocked
        # ``evidence_for``).
        extended = evidence_for(
            doc, question, max_evidence, raw_loader, return_raw=True
        )
        if len(extended) == 5:
            evidence_status, evidence, source_ref, raw_text, raw_content_str = extended
        else:
            evidence_status, evidence, source_ref = extended
            raw_text = raw_content_str = ""
        scope_support = (
            _scope_support_for(doc, scope_family, raw_text=raw_text)
            if scope_active and scope_family else {}
        )
        title_strong = _title_contains_family_surface(
            doc, scope_family, raw_text=raw_text
        )
        intent_hits: dict[str, bool] = {}
        intent_links: dict[str, dict[str, Any]] = {}
        raw_has_anchor = False
        if intents_requested and evidence_status == "verified" and raw_text:
            support, links, raw_has_anchor = compute_entity_intent_links(
                question, doc, scope_family, raw_text, doc.raw_sha256 or "",
            )
            if title_strong:
                # Report-level strong linkage: any verified evidence
                # quote may support the intent.  Structured link
                # provenance still records the closest raw anchor
                # so operators can audit the local mapping too.
                verified_quotes = [
                    str(ev.get("quote") or "")
                    for ev in (evidence or [])
                    if isinstance(ev, dict) and ev.get("quote")
                ]
                fallback_support = _intent_verified_for(question, verified_quotes)
                for intent, verified in fallback_support.items():
                    if verified and not support.get(intent):
                        support[intent] = True
                        links.setdefault(
                            intent,
                            {
                                "anchor_kind": "title",
                                "surface": doc.title,
                                "anchor_block": "heading",
                                "anchor_heading_path": [doc.title],
                                "intent": intent,
                                "intent_block": "evidence_quote",
                                "intent_heading_path": [],
                                "relation": "title_report_level",
                                "distance": 0,
                                "raw_sha256": doc.raw_sha256 or "",
                            },
                        )
            intent_hits = support
            intent_links = links
        # Compute the row's final linkage tier for confidence gating:
        #   ``strong`` = title/H1 contains an approved family surface.
        #   ``local``  = weak linkage but the row has at least one
        #                intent locally linked to a raw anchor.
        #   ``weak``   = neither (intent verification not authorised).
        if title_strong:
            linkage_tier = "strong"
        elif intent_links:
            linkage_tier = "local"
        else:
            linkage_tier = "weak"
        return {
            "report_id": doc.report_id,
            "title": doc.title,
            "writer": doc.writer,
            "date": doc.date,
            "source_lane": doc.source_lane,
            "summary_quality": summary_quality.get(doc.report_id, "unknown"),
            "score": round(score, 4),
            "query_coverage": round(coverage, 4),
            "summary_path": str(doc.summary_path),
            "raw_path": source_ref or str(doc.raw_path),
            "navigation_hits": nav_hits.get(doc.report_id, [])[:5],
            "evidence_status": evidence_status,
            "evidence": evidence,
            "scope_support": scope_support,
            "intent_support": intent_hits,
            "intent_links": intent_links,
            "entity_linkage": linkage_tier,
            "raw_has_family_surface": raw_has_anchor,
        }

    results: list[dict[str, Any]] = []
    cursor = 0
    total_ranked = len(ranked)
    intents_requested_set = set(intents_requested)
    while cursor < total_ranked:
        # In the scoped-management path, keep pulling batches until
        # either scope is exhausted or we have gathered enough evidence
        # to answer.  RT-010 hardening (independent audit blocker C):
        # a compound intent query must not stop after ONE intent has a
        # candidate.  We continue until either every requested intent
        # has at least one raw-verified supporting row OR the scope is
        # fully exhausted.  Non-management paths still consume exactly
        # one batch (the historical cheap path).
        if is_scoped_mgmt and results:
            verified_now = [r for r in results if r["evidence_status"] == "verified"]
            supported_intents: set[str] = set()
            for r in verified_now:
                for intent, verified in (r.get("intent_support") or {}).items():
                    if verified:
                        supported_intents.add(intent)
            all_intents_covered = intents_requested_set.issubset(supported_intents)
            if len(verified_now) >= top_k and all_intents_covered:
                break
        batch = ranked[cursor : cursor + batch_size]
        if not batch:
            break
        cursor += len(batch)
        for score, coverage, doc in batch:
            results.append(_evaluate_row(score, coverage, doc))
        if not is_scoped_mgmt:
            break
    evaluated_pool_size = len(results)

    # For scoped resolved queries, RT-010 requires a three-bucket sort
    # so that raw-verified intent-supporting evidence surfaces above
    # verified-but-intent-quiet rows, above unverified rows.  Preserve
    # BM25 order inside each bucket to keep ranking deterministic and
    # free of per-entity heuristics.  We only apply this reordering
    # when at least one management intent was actually requested;
    # otherwise the plain BM25 order (already verified-first) stands.
    # Within bucket_1 and bucket_2, ``strong`` entity linkage rows
    # (summary_candidate or title match) rank ahead of ``weak`` (raw-
    # anchor-only) rows so incidental raw mentions never outrank a
    # canonical entity-report even when they briefly co-occur with an
    # intent keyword.
    intent_supporting_top = None
    dropped_unverified_count = 0
    if scope_active and resolution.status == "resolved" and results:
        def _supports_any_intent(row: dict[str, Any]) -> bool:
            support = row.get("intent_support") or {}
            return any(bool(v) for v in support.values())

        def _row_intents(row: dict[str, Any]) -> set[str]:
            return {
                intent
                for intent, verified in (row.get("intent_support") or {}).items()
                if verified
            }

        def _linkage_key(row: dict[str, Any]) -> int:
            tier = row.get("entity_linkage")
            if tier == "strong":
                return 0
            if tier == "local":
                return 1
            return 2

        bucket_1: list[dict[str, Any]] = []
        bucket_2: list[dict[str, Any]] = []
        bucket_3: list[dict[str, Any]] = []
        for row in results:
            verified = row.get("evidence_status") == "verified"
            if verified and (not intents_requested or _supports_any_intent(row)):
                bucket_1.append(row)
            elif verified:
                bucket_2.append(row)
            else:
                bucket_3.append(row)
        bucket_1.sort(key=_linkage_key)
        bucket_2.sort(key=_linkage_key)
        dropped_unverified_count = len(bucket_3)
        # Drop bucket_3 from citeable results so the answer layer cannot
        # quote an unverified tail row underneath verified in-scope
        # evidence.  The dropped count is exposed via ``scope`` so
        # operators can still audit that unverified rows were removed.
        # Then trim citeable rows back to ``top_k`` while preserving at
        # least one citeable row per discovered requested intent where
        # possible (RT-010 hardening blocker C — diversify final top_k).
        combined = bucket_1 + bucket_2
        if intents_requested and combined:
            picked: list[dict[str, Any]] = []
            picked_ids: set[str] = set()
            covered_intents: set[str] = set()
            # First pass: honour bucket order (strong/local/weak
            # already sorted) but skip a row until we've reserved at
            # least one row for each intent it uniquely covers.
            # Deterministic greedy: iterate bucket order once,
            # append rows that either fill top_k without over-picking
            # OR uniquely add a not-yet-covered requested intent.
            for row in combined:
                if row.get("report_id") in picked_ids:
                    continue
                row_intents = _row_intents(row)
                new_intents = row_intents & (intents_requested_set - covered_intents)
                if len(picked) < top_k:
                    picked.append(row)
                    picked_ids.add(row.get("report_id"))
                    covered_intents |= row_intents
                elif new_intents:
                    # Replace the last non-diversifying row to keep
                    # size at top_k while surfacing the new intent.
                    replaced = False
                    for idx in range(len(picked) - 1, -1, -1):
                        existing_intents = _row_intents(picked[idx])
                        # A row is safe to displace if every intent
                        # it uniquely covers among the picked set
                        # will still be covered by other picked rows
                        # after removal.
                        others_intents: set[str] = set()
                        for j, other in enumerate(picked):
                            if j == idx:
                                continue
                            others_intents |= _row_intents(other)
                        others_intents |= row_intents
                        if existing_intents.issubset(others_intents):
                            picked_ids.discard(picked[idx].get("report_id"))
                            picked[idx] = row
                            picked_ids.add(row.get("report_id"))
                            covered_intents = set()
                            for r in picked:
                                covered_intents |= _row_intents(r)
                            replaced = True
                            break
                    if not replaced:
                        # Cannot diversify without losing an intent
                        # already covered; stop diversifying.
                        continue
            results = picked
        else:
            results = combined[:top_k]
        if bucket_1:
            intent_supporting_top = bucket_1[0]

    # Confidence anchor: for scoped-resolved queries, anchor on the
    # first raw-verified row that also supports at least one requested
    # intent (if any intent was requested).  If no such row exists, we
    # will convert to safe-empty below (management intent path only) or
    # fall back to standard BM25 confidence for plain scope queries.
    anchor_row = None
    if scope_active and resolution.status == "resolved" and results:
        anchor_row = intent_supporting_top
        if anchor_row is None and not intents_requested:
            # Plain scoped query with no management intent — the top
            # verified row (bucket 2) is a valid anchor; only when no
            # verified rows exist at all do we drop to the top row.
            anchor_row = next(
                (row for row in results if row.get("evidence_status") == "verified"),
                results[0],
            )
    if anchor_row is not None:
        top_score = anchor_row["score"]
        top_coverage = anchor_row["query_coverage"]
        top_status = anchor_row["evidence_status"]
    else:
        top_score = results[0]["score"] if results else 0.0
        top_coverage = results[0]["query_coverage"] if results else 0.0
        top_status = results[0]["evidence_status"] if results else "missing"
    base_confidence = confidence_for(top_score, top_coverage, top_status, len(results))

    # Entity-only scoped queries rank by date/quality; scores are zero
    # by construction so ``confidence_for`` would clear the result set.
    # A ``medium`` floor keeps the scoped postings visible while making
    # it clear that we have no query-side verification signal.
    if scope_active and residual_query_empty and results:
        base_confidence = "medium"

    scope_precision = 1.0
    if scope_active and results:
        in_scope_count = sum(1 for r in results if (r.get("scope_support") or {}).get("in_scope"))
        scope_precision = in_scope_count / len(results)

    # Recompute intent verification strictly from the CITEABLE result
    # set (post-bucket sort + drop_unverified + top_k trim).  An intent
    # supported only by a row we ultimately dropped or truncated away
    # must not leak into ``query_support`` — the answer layer would
    # then quote no rows for that intent while ``verified`` says
    # otherwise.  This is the exact regression the independent audit
    # asked for.
    verified_intents_union: set[str] = set()
    for row in results:
        for intent, verified in (row.get("intent_support") or {}).items():
            if verified:
                verified_intents_union.add(intent)
    intents_verified = sorted(verified_intents_union)
    intents_missing = [intent for intent in intents_requested if intent not in verified_intents_union]

    # ``source_integrity=verified`` requires EVERY returned row to have
    # its evidence confirmed against raw (RT-010: no single-row cherry
    # picking).  A partial verification downgrades the compound
    # confidence gate below.
    if not results:
        source_integrity = "unassessed"
    elif all(r["evidence_status"] == "verified" for r in results):
        source_integrity = "verified"
    else:
        source_integrity = "partial"
    if scope_active:
        scope_integrity = "verified" if scope_precision >= 1.0 and results else "insufficient"
    else:
        scope_integrity = "unassessed"
    if intents_requested:
        query_support = "verified" if not intents_missing else ("partial" if intents_verified else "insufficient")
    else:
        query_support = "unassessed"

    confidence = base_confidence
    # The entity/scope high-confidence gate only applies when we are
    # actually operating in scoped mode or when the query is entity-
    # shaped AND carries a management intent (i.e. progress/plan/risk/
    # owner).  A bare factual query like ``财务审核两周 Token 10.91 亿``
    # names an acronym but has no management intent, so it must retain
    # the pre-RT-010 high semantics and not regress general recall.
    entity_management_query = _query_looks_entity_shaped(question) and bool(intents_requested)
    if base_confidence == "high" and (scope_active or entity_management_query):
        if (
            resolution.status != "resolved"
            or resolution.resolution_kind not in {"exact", "approved_alias"}
            or source_integrity != "verified"
            or scope_integrity != "verified"
        ):
            confidence = "medium"
        elif intents_requested and query_support != "verified":
            confidence = "medium"
    # RT-010 extra gate for scoped management queries:
    # * ``high`` requires every requested intent to be verified AND
    #   linked AND at least one strong-linkage row that ITSELF supports
    #   every requested intent.  A strong-but-intent-quiet row plus a
    #   separate local-linkage row that supplies the intents must not
    #   promote to ``high`` – the audit blocker F regression: the strong
    #   row is the report-level guarantee, so it must carry the intent
    #   evidence on its own to justify the ``high`` claim.  Raw-only
    #   local linkage anywhere in results caps confidence at ``medium``.
    if (
        scope_active
        and resolution.status == "resolved"
        and intents_requested
        and confidence == "high"
    ):
        all_intents_linked = intents_requested and not intents_missing
        strong_rows = [r for r in results if r.get("entity_linkage") == "strong"]
        strong_supports_all = any(
            intents_requested_set.issubset(
                {i for i, v in (r.get("intent_support") or {}).items() if v}
            )
            for r in strong_rows
        )
        if not (all_intents_linked and strong_supports_all):
            confidence = "medium"
    # ----- Scoped-resolved floor -----
    # Once the scope hard-filter has selected candidates that are
    # provably in-scope and carry raw-verified evidence, the scoped
    # BM25 residual is only a re-ranking signal — it must not silently
    # discard the entire result set just because the residual query
    # (e.g. ``项目进展、下一步计划、风险``) is dominated by generic
    # management scaffolding.  RT-010's user decision says only genuine
    # zero/effective-empty scope becomes ``resolved_empty``; a scoped
    # query with verified in-scope evidence must remain visible at
    # ``medium`` when compound-intent support is incomplete, or ``low``
    # when evidence verification is partial.  The floor never rises to
    # ``high`` on its own — that still requires the full three-support
    # gate above.
    if scope_active and resolution.status == "resolved":
        # RT-010 hardening (independent audit blocker D): if the bucket
        # filter dropped every row (all in-scope candidates unverified)
        # we MUST always return ``resolved_empty / no_query_evidence_in_
        # scope``.  Guarding on ``and results:`` would let ``confidence
        # = none / results = []`` slip out as ``resolved`` – the exact
        # regression the audit called out.
        # Fail-closed conditions (recomputed after bucket trim):
        # * Management-intent scoped query with NO row that both raw-
        #   verifies AND carries a valid ``entity_intent_link``
        #   provenance → resolved_empty (the scope is real but the
        #   query has no locally-linked auditable answer).  This is
        #   stricter than "any intent hit anywhere": intent_support
        #   only flips True when the linkage helper builds a link
        #   record, so ``intent_supporting_top is None`` is a
        #   sufficient signal.
        # * Plain scoped query (no intent) with NO raw-verified row at
        #   all → resolved_empty on the same grounds.
        # * Any scoped query with EMPTY ``results`` after bucket sort
        #   → resolved_empty regardless of the intent situation.
        management_query = bool(intents_requested)
        anchor_missing = (
            not results
            or (management_query and intent_supporting_top is None)
            or (not management_query
                and not any(row.get("evidence_status") == "verified" for row in results))
        )
        if anchor_missing:
            resolution.status = "resolved_empty"
            resolution.reason = "no_query_evidence_in_scope"
            empty_payload = _safe_empty_payload(
                question,
                resolution,
                mirror=mirror,
                catalog_bound=catalog_bound,
                reason_override="no_query_evidence_in_scope",
            )
            empty_payload["indexed"] = {
                "summaries": len(summaries),
                "navigation_pages": len(navigation),
                "summary_quality": quality_counts,
                **index_info,
            }
            empty_payload["scope"] = {
                "applied": True,
                "postings_size": len(scope_postings),
                "filtered_out": scope_filtered_out,
                "scope_hash": scope_hash,
                "filter_reasons": ["no_query_evidence_in_scope"],
            }
            return empty_payload
        floor = "low"
        if source_integrity == "verified" and (
            not intents_requested or intents_verified
        ):
            floor = "medium"
        order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        if order.get(confidence, 0) < order[floor]:
            confidence = floor
    if confidence == "none":
        results = []

    payload = {
        "schema_version": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mirror_root": str(mirror),
        "query": question,
        "filters": {
            "from_date": from_date or None,
            "to_date": to_date or None,
            "writer": writer or None,
            "kind": kind,
        },
        "indexed": {
            "summaries": len(summaries),
            "navigation_pages": len(navigation),
            "summary_quality": quality_counts,
            **index_info,
        },
        "entity_resolution": {
            "status": resolution.status,
            "family_id": resolution.family_id,
            "family_name": resolution.family_name,
            "entity_types": resolution.entity_types,
            "matched_surfaces": resolution.matched_surfaces,
            "resolution_kind": resolution.resolution_kind,
            "reason": resolution.reason,
        },
        "scope": {
            "applied": scope_active,
            "postings_size": len(scope_postings),
            "filtered_out": scope_filtered_out,
            "scope_hash": scope_hash,
            "scope_precision": round(scope_precision, 4) if scope_active else None,
            "filter_reasons": [],
            "dropped_unverified_rows": dropped_unverified_count,
            "evaluated_pool_size": evaluated_pool_size,
        },
        "intents": {
            "requested": intents_requested,
            "verified": intents_verified,
            "missing": intents_missing,
        },
        "support": {
            "source_integrity": source_integrity,
            "scope_integrity": scope_integrity,
            "query_support": query_support,
        },
        "confidence": confidence,
        "global_fallback_used": False,
        "catalog_bound": catalog_bound,
        "answer_policy": (
            "Answer only from verified evidence and cite report_id/raw_path."
            if confidence != "none"
            else "Evidence is insufficient; abstain or broaden the query."
        ),
        "results": results,
    }
    return payload


def query_cloud(
    question: str,
    *,
    top_k: int,
    max_evidence: int,
    from_date: str,
    to_date: str,
    writer: str,
    kind: str,
    min_score: float,
    sender_id: str,
    account_id: str,
    project_id: str,
    root_file_id: str,
    cache_root: str,
    min_index_version: int,
) -> dict[str, Any]:
    from cwk_docdb_cloud import DocDBCloudRepository
    from cwk_sync_mirror_to_docdb import sanitize_error

    repo = DocDBCloudRepository(
        sender_id=sender_id,
        account_id=account_id,
        project_id=project_id,
        root_file_id=root_file_id,
        cache_root=Path(cache_root).expanduser().resolve() if cache_root else None,
    )
    cache_mirror, catalog = repo.bootstrap(min_index_version=min_index_version)
    catalog_holder = [catalog]

    def cloud_raw_loader(doc: SummaryDoc) -> tuple[str, str]:
        raw_rel = doc.raw_path.relative_to(cache_mirror).as_posix()
        for attempt in range(2):
            active_catalog = catalog_holder[0]
            row = (active_catalog.get("objects") or {}).get(raw_rel) or {}
            catalog_sha = str(row.get("content_sha256") or "")
            if doc.raw_sha256 and catalog_sha == doc.raw_sha256:
                raw_text, file_id, _ = repo.raw_text(active_catalog, raw_rel)
                return raw_text, f"docdb:{file_id}"
            if attempt == 0:
                _, refreshed = repo.bootstrap(min_index_version=min_index_version)
                catalog_holder[0] = refreshed
                continue
            raise RuntimeError(f"raw hash mismatch for {doc.report_id}")

    payload = query_mirror(
        cache_mirror,
        question,
        top_k=top_k,
        max_evidence=max_evidence,
        from_date=from_date,
        to_date=to_date,
        writer=writer,
        kind=kind,
        min_score=min_score,
        use_index=True,
        raw_loader=cloud_raw_loader,
    )
    catalog = catalog_holder[0]
    raw_objects = {
        str(rel): row for rel, row in (catalog.get("objects") or {}).items()
        if str(rel).startswith("raw/") and row.get("file_id")
    }
    preview_errors: list[dict[str, str]] = []
    for result in payload.get("results", []):
        raw_ref = str(result.get("raw_path") or "")
        row = {}
        if raw_ref.startswith("docdb:"):
            file_id_from_evidence = raw_ref.split(":", 1)[1]
            row = next((value for value in raw_objects.values() if str(value.get("file_id")) == file_id_from_evidence), {})
        file_id = str(row.get("file_id") or "")
        if file_id:
            result["cloud_file_id"] = file_id
            result["cloud_storage"] = str(row.get("storage") or "")
            try:
                if row.get("parts"):
                    summary_row = (catalog.get("objects") or {}).get(
                        f"wiki/summaries/{result.get('report_id')}.md"
                    ) or {}
                    summary_file_id = str(summary_row.get("file_id") or "")
                    result["cloud_preview_url"] = repo.preview_url(summary_file_id) if summary_file_id else ""
                    result["cloud_preview_kind"] = "summary"
                    result["cloud_raw_part_count"] = len(row.get("parts") or [])
                else:
                    result["cloud_preview_url"] = repo.preview_url(file_id)
                    result["cloud_preview_kind"] = "raw"
            except Exception as exc:
                result["cloud_preview_url"] = ""
                result["cloud_preview_error"] = sanitize_error(exc)
                preview_errors.append({"report_id": str(result.get("report_id") or ""), "error": sanitize_error(exc)})
    payload["mode"] = "cloud"
    payload["cloud"] = {
        "project_id": repo.project_id,
        "root_file_id": repo.root_file_id,
        "index_version": catalog.get("index_version"),
        "index_sha256": catalog.get("index_sha256"),
        "preview_errors": preview_errors,
    }
    return payload


def lint_mirror(mirror: Path) -> dict[str, Any]:
    summaries = load_summaries(mirror)
    known_ids = {doc.report_id for doc in summaries}
    duplicate_ids = sorted(rid for rid, count in Counter(doc.report_id for doc in summaries).items() if count > 1)
    missing_raw = sorted(doc.report_id for doc in summaries if not doc.raw_path.is_file())
    invalid_quotes: list[dict[str, str]] = []
    for doc in summaries:
        if not doc.raw_path.is_file() or not doc.evidence_quotes:
            continue
        content = raw_evidence_haystack(doc.raw_path.read_text(encoding="utf-8", errors="replace"))
        for quote in doc.evidence_quotes:
            if not normalized_contains(quote, content):
                invalid_quotes.append({"report_id": doc.report_id, "quote": compact(quote, 120)})
    dangling_nav: list[dict[str, str]] = []
    for nav in load_navigation(mirror):
        for report_id in nav.report_ids:
            if report_id not in known_ids:
                dangling_nav.append({"report_id": report_id, "path": str(nav.path)})
    checks = {
        "summary_count": len(summaries),
        "duplicate_ids": duplicate_ids,
        "missing_raw": missing_raw,
        "invalid_quotes": invalid_quotes,
        "dangling_navigation_refs": dangling_nav,
    }
    return {
        "schema_version": "cwk.wiki_lint.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mirror_root": str(mirror),
        "overall_pass": not any((duplicate_ids, missing_raw, invalid_quotes, dangling_nav)),
        "checks": checks,
    }


def markdown_query(payload: dict[str, Any]) -> str:
    lines = [
        f"# CWK Evidence Packet",
        "",
        f"- Query: {payload['query']}",
        f"- Confidence: `{payload['confidence']}`",
        f"- Indexed: {payload.get('indexed', {}).get('summaries', 0)} summaries / {payload.get('indexed', {}).get('navigation_pages', 0)} navigation pages",
        f"- Policy: {payload.get('answer_policy', '')}",
    ]
    if not payload.get("results"):
        lines.extend(["", "No evidence found. Do not answer from memory."])
        return "\n".join(lines) + "\n"
    for index, item in enumerate(payload["results"], 1):
        lines.extend(
            [
                "",
                f"## {index}. {item['title']}",
                "",
                f"- report_id: `{item['report_id']}`",
                f"- writer/date: {item['writer'] or '未知'} / {item['date'] or '未知'}",
                f"- score: {item['score']} · query coverage: {item['query_coverage']} · evidence: `{item['evidence_status']}`",
                f"- summary quality: `{item.get('summary_quality', 'unknown')}`",
                f"- summary: `{item['summary_path']}`",
                f"- raw: `{item['raw_path']}`",
            ]
        )
        for evidence in item["evidence"]:
            lines.append(f"- Quote ({evidence['kind']}): > {evidence['quote']}")
        if item["navigation_hits"]:
            labels = ", ".join(f"{x['kind']}:{x['title']}" for x in item["navigation_hits"][:3])
            lines.append(f"- Navigation: {labels}")
    return "\n".join(lines) + "\n"


def markdown_lint(payload: dict[str, Any]) -> str:
    checks = payload["checks"]
    return "\n".join(
        [
            "# CWK Wiki Lint",
            "",
            f"- overall: `{'PASS' if payload['overall_pass'] else 'FAIL'}`",
            f"- summaries: {checks['summary_count']}",
            f"- duplicate ids: {len(checks['duplicate_ids'])}",
            f"- missing raw: {len(checks['missing_raw'])}",
            f"- invalid evidence quotes: {len(checks['invalid_quotes'])}",
            f"- dangling navigation refs: {len(checks['dangling_navigation_refs'])}",
        ]
    ) + "\n"


def emit(payload: dict[str, Any], output_format: str, output: str) -> None:
    if output_format == "json":
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    elif payload.get("schema_version") == "cwk.wiki_lint.v1":
        content = markdown_lint(payload)
    else:
        content = markdown_query(payload)
    if output:
        target = Path(output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search the CWK Wiki and return raw-verified evidence.")
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-evidence", type=int, default=3)
    parser.add_argument("--from-date", default="")
    parser.add_argument("--to-date", default="")
    parser.add_argument("--writer", default="")
    parser.add_argument("--kind", choices=("all", "summary", "topic", "entity"), default="all")
    parser.add_argument("--min-score", type=float, default=0.1)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", default="")
    parser.add_argument("--lint", action="store_true", help="Check summary/raw/navigation link integrity instead of querying.")
    parser.add_argument("--no-index", action="store_true", help="Ignore the persistent index and scan Wiki files live.")
    parser.add_argument(
        "--require-catalog",
        action="store_true",
        help="Fail closed if the entity catalog is missing or hash-mismatched against the index.",
    )
    parser.add_argument("--mode", choices=("local", "cloud", "shadow"), default="local")
    parser.add_argument(
        "--experimental-cloud",
        action="store_true",
        help="Explicitly unlock paused cloud/shadow query modes for a controlled experiment.",
    )
    parser.add_argument("--sender-id", default=os.environ.get("CWK_SENDER_ID", ""))
    parser.add_argument("--account-id", default=os.environ.get("CWK_ACCOUNT_ID", "default"))
    parser.add_argument("--project-id", default=os.environ.get("CWK_DOCDB_PROJECT_ID", ""))
    parser.add_argument("--root-file-id", default=os.environ.get("CWK_DOCDB_ROOT_FILE_ID", ""))
    parser.add_argument("--cache-root", default=os.environ.get("CWK_CLOUD_CACHE_ROOT", ""))
    parser.add_argument("--min-index-version", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode in {"cloud", "shadow"} and not args.experimental_cloud:
        raise SystemExit(
            "CWK cloud/shadow query is paused; production queries must use --mode local. "
            "For a controlled experiment, also pass --experimental-cloud."
        )
    mirror = Path(args.mirror_root).expanduser().resolve()
    if args.mode in {"local", "shadow"} and (not (mirror / "wiki" / "summaries").is_dir() or not (mirror / "raw").is_dir()):
        raise SystemExit(f"invalid CWK mirror root: {mirror}")
    if args.lint:
        payload = lint_mirror(mirror)
        emit(payload, args.format, args.output)
        return 0 if payload["overall_pass"] else 1
    if not args.query.strip():
        raise SystemExit("query is required unless --lint is used")
    if args.top_k < 1 or args.max_evidence < 1:
        raise SystemExit("--top-k and --max-evidence must be positive")
    common = dict(
        top_k=args.top_k, max_evidence=args.max_evidence,
        from_date=args.from_date, to_date=args.to_date, writer=args.writer,
        kind=args.kind, min_score=args.min_score,
    )
    if args.mode == "cloud":
        payload = query_cloud(
            args.query, **common,
            sender_id=args.sender_id, account_id=args.account_id,
            project_id=args.project_id, root_file_id=args.root_file_id,
            cache_root=args.cache_root, min_index_version=args.min_index_version,
        )
    else:
        payload = query_mirror(
            mirror, args.query, **common,
            use_index=not args.no_index,
            require_catalog=args.require_catalog,
        )
        payload["mode"] = "local"
        if args.mode == "shadow":
            cloud_payload = query_cloud(
                args.query, **common,
                sender_id=args.sender_id, account_id=args.account_id,
                project_id=args.project_id, root_file_id=args.root_file_id,
                cache_root=args.cache_root, min_index_version=args.min_index_version,
            )
            local_ids = [row["report_id"] for row in payload.get("results", [])]
            cloud_ids = [row["report_id"] for row in cloud_payload.get("results", [])]
            payload["mode"] = "shadow"
            payload["shadow"] = {
                "cloud_confidence": cloud_payload.get("confidence"),
                "local_report_ids": local_ids,
                "cloud_report_ids": cloud_ids,
                "same_ranking": local_ids == cloud_ids,
            }
    emit(payload, args.format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
