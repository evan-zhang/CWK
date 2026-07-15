import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_ai_common import (  # noqa: E402
    EVENTS_SCHEMA,
    PRIORITIES_SCHEMA,
    RECORD_SCHEMA,
    extract_json_object,
    fallback_record,
    normalize_record,
    safe_agent_policy,
    sanitized_ai_environment,
    validate_events,
    validate_priorities,
    validate_record,
)
from cwk_ai_event_clustering import normalize_bundle, validate_cluster_evidence  # noqa: E402
from cwk_nightly_pipeline import copy_to_mirror, redact_cmd  # noqa: E402


class AIContractTests(unittest.TestCase):
    def test_extracts_schema_payload_from_openclaw_envelope(self):
        payload = {"schema_version": RECORD_SCHEMA, "report_id": "1"}
        envelope = {"result": {"payloads": [{"text": json.dumps(payload)}]}}
        self.assertEqual(extract_json_object(json.dumps(envelope)), payload)

    def test_fallback_record_has_traceable_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "1-sample.md"
            raw.write_text(
                '---\nreport_id: "1"\ntitle: "示例周会"\nwriter: "同事"\ncreate_time: "2026-01-01T10:00:00+08:00"\n---\n\n# 示例周会\n\n会议确认先完成只读验证。',
                encoding="utf-8",
            )
            payload = fallback_record(raw, {"source_ids": ["1"], "title": "示例周会", "event_anchor": "示例项目"})
            self.assertEqual(validate_record(payload, "1"), [])
            self.assertEqual(payload["evidence_refs"][0]["quote"], "会议确认先完成只读验证。")

    def test_event_and_priority_ids_must_be_from_input(self):
        events = {"schema_version": EVENTS_SCHEMA, "events": [{"event_id": "e1", "event_title": "示例", "event_type": "other", "status": "new", "priority": "P2", "merged_summary": "摘要", "why_it_matters": "原因", "record_ids": ["1"]}]}
        priorities = {"schema_version": PRIORITIES_SCHEMA, "priorities": [{"rank": 1, "event_id": "e1", "title": "示例", "priority": "P2", "status": "new", "summary": "摘要", "why_it_matters": "原因", "record_ids": ["1"]}]}
        self.assertEqual(validate_events(events, {"1"}), [])
        self.assertEqual(validate_priorities(priorities, {"1"}), [])
        self.assertTrue(validate_events(events, {"2"}))
        self.assertTrue(validate_priorities(priorities, {"2"}))

    def test_normalization_fills_non_evidentiary_anchor_only(self):
        fallback = {
            "schema_version": RECORD_SCHEMA,
            "report_id": "1",
            "title": "示例",
            "writer": "同事",
            "created_at_shanghai": "2026-01-01 10:00:00",
            "source_lane": "unknown",
            "document_type": "other",
            "event_anchor": "示例项目",
            "event_anchor_confidence": 0.5,
            "summary": "规则摘要",
            "background": "规则背景",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "entities": {"people": [], "teams": [], "systems": [], "products": [], "projects": []},
            "priority_hint": "review",
            "noise_flags": [],
            "evidence_refs": [{"report_id": "1", "quote": "原文证据"}],
        }
        model = {**fallback, "summary": "AI 摘要"}
        model.pop("event_anchor")
        normalized = normalize_record(model, fallback)
        self.assertEqual(normalized["event_anchor"], "示例项目")
        self.assertEqual(normalized["summary"], "AI 摘要")
        self.assertIn("event_anchor", normalized["normalization_flags"])

    def test_cluster_priority_cannot_exceed_deterministic_hint(self):
        bundle = {
            "events": {
                "events": [
                    {
                        "event_id": "e1",
                        "title": "示例",
                        "status": "active",
                        "priority": "P0",
                        "record_ids": ["1"],
                        "summary": "摘要",
                    }
                ]
            },
            "priorities": {
                "priorities": [
                    {"related_event_id": "e1", "title": "示例", "level": "P0", "record_ids": ["1"], "reason": "原因"}
                ]
            },
        }
        records = [{"report_id": "1", "priority_hint": "review", "risks": []}]
        events, priorities = normalize_bundle(bundle, "run", records)
        self.assertEqual(events["events"][0]["priority"], "P2")
        self.assertEqual(priorities["priorities"][0]["priority"], "P2")

    def test_ai_agent_policy_allows_read_only_minimal_agent(self):
        safe, _ = safe_agent_policy({"skills": [], "tools": {"profile": "minimal", "alsoAllow": ["read"]}})
        self.assertTrue(safe)
        unsafe, _ = safe_agent_policy({"skills": [], "tools": {"profile": "coding", "deny": ["message"]}})
        self.assertFalse(unsafe)

    def test_cluster_rejects_untraceable_evidence(self):
        records = [{"report_id": "1", "evidence_refs": [{"report_id": "1", "quote": "原文"}], "decisions": [], "action_items": [], "risks": []}]
        events = {"events": [{"record_ids": ["1"], "decisions": [{"text": "结论", "evidence": "编造证据"}], "action_items": [], "risks": []}]}
        self.assertTrue(validate_cluster_evidence(events, records))
        unsafe, _ = safe_agent_policy({"skills": [], "tools": {"profile": "minimal", "alsoAllow": ["read", "exec"]}})
        self.assertFalse(unsafe)
        unsafe, _ = safe_agent_policy({"tools": {"profile": "minimal", "alsoAllow": ["read"]}})
        self.assertFalse(unsafe)

    def test_manifest_command_redacts_secret_flags(self):
        self.assertEqual(redact_cmd(["collector", "--app-key", "secret-value"]), ["collector", "--app-key", "<redacted>"])

    def test_ai_subprocess_environment_drops_business_secrets(self):
        with patch.dict("os.environ", {"CWORK_APP_KEY": "secret", "OPENAI_API_KEY": "provider-secret", "OPENCLAW_GATEWAY_TOKEN": "gateway", "CWK_AI_THINKING": "high"}, clear=True):
            env = sanitized_ai_environment()
        self.assertNotIn("CWORK_APP_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["OPENCLAW_GATEWAY_TOKEN"], "gateway")
        self.assertEqual(env["CWK_AI_THINKING"], "high")

    def test_ai_outputs_publish_beside_rules_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "sample"
            run_dir.mkdir(parents=True)
            for name in ("digest-human-v4.md", "digest-human-v4.html", "digest-ai-enhanced.md", "digest-ai-enhanced.html", "quality-review.md"):
                (run_dir / name).write_text(name, encoding="utf-8")
            with patch("cwk_nightly_pipeline.PROJECT", root), patch("cwk_nightly_pipeline.MIRROR", root / "mirror"):
                outputs = copy_to_mirror(run_dir, "2026-01-01")
            self.assertIn("daily_ai_md", outputs)
            self.assertIn("daily_ai_html", outputs)
            self.assertIn("ai-quality-review", outputs)


if __name__ == "__main__":
    unittest.main()
