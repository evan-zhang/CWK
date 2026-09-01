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

import errno
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def run_cli_raw(self, *args: str) -> tuple[int, str]:
        argv = ["--state-dir", str(self.state_dir), "--now", self.tick(), *args]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = W.main(argv)
        return code, buffer.getvalue()

    def run_cli(self, *args: str) -> tuple[int, dict]:
        code, text = self.run_cli_raw(*args)
        return code, json.loads(text)

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

    def profile_copy(self, **overrides) -> str:
        payload = json.loads((FIXTURES / "profile.json").read_text(encoding="utf-8"))
        payload.update(overrides)
        target = self.root / f"profile-{len(list(self.root.glob('profile-*.json')))}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return str(target)

    def collect_copy(self, **overrides) -> str:
        payload = json.loads((FIXTURES / "collect-manifest.json").read_text(encoding="utf-8"))
        payload.update(overrides)
        target = self.root / f"collect-{len(list(self.root.glob('collect-*.json')))}.json"
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

    def test_omitting_the_collection_receipt_cannot_pass(self):
        """Leaving the argument off is missing evidence, not a neutral choice."""

        self.stage_profile()
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
        )
        self.assertEqual(code, W.EXIT_PILOT_FAILED)
        self.assertEqual(payload["state"], "DEGRADED")
        self.assertEqual(payload["pilot_receipt"]["result"], "FAIL")
        self.assertIn("collect_receipt_present", payload["pilot_receipt"]["failed_predicates"])
        self.assertIn("daily_source_complete", payload["pilot_receipt"]["failed_predicates"])
        # And the missing evidence is named in the receipt, not just implied.
        self.assertIn(
            "collect_receipt_omitted", payload["pilot_receipt"]["collection_receipt"]["problems"]
        )

    def test_a_pilot_without_a_collection_receipt_cannot_be_activated(self):
        self.stage_profile()
        self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
        )
        code, _ = self.run_cli("confirm-activation")
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertEqual(self.read_state()["state"], "DEGRADED")

    def test_a_missing_collection_receipt_file_is_a_usage_error(self):
        """Naming a file that is not there is a different failure from omitting it."""

        self.stage_profile()
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            str(self.root / "no-such-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("not found", payload["error"])
        self.assertEqual(self.read_state()["state"], "PROFILE_CONFIRMED")

    def test_a_collection_receipt_of_the_wrong_shape_cannot_pass(self):
        self.stage_profile()
        broken = self.root / "broken-collect.json"
        broken.write_text(json.dumps({"daily_source_complete": "yes"}), encoding="utf-8")
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            str(broken),
        )
        self.assertEqual(code, W.EXIT_PILOT_FAILED)
        self.assertEqual(payload["state"], "DEGRADED")
        self.assertIn(
            "collect_receipt_shape_valid", payload["pilot_receipt"]["failed_predicates"]
        )

    def test_a_failed_collection_run_cannot_pass(self):
        self.stage_profile()
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.collect_copy(daily_source_complete=False, daily_source_failure_count=2),
        )
        self.assertEqual(code, W.EXIT_PILOT_FAILED)
        self.assertEqual(payload["state"], "DEGRADED")
        self.assertIn("collect_receipt_success", payload["pilot_receipt"]["failed_predicates"])

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

    def test_rerunning_the_pilot_on_new_evidence_invalidates_the_confirmation(self):
        """New collection evidence is a new fact; the old acceptance lapses.

        The second pilot still passes, so nothing here is a failure the user
        would notice — that is exactly the case where a stale confirmation
        would be dangerous.
        """

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
            "--collect-manifest",
            self.collect_copy(written_count=19),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["pilot_receipt"]["result"], "PASS")
        self.assertFalse(payload["confirmations"]["activation"]["valid"])
        self.assertEqual(payload["next_step"], "confirm_activation")

        code, _ = self.run_cli(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"
        )
        self.assertEqual(code, W.EXIT_REFUSED)

    def test_rerunning_the_pilot_on_identical_evidence_changes_nothing(self):
        """The receipt is a function of the evidence, so re-reading it is a no-op.

        This is the flip side of the test above: the confirmation survives only
        because every fact it was bound to is byte-for-byte the same one.
        """

        self.stage_pilot()
        self.run_cli("confirm-activation")
        first = self.read_state()["pilot_receipt_sha256"]

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
        self.assertEqual(payload["hashes"]["pilot_receipt_sha256"], first)
        self.assertTrue(payload["confirmations"]["activation"]["valid"])

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


