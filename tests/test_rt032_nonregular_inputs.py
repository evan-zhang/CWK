"""RT-032 BL-2: no activation read path may block on a non-regular file.

Every file this feature reads sits at a name that something else can replace:
the private state file, the lock, the artifacts the wizard writes and reads
back, and the input files a caller names on the command line. Replace any of
those names with a FIFO and a plain ``open(name, O_RDONLY)`` waits for a writer
that will never arrive. The process does not crash and does not return. It just
stops.

That failure mode is worse than an error, and the reason is who the callers
are. ``install.sh`` runs the readiness probe in a command substitution;
``cwk_doctor.py`` runs it inside install checks; the Skill runs the wizard and
waits for one JSON object. None of them has a timeout. A hang there is not a
red line in someone's terminal, it is an install that never finishes, and
nobody reads a log for a thing that did not fail.

So the property under test is not "the wrong file is rejected" — the older
``is_file()`` guards already did that on a quiet disk. It is **liveness**:
every one of these entry points returns, promptly, on every kind of thing that
is not a regular file. That cannot be tested in-process, because a test that
hangs is indistinguishable from a test suite that is merely slow, and the
assertion would never run. So each check is a real subprocess with a real
timeout, and the timeout expiring *is* the failure.

Two supporting properties ride along, because a fix that traded a hang for a
leak or for a corrupted record would be no fix:

* the refusal must stay redacted — no absolute path, no errno, no traceback;
* the probe paths must stay non-fatal and must not write anything.

Nothing here touches real data. The FIFOs, sockets and symlinks are created in
a temp directory and unlinked by name; no test ever opens one, so the cleanup
cannot hang either.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import activation_state as S  # noqa: E402
import activation_wizard as W  # noqa: E402
from test_install_modes import InstallerFixture, _make_available  # noqa: E402

FIXTURES = PROJECT / "tests" / "fixtures" / "activation"

# Generous on purpose. The point is to separate "returns" from "never
# returns", not to measure speed; a loaded CI box must not be able to turn a
# correct implementation red. A blocked ``open`` waits forever, so any finite
# budget distinguishes the two, and a large one only makes the failure slower
# to report.
TIMEOUT = 60.0

# A private state dir is 0700 and holds exactly these names. Everything the
# activation code opens by name under it is one of them or an artifact.
STATE_NAMES = (S.STATE_FILE, S.LOCK_FILE)
ARTIFACT_NAMES = (
    W.DISCOVERY_REPORT_FILE,
    W.EXECUTION_CONTRACT_FILE,
    W.PILOT_RECEIPT_FILE,
    W.SCHEDULER_HANDOFF_FILE,
)


# ── planting non-regular things ───────────────────────────────────────────


def plant_fifo(target: Path) -> None:
    os.mkfifo(target, 0o600)


def plant_directory(target: Path) -> None:
    target.mkdir(mode=0o700)


def plant_socket(target: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(target))
    finally:
        # Closing the socket object leaves the filesystem entry, which is the
        # part under test. Nothing ever connects to it.
        sock.close()


def plant_symlink_to_fifo(target: Path) -> None:
    elsewhere = target.parent / (target.name + ".pipe")
    os.mkfifo(elsewhere, 0o600)
    os.symlink(str(elsewhere), str(target))


def plant_dangling_symlink(target: Path) -> None:
    os.symlink(str(target.parent / "no-such-thing"), str(target))


def plant_symlink_to_regular(target: Path) -> None:
    elsewhere = target.parent / (target.name + ".real")
    elsewhere.write_text("{}\n", encoding="utf-8")
    os.symlink(str(elsewhere), str(target))


def plant_symlink_to_device(target: Path) -> None:
    # /dev/zero never ends. A reader that follows this and does not check what
    # it opened does not hang on ``open``; it hangs on ``read``, filling memory
    # on the way. Same liveness bug, different system call.
    os.symlink("/dev/zero", str(target))


def plant_hardlink(target: Path) -> None:
    elsewhere = target.parent / (target.name + ".other")
    elsewhere.write_text("{}\n", encoding="utf-8")
    os.link(str(elsewhere), str(target))


#: Name → planter. Used to parametrise; the *expected* verdict differs between
#: the private state dir (where a symlink or a second hard link is itself
#: evidence of tampering) and a caller-named input file (where it is the
#: user's own business), so each test says which it means.
PLANTERS = {
    "fifo": plant_fifo,
    "directory": plant_directory,
    "socket": plant_socket,
    "symlink_to_fifo": plant_symlink_to_fifo,
    "dangling_symlink": plant_dangling_symlink,
    "symlink_to_device": plant_symlink_to_device,
    "hardlink": plant_hardlink,
}

#: The subset that is unacceptable *anywhere*, including at a path the caller
#: chose. A symlink to a regular file and a second hard link are absent on
#: purpose: those are legitimate for caller inputs.
NEVER_ACCEPTABLE = (
    "fifo",
    "directory",
    "socket",
    "symlink_to_fifo",
    "dangling_symlink",
    "symlink_to_device",
)


def remove_planted(target: Path) -> None:
    """Unlink by name, never by opening.

    ``shutil.rmtree`` on a directory containing a FIFO is safe for the same
    reason: it unlinks entries, it does not open them. Spelling it out because
    a cleanup routine that opened these would hang the suite in teardown, where
    it would look like an unrelated flake.
    """
    if target.is_symlink() or not target.exists():
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    if target.is_dir():
        shutil.rmtree(target)
    else:
        os.unlink(target)


# ── running things with a deadline ────────────────────────────────────────


class DeadlineMixin:
    """Every subprocess in this file goes through here, so none can hang."""

    def run_deadline(self, command, *, cwd=None, env_extra=None, label=""):
        env = dict(os.environ)
        # Clear anything that could make a probe read a real project, a real
        # credential, or a real mirror. These tests must be identical on a
        # developer laptop and on a bare box.
        for name in (
            "CWK_PROJECT_DIR", "CWK_WORKSPACE_DIR", "CWK_SKILL_ROOTS",
            "OPENCLAW_SKILLS_DIR", "CMS_CWORK_WORKFLOW_DIR",
            "CMS_AUTH_SKILL_DIR", "CMS_DOCDB_SKILL_DIR", "CMS_AUTH_LOGIN",
            "CWORK_APP_KEY", "XG_BIZ_API_KEY", "XG_APP_KEY", "CWK_SENDER_ID",
            "CWK_MIRROR_ROOT", "CWK_SYNC_DOCDB", "CWK_HISTORY_RUN_NAME",
        ):
            env.pop(name, None)
        if env_extra:
            env.update(env_extra)
        try:
            return subprocess.run(
                command,
                cwd=None if cwd is None else str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # ``subprocess.run`` has already killed the child. Fail loudly and
            # name the thing that blocked, because "the suite got slower" is
            # exactly the reading this test exists to prevent.
            self.fail(
                "%s did not return within %.0fs -- an activation read path is "
                "blocking on a non-regular file" % (label or command, TIMEOUT)
            )

    def assert_clean_refusal(self, result, *, secrets=(), label=""):
        """A refusal may say no. It may not say where, why at errno level, or how."""
        blob = result.stdout + result.stderr
        self.assertNotIn("Traceback (most recent call last)", blob, label)
        for secret in secrets:
            self.assertNotIn(str(secret), blob, "%s leaked a path" % label)
        for token in ("ENXIO", "ELOOP", "EAGAIN", "Errno", "errno"):
            self.assertNotIn(token, blob, "%s leaked an errno" % label)


# ── 1. the read-only probe: install.sh and the doctor ─────────────────────


READINESS_SNIPPET = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import activation_state as activation
print(json.dumps(activation.readiness(Path(sys.argv[2]))))
"""


