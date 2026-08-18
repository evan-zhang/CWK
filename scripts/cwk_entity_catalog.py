#!/usr/bin/env python3
"""Build the deterministic machine entity catalog / postings for CWK.

This module is the sole scope source for entity-constrained local retrieval.
It derives a complete family/surface/posting graph from
``wiki/summaries/*.md`` (the ``候选实体`` section, primary source) and,
for already known canonical or approved-alias surfaces, exact anchors
inside the immutable ``raw/**/*.md`` layer (secondary source).  Human
authored Markdown pages under ``wiki/entities/`` and ``wiki/topics/``
are **never** used as a scope source.

Same-type family rules (all general, all deterministic):

- ``exact_normalized``
- ``parenthetical_acronym``   (e.g. ``TBS（训战系统）``)
- ``controlled_generic_suffix``   (a small closed suffix set per type)
- ``compound_alias``   (``A + B`` where ``B`` is a controlled suffix)

Cross-type merges are **never automatic**.  They only happen when an
explicit, versioned, evidence-backed entry in
``wiki/_system/entity-family-registry.json`` says so.  In particular,
this is the sole mechanism by which the system-typed ``TBS`` node and
the project-typed ``TBS`` node can end up in the same approved family.

Raw anchor extension uses a self-contained Aho-Corasick automaton to
scan every raw file in a single pass (O(len(text) + #matches)).  Match
positions still pass a normalised boundary check so that we never treat
``TBS销售部`` as evidence for the ``TBS`` family.  Results are cached
by ``raw_sha256`` in ``wiki/_system/entity-anchors-cache.json`` so that
subsequent rebuilds only pay for changed raws.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from cwk_ai_common import parse_frontmatter


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"
SCHEMA = "cwk.entity_catalog.v1"
REGISTRY_SCHEMA = "cwk.entity_family_registry.v1"
ANCHOR_CACHE_SCHEMA = "cwk.entity_anchor_cache.v1"

# ---------------------------------------------------------------------------
# Family rule configuration.  Documented in RT/RT-010/specs/技术方案.md.
# Keep these sets small, general and closed.
# ---------------------------------------------------------------------------
GENERIC_SUFFIXES: dict[str, tuple[str, ...]] = {
    "system": ("系统", "平台", "体系"),
    "project": ("项目", "项目组"),
}

ALLOWED_ENTITY_TYPES = ("system", "project", "product", "person", "organization", "other")

_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9._-]{1,4}$")
_PARENS_LEADING_ACR_RE = re.compile(
    r"^(?P<acr>[A-Z][A-Z0-9._-]{1,4})\s*[（(]\s*(?P<full>[^)）]{1,64})\s*[)）]$"
)
_PARENS_TRAILING_ACR_RE = re.compile(
    r"^(?P<full>[^（(]{1,64})\s*[（(]\s*(?P<acr>[A-Z][A-Z0-9._-]{1,4})\s*[)）]$"
)

_CJK_CHAR_RE = re.compile(r"[㐀-鿿]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_\-.]")
_ASCII_SURFACE_CHAR_RE = re.compile(r"^[A-Za-z0-9_\-. ]+$")
_CANDIDATE_LINE_RE = re.compile(r"^-\s+(?P<text>.+?)\s+`(?P<type>[a-z]+)`\s*$")
_EVIDENCE_LINE_RE = re.compile(r"^\s*证据：>\s*(?P<quote>.+)$")


def _surface_kind(surface: str) -> str:
    """Classify a surface for boundary policy.

    - ``ascii``: purely ASCII / acronym-style (no CJK anywhere).  Requires
      strict word boundary against adjacent ASCII word characters, but
      may touch CJK freely.  This lets ``TBS`` match inside 中文 prose
      like ``推进TBS项目`` while still rejecting ``TBSADMIN``.
    - ``cjk``: contains at least one CJK code point.  No adjacency wall
      because Chinese prose has no whitespace boundaries; overlaps are
      resolved by longest-match suppression instead.  This lets
      ``训战系统`` match in ``推动训战系统落地``.
    """
    if not surface:
        return "ascii"
    if _CJK_CHAR_RE.search(surface):
        return "cjk"
    return "ascii"


def _boundary_ok(text: str, start: int, end: int, surface_kind: str) -> bool:
    """Surface-aware boundary check.

    See :func:`_surface_kind` for the policy.  For CJK surfaces we do not
    reject on adjacency; longest-match suppression handles the ``云端`` /
    ``云端虾`` case at match-set level.
    """
    if surface_kind == "cjk":
        return True
    left = text[start - 1] if start > 0 else ""
    right = text[end] if end < len(text) else ""
    if left and _ASCII_WORD_RE.match(left):
        return False
    if right and _ASCII_WORD_RE.match(right):
        return False
    return True


def normalize_surface(value: str) -> str:
    """Normalise an entity surface for comparison.

    NFKC + case-fold + strip whitespace + normalise decorative quotes.
    We deliberately keep ``.``, ``-`` and ``_`` because they distinguish
    e.g. ``TBS.ADMIN`` from ``TBSADMIN``.
    """
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = value.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    value = re.sub(r"\s+", "", value)
    return value.casefold()


def _clean_display(value: str, limit: int = 160) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip(" \t-·，,。.：:；;")
    return value[:limit]


def find_exact_anchors(text: str, needle: str) -> list[int]:
    """Return start offsets where ``needle`` occurs in ``text`` with
    strict normalised boundaries.  Boundary policy is surface-aware
    (see :func:`_surface_kind`).  ASCII surfaces reject ASCII-word
    neighbours; CJK surfaces do not enforce adjacency (callers rely on
    longest-match suppression for overlap resolution).
    """
    if not needle or not text:
        return []
    kind = _surface_kind(needle)
    lower_text = text.casefold()
    lower_needle = needle.casefold()
    offsets: list[int] = []
    start = 0
    while True:
        pos = lower_text.find(lower_needle, start)
        if pos < 0:
            break
        end = pos + len(lower_needle)
        if _boundary_ok(text, pos, end, kind):
            offsets.append(pos)
        start = pos + 1
    return offsets


def suppress_shorter_overlaps(
    hits: list[tuple[int, int, Any]],
) -> list[tuple[int, int, Any]]:
    """Drop any hit whose span is strictly contained inside another hit.

    ``hits`` is a list of ``(start, end, payload)`` triples where ``end``
    is the exclusive end offset.  A span ``(s, e)`` is dropped iff there
    exists another distinct span ``(s', e')`` with ``s' <= s`` and
    ``e' >= e``.  Identical spans carrying different payloads are kept
    together so a single covered range can still name multiple entity
    families deterministically.

    Runs in ``O(n log n)`` on ``n`` input hits via a single sort + sweep:

    1. Deduplicate spans; sort by ``(start asc, end desc)``.
    2. Walk in sort order, tracking the maximum end seen so far.
    3. A span survives iff its end strictly exceeds the running max.

    The invariant is that any surviving span has a larger end than all
    earlier-processed spans, and all earlier-processed spans have a
    smaller-or-equal start (thanks to the sort).  Any dropped span is
    therefore strictly contained in some earlier survivor.
    """
    if not hits:
        return []
    unique_spans = sorted({(start, end) for start, end, _ in hits}, key=lambda x: (x[0], -x[1]))
    survivors: set[tuple[int, int]] = set()
    max_end = -1
    for s, e in unique_spans:
        if e > max_end:
            survivors.add((s, e))
            max_end = e
    return [(s, e, payload) for s, e, payload in hits if (s, e) in survivors]


def _naive_suppress_shorter_overlaps(
    hits: list[tuple[int, int, Any]],
) -> list[tuple[int, int, Any]]:
    """Reference implementation used only by parity tests."""
    if not hits:
        return []
    unique_spans = sorted({(start, end) for start, end, _ in hits})
    survivors: set[tuple[int, int]] = set()
    for i, (s, e) in enumerate(unique_spans):
        subsumed = False
        for j, (s2, e2) in enumerate(unique_spans):
            if j == i:
                continue
            if s2 <= s and e2 >= e:
                subsumed = True
                break
        if not subsumed:
            survivors.add((s, e))
    return [(s, e, payload) for s, e, payload in hits if (s, e) in survivors]


# ---------------------------------------------------------------------------
# Aho-Corasick multi-pattern matcher
# ---------------------------------------------------------------------------


class AhoCorasick:
    """Minimal deterministic Aho-Corasick automaton over Unicode strings.

    Each pattern is associated with an opaque payload; ``find_all``
    returns ``(start, end, payload)`` for every match.  Matching is
    case-insensitive (``str.casefold``) on both text and patterns.
    Boundary enforcement is done by callers after we hand back offsets
    so that the pattern semantics stay independent of tokenisation.
    """

    __slots__ = ("_goto", "_fail", "_output", "_patterns")

    def __init__(self) -> None:
        # Trie represented as list of dicts; index 0 is the root.
        self._goto: list[dict[str, int]] = [dict()]
        self._fail: list[int] = [0]
        self._output: list[list[int]] = [[]]
        self._patterns: list[tuple[str, Any]] = []

    def add(self, pattern: str, payload: Any) -> None:
        if not pattern:
            return
        folded = pattern.casefold()
        pattern_id = len(self._patterns)
        self._patterns.append((len(folded), payload))
        node = 0
        for ch in folded:
            children = self._goto[node]
            nxt = children.get(ch)
            if nxt is None:
                nxt = len(self._goto)
                self._goto.append(dict())
                self._fail.append(0)
                self._output.append([])
                children[ch] = nxt
            node = nxt
        self._output[node].append(pattern_id)

    def finalize(self) -> None:
        queue: deque[int] = deque()
        for ch, child in self._goto[0].items():
            self._fail[child] = 0
            queue.append(child)
        while queue:
            node = queue.popleft()
            for ch, child in self._goto[node].items():
                queue.append(child)
                fallback = self._fail[node]
                while fallback and ch not in self._goto[fallback]:
                    fallback = self._fail[fallback]
                self._fail[child] = self._goto[fallback].get(ch, 0) if fallback or ch in self._goto[0] else 0
                # Merge outputs along the failure link.
                self._output[child].extend(self._output[self._fail[child]])

    def find_all(self, text: str) -> list[tuple[int, int, Any]]:
        """Return every match as ``(start, end, payload)``.

        Uses a fully iterative sweep (no generator) and materialises the
        result in one Python list to keep per-match overhead close to a
        C-level tight loop.  ``end`` is exclusive.
        """
        if not self._patterns:
            return []
        folded = text.casefold()
        goto = self._goto
        fail = self._fail
        output = self._output
        patterns = self._patterns
        node = 0
        matches: list[tuple[int, int, Any]] = []
        append = matches.append
        for index in range(len(folded)):
            ch = folded[index]
            children = goto[node]
            if ch in children:
                node = children[ch]
            else:
                while node and ch not in goto[node]:
                    node = fail[node]
                node = goto[node].get(ch, 0)
            outputs = output[node]
            if outputs:
                idx_plus_one = index + 1
                for pattern_id in outputs:
                    length, payload = patterns[pattern_id]
                    append((idx_plus_one - length, idx_plus_one, payload))
        return matches


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SurfaceDeclaration:
    display: str
    normalized: str
    entity_type: str
    report_id: str
    quote: str
    origin: str  # "summary_candidate"


@dataclass
class ApprovedSurface:
    display: str
    normalized: str
    entity_type: str
    origin: str  # declared | parenthetical_acronym | controlled_generic_suffix
                 # | compound_alias | approved_family_registry
    provenance: list[dict[str, str]] = field(default_factory=list)
    # ``scope_role`` distinguishes surfaces that may authorise scope
    # expansion (``hard``) from surfaces that are only kept for operator
    # visibility (``generic_candidate``).  Parenthetical-derived bare
    # generic aliases – e.g. ``训战系统`` extracted from
    # ``训战系统（TBS）`` – must never hard-scope the acronym family
    # unless a registry entry explicitly promotes them; otherwise a
    # report that mentions only ``训战系统`` (no TBS) would silently
    # leak into TBS scope.  See RT-010 follow-up blocker F.
    scope_role: str = "hard"


@dataclass
class Family:
    family_id: str
    entity_types: set[str]
    canonical_display: str
    canonical_normalized: str
    canonical_entity_type: str
    surfaces: dict[str, ApprovedSurface] = field(default_factory=dict)
    postings: set[str] = field(default_factory=set)
    posting_provenance: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    approved_family_evidence: list[dict[str, str]] = field(default_factory=list)

    def add_posting(self, report_id: str, provenance: dict[str, str]) -> None:
        self.postings.add(report_id)
        self.posting_provenance[report_id].append(provenance)


def _family_id(entity_type: str, canonical_normalized: str) -> str:
    key = f"{entity_type}\x00{canonical_normalized}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Summary reader
# ---------------------------------------------------------------------------


@dataclass
class SummaryRecord:
    report_id: str
    summary_path: Path
    raw_path: Path
    raw_sha256: str
    candidates: list[SurfaceDeclaration]


def _parse_candidate_section(body: str, report_id: str) -> Iterable[SurfaceDeclaration]:
    lines = body.splitlines()
    in_section = False
    for index, raw_line in enumerate(lines):
        stripped = raw_line.rstrip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == "候选实体"
            continue
        if not in_section:
            continue
        m = _CANDIDATE_LINE_RE.match(stripped)
        if not m:
            continue
        entity_type = m.group("type").strip().lower()
        if entity_type not in ALLOWED_ENTITY_TYPES:
            continue
        text = _clean_display(m.group("text"), 200)
        if not text:
            continue
        quote = ""
        for follow in lines[index + 1 : index + 4]:
            m2 = _EVIDENCE_LINE_RE.match(follow)
            if m2:
                quote = _clean_display(m2.group("quote"), 240)
                break
        normalized = normalize_surface(text)
        if not normalized:
            continue
        yield SurfaceDeclaration(
            display=text,
            normalized=normalized,
            entity_type=entity_type,
            report_id=report_id,
            quote=quote,
            origin="summary_candidate",
        )


def _resolve_raw_path(summary_path: Path, source: str) -> Path:
    source = str(source or "").strip().strip('"')
    if not source:
        return Path()
    return (summary_path.parent / source).resolve()


def load_summaries(mirror: Path) -> list[SummaryRecord]:
    records: list[SummaryRecord] = []
    root = mirror / "wiki" / "summaries"
    if not root.is_dir():
        return records
    for path in sorted(root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        report_id = _clean_display(str(meta.get("report_id") or path.stem), 40)
        raw_path = _resolve_raw_path(path, str(meta.get("source") or ""))
        candidates = list(_parse_candidate_section(body, report_id))
        records.append(
            SummaryRecord(
                report_id=report_id,
                summary_path=path.resolve(),
                raw_path=raw_path,
                raw_sha256=str(meta.get("source_sha256") or ""),
                candidates=candidates,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Approved Family Registry
# ---------------------------------------------------------------------------


def registry_candidates(mirror: Path) -> list[Path]:
    """Ordered list of registry source paths, most-preferred first.

    The repo-committed ``config/entity-family-registry.json`` is the
    canonical, version-controlled source of truth.  A per-mirror
    override at ``wiki/_system/entity-family-registry.json`` is accepted
    for local experimentation, but the repo file wins when present so
    that catalog builds are reproducible across environments.
    """
    return [
        PROJECT / "config" / "entity-family-registry.json",
        mirror / "wiki" / "_system" / "entity-family-registry.json",
    ]


def _registry_source_label(path: Path, mirror: Path) -> str:
    """Return a stable, non-sensitive logical source label for
    ``path``. RT-010 final blockers (Blocker 8): the previous code
    handed the absolute filesystem path to ``index-meta.json``,
    which leaked per-machine home paths into cloud manifests and made
    two rebuilds on different machines look different even when the
    semantic content was identical. The logical labels are:

    - ``repo:config/entity-family-registry.json`` for the canonical
      version-controlled source;
    - ``mirror:<relative>`` for a per-mirror override (experimental
      only);
    - ``external:<basename>`` for anything else (test fixtures).
    """
    repo_path = PROJECT / "config" / "entity-family-registry.json"
    try:
        if path.resolve() == repo_path.resolve():
            return "repo:config/entity-family-registry.json"
    except OSError:
        pass
    try:
        rel = path.resolve().relative_to(mirror.resolve()).as_posix()
        return f"mirror:{rel}"
    except (OSError, ValueError):
        return f"external:{path.name}"


def load_registry(
    mirror: Path, *, override_paths: list[Path] | None = None
) -> tuple[dict[str, Any], str]:
    """Return ``(registry_payload, source_label)``.

    ``source_label`` is a stable non-sensitive logical string (see
    :func:`_registry_source_label`) — not an absolute path — so
    ``index-meta.json`` / ``manifest.json`` remain reproducible
    across machines and never leak per-host filesystem layout. The
    label is informational only and is **not** folded into the catalog
    hash; the catalog embeds ``registry.version`` and the applied
    entries themselves. Tests may pass ``override_paths`` to inject a
    fixture registry (bypassing the repo-config-first precedence);
    production callers keep the default order.
    """
    for path in (override_paths if override_paths is not None else registry_candidates(mirror)):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("schema_version") != REGISTRY_SCHEMA:
            continue
        payload.setdefault("entries", [])
        return payload, _registry_source_label(path, mirror)
    return (
        {"schema_version": REGISTRY_SCHEMA, "version": "unset", "entries": []},
        "",
    )


# ---------------------------------------------------------------------------
# Family builder helpers
# ---------------------------------------------------------------------------


def _pick_canonical_seed(surfaces: list[SurfaceDeclaration]) -> SurfaceDeclaration:
    frequency: Counter[str] = Counter()
    display_by_norm: dict[str, str] = {}
    reports_by_norm: dict[str, set[str]] = defaultdict(set)
    for decl in surfaces:
        display_by_norm.setdefault(decl.normalized, decl.display)
        reports_by_norm[decl.normalized].add(decl.report_id)
        frequency[decl.normalized] += 1
    best_norm = sorted(
        display_by_norm.keys(),
        key=lambda norm: (-len(reports_by_norm[norm]), len(norm), norm),
    )[0]
    return SurfaceDeclaration(
        display=display_by_norm[best_norm],
        normalized=best_norm,
        entity_type=surfaces[0].entity_type,
        report_id="",
        quote="",
        origin="canonical",
    )


def _build_seed_families(
    declarations: list[SurfaceDeclaration],
) -> tuple[dict[str, Family], dict[tuple[str, str], str]]:
    by_type: dict[str, list[SurfaceDeclaration]] = defaultdict(list)
    for decl in declarations:
        by_type[decl.entity_type].append(decl)

    families: dict[str, Family] = {}
    lookup: dict[tuple[str, str], str] = {}
    for entity_type, group in by_type.items():
        by_norm: dict[str, list[SurfaceDeclaration]] = defaultdict(list)
        for decl in group:
            by_norm[decl.normalized].append(decl)
        for normalized, surfaces in by_norm.items():
            canonical = _pick_canonical_seed(surfaces)
            fid = _family_id(entity_type, canonical.normalized)
            family = families.get(fid)
            if family is None:
                family = Family(
                    family_id=fid,
                    entity_types={entity_type},
                    canonical_display=canonical.display,
                    canonical_normalized=canonical.normalized,
                    canonical_entity_type=entity_type,
                )
                family.surfaces[canonical.normalized] = ApprovedSurface(
                    display=canonical.display,
                    normalized=canonical.normalized,
                    entity_type=entity_type,
                    origin="declared",
                )
                families[fid] = family
                lookup[(entity_type, canonical.normalized)] = fid
    return families, lookup


def _merge_family(target: Family, source: Family) -> None:
    if target is source:
        return
    for norm, surface in source.surfaces.items():
        existing = target.surfaces.get(norm)
        if existing is None:
            target.surfaces[norm] = surface
        else:
            existing.provenance.extend(surface.provenance)
    for report_id, entries in source.posting_provenance.items():
        target.posting_provenance[report_id].extend(entries)
    target.postings |= source.postings
    target.entity_types |= source.entity_types
    target.approved_family_evidence.extend(source.approved_family_evidence)


def _link_family(
    entity_type: str,
    fid: str,
    alias_norm: str,
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
) -> None:
    key = (entity_type, alias_norm)
    other_fid = lookup.get(key)
    if other_fid is None:
        lookup[key] = fid
        return
    if other_fid == fid:
        return
    target = families[fid]
    source = families.get(other_fid)
    if source is None:
        lookup[key] = fid
        return
    if entity_type not in source.entity_types and entity_type not in target.entity_types:
        return  # sanity
    # Same-type only – cross-type merges are gated by the registry.
    if source.canonical_entity_type != target.canonical_entity_type:
        return
    _merge_family(target, source)
    for norm in list(source.surfaces):
        lookup[(source.surfaces[norm].entity_type, norm)] = fid
    families.pop(other_fid, None)


def _apply_parenthetical_acronym(
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
    declarations_by_key: dict[str, list[SurfaceDeclaration]],
) -> None:
    pending: list[tuple[str, str, str, str, bool, dict[str, str]]] = []
    for key, decls in list(declarations_by_key.items()):
        entity_type, normalized = key.split("\x00", 1)
        for decl in decls:
            surface = decl.display.strip()
            m = _PARENS_LEADING_ACR_RE.match(surface) or _PARENS_TRAILING_ACR_RE.match(surface)
            if not m:
                continue
            acr = m.group("acr").strip()
            full = m.group("full").strip()
            if not _ACRONYM_RE.match(acr):
                continue
            acr_norm = normalize_surface(acr)
            full_norm = normalize_surface(full)
            if not acr_norm or not full_norm or acr_norm == full_norm:
                continue
            # ``is_acronym_side`` distinguishes the specific acronym
            # alias (safe to hard-scope) from the generic full
            # parenthetical (must NOT hard-scope a distinct family).
            for alias_display, alias_norm, is_acronym_side in (
                (acr, acr_norm, True),
                (full, full_norm, False),
            ):
                pending.append(
                    (
                        entity_type,
                        normalized,
                        alias_norm,
                        alias_display,
                        is_acronym_side,
                        {
                            "rule": "parenthetical_acronym",
                            "source_report_id": decl.report_id,
                            "source_surface": decl.display,
                            "alias_side": "acronym" if is_acronym_side else "full",
                        },
                    )
                )
    for entity_type, seed_norm, alias_norm, alias_display, is_acronym_side, provenance in pending:
        # Re-resolve the seed's family after every prior merge so we do
        # not carry a stale ``fid`` from before an intermediate link
        # operation.  The seed family may itself have been merged into
        # another one.
        seed_fid = lookup.get((entity_type, seed_norm))
        if seed_fid is None or seed_fid not in families:
            continue
        existing_fid = lookup.get((entity_type, alias_norm))
        # RT-010 follow-up (independent audit blocker F): only the
        # acronym alias may absorb an independently-declared same-type
        # family.  The FULL parenthetical alias is generic prose; if a
        # separate family already owns that surface we leave it alone
        # and instead record a soft ``generic_candidate`` surface on
        # the seed family so operators can still see the parenthetical
        # relation.  This prevents ``训战系统``-only reports leaking
        # into the TBS scope.
        if is_acronym_side:
            _link_family(entity_type, seed_fid, alias_norm, families, lookup)
        else:
            if existing_fid is None:
                # Fresh alias — safe to attach to the seed family.
                lookup[(entity_type, alias_norm)] = seed_fid
            elif existing_fid == seed_fid:
                # Same family already — nothing to reroute.
                pass
            else:
                # An independent family already owns this generic
                # surface.  Do NOT merge, do NOT reroute lookup; the
                # seed family will still list the surface below but
                # tagged ``generic_candidate`` so scope resolution and
                # posting extension skip it.
                pass
        # Only add the surface to the seed family when this iteration
        # is authorised to do so (i.e. the surface lookup either now
        # points at the seed family or the alias is a generic-only
        # visibility add — never overwrite the surface of a different
        # existing family).
        target_family = families[seed_fid]
        # RT-010 final blockers (Blocker 6): the bare parenthetical
        # FULL form (the non-acronym side) always lands as
        # ``generic_candidate`` — even when no independent same-name
        # family exists in the corpus. The acronym is a globally
        # distinguishing surface; the full form is generic prose that
        # could collide with any future report that mentions it on its
        # own (e.g. ``AI陪练系统`` derived from ``ALPHA（AI陪练系统）``
        # must not become a hard scope key just because no
        # ``AI陪练系统``-only report exists yet). The only path to
        # promote a generic-candidate alias to ``hard`` is an explicit
        # registry entry that lists it as a member (see
        # ``_apply_registry`` promotion block below). Acronym aliases
        # stay ``hard`` because their proper-noun shape does not
        # collide with unrelated prose.
        scope_role = "hard" if is_acronym_side else "generic_candidate"
        surface = target_family.surfaces.setdefault(
            alias_norm,
            ApprovedSurface(
                display=alias_display,
                normalized=alias_norm,
                entity_type=entity_type,
                origin="parenthetical_acronym",
                scope_role=scope_role,
            ),
        )
        # Never upgrade an existing declared/hard surface to generic.
        # But do allow demotion when the surface only exists as a
        # parenthetical alias and a competing independent family owns
        # the same norm (guarding against later runs re-adding it as
        # hard).
        if surface.origin == "parenthetical_acronym" and scope_role == "generic_candidate":
            surface.scope_role = "generic_candidate"
        surface.provenance.append(provenance)


def _apply_controlled_suffix(
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
) -> None:
    changed = True
    while changed:
        changed = False
        for entity_type, suffixes in GENERIC_SUFFIXES.items():
            keys = [key for key in lookup if key[0] == entity_type]
            for key in list(keys):
                _, long_norm = key
                for suffix in suffixes:
                    suffix_norm = normalize_surface(suffix)
                    if not suffix_norm or not long_norm.endswith(suffix_norm):
                        continue
                    short_norm = long_norm[: -len(suffix_norm)]
                    if not short_norm or short_norm == long_norm:
                        continue
                    short_key = (entity_type, short_norm)
                    if short_key not in lookup:
                        continue
                    short_fid = lookup[short_key]
                    long_fid = lookup[key]
                    if short_fid == long_fid:
                        continue
                    target = families.get(short_fid)
                    source = families.get(long_fid)
                    if not target or not source:
                        continue
                    if target.canonical_entity_type != source.canonical_entity_type:
                        continue
                    _merge_family(target, source)
                    for norm in list(source.surfaces):
                        lookup[(source.surfaces[norm].entity_type, norm)] = short_fid
                        surface = target.surfaces[norm]
                        if surface.origin == "declared" and norm != target.canonical_normalized:
                            surface.origin = "controlled_generic_suffix"
                            surface.provenance.append({
                                "rule": "controlled_generic_suffix",
                                "suffix": suffix,
                                "short_normalized": short_norm,
                            })
                    families.pop(long_fid, None)
                    changed = True


def _apply_compound_alias(
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
) -> None:
    """Merge compound aliases into their base family.

    Two orthogonal, deterministic sub-rules apply, both same-type only:

    - ``A + suffix``: ``A`` is an approved family surface and ``suffix``
      is drawn from :data:`GENERIC_SUFFIXES` for ``A``'s entity type;
    - ``A + B``: both ``A`` and ``B`` are approved surfaces of the same
      family.  This is the generalisation that lets ``TBS`` and
      ``训战系统`` (both approved for the ``TBS`` family) absorb the
      independently declared ``TBS训战系统`` surface without introducing
      any TBS-specific code.

    In every case the compound surface must already exist as an
    independently declared surface in another same-type family; we never
    invent surfaces out of thin air.
    """
    changed = True
    while changed:
        changed = False
        for fid, family in list(families.items()):
            # After the cross-type registry merge, a family may carry
            # surfaces for several entity_types.  Iterate all of them so
            # that a system-typed ``TBS`` can absorb the product-typed
            # ``TBS训战系统`` sibling once the registry has bridged them.
            entity_types = family.entity_types or {family.canonical_entity_type}
            approved_norms = list(family.surfaces.keys())
            candidates: list[tuple[str, str, str, dict[str, str]]] = []
            for entity_type in entity_types:
                suffixes = GENERIC_SUFFIXES.get(entity_type, ())
                for base_norm in approved_norms:
                    base_surface = family.surfaces[base_norm]
                    for suffix in suffixes:
                        suffix_norm = normalize_surface(suffix)
                        candidates.append(
                            (
                                entity_type,
                                base_norm + suffix_norm,
                                "controlled_suffix",
                                {
                                    "rule": "compound_alias",
                                    "base_surface": base_surface.display,
                                    "suffix": suffix,
                                    "entity_type": entity_type,
                                },
                            )
                        )
                    for other_norm in approved_norms:
                        if other_norm == base_norm:
                            continue
                        compound_norm = base_norm + other_norm
                        candidates.append(
                            (
                                entity_type,
                                compound_norm,
                                "surface_pair",
                                {
                                    "rule": "compound_alias",
                                    "base_surface": base_surface.display,
                                    "tail_surface": family.surfaces[other_norm].display,
                                    "entity_type": entity_type,
                                },
                            )
                        )
            for entity_type, compound_norm, mode, provenance in candidates:
                compound_key = (entity_type, compound_norm)
                if compound_key not in lookup:
                    continue
                other_fid = lookup[compound_key]
                if other_fid == fid:
                    continue
                other = families.get(other_fid)
                if other is None:
                    continue
                # Only pull in a sibling family whose canonical type is
                # one already recognised by this family — either its
                # canonical type or one bridged in by the registry.
                if other.canonical_entity_type not in entity_types:
                    continue
                _merge_family(family, other)
                for norm in list(other.surfaces):
                    lookup[(other.surfaces[norm].entity_type, norm)] = fid
                    surface = family.surfaces[norm]
                    if surface.origin == "declared" and norm != family.canonical_normalized:
                        surface.origin = "compound_alias"
                        surface.provenance.append(provenance)
                families.pop(other_fid, None)
                changed = True


class RegistryValidationError(ValueError):
    """Raised when the registry contains structurally invalid entries."""


def _registry_approver(entry: dict[str, Any]) -> str:
    """Return the truthful approver string.  We accept the modern
    ``decided_by`` field and fall back to the legacy ``approved_by``
    for fixtures that still use it; call sites treat either as the
    canonical audit label.
    """
    value = str(entry.get("decided_by") or entry.get("approved_by") or "").strip()
    return value


def _registry_approved_at(entry: dict[str, Any]) -> str:
    return str(entry.get("decided_at") or entry.get("approved_at") or "").strip()


def _validate_registry_entry(entry: dict[str, Any], index: int) -> None:
    """Strictly validate one registry entry before we act on it.

    We refuse to merge families based on malformed audit data: missing
    ``entry_id``, missing members, missing evidence, missing approver,
    and missing approval timestamp are all hard errors.  Both the
    modern (``decided_by`` / ``decided_at``) and the legacy
    (``approved_by`` / ``approved_at``) audit field pairs are accepted;
    at least one pair must be present with non-empty values.
    """
    if not isinstance(entry, dict):
        raise RegistryValidationError(f"registry[{index}] is not an object")
    members = entry.get("members")
    if not isinstance(members, list) or len(members) < 2:
        raise RegistryValidationError(
            f"registry[{index}] must list at least two members, got {members!r}"
        )
    for j, member in enumerate(members):
        if not isinstance(member, dict):
            raise RegistryValidationError(f"registry[{index}].members[{j}] must be object")
        if str(member.get("entity_type") or "").strip().lower() not in ALLOWED_ENTITY_TYPES:
            raise RegistryValidationError(
                f"registry[{index}].members[{j}].entity_type must be one of "
                f"{ALLOWED_ENTITY_TYPES}"
            )
        if not str(member.get("normalized") or "").strip():
            raise RegistryValidationError(
                f"registry[{index}].members[{j}].normalized must be non-empty"
            )
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RegistryValidationError(f"registry[{index}].evidence is required")
    for j, ev in enumerate(evidence):
        if not isinstance(ev, dict):
            raise RegistryValidationError(
                f"registry[{index}].evidence[{j}] must be object"
            )
        if not str(ev.get("report_id") or "").strip():
            raise RegistryValidationError(
                f"registry[{index}].evidence[{j}].report_id must be non-empty"
            )
        if not str(ev.get("quote") or "").strip():
            raise RegistryValidationError(
                f"registry[{index}].evidence[{j}].quote must be non-empty"
            )
    if not str(entry.get("entry_id") or "").strip():
        raise RegistryValidationError(
            f"registry[{index}].entry_id is required (stable audit id)"
        )
    if not _registry_approver(entry):
        raise RegistryValidationError(
            f"registry[{index}].decided_by (or legacy approved_by) is required"
        )
    if not _registry_approved_at(entry):
        raise RegistryValidationError(
            f"registry[{index}].decided_at (or legacy approved_at) is required"
        )


def _apply_registry(
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
    registry: dict[str, Any],
    *,
    raw_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Apply the cross-type approved-family registry.

    Each entry lists ``members`` = ``(entity_type, normalized)`` pairs
    that must be treated as one family, plus ``evidence`` and audit
    fields.  Structurally invalid entries raise
    :class:`RegistryValidationError`; requested members that are not
    present in the currently-declared corpus are recorded but do not
    silently succeed.  Merges are deterministic and provenance-tracked.

    RT-010 follow-up (independent audit blocker F): when a ``raw_index``
    keyed by ``report_id → raw_text`` is provided, every registry
    evidence quote must appear verbatim in the referenced raw report;
    any mismatch fails the whole build closed via
    :class:`RegistryValidationError`.  The check is skipped when the
    caller intentionally passes ``raw_index=None`` (e.g. a fixture that
    does not need to gate on raw availability), so old test paths keep
    working.
    """
    applied: list[dict[str, Any]] = []
    for index, entry in enumerate(registry.get("entries", []) or []):
        _validate_registry_entry(entry, index)
        entry_id = str(entry.get("entry_id") or "").strip()
        decided_by = _registry_approver(entry)
        decided_at = _registry_approved_at(entry)
        # RT-010 final blockers (Blocker 9): ``decision_ref`` is an
        # audit-critical pointer to the decision doc/ticket that
        # authorised the cross-type merge. Propagate it into every
        # provenance record and applied entry so the semantic catalog
        # hash changes whenever it changes. Legacy entries without a
        # decision_ref record an empty string (stable placeholder).
        decision_ref = str(entry.get("decision_ref") or "").strip()
        members = entry.get("members") or []
        seed_fids: list[str] = []
        member_snapshot: list[dict[str, str]] = []
        missing_members: list[dict[str, str]] = []
        for member in members:
            entity_type = str(member.get("entity_type") or "").strip().lower()
            normalized = normalize_surface(str(member.get("normalized") or ""))
            fid = lookup.get((entity_type, normalized))
            if fid is None or fid not in families:
                # A registry member the current corpus does not know
                # about is recorded so audit can see the drift; we do
                # NOT invent a family for it.
                missing_members.append({
                    "entity_type": entity_type,
                    "normalized": normalized,
                })
                continue
            seed_fids.append(fid)
            member_snapshot.append({"entity_type": entity_type, "normalized": normalized})
        deduped = list(dict.fromkeys(seed_fids))
        # RT-010 follow-up (blocker F): only verify registry evidence
        # against raw when the entry is about to actually merge families
        # (i.e. at least two members are present).  Fixtures that
        # inherit the repo registry but don't ship the TBS raws
        # therefore land as ``no_effect_members_missing`` rather than
        # a hard registry error – the entry has no effect on their
        # catalog anyway.  When the entry DOES apply, we still require
        # every listed evidence quote to appear in the referenced raw.
        if raw_index is not None and len(deduped) >= 2:
            _verify_registry_evidence(
                entry, index=index, entry_id=entry_id, raw_index=raw_index,
            )
        if len(deduped) < 2:
            applied.append(
                {
                    "entry_id": entry_id,
                    "canonical_display": _clean_display(str(entry.get("canonical_display") or ""), 160),
                    "family_id": deduped[0] if deduped else "",
                    "members": member_snapshot,
                    "missing_members": missing_members,
                    "evidence": [
                        {
                            "report_id": str(ev.get("report_id") or ""),
                            "quote": _clean_display(str(ev.get("quote") or ""), 240),
                        }
                        for ev in entry.get("evidence", []) or []
                    ],
                    "registry_version": str(registry.get("version") or ""),
                    "decided_by": decided_by,
                    "decided_at": decided_at,
                    "decision_ref": decision_ref,
                    "status": "no_effect_members_missing" if missing_members else "no_effect_single_family",
                }
            )
            continue
        target = families[deduped[0]]
        canonical_display = _clean_display(str(entry.get("canonical_display") or target.canonical_display), 160)
        target.canonical_display = canonical_display
        evidence_payload = []
        for evidence_index, ev in enumerate(entry.get("evidence", []) or []):
            # Carry the approval audit fields into every evidence row
            # so the query-layer ``scope_support.registry_evidence``
            # exposes the full audit chain (Blocker 9 contract).
            evidence_payload.append(
                {
                    "rule": "approved_family_registry",
                    "report_id": str(ev.get("report_id") or ""),
                    "quote": _clean_display(str(ev.get("quote") or ""), 240),
                    # Stable compact pointer used by every posting-level
                    # approval edge.  The full quote stays once at family
                    # level, avoiding hundreds of repeated raw snippets in
                    # large approved families while keeping the edge
                    # independently auditable.
                    "evidence_ref": f"{entry_id}:evidence:{evidence_index}",
                    "entry_id": entry_id,
                    "decided_by": decided_by,
                    "decided_at": decided_at,
                    "decision_ref": decision_ref,
                    "registry_version": str(registry.get("version") or ""),
                }
            )
        target.approved_family_evidence.extend(evidence_payload)
        for other_fid in deduped[1:]:
            source = families.get(other_fid)
            if source is None:
                continue
            _merge_family(target, source)
            for norm in list(source.surfaces):
                lookup[(source.surfaces[norm].entity_type, norm)] = target.family_id
                surface = target.surfaces[norm]
                surface.provenance.append(
                    {
                        "rule": "approved_family_registry",
                        "registry_version": str(registry.get("version") or ""),
                        "entry_id": entry_id,
                        "decided_by": decided_by,
                        "decided_at": decided_at,
                        "decision_ref": decision_ref,
                    }
                )
            families.pop(other_fid, None)
        # Every member listed in a registry entry is explicitly hard-
        # scoped: the registry is the authorised promotion path from
        # ``generic_candidate`` back to ``hard``.  This applies to the
        # exact normalised member surfaces named in the entry, not to
        # aliases produced by other rules.
        for member in member_snapshot:
            member_norm = str(member.get("normalized") or "")
            member_surface = target.surfaces.get(member_norm)
            if member_surface is not None:
                member_surface.scope_role = "hard"
        applied.append(
            {
                "entry_id": entry_id,
                "canonical_display": canonical_display,
                "family_id": target.family_id,
                "members": member_snapshot,
                "missing_members": missing_members,
                "evidence": evidence_payload,
                "registry_version": str(registry.get("version") or ""),
                "decided_by": decided_by,
                "decided_at": decided_at,
                "decision_ref": decision_ref,
                "status": "applied",
            }
        )
    return applied


