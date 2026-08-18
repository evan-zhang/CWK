"""RT-010: bounded entity-constrained local retrieval hardening tests.

The property/synthetic tests deliberately avoid TBS literals so the
implementation is not tuned around a single acronym.  A single
real-corpus regression at the bottom exercises the TBS registry entry
against the shipped mirror; it never asserts a fixed report id or rank.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_entity_catalog as ec  # noqa: E402
import cwk_wiki_query as wq  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _raw(report_id: str, title: str, writer: str, date: str, content: str) -> str:
    return f'''---
report_id: "{report_id}"
title: "{title}"
writer: "{writer}"
create_time: "{date} 10:00:00"
source_lane: inbox_awareness
---

# {title}

<content>
{content}
</content>
'''


def _summary(
    report_id: str,
    title: str,
    writer: str,
    date: str,
    raw_rel: str,
    summary: str,
    key_facts: list[tuple[str, str]] | None = None,
    candidate_entities: list[tuple[str, str, str]] | None = None,
) -> str:
    key_facts = key_facts or []
    candidate_entities = candidate_entities or []
    facts_block = ""
    for text, quote in key_facts:
        facts_block += f"- {text}  \n  证据：> {quote}\n"
    ent_block = ""
    for text, entity_type, quote in candidate_entities:
        ent_block += f"- {text} `{entity_type}`  \n  证据：> {quote}\n"
    return f'''---
type: SourceSummary
report_id: "{report_id}"
source: "{raw_rel}"
---

# {title}

- 原文：[`{report_id}`]({raw_rel})
- 发送人：{writer}
- 时间：{date} 10:00:00
- 来源类型：`inbox_awareness`

## 摘要

{summary}

## 关键事实

{facts_block}
## 候选实体

{ent_block}
## 证据边界

事实以原文为准。
'''


class SyntheticMirror:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mirror = Path(self.tmp.name) / "工作协同镜像"
        (self.mirror / "raw" / "2026-08" / "2026-08-01").mkdir(parents=True)
        (self.mirror / "wiki" / "summaries").mkdir(parents=True)
        (self.mirror / "wiki" / "_system").mkdir(parents=True)
        (self.mirror / "wiki" / "_system" / "manifest.json").write_text("{}", encoding="utf-8")

    def cleanup(self) -> None:
        self.tmp.cleanup()

    def add_report(
        self,
        report_id: str,
        title: str,
        raw_body: str,
        *,
        date: str = "2026-08-01",
        writer: str = "张三",
        summary_text: str = "示例摘要。",
        key_facts: list[tuple[str, str]] | None = None,
        candidate_entities: list[tuple[str, str, str]] | None = None,
    ) -> None:
        raw_dir = self.mirror / "raw" / date[:7] / date
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_name = f"{report_id}-{title}.md"
        raw_path = raw_dir / raw_name
        raw_path.write_text(_raw(report_id, title, writer, date, raw_body), encoding="utf-8")
        raw_rel = f"../../raw/{date[:7]}/{date}/{raw_name}"
        summary_path = self.mirror / "wiki" / "summaries" / f"{report_id}.md"
        summary_path.write_text(
            _summary(
                report_id, title, writer, date, raw_rel, summary_text,
                key_facts=key_facts, candidate_entities=candidate_entities,
            ),
            encoding="utf-8",
        )

    def write_registry(self, entries: list[dict]) -> Path:
        """Write a fixture registry and return its path.

        The loader normally prefers the repo config, so callers must
        pass the returned path to ``build_catalog(registry_paths=...)``
        for the fixture to take effect.
        """
        path = self.mirror / "wiki" / "_system" / "entity-family-registry.json"
        path.write_text(
            json.dumps(
                {"schema_version": ec.REGISTRY_SCHEMA, "version": "test", "entries": entries},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._registry_path = path
        return path

    def build_catalog(self, *, scan_raw: bool = True) -> dict:
        override = [self._registry_path] if getattr(self, "_registry_path", None) else None
        payload, cache, _src = ec.build_catalog(
            self.mirror, scan_raw=scan_raw, use_cache=False,
            registry_paths=override,
        )
        ec.write_catalog(self.mirror, payload, cache)
        return payload

    def query(self, question: str, **kwargs) -> dict:
        return wq.query_mirror(self.mirror, question, use_index=False, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class OverlapSweepTests(unittest.TestCase):
    def test_sweep_matches_naive_reference_across_random_inputs(self):
        random.seed(2026)
        for trial in range(150):
            n = random.randint(0, 40)
            hits = []
            for _ in range(n):
                start = random.randint(0, 30)
                length = random.randint(1, 8)
                hits.append((start, start + length, f"p-{trial}"))
            sweep = {(s, e) for s, e, _ in ec.suppress_shorter_overlaps(hits)}
            naive = {(s, e) for s, e, _ in ec._naive_suppress_shorter_overlaps(hits)}
            self.assertEqual(sweep, naive, f"mismatch on trial {trial}: {hits}")

    def test_identical_spans_survive_together(self):
        hits = [(0, 5, "A"), (0, 5, "B")]
        self.assertEqual(
            sorted(ec.suppress_shorter_overlaps(hits)),
            sorted(hits),
        )


class BoundaryAndAnchorTests(unittest.TestCase):
    def test_ascii_surface_rejects_ascii_word_neighbour(self):
        self.assertEqual(ec.find_exact_anchors("ADMINALPHA 部署", "ALPHA"), [])

    def test_ascii_surface_matches_next_to_cjk_prose(self):
        self.assertEqual(ec.find_exact_anchors("正在推进ALPHA项目", "ALPHA"), [4])

    def test_cjk_surface_matches_in_running_text(self):
        self.assertEqual(ec.find_exact_anchors("推动云端虾落地", "云端虾"), [2])

    def test_longest_match_beats_prefix_via_suppression(self):
        ac = ec.AhoCorasick()
        ac.add("云端", ("f", "云端", "云端"))
        ac.add("云端虾", ("f", "云端虾", "云端虾"))
        ac.finalize()
        text = "云端虾服务"
        hits = ac.find_all(text)
        triples = [
            (s, e, pl)
            for s, e, pl in hits
            if ec._boundary_ok(text, s, e, ec._surface_kind(pl[2]))
        ]
        kept = ec.suppress_shorter_overlaps(triples)
        self.assertEqual(sorted({pl[2] for _s, _e, pl in kept}), ["云端虾"])


class NormalizationTests(unittest.TestCase):
    def test_english_intent_variants_resolve_when_registered(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000019", "ALPHA", "ALPHA 相关",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            for variant in ("ALPHA risk", "alpha next plan", "ALPHA progress"):
                resolution = wq.resolve_entity(variant, payload)
                self.assertEqual(resolution.status, "resolved", variant)
        finally:
            mirror.cleanup()

    def test_fullwidth_and_spaced_variants_resolve_to_same_family(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000020", "ALPHA 变体", "ALPHA 相关",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            for variant in ("ALPHA 进展", "alpha next plan", "ＡＬＰＨＡ 风险", "A L P H A 进展"):
                resolution = wq.resolve_entity(variant, payload)
                self.assertEqual(resolution.status, "resolved", variant)
        finally:
            mirror.cleanup()

    def test_ascii_word_boundary_still_rejects_glued_neighbour(self):
        """``ALPHARISK`` must NOT be resolved to the registered
        ``ALPHA`` family.  Whether the resolver labels the shorthand
        as ``unscoped`` or ``unknown`` is a policy choice; the invariant
        is that we never confidently bind it (result set stays empty)."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000021", "ALPHA", "ALPHA 相关",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            resolution = wq.resolve_entity("ALPHARISK is unrelated", payload)
            self.assertNotEqual(resolution.status, "resolved")
            self.assertEqual(resolution.family_id, "")
        finally:
            mirror.cleanup()

    def test_short_registered_entity_not_bound_inside_longer_unknown_noun(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000022", "AB", "AB 是系统",
                candidate_entities=[("AB", "system", "AB")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            # ``ABCDE`` is an unknown longer name that just happens to
            # start with the registered acronym ``AB``; the resolver
            # must not confidently bind it to the AB family.  We accept
            # either ``unknown`` (fail-closed) or ``unscoped``.
            resolution = wq.resolve_entity("ABCDE 项目进展", payload)
            self.assertNotEqual(resolution.status, "resolved")
        finally:
            mirror.cleanup()


class CatalogRuleTests(unittest.TestCase):
    def test_parenthetical_acronym_creates_alias_within_type(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000001", "示例1", "ALPHA 相关",
                candidate_entities=[("ALPHA（示例平台）", "system", "ALPHA")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            families = [
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            ]
            self.assertEqual(len(families), 1)
            surfaces = {s["normalized"] for s in families[0]["surfaces"]}
            self.assertIn("alpha", surfaces)
            self.assertIn("示例平台", surfaces)
        finally:
            mirror.cleanup()

    def test_controlled_generic_suffix_within_same_type(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000002", "示例2", "示例部署",
                candidate_entities=[
                    ("示例A", "system", "示例A"),
                    ("示例A系统", "system", "示例A系统"),
                ],
            )
            payload = mirror.build_catalog(scan_raw=False)
            families = [
                f for f in payload["families"]
                if any(s["normalized"] == "示例a" for s in f["surfaces"])
            ]
            self.assertEqual(len(families), 1)
            surfaces = {s["normalized"] for s in families[0]["surfaces"]}
            self.assertIn("示例a系统", surfaces)
        finally:
            mirror.cleanup()

    def test_cross_type_same_surface_is_ambiguous_without_registry(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000003", "示例3", "示例B 系统",
                candidate_entities=[("示例B", "system", "示例B")],
            )
            mirror.add_report(
                "100000000000000004", "示例4", "示例B 项目",
                candidate_entities=[("示例B", "project", "示例B")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            families = [
                f for f in payload["families"]
                if any(s["normalized"] == "示例b" for s in f["surfaces"])
            ]
            self.assertEqual(len(families), 2)
            resolution = wq.resolve_entity("示例B 项目进展", payload)
            self.assertEqual(resolution.status, "ambiguous")
        finally:
            mirror.cleanup()

    def test_registry_bridges_cross_type_into_single_family(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000005", "示例5", "示例C 系统",
                candidate_entities=[("示例C", "system", "示例C")],
            )
            mirror.add_report(
                "100000000000000006", "示例6", "示例C 项目",
                candidate_entities=[("示例C", "project", "示例C")],
            )
            mirror.write_registry([
                {
                    "entry_id": "cross-type-示例c-unit-test",
                    "canonical_display": "示例C",
                    "members": [
                        {"entity_type": "system", "normalized": "示例c"},
                        {"entity_type": "project", "normalized": "示例c"},
                    ],
                    "evidence": [{"report_id": "100000000000000005", "quote": "示例C 系统"}],
                    "decided_by": "unit-test",
                    "decided_at": "2026-08-18",
                }
            ])
            payload = mirror.build_catalog(scan_raw=False)
            families = [
                f for f in payload["families"]
                if any(s["normalized"] == "示例c" for s in f["surfaces"])
            ]
            # NB: the repo-committed registry is preferred; the mirror
            # registry only wins because its members are 示例C which is
            # not present in the repo entry.
            self.assertEqual(len(families), 1)
            self.assertEqual(sorted(families[0]["entity_types"]), ["project", "system"])
            resolution = wq.resolve_entity("示例C 项目进展", payload)
            self.assertEqual(resolution.status, "resolved")
        finally:
            mirror.cleanup()

    def test_more_than_30_postings_are_all_included(self):
        mirror = SyntheticMirror()
        try:
            for i in range(35):
                mirror.add_report(
                    f"2000000000000000{i:03d}", f"示例{i}", "ALPHA",
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    date="2026-08-01" if i % 2 == 0 else "2026-08-02",
                )
            payload = mirror.build_catalog(scan_raw=False)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            self.assertEqual(family["posting_count"], 35)
        finally:
            mirror.cleanup()

    def test_single_report_entity_still_indexed(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "300000000000000001", "只有一条", "ALPHA",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            self.assertEqual(family["posting_count"], 1)
        finally:
            mirror.cleanup()

    def test_stale_entity_pages_are_ignored(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "400000000000000001", "示例", "ALPHA",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            stale = mirror.mirror / "wiki" / "entities" / "systems" / "ALPHA.md"
            stale.parent.mkdir(parents=True)
            stale.write_text(
                "# ALPHA\n\n- [`999999999999999999`](../../summaries/999999999999999999.md) 陈旧\n",
                encoding="utf-8",
            )
            payload = mirror.build_catalog(scan_raw=False)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            self.assertEqual(family["postings"], ["400000000000000001"])
        finally:
            mirror.cleanup()

    def test_raw_scan_promotes_known_alias_into_postings(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "500000000000000001", "首次登场", "推进ALPHA部署",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            mirror.add_report(
                "500000000000000002", "隐式提及", "本周会议提到 ALPHA 需要与 BRAVO 联调",
                candidate_entities=[("BRAVO", "system", "BRAVO")],
            )
            payload = mirror.build_catalog(scan_raw=True)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            self.assertIn("500000000000000002", family["postings"])
            provenance = family["posting_provenance"]["500000000000000002"]
            raw_records = [row for row in provenance if row.get("origin") == "raw_anchor"]
            self.assertEqual(len(raw_records), 1)
            self.assertIn("match_count", raw_records[0])
        finally:
            mirror.cleanup()

    def test_raw_scan_boundary_rejects_glued_ascii_neighbour(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "600000000000000001", "ALPHA", "ALPHA 上线",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            mirror.add_report(
                "600000000000000002", "无关", "the ALPHAADMIN service does not exist",
                candidate_entities=[("BRAVO", "system", "BRAVO")],
            )
            payload = mirror.build_catalog(scan_raw=True)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            self.assertNotIn("600000000000000002", family["postings"])
        finally:
            mirror.cleanup()

    def test_registry_missing_evidence_is_rejected(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000007", "示例7", "示例D",
                candidate_entities=[("示例D", "system", "示例D")],
            )
            mirror.write_registry([
                {
                    "entry_id": "cross-type-示例d-unit-test",
                    "canonical_display": "示例D",
                    "members": [
                        {"entity_type": "system", "normalized": "示例d"},
                        {"entity_type": "project", "normalized": "示例d"},
                    ],
                    # evidence missing on purpose
                    "decided_by": "unit-test",
                    "decided_at": "2026-08-18",
                }
            ])
            with self.assertRaises(ec.RegistryValidationError):
                mirror.build_catalog(scan_raw=False)
        finally:
            mirror.cleanup()

    def test_registry_missing_members_recorded_as_missing(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "100000000000000008", "示例8", "示例E 系统",
                candidate_entities=[("示例E", "system", "示例E")],
            )
            mirror.write_registry([
                {
                    "entry_id": "cross-type-示例e-unit-test",
                    "canonical_display": "示例E",
                    "members": [
                        {"entity_type": "system", "normalized": "示例e"},
                        {"entity_type": "project", "normalized": "示例e"},
                    ],
                    "evidence": [{"report_id": "100000000000000008", "quote": "示例E"}],
                    "decided_by": "unit-test",
                    "decided_at": "2026-08-18",
                }
            ])
            payload = mirror.build_catalog(scan_raw=False)
            applied = payload["registry"]["applied"]
            self.assertTrue(any(entry.get("missing_members") for entry in applied))
        finally:
            mirror.cleanup()

    def test_raw_scan_only_touches_summary_paired_raws(self):
        """Snapshot-style files under ``raw/_system/timelines/**`` must
        not contribute postings; only raws that have a matching
        summary count."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "700000000000000010", "ALPHA", "ALPHA 提及",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            snapshot_dir = (
                mirror.mirror / "raw" / "_system" / "timelines" / "example" / "snapshots"
            )
            snapshot_dir.mkdir(parents=True)
            snapshot = snapshot_dir / (
                "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08.md"
            )
            snapshot.write_text(
                "---\nreport_id: \"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\"\n---\n\nALPHA 出现在快照\n",
                encoding="utf-8",
            )
            payload = mirror.build_catalog(scan_raw=True)
            summary_ids = {p.stem for p in (mirror.mirror / "wiki" / "summaries").glob("*.md")}
            for family in payload["families"]:
                for posting in family["postings"]:
                    self.assertIn(posting, summary_ids)
        finally:
            mirror.cleanup()

    def test_catalog_hash_is_deterministic_cold_vs_warm(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000010", "cold_vs_warm", "ALPHA 提及",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            cold_payload, cold_cache, _src = ec.build_catalog(
                mirror.mirror, scan_raw=True, use_cache=False,
            )
            ec.write_catalog(mirror.mirror, cold_payload, cold_cache)
            warm_payload, _warm_cache, _src2 = ec.build_catalog(
                mirror.mirror, scan_raw=True, use_cache=True,
            )
            self.assertEqual(cold_payload["catalog_sha256"], warm_payload["catalog_sha256"])
        finally:
            mirror.cleanup()

    def test_duplicate_raw_occurrences_aggregate_to_one_record(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "700000000000000001", "ALPHA", "ALPHA 首次",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            mirror.add_report(
                "700000000000000002", "重复提及",
                "ALPHA 今日上线。ALPHA 明日巡检。ALPHA 结果如下。",
                candidate_entities=[("BRAVO", "system", "BRAVO")],
            )
            payload = mirror.build_catalog(scan_raw=True)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "alpha" for s in f["surfaces"])
            )
            records = family["posting_provenance"]["700000000000000002"]
            raw_records = [row for row in records if row.get("origin") == "raw_anchor"]
            self.assertEqual(len(raw_records), 1)
            self.assertEqual(int(raw_records[0]["match_count"]), 3)
        finally:
            mirror.cleanup()


class ResolverAndQueryTests(unittest.TestCase):
    def _build_fixture(self) -> SyntheticMirror:
        mirror = SyntheticMirror()
        for i in range(5):
            # The raw body embeds every summary quote verbatim so
            # evidence verification succeeds for all three intents
            # (progress / plan / risk).  This lets ResolverAndQueryTests
            # exercise the full bucket_1 path with confirmed intent
            # support, rather than the fail-closed no_query_evidence
            # branch.
            mirror.add_report(
                f"800000000000000{i:03d}", f"ALPHA 进展{i}",
                (
                    f"ALPHA 已完成阶段{i}。"
                    "ALPHA 阶段进展已确认。"
                    "下一步计划推进 A/B/C。"
                    "风险在于 X。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("ALPHA 阶段进展", "ALPHA 阶段进展已确认"),
                    ("下一步计划: 推进 A/B/C", "下一步计划推进 A/B/C"),
                    ("风险在于 X", "风险在于 X"),
                ],
                date="2026-08-01",
            )
        for i in range(3):
            mirror.add_report(
                f"900000000000000{i:03d}", f"其他系统{i}",
                "BRAVO 与运维讨论。",
                candidate_entities=[("BRAVO", "system", "BRAVO")],
                date="2026-08-02",
            )
        mirror.build_catalog(scan_raw=True)
        return mirror

    def test_unscoped_generic_query_stays_unscoped(self):
        mirror = self._build_fixture()
        try:
            payload = mirror.query("项目进展")
            self.assertEqual(payload["entity_resolution"]["status"], "unscoped")
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()

    def test_ascii_nonce_entity_management_query_fails_closed(self):
        mirror = self._build_fixture()
        try:
            payload = mirror.query("ZULU-99 进展")
            self.assertEqual(payload["entity_resolution"]["status"], "unknown")
            self.assertEqual(payload["results"], [])
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()

    def test_long_cjk_nonce_management_query_fails_closed(self):
        """Long unregistered CJK proper-noun sequences combined with a
        management intent still fail closed (unknown), matching the
        heuristic documented in ``_query_looks_entity_shaped``."""
        mirror = self._build_fixture()
        try:
            payload = mirror.query("霜蓝鲸鱼量子披萨-进展")
            self.assertEqual(payload["entity_resolution"]["status"], "unknown")
            self.assertEqual(payload["results"], [])
        finally:
            mirror.cleanup()

    def test_resolved_query_hard_filters_scope_precision_100(self):
        mirror = self._build_fixture()
        try:
            payload = mirror.query("ALPHA 进展")
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            for result in payload["results"]:
                self.assertTrue(result["scope_support"]["in_scope"])
            self.assertEqual(payload["scope"]["scope_precision"], 1.0)
        finally:
            mirror.cleanup()

    def test_resolved_empty_when_family_has_zero_postings(self):
        """A family exists in the catalog (surface known, hash valid)
        but has zero postings – must return resolved_empty, not unknown
        and not filters_removed_all."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000001", "CHARLIE 唯一", "CHARLIE 相关",
                candidate_entities=[("CHARLIE", "system", "CHARLIE")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            family = next(
                f for f in payload["families"]
                if any(s["normalized"] == "charlie" for s in f["surfaces"])
            )
            family["postings"] = []
            family["posting_count"] = 0
            family["posting_provenance"] = {}
            resolution = wq.resolve_entity("CHARLIE 进展", payload)
            self.assertEqual(resolution.status, "resolved_empty")
        finally:
            mirror.cleanup()

    def test_filters_removed_all_promotes_to_resolved_empty(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "A00000000000000002", "ALPHA 早年", "ALPHA 已发生",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                date="2026-08-01",
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 项目进展", from_date="2027-01-01", to_date="2027-12-31")
            self.assertEqual(payload["entity_resolution"]["status"], "resolved_empty")
            self.assertEqual(payload["entity_resolution"]["reason"], "filters_removed_all")
            self.assertEqual(payload["scope"]["filter_reasons"], ["filters_removed_all"])
            self.assertEqual(payload["confidence"], "none")
            self.assertEqual(payload["results"], [])
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()

    def test_no_query_evidence_in_scope_distinct_from_filters_removed_all(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "B00000000000000001", "ALPHA 简短", "ALPHA 是一个平台。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                summary_text="简短说明。",
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 阿米巴文化贯彻")
            self.assertTrue(payload["scope"]["applied"])
            if not payload["results"]:
                self.assertNotEqual(payload["scope"]["filter_reasons"], ["filters_removed_all"])
        finally:
            mirror.cleanup()

    def test_entity_only_query_returns_exact_scope_postings_with_safe_confidence(self):
        mirror = self._build_fixture()
        try:
            payload = mirror.query("ALPHA")
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            actual_ids = {r["report_id"] for r in payload["results"]}
            expected_ids = {f"800000000000000{i:03d}" for i in range(5)}
            self.assertEqual(actual_ids, expected_ids)
            self.assertEqual(payload["confidence"], "medium")
        finally:
            mirror.cleanup()

    def test_multi_entity_query_is_unsupported_not_ambiguous(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "AAA0000000000000030", "ALPHA", "ALPHA 相关",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            mirror.add_report(
                "AAA0000000000000031", "BRAVO", "BRAVO 相关",
                candidate_entities=[("BRAVO", "system", "BRAVO")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            resolution = wq.resolve_entity("ALPHA 与 BRAVO 对比", payload)
            self.assertEqual(resolution.status, "unsupported_multi_entity")
        finally:
            mirror.cleanup()

    def test_generic_management_queries_stay_unscoped(self):
        for query in (
            "本周有哪些风险",
            "最近的项目进展",
            "现在项目进展如何",
            "团队有哪些风险",
        ):
            self.assertFalse(wq._query_looks_entity_shaped(query), query)

    def test_compound_intent_confidence_caps_at_medium_when_missing_intent(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "C00000000000000001", "ALPHA 单意图", "ALPHA 进展如下。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 进展如下", "ALPHA 进展如下")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展与风险")
            self.assertNotEqual(payload["confidence"], "high")
        finally:
            mirror.cleanup()

    def test_intent_order_and_punctuation_are_stable(self):
        canonical = list(wq.INTENT_PATTERNS)
        for query in ("ALPHA 进展与风险", "ALPHA 风险、进展", "ALPHA：进展/风险"):
            intents = wq.detect_intents(query)
            self.assertIn("progress", intents)
            self.assertIn("risk", intents)
            self.assertEqual(intents, sorted(intents, key=canonical.index))

    def test_unscoped_fact_query_preserves_previous_high_confidence(self):
        """Baseline confidence for non-entity queries must stay high on
        strong BM25 matches (RT-010 contract line 10).

        Uses a title that is a substring of the question so the pre-
        existing title-anchor bonus (+18) clears the 14-point ``high``
        threshold on an unscoped query.  This guards against RT-010
        accidentally regressing the unscoped high path via its new
        confidence gate.
        """
        mirror = SyntheticMirror()
        try:
            title = "财务审核两周 Token 10.91 亿"
            fact = f"{title}. {title}. {title}."
            mirror.add_report(
                "E00000000000000001", title, fact,
                summary_text=fact,
                key_facts=[(title, title)],
                candidate_entities=[],
            )
            mirror.build_catalog(scan_raw=True)
            payload = mirror.query(title)
            self.assertEqual(payload["entity_resolution"]["status"], "unscoped")
            self.assertEqual(payload["confidence"], "high")
        finally:
            mirror.cleanup()

    def test_intent_support_uses_only_returned_verified_evidence(self):
        mirror = SyntheticMirror()
        try:
            # Summary declares a risk quote but the raw doesn't mention
            # progress; a progress+risk query must not become high just
            # because the summary happens to include the words.
            mirror.add_report(
                "F00000000000000001", "ALPHA 摘要含风险", "ALPHA 简报只讨论上线。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                summary_text="ALPHA 简报只讨论上线。",
                key_facts=[
                    ("上线记录", "ALPHA 简报只讨论上线。"),
                    ("风险为 X", "风险为 X"),
                ],
            )
            mirror.build_catalog(scan_raw=True)
            payload = mirror.query("ALPHA 进展与风险")
            self.assertNotEqual(payload["confidence"], "high")
        finally:
            mirror.cleanup()

    def test_verified_intent_bucket_beats_verified_no_intent_distractor(self):
        """RT-010 release blocker + intent-aware sort.

        Row A: raw-verified evidence quote AND mentions ``进展``  → bucket 1
        Row B: raw-verified via entity anchor but NO intent keyword → bucket 2
        Row C: unverified evidence quote (repeats query terms) → bucket 3

        The scoped ranking must surface row A first, row B second,
        row C last.  Confidence anchors on row A (bucket 1) so the
        scoped query stays visible at low/medium and never collapses
        to ``none`` because of the high-BM25 unverified distractor.
        """
        mirror = SyntheticMirror()
        try:
            # Bucket 1: verified evidence + progress intent.
            mirror.add_report(
                "V00000000000000010", "ALPHA 阶段进展",
                "ALPHA 阶段进展已确认。风险与规划另议。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认",
                            "ALPHA 阶段进展已确认")],
            )
            # Bucket 2: verified evidence but the quote itself carries
            # NO intent keyword (no 进展/规划/风险/负责).  The raw file
            # DOES contain the summary quote verbatim so evidence
            # verification succeeds; the entity is anchored via the
            # candidate declaration.
            mirror.add_report(
                "V00000000000000011", "ALPHA 上线纪要",
                "ALPHA 已发布，团队人员到齐。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("ALPHA 已发布",
                     "ALPHA 已发布"),
                ],
            )
            # Bucket 3: TRULY unverified.  Raw title AND body must
            # contain none of the query tokens, so both the summary-
            # quote check and the fallback BM25 excerpt search return
            # ``unverified``.  The summary body repeats the query terms
            # to guarantee BM25 Top-1; the entity anchor is provided by
            # the ``candidate_entities`` declaration, so this report is
            # still in scope.
            mirror.add_report(
                "V00000000000000012", "无关标题占位符",
                "完全无关的内容，只出现无意义符号占位。",
                summary_text=(
                    "项目进展 项目进展 项目进展 项目进展 项目进展 "
                    "项目进展 项目进展 项目进展 项目进展 项目进展"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("完全不在原文里的引文",
                     "这条引文完全不在原文里出现，不会通过 hash 验证。"),
                ],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 项目进展", min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            ids = [row["report_id"] for row in payload["results"]]
            self.assertGreater(len(ids), 0)
            # Bucket 1 → then bucket 2.  Bucket 3 must NOT be citeable.
            self.assertEqual(ids[0], "V00000000000000010")
            self.assertIn("V00000000000000011", ids)
            self.assertNotIn(
                "V00000000000000012", ids,
                "unverified bucket_3 row must be dropped from citeable results",
            )
            # Scope diagnostics record that the drop happened.
            self.assertGreaterEqual(payload["scope"]["dropped_unverified_rows"], 1)
            # Confidence anchors on bucket 1 → at least low, at most
            # medium (missing plan/risk intents means not high).
            self.assertIn(payload["confidence"], {"low", "medium"})
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()

    def test_top_k_masking_bounded_pool_rescues_late_verified_row(self):
        """RT-010 masking edge case.

        Ten unverified in-scope distractors dominate BM25 above one
        verified intent-supporting row.  With ``top_k=5`` the naive
        ``ranked[:top_k]`` slice would never see the verified row.  The
        bounded evaluation pool must inspect enough candidates to find
        it, hoist it into bucket_1, drop the distractors, and hand the
        answer layer the true evidence.
        """
        mirror = SyntheticMirror()
        try:
            # 10 unverified but high-BM25 distractors (raw body has no
            # ALPHA / management-intent tokens; summary body repeats
            # them so BM25 boosts them above the verified row).
            for i in range(10):
                mirror.add_report(
                    f"M00000000000000{i:03d}", f"占位标题 {i}",
                    "完全无关的原文内容占位。",
                    summary_text=(
                        "项目进展 项目进展 项目进展 项目进展 项目进展 "
                        "项目进展 项目进展 项目进展 项目进展 项目进展"
                    ),
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    key_facts=[("完全不在原文里的引文",
                                 "这条引文完全不在原文里出现，不会通过 hash 验证。")],
                )
            # Verified intent-supporting row at natural BM25 rank ~11.
            mirror.add_report(
                "M00000000000000200", "ALPHA 阶段进展",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认",
                            "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 项目进展", top_k=5, min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            ids = [row["report_id"] for row in payload["results"]]
            self.assertLessEqual(
                len(ids), 5, "citeable results must be trimmed back to top_k",
            )
            self.assertIn(
                "M00000000000000200", ids,
                "verified intent-supporting row must survive top_k masking",
            )
            self.assertEqual(
                ids[0], "M00000000000000200",
                "bucket_1 row must rank first regardless of BM25",
            )
            # All 10 unverified distractors were dropped.
            self.assertGreaterEqual(
                payload["scope"]["dropped_unverified_rows"], 10,
                f"expected >=10 dropped unverified rows, got"
                f" {payload['scope']['dropped_unverified_rows']}",
            )
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()

    def test_intents_missing_recomputed_after_top_k_trim(self):
        """Blocker 1 regression.

        top_k=1 with one row supporting progress but a lower-ranked row
        supporting risk.  After trim, only the progress row remains
        citeable, so risk MUST be reported missing and confidence MUST
        NOT be ``high``.  The old code aggregated intent verification
        across the full evaluation pool and would falsely report both
        intents verified.
        """
        mirror = SyntheticMirror()
        try:
            # Bucket_1 row that supports progress only.  Raw contains
            # the verified evidence quote for both entity and intent.
            mirror.add_report(
                "T00000000000000001", "ALPHA 进展",
                "ALPHA 进展报告，进展稳定，本周确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 进展稳定", "ALPHA 进展稳定，本周确认")],
            )
            # Second in-scope row supports risk only.  Would join the
            # evaluation pool but must be trimmed away at top_k=1.
            mirror.add_report(
                "T00000000000000002", "ALPHA 风险",
                "ALPHA 风险台账，风险为 X。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 风险为 X", "ALPHA 风险为 X")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展与风险", top_k=1)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertEqual(len(payload["results"]), 1)
            # Only progress verified; risk still missing.
            self.assertIn("progress", payload["intents"]["verified"])
            self.assertNotIn("risk", payload["intents"]["verified"])
            self.assertIn("risk", payload["intents"]["missing"])
            self.assertNotEqual(payload["confidence"], "high")
        finally:
            mirror.cleanup()

    def test_iterative_batches_recover_late_verified_intent_row(self):
        """Blocker 2 regression.

        Forty in-scope raw-verified rows dominate BM25 for the residual
        query (their summary bodies repeat the intent keyword many
        times) but carry NO intent-supporting evidence quote.  The one
        valid intent-supporting row has minimal BM25 and lands past
        rank 32.  A fixed 32-row evaluation pool would miss it; the
        iterative batch strategy MUST continue within the scoped
        candidate list until it finds a locally-linked intent hit or
        the scope is exhausted.
        """
        mirror = SyntheticMirror()
        try:
            # Distractors: high BM25 for ``ALPHA 进展`` via repeated
            # summary body tokens, but the only key_fact quote does not
            # contain the intent keyword, so no bucket_1 hit.  Raw body
            # contains ALPHA so the report is in scope and the summary
            # quote verifies.
            for i in range(40):
                mirror.add_report(
                    f"B00000000000000{i:03d}",
                    f"占位标题 {i}",
                    (
                        f"ALPHA 侧记 {i}。ALPHA 上线纪要。"
                    ),
                    summary_text=(
                        "进展 进展 进展 进展 进展 进展 进展 进展 "
                        "进展 进展 进展 进展 进展 进展 进展 进展"
                    ),
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    key_facts=[("ALPHA 上线纪要", "ALPHA 上线纪要")],
                )
            # Late row: minimal summary body ⇒ low BM25, but intent-
            # supporting evidence quote co-occurs with ALPHA anchor in
            # raw (same paragraph).
            mirror.add_report(
                "B00000000000000200",
                "内部备忘",
                "ALPHA 阶段进展已确认。",
                summary_text="内部备忘",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认"),
                ],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=8, min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            ids = [row["report_id"] for row in payload["results"]]
            self.assertIn(
                "B00000000000000200", ids,
                "iterative batches must reach past rank 32 to surface"
                " a late verified intent-supporting row",
            )
            self.assertEqual(
                ids[0], "B00000000000000200",
                "recovered bucket_1 row must lead ranking",
            )
            self.assertGreater(
                payload["scope"].get("evaluated_pool_size", 0), 32,
                "evaluation must exceed the initial 32-row batch",
            )
        finally:
            mirror.cleanup()

    def test_persistent_index_without_catalog_sha_fails_closed_for_entity_management(
        self,
    ):
        """Blocker 3 regression.

        A persistent index built before RT-010 (no ``entity_catalog_
        sha256`` field) must fail closed for entity-management queries
        even when ``require_catalog=False``, so an operator running the
        old CLI cannot accidentally quote answers from a scope that no
        one bound at build time.  A plain unscoped fact query is
        unaffected — it still runs on BM25 as before.
        """
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "S00000000000000001", "ALPHA 基础事实",
                "ALPHA 基础事实占位。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 基础事实占位", "ALPHA 基础事实占位")],
            )
            mirror.build_catalog(scan_raw=False)
            # Rebuild a full search index (has entity_catalog_sha256).
            import cwk_wiki_search_index as swi
            swi.build_index(mirror.mirror, force=True)
            # Corrupt: strip the sha bindings from the on-disk payload
            # to simulate an old pre-RT-010 persistent index.
            system = mirror.mirror / "wiki" / "_system"
            import gzip as _gz
            gz = system / "search-index.json.gz"
            with _gz.open(gz, "rt", encoding="utf-8") as h:
                payload_index = json.load(h)
            payload_index.pop("entity_catalog_sha256", None)
            payload_index.pop("entity_catalog_schema", None)
            payload_index.pop("entity_catalog_registry_version", None)
            # Recompute the index sha256 over the stripped payload so
            # the loader still accepts it as a valid persistent index.
            import hashlib as _hashlib
            core = {
                k: v for k, v in payload_index.items()
                if k not in {"index_version", "index_sha256", "generated_at"}
            }
            new_sha = _hashlib.sha256(
                json.dumps(core, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            payload_index["index_sha256"] = new_sha
            with _gz.open(gz, "wt", encoding="utf-8") as h:
                json.dump(payload_index, h, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
            meta_path = system / "index-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["index_sha256"] = new_sha
            meta.pop("entity_catalog_sha256", None)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            plain = system / "search-index.json"
            if plain.exists():
                plain.unlink()
            # Invalidate any in-process index cache from earlier builds.
            wq._INDEX_CACHE.clear()

            # Entity-management query MUST fail closed.
            entity_payload = wq.query_mirror(
                mirror.mirror, "ALPHA 项目进展", use_index=True,
                require_catalog=False,
            )
            self.assertEqual(
                entity_payload["entity_resolution"]["status"], "unknown",
            )
            self.assertEqual(entity_payload["results"], [])
            self.assertFalse(entity_payload["global_fallback_used"])

            # Plain unscoped fact query MUST still return normally.
            fact_payload = wq.query_mirror(
                mirror.mirror, "基础事实", use_index=True,
                require_catalog=False,
            )
            self.assertIn(
                fact_payload["entity_resolution"]["status"],
                {"unscoped", "resolved", "resolved_empty"},
            )
        finally:
            mirror.cleanup()

    def test_summary_candidate_alone_is_not_report_level_strong(self):
        """A structured candidate declaration only proves the summary
        mentions the entity; it must NOT let an intent keyword elsewhere
        in raw silently support the entity query.  Title/H1 remains the
        only report-level strong linkage source.
        """
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SC0000000000000001", "BETA 备忘",
                (
                    "第一节标题：项目回顾\n\n"
                    "ALPHA 出现在参会名单中。\n\n"
                    "第二节标题：进展与规划\n\n"
                    "进展为 X。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("参会名单", "ALPHA 出现在参会名单中")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            # ALPHA is a scoped entity; but the report is not
            # report-level linked (title says BETA), and intent
            # ``进展`` sits in a different section from any ALPHA
            # anchor.  Result must be resolved_empty.
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                payload["entity_resolution"],
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"],
                "no_query_evidence_in_scope",
            )
        finally:
            mirror.cleanup()

    def test_same_block_and_same_heading_subtree_accept_local_link(self):
        """Weak-linkage rows accept intent linkage when the intent
        keyword sits in the same block or the same heading subtree as
        an approved family surface — the auditable local relation
        contract."""
        mirror = SyntheticMirror()
        try:
            # Same block.
            mirror.add_report(
                "L0000000000000001", "BETA 会议",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
            )
            # Same heading subtree (different paragraph, same section).
            mirror.add_report(
                "L0000000000000002", "BETA 备忘",
                (
                    "# 项目状态\n\n"
                    "本周主要工作包括 ALPHA 相关子模块。\n\n"
                    "进展稳定，无阻塞。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关子模块", "ALPHA 相关子模块")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4, min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            ids = [r["report_id"] for r in payload["results"]]
            self.assertIn("L0000000000000001", ids)
            self.assertIn("L0000000000000002", ids)
            self.assertIn("progress", payload["intents"]["verified"])
            for row in payload["results"]:
                self.assertEqual(row.get("entity_linkage"), "local")
                self.assertIn("progress", row.get("intent_links") or {})
                link = row["intent_links"]["progress"]
                self.assertIn(link["relation"], {"same_block", "same_sentence", "heading_ancestor", "same_leaf_section"})
                self.assertEqual(link["intent"], "progress")
                self.assertIn("anchor_block", link)
                self.assertIn("relation", link)
                self.assertIn("distance", link)
                self.assertIn("raw_sha256", link)
        finally:
            mirror.cleanup()

    def test_flattened_markdown_still_parses_into_blocks(self):
        """Flattened markdown (soft heading = short line ending in
        ``：``) must still produce a heading block so downstream
        intent linkage can honour section boundaries."""
        mirror = SyntheticMirror()
        try:
            content = (
                "回顾：\n\n"
                "ALPHA 前期上线。\n\n"
                "进展与风险：\n\n"
                "本周风险为 Y。"
            )
            mirror.add_report(
                "F0000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 前期上线", "ALPHA 前期上线")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            # Risk sits under a different soft heading; ALPHA sits
            # under 回顾.  No local link → resolved_empty.
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                payload["entity_resolution"],
            )
        finally:
            mirror.cleanup()

    def test_second_entity_occurrence_can_provide_local_link(self):
        """Rescanning ALL approved surfaces in raw lets a later
        occurrence of the entity satisfy the local-link contract even
        when the first occurrence sits in an unrelated section."""
        mirror = SyntheticMirror()
        try:
            content = (
                "# 会议记录\n\n"
                "ALPHA 出现在议程列表。\n\n"
                "# 进展汇报\n\n"
                "ALPHA 本周阶段进展已确认。"
            )
            mirror.add_report(
                "R0000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 本周阶段进展已确认",
                             "ALPHA 本周阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertIn("progress", payload["intents"]["verified"])
            row = payload["results"][0]
            self.assertEqual(row.get("entity_linkage"), "local")
            link = row["intent_links"]["progress"]
            self.assertIn(link["relation"], {"same_block", "same_sentence", "heading_ancestor", "same_leaf_section"})
        finally:
            mirror.cleanup()

    def test_bp_multi_goal_generic_mention_is_rejected(self):
        """BP-style multi-goal report where ALPHA is mentioned once as
        a process detail while a generic progress paragraph lives
        elsewhere must NOT support ALPHA-progress.  The two paragraphs
        are in different heading subtrees and >256 chars apart."""
        mirror = SyntheticMirror()
        try:
            # Two long distinct sections, each >256 chars, guaranteeing
            # separation from the ALPHA mention block.
            filler = "全文占位内容 " * 40
            content = (
                "# 六大战略目标\n\n"
                "1) 目标 A: 提升覆盖\n"
                "2) 目标 B: 稳定运营，涉及 ALPHA 数据接入\n"
                "3) 目标 C: 精细化管理\n\n"
                f"{filler}\n\n"
                "# 整体进展汇报\n\n"
                "整体进展稳定。风险与规划下节讨论。\n\n"
                f"{filler}\n"
            )
            mirror.add_report(
                "P0000000000000001", "六大战略目标 BP", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 数据接入", "ALPHA 数据接入")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                payload["entity_resolution"],
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"],
                "no_query_evidence_in_scope",
            )
        finally:
            mirror.cleanup()

    def test_generic_sibling_section_addition_does_not_add_support(self):
        """Property: adding a generic progress / risk / plan paragraph
        in a sibling section far from any entity anchor must NEVER
        upgrade a row from ``local``/``weak`` to ``verified`` intent
        support.
        """
        mirror = SyntheticMirror()
        try:
            # Baseline: strong local link (same block).
            base_content = (
                "# 项目状态\n\n"
                "ALPHA 阶段进展已确认。"
            )
            mirror.add_report(
                "P1000000000000001", "BETA 备忘", base_content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            baseline = mirror.query("ALPHA 进展 风险", top_k=4)
            baseline_verified = set(baseline["intents"]["verified"])
            baseline_missing = set(baseline["intents"]["missing"])
            mirror.cleanup()

            # Now add a generic risk paragraph in a sibling section
            # >256 chars away from any ALPHA anchor.  The set of
            # verified intents must not change.
            mirror = SyntheticMirror()
            filler = "全文占位内容 " * 60
            perturbed_content = (
                "# 项目状态\n\n"
                "ALPHA 阶段进展已确认。\n\n"
                f"{filler}\n\n"
                "# 其他团队风险\n\n"
                "本周整体风险为 Y。"
            )
            mirror.add_report(
                "P1000000000000001", "BETA 备忘", perturbed_content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            perturbed = mirror.query("ALPHA 进展 风险", top_k=4)
            self.assertEqual(
                set(perturbed["intents"]["verified"]), baseline_verified,
                "sibling generic risk paragraph must not upgrade support",
            )
            self.assertEqual(
                set(perturbed["intents"]["missing"]), baseline_missing,
            )
        finally:
            mirror.cleanup()

    def test_title_report_level_link_requires_local_anchor_for_intent(self):
        """RT-010 follow-up (blocker C): the raw H1 proves entity
        SCOPE but must NOT authorise distant intent evidence on its
        own.  When ALPHA sits only in the raw H1 (outside the
        ``<content>`` envelope) and the body carries the intent
        keyword in a separate section, every intent needs a locally
        auditable link and the row must fail closed to
        ``resolved_empty / no_query_evidence_in_scope``.
        """
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "H0000000000000001", "ALPHA 阶段进展",
                (
                    "# 会议记录\n\n"
                    "本次讨论覆盖多个议题。\n\n"
                    "# 阶段进展\n\n"
                    "本周阶段进展已确认。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("本周阶段进展已确认", "本周阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "H1-only ALPHA cannot authorise a distant intent link",
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"],
                "no_query_evidence_in_scope",
            )

        finally:
            mirror.cleanup()

    def test_title_strong_row_with_local_anchor_still_verifies(self):
        """When the raw H1 names the family AND the raw content also
        carries a locally-linked ALPHA + intent co-occurrence, the row
        may still verify the intent – but the link must come from the
        LOCAL raw anchor, not from the H1 alone."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "H0000000000000002", "ALPHA 阶段进展",
                (
                    "# 会议记录\n\n"
                    "ALPHA 本周阶段进展已确认。\n"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 本周阶段进展已确认",
                             "ALPHA 本周阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertIn("progress", payload["intents"]["verified"])
            row = payload["results"][0]
            self.assertEqual(row.get("entity_linkage"), "strong")
            link = row["intent_links"]["progress"]
            self.assertIn(link["relation"],
                           {"same_block", "same_sentence",
                            "heading_ancestor", "same_leaf_section"})
        finally:
            mirror.cleanup()

    def test_weak_raw_only_anchor_needs_intent_cooccurrence(self):
        """Blocker 4 regression.

        Two in-scope rows:

        - Strong: entity in title and candidate declaration, plus a
          verified quote for progress.
        - Weak: raw contains an ALPHA mention (raw_anchor only) AND a
          risk sentence elsewhere in raw.  The two do not co-occur in
          any bounded excerpt, so the weak row must NOT contribute
          risk verification to ``intents.verified`` / ``query_support``.
        """
        mirror = SyntheticMirror()
        try:
            # Strong-linkage row.
            mirror.add_report(
                "W00000000000000001", "ALPHA 阶段进展",
                "ALPHA 阶段进展已确认。ALPHA 进展如下。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认"),
                ],
            )
            # Weak-linkage row.  Summary declares BETA candidate, so
            # ALPHA lands here only via a raw substring anchor.  Risk
            # sentence sits in a separate paragraph from ALPHA.
            mirror.add_report(
                "W00000000000000002", "BETA 备忘",
                (
                    "第一段：BETA 状态记录。\n\n"
                    "第二段：昨日 ALPHA 出现在监控画面中。\n\n"
                    "第三段：项目风险为 X，需持续跟进。"
                ),
                candidate_entities=[("BETA", "system", "BETA")],
                key_facts=[("BETA 状态记录", "BETA 状态记录")],
            )
            mirror.build_catalog(scan_raw=True)
            payload = mirror.query("ALPHA 进展与风险", top_k=4)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            # Strong row leads ranking.
            self.assertEqual(payload["results"][0]["report_id"],
                              "W00000000000000001")
            # Weak-linkage row is present in scope but does not contribute
            # risk verification.
            weak = next(
                (r for r in payload["results"] if r["report_id"] == "W00000000000000002"),
                None,
            )
            if weak is not None:
                self.assertEqual(weak.get("entity_linkage"), "weak")
                self.assertFalse(
                    (weak.get("intent_support") or {}).get("risk", False),
                    "risk sentence in a separate paragraph must not"
                    " support the ALPHA family via a raw-only anchor",
                )
            self.assertNotIn(
                "risk", payload["intents"]["verified"],
                "risk must remain missing when no strong-linkage row"
                " supports it",
            )
            self.assertNotEqual(payload["confidence"], "high")
        finally:
            mirror.cleanup()

    def test_no_verified_evidence_in_scope_becomes_resolved_empty(self):
        """Distinct from the row-reorder path: if *every* in-scope
        candidate has unverified evidence, RT-010's safe-empty contract
        requires a ``resolved_empty`` verdict with a distinct reason."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "V00000000000000010", "ALPHA 未验证",
                "raw body has no evidence for the summary quote",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[
                    ("summary quote never appears in raw",
                     "this quote never appears in raw content"),
                ],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 项目进展")
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "all in-scope rows unverified must resolve empty",
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"], "no_query_evidence_in_scope",
            )
            self.assertEqual(payload["results"], [])
            self.assertFalse(payload["global_fallback_used"])
            self.assertEqual(
                payload["scope"]["filter_reasons"], ["no_query_evidence_in_scope"],
            )
        finally:
            mirror.cleanup()

    def test_catalog_index_hash_mismatch_fails_closed_for_entity_query(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "D00000000000000001", "ALPHA", "ALPHA 相关",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
            )
            mirror.build_catalog(scan_raw=False)
            (mirror.mirror / "wiki" / "_system" / "entity-catalog.json").write_text(
                "{}", encoding="utf-8"
            )
            (mirror.mirror / "wiki" / "_system" / "entity-catalog.json.gz").unlink(missing_ok=True)
            payload = wq.query_mirror(
                mirror.mirror, "ALPHA 项目进展", use_index=False, require_catalog=True
            )
            self.assertEqual(payload["entity_resolution"]["status"], "unknown")
            self.assertFalse(payload["global_fallback_used"])
        finally:
            mirror.cleanup()


# ---------------------------------------------------------------------------
# RT-010 final hardening (independent audit blockers A–G).
# Each test targets one blocker with a synthetic fixture and never
# hardcodes a real report id or rank.  Fixtures avoid TBS to keep the
# implementation independent of any single acronym.
# ---------------------------------------------------------------------------


class FinalHardeningStructuralLeakageTests(unittest.TestCase):
    """Blocker A: compute_entity_intent_links must not leak across
    sibling headings, adjacent list items, table rows, blockquotes,
    or single-newline structural boundaries just because
    distance ≤ 256."""

    def test_sibling_headings_do_not_link(self):
        mirror = SyntheticMirror()
        try:
            content = (
                "# 目标 A\n\n"
                "本节涉及 ALPHA 相关背景描述。\n\n"
                "# 目标 B\n\n"
                "本节风险为 X。"
            )
            mirror.add_report(
                "AH000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关背景描述", "ALPHA 相关背景描述")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "sibling H1 sections must not link ALPHA to 风险",
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"],
                "no_query_evidence_in_scope",
            )
        finally:
            mirror.cleanup()

    def test_adjacent_list_items_do_not_link(self):
        mirror = SyntheticMirror()
        try:
            content = (
                "# 今日事项\n\n"
                "- 完成 ALPHA 数据接入\n"
                "- 明日预留时间处理 X 风险\n"
                "- 例行会议\n"
            )
            mirror.add_report(
                "AL000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("完成 ALPHA 数据接入", "完成 ALPHA 数据接入")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "adjacent list items must not link ALPHA to a sibling 风险",
            )
        finally:
            mirror.cleanup()

    def test_adjacent_table_rows_do_not_link(self):
        mirror = SyntheticMirror()
        try:
            content = (
                "# 表格记录\n\n"
                "| 项目 | 情况 |\n"
                "| --- | --- |\n"
                "| ALPHA | 进度稳定 |\n"
                "| BETA | 存在风险 |\n"
            )
            mirror.add_report(
                "AR000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA", "ALPHA")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "different table rows must not link ALPHA to a sibling 风险",
            )
        finally:
            mirror.cleanup()

    def test_blockquote_boundary_rejects_link(self):
        mirror = SyntheticMirror()
        try:
            content = (
                "# 会议记录\n\n"
                "ALPHA 出现在参会名单中。\n\n"
                "> 摘录：本周整体风险为 Y。\n"
            )
            mirror.add_report(
                "AB000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("参会名单", "ALPHA 出现在参会名单中")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "blockquote is structurally distinct from surrounding para",
            )
        finally:
            mirror.cleanup()

    def test_flattened_inline_headings_are_split(self):
        """A raw line that carries an inline ``## 风险`` marker without
        a leading newline must be split so the intent cannot borrow
        an ALPHA anchor from a preceding inline paragraph."""
        mirror = SyntheticMirror()
        try:
            content = (
                "ALPHA 前期上线简报 ## 风险 本周整体风险为 Y"
            )
            mirror.add_report(
                "AF000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 前期上线简报", "ALPHA 前期上线简报")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "inline ## marker must split blocks, no cross-section link",
            )
        finally:
            mirror.cleanup()

    def test_single_newline_structural_boundary_rejects_link(self):
        """A single ``\\n`` structural gap between the ALPHA paragraph
        and the risk paragraph must not authorise a bounded-window
        fallback link."""
        mirror = SyntheticMirror()
        try:
            content = (
                "# 项目状态\n\n"
                "ALPHA 阶段小结如下。\n"
                "另有会议纪要归档。\n\n"
                "- 本周风险: X\n"
            )
            mirror.add_report(
                "AN000000000000001", "BETA 备忘", content,
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段小结", "ALPHA 阶段小结如下")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "single-newline gap + list item boundary must block link",
            )
        finally:
            mirror.cleanup()


class FinalHardeningTitleBoundaryTests(unittest.TestCase):
    """Blocker B: strong linkage must come from canonical raw H1
    verified with exact-boundary matching, never from a forged summary
    title or a bare substring."""

    def test_forged_summary_title_does_not_grant_strong_linkage(self):
        """The summary title / body claims ALPHA in H1, but the raw H1
        actually says BETA.  Strong linkage must come from raw, not
        from the summary layer."""
        mirror = SyntheticMirror()
        try:
            # Create the raw with an explicit BETA H1 that does NOT
            # mention ALPHA anywhere in its title.  Include an ALPHA
            # anchor deeper in raw for scope resolution.
            raw_body = (
                "ALPHA 在正文中提及。\n\n"
                "本周整体进展稳定。"
            )
            # We craft the summary directly so its # heading claims
            # ALPHA, while the raw H1 stays BETA.  Use the summary's
            # title = raw title path but tamper with the summary body
            # heading only.  add_report writes raw.title from ``title``
            # (used both for raw H1 and summary H1); to differ, write
            # raw manually and then override the summary body.
            report_id = "TF000000000000001"
            date = "2026-08-01"
            raw_dir = mirror.mirror / "raw" / date[:7] / date
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_name = f"{report_id}-BETA 会议.md"
            raw_path = raw_dir / raw_name
            raw_path.write_text(_raw(report_id, "BETA 会议", "张三", date, raw_body), encoding="utf-8")
            raw_rel = f"../../raw/{date[:7]}/{date}/{raw_name}"
            summary_path = mirror.mirror / "wiki" / "summaries" / f"{report_id}.md"
            summary_path.write_text(
                _summary(
                    report_id, "ALPHA 阶段进展", "张三", date, raw_rel,
                    "示例摘要", key_facts=[("ALPHA 在正文中提及", "ALPHA 在正文中提及")],
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                ),
                encoding="utf-8",
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            # Row is in scope but must not enjoy report-level strong
            # linkage – the raw H1 does not contain ALPHA.
            for row in payload.get("results", []):
                if row["report_id"] == report_id:
                    self.assertNotEqual(
                        row.get("entity_linkage"), "strong",
                        "forged summary title must not grant strong linkage",
                    )
        finally:
            mirror.cleanup()

    def test_bare_substring_ab_inside_table_is_not_strong(self):
        """The raw H1 contains ``TABLE`` (which literally contains
        ``AB``).  If ``AB`` is a registered surface, it must NOT
        satisfy strong linkage via bare substring – only exact-boundary
        anchors qualify."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "TS000000000000001", "TABLE 会议",
                "AB 出现在正文中。会议关于 TABLE 的进展。",
                candidate_entities=[("AB", "system", "AB")],
                key_facts=[("AB", "AB")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("AB 进展", top_k=4)
            for row in payload.get("results", []):
                if row["report_id"] == "TS000000000000001":
                    self.assertNotEqual(
                        row.get("entity_linkage"), "strong",
                        "AB substring in TABLE is not a boundary-verified anchor",
                    )
        finally:
            mirror.cleanup()

    def test_glued_ascii_neighbour_in_title_is_not_strong(self):
        """Raw H1 ``ALPHABETA``: ``ALPHA`` substring hits at offset 0
        but the ASCII-word boundary check rejects it.  Strong linkage
        must fail closed for the glued neighbour."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "TG000000000000001", "ALPHABETA release",
                "ALPHA 是独立系统。ALPHA 阶段进展如下。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展", "ALPHA 阶段进展如下")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            for row in payload.get("results", []):
                if row["report_id"] == "TG000000000000001":
                    # Row is in scope via raw substring/body anchor; but
                    # the H1 says ALPHABETA, not ALPHA -> not strong.
                    self.assertNotEqual(
                        row.get("entity_linkage"), "strong",
                        "glued ASCII neighbour must not grant strong linkage",
                    )
        finally:
            mirror.cleanup()


class FinalHardeningIterationCompletenessTests(unittest.TestCase):
    """Blocker C: iteration must cover every requested intent, not
    stop after the first.  Test that a compound-intent query with
    risk evidence buried at rank 41 behind progress rows still
    surfaces the risk row."""

    def test_risk_evidence_at_rank_41_is_recovered(self):
        mirror = SyntheticMirror()
        try:
            # 40 distractors: their SUMMARY body contains both intent
            # keywords (so BM25 is high) but their key_fact quote only
            # proves progress against raw; a fake "risk" quote in the
            # summary body never appears in raw, so risk stays
            # unverified for the distractors.
            for i in range(40):
                mirror.add_report(
                    f"IC000000000000{i:03d}", f"占位 {i}",
                    (
                        f"ALPHA 阶段进展已确认 {i}."
                    ),
                    summary_text=(
                        "ALPHA 进展 风险 进展 风险 进展 风险 进展 风险 "
                        "进展 风险 进展 风险 进展 风险 进展 风险 进展 风险"
                    ),
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
                )
            # Late row (rank ≥ 41): minimal summary body ⇒ low BM25,
            # but raw has a locally-linked ALPHA + risk sentence and
            # the summary key_fact quote proves risk against raw.
            mirror.add_report(
                "IC000000000000200",
                "内部备忘",
                "ALPHA 风险已识别。",
                summary_text="备忘",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("识别", "ALPHA 风险已识别")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展 风险", top_k=8, min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertIn(
                "risk", payload["intents"]["verified"],
                "risk intent must recover the late-verified row",
            )
            self.assertIn(
                "progress", payload["intents"]["verified"],
                "progress must remain verified",
            )
            self.assertGreater(
                payload["scope"].get("evaluated_pool_size", 0), 32,
                "iteration must sweep past the initial 32-row batch",
            )
            ids = [row["report_id"] for row in payload["results"]]
            self.assertIn(
                "IC000000000000200", ids,
                "top_k must retain the risk-supporting row after diversify",
            )
        finally:
            mirror.cleanup()


class FinalHardeningEmptyStatusTests(unittest.TestCase):
    """Blocker D: when bucket filtering leaves results empty, always
    return resolved_empty / no_query_evidence_in_scope."""

    def test_all_candidates_unverified_becomes_resolved_empty(self):
        mirror = SyntheticMirror()
        try:
            # Three in-scope rows whose summary evidence quotes never
            # appear in the corresponding raw body -> all evidence
            # unverified -> bucket_1 and bucket_2 empty.
            for i in range(3):
                mirror.add_report(
                    f"EM000000000000{i:03d}",
                    f"ALPHA 未验证 {i}",
                    "raw body has no matching evidence for the summary quote",
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    key_facts=[
                        (f"summary quote {i} never appears in raw",
                         f"this quote {i} never appears in raw content"),
                    ],
                )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 项目进展 风险", top_k=8)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
            )
            self.assertEqual(
                payload["entity_resolution"]["reason"],
                "no_query_evidence_in_scope",
            )
            self.assertEqual(payload["results"], [])
            self.assertFalse(payload["global_fallback_used"])
            self.assertEqual(
                payload["scope"]["filter_reasons"],
                ["no_query_evidence_in_scope"],
            )
            self.assertGreater(
                payload["scope"]["postings_size"], 0,
                "scope still has postings, but none survived the bucket filter",
            )
        finally:
            mirror.cleanup()


class FinalHardeningCatalogBindingTests(unittest.TestCase):
    """Blocker E: bare entity-only queries must fail closed when
    catalog binding is broken; plain fact queries must be unaffected."""

    def test_bare_alpha_fails_closed_when_catalog_missing(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "BC000000000000001", "ALPHA", "ALPHA 相关内容",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关内容", "ALPHA 相关内容")],
            )
            mirror.build_catalog(scan_raw=False)
            # Build persistent index (has entity_catalog_sha256).
            import cwk_wiki_search_index as swi
            swi.build_index(mirror.mirror, force=True)
            # Strip the entity_catalog_sha256 to simulate a pre-RT-010
            # persistent index (broken binding).
            system = mirror.mirror / "wiki" / "_system"
            import gzip as _gz
            gz = system / "search-index.json.gz"
            with _gz.open(gz, "rt", encoding="utf-8") as h:
                idx = json.load(h)
            idx.pop("entity_catalog_sha256", None)
            idx.pop("entity_catalog_schema", None)
            idx.pop("entity_catalog_registry_version", None)
            import hashlib as _hashlib
            core = {
                k: v for k, v in idx.items()
                if k not in {"index_version", "index_sha256", "generated_at"}
            }
            new_sha = _hashlib.sha256(
                json.dumps(core, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            idx["index_sha256"] = new_sha
            with _gz.open(gz, "wt", encoding="utf-8") as h:
                json.dump(idx, h, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
            meta_path = system / "index-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["index_sha256"] = new_sha
            meta.pop("entity_catalog_sha256", None)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            plain = system / "search-index.json"
            if plain.exists():
                plain.unlink()
            wq._INDEX_CACHE.clear()

            # Bare entity query MUST fail closed.
            payload = wq.query_mirror(
                mirror.mirror, "ALPHA", use_index=True, require_catalog=False,
            )
            self.assertEqual(
                payload["entity_resolution"]["status"], "unknown",
                "bare ALPHA with broken catalog binding must fail closed",
            )
            self.assertEqual(payload["results"], [])
            self.assertFalse(payload["global_fallback_used"])

            # Plain non-entity fact query MUST still return normally.
            plain_payload = wq.query_mirror(
                mirror.mirror, "内容概况", use_index=True, require_catalog=False,
            )
            self.assertIn(
                plain_payload["entity_resolution"]["status"],
                {"unscoped", "resolved", "resolved_empty"},
            )
        finally:
            mirror.cleanup()


class FinalHardeningHighGateTests(unittest.TestCase):
    """Blocker F: high confidence requires a strong row that itself
    supports the requested intents.  A strong-but-intent-quiet row plus
    a local intent row must not yield high."""

    def test_strong_intent_quiet_row_plus_local_intent_row_stays_medium(self):
        mirror = SyntheticMirror()
        try:
            # Strong row: title contains ALPHA (report-level strong),
            # but its key_fact quotes talk about something other than
            # the requested intent (progress).
            mirror.add_report(
                "HG000000000000001", "ALPHA 阶段",
                "ALPHA 是一个 A 类系统。ALPHA 涉及若干模块。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 是一个 A 类系统", "ALPHA 是一个 A 类系统")],
            )
            # Local row: raw has ALPHA + progress co-occurrence.  This
            # supplies progress locally but is not report-level strong.
            mirror.add_report(
                "HG000000000000002", "BETA 备忘",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4, min_score=0.0)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertIn("progress", payload["intents"]["verified"])
            self.assertNotEqual(
                payload["confidence"], "high",
                "strong intent-quiet row + local intent row must not"
                " promote to high",
            )
        finally:
            mirror.cleanup()


class FinalHardeningLinkProvenanceTests(unittest.TestCase):
    """Blocker G: every true intent link must expose auditable surface,
    anchor/intent snippets/offsets, relation, distance and raw sha."""

    def test_link_records_expose_offsets_and_snippets(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "LP000000000000001", "BETA 备忘",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认", "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            row = payload["results"][0]
            link = row["intent_links"]["progress"]
            for key in (
                "surface", "anchor_offset", "anchor_snippet",
                "intent_offset", "intent_snippet", "relation",
                "distance", "raw_sha256", "anchor_heading_path",
                "intent_heading_path", "anchor_block", "intent_block",
            ):
                self.assertIn(key, link, f"missing provenance field {key}")
            # Offsets are inclusive/exclusive pairs of ints.
            self.assertEqual(len(link["anchor_offset"]), 2)
            self.assertEqual(len(link["intent_offset"]), 2)
            self.assertTrue(link["anchor_snippet"])
            self.assertTrue(link["intent_snippet"])
        finally:
            mirror.cleanup()

    def test_candidate_quotes_are_raw_verified_or_dropped(self):
        mirror = SyntheticMirror()
        try:
            # summary declares a candidate quote that ISN'T present in
            # raw – it must be dropped from candidate_quotes.
            mirror.add_report(
                "LP000000000000010", "BETA 备忘",
                "ALPHA 阶段进展。",
                candidate_entities=[("ALPHA", "system", "this candidate quote is not in raw")],
                key_facts=[("ALPHA 阶段进展", "ALPHA 阶段进展")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            row = payload["results"][0]
            scope_support = row.get("scope_support") or {}
            self.assertGreaterEqual(scope_support.get("candidate_quotes_total", 0), 1)
            self.assertEqual(
                scope_support.get("candidate_quotes"), [],
                "unverified candidate quote must be dropped",
            )
        finally:
            mirror.cleanup()


# ---------------------------------------------------------------------------
# RT-010 follow-up (independent audit second pass) — neutral regressions
# for blockers A/B/C/D/E/F/G.  Fixtures avoid any real acronym so the
# implementation stays generic.
# ---------------------------------------------------------------------------


class FollowUpScopedRecallCompletenessTests(unittest.TestCase):
    """Blocker A: scoped candidates must not be pruned by
    ``min_score=0.1``; a deterministic zero-score tail keeps
    verified evidence reachable when the summary body is minimal."""

    def test_scoped_zero_score_tail_recovers_late_verified_row(self):
        mirror = SyntheticMirror()
        try:
            # Distractors with BM25-heavy residual text but no risk
            # evidence in their key_fact quote.
            for i in range(6):
                mirror.add_report(
                    f"AA000000000000{i:03d}", f"占位 {i}",
                    "ALPHA 阶段进展。",
                    summary_text=(
                        "ALPHA 进展 风险 进展 风险 进展 风险 "
                        "进展 风险 进展 风险 进展 风险"
                    ),
                    candidate_entities=[("ALPHA", "system", "ALPHA")],
                    key_facts=[("ALPHA 阶段进展", "ALPHA 阶段进展")],
                )
            # Late in-scope row with essentially zero BM25 for the
            # residual query (title=summary body carries no scoring
            # tokens) but a locally-linked ALPHA + risk sentence in raw.
            mirror.add_report(
                "AA000000000000900", "备忘",
                "ALPHA 风险已识别。",
                summary_text="备忘",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("识别", "ALPHA 风险已识别")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展 风险", top_k=8, min_score=0.1)
            self.assertEqual(payload["entity_resolution"]["status"], "resolved")
            self.assertIn("risk", payload["intents"]["verified"])
            ids = [row["report_id"] for row in payload["results"]]
            self.assertIn(
                "AA000000000000900", ids,
                "zero-score in-scope tail must remain reachable",
            )
        finally:
            mirror.cleanup()


class FollowUpCatalogMismatchCJKTests(unittest.TestCase):
    """Blocker B: catalog mismatch must fail closed for registered
    CJK entities (e.g. ``云端虾``) using the untrusted catalog as an
    entity DETECTOR only, never for ranking.  Generic non-entity
    queries must still run on the unscoped BM25 path."""

    def test_cjk_registered_entity_fails_closed_on_mismatch(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "MC000000000000001", "云端虾申请",
                "云端虾 项目相关记录。",
                candidate_entities=[("云端虾", "system", "云端虾")],
                key_facts=[("云端虾 项目相关记录", "云端虾 项目相关记录")],
            )
            mirror.build_catalog(scan_raw=False)
            # Build persistent index (has entity_catalog_sha256).
            import cwk_wiki_search_index as swi
            swi.build_index(mirror.mirror, force=True)
            # Strip entity_catalog_sha256 to simulate pre-RT-010
            # persistent index (broken catalog binding).
            system = mirror.mirror / "wiki" / "_system"
            import gzip as _gz
            gz = system / "search-index.json.gz"
            with _gz.open(gz, "rt", encoding="utf-8") as h:
                idx = json.load(h)
            idx.pop("entity_catalog_sha256", None)
            idx.pop("entity_catalog_schema", None)
            idx.pop("entity_catalog_registry_version", None)
            import hashlib as _hashlib
            core = {
                k: v for k, v in idx.items()
                if k not in {"index_version", "index_sha256", "generated_at"}
            }
            new_sha = _hashlib.sha256(
                json.dumps(core, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            idx["index_sha256"] = new_sha
            with _gz.open(gz, "wt", encoding="utf-8") as h:
                json.dump(idx, h, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
            meta_path = system / "index-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["index_sha256"] = new_sha
            meta.pop("entity_catalog_sha256", None)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            plain = system / "search-index.json"
            if plain.exists():
                plain.unlink()
            wq._INDEX_CACHE.clear()

            for question in ("云端虾", "云端虾 进展"):
                payload = wq.query_mirror(
                    mirror.mirror, question, use_index=True,
                    require_catalog=False,
                )
                self.assertEqual(
                    payload["entity_resolution"]["status"], "unknown",
                    f"CJK entity {question!r} must fail closed under mismatch",
                )
                self.assertEqual(payload["results"], [])
                self.assertFalse(payload["global_fallback_used"])
            # Generic non-entity query still runs on BM25.
            plain_payload = wq.query_mirror(
                mirror.mirror, "本周有哪些风险", use_index=True,
                require_catalog=False,
            )
            self.assertIn(
                plain_payload["entity_resolution"]["status"],
                {"unscoped", "resolved", "resolved_empty"},
            )
        finally:
            mirror.cleanup()


class FollowUpSemanticLinkageTests(unittest.TestCase):
    """Blocker C: same-block links are bounded by distance + sentence
    terminator + newline; heading-ancestor links are bounded by
    distance OR direct-child depth; multi-space indents and inline
    list/table/quote markers are recovered by the block parser."""

    def test_two_statements_in_one_paragraph_do_not_cross_link(self):
        """``ALPHA 已上线。BETA项目存在风险`` – one paragraph, two
        sentences, unrelated subjects.  ALPHA must NOT link 风险."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000001", "跨主体备忘",
                "ALPHA 已上线。BETA项目存在风险。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 已上线", "ALPHA 已上线")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "sentence terminator blocks intent linkage within a block",
            )
        finally:
            mirror.cleanup()

    def test_newline_between_intent_and_anchor_blocks_link(self):
        """Same paragraph but the anchor and intent are on different
        wrapped lines separated by ``\\n``.  With ``_same_sentence``
        treating any newline as a structural gap the intent cannot
        link an anchor across the wrap."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000002", "换行备忘",
                "ALPHA 上线纪要\n本周风险 X 待跟进",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 上线纪要", "ALPHA 上线纪要")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
            )
        finally:
            mirror.cleanup()

    def test_distance_gt_421_chars_within_block_blocks_link(self):
        """Long single paragraph where ALPHA sits at the head and
        风险 sits > 421 chars away.  Even without a sentence
        terminator the intent must not link (raw distance exceeds
        ``_INTENT_LINK_WINDOW`` = 256)."""
        mirror = SyntheticMirror()
        try:
            filler = "内容占位 " * 80  # >421 chars
            mirror.add_report(
                "SL000000000000003", "长段备忘",
                f"ALPHA 相关记录 {filler} 本周风险 X",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关记录", "ALPHA 相关记录")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
            )
        finally:
            mirror.cleanup()

    def test_flattened_inline_list_items_are_split(self):
        """A stitched paragraph ``完成 ALPHA - 明日风险 X`` must be
        split into distinct blocks so the intent cannot link."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000004", "扁平列表备忘",
                "完成 ALPHA 数据接入 - 明日 X 风险预留",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("完成 ALPHA 数据接入", "完成 ALPHA 数据接入")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
            )
        finally:
            mirror.cleanup()

    def test_flattened_inline_table_rows_are_split(self):
        """Stitched inline table markers ``| ALPHA | ... | 风险 |``
        recover to distinct table_row blocks."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000005", "扁平表格备忘",
                "| ALPHA | 稳定 | | BETA | 风险 |",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA", "ALPHA")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
            )
        finally:
            mirror.cleanup()

    def test_multi_space_indent_recovers_bullet_at_line_start(self):
        """Five spaces / twenty spaces before a bullet must NOT trap
        the bullet inside a paragraph – it recovers as a list_item."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000006", "缩进备忘",
                (
                    "# 今日事项\n\n"
                    "     - 完成 ALPHA 数据接入\n"
                    "                    - 明日 X 风险预留\n"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("完成 ALPHA 数据接入", "完成 ALPHA 数据接入")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "list_item boundary must reject sibling list_item link",
            )
        finally:
            mirror.cleanup()

    def test_root_h1_does_not_link_far_descendant_subsection(self):
        """The root H1 names ALPHA; a subsection >256 chars into the
        document mentions 风险 without ALPHA nearby.  The H1
        ancestor rule must NOT authorise the intent because it
        exceeds the local-window bound AND crosses more than one
        heading level below the root."""
        mirror = SyntheticMirror()
        try:
            filler = "" # start empty; H1 provides distance via subsection depth
            deep_intro = "本节讨论其他项目背景。 " * 40  # >256 chars filler
            mirror.add_report(
                "SL000000000000007", "ALPHA 项目周报",
                (
                    "# ALPHA 项目周报\n\n"
                    "本周整体推进良好。\n\n"
                    "## 其他事项\n\n"
                    "### 小龙虾\n\n"
                    f"{deep_intro}\n\n"
                    "本周小龙虾风险 Y 需关注。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("整体推进良好", "本周整体推进良好")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            # The 风险 keyword lives under ``其他事项 > 小龙虾`` — its
            # ancestor chain includes the H1 that carries ALPHA, but
            # it is > 256 chars away and > 1 heading level deep.  No
            # heading_ancestor link may fire.
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "root H1 must not reach a deep distant subsection intent",
            )
        finally:
            mirror.cleanup()

    def test_evidence_contains_linked_snippet(self):
        """Every intent link must have a corresponding entry in the
        row's evidence that carries the linked raw span."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "SL000000000000008", "备忘",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认",
                             "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            row = payload["results"][0]
            link = row["intent_links"]["progress"]
            a0 = int(link["anchor_offset"][0])
            i1 = int(link["intent_offset"][1])
            # At least one evidence quote must cover the linked span.
            covered = False
            for ev in row["evidence"]:
                quote = str(ev.get("quote") or "")
                if not quote:
                    continue
                # A summary_quote's raw_offset is optional; we accept
                # substring coverage as evidence-contains-snippet.
                if "ALPHA" in quote and "进展" in quote:
                    covered = True
                    break
                offsets = ev.get("raw_offset")
                if isinstance(offsets, list) and len(offsets) == 2:
                    lo, hi = int(offsets[0]), int(offsets[1])
                    if lo <= a0 and hi >= i1:
                        covered = True
                        break
            self.assertTrue(
                covered,
                "final evidence must cover the linked raw snippet",
            )
        finally:
            mirror.cleanup()


class FollowUpCJKPrefixCollisionTests(unittest.TestCase):
    """Blocker D: family longest-overlap awareness in the raw H1
    strong-linkage check — a CJK prefix like ``云端`` must not
    strong-link a title actually owned by the longer family
    ``云端虾``."""

    def test_shorter_family_prefix_does_not_strong_link_longer_title(self):
        mirror = SyntheticMirror()
        try:
            # Both families declared: shorter surface ``云端`` (system)
            # and longer surface ``云端虾`` (system).  A report titled
            # ``云端虾项目周报`` must strong-link the LONGER family.
            mirror.add_report(
                "PC000000000000001", "云端 概念说明",
                "本文介绍 云端 是 A 类系统。",
                candidate_entities=[("云端", "system", "云端")],
                key_facts=[("云端 是 A 类系统", "云端 是 A 类系统")],
            )
            mirror.add_report(
                "PC000000000000002", "云端虾项目周报",
                "云端虾 项目本周进展已确认。",
                candidate_entities=[("云端虾", "system", "云端虾")],
                key_facts=[("云端虾 项目本周进展已确认",
                             "云端虾 项目本周进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload_prefix = mirror.query("云端 进展", top_k=4)
            # The ``云端`` query resolves to the short family; the
            # long-titled report must NOT appear as strong-linked
            # under it.
            for row in payload_prefix.get("results", []):
                if row["report_id"] == "PC000000000000002":
                    self.assertNotEqual(
                        row.get("entity_linkage"), "strong",
                        "云端虾 title must not be strong under 云端 family",
                    )
            # Sanity: the long family still strong-links its own title.
            payload_long = mirror.query("云端虾 进展", top_k=4)
            self.assertEqual(payload_long["entity_resolution"]["status"], "resolved")
            long_rows = [r for r in payload_long["results"]
                         if r["report_id"] == "PC000000000000002"]
            self.assertEqual(len(long_rows), 1)
            self.assertEqual(long_rows[0].get("entity_linkage"), "strong")
        finally:
            mirror.cleanup()


class FollowUpLocalFirstSyncTests(unittest.TestCase):
    """Blocker E: catalog / anchor-cache artifacts are LOCAL-ONLY;
    ``changed_relative_paths`` from ``cwk_wiki_search_index`` must
    not include them, and the sync layer must exclude them too."""

    def test_index_meta_excludes_catalog_artifacts(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "LF000000000000001", "ALPHA 备忘",
                "ALPHA 相关内容。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关内容", "ALPHA 相关内容")],
            )
            mirror.build_catalog(scan_raw=False)
            import cwk_wiki_search_index as swi
            meta = swi.build_index(mirror.mirror, force=True)
            paths = set(meta.get("changed_relative_paths") or [])
            for forbidden in (
                "wiki/_system/entity-catalog.json",
                "wiki/_system/entity-catalog.json.gz",
                "wiki/_system/entity-catalog-meta.json",
                "wiki/_system/entity-anchors-cache.json",
            ):
                self.assertNotIn(
                    forbidden, paths,
                    f"catalog artifact {forbidden} must stay local-only",
                )
            # index-meta.json still records the catalog SHA so cloud
            # readers can detect drift.
            meta_path = mirror.mirror / "wiki" / "_system" / "index-meta.json"
            meta_disk = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertTrue(meta_disk.get("entity_catalog_sha256"))
        finally:
            mirror.cleanup()

    def test_sync_iterator_never_picks_up_catalog_artifacts(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "LF000000000000002", "ALPHA 备忘",
                "ALPHA 相关内容。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 相关内容", "ALPHA 相关内容")],
            )
            mirror.build_catalog(scan_raw=False)
            import cwk_wiki_search_index as swi
            swi.build_index(mirror.mirror, force=True)
            # Simulate iter_items() by inspecting MIRROR redirected.
            import cwk_sync_mirror_to_docdb as sync_mod
            orig_mirror = sync_mod.MIRROR
            sync_mod.MIRROR = mirror.mirror
            try:
                items = sync_mod.iter_items(limit=None, only_prefix=None)
                rels = {item.rel.as_posix() for item in items}
            finally:
                sync_mod.MIRROR = orig_mirror
            for forbidden in (
                "wiki/_system/entity-catalog.json",
                "wiki/_system/entity-catalog.json.gz",
                "wiki/_system/entity-catalog-meta.json",
                "wiki/_system/entity-anchors-cache.json",
            ):
                self.assertNotIn(
                    forbidden, rels,
                    f"sync must never pick up local artifact {forbidden}",
                )
        finally:
            mirror.cleanup()


class FollowUpRegistrySafetyTests(unittest.TestCase):
    """Blocker F: registry evidence must be raw-verified when the
    entry actually applies; parenthetical-derived bare generic
    aliases stay generic candidates unless explicitly registered."""

    def test_registry_evidence_mismatch_fails_closed(self):
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "RF000000000000001", "示例 系统",
                "示例 相关内容。",
                candidate_entities=[("示例F", "system", "示例F")],
                key_facts=[("示例", "示例 相关内容")],
            )
            mirror.add_report(
                "RF000000000000002", "示例 产品",
                "示例 相关内容。",
                candidate_entities=[("示例F", "project", "示例F")],
                key_facts=[("示例", "示例 相关内容")],
            )
            mirror.write_registry([
                {
                    "entry_id": "cross-type-示例f-2026-08-18",
                    "canonical_display": "示例F",
                    "members": [
                        {"entity_type": "system", "normalized": "示例f"},
                        {"entity_type": "project", "normalized": "示例f"},
                    ],
                    # This quote does NOT appear in either raw report.
                    "evidence": [
                        {"report_id": "RF000000000000001",
                         "quote": "this quote never appears in raw"},
                    ],
                    "decided_by": "unit-test",
                    "decided_at": "2026-08-18",
                }
            ])
            with self.assertRaises(ec.RegistryValidationError):
                mirror.build_catalog(scan_raw=False)
        finally:
            mirror.cleanup()

    def test_generic_alias_does_not_hard_scope_acronym_family(self):
        """A report that declares only the generic full form (no
        acronym) must NOT be admitted as a posting of the acronym
        family formed by ``ALPHA（示例平台）``."""
        mirror = SyntheticMirror()
        try:
            # Y: parenthetical acronym declaration – forms an ``alpha``
            # family that also carries ``示例平台`` as a surface.
            mirror.add_report(
                "RF000000000000010", "Y 报告",
                "ALPHA 相关记录。",
                candidate_entities=[
                    ("ALPHA（示例平台）", "system", "ALPHA（示例平台）"),
                ],
                key_facts=[("ALPHA 相关记录", "ALPHA 相关记录")],
            )
            # X: independent 示例平台-only report; must NOT join
            # ALPHA's postings.
            mirror.add_report(
                "RF000000000000011", "X 报告",
                "示例平台 相关内容。",
                candidate_entities=[("示例平台", "system", "示例平台")],
                key_facts=[("示例平台 相关内容", "示例平台 相关内容")],
            )
            payload = mirror.build_catalog(scan_raw=False)
            # Locate the acronym family.
            acronym_family = next(
                (f for f in payload["families"]
                 if any(s["normalized"] == "alpha" for s in f["surfaces"])),
                None,
            )
            self.assertIsNotNone(acronym_family)
            self.assertNotIn(
                "RF000000000000011", acronym_family["postings"],
                "generic-only report must not join the acronym scope",
            )
            # ``ALPHA 进展`` query must resolve without picking up X.
            answer = mirror.query("ALPHA 进展", top_k=8)
            self.assertNotIn(
                "RF000000000000011",
                [r["report_id"] for r in answer.get("results", [])],
            )
            # Query for the generic-only surface must resolve to the
            # independent family, not the acronym family.
            resolution = wq.resolve_entity("示例平台 进展", payload)
            # Either resolved to the independent family or unscoped —
            # never leaks into the acronym family.
            if resolution.family_id:
                self.assertNotEqual(
                    resolution.family_id, acronym_family["family_id"],
                    "generic surface must not resolve to acronym family",
                )
        finally:
            mirror.cleanup()

    def test_registry_promotes_generic_alias_to_hard(self):
        """When the registry explicitly lists a generic surface as a
        member, it MUST be promoted from ``generic_candidate`` back
        to ``hard`` so operator-approved cross-type merges take
        effect as documented."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "RF000000000000020", "示例G-1",
                "ALFA 相关内容。",
                candidate_entities=[
                    ("ALFA（示例G平台）", "system", "ALFA（示例G平台）"),
                ],
                key_facts=[("ALFA 相关内容", "ALFA 相关内容")],
            )
            mirror.add_report(
                "RF000000000000021", "示例G-2",
                "ALFA 项目侧记。",
                candidate_entities=[
                    ("ALFA", "project", "ALFA"),
                ],
                key_facts=[("ALFA 项目侧记", "ALFA 项目侧记")],
            )
            mirror.write_registry([
                {
                    "entry_id": "cross-type-alfa-unit-test",
                    "canonical_display": "ALFA",
                    "members": [
                        {"entity_type": "system", "normalized": "alfa"},
                        {"entity_type": "project", "normalized": "alfa"},
                    ],
                    "evidence": [
                        {"report_id": "RF000000000000020",
                         "quote": "ALFA 相关内容"},
                    ],
                    "decided_by": "unit-test",
                    "decided_at": "2026-08-18",
                }
            ])
            payload = mirror.build_catalog(scan_raw=False)
            family = next(
                (f for f in payload["families"]
                 if any(s["normalized"] == "alfa" for s in f["surfaces"])),
                None,
            )
            self.assertIsNotNone(family)
            self.assertEqual(sorted(family["entity_types"]), ["project", "system"])
            for surface in family["surfaces"]:
                if surface["normalized"] == "alfa":
                    self.assertEqual(surface.get("scope_role"), "hard")
        finally:
            mirror.cleanup()


class FollowUpEvidenceStitchGuardTests(unittest.TestCase):
    """Blocker G: even a strong (raw H1) row must not stitch a
    distant unrelated intent into ``intents.verified`` without a
    locally auditable link."""

    def test_h1_alpha_plus_body_beta_risk_does_not_verify_alpha_risk(self):
        mirror = SyntheticMirror()
        try:
            # Raw H1 = ``ALPHA 阶段``.  Body has NO ALPHA anchor but
            # names a BETA risk.  The ALPHA scope resolves via the
            # H1, but risk intent must NOT verify — that would be the
            # exact stitching regression.
            mirror.add_report(
                "EG000000000000001", "ALPHA 阶段",
                (
                    "# 会议记录\n\n"
                    "本次讨论覆盖多个议题。\n\n"
                    "# 备注\n\n"
                    "BETA 项目本周风险 Y。"
                ),
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("BETA 项目本周风险 Y",
                             "BETA 项目本周风险 Y")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 风险", top_k=4)
            # No local ALPHA anchor in raw content → resolved_empty.
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved_empty",
                "H1-only ALPHA cannot stitch a BETA risk in body",
            )
        finally:
            mirror.cleanup()

    def test_link_records_carry_all_provenance_fields(self):
        """Regression: every intent link must carry both offsets +
        snippets, block kinds, heading paths, relation, distance,
        raw_sha256.  Neutral synthetic to guard against silent
        provenance regressions."""
        mirror = SyntheticMirror()
        try:
            mirror.add_report(
                "EG000000000000002", "备忘",
                "ALPHA 阶段进展已确认。",
                candidate_entities=[("ALPHA", "system", "ALPHA")],
                key_facts=[("ALPHA 阶段进展已确认",
                             "ALPHA 阶段进展已确认")],
            )
            mirror.build_catalog(scan_raw=False)
            payload = mirror.query("ALPHA 进展", top_k=4)
            row = payload["results"][0]
            link = row["intent_links"]["progress"]
            for key in (
                "surface", "anchor_offset", "anchor_snippet",
                "intent_offset", "intent_snippet", "relation",
                "distance", "raw_sha256", "anchor_heading_path",
                "intent_heading_path", "anchor_block", "intent_block",
            ):
                self.assertIn(key, link, f"missing {key} in intent link")
        finally:
            mirror.cleanup()


# ---------------------------------------------------------------------------
# Single real-corpus regression for the shipped TBS registry entry.
# Skipped automatically when the mirror is not present.
# ---------------------------------------------------------------------------


def _resolve_real_mirror() -> Path | None:
    candidate = os.environ.get("CWK_TEST_MIRROR_ROOT")
    if candidate:
        path = Path(candidate).expanduser().resolve()
    else:
        path = (PROJECT / "knowledge" / "工作协同镜像").resolve()
    if (path / "wiki" / "summaries").is_dir():
        return path
    return None


REAL_MIRROR = _resolve_real_mirror()


@unittest.skipIf(REAL_MIRROR is None, "real corpus not available; skipping")
class RealCorpusRegressionTests(unittest.TestCase):
    """One end-to-end regression that exercises the shipped TBS
    registry entry against the real mirror.  Never asserts a fixed
    report id, rank or count – only structural invariants.
    """

    @classmethod
    def setUpClass(cls):
        assert REAL_MIRROR is not None
        cls.mirror = REAL_MIRROR
        # Prefer the on-disk catalog so structural checks compare against
        # the same payload query_mirror will read.  If none is present
        # we build a no-raw payload just for structural inspection; never
        # write to the real mirror during tests.
        loaded = ec.load_catalog(cls.mirror)
        if loaded is not None:
            cls.payload = loaded
            cls.registry_source = str(
                (PROJECT / "config" / "entity-family-registry.json").resolve()
            )
        else:
            cls.payload, _cache, cls.registry_source = ec.build_catalog(
                cls.mirror, scan_raw=False
            )

    def _family_for(self, normalized: str) -> dict:
        for family in self.payload["families"]:
            if any(s["normalized"] == normalized for s in family["surfaces"]):
                return family
        raise AssertionError(f"no family found for {normalized}")

    def test_repo_registry_is_the_active_source(self):
        self.assertTrue(str(self.registry_source).endswith("config/entity-family-registry.json"))

    def test_family_span_all_registered_variants(self):
        family = self._family_for("tbs")
        surfaces = {s["normalized"] for s in family["surfaces"]}
        # Every controlled-suffix variant of the acronym is merged.
        for variant in ("tbs", "tbs系统", "tbs平台", "tbs项目"):
            self.assertIn(variant, surfaces)
        # Registry brings all three entity types into one family.
        self.assertEqual(sorted(family["entity_types"]), ["product", "project", "system"])
        # Postings should easily exceed the 30-item human-page limit.
        self.assertGreater(family["posting_count"], 30)

    def test_query_variants_resolve_to_the_same_family(self):
        family_id = self._family_for("tbs")["family_id"]
        for question in (
            "TBS 项目进展",
            "TBS 训战 下一步计划",
            "TBS 训战系统 风险",
            "TBS(训战系统) 负责人",
            "tbs 平台 next plan",
        ):
            resolution = wq.resolve_entity(question, self.payload)
            self.assertEqual(resolution.status, "resolved", question)
            self.assertEqual(resolution.family_id, family_id, question)

    def test_dynamic_sampling_scope_precision_via_query_mirror(self):
        """Sample surface × intent template pairs deterministically,
        run query_mirror end-to-end against the real mirror (read-only),
        and assert every returned report_id sits inside the resolved
        family's postings.  No fixed rank is asserted.
        """
        family = self._family_for("tbs")
        scope_ids = set(family["postings"])
        surfaces = [s["display"] for s in family["surfaces"] if s["display"]]
        templates = ["{} 进展", "{} 风险", "{} 下一步", "{} 负责人", "{} 项目进度"]
        random.seed(2026)
        violations: list[tuple[str, str]] = []
        for _ in range(6):
            surface = random.choice(surfaces)
            question = random.choice(templates).format(surface)
            payload = wq.query_mirror(
                self.mirror, question, use_index=False, top_k=8
            )
            self.assertEqual(payload["entity_resolution"]["status"], "resolved", question)
            self.assertEqual(
                payload["entity_resolution"]["family_id"], family["family_id"], question,
            )
            for result in payload.get("results", []):
                if str(result["report_id"]) not in scope_ids:
                    violations.append((question, result["report_id"]))
        self.assertEqual(violations, [], f"scope precision broke: {violations}")

    def test_unknown_ascii_nonce_query_fails_closed(self):
        resolution = wq.resolve_entity("ZULU-99 进展", self.payload)
        self.assertEqual(resolution.status, "unknown")

    def test_primary_compound_query_stays_visible_and_never_leaks_scope(self):
        """The RT-010 release blocker: the primary TBS compound query
        used to be cleared to ``confidence=none / results=[]`` because
        the top BM25 row happened to be unverified.  Now:

        - the query must resolve to the TBS family,
        - Top-K must contain at least one result (no silent empty),
        - every returned report must sit inside the family's postings,
        - global BM25 fallback must remain OFF,
        - confidence must NOT be ``high`` unless every requested intent
          is verified from raw evidence, and
        - confidence must NOT collapse to ``none`` when in-scope
          verified evidence exists.
        """
        family = self._family_for("tbs")
        scope_ids = set(family["postings"])
        for question in (
            "TBS 项目进展",
            "TBS 项目进展、下一步计划、风险",
        ):
            payload = wq.query_mirror(
                self.mirror, question, use_index=True, top_k=8,
            )
            self.assertEqual(
                payload["entity_resolution"]["status"], "resolved", question,
            )
            self.assertGreater(len(payload["results"]), 0, question)
            self.assertFalse(payload["global_fallback_used"], question)
            for result in payload["results"]:
                self.assertIn(
                    str(result["report_id"]), scope_ids,
                    f"{question}: {result['report_id']} escaped scope",
                )
            missing = payload["intents"]["missing"]
            if payload["confidence"] == "high":
                self.assertEqual(
                    missing, [],
                    f"{question}: high confidence but missing intents {missing}",
                )
                self.assertEqual(
                    payload["support"]["source_integrity"], "verified", question,
                )
                self.assertEqual(
                    payload["support"]["scope_integrity"], "verified", question,
                )
                self.assertEqual(
                    payload["support"]["query_support"], "verified", question,
                )
            else:
                self.assertIn(
                    payload["confidence"], {"low", "medium"},
                    f"{question}: confidence must not be `none` when in-scope"
                    f" verified evidence exists (got {payload['confidence']})",
                )


if __name__ == "__main__":
    unittest.main()
