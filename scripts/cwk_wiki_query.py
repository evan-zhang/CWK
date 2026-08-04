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


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"
SCHEMA = "cwk.wiki_query.v1"
REPORT_ID_RE = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")
SUMMARY_LINK_RE = re.compile(r"\[`?(\d{15,20})`?\]\([^)]*summaries/\1\.md\)")
EVIDENCE_RE = re.compile(r"证据：>\s*(.+?)\s*$", flags=re.M)
ASCII_RE = re.compile(r"[a-z0-9][a-z0-9._/+-]*", re.I)
CJK_RE = re.compile(r"[\u3400-\u9fff]+")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(?:api[_-]?key|token|appkey)\s*[:=]\s*[^\s,;]{8,}"),
)
STOP_ASCII = {"the", "a", "an", "of", "to", "is", "are", "and", "or", "in", "on", "for"}
STOP_CJK = {
    "什么", "哪些", "怎么", "怎样", "如何", "一下", "这个", "那个", "我们", "关于",
    "目前", "现在", "最近", "情况", "介绍", "分析", "帮我", "给我", "是否", "有没有",
    "中旬", "月底", "月初", "月中", "月末", "月", "到", "至", "底",
}
_INDEX_CACHE: dict[tuple[str, int, int, int, int], tuple[Any, ...]] = {}


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
        return {}, {"ai_refined": 0, "fallback_pending": 0, "withheld_sensitive": 0, "fallback_terminal_error": 0, "unknown": 0}
    refined = {str(value) for value in payload.get("ai_refined_report_ids", [])}
    fallback = {str(value) for value in payload.get("fallback_report_ids", [])}
    withheld = {str(value) for value in payload.get("withheld_report_ids", [])}
    terminal = {
        str(item.get("report_id"))
        for item in payload.get("failure_queue", [])
        if item.get("report_id") and int(item.get("attempts", 1)) >= 3
    }
    quality = {report_id: "ai_refined" for report_id in refined}
    quality.update({report_id: "fallback_pending" for report_id in fallback})
    quality.update({report_id: "fallback_terminal_error" for report_id in fallback & terminal})
    quality.update({report_id: "withheld_sensitive" for report_id in withheld})
    return quality, {
        "ai_refined": len(refined),
        "fallback_pending": len(fallback - withheld - terminal),
        "withheld_sensitive": len(withheld),
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


def redact(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


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
) -> tuple[str, list[dict[str, str]], str]:
    if raw_loader:
        try:
            raw_text, source_ref = raw_loader(doc)
        except Exception as exc:
            status = "hash_mismatch" if "hash mismatch" in str(exc).lower() else "missing"
            return status, [], ""
    else:
        if not doc.raw_path or not doc.raw_path.is_file():
            return "missing", [], ""
        if doc.raw_sha256 and file_sha256(doc.raw_path) != doc.raw_sha256:
            return "hash_mismatch", [], str(doc.raw_path)
        raw_text = doc.raw_path.read_text(encoding="utf-8", errors="replace")
        source_ref = str(doc.raw_path)
    content = raw_content(raw_text)
    evidence_haystack = raw_evidence_haystack(raw_text)
    verified: list[dict[str, str]] = []
    for quote in doc.evidence_quotes:
        if normalized_contains(quote, evidence_haystack):
            verified.append({"kind": "summary_quote", "quote": redact(quote)})
        if len(verified) >= max_evidence:
            break
    if verified:
        return "verified", verified, source_ref

    candidates = excerpt_candidates(content)
    if not candidates:
        return "missing", [], source_ref
    index = BM25(candidates)
    query_tokens = tokenize(question)
    ranked = sorted(
        ((index.score(query_tokens, i), value) for i, value in enumerate(candidates)),
        key=lambda item: (-item[0], len(item[1])),
    )
    excerpts = [
        {"kind": "raw_excerpt", "quote": redact(value)}
        for score, value in ranked[:max_evidence]
        if score > 0
    ]
    if not excerpts:
        return "unverified", [], source_ref
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
    query_tokens = tokenize(question)
    if not query_tokens:
        return {
            "schema_version": SCHEMA,
            "query": question,
            "confidence": "none",
            "reason": "query_has_no_searchable_tokens",
            "results": [],
        }

    nav_bonus: defaultdict[str, float] = defaultdict(float)
    nav_hits: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    if kind in {"all", "topic", "entity"}:
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
    for i, doc in enumerate(summaries):
        # Withheld sources must not be returned by either local or cloud
        # retrieval. Their presence in the index is only for audit/counting.
        if summary_quality.get(doc.report_id) == "withheld_sensitive":
            continue
        if not date_ok(doc.date, from_date, to_date):
            continue
        if writer and normalize(writer) not in normalize(doc.writer):
            continue
        score = summary_index.score(query_tokens, i)
        title_norm = normalize(doc.title)
        if normalized_question and normalized_question in title_norm:
            score += 18.0
        if title_norm and title_norm in normalized_question:
            score += 12.0
        if requested_ids and doc.report_id in requested_ids:
            score += 50.0
        score += nav_bonus.get(doc.report_id, 0.0)
        coverage = summary_index.coverage(query_tokens, i)
        if score >= min_score:
            ranked.append((score, coverage, doc))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].date or "9999-99-99", item[2].report_id))

    results: list[dict[str, Any]] = []
    for score, coverage, doc in ranked[:top_k]:
        evidence_status, evidence, source_ref = evidence_for(doc, question, max_evidence, raw_loader)
        results.append(
            {
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
            }
        )
    top_score = results[0]["score"] if results else 0.0
    top_coverage = results[0]["query_coverage"] if results else 0.0
    top_status = results[0]["evidence_status"] if results else "missing"
    confidence = confidence_for(top_score, top_coverage, top_status, len(results))
    if confidence == "none":
        results = []
    return {
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
        "confidence": confidence,
        "answer_policy": (
            "Answer only from verified evidence and cite report_id/raw_path."
            if confidence != "none"
            else "Evidence is insufficient; abstain or broaden the query."
        ),
        "results": results,
    }


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
    parser.add_argument("--mode", choices=("local", "cloud", "shadow"), default="local")
    parser.add_argument("--sender-id", default=os.environ.get("CWK_SENDER_ID", ""))
    parser.add_argument("--account-id", default=os.environ.get("CWK_ACCOUNT_ID", "default"))
    parser.add_argument("--project-id", default=os.environ.get("CWK_DOCDB_PROJECT_ID", ""))
    parser.add_argument("--root-file-id", default=os.environ.get("CWK_DOCDB_ROOT_FILE_ID", ""))
    parser.add_argument("--cache-root", default=os.environ.get("CWK_CLOUD_CACHE_ROOT", ""))
    parser.add_argument("--min-index-version", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
        payload = query_mirror(mirror, args.query, **common, use_index=not args.no_index)
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
