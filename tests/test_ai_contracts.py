import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_ai_common import (  # noqa: E402
    EVENTS_SCHEMA,
    PRIORITIES_SCHEMA,
    RECORD_SCHEMA,
    ai_agent_workspace,
    ai_runtime_guard,
    contains_sensitive_text,
    extract_json_object,
    fallback_record,
    invoke_openclaw_json,
    normalize_record,
    sanitize_record_evidence,
    safe_agent_policy,
    sanitized_ai_environment,
    validate_events,
    validate_priorities,
    validate_record,
)
from cwk_ai_event_clustering import merge_event_batches, normalize_bundle, prompt_for as cluster_prompt_for, recover_cluster_batch, validate_cluster_evidence, validate_event_coverage  # noqa: E402
from cwk_nightly_pipeline import copy_to_mirror, merge_changed_paths_manifest, redact_cmd, redact_text, require_publish_safe, write_manifest  # noqa: E402
from cwk_ai_quality_review import prompt_for as quality_prompt_for  # noqa: E402
from cwk_ai_record_understanding import process_one as process_ai_record  # noqa: E402
from cwk_sample_pilot import build_relations, event_family, load_items, title_anchor, unique_relation_pairs  # noqa: E402
from cwk_relation_eval import evaluate as evaluate_relations  # noqa: E402