class ReadinessProbeLivenessTests(DeadlineMixin, unittest.TestCase):
    """``readiness()`` is the one function install and doctor both depend on."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state" / "activation"
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.addCleanup(self.tmp.cleanup)

    def probe(self, state_dir: Path, label: str):
        return self.run_deadline(
            [sys.executable, "-c", READINESS_SNIPPET, str(PROJECT / "scripts"), str(state_dir)],
            label=label,
        )

    def test_the_probe_returns_on_every_kind_of_broken_state_file(self) -> None:
        for name, plant in PLANTERS.items():
            with self.subTest(planted=name):
                target = self.state_dir / S.STATE_FILE
                remove_planted(target)
                plant(target)
                result = self.probe(self.state_dir, "readiness with %s state file" % name)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                # Fail closed. Never ``not_started``: that is the one answer
                # that would let a caller conclude nothing was ever authorised.
                self.assertEqual(payload["status"], "unreadable", name)
                self.assertFalse(payload["healthy"], name)
                self.assert_clean_refusal(result, secrets=(self.root,), label=name)

    def test_the_probe_returns_when_the_state_dir_itself_is_replaced(self) -> None:
        for name, plant in PLANTERS.items():
            if name == "directory":
                continue  # a directory there is the normal case, not a probe
            with self.subTest(planted=name):
                victim = self.root / "state" / "activation"
                remove_planted(victim)
                plant(victim)
                result = self.probe(victim, "readiness with %s state dir" % name)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "unreadable", name)
                self.assertFalse(payload["healthy"], name)
        remove_planted(self.root / "state" / "activation")
        self.state_dir.mkdir(parents=True, mode=0o700)

    def test_a_fifo_state_file_leaves_the_probe_writing_nothing(self) -> None:
        """A read-only probe stays read-only even when it refuses."""
        target = self.state_dir / S.STATE_FILE
        plant_fifo(target)
        before = sorted(p.name for p in self.state_dir.iterdir())
        self.probe(self.state_dir, "readiness write check")
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), before)


class DoctorLivenessTests(DeadlineMixin, unittest.TestCase):
    """The doctor relays the probe. It must relay a refusal, not inherit a hang."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state" / "activation").mkdir(parents=True, mode=0o700)
        self.addCleanup(self.tmp.cleanup)

    def test_the_doctor_reports_and_returns_on_a_fifo_state_file(self) -> None:
        for name in NEVER_ACCEPTABLE:
            with self.subTest(planted=name):
                target = self.root / "state" / "activation" / S.STATE_FILE
                remove_planted(target)
                PLANTERS[name](target)
                result = self.run_deadline(
                    [
                        sys.executable, str(PROJECT / "scripts" / "cwk_doctor.py"),
                        "--project-dir", str(self.root), "--check-only", "--json",
                    ],
                    label="doctor with %s state file" % name,
                )
                blob = result.stdout + result.stderr
                self.assertNotIn("Traceback (most recent call last)", blob, name)
                self.assertIn("activation", blob, name)

    def test_the_doctor_returns_when_the_state_dir_is_a_fifo(self) -> None:
        remove_planted(self.root / "state" / "activation")
        plant_fifo(self.root / "state" / "activation")
        result = self.run_deadline(
            [
                sys.executable, str(PROJECT / "scripts" / "cwk_doctor.py"),
                "--project-dir", str(self.root), "--check-only", "--json",
            ],
            label="doctor with fifo state dir",
        )
        self.assertNotIn("Traceback (most recent call last)", result.stdout + result.stderr)


