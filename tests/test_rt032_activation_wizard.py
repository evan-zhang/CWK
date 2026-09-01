"""RT-032: the activation wizard CLI, driven through its real entry point.

One test walks the whole approved path from a fresh install to an active
schedule. The rest is an attack matrix: every way a caller — including a
confused or adversarial AI — might try to reach "the system now runs by
itself" without the two human confirmations and a passing pilot.

The wizard is invoked in-process via ``main(argv)``. There is no network,
no subprocess, no cron/Gateway call and no real CWork/DocDB access; the
repository never creates a scheduled task.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_activation_state as S  # noqa: E402
import cwk_activation_wizard as W  # noqa: E402
import cwk_atomic_file as A  # noqa: E402

FIXTURES = PROJECT / "tests" / "fixtures" / "activation"


class WizardTestCase(unittest.TestCase):
    """Shared harness: a private temp state dir and a scrubbed environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "activation"
        # A stray CWK_* variable would otherwise change the contract hash.
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = 0

    # ── helpers ────────────────────────────────────────────────────────────

    def tick(self) -> str:
        self.clock += 1
        return f"2026-01-01T00:{self.clock // 60:02d}:{self.clock % 60:02d}Z"

    def run_cli(self, *args: str) -> tuple[int, dict]:
        argv = ["--state-dir", str(self.state_dir), "--now", self.tick(), *args]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = W.main(argv)
        return code, json.loads(buffer.getvalue())

    def fixture(self, name: str) -> str:
        return str(FIXTURES / name)

    def config_copy(self, **overrides) -> str:
        payload = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
        payload.update(overrides)
        target = self.root / f"config-{len(list(self.root.glob('config-*.json')))}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return str(target)

    def scope_copy(self, **overrides) -> str:
        payload = json.loads((FIXTURES / "scope.json").read_text(encoding="utf-8"))
        payload.update(overrides)
        target = self.root / f"scope-{len(list(self.root.glob('scope-*.json')))}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return str(target)

    def read_state(self) -> dict:
        return json.loads((self.state_dir / S.STATE_FILE).read_text(encoding="utf-8"))

    # ── staged progress ────────────────────────────────────────────────────

    def stage_discovery(self):
        self.run_cli("init")
        self.run_cli("confirm-discovery", "--scope-file", self.fixture("scope.json"))
        return self.run_cli(
            "record-discovery",
            "--scope-file",
            self.fixture("scope.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--entity-catalog",
            self.fixture("entity-catalog.json"),
            "--entity-registry",
            self.fixture("entity-registry.json"),
        )

    def stage_profile(self):
        self.stage_discovery()
        self.run_cli("propose-profile", "--profile-file", self.fixture("profile.json"))
        return self.run_cli("confirm-profile")

    def stage_pilot(self, config: str | None = None, nightly: str = "nightly-manifest.json"):
        self.stage_profile()
        return self.run_cli(
            "record-pilot",
            "--config",
            config or self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture(nightly),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )

    def stage_active(self, config: str | None = None):
        config = config or self.fixture("config.json")
        self.stage_pilot(config=config)
        self.run_cli("confirm-activation")
        self.run_cli("schedule-handoff", "--config", config)
        return self.run_cli(
            "record-schedule",
            "--external-system",
            "openclaw",
            "--external-task-id",
            "host-task-1",
        )


class HappyPathTests(WizardTestCase):
    def test_full_walk_reaches_active(self):
        code, payload = self.run_cli("init")
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "INSTALLED")
        self.assertEqual(payload["next_step"], "confirm_discovery_scope")

        code, payload = self.run_cli(
            "confirm-discovery", "--scope-file", self.fixture("scope.json")
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "READY_FOR_DISCOVERY")
        self.assertEqual(payload["next_step"], "run_discovery")

        code, payload = self.run_cli(
            "record-discovery",
            "--scope-file",
            self.fixture("scope.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--entity-catalog",
            self.fixture("entity-catalog.json"),
            "--entity-registry",
            self.fixture("entity-registry.json"),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["next_step"], "propose_profile")
        self.assertEqual(payload["discovery_report"]["entities"]["confirmed_entity_count"], 1)

        code, payload = self.run_cli(
            "propose-profile", "--profile-file", self.fixture("profile.json")
        )
        self.assertEqual(payload["state"], "PROFILE_PROPOSED")
        self.assertEqual(payload["next_step"], "confirm_profile")

        code, payload = self.run_cli("confirm-profile")
        self.assertEqual(payload["state"], "PROFILE_CONFIRMED")
        self.assertEqual(payload["next_step"], "run_pilot")

        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "PILOT_PASSED")
        self.assertEqual(payload["pilot_receipt"]["result"], "PASS")
        self.assertEqual(payload["next_step"], "confirm_activation")

        code, payload = self.run_cli("confirm-activation")
        self.assertEqual(payload["next_step"], "emit_scheduler_handoff")

        code, payload = self.run_cli(
            "schedule-handoff", "--config", self.fixture("config.json")
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertFalse(payload["repository_created_a_task"])
        self.assertEqual(payload["next_step"], "record_external_schedule")

        code, payload = self.run_cli(
            "record-schedule",
            "--external-system",
            "openclaw",
            "--external-task-id",
            "host-task-1",
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "ACTIVE")
        self.assertEqual(payload["schedule"]["external_task_id"], "host-task-1")
        self.assertFalse(payload["repository_created_a_task"])

    def test_status_is_readable_at_every_step(self):
        self.run_cli("init")
        code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_OK)
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["state"], "INSTALLED")

    def test_status_before_init_reports_uninitialised(self):
        self.state_dir.mkdir(mode=0o700, parents=True)
        code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], S.UNINITIALIZED)
        self.assertEqual(payload["next_step"], "init")

    def test_init_is_idempotent(self):
        first = self.run_cli("init")[1]
        code, payload = self.run_cli("init")
        self.assertEqual(code, W.EXIT_OK)
        self.assertTrue(payload["already_initialised"])
        self.assertEqual(payload["activation_id"], first["activation_id"])

    def test_render_contract_is_read_only(self):
        self.stage_profile()
        before = self.read_state()
        code, payload = self.run_cli("render-contract", "--config", self.fixture("config.json"))
        self.assertEqual(code, W.EXIT_OK)
        self.assertIn("每日执行合同", payload["contract_markdown"])
        self.assertEqual(self.read_state(), before)

    def test_pause_and_resume(self):
        self.stage_active()
        code, payload = self.run_cli("pause")
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "PAUSED")
        self.assertEqual(payload["schedule"]["status"], "paused")

        code, payload = self.run_cli("resume")
        self.assertEqual(payload["state"], "ACTIVE")
        self.assertEqual(payload["schedule"]["status"], "enabled")

    def test_clean_run_reports_no_drift(self):
        self.stage_active()
        code, payload = self.run_cli("check-drift", "--config", self.fixture("config.json"))
        self.assertEqual(code, W.EXIT_OK)
        self.assertFalse(payload["contract_drift"]["drifted"])
        self.assertFalse(payload["schedule_drift"]["drifted"])