class AIContractTests(unittest.TestCase):
    def test_extracts_schema_payload_from_openclaw_envelope(self):
        payload = {"schema_version": RECORD_SCHEMA, "report_id": "1"}
        envelope = {"result": {"payloads": [{"text": json.dumps(payload)}]}}
        self.assertEqual(extract_json_object(json.dumps(envelope)), payload)

    def test_model_call_retries_transient_failure(self):
        payload = {"schema_version": RECORD_SCHEMA, "report_id": "1"}
        fail = SimpleNamespace(returncode=1, stdout="", stderr="temporary unavailable")
        success = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with (
                patch("cwk_ai_common.PROJECT", project),
                patch("cwk_ai_common.assert_safe_ai_agent"),
                patch("cwk_ai_common.subprocess.run", side_effect=[fail, success]) as run,
                patch("cwk_ai_common.time.sleep"),
                patch.dict("os.environ", {"CWK_AI_CALL_RETRIES": "2"}, clear=False),
            ):
                result = invoke_openclaw_json("safe prompt", model="newapi/BD-MiniMax", stage="test", timeout_seconds=1, prompt_dir=project)
        self.assertEqual(result, payload)
        self.assertEqual(run.call_count, 2)

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

    def test_untraceable_model_evidence_is_pruned(self):
        fallback = {
            "report_id": "1",
            "evidence_refs": [{"report_id": "1", "quote": "原文证据"}],
        }
        payload = {
            "decisions": [{"text": "保留", "evidence": "原文证据"}, {"text": "删除", "evidence": "改写证据"}],
            "action_items": [],
            "risks": [],
            "evidence_refs": [{"report_id": "1", "quote": "改写证据"}],
        }
        sanitized = sanitize_record_evidence(payload, fallback, "这里包含原文证据")
        self.assertEqual(len(sanitized["decisions"]), 1)
        self.assertEqual(sanitized["evidence_refs"], fallback["evidence_refs"])
        self.assertIn("untraceable_evidence_pruned", sanitized["normalization_flags"])


    def test_copy_to_mirror_supports_external_root(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            run = project / "runs" / "nightly-test"
            external_mirror = Path(directory) / "external-mirror"
            run.mkdir(parents=True)
            (run / "digest-human-v4.md").write_text("# digest\n", encoding="utf-8")
            (run / "digest-human-v4.html").write_text("<p>digest</p>\n", encoding="utf-8")
            (run / "ACCEPTANCE-RESULT.md").write_text("ok\n", encoding="utf-8")
            (run / "incremental-link-preview-v1.md").write_text("ok\n", encoding="utf-8")
            with patch("cwk_nightly_pipeline.PROJECT", project):
                outputs = copy_to_mirror(run, "2026-08-02", external_mirror)
            self.assertTrue(outputs["daily_md"].startswith(str(external_mirror.resolve())))
            self.assertTrue((external_mirror / "daily" / "2026-08" / "2026-08-02.md").exists())

    def test_merge_changed_paths_manifest_combines_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compile_manifest = root / "compile.json"
            topics_manifest = root / "topics.json"
            output = root / "merged.json"
            compile_manifest.write_text(json.dumps({"changed_relative_paths": ["wiki/summaries/1.md", "wiki/_system/manifest.json"]}), encoding="utf-8")
            topics_manifest.write_text(json.dumps({"changed_relative_paths": ["wiki/topics/a.md", "wiki/_system/manifest.json"]}), encoding="utf-8")
            merged = merge_changed_paths_manifest(output, compile_manifest, topics_manifest)
            self.assertEqual(merged, output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["changed_relative_paths"], ["wiki/_system/manifest.json", "wiki/summaries/1.md", "wiki/topics/a.md"])

    def test_sensitive_source_pattern_is_detected(self):
        self.assertTrue(contains_sensitive_text("credential sk-example_12345678901234567890"))
        self.assertFalse(contains_sensitive_text("ordinary work report"))

    def test_sensitive_source_is_skipped_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "1-sensitive.md"
            raw.write_text('---\nreport_id: "1"\ntitle: "敏感材料"\n---\n\ncredential sk-example_12345678901234567890', encoding="utf-8")
            payload, error, _ = process_ai_record(
                raw,
                {"source_ids": ["1"], "title": "敏感材料", "event_anchor": "敏感材料"},
                dry_run=False,
                model="unused",
                timeout_seconds=1,
                prompt_dir=Path(directory),
            )
            self.assertEqual(payload["ai_status"], "skipped_sensitive")
            self.assertIsNone(error)
            self.assertEqual(payload["evidence_refs"], [])

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

    def test_ai_agent_policy_allows_unsandboxed_zero_tool_agent(self):
        base = {
            "skills": [],
            "workspace": str(ai_agent_workspace()),
            "sandbox": {"mode": "off"},
        }
        safe, _ = safe_agent_policy(
            {**base, "tools": {"profile": "minimal", "allow": [], "alsoAllow": [], "deny": ["*"]}}
        )
        self.assertTrue(safe)
        unsafe, _ = safe_agent_policy({**base, "tools": {"profile": "coding", "deny": ["message"]}})
        self.assertFalse(unsafe)
        unsafe, _ = safe_agent_policy({**base, "tools": {"profile": "minimal", "alsoAllow": ["read"], "deny": ["*"]}})
        self.assertFalse(unsafe)
        unsafe, _ = safe_agent_policy({**base, "tools": {"profile": "minimal", "alsoAllow": [], "deny": ["exec"]}})
        self.assertFalse(unsafe)
        unsafe, _ = safe_agent_policy({"tools": {"profile": "minimal", "alsoAllow": [], "deny": ["*"]}})
        self.assertFalse(unsafe)

        sandboxed, _ = safe_agent_policy(
            {
                **base,
                "sandbox": {"mode": "all", "scope": "agent", "workspaceAccess": "ro"},
                "tools": {"profile": "minimal", "allow": [], "alsoAllow": [], "deny": ["*"]},
            }
        )
        self.assertFalse(sandboxed)

    def test_ai_agent_policy_rejects_workspace_outside_project(self):
        agent = {
            "skills": [],
            "workspace": "/tmp/cwk-untrusted-workspace",
            "sandbox": {"mode": "off"},
            "tools": {"profile": "minimal", "allow": [], "alsoAllow": [], "deny": ["*"]},
        }
        safe, reason = safe_agent_policy(agent)
        self.assertFalse(safe)
        self.assertIn("fixed private", reason)

    def test_ai_agent_workspace_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            outside = Path(temp_dir) / "outside"
            project.mkdir()
            outside.mkdir()
            (project / ".cwk-ai-runtime").symlink_to(outside, target_is_directory=True)
            with patch("cwk_ai_common.PROJECT", project):
                with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                    ai_agent_workspace()

    def test_ai_agent_policy_rejects_external_symlink_to_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            runtime = project / ".cwk-ai-runtime"
            runtime.mkdir()
            external_link = root / "external-workspace"
            external_link.symlink_to(runtime, target_is_directory=True)
            agent = {
                "skills": [],
                "workspace": str(external_link),
                "sandbox": {"mode": "off"},
                "tools": {"profile": "minimal", "allow": [], "alsoAllow": [], "deny": ["*"]},
            }
            with patch("cwk_ai_common.PROJECT", project):
                safe, reason = safe_agent_policy(agent)
            self.assertFalse(safe)
            self.assertIn("non-symlink", reason)

    def test_ai_agent_workspace_cannot_be_overridden_by_environment(self):
        expected = PROJECT.resolve() / ".cwk-ai-runtime"
        with patch.dict("os.environ", {"CWK_AI_AGENT_WORKSPACE": "/tmp/cwk-untrusted-workspace"}):
            self.assertEqual(ai_agent_workspace(), expected)

    def test_ai_runtime_guard_cleans_orphans_and_rejects_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            prompts = project / ".cwk-ai-runtime" / "prompts"
            prompts.mkdir(parents=True)
            (prompts / "orphan.md").write_text("temporary", encoding="utf-8")
            with patch("cwk_ai_common.PROJECT", project):
                with ai_runtime_guard():
                    self.assertFalse((prompts / "orphan.md").exists())
                    with self.assertRaisesRegex(RuntimeError, "another CWK AI pilot"):
                        with ai_runtime_guard():
                            pass

    def test_cluster_rejects_untraceable_evidence(self):
        records = [{"report_id": "1", "evidence_refs": [{"report_id": "1", "quote": "原文"}], "decisions": [], "action_items": [], "risks": []}]
        events = {"events": [{"record_ids": ["1"], "decisions": [{"text": "结论", "evidence": "编造证据"}], "action_items": [], "risks": []}]}
        self.assertTrue(validate_cluster_evidence(events, records))

    def test_cluster_prompt_requires_exact_evidence(self):
        prompt = cluster_prompt_for([{"report_id": "1"}], "run", None)
        self.assertIn("copy its evidence value exactly", prompt)

    def test_cluster_requires_nonempty_coverage(self):
        self.assertTrue(validate_event_coverage({"events": []}, {"1"}))
        self.assertEqual(validate_event_coverage({"events": [{"record_ids": ["1"]}]}, {"1"}), [])

    def test_cluster_invalid_batch_has_traceable_deterministic_recovery(self):
        record = {
            "report_id": "1",
            "title": "示例事项",
            "event_anchor": "示例事项",
            "document_type": "other",
            "summary": "摘要",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "priority_hint": "review",
        }
        events, priorities, recovery = recover_cluster_batch(
            [record], "run", set(), ["events must not be empty"]
        )
        self.assertEqual(validate_event_coverage(events, {"1"}, 1.0), [])
        self.assertEqual(priorities["priorities"][0]["record_ids"], ["1"])
        self.assertEqual(recovery["mode"], "deterministic_evidence_fallback")
        self.assertIn("events must not be empty", recovery["reason"])

    def test_cluster_batches_merge_same_event(self):
        base = {
            "event_title": "示例项目",
            "event_type": "other",
            "status": "new",
            "priority": "P2",
            "history_match": {"matched": False},
            "merged_summary": "摘要",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "why_it_matters": "原因",
        }
        events, priorities = merge_event_batches([{**base, "record_ids": ["1"]}, {**base, "record_ids": ["2"]}], "run")
        self.assertEqual(len(events["events"]), 1)
        self.assertEqual(events["events"][0]["record_ids"], ["1", "2"])
        self.assertEqual(priorities["priorities"][0]["event_id"], events["events"][0]["event_id"])

    def test_cluster_batch_merge_normalizes_string_history(self):
        event = {
            "event_title": "示例项目",
            "event_type": "other",
            "status": "new",
            "priority": "P2",
            "history_match": "none",
            "merged_summary": "摘要",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "why_it_matters": "原因",
            "record_ids": ["1"],
        }
        events, _ = merge_event_batches([event], "run")
        self.assertEqual(events["events"][0]["history_match"], {})

    def test_cluster_calibrates_nonblocked_p0(self):
        event = {
            "event_title": "示例项目",
            "event_type": "other",
            "status": "updated",
            "priority": "P0",
            "history_match": {},
            "merged_summary": "摘要",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "why_it_matters": "原因",
            "record_ids": ["1"],
        }
        events, _ = merge_event_batches([event], "run")
        self.assertEqual(events["events"][0]["priority"], "P1")

    def test_manifest_command_redacts_secret_flags(self):
        self.assertEqual(redact_cmd(["collector", "--app-key", "secret-value"]), ["collector", "--app-key", "<redacted>"])

    def test_manifest_and_logs_redact_runtime_secret_values(self):
        self.assertEqual(redact_text("failed secret-value", ("secret-value",)), "failed <redacted>")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            path = write_manifest(run_dir, {"stdout": "secret-value"}, ("secret-value",))
            self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))

    def test_publish_gate_rejects_secret_shaped_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "digest.md"
            path.write_text("credential sk-example_12345678901234567890", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "secret gate blocked"):
                require_publish_safe([path])

    def test_rule_anchor_rejects_dates_and_connectors(self):
        empty = {key: [] for key in ("projects", "systems", "products", "customers", "contracts")}
        self.assertEqual(title_anchor("0715", empty), "未命名事项")
        self.assertEqual(title_anchor("会议纪要及跟进", empty), "未命名事项")

    def test_product_task_summary_is_recurring_report(self):
        self.assertEqual(event_family("7月第二周-云端虾-产品任务进度汇总-来源会议任务", "persistent_stream"), "recurring_report")
        self.assertEqual(event_family("SFE行为积分内测周总结（07/10）", "persistent_stream"), "recurring_report")

    def test_rule_anchor_canonicalizes_known_system_alias(self):
        entities = {"projects": [], "systems": ["SFE"], "products": [], "customers": [], "contracts": []}
        self.assertEqual(title_anchor("7月第二周'SFE系统'产品任务进度汇总", entities), "SFE")

    def test_rule_anchor_canonicalizes_known_business_topics(self):
        empty = {key: [] for key in ("projects", "systems", "products", "customers", "contracts")}
        self.assertEqual(title_anchor("关于下半年大模型使用预算申请的请示", empty), "AI费用")
        self.assertEqual(title_anchor("法务部合同AI建设总体规划", empty), "法务部AI")
        self.assertEqual(title_anchor("敏感词扫描与清洁双周进展会", empty), "内容合规治理")

    def test_rule_relations_are_unique_undirected_pairs(self):
        base = {
            "event_anchor": "示例项目A",
            "event_family": "recurring_report",
            "source_lane": "persistent_stream",
            "title": "示例项目A周报",
            "entities": {"systems": ["示例系统"], "projects": ["示例项目A"], "products": [], "customers": [], "contracts": [], "orgs": []},
        }
        relations = build_relations({"1": base, "2": {**base, "title": "示例项目A进展周报"}})
        pairs = unique_relation_pairs(relations)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source_ids"], ["1", "2"])

    def test_rule_pilot_accepts_external_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "outside"
            source.mkdir()
            (source / "1-report.md").write_text('---\nreport_id: "1"\ntitle: "示例项目周报"\n---\n\n# 示例项目周报', encoding="utf-8")
            with patch("cwk_sample_pilot.PROJECT", PROJECT):
                items = load_items([source])
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0].path.startswith("external-source/"))

    def test_relation_gold_has_thirty_unique_pairs(self):
        gold = json.loads((PROJECT / "references" / "relation-gold-v1.json").read_text(encoding="utf-8"))
        pairs = {tuple(sorted(item["source_ids"])) for item in gold["pairs"]}
        self.assertEqual(len(pairs), 30)

    def test_relation_evaluator_counts_confusion_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "relations").mkdir()
            gold_pairs = []
            for index in range(30):
                gold_pairs.append({"source_ids": [str(index), str(index + 100)], "related": index < 15})
            gold_path = root / "gold.json"
            gold_path.write_text(json.dumps({"pairs": gold_pairs}), encoding="utf-8")
            (root / "relations" / "0.json").write_text(json.dumps([{"source_ids": ["0", "100"], "decision": "mark_suspected"}]), encoding="utf-8")
            result = evaluate_relations(root, gold_path)
            self.assertEqual((result["tp"], result["fp"], result["fn"], result["tn"]), (1, 0, 14, 15))
            self.assertEqual(result["accuracy"], 0.5333)

    def test_quality_prompt_contains_exact_contract_keys(self):
        prompt = quality_prompt_for("rules", "enhanced", {"events": []}, {"report-1"})
        for key in ("schema_version", "quality_score", "rules_score", "evidence_coverage", "release_recommendation"):
            self.assertIn(f'"{key}"', prompt)

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