def _verify_registry_evidence(
    entry: dict[str, Any],
    *,
    index: int,
    entry_id: str,
    raw_index: dict[str, str],
) -> None:
    """Verify every registry ``evidence.quote`` against the referenced
    ``report_id``'s raw text.  Raises :class:`RegistryValidationError`
    on the first mismatch.  The quote must appear verbatim (after
    whitespace collapse) in the raw envelope; otherwise the registry
    entry cannot be trusted to authorise a cross-type merge.
    """
    for j, ev in enumerate(entry.get("evidence") or []):
        report_id = str(ev.get("report_id") or "").strip()
        quote = str(ev.get("quote") or "").strip()
        if not report_id or not quote:
            raise RegistryValidationError(
                f"registry[{index}]({entry_id}).evidence[{j}]"
                " must carry report_id + quote"
            )
        raw_text = raw_index.get(report_id) or ""
        if not raw_text:
            raise RegistryValidationError(
                f"registry[{index}]({entry_id}).evidence[{j}] references"
                f" report_id={report_id} but raw is not present in mirror"
            )
        needle = re.sub(r"\s+", "", quote)
        haystack = re.sub(r"\s+", "", raw_text)
        if needle not in haystack:
            raise RegistryValidationError(
                f"registry[{index}]({entry_id}).evidence[{j}] quote does"
                f" not appear verbatim in raw {report_id}: registry"
                " cannot authorise a cross-type merge without genuine"
                " raw evidence"
            )