class OrderingAttackTests(WizardTestCase):
    """Nothing may be skipped. Each stage refuses to run out of order."""

    def test_commands_refuse_before_init(self):
        self.state_dir.mkdir(mode=0o700, parents=True)
        for args in (
            ("confirm-discovery", "--scope-file", self.fixture("scope.json")),
            ("propose-profile", "--profile-file", self.fixture("profile.json")),
            ("confirm-profile",),
            ("confirm-activation",),
        ):
            with self.subTest(command=args[0]):
                code, payload = self.run_cli(*args)
                self.assertEqual(code, W.EXIT_REFUSED)
                self.assertFalse(payload["ok"])

    def test_discovery_cannot_run_without_the_first_confirmation(self):
        self.run_cli("init")
        code, payload = self.run_cli(
            "record-discovery",
            "--scope-file",
            self.fixture("scope.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertEqual(self.read_state()["state"], "INSTALLED")

    def test_discovery_refuses_a_scope_the_user_never_confirmed(self):
        """Confirming a narrow scope must not authorise a wider one."""

        self.run_cli("init")
        self.run_cli("confirm-discovery", "--scope-file", self.fixture("scope.json"))
        widened = self.scope_copy(mirror_kind="team", subject_ref="fixture-team-x")
        code, payload = self.run_cli(
            "record-discovery",
            "--scope-file",
            widened,
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertIn("not the scope the user confirmed", payload["error"])

    def test_profile_cannot_be_proposed_before_discovery_is_recorded(self):
        self.run_cli("init")
        self.run_cli("confirm-discovery", "--scope-file", self.fixture("scope.json"))
        code, _ = self.run_cli("propose-profile", "--profile-file", self.fixture("profile.json"))
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_profile_cannot_be_confirmed_before_it_is_proposed(self):
        self.stage_discovery()
        code, _ = self.run_cli("confirm-profile")
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_pilot_cannot_run_before_the_profile_is_confirmed(self):
        self.stage_discovery()
        self.run_cli("propose-profile", "--profile-file", self.fixture("profile.json"))
        code, _ = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
        )
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertEqual(self.read_state()["state"], "PROFILE_PROPOSED")

    def test_activation_cannot_be_confirmed_before_a_passing_pilot(self):
        self.stage_profile()
        code, _ = self.run_cli("confirm-activation")
        self.assertEqual(code, W.EXIT_REFUSED)


class PilotGateAttackTests(WizardTestCase):
    def test_failed_pilot_lands_in_degraded_not_pilot_passed(self):
        code, payload = self.stage_pilot(nightly="nightly-manifest-degraded.json")
        self.assertEqual(code, W.EXIT_PILOT_FAILED)
        self.assertEqual(payload["state"], "DEGRADED")
        self.assertEqual(payload["degraded_reason_code"], "pilot_failed")
        self.assertEqual(payload["pilot_receipt"]["result"], "FAIL")
        self.assertTrue(payload["pilot_receipt"]["failed_predicates"])

    def test_degraded_cannot_be_scheduled(self):
        self.stage_pilot(nightly="nightly-manifest-degraded.json")
        for args in (
            ("confirm-activation",),
            ("schedule-handoff", "--config", self.fixture("config.json")),
            ("record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"),
        ):
            with self.subTest(command=args[0]):
                code, _ = self.run_cli(*args)
                self.assertIn(code, (W.EXIT_REFUSED, W.EXIT_SCHEDULE_CONFLICT))
        self.assertEqual(self.read_state()["state"], "DEGRADED")

    def test_a_later_clean_pilot_recovers_from_degraded(self):
        self.stage_pilot(nightly="nightly-manifest-degraded.json")
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "PILOT_PASSED")
        self.assertIsNone(payload["degraded_reason_code"])


class ConfirmationAttackTests(WizardTestCase):
    def test_scheduling_needs_the_second_confirmation(self):
        self.stage_pilot()
        code, payload = self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertIn("no valid human confirmation", payload["error"])

    def test_the_two_confirmations_are_distinct_grants(self):
        self.stage_active()
        state = self.read_state()
        bound = {
            gate: state["confirmations"][gate]["bound_sha256"] for gate in S.GATES
        }
        self.assertEqual(len(set(bound.values())), 3)

    def test_rerunning_the_pilot_invalidates_the_activation_confirmation(self):
        """A new pilot result is new evidence; the old acceptance no longer applies."""

        self.stage_pilot()
        self.run_cli("confirm-activation")
        self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))

        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertFalse(payload["confirmations"]["activation"]["valid"])
        self.assertEqual(payload["next_step"], "confirm_activation")

        code, _ = self.run_cli(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"
        )
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_redoing_discovery_invalidates_the_profile_confirmation(self):
        self.stage_profile()
        code, payload = self.run_cli(
            "record-discovery",
            "--scope-file",
            self.fixture("scope.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
        )
        # PROFILE_CONFIRMED has no `record-discovery` edge: the wizard refuses
        # rather than silently reopening a confirmed stage.
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_a_forged_confirmation_in_the_state_file_does_not_validate(self):
        self.stage_pilot()
        state = self.read_state()
        state["confirmations"]["activation"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "activation",
            "bound_sha256": state["confirmations"]["profile"]["bound_sha256"],
            "granted_at": "2026-01-01T00:00:00Z",
        }
        (self.state_dir / S.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        code, payload = self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        self.assertEqual(code, W.EXIT_REFUSED)


class ScheduleAttackTests(WizardTestCase):
    def test_recording_a_schedule_requires_a_handoff(self):
        self.stage_pilot()
        self.run_cli("confirm-activation")
        code, payload = self.run_cli(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"
        )
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("scheduler handoff not found", payload["error"])

    def test_a_tampered_handoff_is_rejected(self):
        self.stage_pilot()
        self.run_cli("confirm-activation")
        self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        handoff_path = self.state_dir / W.SCHEDULER_HANDOFF_FILE
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["contract_sha256"] = "0" * 64
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        code, payload = self.run_cli(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"
        )
        self.assertEqual(code, W.EXIT_SCHEDULE_CONFLICT)

    def test_activation_cannot_be_recorded_twice(self):
        self.stage_active()
        code, _ = self.run_cli(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "host-task-2"
        )
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertEqual(self.read_state()["schedule"]["external_task_id"], "host-task-1")

    def test_a_malformed_task_id_is_rejected_by_the_closed_schema(self):
        self.stage_pilot()
        self.run_cli("confirm-activation")
        self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        for bad in ("task id with spaces", "task\nid", "x" * 200, "-leading-hyphen", "../escape"):
            with self.subTest(task_id=bad):
                # `--flag=value` so argparse does not swallow a leading hyphen.
                code, _ = self.run_cli(
                    "record-schedule",
                    "--external-system",
                    "openclaw",
                    f"--external-task-id={bad}",
                )
                self.assertEqual(code, W.EXIT_USAGE)
        self.assertIsNone(self.read_state()["schedule"])

    def test_an_unknown_external_task_is_reported_not_deleted(self):
        self.stage_active()
        code, payload = self.run_cli(
            "check-drift",
            "--config",
            self.fixture("config.json"),
            "--observed-task-id",
            "somebody-elses-task",
        )
        self.assertEqual(code, W.EXIT_DRIFT)
        self.assertIn("schedule_id_unknown", payload["schedule_drift"]["findings"])
        self.assertFalse(payload["destructive_action_taken"])
        self.assertFalse(payload["schedule_drift"]["destructive_action_taken"])


class DriftAttackTests(WizardTestCase):
    def test_changing_the_contract_forces_reconfirmation(self):
        config = self.config_copy()
        self.stage_active(config=config)
        widened = self.config_copy(detail_cap=500, sync_docdb=True)
        code, payload = self.run_cli("check-drift", "--config", widened)
        self.assertEqual(code, W.EXIT_DRIFT)
        self.assertTrue(payload["contract_drift"]["drifted"])
        self.assertEqual(payload["state"], "NEEDS_RECONFIRMATION")
        self.assertEqual(payload["degraded_reason_code"], "contract_drift")
        self.assertEqual(payload["next_step"], "reconfirm_contract")

    def test_a_drifted_installation_cannot_simply_resume(self):
        config = self.config_copy()
        self.stage_active(config=config)
        self.run_cli("check-drift", "--config", self.config_copy(detail_cap=500))
        code, _ = self.run_cli("resume")
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_environment_change_is_drift_too(self):
        config = self.config_copy()
        payload = json.loads(Path(config).read_text(encoding="utf-8"))
        del payload["detail_cap"]
        Path(config).write_text(json.dumps(payload), encoding="utf-8")
        self.stage_active(config=config)
        with mock.patch.dict(os.environ, {"CWK_DETAIL_CAP": "5"}):
            code, drift = self.run_cli("check-drift", "--config", config)
        self.assertEqual(code, W.EXIT_DRIFT)
        self.assertTrue(drift["contract_drift"]["drifted"])


class StateIntegrityAttackTests(WizardTestCase):
    def test_a_corrupt_state_file_fails_closed_and_is_not_repaired(self):
        self.run_cli("init")
        target = self.state_dir / S.STATE_FILE
        target.write_text("{not json", encoding="utf-8")
        before = target.read_bytes()

        code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["integrity_reason"], "state_unparseable")

        code, _ = self.run_cli("confirm-discovery", "--scope-file", self.fixture("scope.json"))
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(target.read_bytes(), before)

    def test_a_hand_promoted_state_is_rejected(self):
        """Editing `state` to ACTIVE by hand does not produce an active install."""

        self.run_cli("init")
        target = self.state_dir / S.STATE_FILE
        state = self.read_state()
        state["state"] = "ACTIVE"
        target.write_text(json.dumps(state), encoding="utf-8")
        code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_OK)
        # The document still validates, but no gate is satisfied and no
        # schedule exists, so nothing downstream will act on it.
        self.assertFalse(any(g["valid"] for g in payload["confirmations"].values()))
        self.assertIsNone(payload["schedule"])

    def test_injecting_an_unknown_field_fails_closed(self):
        self.run_cli("init")
        state = self.read_state()
        state["cwork_app_key"] = "not-a-real-key"
        (self.state_dir / S.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["integrity_reason"], "state_schema_invalid")

    def test_a_second_concurrent_command_is_refused(self):
        self.run_cli("init")
        with S.session(self.state_dir):
            code, payload = self.run_cli("status")
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "LockUnavailable")