class InvalidatedGateReportingTests(WizardTestCase):
    """`invalidated_gates` must name what *this* command invalidated.

    A command can drop a confirmation twice over: once on entry, for grants
    that were already stale, and again after it writes new facts of its own.
    Reporting only the first batch produces the worst kind of success payload
    — one that says "nothing lapsed" while `next_step` asks the user to
    confirm again. An AI reading that field would tell the user the pilot
    changed nothing and then be unable to explain the extra question.
    """

    def confirmed_pilot(self):
        """A passing pilot whose second confirmation is in place."""

        self.stage_pilot()
        _, payload = self.run_cli("confirm-activation")
        self.assertTrue(payload["confirmations"]["activation"]["valid"])

    def test_a_pilot_on_new_evidence_reports_the_gate_it_invalidated(self):
        self.confirmed_pilot()
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.collect_copy(written_count=19),
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["pilot_receipt"]["result"], "PASS")
        # The point of this test: the payload names the lapsed gate.
        self.assertEqual(payload["invalidated_gates"], ["activation"])
        # …and stays consistent with the rest of the same payload.
        self.assertFalse(payload["confirmations"]["activation"]["granted"])
        self.assertFalse(payload["confirmations"]["activation"]["valid"])
        self.assertEqual(payload["next_step"], "confirm_activation")
        self.assertEqual(payload["state"], "PILOT_PASSED")

    def test_a_pilot_on_identical_evidence_reports_no_invalidation(self):
        """The other half of the contract: it must not cry wolf either."""

        self.confirmed_pilot()
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
        self.assertEqual(payload["invalidated_gates"], [])
        self.assertTrue(payload["confirmations"]["activation"]["valid"])
        self.assertEqual(payload["next_step"], "emit_scheduler_handoff")

    def test_the_first_pilot_invalidates_nothing(self):
        self.stage_profile()
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
        self.assertEqual(payload["invalidated_gates"], [])
        self.assertEqual(payload["next_step"], "confirm_activation")

    def test_a_failing_rerun_reports_the_gate_and_still_degrades(self):
        """Reporting the lapse must not disturb the FAIL verdict itself."""

        self.confirmed_pilot()
        code, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.fixture("config.json"),
            "--nightly-manifest",
            self.fixture("nightly-manifest-degraded.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )
        self.assertEqual(code, W.EXIT_PILOT_FAILED)
        self.assertEqual(payload["invalidated_gates"], ["activation"])
        self.assertEqual(payload["state"], "DEGRADED")
        self.assertEqual(payload["degraded_reason_code"], "pilot_failed")
        self.assertEqual(payload["next_step"], "rerun_pilot")

    def test_reported_gates_are_always_known_tokens(self):
        self.confirmed_pilot()
        _, payload = self.run_cli(
            "record-pilot",
            "--config",
            self.config_copy(detail_cap=7),
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )
        gates = payload["invalidated_gates"]
        self.assertTrue(set(gates) <= set(S.GATES))
        self.assertEqual(len(gates), len(set(gates)), "no gate is reported twice")
        self.assertEqual(gates, [g for g in S.GATES if g in set(gates)], "stable order")
        # A changed contract hash also lapses the second confirmation.
        self.assertIn("activation", gates)

    def test_merge_keeps_the_union_deduplicated_and_ordered(self):
        self.assertEqual(W._merge_gates([], []), [])
        self.assertEqual(W._merge_gates(["activation"], ["activation"]), ["activation"])
        self.assertEqual(
            W._merge_gates(["activation"], ["discovery", "profile"]),
            ["discovery", "profile", "activation"],
        )

    # ── the same defect, reachable through propose-profile ─────────────────

    def drifted_from_active(self):
        """ACTIVE → contract drift → NEEDS_RECONFIRMATION, second gate intact.

        Drift is flagged by comparing a *new* config against the recorded
        contract hash; it does not rewrite that hash. So the activation grant
        — bound to (contract, profile, pilot receipt) — survives the drift and
        is still valid when the user comes back to re-propose a profile. That
        is what makes the next command's report load-bearing.
        """

        config = self.fixture("config.json")
        self.stage_active(config=config)
        code, payload = self.run_cli(
            "check-drift", "--config", self.config_copy(detail_cap=11)
        )
        self.assertEqual(code, W.EXIT_DRIFT)
        self.assertEqual(payload["state"], "NEEDS_RECONFIRMATION")
        self.assertEqual(payload["invalidated_gates"], [])
        self.assertTrue(payload["confirmations"]["activation"]["valid"])
        self.assertTrue(payload["confirmations"]["profile"]["valid"])

    def test_reproposing_a_profile_after_drift_reports_the_gates_it_invalidated(self):
        self.drifted_from_active()
        code, payload = self.run_cli(
            "propose-profile", "--profile-file", self.profile_copy(reporting_rhythm="daily")
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["state"], "PROFILE_PROPOSED")
        # The regression: a new profile lapses both gates bound to it, and the
        # payload has to say so. Before the fix this was [] while next_step
        # already demanded a fresh confirmation.
        self.assertEqual(payload["invalidated_gates"], ["profile", "activation"])
        self.assertFalse(payload["confirmations"]["profile"]["valid"])
        self.assertFalse(payload["confirmations"]["activation"]["valid"])
        self.assertEqual(payload["next_step"], "confirm_profile")

    def test_reproposing_the_same_profile_after_drift_does_not_cry_wolf(self):
        self.drifted_from_active()
        code, payload = self.run_cli(
            "propose-profile", "--profile-file", self.fixture("profile.json")
        )
        self.assertEqual(code, W.EXIT_OK)
        self.assertEqual(payload["invalidated_gates"], [])
        self.assertTrue(payload["confirmations"]["profile"]["valid"])
        self.assertTrue(payload["confirmations"]["activation"]["valid"])

    def test_every_command_that_writes_facts_reports_both_batches(self):
        """A structural guard, so the next such command cannot forget.

        The defect was not a typo — it was one command written differently
        from its siblings. Pin the shape: every command that calls
        `invalidate_stale_confirmations` a second time must feed that return
        value into `_merge_gates`, never drop it on the floor.
        """

        source = Path(W.__file__).read_text(encoding="utf-8")
        bodies = re.split(r"\ndef (cmd_[a-z_]+)\(", source)
        checked = []
        for name, body in zip(bodies[1::2], bodies[2::2]):
            # `_prepare` already performs the entry-time sweep. A command that
            # *also* calls the sweep directly has two batches to account for.
            if "_prepare(sess)" not in body:
                continue
            if "invalidate_stale_confirmations(" not in body:
                continue
            checked.append(name)
            self.assertIn(
                "_merge_gates(",
                body,
                f"{name} writes new facts and re-checks confirmations but "
                "reports only the first batch",
            )
        self.assertEqual(
            checked,
            ["cmd_confirm_discovery", "cmd_record_discovery", "cmd_propose_profile",
             "cmd_record_pilot"],
            "the set of two-batch commands changed; re-derive which gates each one "
            "can lapse instead of only updating this list",
        )


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


class InputAndPersistenceFailureTests(WizardTestCase):
    """Broken paths are input errors, not crashes.

    Every one of these used to leave a Python traceback on stderr, which both
    breaks the "one JSON object per command" contract the Skill relies on and
    prints the absolute path of a private state directory.
    """

    def assert_no_absolute_path(self, text: str) -> None:
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.state_dir), text)
        self.assertNotIn(str(PROJECT), text)

    def test_a_state_dir_that_is_really_a_file_fails_closed(self):
        self.state_dir.write_text("not a directory", encoding="utf-8")
        code, text = self.run_cli_raw("init")
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_kind"], "IOFailure")
        self.assertEqual(payload["errno"], "EEXIST")
        self.assert_no_absolute_path(text)
        self.assertEqual(self.state_dir.read_text(encoding="utf-8"), "not a directory")

    def test_a_symlinked_state_dir_is_refused(self):
        real = self.root / "elsewhere"
        real.mkdir(mode=0o700)
        self.state_dir.symlink_to(real)
        code, text = self.run_cli_raw("init")
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "ContainmentError")
        self.assert_no_absolute_path(text)
        self.assertEqual(list(real.iterdir()), [], "nothing was written through the symlink")

    def test_a_dangling_symlink_state_dir_fails_closed(self):
        self.state_dir.symlink_to(self.root / "nowhere")
        code, text = self.run_cli_raw("init")
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "IOFailure")
        self.assert_no_absolute_path(text)
        self.assertFalse((self.root / "nowhere").exists())

    def test_a_missing_state_dir_is_reported_without_the_path(self):
        code, text = self.run_cli_raw("status")
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("does not exist", payload["error"])
        self.assert_no_absolute_path(text)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_an_unreadable_input_file_is_reported_without_the_path(self):
        self.run_cli("init")
        blocked = self.root / "blocked.json"
        blocked.write_text(json.dumps({"mirror_kind": "personal"}), encoding="utf-8")
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o600)
        code, text = self.run_cli_raw("confirm-discovery", "--scope-file", str(blocked))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "InputIOError")
        self.assertIn("EACCES", payload["error"])
        self.assert_no_absolute_path(text)
        self.assertEqual(self.read_state()["state"], "INSTALLED")

    def test_a_directory_passed_as_an_input_file_is_refused(self):
        self.run_cli("init")
        code, text = self.run_cli_raw("confirm-discovery", "--scope-file", str(self.root))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("not found", payload["error"])
        self.assert_no_absolute_path(text)

    def test_a_dangling_symlink_input_file_is_refused(self):
        self.run_cli("init")
        link = self.root / "scope-link.json"
        link.symlink_to(self.root / "gone.json")
        code, text = self.run_cli_raw("confirm-discovery", "--scope-file", str(link))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("not found", payload["error"])
        self.assert_no_absolute_path(text)

    def test_input_file_contents_are_never_echoed_back(self):
        self.run_cli("init")
        secret = self.root / "secret.json"
        secret.write_text("{ CWORK_APP_KEY: sk-fixture-not-a-real-key", encoding="utf-8")
        code, text = self.run_cli_raw("confirm-discovery", "--scope-file", str(secret))
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertNotIn("sk-fixture-not-a-real-key", text)
        self.assertNotIn("CWORK_APP_KEY", text)
        self.assert_no_absolute_path(text)

    def test_input_json_that_cannot_be_canonicalised_is_a_usage_error(self):
        """A number outside the safe integer range is bad input, not a crash."""

        self.run_cli("init")
        unsafe = self.root / "unsafe.json"
        unsafe.write_text('{"mirror_kind": "personal", "n": 9007199254740993}', encoding="utf-8")
        code, text = self.run_cli_raw("confirm-discovery", "--scope-file", str(unsafe))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "ContractError")
        self.assert_no_absolute_path(text)
        self.assertEqual(self.read_state()["state"], "INSTALLED")

    def test_a_corrupt_artifact_is_reported_without_the_path(self):
        self.stage_pilot()
        self.run_cli("confirm-activation")
        self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        (self.state_dir / W.SCHEDULER_HANDOFF_FILE).write_text("{ broken", encoding="utf-8")
        code, text = self.run_cli_raw(
            "record-schedule", "--external-system", "openclaw", "--external-task-id", "t1"
        )
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertIn("corrupt", payload["error"])
        self.assert_no_absolute_path(text)

    def test_every_failure_still_emits_exactly_one_json_object(self):
        self.state_dir.write_text("not a directory", encoding="utf-8")
        for args in (
            ("init",),
            ("status",),
            ("confirm-discovery", "--scope-file", self.fixture("scope.json")),
            ("record-pilot", "--config", self.fixture("config.json")),
        ):
            with self.subTest(command=args[0]):
                code, text = self.run_cli_raw(*args)
                payload = json.loads(text)
                self.assertNotEqual(code, W.EXIT_OK)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["command"], args[0])
                self.assertIn("error_kind", payload)
                self.assertEqual(text.count("\n}"), 1)
                self.assert_no_absolute_path(text)

    # ── the intended I/O contract, stated as assertions ────────────────────
    #
    # Reading an input file or writing the state directory can fail for
    # reasons that say nothing about the user's data: a typo in a path, a
    # read-only volume, a full disk. None of that is a verdict about the
    # nightly run, so none of it may look like one. The wizard aborts, the
    # activation state stays byte-for-byte as it was, and no transition
    # receipt is appended — DEGRADED is reserved for a real FAIL receipt.

    def assert_state_untouched(self, before_bytes: bytes, before: dict) -> None:
        after_bytes = (self.state_dir / S.STATE_FILE).read_bytes()
        self.assertEqual(after_bytes, before_bytes, "the state file was rewritten")
        after = json.loads(after_bytes.decode("utf-8"))
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(len(after["history"]), len(before["history"]))
        self.assertIsNone(after["degraded_reason_code"])
        self.assertNotEqual(after["state"], "DEGRADED")

    def pilot_argv(self, config: str) -> tuple[str, ...]:
        return (
            "record-pilot",
            "--config",
            config,
            "--nightly-manifest",
            self.fixture("nightly-manifest.json"),
            "--acceptance",
            self.fixture("acceptance.json"),
            "--collect-manifest",
            self.fixture("collect-manifest.json"),
        )

    def test_an_unreadable_input_is_a_usage_failure_not_a_degradation(self):
        self.stage_profile()
        before_bytes = (self.state_dir / S.STATE_FILE).read_bytes()
        before = self.read_state()
        code, text = self.run_cli_raw(*self.pilot_argv(str(self.root / "gone.json")))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertNotEqual(code, W.EXIT_PILOT_FAILED)
        self.assertFalse(payload["ok"])
        self.assertNotIn("pilot_receipt", payload)
        self.assert_no_absolute_path(text)
        self.assert_state_untouched(before_bytes, before)
        self.assertEqual(self.read_state()["state"], "PROFILE_CONFIRMED")

    def test_a_failure_to_persist_records_no_verdict(self):
        """A full disk at commit time must not be mistaken for a bad run."""

        self.stage_profile()
        before_bytes = (self.state_dir / S.STATE_FILE).read_bytes()
        before = self.read_state()
        boom = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(S, "cas_write", side_effect=boom):
            code, text = self.run_cli_raw(*self.pilot_argv(self.fixture("config.json")))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["error_kind"], "IOFailure")
        self.assertEqual(payload["errno"], "ENOSPC")
        self.assert_no_absolute_path(text)
        self.assert_state_untouched(before_bytes, before)

    def test_a_failure_to_write_an_artifact_records_no_verdict(self):
        self.stage_profile()
        before_bytes = (self.state_dir / S.STATE_FILE).read_bytes()
        before = self.read_state()
        boom = OSError(errno.EROFS, "Read-only file system")
        with mock.patch.object(W, "write_atomic", side_effect=boom):
            code, text = self.run_cli_raw(*self.pilot_argv(self.fixture("config.json")))
        payload = json.loads(text)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(payload["errno"], "EROFS")
        self.assert_no_absolute_path(text)
        self.assert_state_untouched(before_bytes, before)

    def test_no_usage_failure_ever_enters_degraded(self):
        self.stage_profile()
        before_bytes = (self.state_dir / S.STATE_FILE).read_bytes()
        before = self.read_state()
        not_json = self.root / "not-json.json"
        not_json.write_text("{ still not json", encoding="utf-8")
        an_array = self.root / "array.json"
        an_array.write_text("[1, 2, 3]", encoding="utf-8")
        for label, config in (
            ("missing file", str(self.root / "absent.json")),
            ("a directory", str(self.root)),
            ("not json", str(not_json)),
            ("not an object", str(an_array)),
        ):
            with self.subTest(config=label):
                code, text = self.run_cli_raw(*self.pilot_argv(config))
                self.assertEqual(code, W.EXIT_USAGE)
                self.assertFalse(json.loads(text)["ok"])
                self.assert_no_absolute_path(text)
                self.assert_state_untouched(before_bytes, before)

    def test_redaction_strips_a_path_but_keeps_the_sentence(self):
        message = W.redact_message(f"scope file {self.root}/scope.json is unreadable")
        self.assertNotIn(str(self.root), message)
        self.assertIn("<redacted-path>", message)
        self.assertIn("is unreadable", message)
        # A slash inside a word is not a path and must survive untouched.
        self.assertEqual(W.redact_message("read/write mismatch"), "read/write mismatch")