def _seed_postings(
    families: dict[str, Family],
    lookup: dict[tuple[str, str], str],
    declarations: list[SurfaceDeclaration],
) -> None:
    for decl in declarations:
        fid = lookup.get((decl.entity_type, decl.normalized))
        if fid is None:
            continue
        family = families[fid]
        # RT-010 follow-up (blocker F): a declaration matching a
        # ``generic_candidate`` surface must NOT be admitted as a
        # posting for that family.  Otherwise a bare ``训战系统``
        # declaration in an unrelated report would silently join the
        # TBS acronym family's scope.  The declaration still exists
        # on its own family via the seed pass; the merged/parenthetical
        # family only shows the surface for visibility.
        target_surface = family.surfaces.get(decl.normalized)
        if target_surface is not None and getattr(target_surface, "scope_role", "hard") != "hard":
            continue
        family.add_posting(
            decl.report_id,
            {
                "origin": "summary_candidate",
                "surface": decl.display,
                "entity_type": decl.entity_type,
                # Preserve the candidate ``证据：>`` quote so the query
                # layer can verify it against raw before promoting the
                # summary candidate to a structured entity anchor.
                # Without this the candidate declaration is unauditable
                # and query time must treat it as a weak link.
                "quote": decl.quote,
            },
        )


def _pick_canonical_from_family(family: Family, declaration_counts: Counter[str]) -> None:
    if not family.surfaces:
        return
    # RT-010 follow-up (blocker F): generic_candidate surfaces must
    # never win canonical.  Otherwise the parenthetical family for
    # ``ALPHA（示例平台）`` would elect the shorter generic ``示例平台``
    # as its canonical, collide family_ids with an independent
    # ``system:示例平台`` family, and one would overwrite the other in
    # the reindex pass.  Sort hard surfaces first, then apply the
    # historical ``(-declaration_count, length, norm)`` tie-break.
    def sort_key(norm: str) -> tuple[int, int, int, str]:
        role = getattr(family.surfaces[norm], "scope_role", "hard")
        role_rank = 0 if role == "hard" else 1
        return (
            role_rank,
            -declaration_counts.get(norm, 0),
            len(norm),
            norm,
        )
    best = sorted(family.surfaces.keys(), key=sort_key)[0]
    # Only override auto-picked canonical when no registry-supplied name exists.
    if not family.approved_family_evidence or not family.canonical_display:
        family.canonical_display = family.surfaces[best].display
    family.canonical_normalized = best
    family.canonical_entity_type = family.surfaces[best].entity_type
    family.family_id = _family_id(family.canonical_entity_type, best)