@unittest.skipUnless(_make_available(), "make is required to run the installer")
class InstallerLivenessTests(DeadlineMixin, unittest.TestCase):
    """The real installer, not a re-implementation of its probe.

    ``install.sh`` calls readiness inside ``$(...)``. A command substitution
    that never returns hangs the shell with no output at all, so this is the
    one place where the difference between "rejected" and "blocked" is
    invisible to every other test in the repo.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = InstallerFixture(self.root)
        (self.root / "state" / "activation").mkdir(parents=True, mode=0o700)
        self.addCleanup(self.tmp.cleanup)

    def run_installer(self, label: str):
        return self.run_deadline(
            ["bash", str(self.root / "install.sh")],
            cwd=self.root,
            env_extra={"PYTHON": sys.executable},
            label=label,
        )

    def test_the_installer_finishes_and_stays_non_fatal_on_every_planted_shape(self) -> None:
        for name in NEVER_ACCEPTABLE:
            with self.subTest(planted=name):
                target = self.root / "state" / "activation" / S.STATE_FILE
                remove_planted(target)
                PLANTERS[name](target)
                result = self.run_installer("install.sh with %s state file" % name)
                self.assertEqual(result.returncode, 0, "install must stay non-fatal: " + result.stderr)
                self.assertIn("CWK_ACTIVATION=", result.stdout, name)
                # Never NOT_STARTED: the installer must not tell a user that no
                # activation exists when something is sitting on the record.
                self.assertNotIn("CWK_ACTIVATION=NOT_STARTED", result.stdout, name)
                self.assertNotIn("Traceback (most recent call last)", result.stdout + result.stderr)

    def test_the_installer_finishes_when_the_state_dir_is_a_fifo(self) -> None:
        remove_planted(self.root / "state" / "activation")
        plant_fifo(self.root / "state" / "activation")
        result = self.run_installer("install.sh with fifo state dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_ACTIVATION=", result.stdout)
        self.assertNotIn("CWK_ACTIVATION=NOT_STARTED", result.stdout)


# ── 2. the wizard: reads, writes, and the lock ────────────────────────────


class WizardLivenessTests(DeadlineMixin, unittest.TestCase):
    """Read commands, write commands, and the lock they all take first."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state" / "activation"
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.addCleanup(self.tmp.cleanup)

    def wizard(self, *args: str, label: str = ""):
        return self.run_deadline(
            [
                sys.executable, str(PROJECT / "scripts" / "activation_wizard.py"),
                "--state-dir", str(self.state_dir), *args,
            ],
            label=label or " ".join(args),
        )

    def assert_wizard_refused(self, result, name: str) -> None:
        self.assertNotEqual(result.returncode, 0, "%s must not be accepted" % name)
        # A crash is not a refusal. The CLI contract is one JSON object on
        # stdout and one of the documented exit codes.
        self.assertIn(result.returncode, (W.EXIT_USAGE, W.EXIT_REFUSED), name)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"], name)
        self.assert_clean_refusal(result, secrets=(self.root, PROJECT), label=name)

    def test_status_returns_on_every_planted_state_file(self) -> None:
        for name, plant in PLANTERS.items():
            with self.subTest(planted=name):
                target = self.state_dir / S.STATE_FILE
                remove_planted(target)
                plant(target)
                result = self.wizard("status", label="status with %s" % name)
                self.assert_wizard_refused(result, name)

    def test_a_write_command_returns_on_every_planted_state_file(self) -> None:
        """``init`` writes. It must refuse to write *over* an unknown object."""
        for name, plant in PLANTERS.items():
            with self.subTest(planted=name):
                target = self.state_dir / S.STATE_FILE
                remove_planted(target)
                plant(target)
                result = self.wizard("init", label="init with %s" % name)
                self.assert_wizard_refused(result, name)

    def test_taking_the_lock_returns_on_every_planted_lock_file(self) -> None:
        """The lock is taken before anything else, so a hang here hangs all commands."""
        for name, plant in PLANTERS.items():
            if name == "hardlink":
                continue  # the lock file is created by us; a second link is the normal open case
            with self.subTest(planted=name):
                target = self.state_dir / S.LOCK_FILE
                remove_planted(target)
                plant(target)
                result = self.wizard("status", label="lock is %s" % name)
                self.assert_wizard_refused(result, name)

    def test_the_state_dir_itself_can_be_replaced_without_hanging(self) -> None:
        for name in NEVER_ACCEPTABLE:
            if name == "directory":
                continue  # an empty directory there is a fresh install, not an attack
            with self.subTest(planted=name):
                remove_planted(self.state_dir)
                PLANTERS[name](self.state_dir)
                result = self.wizard("status", label="state dir is %s" % name)
                self.assert_wizard_refused(result, name)
        remove_planted(self.state_dir)
        self.state_dir.mkdir(parents=True, mode=0o700)