class RedactionTests(unittest.TestCase):
    """The last gate on error text, exercised from both sides.

    Matching "anything with a slash" would turn `read/write mismatch` into
    noise; matching only "anything starting with a slash" lets a bare
    `state/activation/activation.json` through. Both directions are pinned.
    """

    LEAKS = (
        # the two the contract names explicitly
        "state/activation/activation.json",
        "client/secret.json",
        # and the shapes around them
        "cannot open state/activation/discovery-report.json for reading",
        "home/alice/cwk-mirror",
        "tests/fixtures/activation/config.json",
        "scripts/cwk_doctor.py",
        "a/b/c.md",
        # the absolute forms the first rule already owned
        "/Users/alice/cwk/config.json",
        "~/cwk/cwk-mirror.local.json",
        "./relative/config.json",
        "../sibling/config.json",
    )

    SAFE = (
        "read/write mismatch",
        "and/or",
        "input/output error",
        "pass/fail",
        "yes/no/maybe",
        "gateway_production/gateway_control",
        "the value 1/2.5 is out of range",
        "PASS/FAIL/SKIP",
        "24/7",
    )

    def test_relative_paths_are_redacted(self):
        for text in self.LEAKS:
            with self.subTest(text=text):
                cleaned = W.redact_message(text)
                self.assertIn("<redacted-path>", cleaned)

    def test_a_leaked_file_name_does_not_survive_redaction(self):
        cleaned = W.redact_message("refusing to read client/secret.json")
        self.assertNotIn("secret.json", cleaned)
        self.assertIn("refusing to read", cleaned)

    def test_ordinary_slash_phrases_are_left_alone(self):
        for text in self.SAFE:
            with self.subTest(text=text):
                self.assertEqual(W.redact_message(text), text)

    def test_a_trailing_period_survives_the_redaction(self):
        cleaned = W.redact_message("cannot open state/activation/activation.json.")
        self.assertEqual(cleaned, "cannot open <redacted-path>.")

    def test_every_command_error_is_short_and_single_line(self):
        cleaned = W.redact_message("x" * 5000 + "\n\tsecond line")
        self.assertLessEqual(len(cleaned), W.MAX_ERROR_CHARS)
        self.assertNotIn("\n", cleaned)


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

    def test_the_success_payload_carries_no_absolute_path(self):
        """The *success* side, not just the failure side.

        Failures were already scrubbed by `redact_message`. The handoff is the
        one success payload that used to echo back a host path — it named the
        config with `str(args.config)`, which on a real machine reads
        `/Users/<name>/…` and then entered `handoff_sha256`.
        """

        self.stage_pilot()
        self.run_cli("confirm-activation")
        _, payload = self.run_cli("schedule-handoff", "--config", self.fixture("config.json"))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(PROJECT), serialized)
        self.assertNotIn(str(self.root), serialized)
        locator = payload["handoff"]["config_locator"]
        self.assertEqual(locator["path"], "tests/fixtures/activation/config.json")
        self.assertEqual(locator["kind"], "project_relative")

    def test_no_stored_artifact_carries_an_absolute_path(self):
        self.stage_active()
        for path in self.state_dir.iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(artifact=path.name):
                self.assertNotIn(str(PROJECT), text)
                self.assertNotIn(str(self.root), text)

    def test_a_config_outside_the_project_refuses_the_handoff(self):
        """Fail closed: no handoff at all beats a handoff that leaks the path."""

        self.stage_pilot()
        self.run_cli("confirm-activation")
        outside = self.config_copy()  # written into the private temp dir
        code, payload = self.run_cli("schedule-handoff", "--config", outside)
        self.assertEqual(code, W.EXIT_REFUSED)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_kind"], "ConfigLocatorError")
        self.assertNotIn(str(self.root), payload["error"])
        self.assertNotIn(str(PROJECT), payload["error"])
        # …and nothing was written or advanced.
        self.assertFalse((self.state_dir / W.SCHEDULER_HANDOFF_FILE).exists())
        self.assertIsNone(self.read_state()["schedule_handoff_sha256"])
        self.assertEqual(self.read_state()["state"], "PILOT_PASSED")


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