def _reindex_after_canonical(families: dict[str, Family]) -> dict[str, Family]:
    fresh: dict[str, Family] = {}
    for family in families.values():
        family.family_id = _family_id(family.canonical_entity_type, family.canonical_normalized)
        fresh[family.family_id] = family
    return fresh


def _rebuild_lookup(
    families: dict[str, Family],
    registry_applications: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, str], str]:
    """Rebuild the ``(entity_type, normalized) → family_id`` lookup.

    RT-010 follow-up (blocker F): after ``_apply_registry`` merges a
    project family into a system family the merged family owns the
    surface with the SYSTEM entity_type (``_merge_family`` only
    extends provenance on collisions, it does not clone the surface
    per entity_type).  Rebuilding lookup from ``surface.entity_type``
    alone therefore loses the ``(project, norm)`` route and later
    declarations would form a phantom sibling family.

    We fix this by broadcasting each applied registry member's
    ``(entity_type, normalized) → family_id`` pair into lookup, in
    addition to the ordinary surface-derived entries.  Only members
    that actually resolved to an existing family are broadcast.
    """
    lookup: dict[tuple[str, str], str] = {}
    for family in families.values():
        for norm in family.surfaces:
            lookup[(family.surfaces[norm].entity_type, norm)] = family.family_id
    if not registry_applications:
        return lookup
    # Map declared members back to the current family_id.  The
    # ``family_id`` recorded in the applied entry may have changed via
    # a subsequent reindex, so we route through any surviving member
    # lookup entry to identify the current family.
    for entry in registry_applications:
        members = entry.get("members") or []
        family_id = ""
        for member in members:
            entity_type = str(member.get("entity_type") or "").strip().lower()
            normalized = str(member.get("normalized") or "")
            if not entity_type or not normalized:
                continue
            fid = lookup.get((entity_type, normalized))
            if fid and fid in families:
                family_id = fid
                break
        if not family_id:
            continue
        for member in members:
            entity_type = str(member.get("entity_type") or "").strip().lower()
            normalized = str(member.get("normalized") or "")
            if not entity_type or not normalized:
                continue
            lookup[(entity_type, normalized)] = family_id
    return lookup


