"""RT-032: discovery report, execution contract, pilot gate, scheduler handoff.

The properties under test:

* the daily execution contract is computed from the *actual* configuration,
  and its hash moves whenever any promise in it moves;
* the discovery report never inflates "confirmed" counts with machine guesses;
* the pilot gate is a conjunction — one bad predicate is a FAIL;
* the scheduler handoff describes a task for the host to create and carries
  environment variable *names* only, never values.

The cap/lookback constants mirrored into the contract are pinned against the
upstream collector and pipeline sources, so a silent upstream change fails
here instead of quietly making the contract lie to the user.

No network, no subprocess, no real CWork/DocDB access.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_activation_contract as C  # noqa: E402

FIXTURES = PROJECT / "tests" / "fixtures" / "activation"
NOW = "2026-01-01T00:00:00Z"
SHA_PROFILE = "b" * 64


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def contract_from(config: dict, *, env=None, run_at="02:30", tz="Asia/Shanghai") -> dict:
    return C.build_execution_contract(
        config=config,
        env=env if env is not None else {},
        profile_sha256=SHA_PROFILE,
        run_at_local=run_at,
        timezone=tz,
        generated_at=NOW,
    )


class UpstreamDefaultPinningTests(unittest.TestCase):
    """The contract must not drift away from what the collector actually does."""

    def test_collector_caps_match_upstream_argparse_defaults(self):
        upstream = C.upstream_collect_defaults()
        self.assertEqual(
            upstream,
            {
                "detail_cap": C.DEFAULT_DETAIL_CAP,
                "continuation_cap": C.DEFAULT_CONTINUATION_CAP,
                "backfill_cap": C.DEFAULT_BACKFILL_CAP,
                "backfill_page_size": C.DEFAULT_BACKFILL_PAGE_SIZE,
            },
        )

    def test_lookback_matches_upstream_pipeline_default(self):
        self.assertEqual(C.upstream_lookback_default(), C.DEFAULT_LOOKBACK_DAYS)

    def test_the_contract_module_never_imports_the_collector(self):
        """Importing the collector to read a default would risk collecting."""

        source = (PROJECT / "scripts" / "cwk_activation_contract.py").read_text(
            encoding="utf-8"
        )
        for banned in ("import cwk_collect_live", "import cwk_nightly_pipeline"):
            self.assertNotIn(banned, source)

    def test_parsing_does_not_execute_the_parsed_file(self):
        """Hand the parser a file that explodes if executed; it must still parse."""

        with tempfile.TemporaryDirectory() as tmp:
            bomb = Path(tmp) / "bomb.py"
            bomb.write_text(
                "raise SystemExit('this module must never be executed')\n"
                "parser.add_argument('--detail-cap', type=int, default=41)\n"
                "parser.add_argument('--continuation-cap', type=int, default=42)\n",
                encoding="utf-8",
            )
            found = C.upstream_collect_defaults(bomb)
        self.assertEqual(found["detail_cap"], 41)
        self.assertEqual(found["continuation_cap"], 42)


class DiscoveryReportTests(unittest.TestCase):
    def setUp(self):
        self.scope = load("scope.json")
        self.report = C.build_discovery_report(
            scope=self.scope,
            collect_manifest=load("collect-manifest.json"),
            nightly_manifest=load("nightly-manifest.json"),
            acceptance=load("acceptance.json"),
            entity_catalog=load("entity-catalog.json"),
            entity_registry=load("entity-registry.json"),
            generated_at=NOW,
        )

    def test_report_states_its_coverage_limit(self):
        self.assertIn("authorized visible scope", self.report["coverage_caveat"])
        self.assertEqual(self.report["authorized_visible_scope"], self.scope)

    def test_lane_counts_are_reported(self):
        self.assertEqual(self.report["source_lanes"]["daily_lane_count"], 5)
        self.assertEqual(self.report["source_lanes"]["backfill_lane_count"], 5)

    def test_entity_counts_are_independent(self):
        entities = self.report["entities"]
        # 6 surfaces (3 normalized + 3 aliases), 2 candidate families,
        # 1 registry entry that actually carries a decision.
        self.assertEqual(entities["entity_surface_count"], 6)
        self.assertEqual(entities["candidate_entity_family_count"], 2)
        self.assertEqual(entities["confirmed_entity_count"], 1)
        self.assertTrue(entities["counts_are_independent"])

    def test_undecided_registry_entries_are_not_counted_as_confirmed(self):
        registry = load("entity-registry.json")
        registry["entries"][0]["decided_by"] = None
        report = C.build_discovery_report(
            scope=self.scope,
            collect_manifest=None,
            nightly_manifest=None,
            acceptance=None,
            entity_catalog=None,
            entity_registry=registry,
            generated_at=NOW,
        )
        self.assertEqual(report["entities"]["confirmed_entity_count"], 0)

    def test_unknown_relations_are_not_hidden(self):
        relations = self.report["relations"]
        self.assertEqual(
            relations["unknown_relation_count"],
            relations["unique_relation_pairs"]
            - relations["strong_relations"]
            - relations["suspected_relations"],
        )

    def test_missing_inputs_become_explicit_gaps(self):
        report = C.build_discovery_report(
            scope=self.scope,
            collect_manifest=None,
            nightly_manifest=None,
            acceptance=None,
            entity_catalog=None,
            entity_registry=None,
            generated_at=NOW,
        )
        self.assertEqual(
            sorted(report["gaps"]),
            [
                "acceptance_missing",
                "collect_manifest_missing",
                "entity_catalog_missing",
                "nightly_manifest_missing",
            ],
        )

    def test_hash_ignores_the_timestamp(self):
        again = C.build_discovery_report(
            scope=self.scope,
            collect_manifest=load("collect-manifest.json"),
            nightly_manifest=load("nightly-manifest.json"),
            acceptance=load("acceptance.json"),
            entity_catalog=load("entity-catalog.json"),
            entity_registry=load("entity-registry.json"),
            generated_at="2030-12-31T23:59:59Z",
        )
        self.assertEqual(again["report_sha256"], self.report["report_sha256"])

    def test_hash_moves_when_the_scope_moves(self):
        other = dict(self.scope, subject_ref="fixture-user-b")
        report = C.build_discovery_report(
            scope=other,
            collect_manifest=load("collect-manifest.json"),
            nightly_manifest=load("nightly-manifest.json"),
            acceptance=load("acceptance.json"),
            entity_catalog=load("entity-catalog.json"),
            entity_registry=load("entity-registry.json"),
            generated_at=NOW,
        )
        self.assertNotEqual(report["report_sha256"], self.report["report_sha256"])


class ExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.config = load("config.json")
        self.contract = contract_from(self.config)

    def test_contract_is_read_only_and_says_so(self):
        self.assertTrue(self.contract["read_only"])
        self.assertTrue(self.contract["raw_boundary"]["raw_never_written_back"])
        self.assertTrue(self.contract["raw_boundary"]["raw_never_uploaded"])
        self.assertFalse(self.contract["publishing"]["uploads_raw"])

    def test_forbidden_actions_cover_every_cwork_mutation(self):
        forbidden = " ".join(self.contract["forbidden_actions"]).lower()
        for word in ("read", "repl", "approve", "reject", "delete", "send"):
            self.assertIn(word, forbidden)

    def test_config_beats_environment(self):
        env = {"CWK_DETAIL_CAP": "999"}
        contract = contract_from(self.config, env=env)
        self.assertEqual(contract["caps"]["detail_cap"], self.config["detail_cap"])

    def test_environment_beats_default(self):
        config = {k: v for k, v in self.config.items() if k != "detail_cap"}
        contract = contract_from(config, env={"CWK_DETAIL_CAP": "7"})
        self.assertEqual(contract["caps"]["detail_cap"], 7)

    def test_defaults_apply_when_nothing_is_configured(self):
        contract = contract_from({}, env={})
        self.assertEqual(contract["caps"]["detail_cap"], C.DEFAULT_DETAIL_CAP)
        self.assertEqual(contract["late_data_lookback_days"], C.DEFAULT_LOOKBACK_DAYS)

    def test_hash_is_stable_across_regeneration(self):
        again = contract_from(self.config)
        self.assertEqual(again["contract_sha256"], self.contract["contract_sha256"])

    def test_hash_ignores_the_timestamp(self):
        again = C.build_execution_contract(
            config=self.config,
            env={},
            profile_sha256=SHA_PROFILE,
            run_at_local="02:30",
            timezone="Asia/Shanghai",
            generated_at="2030-12-31T23:59:59Z",
        )
        self.assertEqual(again["contract_sha256"], self.contract["contract_sha256"])

    def test_every_promise_moves_the_hash(self):
        """If a user-visible promise changes, the confirmation must not survive."""

        mutations = [
            {"detail_cap": 61},
            {"continuation_cap": 16},
            {"backfill_cap": 21},
            {"backfill_page_size": 21},
            {"backfill_enabled": False},
            {"sync_docdb": True},
            {"source_completeness_lookback_days": 3},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                contract = contract_from({**self.config, **mutation})
                self.assertNotEqual(
                    contract["contract_sha256"], self.contract["contract_sha256"]
                )

    def test_schedule_time_moves_the_hash(self):
        self.assertNotEqual(
            contract_from(self.config, run_at="03:30")["contract_sha256"],
            self.contract["contract_sha256"],
        )
        self.assertNotEqual(
            contract_from(self.config, tz="UTC")["contract_sha256"],
            self.contract["contract_sha256"],
        )

    def test_profile_moves_the_hash(self):
        other = C.build_execution_contract(
            config=self.config,
            env={},
            profile_sha256="c" * 64,
            run_at_local="02:30",
            timezone="Asia/Shanghai",
            generated_at=NOW,
        )
        self.assertNotEqual(other["contract_sha256"], self.contract["contract_sha256"])

    def test_disabling_backfill_empties_the_backfill_lanes(self):
        contract = contract_from({**self.config, "backfill_enabled": False})
        self.assertEqual(contract["sources"]["backfill_lanes"], [])

    def test_drift_detection(self):
        clean = C.contract_drift(self.contract, self.contract["contract_sha256"])
        self.assertFalse(clean["drifted"])
        dirty = C.contract_drift(self.contract, "0" * 64)
        self.assertTrue(dirty["drifted"])
        unknown = C.contract_drift(self.contract, None)
        self.assertFalse(unknown["drifted"])

    def test_markdown_restates_the_contract_without_inventing_facts(self):
        markdown = C.render_contract_markdown(self.contract)
        self.assertIn(self.contract["contract_sha256"], markdown)
        self.assertIn("02:30", markdown)
        self.assertIn(str(self.contract["caps"]["detail_cap"]), markdown)
        self.assertIn(str(self.contract["late_data_lookback_days"]), markdown)
        for action in self.contract["forbidden_actions"]:
            self.assertIn(action, markdown)

    def test_markdown_carries_no_credentials(self):
        markdown = C.render_contract_markdown(self.contract)
        for token in ("CWORK_APP_KEY", "password", "secret", "token="):
            self.assertNotIn(token, markdown)


class PilotGateTests(unittest.TestCase):
    def setUp(self):
        self.nightly = load("nightly-manifest.json")
        self.acceptance = load("acceptance.json")
        self.collect = load("collect-manifest.json")
        self.contract_sha = "a" * 64

    def evaluate(self, **overrides) -> dict:
        payload = dict(
            nightly_manifest=self.nightly,
            acceptance=self.acceptance,
            collect_manifest=self.collect,
            bound_contract_sha256=self.contract_sha,
            generated_at=NOW,
        )
        payload.update(overrides)
        return C.evaluate_pilot(**payload)

    def test_clean_run_passes(self):
        receipt = self.evaluate()
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["failed_predicates"], [])

    def test_receipt_is_bound_to_the_contract(self):
        self.assertEqual(self.evaluate()["bound_contract_sha256"], self.contract_sha)

    def test_missing_evidence_fails(self):
        self.assertEqual(self.evaluate(nightly_manifest=None)["result"], "FAIL")
        self.assertEqual(self.evaluate(acceptance=None)["result"], "FAIL")
        self.assertEqual(self.evaluate(collect_manifest=None)["result"], "FAIL")

    def test_an_omitted_collection_receipt_cannot_pass(self):
        """Not passing the argument at all is missing evidence, not a waiver.

        `evaluate_pilot` gives `collect_manifest` a default, so an omitted
        argument must be caught by a predicate rather than by the signature.
        """

        receipt = C.evaluate_pilot(
            nightly_manifest=self.nightly,
            acceptance=self.acceptance,
            bound_contract_sha256=self.contract_sha,
            generated_at=NOW,
        )
        self.assertEqual(receipt["result"], "FAIL")
        for predicate in ("collect_receipt_present", "daily_source_complete"):
            self.assertIn(predicate, receipt["failed_predicates"])
        self.assertEqual(
            receipt["collection_receipt"]["problems"], ["collect_receipt_omitted"]
        )
        self.assertIsNone(receipt["collection_receipt"]["verified"])

    def test_daily_source_completeness_is_never_assumed(self):
        """Without a receipt there is no basis for claiming the day is complete."""

        for manifest in (None, {}, {"daily_source_complete": True}):
            with self.subTest(manifest=manifest):
                receipt = self.evaluate(collect_manifest=manifest)
                self.assertEqual(receipt["result"], "FAIL")

    def test_a_collection_receipt_of_the_wrong_shape_cannot_pass(self):
        cases = {
            "not_an_object": [],
            "missing_field": {k: v for k, v in load("collect-manifest.json").items()
                              if k != "written_count"},
            "string_instead_of_bool": dict(load("collect-manifest.json"),
                                           daily_source_complete="true"),
            "negative_count": dict(load("collect-manifest.json"), written_count=-1),
            "errors_not_a_list": dict(load("collect-manifest.json"), errors="none"),
        }
        for name, manifest in cases.items():
            with self.subTest(case=name):
                receipt = self.evaluate(collect_manifest=manifest)
                self.assertEqual(receipt["result"], "FAIL")
                self.assertIn("collect_receipt_shape_valid", receipt["failed_predicates"])
                self.assertIsNone(receipt["collection_receipt"]["verified"])

    def test_a_collection_run_that_did_not_succeed_cannot_pass(self):
        cases = {
            "incomplete_day": {"daily_source_complete": False},
            "source_failures": {"daily_source_failure_count": 1},
            "collector_errors": {"errors": ["fixture-lane-timeout"]},
            "mutating_calls": {"mutating_commands_called": ["markRead"]},
        }
        for name, mutation in cases.items():
            with self.subTest(case=name):
                receipt = self.evaluate(collect_manifest={**self.collect, **mutation})
                self.assertEqual(receipt["result"], "FAIL")
                self.assertIn("collect_receipt_success", receipt["failed_predicates"])
                self.assertTrue(receipt["collection_receipt"]["problems"])

    def test_verified_collection_facts_are_bound_into_the_receipt(self):
        receipt = self.evaluate()
        checked = receipt["collection_receipt"]
        self.assertTrue(checked["present"] and checked["shape_valid"] and checked["success"])
        self.assertEqual(
            checked["verified"],
            {
                "daily_source_complete": True,
                "daily_source_failure_count": 0,
                "written_count": self.collect["written_count"],
                "error_count": 0,
                "mutating_command_count": 0,
            },
        )
        self.assertEqual(
            receipt["evidence"]["collect_manifest_sha256"], checked["receipt_sha256"]
        )

    def test_new_evidence_moves_the_receipt_hash_even_when_the_verdict_holds(self):
        """Otherwise a confirmation bound to the old receipt would survive it."""

        baseline = self.evaluate()
        variants = {
            "collect": {"collect_manifest": dict(self.collect, written_count=19)},
            "nightly": {"nightly_manifest": dict(self.nightly, processed_count=19)},
            "acceptance": {"acceptance": dict(self.acceptance, raw_count=19)},
        }
        for name, override in variants.items():
            with self.subTest(evidence=name):
                receipt = self.evaluate(**override)
                self.assertEqual(receipt["result"], "PASS")
                self.assertNotEqual(receipt["receipt_sha256"], baseline["receipt_sha256"])

    def test_the_receipt_hash_is_a_function_of_the_evidence(self):
        first = self.evaluate()
        again = self.evaluate(generated_at="2030-12-31T23:59:59Z")
        self.assertEqual(again["receipt_sha256"], first["receipt_sha256"])

    def test_a_single_bad_predicate_fails_the_gate(self):
        cases = [
            ("nightly", "overall_pass", False),
            ("nightly", "content_quality_pass", False),
            ("nightly", "degraded", True),
            ("nightly", "sync_failures", ["boom"]),
            ("nightly", "source_completeness_failures", ["gap"]),
            ("acceptance", "overall_pass", False),
            ("acceptance", "failures", ["A2"]),
            ("acceptance", "A4_status", "FAIL"),
            ("collect", "daily_source_complete", False),
        ]
        for target, key, value in cases:
            with self.subTest(target=target, key=key):
                payload = {
                    "nightly": dict(self.nightly),
                    "acceptance": dict(self.acceptance),
                    "collect": dict(self.collect),
                }
                payload[target][key] = value
                receipt = self.evaluate(
                    nightly_manifest=payload["nightly"],
                    acceptance=payload["acceptance"],
                    collect_manifest=payload["collect"],
                )
                self.assertEqual(receipt["result"], "FAIL")
                self.assertTrue(receipt["failed_predicates"])

    def test_low_volume_acceptance_still_passes(self):
        acceptance = dict(self.acceptance, A4_status="PASS_LOW_VOLUME")
        self.assertEqual(self.evaluate(acceptance=acceptance)["result"], "PASS")

    def test_any_mutating_command_fails_the_gate(self):
        for target in ("nightly", "acceptance", "collect"):
            with self.subTest(target=target):
                payload = {
                    "nightly": dict(self.nightly),
                    "acceptance": dict(self.acceptance),
                    "collect": dict(self.collect),
                }
                payload[target]["mutating_commands_called"] = ["markRead"]
                receipt = self.evaluate(
                    nightly_manifest=payload["nightly"],
                    acceptance=payload["acceptance"],
                    collect_manifest=payload["collect"],
                )
                self.assertEqual(receipt["result"], "FAIL")
                self.assertIn("no_mutating_commands", receipt["failed_predicates"])

    def test_empty_check_set_does_not_count_as_passing(self):
        acceptance = dict(self.acceptance, checks={})
        receipt = self.evaluate(acceptance=acceptance)
        self.assertEqual(receipt["result"], "FAIL")
        self.assertIn("acceptance_all_checks_pass", receipt["failed_predicates"])


class SchedulerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract_from(load("config.json"))
        self.root = Path(tempfile.mkdtemp(prefix="cwk-handoff-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.handoff = self.build()

    def build(self, *, config_path=None, project_root=None):
        return C.build_scheduler_handoff(
            contract=self.contract,
            contract_sha256=self.contract["contract_sha256"],
            profile_sha256=SHA_PROFILE,
            pilot_receipt_sha256="c" * 64,
            config_path=(
                config_path
                if config_path is not None
                else self.root / "cwk-mirror.local.json"
            ),
            project_root=self.root if project_root is None else project_root,
            generated_at=NOW,
        )

    def test_handoff_states_the_repository_creates_nothing(self):
        joined = " ".join(self.handoff["repository_does_not"]).lower()
        self.assertIn("create, modify or delete scheduled tasks", joined)
        self.assertIn("cron", joined)

    def test_handoff_carries_env_names_but_no_values(self):
        spec = self.handoff["command_spec"]
        self.assertEqual(spec["env_allowlist"], ["CWORK_APP_KEY"])
        self.assertFalse(spec["secrets_included"])
        serialized = json.dumps(self.handoff, ensure_ascii=False)
        self.assertNotIn("CWORK_APP_KEY=", serialized)

    def test_handoff_requires_the_second_confirmation(self):
        self.assertTrue(self.handoff["requires_second_confirmation"])
        self.assertIn(
            "second human confirmation is bound to this contract_sha256",
            self.handoff["preconditions"],
        )

    def test_command_is_the_read_only_pipeline(self):
        argv = self.handoff["command_spec"]["argv"]
        self.assertIn("scripts/cwk_nightly_pipeline.py", argv)
        for banned in ("--mark-read", "--reply", "--approve", "--delete"):
            self.assertNotIn(banned, argv)

    def test_receipt_validation_accepts_a_matching_id(self):
        result = C.validate_schedule_receipt(
            handoff=self.handoff,
            contract_sha256=self.contract["contract_sha256"],
            external_task_id="host-task-1",
        )
        self.assertTrue(result["ok"])

    def test_receipt_validation_rejects_a_contract_mismatch(self):
        result = C.validate_schedule_receipt(
            handoff=self.handoff,
            contract_sha256="0" * 64,
            external_task_id="host-task-1",
        )
        self.assertFalse(result["ok"])
        self.assertIn("handoff_contract_mismatch", result["problems"])

    def test_receipt_validation_rejects_an_empty_id(self):
        result = C.validate_schedule_receipt(
            handoff=self.handoff,
            contract_sha256=self.contract["contract_sha256"],
            external_task_id="   ",
        )
        self.assertFalse(result["ok"])
        self.assertIn("external_task_id_missing", result["problems"])

    def test_receipt_validation_rejects_an_unknown_schema(self):
        result = C.validate_schedule_receipt(
            handoff=dict(self.handoff, schema="something.else.v1"),
            contract_sha256=self.contract["contract_sha256"],
            external_task_id="host-task-1",
        )
        self.assertFalse(result["ok"])
        self.assertIn("handoff_schema_unknown", result["problems"])

    # ── 配置定位符：绝对路径既不进负载，也不进 handoff_sha256 ──────────────

    def test_handoff_never_carries_the_absolute_project_path(self):
        serialized = json.dumps(self.handoff, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(PROJECT), serialized)

    def test_config_is_described_as_a_project_relative_locator(self):
        locator = self.handoff["config_locator"]
        self.assertEqual(locator["kind"], "project_relative")
        self.assertEqual(locator["path"], "cwk-mirror.local.json")
        self.assertTrue(locator["absolute_path_omitted"])
        self.assertEqual(self.handoff["command_spec"]["argv"][3], "cwk-mirror.local.json")
        self.assertFalse(self.handoff["command_spec"]["absolute_paths_included"])

    def test_nested_config_keeps_its_relative_segments(self):
        handoff = self.build(config_path=self.root / "config" / "mine.json")
        self.assertEqual(handoff["config_locator"]["path"], "config/mine.json")
        self.assertNotIn(str(self.root), json.dumps(handoff, ensure_ascii=False))

    def test_host_is_told_how_to_find_the_project_root_not_where_it_is(self):
        locator = self.handoff["config_locator"]["project_root_locator"]
        self.assertEqual(locator["kind"], "cwk_project_root")
        self.assertEqual(locator["verify_marker"], "scripts/cwk_doctor.py")
        self.assertTrue(locator["absolute_path_intentionally_omitted"])
        joined = " ".join(locator["resolution_order"])
        self.assertIn("CWK_PROJECT_DIR", joined)
        self.assertIn(
            "resolve the CWK project root locally using project_root_locator",
            self.handoff["host_responsibilities"],
        )

    def test_project_root_locator_is_a_copy_callers_cannot_corrupt(self):
        first = C.project_root_locator()
        first["verify_marker"] = "tampered"
        first["resolution_order"].append("tampered")
        self.assertEqual(C.project_root_locator()["verify_marker"], "scripts/cwk_doctor.py")
        self.assertNotIn("tampered", C.project_root_locator()["resolution_order"])

    def test_the_hash_is_stable_across_different_host_layouts(self):
        """同一份配置在两台机器的不同绝对路径下，交接单摘要必须相同。"""

        other = Path(tempfile.mkdtemp(prefix="cwk-handoff-other-")).resolve()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        elsewhere = self.build(
            config_path=other / "cwk-mirror.local.json", project_root=other
        )
        self.assertEqual(
            elsewhere["handoff_sha256"], self.handoff["handoff_sha256"]
        )

    # ── fail closed：表述不了就不出交接单 ─────────────────────────────────

    def test_a_config_outside_the_project_is_refused_not_downgraded(self):
        outside = Path(tempfile.mkdtemp(prefix="cwk-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with self.assertRaises(C.ConfigLocatorError) as raised:
            self.build(config_path=outside / "cwk-mirror.local.json")
        message = str(raised.exception)
        self.assertNotIn(str(outside), message)
        self.assertNotIn(str(self.root), message)

    def test_a_parent_escape_is_refused(self):
        with self.assertRaises(C.ConfigLocatorError):
            self.build(config_path=self.root / ".." / "escaped.json")

    def test_the_project_root_itself_is_not_a_config(self):
        with self.assertRaises(C.ConfigLocatorError):
            self.build(config_path=self.root)

    def test_a_control_character_in_the_name_is_refused(self):
        with self.assertRaises(C.ConfigLocatorError):
            C.build_config_locator(
                config_path=f"{self.root}/bad\nname.json", project_root=self.root
            )

    def test_a_name_that_would_be_parsed_as_an_option_is_refused(self):
        with self.assertRaises(C.ConfigLocatorError):
            C.build_config_locator(
                config_path=f"{self.root}/--config.json", project_root=self.root
            )


class ScheduleDriftTests(unittest.TestCase):
    def setUp(self):
        self.contract_sha = "a" * 64
        self.schedule = {
            "external_system": "openclaw",
            "external_task_id": "host-task-1",
            "bound_contract_sha256": self.contract_sha,
            "handoff_sha256": "b" * 64,
            "recorded_at": NOW,
            "status": "enabled",
        }

    def test_matching_schedule_is_clean(self):
        result = C.detect_schedule_drift(
            state_schedule=self.schedule,
            observed_task_id="host-task-1",
            contract_sha256=self.contract_sha,
        )
        self.assertFalse(result["drifted"])
        self.assertFalse(result["destructive_action_taken"])

    def test_unknown_task_is_reported_never_deleted(self):
        result = C.detect_schedule_drift(
            state_schedule=None,
            observed_task_id="somebody-elses-task",
            contract_sha256=self.contract_sha,
        )
        self.assertTrue(result["drifted"])
        self.assertIn("unknown_external_task", result["findings"])
        self.assertFalse(result["destructive_action_taken"])

    def test_foreign_task_id_is_reported_never_deleted(self):
        result = C.detect_schedule_drift(
            state_schedule=self.schedule,
            observed_task_id="not-our-task",
            contract_sha256=self.contract_sha,
        )
        self.assertIn("schedule_id_unknown", result["findings"])
        self.assertFalse(result["destructive_action_taken"])

    def test_contract_change_is_drift(self):
        result = C.detect_schedule_drift(
            state_schedule=self.schedule,
            observed_task_id="host-task-1",
            contract_sha256="0" * 64,
        )
        self.assertIn("contract_drift", result["findings"])


if __name__ == "__main__":
    unittest.main()