class ArtifactReadLivenessTests(DeadlineMixin, unittest.TestCase):
    """The wizard writes artifacts and reads them back. Both directions count.

    ``schedule-handoff`` reads the execution contract back and checks it
    against the hash the second consent gate is bound to; ``record-schedule``
    reads the handoff. Those reads happen *after* a real walk, holding the
    lock, so a hang here would strand a half-finished activation — consent
    already given, nothing scheduled, and no error anyone will see.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state" / "activation"
        self.addCleanup(self.tmp.cleanup)
        self.clock = 0
        self.walk_to_pilot()

    def tick(self) -> str:
        self.clock += 1
        return "2026-01-01T00:%02d:%02dZ" % (self.clock // 60, self.clock % 60)

    def cli(self, *args: str) -> int:
        with redirect_stdout(io.StringIO()):
            return W.main(["--state-dir", str(self.state_dir), "--now", self.tick(), *args])

    def fx(self, name: str) -> str:
        return str(FIXTURES / name)

    def walk_to_pilot(self) -> None:
        """A genuine record, produced by the same entry point a user drives."""
        self.cli("init")
        self.cli("confirm-discovery", "--scope-file", self.fx("scope.json"))
        self.cli(
            "record-discovery",
            "--scope-file", self.fx("scope.json"),
            "--collect-manifest", self.fx("collect-manifest.json"),
            "--nightly-manifest", self.fx("nightly-manifest.json"),
            "--acceptance", self.fx("acceptance.json"),
            "--entity-catalog", self.fx("entity-catalog.json"),
            "--entity-registry", self.fx("entity-registry.json"),
        )
        self.cli("propose-profile", "--profile-file", self.fx("profile.json"))
        self.cli("confirm-profile")
        self.cli(
            "record-pilot",
            "--config", self.fx("config.json"),
            "--nightly-manifest", self.fx("nightly-manifest.json"),
            "--acceptance", self.fx("acceptance.json"),
            "--collect-manifest", self.fx("collect-manifest.json"),
        )

    def wizard(self, *args: str, label: str = ""):
        return self.run_deadline(
            [
                sys.executable, str(PROJECT / "scripts" / "activation_wizard.py"),
                "--state-dir", str(self.state_dir), "--now", self.tick(), *args,
            ],
            label=label or " ".join(args),
        )

    def test_schedule_handoff_returns_on_every_planted_contract(self) -> None:
        self.cli("confirm-activation")
        for name in NEVER_ACCEPTABLE + ("hardlink",):
            with self.subTest(planted=name):
                target = self.state_dir / W.EXECUTION_CONTRACT_FILE
                remove_planted(target)
                PLANTERS[name](target)
                result = self.wizard(
                    "schedule-handoff", "--config", self.fx("config.json"),
                    label="contract is %s" % name,
                )
                self.assertNotEqual(result.returncode, 0, name)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"], name)
                self.assert_clean_refusal(result, secrets=(self.root, PROJECT), label=name)

    def test_a_symlinked_contract_is_refused_even_when_the_bytes_are_right(self) -> None:
        """The decoy has the *same content*, so only a no-follow read stops it.

        The hash check on the next line would pass. That is the point:
        containment is a separate property from integrity, and this artifact is
        what the scheduling consent is bound to — if a link can stand in for
        it, the binding proves nothing about which file the scheduler gets.
        """
        self.cli("confirm-activation")
        target = self.state_dir / W.EXECUTION_CONTRACT_FILE
        decoy = self.root / "decoy-contract.json"
        decoy.write_bytes(target.read_bytes())
        os.unlink(target)
        os.symlink(str(decoy), str(target))
        result = self.wizard(
            "schedule-handoff", "--config", self.fx("config.json"), label="symlinked contract",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_record_schedule_returns_on_a_planted_handoff(self) -> None:
        self.cli("confirm-activation")
        self.cli("schedule-handoff", "--config", self.fx("config.json"))
        for name in NEVER_ACCEPTABLE:
            with self.subTest(planted=name):
                target = self.state_dir / W.SCHEDULER_HANDOFF_FILE
                remove_planted(target)
                PLANTERS[name](target)
                result = self.wizard(
                    "record-schedule",
                    "--external-system", "openclaw",
                    "--external-task-id", "host-task-1",
                    label="handoff is %s" % name,
                )
                self.assertNotEqual(result.returncode, 0, name)
                self.assertFalse(json.loads(result.stdout)["ok"], name)
                self.assert_clean_refusal(result, secrets=(self.root, PROJECT), label=name)

    def test_a_refused_artifact_read_does_not_advance_the_record(self) -> None:
        """Liveness must not have been bought with a partial transition."""
        self.cli("confirm-activation")
        before = (self.state_dir / S.STATE_FILE).read_bytes()
        target = self.state_dir / W.EXECUTION_CONTRACT_FILE
        remove_planted(target)
        plant_fifo(target)
        self.wizard(
            "schedule-handoff", "--config", self.fx("config.json"),
            label="fifo contract, no-write check",
        )
        self.assertEqual((self.state_dir / S.STATE_FILE).read_bytes(), before)


# ── 3. caller-named input files ───────────────────────────────────────────


class CallerInputLivenessTests(DeadlineMixin, unittest.TestCase):
    """``--config``, ``--scope-file`` and friends name paths we do not own.

    These have a different rule from the private state dir. A user may keep
    their own config behind a symlink, or hard-linked into a dotfiles repo;
    refusing that would be us overreaching. What is never acceptable is a thing
    that is not a regular file — and specifically, a thing that can block.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state" / "activation"
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def wizard(self, *args: str, label: str = ""):
        return self.run_deadline(
            [
                sys.executable, str(PROJECT / "scripts" / "activation_wizard.py"),
                "--state-dir", str(self.state_dir), *args,
            ],
            label=label or " ".join(args),
        )

    def test_a_scope_file_that_is_not_a_regular_file_is_refused_promptly(self) -> None:
        self.wizard("init")
        for name in NEVER_ACCEPTABLE:
            with self.subTest(planted=name):
                target = self.inputs / "scope.json"
                remove_planted(target)
                PLANTERS[name](target)
                result = self.wizard(
                    "confirm-discovery", "--scope-file", str(target),
                    label="--scope-file is %s" % name,
                )
                self.assertEqual(result.returncode, W.EXIT_USAGE, name)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"], name)
                self.assert_clean_refusal(result, secrets=(PROJECT,), label=name)

    def test_a_config_that_is_a_device_does_not_read_forever(self) -> None:
        """/dev/zero would supply bytes until memory ran out. It must not be read."""
        target = self.inputs / "config.json"
        plant_symlink_to_device(target)
        self.wizard("init")
        result = self.wizard(
            "record-pilot", "--config", str(target),
            "--nightly-manifest", str(FIXTURES / "nightly-manifest.json"),
            "--acceptance", str(FIXTURES / "acceptance.json"),
            "--collect-manifest", str(FIXTURES / "collect-manifest.json"),
            label="--config is /dev/zero",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_a_symlinked_input_the_user_owns_is_still_accepted(self) -> None:
        """The fix must not have been a blanket ban on links.

        Tightening a caller-named path to no-follow would be a silent
        compatibility break dressed up as hardening, so it is pinned as a
        requirement rather than left to whoever edits the reader next.
        """
        target = self.inputs / "scope.json"
        os.symlink(str(FIXTURES / "scope.json"), str(target))
        self.wizard("init")
        result = self.wizard(
            "confirm-discovery", "--scope-file", str(target), label="symlinked scope",
        )
        self.assertEqual(result.returncode, W.EXIT_OK, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_a_hardlinked_input_the_user_owns_is_still_accepted(self) -> None:
        target = self.inputs / "scope.json"
        os.link(str(FIXTURES / "scope.json"), str(target))
        self.wizard("init")
        result = self.wizard(
            "confirm-discovery", "--scope-file", str(target), label="hardlinked scope",
        )
        self.assertEqual(result.returncode, W.EXIT_OK, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])


# ── 3b. the project root's own .env ───────────────────────────────────────


PROJECT_ENV_SNIPPET = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import activation_contract as contract
try:
    layer = contract.read_project_env(Path(sys.argv[2]))
except contract.ProjectEnvironmentError as exc:
    print(json.dumps({"refused": True, "message": str(exc)}))
else:
    print(json.dumps({"refused": False, "present": layer.present, "count": len(layer.values)}))
"""


class ProjectEnvLivenessTests(DeadlineMixin, unittest.TestCase):
    """``.env`` is read on every contract build, and it is not a caller's file.

    Nobody names it on the command line, so nobody chooses it — the wizard goes
    to a fixed path in the project root because that is where the nightly
    process goes. A name the wizard must open unprompted is the worst place for
    a blocking read: ``render-contract``, ``record-pilot``, ``check-drift`` and
    ``schedule-handoff`` would all stop returning, and a wizard that never
    answers looks exactly like a wizard that is thinking.

    Same rule as everywhere else in this file: a real subprocess with a real
    deadline, and the deadline expiring *is* the failure.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.target = self.root / ".env"

    def probe(self, label: str):
        return self.run_deadline(
            [sys.executable, "-c", PROJECT_ENV_SNIPPET, str(PROJECT / "scripts"), str(self.root)],
            label=label,
        )

    def test_the_read_returns_on_every_planted_shape(self) -> None:
        for name, plant in PLANTERS.items():
            with self.subTest(planted=name):
                remove_planted(self.target)
                plant(self.target)
                result = self.probe("read_project_env with a %s .env" % name)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                if name in NEVER_ACCEPTABLE and name != "dangling_symlink":
                    self.assertTrue(payload["refused"], name)
                self.assert_clean_refusal(
                    result, secrets=(self.root, self.target), label=name,
                )

    def test_a_dangling_symlink_reads_as_absent_not_as_a_refusal(self) -> None:
        """Upstream's ``path.exists()`` is false there, so it simply returns."""

        remove_planted(self.target)
        plant_dangling_symlink(self.target)
        payload = json.loads(self.probe("dangling .env").stdout)
        self.assertEqual(payload, {"refused": False, "present": False, "count": 0})

    def test_an_absent_file_is_not_an_error(self) -> None:
        payload = json.loads(self.probe("absent .env").stdout)
        self.assertEqual(payload, {"refused": False, "present": False, "count": 0})

    def test_a_regular_file_is_read_and_nothing_in_it_is_echoed(self) -> None:
        self.target.write_text(
            "CWK_SYNC_DOCDB=1\nOPENAI_API_KEY=sk-not-a-real-key\n", encoding="utf-8"
        )
        result = self.probe("regular .env")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"refused": False, "present": True, "count": 2})
        self.assertNotIn("sk-not-a-real-key", result.stdout + result.stderr)

    def test_a_refusal_names_neither_the_path_nor_the_contents(self) -> None:
        self.target.write_bytes(b"CWORK_APP_KEY=sk-not-a-real-key\n\xff\xfe\n")
        result = self.probe("undecodable .env")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["refused"])
        self.assertNotIn("sk-not-a-real-key", payload["message"])
        self.assertNotIn(str(self.root), payload["message"])
        self.assert_clean_refusal(result, secrets=(self.root, self.target), label="undecodable")