# ---------------------------------------------------------------------------
# Raw anchor extension
# ---------------------------------------------------------------------------


def _iterate_raw_paths(mirror: Path) -> list[Path]:
    root = mirror / "raw"
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def _report_id_from_raw(path: Path) -> str:
    stem = path.stem
    m = re.match(r"^(\d{15,20})", stem)
    return m.group(1) if m else stem


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _load_anchor_cache(mirror: Path) -> dict[str, Any]:
    path = mirror / "wiki" / "_system" / "entity-anchors-cache.json"
    if not path.is_file():
        return {"schema_version": ANCHOR_CACHE_SCHEMA, "families_signature": "", "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": ANCHOR_CACHE_SCHEMA, "families_signature": "", "entries": {}}
    if payload.get("schema_version") != ANCHOR_CACHE_SCHEMA:
        return {"schema_version": ANCHOR_CACHE_SCHEMA, "families_signature": "", "entries": {}}
    payload.setdefault("entries", {})
    payload.setdefault("families_signature", "")
    return payload


def _families_signature(families: dict[str, Family]) -> str:
    """Signature that captures which (family, surface) pairs feed the AC.

    Any change to the surface set changes the signature and therefore
    invalidates the anchor cache in one shot.
    """
    parts = []
    for family in sorted(families.values(), key=lambda f: (f.canonical_entity_type, f.canonical_normalized)):
        for norm in sorted(family.surfaces):
            parts.append(f"{family.family_id}|{norm}|{family.surfaces[norm].display}")
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _extend_with_raw_anchors(
    families: dict[str, Family],
    mirror: Path,
    *,
    cache: dict[str, Any] | None,
    max_files: int | None,
    summaries: list[SummaryRecord] | None = None,
) -> dict[str, Any]:
    stats = {
        "raw_files_seen": 0,
        "raw_files_scanned": 0,
        "raw_files_from_cache": 0,
        "raw_anchor_hits": 0,
        "raw_reports_touched": 0,
        "surface_count": 0,
    }
    if not families:
        return stats
    ac = AhoCorasick()
    needle_count = 0
    for family in families.values():
        for surface in family.surfaces.values():
            display = surface.display or surface.normalized
            if len(display) < 2:
                continue
            # RT-010 follow-up (blocker F): only ``hard`` surfaces
            # authorise raw-anchor scope extension.  Generic candidate
            # surfaces (bare parenthetical full forms like ``训战系统``)
            # remain visible in the catalog but must not pull raw
            # reports into the acronym family's postings.
            if getattr(surface, "scope_role", "hard") != "hard":
                continue
            # Precompute the surface kind so the raw-scan hot loop
            # skips the boundary regex for CJK surfaces entirely.
            kind_flag = _surface_kind(display)  # "ascii" or "cjk"
            ac.add(display, (family.family_id, surface.normalized, surface.display, kind_flag))
            needle_count += 1
    stats["surface_count"] = needle_count
    if not needle_count:
        return stats
    ac.finalize()

    signature = _families_signature(families)
    cache_valid = bool(cache) and cache.get("families_signature") == signature
    cache_entries: dict[str, list[list[Any]]] = (
        cache.get("entries", {}) if cache_valid and cache is not None else {}
    )
    fresh_entries: dict[str, list[list[Any]]] = {}
    families_by_id = {family.family_id: family for family in families.values()}
    touched_reports: set[str] = set()

    # Prefer scanning only the raw files that have a matching summary –
    # this ties every posting to a known ``report_id`` and quietly
    # skips ``raw/_system/timelines/**/*.md`` snapshot files whose
    # SHA-shaped filenames would otherwise pollute the catalog.
    if summaries is not None:
        pairs: list[tuple[Path, str]] = []
        for record in summaries:
            if record.raw_path and record.raw_path.is_file() and record.report_id:
                pairs.append((record.raw_path, record.report_id))
    else:
        pairs = [
            (path, _report_id_from_raw(path)) for path in _iterate_raw_paths(mirror)
        ]
        pairs = [(p, r) for p, r in pairs if r]
    if max_files is not None:
        pairs = pairs[:max_files]

    for raw_path, report_id in pairs:
        stats["raw_files_seen"] += 1
        raw_sha = _sha256_file(raw_path)
        if not raw_sha:
            continue
        cached = cache_entries.get(raw_sha)
        if cached is not None:
            stats["raw_files_from_cache"] += 1
            fresh_entries[raw_sha] = cached
            for entry in cached:
                # Aggregated cache rows are ``[family_id, surface,
                # first_offset, match_count]``.  Legacy per-occurrence
                # rows (3-tuples) are treated as one match each so that
                # a stale cache never over-counts.
                if len(entry) == 4:
                    family_id, surface_display, offset, match_count = entry
                else:
                    family_id, surface_display, offset = entry[:3]
                    match_count = 1
                family = families_by_id.get(family_id)
                if family is None:
                    continue
                family.add_posting(
                    report_id,
                    {
                        "origin": "raw_anchor",
                        "surface": surface_display,
                        "first_char_offset": str(offset),
                        "match_count": str(match_count),
                        "source_relative": raw_path.relative_to(mirror).as_posix()
                        if str(raw_path).startswith(str(mirror)) else str(raw_path),
                    },
                )
                stats["raw_anchor_hits"] += int(match_count)
                touched_reports.add(report_id)
            continue
        try:
            text = raw_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stats["raw_files_scanned"] += 1
        raw_hits: list[tuple[int, int, tuple[str, str, str, str]]] = []
        text_len = len(text)
        for start, end, payload in ac.find_all(text):
            kind = payload[3]
            if kind == "ascii":
                left_ok = start == 0 or not _ASCII_WORD_RE.match(text[start - 1])
                if not left_ok:
                    continue
                right_ok = end >= text_len or not _ASCII_WORD_RE.match(text[end])
                if not right_ok:
                    continue
            raw_hits.append((start, end, payload))
        # Longest-match / overlap suppression to avoid ``云端`` stealing
        # ``云端虾``; identical spans (same family, different surfaces)
        # survive together but strictly-shorter contained hits are dropped.
        raw_hits = suppress_shorter_overlaps(raw_hits)
        # Aggregate per (family_id, surface_normalized): scope is report-
        # level, so we only need one provenance record per surface per
        # raw file.  Keep the earliest offset plus a count.
        aggregated: dict[tuple[str, str], list[Any]] = {}
        for start, end, payload in raw_hits:
            family_id, surface_normalized, surface_display, _ = payload
            key = (family_id, surface_normalized)
            entry = aggregated.get(key)
            if entry is None:
                aggregated[key] = [family_id, surface_display, start, 1]
            else:
                if start < entry[2]:
                    entry[2] = start
                entry[3] += 1
        hits: list[list[Any]] = list(aggregated.values())
        fresh_entries[raw_sha] = hits
        for family_id, surface_display, offset, match_count in hits:
            family = families_by_id.get(family_id)
            if family is None:
                continue
            family.add_posting(
                report_id,
                {
                    "origin": "raw_anchor",
                    "surface": surface_display,
                    "first_char_offset": str(offset),
                    "match_count": str(match_count),
                    "source_relative": raw_path.relative_to(mirror).as_posix()
                    if str(raw_path).startswith(str(mirror)) else str(raw_path),
                },
            )
            stats["raw_anchor_hits"] += int(match_count)
            touched_reports.add(report_id)

    stats["raw_reports_touched"] = len(touched_reports)
    fresh_cache = {
        "schema_version": ANCHOR_CACHE_SCHEMA,
        "families_signature": signature,
        "entries": fresh_entries,
    }
    return {"stats": stats, "cache": fresh_cache}


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _serialise_family(family: Family) -> dict[str, Any]:
    postings = sorted(family.postings)
    scope_hash = hashlib.sha256(
        "\x00".join(postings).encode("utf-8")
    ).hexdigest()[:16] if postings else ""
    surfaces_payload = []
    for surface in sorted(family.surfaces.values(), key=lambda s: (s.origin, s.normalized)):
        surfaces_payload.append(
            {
                "display": surface.display,
                "normalized": surface.normalized,
                "entity_type": surface.entity_type,
                "origin": surface.origin,
                # ``scope_role`` = ``hard`` (default) means the surface
                # participates in scope resolution / raw-anchor
                # extension / strong H1 linkage.  ``generic_candidate``
                # keeps the surface visible for operators but excludes
                # it from those scope-widening paths (see RT-010
                # follow-up blocker F).
                "scope_role": getattr(surface, "scope_role", "hard"),
                "provenance": surface.provenance,
            }
        )
    # Compact posting provenance: one row per (report, origin) that
    # lists deduplicated surfaces + a match count.  Scope is report-
    # level so we do not need one row per (origin, surface, report);
    # the compact form still lets scope_support audit every posting
    # by surface + origin while keeping the catalog well within its
    # size budget on the real 8k+ mirror.
    posting_provenance: dict[str, list[dict[str, Any]]] = {}

    # A registry-approved cross-type merge is itself a scope edge.  It
    # therefore needs report-level provenance in addition to the original
    # summary/raw anchor provenance.  Build one compact audit edge per
    # approval entry and attach it to every posting in the approved family,
    # including postings discovered only by the later raw-anchor scan.
    # ``approved_family_evidence`` remains the single source for the full
    # evidence quotes; the posting edge carries stable references to those
    # rows plus the complete decision metadata required by RT-010.
    registry_audits: dict[str, dict[str, Any]] = {}
    for evidence_index, evidence in enumerate(family.approved_family_evidence):
        entry_id = str(evidence.get("entry_id") or "")
        if not entry_id:
            continue
        audit = registry_audits.setdefault(
            entry_id,
            {
                "origin": "approved_family_registry",
                "rule": "approved_family_registry",
                "entry_id": entry_id,
                "decided_by": str(evidence.get("decided_by") or ""),
                "decided_at": str(evidence.get("decided_at") or ""),
                "decision_ref": str(evidence.get("decision_ref") or ""),
                "registry_version": str(evidence.get("registry_version") or ""),
                "evidence_refs": [],
            },
        )
        evidence_ref = str(evidence.get("evidence_ref") or "")
        if not evidence_ref:
            evidence_ref = f"{entry_id}:evidence:{evidence_index}"
        if evidence_ref not in audit["evidence_refs"]:
            audit["evidence_refs"].append(evidence_ref)

    for report_id in postings:
        by_origin: dict[str, dict[str, Any]] = {}
        for entry in family.posting_provenance.get(report_id, []):
            origin = str(entry.get("origin") or "unknown")
            surface = str(entry.get("surface") or "")
            bucket = by_origin.setdefault(
                origin,
                {
                    "surfaces": set(),
                    "entity_types": set(),
                    "match_count": 0,
                    "quotes": set(),
                },
            )
            if surface:
                bucket["surfaces"].add(surface)
            entity_type = str(entry.get("entity_type") or "")
            if entity_type:
                bucket["entity_types"].add(entity_type)
            try:
                bucket["match_count"] += int(entry.get("match_count", "1"))
            except (TypeError, ValueError):
                bucket["match_count"] += 1
            quote = str(entry.get("quote") or "")
            if quote:
                bucket["quotes"].add(quote)
        rows = [
            {
                "origin": origin,
                "surfaces": sorted(bucket["surfaces"]),
                "entity_types": sorted(bucket["entity_types"]),
                "match_count": bucket["match_count"],
                # Candidate ``证据：>`` quotes carried from the summary
                # layer so the query side can verify them against raw
                # before treating the anchor as auditable.
                "quotes": sorted(bucket["quotes"]),
            }
            for origin, bucket in sorted(by_origin.items())
        ]
        for entry_id in sorted(registry_audits):
            audit = registry_audits[entry_id]
            rows.append(
                {
                    **audit,
                    "evidence_refs": sorted(audit["evidence_refs"]),
                }
            )
        posting_provenance[report_id] = rows
    return {
        "family_id": family.family_id,
        "canonical_entity_type": family.canonical_entity_type,
        "entity_types": sorted(family.entity_types),
        "canonical_surface": family.canonical_display,
        "canonical_normalized": family.canonical_normalized,
        "surfaces": surfaces_payload,
        "postings": postings,
        "posting_provenance": posting_provenance,
        "scope_hash": scope_hash,
        "posting_count": len(postings),
        "approved_family_evidence": family.approved_family_evidence,
    }