class FailingSink(io.StringIO):
    """A stdout stand-in that fails the way a closed pipe or a full disk does.

    Deterministic on purpose: no subprocess, no real pipe timing, no reliance
    on how much the caller happened to buffer.
    """

    def __init__(self, exc: BaseException, *, after: int = 0, on_flush: bool = False):
        super().__init__()
        self.exc = exc
        self.after = after
        self.on_flush = on_flush
        self.write_calls = 0
        self.flush_calls = 0

    def write(self, text):  # type: ignore[override]
        self.write_calls += 1
        if not self.on_flush and self.write_calls > self.after:
            raise self.exc
        return super().write(text)

    def flush(self):  # type: ignore[override]
        self.flush_calls += 1
        if self.on_flush:
            raise self.exc
        return super().flush()


class BrokenOutputSinkTests(WizardTestCase):
    """stdout itself can fail. That is still a failure, and still quiet.

    `cwk_activation_wizard … | head -1` closes the pipe under the wizard.
    The contract for that case: never claim success, never print a traceback,
    never leak a private path, and never hand back an exit code outside the
    documented set — including the interpreter's own 120 for "error flushing
    stdout at exit".
    """

    def run_with_sink(self, sink, *args: str) -> tuple[int, str]:
        argv = ["--state-dir", str(self.state_dir), "--now", self.tick(), *args]
        stderr = io.StringIO()
        with redirect_stdout(sink), redirect_stderr(stderr):
            code = W.main(argv)
        return code, stderr.getvalue()

    def assert_clean_failure(self, code: int, stderr: str, emitted: str) -> None:
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertNotEqual(code, W.EXIT_OK, "a lost payload is never a success")
        self.assertEqual(stderr, "", "nothing is printed on the side channel")
        for marker in ("Traceback", 'File "', "BrokenPipeError", "cwk_activation_wizard.py"):
            self.assertNotIn(marker, emitted)
        self.assertNotIn(str(self.root), emitted)
        self.assertNotIn(str(self.state_dir), emitted)
        self.assertNotIn(str(PROJECT), emitted)

    def test_a_pipe_that_breaks_mid_payload_fails_quietly(self):
        self.run_cli("init")
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"), after=3)
        code, stderr = self.run_with_sink(sink, "status")
        self.assert_clean_failure(code, stderr, sink.getvalue())
        self.assertGreater(sink.write_calls, 3, "the break happened while writing")

    def test_a_pipe_that_breaks_on_the_first_write_fails_quietly(self):
        self.run_cli("init")
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"))
        code, stderr = self.run_with_sink(sink, "status")
        self.assert_clean_failure(code, stderr, sink.getvalue())
        self.assertEqual(sink.getvalue(), "", "not a single byte escaped")

    def test_a_pipe_that_breaks_on_the_final_flush_fails_quietly(self):
        self.run_cli("init")
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"), on_flush=True)
        code, stderr = self.run_with_sink(sink, "status")
        self.assert_clean_failure(code, stderr, sink.getvalue())
        self.assertEqual(sink.flush_calls, 1)

    def test_a_full_disk_on_stdout_behaves_the_same(self):
        """EPIPE is not special-cased; any write failure is handled alike."""

        self.run_cli("init")
        sink = FailingSink(OSError(errno.ENOSPC, "No space left on device"), after=2)
        code, stderr = self.run_with_sink(sink, "status")
        self.assert_clean_failure(code, stderr, sink.getvalue())

    def test_a_command_that_would_have_succeeded_still_exits_nonzero(self):
        """The state advanced, but the caller never heard about it."""

        self.stage_profile()
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"), after=1)
        code, stderr = self.run_with_sink(
            sink,
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
        self.assert_clean_failure(code, stderr, sink.getvalue())
        # The verdict itself was committed before the output was attempted,
        # so a re-read shows it; the exit code just refuses to vouch for it.
        self.assertEqual(self.read_state()["state"], "PILOT_PASSED")

    def test_an_already_failing_command_also_stays_nonzero(self):
        self.state_dir.mkdir(mode=0o700, parents=True)
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"), after=1)
        code, stderr = self.run_with_sink(sink, "confirm-profile")
        self.assert_clean_failure(code, stderr, sink.getvalue())

    def test_a_broken_pipe_on_an_error_payload_leaks_nothing(self):
        self.run_cli("init")
        secret = self.root / "secret.json"
        secret.write_text("{ CWORK_APP_KEY: sk-fixture-not-a-real-key", encoding="utf-8")
        sink = FailingSink(BrokenPipeError(errno.EPIPE, "Broken pipe"), after=6)
        code, stderr = self.run_with_sink(
            sink, "confirm-discovery", "--scope-file", str(secret)
        )
        self.assert_clean_failure(code, stderr, sink.getvalue())
        self.assertNotIn("sk-fixture-not-a-real-key", sink.getvalue())
        self.assertNotIn("CWORK_APP_KEY", sink.getvalue())

    def test_a_programmer_bug_in_the_output_sink_still_surfaces(self):
        """Only OSError is absorbed. A real defect must not be swallowed."""

        self.run_cli("init")
        sink = FailingSink(RuntimeError("this is a bug, not a broken pipe"), after=1)
        argv = ["--state-dir", str(self.state_dir), "--now", self.tick(), "status"]
        with redirect_stdout(sink):
            with self.assertRaises(RuntimeError):
                W.main(argv)

    def test_a_broken_real_pipe_is_neutralised_before_the_exit_flush(self):
        """CPython flushes stdout again at exit; that flush must not blow up.

        Left alone it prints "Exception ignored in: <_io.TextIOWrapper …>"
        and rewrites the exit status to 120 — neither of which is one of this
        module's exit codes, and neither of which the Skill can parse.
        """

        self.run_cli("init")
        read_fd, write_fd = os.pipe()
        os.close(read_fd)  # the downstream reader is gone before we write
        stream = os.fdopen(write_fd, "w", encoding="utf-8")
        self.addCleanup(stream.close)
        stderr = io.StringIO()
        argv = ["--state-dir", str(self.state_dir), "--now", self.tick(), "status"]
        with redirect_stdout(stream), redirect_stderr(stderr):
            code = W.main(argv)
        self.assertEqual(code, W.EXIT_USAGE)
        self.assertEqual(stderr.getvalue(), "")
        # The descriptor now points at the void, so the interpreter's own
        # exit-time flush has nothing left to fail on.
        self.assertTrue(stat.S_ISCHR(os.fstat(write_fd).st_mode))
        stream.write("x")
        stream.flush()  # would raise BrokenPipeError without the fix

    def test_neutralising_a_stream_without_a_descriptor_is_a_no_op(self):
        """In-memory stdout has no fileno; the guard must not crash on it."""

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            W._silence_broken_stdout()
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
