import hashlib
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cwk_cloud_objects import merge
from cwk_summary_source_refs import apply_refs
from cwk_wiki_query import query_mirror
from cwk_wiki_search_index import build_index
from cwk_restore_from_docdb import restore
from cwk_docdb_cloud import DocDBCloudRepository
from cwk_shadow_consistency import compare


def fixture_mirror(root: Path) -> Path:
    raw = root / "raw" / "2026-08" / "2026-08-04"
    summaries = root / "wiki" / "summaries"
    system = root / "wiki" / "_system"
    topics = root / "wiki" / "topics"
    raw.mkdir(parents=True)
    summaries.mkdir(parents=True)
    system.mkdir(parents=True)
    topics.mkdir(parents=True)
    rid = "123456789012345"
    raw_path = raw / f"{rid}-Token异常.md"
    raw_path.write_text(
        f'---\nreport_id: "{rid}"\n---\n<content>Token异常达到50亿，需要检查。</content>\n',
        encoding="utf-8",
    )
    summaries.joinpath(f"{rid}.md").write_text(
        "\n".join(
            [
                "---", "type: SourceSummary", f'report_id: "{rid}"',
                f'source: "../../{raw_path.relative_to(root).as_posix()}"', "---", "",
                "# Token异常分析", "", "- 发送人：测试人", "- 时间：2026-08-04 10:00:00",
                "- 来源类型：`test`", "", "## 摘要", "", "Token异常达到50亿。",
                "", "## 关键事实", "", "- Token异常达到50亿  ", "  证据：> Token异常达到50亿",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    topics.joinpath("Token异常.md").write_text(
        f"# Token异常\n\n- [`{rid}`](../summaries/{rid}.md)\n", encoding="utf-8"
    )
    system.joinpath("manifest.json").write_text(
        json.dumps({"ai_refined_report_ids": [rid], "fallback_report_ids": [], "compiled_report_ids": [rid]}),
        encoding="utf-8",
    )
    return root


class CloudFirstIndexTests(unittest.TestCase):
    def test_fixed_question_shadow_consistency_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = fixture_mirror(Path(tmp))
            build_index(mirror)
            result = compare(mirror, ["Token异常"] * 20, top_k=8, min_overlap=0.99)
            self.assertTrue(result["overall_pass"])
            self.assertEqual(result["query_count"], 20)
            self.assertEqual(result["aggregate_overlap_rate"], 1.0)

    def test_index_is_versioned_deterministically_and_matches_live_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = fixture_mirror(Path(tmp))
            first = build_index(mirror)
            second = build_index(mirror)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(first["index_version"], second["index_version"])
            self.assertTrue(first["index_files"])
            parts = [(mirror / "wiki" / "_system" / name).read_bytes() for name in first["index_files"]]
            artifact = (mirror / "wiki" / "_system" / first["index_file"]).read_bytes()
            self.assertEqual(b"".join(parts), artifact)
            self.assertEqual(first["index_artifact_sha256"], hashlib.sha256(artifact).hexdigest())
            indexed = query_mirror(mirror, "Token异常", top_k=1, use_index=True)
            live = query_mirror(mirror, "Token异常", top_k=1, use_index=False)
            self.assertEqual(indexed["results"][0]["report_id"], live["results"][0]["report_id"])
            self.assertEqual(indexed["results"][0]["evidence_status"], "verified")

    def test_source_refs_include_report_and_raw_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = fixture_mirror(Path(tmp))
            result = apply_refs(mirror)
            self.assertEqual(result["error_count"], 0)
            text = next((mirror / "wiki" / "summaries").glob("*.md")).read_text(encoding="utf-8")
            self.assertIn('source_report_id: "123456789012345"', text)
            self.assertRegex(text, r'source_sha256: "[0-9a-f]{64}"')

    def test_old_sync_receipt_does_not_forge_current_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = fixture_mirror(root / "mirror")
            receipt = root / "sync.json"
            receipt.write_text(
                json.dumps(
                    {
                        "project_id": "1", "root_file_id": "2", "generated_at": "2026-01-01T00:00:00+08:00",
                        "results": [{"relative_path": "wiki/summaries/123456789012345.md", "action": "create", "file_id": "9"}],
                    }
                ),
                encoding="utf-8",
            )
            merge(mirror, [receipt], reset=True)
            catalog = json.loads((mirror / "wiki" / "_system" / "cloud-objects.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["objects"]["wiki/summaries/123456789012345.md"]["content_sha256"], "")

    def test_dry_run_receipt_preserves_verified_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = fixture_mirror(root / "mirror")
            rel = "wiki/summaries/123456789012345.md"
            verified = root / "verified.json"
            verified.write_text(
                json.dumps(
                    {
                        "project_id": "1", "root_file_id": "2", "generated_at": "2026-01-01T00:00:00+08:00",
                        "results": [{"relative_path": rel, "action": "update_version", "file_id": "9", "content_sha256": "a" * 64}],
                    }
                ),
                encoding="utf-8",
            )
            dry = root / "dry.json"
            dry.write_text(
                json.dumps(
                    {
                        "project_id": "1", "root_file_id": "2", "generated_at": "2026-01-02T00:00:00+08:00",
                        "dry_run": True,
                        "results": [{"relative_path": rel, "action": "skip_existing", "file_id": "9"}],
                    }
                ),
                encoding="utf-8",
            )
            merge(mirror, [verified], reset=True)
            merge(mirror, [dry], reset=False)
            catalog = json.loads((mirror / "wiki" / "_system" / "cloud-objects.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["objects"][rel]["content_sha256"], "a" * 64)
            self.assertEqual(catalog["objects"][rel]["storage"], "text")

    def test_corrupt_index_self_hash_falls_back_to_live_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = fixture_mirror(Path(tmp))
            build_index(mirror)
            compressed = mirror / "wiki" / "_system" / "search-index.json.gz"
            import gzip
            with gzip.open(compressed, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["summary_docs"][0]["title"] = "被篡改"
            with gzip.open(compressed, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            result = query_mirror(mirror, "Token异常", top_k=1, use_index=True)
            self.assertEqual(result["indexed"]["provider"], "live_scan")

    def test_restore_requires_hashes_and_reconstructs_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "restored"
            source.mkdir()
            raw_bytes = b"raw-body"
            index_bytes = b"gzip-index-payload"
            part_a, part_b = index_bytes[:8], index_bytes[8:]
            blobs = {"raw-id": raw_bytes, "part-a": part_a, "part-b": part_b}

            class FakeRepo:
                def bootstrap(self, *, min_index_version=0):
                    def row(file_id, data):
                        return {"file_id": file_id, "content_sha256": hashlib.sha256(data).hexdigest()}
                    return source, {
                        "index_version": 9,
                        "index_file": "search-index.json.gz",
                        "index_files": ["search-index-v000009-000.bin", "search-index-v000009-001.bin"],
                        "index_artifact_sha256": hashlib.sha256(index_bytes).hexdigest(),
                        "objects": {
                            "raw/2026-08/2026-08-04/1.md": row("raw-id", raw_bytes),
                            "wiki/_system/search-index-v000009-000.bin": row("part-a", part_a),
                            "wiki/_system/search-index-v000009-001.bin": row("part-b", part_b),
                        },
                    }

                def download(self, file_id, target, expected_sha, force=False):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blobs[file_id])
                    self.assert_hash(target, expected_sha)
                    return target

                def download_object(self, row, target, expected_sha, force=False):
                    return self.download(str(row.get("file_id") or ""), target, expected_sha, force=force)

                @staticmethod
                def assert_hash(target, expected_sha):
                    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha

            result = restore(
                FakeRepo(), destination, prefixes=("raw/", "wiki/"), report_ids=set(),
                min_index_version=9, max_parallel=2,
            )
            self.assertTrue(result["overall_pass"])
            self.assertEqual((destination / "wiki" / "_system" / "search-index.json.gz").read_bytes(), index_bytes)

            class MissingHashRepo(FakeRepo):
                def bootstrap(self, *, min_index_version=0):
                    cache, catalog = super().bootstrap(min_index_version=min_index_version)
                    catalog["objects"]["raw/2026-08/2026-08-04/1.md"]["content_sha256"] = ""
                    return cache, catalog

            failed = restore(
                MissingHashRepo(), root / "failed", prefixes=("raw/",), report_ids=set(),
                min_index_version=9, max_parallel=1,
            )
            self.assertFalse(failed["overall_pass"])
            self.assertEqual(failed["failure_count"], 1)

    def test_chunked_raw_reassembles_and_verifies_logical_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = ("大文件证据\n" * 20000).encode("utf-8")
            compressed = gzip.compress(raw, compresslevel=9, mtime=0)
            midpoint = len(compressed) // 2
            blobs = {"a": compressed[:midpoint], "b": compressed[midpoint:]}
            repo = object.__new__(DocDBCloudRepository)
            repo.cache_root = root / "cache"
            repo.cache_root.mkdir()

            def fake_download(file_id, target, expected_sha256="", force=False):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blobs[file_id])
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), expected_sha256)
                return target

            repo.download = fake_download
            row = {
                "file_id": "a",
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "artifact_sha256": hashlib.sha256(compressed).hexdigest(),
                "parts": [
                    {"file_id": key, "content_sha256": hashlib.sha256(value).hexdigest()}
                    for key, value in blobs.items()
                ],
            }
            target = root / "restored.md"
            repo.download_object(row, target, row["content_sha256"], force=True)
            self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
