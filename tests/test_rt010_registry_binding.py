"""Focused RT-010 registry/catalog audit-binding regressions.

These tests intentionally use neutral synthetic entity names.  They verify
the audit contract rather than tune retrieval around a production entity.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_entity_catalog as ec  # noqa: E402
import cwk_wiki_search_index as search_index  # noqa: E402


def _family(entity_type: str, report_id: str) -> ec.Family:
    normalized = "alpha"
    family = ec.Family(
        family_id=ec._family_id(entity_type, normalized),
        entity_types={entity_type},
        canonical_display="ALPHA",
        canonical_normalized=normalized,
        canonical_entity_type=entity_type,
    )
    family.surfaces[normalized] = ec.ApprovedSurface(
        display="ALPHA",
        normalized=normalized,
        entity_type=entity_type,
        origin="declared",
    )
    family.add_posting(
        report_id,
        {
            "origin": "summary_candidate",
            "surface": "ALPHA",
            "entity_type": entity_type,
            "quote": f"ALPHA {entity_type}",
        },
    )
    return family


def _approved_serialisation(
    *, decision_ref: str = "ticket://A-1", evidence_quote: str = "ALPHA links system and project"
) -> tuple[dict, list[dict], str]:
    system = _family("system", "900000000000000001")
    project = _family("project", "900000000000000002")
    families = {system.family_id: system, project.family_id: project}
    lookup = {
        ("system", "alpha"): system.family_id,
        ("project", "alpha"): project.family_id,
    }
    registry = {
        "schema_version": ec.REGISTRY_SCHEMA,
        "version": "test-v1",
        "entries": [
            {
                "entry_id": "alpha-cross-type",
                "canonical_display": "ALPHA",
                "members": [
                    {"entity_type": "system", "normalized": "alpha"},
                    {"entity_type": "project", "normalized": "alpha"},
                ],
                "evidence": [
                    {
                        "report_id": "900000000000000001",
                        "quote": evidence_quote,
                    }
                ],
                "decided_by": "unit-owner",
                "decided_at": "2026-08-18",
                "decision_ref": decision_ref,
            }
        ],
    }
    applied = ec._apply_registry(
        families,
        lookup,
        registry,
        raw_index={"900000000000000001": evidence_quote},
    )
    family = next(iter(families.values()))
    serialised = ec._serialise_family(family)
    semantic_hash = ec._canonical_hash(
        {
            "registry": {"version": registry["version"], "applied": applied},
            "families": [serialised],
        }
    )
    return serialised, applied, semantic_hash


class RegistryPostingProvenanceTests(unittest.TestCase):
    def test_every_approved_posting_has_full_registry_audit_edge(self):
        family, _applied, _sha = _approved_serialisation()
        family_evidence_refs = {
            row.get("evidence_ref")
            for row in family["approved_family_evidence"]
            if row.get("evidence_ref")
        }
        self.assertEqual(
            family_evidence_refs,
            {"alpha-cross-type:evidence:0"},
        )
        expected = {
            "rule": "approved_family_registry",
            "entry_id": "alpha-cross-type",
            "decided_by": "unit-owner",
            "decided_at": "2026-08-18",
            "decision_ref": "ticket://A-1",
            "registry_version": "test-v1",
        }
        self.assertEqual(set(family["postings"]), set(family["posting_provenance"]))
        for report_id in family["postings"]:
            audit_rows = [
                row
                for row in family["posting_provenance"][report_id]
                if row.get("rule") == "approved_family_registry"
            ]
            self.assertEqual(len(audit_rows), 1, report_id)
            row = audit_rows[0]
            for key, value in expected.items():
                self.assertEqual(row.get(key), value, (report_id, key))
            self.assertEqual(
                row.get("evidence_refs"),
                ["alpha-cross-type:evidence:0"],
            )
            self.assertTrue(set(row["evidence_refs"]).issubset(family_evidence_refs))

    def test_decision_and_evidence_changes_affect_semantic_hash(self):
        _family_a, _applied_a, sha_a = _approved_serialisation()
        _family_b, _applied_b, sha_b = _approved_serialisation(
            decision_ref="ticket://A-2"
        )
        _family_c, _applied_c, sha_c = _approved_serialisation(
            evidence_quote="ALPHA is one approved cross-type referent"
        )
        self.assertNotEqual(sha_a, sha_b)
        self.assertNotEqual(sha_a, sha_c)


class SearchIndexRegistryBindingTests(unittest.TestCase):
    def test_registry_source_is_bound_in_payload_meta_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp) / "mirror"
            system = mirror / "wiki" / "_system"
            system.mkdir(parents=True)
            (system / "manifest.json").write_text("{}\n", encoding="utf-8")

            catalog = {
                "catalog_sha256": "a" * 64,
                "schema_version": ec.SCHEMA,
                "registry": {"version": "test-v1"},
                "statistics": {"families_total": 2},
            }
            source = "repo:config/entity-family-registry.json"
            with (
                mock.patch.object(
                    search_index.entity_catalog,
                    "build_catalog",
                    return_value=(catalog, None, source),
                ),
                mock.patch.object(search_index.entity_catalog, "write_catalog"),
            ):
                meta = search_index.build_index(mirror, force=True)

            payload = json.loads(
                (system / "search-index.json").read_text(encoding="utf-8")
            )
            persisted_meta = json.loads(
                (system / "index-meta.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (system / "manifest.json").read_text(encoding="utf-8")
            )
            for persisted in (payload, persisted_meta, manifest):
                self.assertEqual(
                    persisted.get("entity_catalog_registry_source"), source
                )

            # Catalog/cache/experimental override remain local-only derived
            # artifacts and must not re-enter the DocDB changed-path surface.
            changed = set(meta["changed_relative_paths"])
            self.assertFalse(
                any(
                    name in path
                    for path in changed
                    for name in (
                        "entity-catalog",
                        "entity-anchors-cache",
                        "entity-family-registry",
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
