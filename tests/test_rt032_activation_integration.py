"""RT-032: activation as a product surface — installer, doctor, and the docs.

The state machine itself is covered by ``test_rt032_activation_state`` /
``_wizard`` / ``_contract``. This file covers the three places a person
actually meets it:

* the installer, which must stay side-effect free — installing a program is
  not the same act as authorising it to read someone's work every night;
* the doctor, which must report how far activation got without leaking a
  path, a hash, or any business content, and without failing the install
  checks when the private record is broken;
* the Skill-facing documents, which must keep describing the CLI that
  actually exists.

No network, no scheduled task, no CWork/DocDB call, no credential. The
installer runs against an isolated fixture copy (``InstallerFixture`` does
``cd`` into a temp directory), and every value used here is a sanitized
placeholder.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_activation_state as S  # noqa: E402
import cwk_activation_wizard as W  # noqa: E402
import cwk_doctor  # noqa: E402
from test_install_modes import InstallerFixture, _make_available  # noqa: E402

FIXTURES = PROJECT / "tests" / "fixtures" / "activation"

# The one line the installer is allowed to print about activation.
ACTIVATION_LINE = re.compile(r"^CWK_ACTIVATION=([A-Z_]+)$", re.MULTILINE)


def build_state(state_dir: Path, *steps: tuple[str, ...]) -> None:
    """Drive the real wizard to put a genuine record under ``state_dir``.

    Hand-writing a state file would only prove the probe can read something
    this test invented. Every state inspected below was produced by the same
    entry point a user would run.
    """
    clock = [0]

    def tick() -> str:
        clock[0] += 1
        return "2026-01-01T00:%02d:%02dZ" % (clock[0] // 60, clock[0] % 60)

    for step in steps:
        argv = ["--state-dir", str(state_dir), "--now", tick(), *step]
        with redirect_stdout(io.StringIO()):
            W.main(argv)


# ── 1. the installer ──────────────────────────────────────────────────────


@unittest.skipUnless(_make_available(), "make is required to run the installer")
class InstallSideEffectTests(unittest.TestCase):
    """Installing must never amount to activating."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "CWK"
        self.project.mkdir()
        self.fixture = InstallerFixture(self.project)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.state_dir = self.project / "state" / "activation"

    def statuses(self, stdout: str) -> list[str]:
        return ACTIVATION_LINE.findall(stdout)

    def test_a_plain_install_creates_no_activation_state(self) -> None:
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_CORE_READY", result.stdout)
        self.assertEqual(self.statuses(result.stdout), ["NOT_STARTED"])
        # The directory's existence would itself be a claim about the user.
        self.assertFalse(self.state_dir.exists())
        self.assertFalse((self.project / "state").exists())

    def test_every_integration_mode_leaves_activation_untouched(self) -> None:
        """The four RT-031 modes differ in where they write, not in consent."""
        cases = [
            (("--integration", "none"), None),
            (("--integration", "host-skill"), None),
            (("--integration", "workspace-skill"), self.workspace),
            (("--integration", "router"), self.workspace),
        ]
        for args, workspace in cases:
            with self.subTest(mode=args[1]):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                project = Path(tmp.name) / "CWK"
                project.mkdir()
                fixture = InstallerFixture(project)
                argv = list(args)
                if workspace is not None:
                    space = Path(tmp.name) / "workspace"
                    space.mkdir()
                    argv += ["--workspace", str(space)]
                result = fixture.run(*argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.statuses(result.stdout), ["NOT_STARTED"])
                self.assertFalse((project / "state" / "activation").exists())

    def test_the_installer_never_runs_a_wizard_command(self) -> None:
        """Reading a status is not the same as taking a step.

        ``install.sh`` may report where activation stands, but a report that
        could advance the state machine would be an installer quietly
        confirming things on the user's behalf.
        """
        source = (PROJECT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("cwk_activation_wizard", source)
        for verb in ("init", "confirm-discovery", "confirm-activation",
                     "record-schedule", "schedule-handoff"):
            self.assertNotIn("activation_wizard.py %s" % verb, source)

    def test_an_existing_record_is_reported_and_left_byte_identical(self) -> None:
        build_state(self.state_dir, ("init",))
        path = self.state_dir / S.STATE_FILE
        before = path.read_bytes()

        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.statuses(result.stdout), ["IN_PROGRESS"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_broken_record_is_reported_without_failing_the_install(self) -> None:
        """Fail closed on the status, stay usable as an installer.

        A corrupt private record must never read as "not activated" — that is
        the one answer that would let a scheduled run look innocent. But the
        install itself has to keep working, or the user cannot reinstall their
        way out of the problem.
        """
        self.state_dir.mkdir(parents=True)
        (self.state_dir / S.STATE_FILE).write_text("{ not json", encoding="utf-8")

        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_CORE_READY", result.stdout)
        self.assertEqual(self.statuses(result.stdout), ["UNREADABLE"])
        self.assertNotIn("NOT_STARTED", result.stdout)

    def test_the_status_line_carries_nothing_but_an_enum(self) -> None:
        build_state(self.state_dir, ("init",))
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        [status] = self.statuses(result.stdout)
        self.assertIn(status.lower(), S.READINESS_STATUSES + ("unknown",))
        for line in result.stdout.splitlines():
            if line.startswith("CWK_ACTIVATION="):
                self.assertNotIn(str(self.project), line)
                self.assertNotIn("/", line)
                self.assertFalse(re.search(r"[0-9a-f]{16}", line), line)

    def test_reinstalling_does_not_advance_activation(self) -> None:
        self.fixture.run()
        second = self.fixture.run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.statuses(second.stdout), ["NOT_STARTED"])
        self.assertFalse(self.state_dir.exists())


# ── 2. the doctor ─────────────────────────────────────────────────────────


class DoctorActivationTests(unittest.TestCase):
    """The doctor relays the activation verdict; it never re-derives one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "CWK"
        (self.project / "scripts").mkdir(parents=True)
        for name in ("cwk_nightly_pipeline.py", "cwk_collect_live.py", "cwk_doctor.py"):
            (self.project / "scripts" / name).write_text("# stub\n", encoding="utf-8")
        self.state_dir = self.project / "state" / "activation"

    def check(self, name: str, result: dict) -> dict:
        for entry in result["checks"]:
            if entry["name"] == name:
                return entry
        raise AssertionError("doctor reported no %r check" % name)

    def run_doctor(self) -> dict:
        return cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False,
            project=self.project, env={},
        )

    def test_a_fresh_install_reports_not_started(self) -> None:
        result = self.run_doctor()
        entry = self.check("activation", result)
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["value"], "not_started")
        self.assertEqual(entry["activation_next_step"], "init")
        self.assertIsNone(entry["integrity_reason"])
        self.assertEqual(result["activation"]["state"], S.UNINITIALIZED)

    def test_asking_the_question_does_not_create_the_answer(self) -> None:
        self.run_doctor()
        self.assertFalse(self.state_dir.exists())
        self.assertFalse((self.project / "state").exists())

    def test_a_started_activation_reports_its_state_and_next_step(self) -> None:
        build_state(self.state_dir, ("init",))
        entry = self.check("activation", self.run_doctor())
        self.assertEqual(entry["value"], "in_progress")
        self.assertEqual(entry["activation_state"], "INSTALLED")
        self.assertEqual(entry["activation_next_step"], "confirm_discovery_scope")

    def test_a_broken_record_is_unreadable_not_absent(self) -> None:
        self.state_dir.mkdir(parents=True)
        (self.state_dir / S.STATE_FILE).write_text("{ not json", encoding="utf-8")

        result = self.run_doctor()
        entry = self.check("activation", result)
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["value"], "unreadable")
        self.assertTrue(result["activation"]["state_present"])
        self.assertFalse(result["activation"]["healthy"])
        self.assertIsNotNone(entry["integrity_reason"])

    def test_a_broken_record_warns_but_does_not_break_the_install_checks(self) -> None:
        """An error here would block the reinstall that fixes it."""
        self.state_dir.mkdir(parents=True)
        (self.state_dir / S.STATE_FILE).write_text("{ not json", encoding="utf-8")

        healthy = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False,
            project=self.project, env={},
        )
        self.assertTrue(any("activation" in text for text in healthy["warnings"]))
        self.assertNotIn("activation", " ".join(healthy["errors"]))
        self.assertTrue(self.check("project_scripts", healthy)["ok"])

    def test_the_reported_payload_carries_no_path_hash_or_content(self) -> None:
        build_state(
            self.state_dir,
            ("init",),
            ("confirm-discovery", "--scope-file", str(FIXTURES / "scope.json")),
        )
        result = self.run_doctor()
        blob = json.dumps(result["activation"], ensure_ascii=False)
        self.assertNotIn(str(self.project), blob)
        self.assertNotIn(str(FIXTURES), blob)
        self.assertNotIn("/", blob)
        self.assertIsNone(re.search(r"[0-9a-f]{32}", blob), blob)

    def test_the_doctor_does_not_re_implement_the_verdict(self) -> None:
        """One owner for the schema, or the two will drift apart."""
        source = (PROJECT / "scripts" / "cwk_doctor.py").read_text(encoding="utf-8")
        self.assertIn("import cwk_activation_state", source)
        self.assertNotIn("STATE_READINESS", source)
        for token in ("PILOT_PASSED", "NEEDS_RECONFIRMATION", "confirm_activation"):
            self.assertNotIn(token, source)

    def test_the_probe_never_imports_python_from_the_inspected_project(self) -> None:
        """A project directory is data. Importing out of it would execute it."""
        (self.project / "scripts" / "cwk_activation_state.py").write_text(
            "raise SystemExit('this must never be executed')\n", encoding="utf-8"
        )
        entry = self.check("activation", self.run_doctor())
        self.assertEqual(entry["value"], "not_started")

    def test_activation_status_uses_only_the_enumerated_vocabulary(self) -> None:
        for steps, expected in (
            ((), "not_started"),
            ((("init",),), "in_progress"),
        ):
            with self.subTest(steps=len(steps)):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                project = Path(tmp.name) / "CWK"
                (project / "scripts").mkdir(parents=True)
                build_state(project / "state" / "activation", *steps)
                payload = cwk_doctor.activation_readiness(project)
                self.assertEqual(payload["status"], expected)
                self.assertIn(payload["status"], S.READINESS_STATUSES)
                self.assertNotIn("schema", payload)


# ── 3. the documents ──────────────────────────────────────────────────────


class ActivationDialogueContractTests(unittest.TestCase):
    """The Skill drives the dialogue; these keep it from drifting off the CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (PROJECT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        cls.reference = (PROJECT / "skill" / "references" / "activation.md").read_text(
            encoding="utf-8"
        )

    def test_the_skill_points_at_the_reference_instead_of_restating_it(self) -> None:
        self.assertIn("references/activation.md", self.skill)
        # The procedure lives in one place; the Skill stays a map, not a copy.
        self.assertLess(self.skill.count("cwk_activation_wizard.py"), 4)

    def test_the_skill_separates_installing_from_authorising(self) -> None:
        self.assertIn("CWK_ACTIVATION", self.skill)
        self.assertIn("装好 ≠ 授权", self.skill)

    def test_every_next_step_token_is_explained(self) -> None:
        """An unexplained token is a state the AI would have to improvise in."""
        for token in S.NEXT_STEPS:
            with self.subTest(token=token):
                self.assertIn(token, self.reference)
        self.assertIn("init", self.reference)

    def test_every_wizard_subcommand_is_documented(self) -> None:
        source = (PROJECT / "scripts" / "cwk_activation_wizard.py").read_text(
            encoding="utf-8"
        )
        commands = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', source))
        self.assertIn("confirm-activation", commands)
        for command in sorted(commands):
            with self.subTest(command=command):
                self.assertIn(command, self.reference)

    def test_the_reference_states_the_four_red_lines(self) -> None:
        lowered = self.reference.lower()
        for phrase in (
            "never collect a credential",
            "never treat your current tool access as authorization",
            "never show raw evidence",
            "never create, modify, or delete a scheduled task",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_the_two_gates_are_described_as_separate_questions(self) -> None:
        self.assertIn("confirm-discovery", self.reference)
        self.assertIn("confirm-activation", self.reference)
        self.assertIn("Do not roll this into the profile confirmation", self.reference)

    def test_the_reference_never_invents_a_scheduling_api(self) -> None:
        lowered = self.reference.lower()
        self.assertNotIn("openclaw cron", lowered)
        self.assertNotIn("openclaw schedule ", lowered)
        self.assertIn("the host creates it", lowered)

    def test_the_reference_never_teaches_a_credential_dump(self) -> None:
        for command in ("source .env", "cat .env", "echo $CWORK_APP_KEY"):
            with self.subTest(command=command):
                self.assertNotIn(command, self.reference)

    def test_the_unhealthy_state_is_not_repaired_by_starting_over(self) -> None:
        self.assertIn("healthy: false", self.reference)
        self.assertIn("do not re-`init` over it", self.reference)


class UserPathCoherenceTests(unittest.TestCase):
    """One path, told the same way wherever a user enters it."""

    ONBOARDING = (
        Path("README.md"),
        Path("docs") / "INTERNAL_DISTRIBUTION.md",
        Path("docs") / "SANDBOX_ONBOARDING.md",
    )

    def read(self, relative: Path) -> str:
        return (PROJECT / relative).read_text(encoding="utf-8")

    def test_each_entry_point_says_install_is_not_activation(self) -> None:
        for relative in self.ONBOARDING:
            with self.subTest(doc=str(relative)):
                text = self.read(relative)
                self.assertIn("CWK_ACTIVATION=NOT_STARTED", text)

    def test_each_entry_point_hands_off_to_the_one_reference(self) -> None:
        for relative in self.ONBOARDING:
            with self.subTest(doc=str(relative)):
                self.assertIn("activation.md", self.read(relative))

    def test_the_bootstrap_prompt_forbids_creating_a_scheduled_task(self) -> None:
        text = self.read(Path("prompts") / "OPENCLAW_SANDBOX_BOOTSTRAP.md")
        self.assertIn("不创建、不修改、不删除任何定时任务", text)
        self.assertIn("不把“你现在能调用某个工具”当成我已经授权", text)
        self.assertIn("activation.md", text)

    def test_the_four_install_modes_survive_the_new_step(self) -> None:
        """RT-031's contract is a baseline, not something activation may edit."""
        for relative in (Path("README.md"), Path("docs") / "INTERNAL_DISTRIBUTION.md"):
            with self.subTest(doc=str(relative)):
                text = self.read(relative)
                for mode in ("workspace-skill", "host-skill", "router", "none"):
                    self.assertIn(mode, text)
        sandbox = self.read(Path("docs") / "SANDBOX_ONBOARDING.md")
        self.assertIn("AGENTS_ROUTER_ACTIVATION=NEXT_SESSION", sandbox)
        self.assertIn("OPENCLAW_DISCOVERY=UNVERIFIED", sandbox)

    def test_the_operations_doc_treats_an_invalid_record_as_untrusted(self) -> None:
        text = self.read(Path("docs") / "OPERATIONS.md")
        self.assertIn("check-drift", text)
        self.assertIn("without valid consent", text)

    def test_migration_refuses_to_carry_someone_elses_consent(self) -> None:
        for relative in (
            Path("docs") / "MIGRATION.md",
            Path("skill") / "references" / "migration.md",
        ):
            with self.subTest(doc=str(relative)):
                text = self.read(relative)
                self.assertIn("state/activation", text)
                self.assertIn("NOT_STARTED", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
