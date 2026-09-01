"""RT-032: activation state machine, gate binding and private persistence.

These tests assert the properties the activation design depends on:
the transition table is closed, the two human gates are cryptographically
separated, any change to the bound facts invalidates a confirmation, the
state document is a closed schema with nowhere to hide a credential, and
the state file is written atomically with private permissions.

No network, no subprocess, no real CWork/DocDB access.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_activation_state as S  # noqa: E402
import cwk_atomic_file as A  # noqa: E402

NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def fresh(**overrides) -> dict:
    state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
    state.update(overrides)
    return state


class TransitionTableTests(unittest.TestCase):
    def test_every_transition_target_is_a_known_state(self):
        for (source, event), target in S.TRANSITIONS.items():
            self.assertIn(source, S.STATES + (S.UNINITIALIZED,))
            self.assertIn(event, S.EVENTS)
            self.assertIn(target, S.STATES)

    def test_unknown_event_is_refused(self):
        with self.assertRaises(S.IllegalTransition):
            S.next_state_for("INSTALLED", "definitely-not-an-event")

    def test_absent_pair_is_refused(self):
        """Anything not written in the table is illegal. Fail closed by default."""

        with self.assertRaises(S.IllegalTransition):
            S.next_state_for("INSTALLED", "record-schedule")

    def test_cannot_jump_from_installed_to_active(self):
        for event in S.EVENTS:
            if event == "confirm-discovery":
                continue
            with self.assertRaises(S.IllegalTransition):
                S.next_state_for("INSTALLED", event)

    def test_pilot_pass_is_unreachable_from_a_failing_pilot(self):
        """`record-pilot-fail` never lands on PILOT_PASSED, from any state."""

        for state in S.STATES:
            target = S.TRANSITIONS.get((state, "record-pilot-fail"))
            if target is not None:
                self.assertEqual(target, "DEGRADED")

    def test_active_is_only_reachable_by_recording_a_schedule(self):
        sources = [
            (src, ev) for (src, ev), dst in S.TRANSITIONS.items() if dst == "ACTIVE"
        ]
        self.assertEqual(sorted(sources), [("PAUSED", "resume"), ("PILOT_PASSED", "record-schedule")])

    def test_apply_transition_appends_history_and_bumps_revision(self):
        state = fresh()
        before = state["revision"]
        S.apply_transition(
            state,
            event="confirm-discovery",
            now=LATER,
            authorization="human_confirmation_discovery",
            next_step="run_discovery",
        )
        self.assertEqual(state["state"], "READY_FOR_DISCOVERY")
        self.assertEqual(state["revision"], before + 1)
        self.assertEqual(state["history"][-1]["event"], "confirm-discovery")
        self.assertEqual(state["history"][-1]["from_state"], "INSTALLED")

    def test_history_is_bounded(self):
        state = fresh(state="ACTIVE")
        for index in range(S.MAX_HISTORY + 20):
            event = "pause" if index % 2 == 0 else "resume"
            S.apply_transition(state, event=event, now=NOW)
        self.assertEqual(len(state["history"]), S.MAX_HISTORY)
        S.validate_state(state)


class GateBindingTests(unittest.TestCase):
    def test_gate_is_part_of_the_preimage(self):
        """A discovery-gate hash can never be replayed as an activation-gate hash."""

        activation_id = S.new_activation_id()
        discovery = S.compute_binding_sha256(
            gate="discovery", activation_id=activation_id, discovery_scope_sha256=SHA_A
        )
        profile = S.compute_binding_sha256(
            gate="profile",
            activation_id=activation_id,
            discovery_receipt_sha256=SHA_A,
            profile_sha256=SHA_B,
        )
        activation = S.compute_binding_sha256(
            gate="activation",
            activation_id=activation_id,
            contract_sha256=SHA_A,
            profile_sha256=SHA_B,
            pilot_receipt_sha256=SHA_C,
        )
        self.assertEqual(len({discovery, profile, activation}), 3)

    def test_activation_id_is_part_of_the_preimage(self):
        """A confirmation cannot be lifted from one installation into another."""

        first = S.compute_binding_sha256(
            gate="discovery", activation_id=S.new_activation_id(), discovery_scope_sha256=SHA_A
        )
        second = S.compute_binding_sha256(
            gate="discovery", activation_id=S.new_activation_id(), discovery_scope_sha256=SHA_A
        )
        self.assertNotEqual(first, second)

    def test_binding_rejects_wrong_field_set(self):
        activation_id = S.new_activation_id()
        with self.assertRaises(S.ActivationContractError):
            S.compute_binding_sha256(
                gate="activation", activation_id=activation_id, contract_sha256=SHA_A
            )
        with self.assertRaises(S.ActivationContractError):
            S.compute_binding_sha256(
                gate="discovery",
                activation_id=activation_id,
                discovery_scope_sha256=SHA_A,
                profile_sha256=SHA_B,
            )

    def test_binding_rejects_non_hash_values(self):
        with self.assertRaises(S.ActivationContractError):
            S.compute_binding_sha256(
                gate="discovery",
                activation_id=S.new_activation_id(),
                discovery_scope_sha256="not-a-hash",
            )

    def test_binding_is_stable(self):
        activation_id = S.new_activation_id()
        args = dict(gate="profile", activation_id=activation_id)
        first = S.compute_binding_sha256(
            **args, discovery_receipt_sha256=SHA_A, profile_sha256=SHA_B
        )
        second = S.compute_binding_sha256(
            **args, profile_sha256=SHA_B, discovery_receipt_sha256=SHA_A
        )
        self.assertEqual(first, second)

    def test_grant_becomes_invalid_when_bound_facts_change(self):
        state = fresh(discovery_scope_sha256=SHA_A)
        state["confirmations"]["discovery"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "discovery",
            "bound_sha256": S.current_binding(state, "discovery"),
            "granted_at": NOW,
        }
        self.assertTrue(S.grant_is_valid(state, "discovery"))

        state["discovery_scope_sha256"] = SHA_B
        self.assertFalse(S.grant_is_valid(state, "discovery"))
        self.assertEqual(S.invalidate_stale_confirmations(state), ["discovery"])
        self.assertIsNone(state["confirmations"]["discovery"])

    def test_forged_binding_does_not_validate(self):
        state = fresh(discovery_scope_sha256=SHA_A)
        state["confirmations"]["discovery"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "discovery",
            "bound_sha256": SHA_C,
            "granted_at": NOW,
        }
        self.assertFalse(S.grant_is_valid(state, "discovery"))

    def test_cross_gate_replay_does_not_validate(self):
        """Pasting the discovery hash into the activation slot is rejected."""

        state = fresh(
            discovery_scope_sha256=SHA_A,
            discovery_receipt_sha256=SHA_A,
            profile_sha256=SHA_B,
            contract_sha256=SHA_A,
            pilot_receipt_sha256=SHA_C,
        )
        state["confirmations"]["activation"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "activation",
            "bound_sha256": S.current_binding(state, "discovery"),
            "granted_at": NOW,
        }
        self.assertFalse(S.grant_is_valid(state, "activation"))

    def test_binding_is_none_when_facts_are_incomplete(self):
        state = fresh()
        self.assertIsNone(S.current_binding(state, "activation"))
        self.assertFalse(S.grant_is_valid(state, "activation"))


class ClosedSchemaTests(unittest.TestCase):
    def test_default_state_validates(self):
        S.validate_state(fresh())

    def test_extra_top_level_field_is_rejected(self):
        state = fresh()
        state["cwork_app_key"] = "value"
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)

    def test_missing_field_is_rejected(self):
        state = fresh()
        del state["schedule"]
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)

    def test_long_strings_are_rejected_everywhere(self):
        """Second line of defense: no field can hold a secret or a report body."""

        state = fresh()
        state["schedule"] = {
            "external_system": "openclaw",
            "external_task_id": "x" * 200,
            "bound_contract_sha256": SHA_A,
            "handoff_sha256": SHA_B,
            "recorded_at": NOW,
            "status": "enabled",
        }
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)

    def test_control_characters_are_rejected(self):
        state = fresh()
        state["schedule"] = {
            "external_system": "openclaw",
            "external_task_id": "task\n1",
            "bound_contract_sha256": SHA_A,
            "handoff_sha256": SHA_B,
            "recorded_at": NOW,
            "status": "enabled",
        }
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)

    def test_hash_fields_must_be_sha256_hex(self):
        for value in ("nope", SHA_A.upper(), "a" * 63, 12):
            state = fresh(profile_sha256=value)
            with self.assertRaises(S.ActivationContractError):
                S.validate_state(state)

    def test_timestamps_must_be_utc_seconds(self):
        for value in ("2026-01-01", "2026-01-01T00:00:00+08:00", "2026-01-01T00:00:00.5Z"):
            state = fresh(created_at=value)
            with self.assertRaises(S.ActivationContractError):
                S.validate_state(state)

    def test_unknown_state_name_is_rejected(self):
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(fresh(state="TOTALLY_ACTIVE"))

    def test_unknown_degraded_reason_is_rejected(self):
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(fresh(degraded_reason_code="because"))

    def test_confirmation_gate_must_match_its_slot(self):
        state = fresh(discovery_scope_sha256=SHA_A)
        state["confirmations"]["discovery"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "activation",
            "bound_sha256": SHA_A,
            "granted_at": NOW,
        }
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)

    def test_history_may_not_be_empty(self):
        state = fresh()
        state["history"] = []
        with self.assertRaises(S.ActivationContractError):
            S.validate_state(state)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "activation"

    def tearDown(self):
        self.tmp.cleanup()

    def test_directory_and_file_are_private(self):
        with S.session(self.dir, create=True) as sess:
            sess.state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
            sess.commit()
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.dir / S.STATE_FILE).stat().st_mode), 0o600
        )

    def test_round_trip(self):
        with S.session(self.dir, create=True) as sess:
            state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
            sess.state = state
            sess.commit()
            activation_id = state["activation_id"]
        with S.session(self.dir) as sess:
            self.assertIsNone(sess.integrity_reason)
            self.assertEqual(sess.state["activation_id"], activation_id)

    def test_missing_file_reads_as_uninitialized(self):
        with S.session(self.dir, create=True) as sess:
            self.assertIsNone(sess.state)
            self.assertIsNone(sess.integrity_reason)
            self.assertEqual(sess.current_state_name, S.UNINITIALIZED)
            with self.assertRaises(S.IllegalTransition):
                sess.require_healthy()

    def test_corrupt_file_fails_closed_and_is_left_untouched(self):
        self.dir.mkdir(mode=0o700, parents=True)
        target = self.dir / S.STATE_FILE
        target.write_text("{ this is not json", encoding="utf-8")
        before = target.read_bytes()
        with S.session(self.dir) as sess:
            self.assertEqual(sess.integrity_reason, "state_unparseable")
            with self.assertRaises(S.StateIntegrityError):
                sess.require_healthy()
        self.assertEqual(target.read_bytes(), before)

    def test_unknown_schema_fails_closed(self):
        self.dir.mkdir(mode=0o700, parents=True)
        (self.dir / S.STATE_FILE).write_text(
            json.dumps({"schema": "cwk.activation_state.v99"}), encoding="utf-8"
        )
        with S.session(self.dir) as sess:
            self.assertEqual(sess.integrity_reason, "state_schema_unknown")

    def test_tampered_state_fails_closed(self):
        with S.session(self.dir, create=True) as sess:
            sess.state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
            sess.commit()
        target = self.dir / S.STATE_FILE
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["state"] = "ACTIVE"
        payload["injected"] = "yes"
        target.write_text(json.dumps(payload), encoding="utf-8")
        with S.session(self.dir) as sess:
            self.assertEqual(sess.integrity_reason, "state_schema_invalid")

    def test_duplicate_json_keys_are_rejected(self):
        self.dir.mkdir(mode=0o700, parents=True)
        (self.dir / S.STATE_FILE).write_text(
            '{"schema": "cwk.activation_state.v1", "state": "INSTALLED", "state": "ACTIVE"}',
            encoding="utf-8",
        )
        with S.session(self.dir) as sess:
            self.assertEqual(sess.integrity_reason, "state_unparseable")

    def test_concurrent_session_is_refused(self):
        with S.session(self.dir, create=True) as sess:
            sess.state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
            sess.commit()
            with self.assertRaises(A.LockUnavailable):
                with S.session(self.dir):
                    pass

    def test_compare_and_swap_detects_a_racing_writer(self):
        with S.session(self.dir, create=True) as sess:
            state = S.default_state(activation_id=S.new_activation_id(), now=NOW)
            sess.state = state
            sess.commit()
            dir_fd = sess.dir_fd
            # Someone else rewrote the file behind our back.
            (self.dir / S.STATE_FILE).write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(A.RevisionConflict):
                S.commit_state(dir_fd, state, sess.raw_sha)

    def test_serialisation_is_deterministic(self):
        state = fresh()
        self.assertEqual(S.serialize_state(state), S.serialize_state(dict(state)))
        self.assertTrue(S.serialize_state(state).endswith(b"\n"))


class NextStepTests(unittest.TestCase):
    def test_every_state_maps_to_a_known_token(self):
        for name in S.STATES:
            self.assertIn(S.next_step_for(fresh(state=name)), S.NEXT_STEPS)

    def test_pilot_passed_asks_for_the_second_confirmation_first(self):
        state = fresh(
            state="PILOT_PASSED",
            profile_sha256=SHA_B,
            contract_sha256=SHA_A,
            pilot_receipt_sha256=SHA_C,
        )
        self.assertEqual(S.next_step_for(state), "confirm_activation")
        state["confirmations"]["activation"] = {
            "confirmation_id": S.new_confirmation_id(),
            "gate": "activation",
            "bound_sha256": S.current_binding(state, "activation"),
            "granted_at": NOW,
        }
        self.assertEqual(S.next_step_for(state), "emit_scheduler_handoff")


if __name__ == "__main__":
    unittest.main()