def _canonical_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_catalog(
    mirror: Path,
    *,
    max_raw_files: int | None = None,
    scan_raw: bool = True,
    use_cache: bool = True,
    registry_paths: list[Path] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Build the full deterministic entity catalog.

    Returns ``(catalog_payload, anchor_cache_or_none, registry_source)``.
    The anchor cache is a separate on-disk artefact.  ``registry_paths``
    lets tests inject a fixture registry; production callers leave it
    unset to keep the repo-config-first precedence.
    """
    summaries = load_summaries(mirror)
    declarations = [decl for record in summaries for decl in record.candidates]
    declarations_by_key: dict[str, list[SurfaceDeclaration]] = defaultdict(list)
    for decl in declarations:
        declarations_by_key[f"{decl.entity_type}\x00{decl.normalized}"].append(decl)

    families, lookup = _build_seed_families(declarations)
    _apply_parenthetical_acronym(families, lookup, declarations_by_key)
    _apply_controlled_suffix(families, lookup)
    _apply_compound_alias(families, lookup)

    declaration_counts: Counter[str] = Counter()
    for decl in declarations:
        declaration_counts[decl.normalized] += 1
    for family in families.values():
        _pick_canonical_from_family(family, declaration_counts)
    families = _reindex_after_canonical(families)
    lookup = {}
    for family in families.values():
        for norm in family.surfaces:
            lookup[(family.surfaces[norm].entity_type, norm)] = family.family_id

    registry, registry_source = load_registry(mirror, override_paths=registry_paths)
    # Build a report_id → raw_text index for registry evidence
    # verification.  Missing raws are allowed at index build time
    # (they will simply cause the referenced registry entry to fail
    # closed), but the pass itself is deterministic.
    raw_index_for_registry: dict[str, str] = {}
    for record in summaries:
        raw_path = record.raw_path
        if not raw_path or not raw_path.is_file():
            continue
        try:
            raw_index_for_registry[record.report_id] = raw_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    registry_applications = _apply_registry(
        families, lookup, registry, raw_index=raw_index_for_registry,
    )
    # Registry merges may have changed canonicals; re-elect.
    for family in families.values():
        _pick_canonical_from_family(family, declaration_counts)
    families = _reindex_after_canonical(families)
    lookup = _rebuild_lookup(families, registry_applications)

    # Second compound_alias pass so cross-type registry bridges (e.g.
    # ``TBS`` becoming system+project+product) can now absorb sibling
    # compound families for each newly-recognised type.  Rules stay
    # deterministic and identical to the first pass; only the family
    # entity_type set is now wider.
    _apply_compound_alias(families, lookup)
    # Same-normalised cross-type completion (registry-gated).  Only
    # families with registry-approved ``entity_types`` may absorb
    # NOTE (RT-010): we intentionally do NOT invoke a transitive
    # ``_apply_registry_extension`` pass.  Registry approval for exact
    # cross-type members must not automatically authorise every same-
    # normalised sibling/alias across those types – that would silently
    # widen the audit trail beyond what was reviewed.  Any leftover
    # cross-type ambiguity either fails closed at query time or the
    # operator must add an explicit registry entry with fresh evidence.
    for family in families.values():
        _pick_canonical_from_family(family, declaration_counts)
    families = _reindex_after_canonical(families)
    lookup = _rebuild_lookup(families, registry_applications)

    _seed_postings(families, lookup, declarations)

    anchor_cache = None
    raw_stats: dict[str, Any] = {"raw_files_seen": 0, "raw_files_scanned": 0, "raw_anchor_hits": 0}
    if scan_raw:
        cache = _load_anchor_cache(mirror) if use_cache else None
        outcome = _extend_with_raw_anchors(
            families, mirror, cache=cache, max_files=max_raw_files,
            summaries=summaries,
        )
        if "cache" in outcome:
            raw_stats = outcome["stats"]
            anchor_cache = outcome["cache"]

    family_payloads = [_serialise_family(family) for family in families.values()]
    family_payloads.sort(key=lambda payload: (payload["canonical_entity_type"], payload["canonical_normalized"]))

    surface_index: dict[str, list[str]] = defaultdict(list)
    for payload in family_payloads:
        for surface in payload["surfaces"]:
            surface_index[surface["normalized"]].append(payload["family_id"])
    surface_index_serialised = {
        norm: sorted(set(family_ids)) for norm, family_ids in surface_index.items()
    }

    type_counts = Counter(payload["canonical_entity_type"] for payload in family_payloads)
    posting_lengths = [payload["posting_count"] for payload in family_payloads]
    semantic_core = {
        "schema_version": SCHEMA,
        "family_rules": {
            "generic_suffixes": {k: list(v) for k, v in GENERIC_SUFFIXES.items()},
            "acronym_re": _ACRONYM_RE.pattern,
        },
        "registry": {
            "schema_version": registry.get("schema_version", REGISTRY_SCHEMA),
            "version": registry.get("version", "unset"),
            "applied": registry_applications,
        },
        "families": family_payloads,
        "surface_index": {norm: surface_index_serialised[norm] for norm in sorted(surface_index_serialised)},
        "statistics": {
            "summaries_scanned": len(summaries),
            "declarations_total": len(declarations),
            "families_total": len(family_payloads),
            "families_by_type": {t: type_counts.get(t, 0) for t in ALLOWED_ENTITY_TYPES},
            "average_postings": sum(posting_lengths) / max(1, len(posting_lengths)),
            "max_postings": max(posting_lengths) if posting_lengths else 0,
        },
    }
    # ``catalog_sha256`` covers only the semantic content – NOT the
    # cold-vs-warm raw scan execution counters – so the same corpus
    # always hashes to the same value regardless of anchor cache state.
    catalog_sha256 = _canonical_hash(semantic_core)
    payload = {
        **semantic_core,
        "catalog_sha256": catalog_sha256,
        "build_stats": {"raw_stats": raw_stats},
    }
    payload["statistics"] = {**semantic_core["statistics"], "raw_stats": raw_stats}
    return payload, anchor_cache, registry_source


# ---------------------------------------------------------------------------
# Query helpers used by cwk_wiki_query
# ---------------------------------------------------------------------------


_CATALOG_CACHE: dict[tuple[str, int, int], dict[str, Any] | None] = {}


def load_catalog(mirror: Path) -> dict[str, Any] | None:
    system = mirror / "wiki" / "_system"
    compressed = system / "entity-catalog.json.gz"
    plain = system / "entity-catalog.json"
    path = compressed if compressed.exists() else plain
    if not path.is_file():
        return None
    # In-process cache keyed by (path, mtime_ns, size).  Any rebuild
    # touches at least one of these, so the cache invalidates itself
    # deterministically without needing a manual clear on the hot path.
    try:
        stat = path.stat()
    except OSError:
        return None
    cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if cache_key in _CATALOG_CACHE:
        return _CATALOG_CACHE[cache_key]
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CATALOG_CACHE[cache_key] = None
        return None
    if payload.get("schema_version") != SCHEMA:
        _CATALOG_CACHE[cache_key] = None
        return None
    claimed = str(payload.get("catalog_sha256") or "")
    # ``catalog_sha256`` is computed over the semantic core only.  We
    # rebuild that view here so cold and warm rebuilds (which differ
    # only in ``build_stats.raw_stats``) round-trip validation.
    excluded = {"catalog_sha256", "build_stats"}
    semantic_core = {}
    for key, value in payload.items():
        if key in excluded:
            continue
        if key == "statistics" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "raw_stats"}
        semantic_core[key] = value
    if not claimed or claimed != _canonical_hash(semantic_core):
        return None
    meta_path = system / "entity-catalog-meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _CATALOG_CACHE[cache_key] = None
            return None
        if str(meta.get("catalog_sha256") or "") != claimed:
            _CATALOG_CACHE[cache_key] = None
            return None
    # Evict older cache entries for the same mirror so we do not keep
    # stale multi-generation payloads in memory forever.
    for key in [k for k in _CATALOG_CACHE if k[0] == cache_key[0] and k != cache_key]:
        _CATALOG_CACHE.pop(key, None)
    _CATALOG_CACHE[cache_key] = payload
    return payload


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, payload: dict[str, Any], *, compact: bool = True) -> None:
    if compact:
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    else:
        blob = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, blob)


def atomic_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with open(name, "wb") as raw_handle:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as handle:
                handle.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_catalog(
    mirror: Path,
    payload: dict[str, Any],
    anchor_cache: dict[str, Any] | None,
    *,
    write_uncompressed: bool = True,
) -> dict[str, Any]:
    system = mirror / "wiki" / "_system"
    system.mkdir(parents=True, exist_ok=True)
    gz_path = system / "entity-catalog.json.gz"
    plain_path = system / "entity-catalog.json"
    meta_path = system / "entity-catalog-meta.json"
    cache_path = system / "entity-anchors-cache.json"
    atomic_gzip_json(gz_path, payload)
    if write_uncompressed:
        atomic_json(plain_path, payload, compact=True)
    if anchor_cache is not None:
        atomic_json(cache_path, anchor_cache, compact=True)
    meta = {
        "schema_version": "cwk.entity_catalog_meta.v1",
        "catalog_schema_version": payload["schema_version"],
        "catalog_sha256": payload["catalog_sha256"],
        "families_total": payload["statistics"]["families_total"],
        "summaries_scanned": payload["statistics"]["summaries_scanned"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_json(meta_path, meta, compact=False)
    return meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the CWK machine entity catalog.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--output", default="")
    parser.add_argument("--no-raw", action="store_true", help="Skip raw anchor extension (test speed).")
    parser.add_argument("--no-cache", action="store_true", help="Ignore anchor cache for a full rescan.")
    parser.add_argument("--max-raw-files", type=int, default=None, help="Cap raw scan for benchmarking.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    started = time.time()
    payload, anchor_cache, registry_source = build_catalog(
        mirror,
        scan_raw=not args.no_raw,
        max_raw_files=args.max_raw_files,
        use_cache=not args.no_cache,
    )
    meta = write_catalog(mirror, payload, anchor_cache)
    elapsed = time.time() - started
    report = {
        "schema_version": SCHEMA,
        "mirror_root": str(mirror),
        "families_total": payload["statistics"]["families_total"],
        "summaries_scanned": payload["statistics"]["summaries_scanned"],
        "raw_stats": payload["statistics"]["raw_stats"],
        "catalog_sha256": payload["catalog_sha256"],
        "registry_source": registry_source,
        "registry_version": payload["registry"]["version"],
        "registry_entries_applied": len(payload["registry"]["applied"]),
        "elapsed_seconds": round(elapsed, 3),
        "meta": meta,
    }
    if args.output:
        Path(args.output).expanduser().resolve().write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