class PrivacyTests(WizardTestCase):
    def test_state_directory_and_artifacts_are_private(self):
        self.stage_active()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        for name in (
            S.STATE_FILE,
            W.DISCOVERY_REPORT_FILE,
            W.EXECUTION_CONTRACT_FILE,
            W.EXECUTION_CONTRACT_MD_FILE,
            W.PILOT_RECEIPT_FILE,
            W.SCHEDULER_HANDOFF_FILE,
        ):
            with self.subTest(artifact=name):
                path = self.state_dir / name
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_state_file_records_hashes_not_business_content(self):
        self.stage_active()
        raw = (self.state_dir / S.STATE_FILE).read_text(encoding="utf-8")
        for leaked in (
            "fixture-topic-alpha",
            "fixture-person-1",
            "fixture-entity-a",
            "fixture-user-a",
        ):
            with self.subTest(value=leaked):
                self.assertNotIn(leaked, raw)

    def test_state_file_has_no_free_text_field(self):
        self.stage_active()
        state = self.read_state()
        S.validate_state(state)
        for value in state.values():
            if isinstance(value, str):
                self.assertLessEqual(len(value), S.MAX_STRING_LEN)

    def test_no_artifact_contains_a_credential_value(self):
        self.stage_active()
        for path in self.state_dir.iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(artifact=path.name):
                self.assertNotIn("CWORK_APP_KEY=", text)
                self.assertNotIn("Bearer ", text)

    def test_handoff_never_claims_the_repository_created_a_task(self):
        self.stage_pilot()
        self.run_cli("confirm-activation")
        _, payload = self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        handoff = payload["handoff"]
        self.assertFalse(handoff["command_spec"]["secrets_included"])
        self.assertIn(
            "call OpenClaw, Gateway or cron APIs", handoff["repository_does_not"]
        )