# ── 4. the reader itself, isolated ────────────────────────────────────────


class ReadRegularPathUnitTests(unittest.TestCase):
    """The primitive under all of the above, checked without a subprocess.

    These run in-process on purpose: they exercise the *decision*, not the
    liveness. A FIFO is safe to open here only because ``read_regular_path``
    passes ``O_NONBLOCK``; if that were ever removed, this test would hang and
    the subprocess tests above would fail. Both signals point at the same line.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_regular_file_reads_back_byte_for_byte(self) -> None:
        target = self.root / "plain.json"
        target.write_bytes(b'{"a": 1}\n')
        self.assertEqual(S.read_regular_path(target), b'{"a": 1}\n')

    def test_every_non_regular_shape_is_refused_and_none_of_them_block(self) -> None:
        for name in NEVER_ACCEPTABLE:
            with self.subTest(planted=name):
                target = self.root / ("probe-" + name)
                remove_planted(target)
                PLANTERS[name](target)
                if name == "dangling_symlink":
                    with self.assertRaises(FileNotFoundError):
                        S.read_regular_path(target)
                else:
                    with self.assertRaises(S.ContainmentError):
                        S.read_regular_path(target)

    def test_it_follows_a_symlink_to_a_regular_file(self) -> None:
        real = self.root / "real.json"
        real.write_bytes(b"{}\n")
        link = self.root / "link.json"
        os.symlink(str(real), str(link))
        self.assertEqual(S.read_regular_path(link), b"{}\n")

    def test_it_accepts_a_second_hard_link_that_the_caller_owns(self) -> None:
        real = self.root / "real.json"
        real.write_bytes(b"{}\n")
        other = self.root / "other.json"
        os.link(str(real), str(other))
        self.assertEqual(S.read_regular_path(other), b"{}\n")

    def test_an_oversized_input_is_refused_rather_than_buffered(self) -> None:
        target = self.root / "huge.json"
        with open(target, "wb") as handle:
            handle.truncate(S.MAX_ACTIVATION_FILE_BYTES + 1)
        with self.assertRaises(S.ContainmentError):
            S.read_regular_path(target)

    def test_the_refusal_message_never_names_the_path(self) -> None:
        target = self.root / "secret-name.json"
        plant_fifo(target)
        with self.assertRaises(S.ContainmentError) as caught:
            S.read_regular_path(target)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn("secret-name", str(caught.exception))

    def test_the_reader_leaves_no_descriptor_behind_on_refusal(self) -> None:
        """A refusal path that leaked fds would exhaust the process instead.

        Cheap to check and easy to regress: every ``raise`` between the open
        and the ``finally`` has to close first, and there are several.
        """
        target = self.root / "leaky"
        for name in NEVER_ACCEPTABLE:
            remove_planted(target)
            PLANTERS[name](target)
            before = len(os.listdir("/dev/fd"))
            for _ in range(20):
                try:
                    S.read_regular_path(target)
                except (S.ContainmentError, OSError):
                    pass
            self.assertLessEqual(len(os.listdir("/dev/fd")), before + 2, name)


if __name__ == "__main__":
    unittest.main()