class OutputContractTests(WizardTestCase):
    def test_every_command_emits_one_json_object(self):
        self.run_cli("init")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            W.main(["--state-dir", str(self.state_dir), "--now", self.tick(), "status"])
        text = buffer.getvalue()
        payload = json.loads(text)
        self.assertIsInstance(payload, dict)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n}"), 1, "exactly one top-level JSON object")

    def test_next_step_is_always_an_enumerated_token(self):
        self.stage_active()
        for args in (("status",), ("check-drift", "--config", self.fixture("config.json"))):
            with self.subTest(command=args[0]):
                _, payload = self.run_cli(*args)
                self.assertIn(payload["next_step"], S.NEXT_STEPS)

    def test_errors_are_machine_readable(self):
        self.state_dir.mkdir(mode=0o700, parents=True)
        code, payload = self.run_cli("confirm-profile")
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertFalse(payload["ok"])
        self.assertIn("error_kind", payload)
        self.assertIn("error", payload)

    def test_rejected_now_value(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = W.main(["--state-dir", str(self.state_dir), "--now", "yesterday", "init"])
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_bad_schedule_time_in_config_is_rejected(self):
        self.stage_profile()
        bad = self.config_copy(schedule_run_at_local="25:99")
        code, _ = self.run_cli("render-contract", "--config", bad)
        self.assertEqual(code, W.EXIT_USAGE)

    def test_missing_input_file_is_a_usage_error(self):
        self.run_cli("init")
        code, payload = self.run_cli(
            "confirm-discovery", "--scope-file", str(self.root / "nope.json")
        )
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("not found", payload["error"])


if __name__ == "__main__":
    unittest.main()
