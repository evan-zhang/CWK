"""RT-031: install-mode contract and doctor integration/secrecy tests.

The installer is exercised inside an isolated fixture project. install.sh does
`cd "$(dirname "$0")"`, so a copy placed in a temp directory operates entirely
on that copy: no production backdoor or skip flag is needed, and the real
doctor gate still runs. Only sanitized placeholders are used; no real CWork,
DocDB, Gateway, or credential material is touched.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_doctor  # noqa: E402


BEGIN_MARKER = "<!-- BEGIN CWK ROUTER (managed by CWK install.sh) -->"
END_MARKER = "<!-- END CWK ROUTER -->"

# Sanitized placeholder. Must never appear in any doctor output.
FAKE_KEY = "PLACEHOLDER-ck-7f3a91d2e4b6"

STUB_MAKEFILE = """.PHONY: smoke
smoke:
\t@echo "stub smoke ok"
"""


def _make_available() -> bool:
    return shutil.which("make") is not None


def _top_level_function_source(path: Path, name: str) -> str:
    """Return one top-level function's source text without importing the module."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("def %s(" % name):
            start = index
            break
    if start is None:
        raise AssertionError("%s: top-level def %s(...) not found" % (path.name, name))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace():
            end = index
            break
    return "\n".join(lines[start:end])


class InstallerFixture:
    """A minimal, self-contained copy of the project for installer runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "skill" / "templates").mkdir(parents=True, exist_ok=True)
        (root / "prompts").mkdir(parents=True, exist_ok=True)

        shutil.copy2(PROJECT / "install.sh", root / "install.sh")
        os.chmod(root / "install.sh", 0o755)
        shutil.copy2(PROJECT / "scripts" / "cwk_doctor.py", root / "scripts" / "cwk_doctor.py")
        # The doctor's package-integrity check needs the whole required trio.
        for name in ("cwk_nightly_pipeline.py", "cwk_collect_live.py"):
            (root / "scripts" / name).write_text("# sanitized stub\n", encoding="utf-8")
        # RT-032: the installer and the doctor both probe activation readiness.
        # Real modules, not stubs -- a stub would make "no side effects" vacuous.
        for name in (
            "activation_state.py",
            "cwk_atomic_file.py",
            "cwk_pr001_contracts.py",
        ):
            shutil.copy2(PROJECT / "scripts" / name, root / "scripts" / name)
        shutil.copy2(PROJECT / ".env.example", root / ".env.example")
        shutil.copy2(PROJECT / "skill" / "SKILL.md", root / "skill" / "SKILL.md")
        shutil.copy2(
            PROJECT / "skill" / "templates" / "CONFIG.example.json",
            root / "skill" / "templates" / "CONFIG.example.json",
        )
        shutil.copy2(
            PROJECT / "prompts" / "CWK_AGENTS_ROUTER.md",
            root / "prompts" / "CWK_AGENTS_ROUTER.md",
        )
        (root / "Makefile").write_text(STUB_MAKEFILE, encoding="utf-8")

    def run(
        self,
        *args: str,
        env_extra: dict | None = None,
        umask: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHON"] = sys.executable
        # Keep the fixture from picking up a real project, Workspace, Skill
        # root, or credential from the test host. Individual tests opt in only
        # through env_extra and always use sanitized placeholders.
        for name in (
            "CWK_PROJECT_DIR", "CWK_WORKSPACE_DIR", "CWK_SKILL_ROOTS",
            "OPENCLAW_SKILLS_DIR", "CMS_CWORK_WORKFLOW_DIR",
            "CMS_AUTH_SKILL_DIR", "CMS_DOCDB_SKILL_DIR", "CMS_AUTH_LOGIN",
            "CWORK_APP_KEY", "XG_BIZ_API_KEY", "XG_APP_KEY", "CWK_SENDER_ID",
            "CWK_MIRROR_ROOT",
        ):
            env.pop(name, None)
        # These tests may themselves run under `make test`. Drop the outer
        # make's jobserver state so the fixture's nested make is independent.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKE_TERMOUT", "MAKE_TERMERR"):
            env.pop(name, None)
        if env_extra:
            env.update(env_extra)
        command = ["bash", str(self.root / "install.sh"), *args]
        if umask is not None:
            # A restrictive caller umask must not make the installed public
            # Skill unreadable for the account that runs the Agent.
            command = [
                "bash", "-c", 'umask "$1"; shift; exec bash "$@"',
                "cwk-install", umask, str(self.root / "install.sh"), *args,
            ]
        return subprocess.run(
            command,
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
        )


@unittest.skipUnless(_make_available(), "make is required to run the installer")
class InstallModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "CWK"
        self.project.mkdir()
        self.fixture = InstallerFixture(self.project)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install_residue(self, *roots: Path) -> list[str]:
        """Staged artifacts that outlived the run. Any hit is a leak."""
        found: list[str] = []
        for root in roots or (self.project,):
            if not root.is_dir():
                continue
            for pattern in (".*cwk-install*", ".cwk-skill-install.*", ".*cwk-router.*"):
                found.extend(str(path) for path in root.glob(pattern))
        return sorted(found)

    # --- core install ---------------------------------------------------

    def test_default_run_is_core_only_and_touches_no_skill_root(self) -> None:
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_CORE_READY", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=NONE", result.stdout)
        self.assertNotIn("FORMAL_SKILL", result.stdout)
        self.assertFalse((self.workspace / "skills").exists())

    def test_core_install_creates_private_files_with_mode_0600(self) -> None:
        result = self.fixture.run("--integration", "none")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (".env", "cwk-mirror.local.json"):
            path = self.project / name
            self.assertTrue(path.is_file(), f"{name} was not created")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{name} has mode {oct(mode)}, expected 0o600")

    def test_existing_private_files_are_never_overwritten(self) -> None:
        env_path = self.project / ".env"
        config_path = self.project / "cwk-mirror.local.json"
        env_path.write_text("CWORK_APP_KEY=%s\n" % FAKE_KEY, encoding="utf-8")
        config_path.write_text('{"mirror_root": "knowledge/keep-me"}\n', encoding="utf-8")
        os.chmod(env_path, 0o640)

        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "CWORK_APP_KEY=%s\n" % FAKE_KEY)
        self.assertIn("keep-me", config_path.read_text(encoding="utf-8"))
        # Pre-existing permissions are the user's business; we do not rewrite them.
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o640)

    def test_install_doctor_validates_this_checkout_not_an_inherited_one(self) -> None:
        """An inherited CWK_PROJECT_DIR must not stand in for this project.

        The variable legitimately points at another valid checkout (a second
        clone, a worktree, a sandbox default). If the installer's own gate
        followed it, a broken install here would be reported as healthy because
        some other directory passed.
        """
        other = self.tmp / "other-CWK"
        (other / "scripts").mkdir(parents=True)
        for name in ("cwk_doctor.py", "cwk_nightly_pipeline.py", "cwk_collect_live.py"):
            (other / "scripts" / name).write_text("# sanitized stub\n", encoding="utf-8")
        # This checkout is the broken one.
        (self.project / "scripts" / "cwk_collect_live.py").unlink()

        result = self.fixture.run(env_extra={"CWK_PROJECT_DIR": str(other)})

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("CWK script package is incomplete", result.stdout)
        self.assertIn(str(self.project), result.stdout)
        self.assertNotIn(str(other), result.stdout)
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        self.assertFalse((self.project / ".env").exists())

    def test_missing_template_leaves_no_partial_private_file(self) -> None:
        """A failed render must not leave a 0-byte file for later runs to keep."""
        (self.project / ".env.example").unlink()

        result = self.fixture.run()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("template is missing", result.stderr)
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        # Absent, not empty: an empty .env would be preserved forever.
        self.assertFalse((self.project / ".env").exists())
        self.assertEqual(self._install_residue(), [])
        # The private file created before the failure is complete.
        config = self.project / "cwk-mirror.local.json"
        self.assertTrue(config.is_file())
        self.assertGreater(config.stat().st_size, 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_unreadable_template_leaves_no_zero_byte_target(self) -> None:
        template = self.project / ".env.example"
        os.chmod(template, 0o000)
        try:
            result = self.fixture.run()
        finally:
            os.chmod(template, 0o644)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / ".env").exists())
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        self.assertEqual(self._install_residue(), [])

    def test_dangling_private_file_symlink_is_not_followed(self) -> None:
        outside = self.tmp / "outside-env"
        env_path = self.project / ".env"
        env_path.symlink_to(outside)

        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(env_path.is_symlink())
        self.assertFalse(outside.exists())

    def test_unknown_integration_mode_is_rejected(self) -> None:
        result = self.fixture.run("--integration", "sideload")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown integration mode", result.stderr)
        self.assertNotIn("CWK_CORE_READY", result.stdout)

    def test_self_service_targets_must_stay_inside_workspace(self) -> None:
        outside = self.tmp / "outside"
        skill_result = self.fixture.run(
            "--integration", "workspace-skill",
            "--workspace", str(self.workspace),
            "--skills-dir", str(outside / "skills"),
        )
        self.assertEqual(skill_result.returncode, 3)
        self.assertIn("must stay inside workspace", skill_result.stderr)
        self.assertNotIn("CWK_CORE_READY", skill_result.stdout)
        self.assertFalse(outside.exists())

        router_result = self.fixture.run(
            "--integration", "router",
            "--workspace", str(self.workspace),
            "--agents-file", str(outside / "AGENTS.md"),
        )
        self.assertEqual(router_result.returncode, 3)
        self.assertIn("must stay inside workspace", router_result.stderr)
        self.assertNotIn("CWK_CORE_READY", router_result.stdout)
        self.assertFalse(outside.exists())

    def test_self_service_rejects_symlink_leaf_targets(self) -> None:
        outside = self.tmp / "outside-skills"
        outside.mkdir()
        (self.workspace / "skills").symlink_to(outside, target_is_directory=True)

        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("must not be a symlink", result.stderr)
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        self.assertFalse((outside / "cwk-mirror-workflow").exists())

        outside_agents = self.tmp / "outside-AGENTS.md"
        outside_agents.write_text("keep me\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").symlink_to(outside_agents)
        router_result = self.fixture.run(
            "--integration", "router", "--workspace", str(self.workspace)
        )
        self.assertEqual(router_result.returncode, 3)
        self.assertIn("must not be a symlink", router_result.stderr)
        self.assertEqual(outside_agents.read_text(encoding="utf-8"), "keep me\n")

    def test_workspace_must_not_be_the_cwk_project_root(self) -> None:
        """CWK's own checkout is not an Agent Workspace to install into."""
        for mode in ("router", "workspace-skill"):
            with self.subTest(mode=mode):
                result = self.fixture.run(
                    "--integration", mode, "--workspace", str(self.project)
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("refusing to use the CWK project root", result.stderr)
                self.assertNotIn("CWK_CORE_READY", result.stdout)
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertFalse((self.project / "skills").exists())

    def test_filesystem_root_is_not_a_self_managed_workspace(self) -> None:
        """`/` as the containment boundary would make every path 'inside'."""
        for mode in ("router", "workspace-skill"):
            with self.subTest(mode=mode):
                result = self.fixture.run("--integration", mode, "--workspace", "/")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("filesystem root", result.stderr)
                self.assertNotIn("CWK_CORE_READY", result.stdout)

    # --- one Agent, one integration mode ---------------------------------

    def test_workspace_skill_refuses_to_join_an_existing_router(self) -> None:
        original = "# My Agent\n\n%s\nrouter body\n%s\n" % (BEGIN_MARKER, END_MARKER)
        agents = self._agents_file(original)

        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION_CONFLICT", result.stdout)
        self.assertIn("CWK_EXISTING_INTEGRATION=AGENTS_ROUTER", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        # No destructive auto-switch: the existing mode is left exactly as is.
        self.assertEqual(agents.read_text(encoding="utf-8"), original)
        self.assertFalse((self.workspace / "skills" / "cwk-mirror-workflow").exists())

    def test_force_does_not_silence_the_mixed_mode_refusal(self) -> None:
        original = "# My Agent\n\n%s\nrouter body\n%s\n" % (BEGIN_MARKER, END_MARKER)
        agents = self._agents_file(original)

        result = self.fixture.run(
            "--integration", "workspace-skill",
            "--workspace", str(self.workspace), "--force",
        )

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION_CONFLICT", result.stdout)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)
        self.assertFalse((self.workspace / "skills" / "cwk-mirror-workflow").exists())

    def test_router_refuses_to_join_an_existing_formal_skill(self) -> None:
        target = self.workspace / "skills" / "cwk-mirror-workflow"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("installed skill\n", encoding="utf-8")

        result = self.fixture.run(
            "--integration", "router", "--workspace", str(self.workspace)
        )

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION_CONFLICT", result.stdout)
        self.assertIn("CWK_EXISTING_INTEGRATION=FORMAL_SKILL", result.stdout)
        self.assertNotIn("CWK_CORE_READY", result.stdout)
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "installed skill\n")

    def test_mixed_mode_probe_reads_a_non_utf8_agents_file_as_bytes(self) -> None:
        """A router block still counts when the file is not decodable text."""
        agents = self.workspace / "AGENTS.md"
        agents.write_bytes(b"# head\n\xff\xfe\n" + BEGIN_MARKER.encode() + b"\nbody\n" + END_MARKER.encode() + b"\n")

        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("CWK_EXISTING_INTEGRATION=AGENTS_ROUTER", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_reinstalling_the_same_mode_is_not_treated_as_a_conflict(self) -> None:
        first = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        again = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace), "--force"
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("OPENCLAW_INTEGRATION_CONFLICT", again.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FORMAL_SKILL", again.stdout)

    # --- host-skill ------------------------------------------------------

    def test_host_skill_hands_off_without_writing_any_skill_root(self) -> None:
        skills = self.workspace / "skills"
        skills.mkdir()
        result = self.fixture.run("--integration", "host-skill", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_CORE_READY", result.stdout)
        self.assertIn("SKILL_REGISTRATION_REQUIRES_HOST_ADMIN", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=HOST_SKILL_PENDING", result.stdout)
        self.assertIn("CWK_SKILL_SOURCE=%s" % (self.project / "skill"), result.stdout)
        self.assertIn("CWK_SKILL_SOURCE_SCOPE=CURRENT_EXECUTION_ENV", result.stdout)
        # The fixture project is outside its test Workspace, so the installer
        # must not invent a host-relative mapping.
        self.assertNotIn("CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE=", result.stdout)
        # Even though the root is writable, host mode must not write to it.
        self.assertEqual(list(skills.iterdir()), [])

    def test_host_skill_reports_sandbox_to_host_relative_mapping(self) -> None:
        result = self.fixture.run(
            "--integration", "host-skill", "--workspace", str(self.tmp)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE=CWK/skill",
            result.stdout,
        )
        self.assertIn("sandbox path, not a host path", result.stdout)

    # --- workspace-skill -------------------------------------------------

    def test_workspace_skill_copies_only_public_skill_directory(self) -> None:
        (self.project / ".env").write_text("CWORK_APP_KEY=%s\n" % FAKE_KEY, encoding="utf-8")
        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENCLAW_INTEGRATION=FORMAL_SKILL", result.stdout)

        target = self.workspace / "skills" / "cwk-mirror-workflow"
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertTrue((target / "templates" / "CONFIG.example.json").is_file())

        copied = {p.name for p in target.rglob("*")}
        self.assertNotIn(".env", copied)
        self.assertNotIn("cwk-mirror.local.json", copied)
        blob = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in target.rglob("*")
            if p.is_file()
        )
        self.assertNotIn(FAKE_KEY, blob)

    def test_workspace_skill_target_stays_readable_across_uid_boundaries(self) -> None:
        """Atomic staging must not leave a private 0700 tree behind.

        The Agent runtime that has to load this Skill may run under another
        uid, so the installed copy has to keep the public source semantics
        (0755 directories, world-readable files) even under a strict umask.
        """
        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace),
            umask="077",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        skills_root = self.workspace / "skills"
        target = skills_root / "cwk-mirror-workflow"
        # The Skill root this run created must itself be traversable.
        self.assertEqual(stat.S_IMODE(skills_root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

        entries = list(target.rglob("*"))
        self.assertTrue(entries)
        for path in entries:
            mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(path=str(path.relative_to(target))):
                if path.is_dir():
                    self.assertEqual(mode & 0o055, 0o055, "directory must stay traversable")
                else:
                    self.assertEqual(mode & 0o044, 0o044, "file must stay readable")
                self.assertEqual(mode & 0o022, 0, "must never be group/other writable")
        self.assertEqual(self._install_residue(skills_root), [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_failed_skill_copy_leaves_no_staging_directory(self) -> None:
        os.chmod(self.project / "skill" / "SKILL.md", 0o000)
        try:
            result = self.fixture.run(
                "--integration", "workspace-skill", "--workspace", str(self.workspace)
            )
        finally:
            os.chmod(self.project / "skill" / "SKILL.md", 0o644)

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("SKILL_INSTALL_FAILED", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertFalse((self.workspace / "skills" / "cwk-mirror-workflow").exists())
        self.assertEqual(self._install_residue(self.workspace / "skills"), [])

    def test_workspace_skill_does_not_claim_verified_discovery(self) -> None:
        result = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENCLAW_DISCOVERY=UNVERIFIED", result.stdout)

    def test_workspace_skill_refuses_existing_target_then_force_replaces(self) -> None:
        target = self.workspace / "skills" / "cwk-mirror-workflow"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("hand-written\n", encoding="utf-8")

        refused = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(refused.returncode, 3)
        self.assertIn("Refusing to overwrite", refused.stderr)
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "hand-written\n")

        forced = self.fixture.run(
            "--integration", "workspace-skill", "--workspace", str(self.workspace), "--force"
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("OPENCLAW_INTEGRATION=FORMAL_SKILL", forced.stdout)
        self.assertNotEqual((target / "SKILL.md").read_text(encoding="utf-8"), "hand-written\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_readonly_skill_root_keeps_core_ready_and_reports_clearly(self) -> None:
        skills = self.workspace / "skills"
        skills.mkdir()
        os.chmod(skills, 0o555)
        try:
            result = self.fixture.run(
                "--integration", "workspace-skill", "--workspace", str(self.workspace)
            )
        finally:
            os.chmod(skills, 0o755)

        self.assertEqual(result.returncode, 3)
        # The core program is installed; only the integration step failed.
        self.assertIn("CWK_CORE_READY", result.stdout)
        self.assertIn("SKILL_ROOT_NOT_WRITABLE", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertIn("host-skill", result.stderr)
        self.assertTrue((self.project / ".env").is_file())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_readonly_parent_root_is_reported_not_crashed(self) -> None:
        os.chmod(self.workspace, 0o555)
        try:
            result = self.fixture.run(
                "--integration", "workspace-skill", "--workspace", str(self.workspace)
            )
        finally:
            os.chmod(self.workspace, 0o755)
        self.assertEqual(result.returncode, 3)
        self.assertIn("SKILL_ROOT_NOT_WRITABLE", result.stdout)

    # --- router ----------------------------------------------------------

    def _agents_file(self, content: str) -> Path:
        path = self.workspace / "AGENTS.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_router_appends_block_and_preserves_unrelated_content(self) -> None:
        original = "# My Agent\n\nKeep this line.\n\n## Other section\n\nAnd this one.\n"
        agents = self._agents_file(original)

        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENCLAW_INTEGRATION=AGENTS_ROUTER", result.stdout)
        self.assertIn("AGENTS_ROUTER_ACTIVATION=NEXT_SESSION", result.stdout)
        self.assertIn("AGENTS_ROUTER_BLOCK=appended", result.stdout)

        text = agents.read_text(encoding="utf-8")
        self.assertIn("Keep this line.", text)
        self.assertIn("And this one.", text)
        self.assertIn(BEGIN_MARKER, text)
        self.assertIn(END_MARKER, text)
        self.assertIn(str(self.project / "skill" / "SKILL.md"), text)

    def test_router_is_idempotent_across_repeated_runs(self) -> None:
        agents = self._agents_file("# My Agent\n\nKeep this line.\n")
        first = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(first.returncode, 0, first.stderr)
        after_first = agents.read_text(encoding="utf-8")

        second = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(second.returncode, 0, second.stderr)
        after_second = agents.read_text(encoding="utf-8")

        self.assertEqual(after_first, after_second)
        self.assertEqual(after_second.count(BEGIN_MARKER), 1)
        self.assertEqual(after_second.count(END_MARKER), 1)
        self.assertIn("AGENTS_ROUTER_BLOCK=unchanged", second.stdout)

    def test_router_updates_a_stale_managed_block_in_place(self) -> None:
        agents = self._agents_file(
            "# Head\n\n%s\nstale routing text\n%s\n\n# Tail\n" % (BEGIN_MARKER, END_MARKER)
        )
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AGENTS_ROUTER_BLOCK=updated", result.stdout)

        text = agents.read_text(encoding="utf-8")
        self.assertNotIn("stale routing text", text)
        self.assertIn("# Head", text)
        self.assertIn("# Tail", text)
        self.assertEqual(text.count(BEGIN_MARKER), 1)

    def test_router_refuses_unbalanced_markers(self) -> None:
        original = "# Head\n\n%s\nhalf a block\n\n# Tail\n" % BEGIN_MARKER
        agents = self._agents_file(original)
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 3)
        self.assertIn("AGENTS_ROUTER_CONFLICT", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)

    def test_router_refuses_duplicate_blocks(self) -> None:
        block = "%s\nbody\n%s\n" % (BEGIN_MARKER, END_MARKER)
        original = "# Head\n\n%s\n%s\n# Tail\n" % (block, block)
        agents = self._agents_file(original)
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 3)
        self.assertIn("AGENTS_ROUTER_CONFLICT", result.stdout)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)

    def test_router_refuses_reversed_markers(self) -> None:
        original = "# Head\n%s\nbody\n%s\n# Tail\n" % (END_MARKER, BEGIN_MARKER)
        agents = self._agents_file(original)
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 3)
        self.assertIn("AGENTS_ROUTER_CONFLICT", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)

    def test_router_refuses_a_template_that_carries_its_own_markers(self) -> None:
        """The rendered block must hold exactly one marker pair before writing."""
        original = "# My Agent\n\nKeep this line.\n"
        agents = self._agents_file(original)
        (self.project / "prompts" / "CWK_AGENTS_ROUTER.md").write_text(
            "intro\n%s\nnested\n%s\n" % (BEGIN_MARKER, END_MARKER), encoding="utf-8"
        )

        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("AGENTS_ROUTER_TEMPLATE_INVALID", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(agents.read_text(encoding="utf-8"), original)
        self.assertEqual(self._install_residue(self.workspace), [])

    def test_router_fails_closed_on_a_non_utf8_agents_file(self) -> None:
        agents = self.workspace / "AGENTS.md"
        original = b"# head\n\xff\xfe not utf-8\n"
        agents.write_bytes(original)

        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("AGENTS_ROUTER_UNREADABLE", result.stdout)
        self.assertIn("OPENCLAW_INTEGRATION=FAILED", result.stdout)
        self.assertIn("not valid UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(agents.read_bytes(), original)
        self.assertEqual(self._install_residue(self.workspace), [])

    def test_router_atomic_update_preserves_existing_mode(self) -> None:
        agents = self._agents_file("# My Agent\n")
        os.chmod(agents, 0o640)
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(agents.stat().st_mode), 0o640)
        self.assertEqual(list(self.workspace.glob(".AGENTS.md.cwk-router.*")), [])

    def test_router_creates_agents_file_when_absent(self) -> None:
        agents = self.workspace / "AGENTS.md"
        self.assertFalse(agents.exists())
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(agents.is_file())
        self.assertEqual(agents.read_text(encoding="utf-8").count(BEGIN_MARKER), 1)

    def test_router_does_not_write_any_skill_root(self) -> None:
        skills = self.workspace / "skills"
        skills.mkdir()
        self._agents_file("# My Agent\n")
        result = self.fixture.run("--integration", "router", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(skills.iterdir()), [])

    # --- legacy compatibility -------------------------------------------

    def test_legacy_install_skill_still_works_and_prints_migration_path(self) -> None:
        skills = self.workspace / "legacy-skills"
        result = self.fixture.run("--install-skill", "--skills-dir", str(skills))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CWK_INSTALL_SKILL_DEPRECATED", result.stdout)
        self.assertIn("--integration workspace-skill", result.stderr)

        target = skills / "cwk-mirror-workflow"
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(self.project / "skill"))

    def test_legacy_install_skill_does_not_claim_verified_discovery(self) -> None:
        skills = self.workspace / "legacy-skills"
        result = self.fixture.run("--install-skill", "--skills-dir", str(skills))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENCLAW_DISCOVERY=UNVERIFIED", result.stdout)
        self.assertIn("does not prove", result.stdout)

    def test_legacy_install_skill_refuses_non_link_target(self) -> None:
        skills = self.workspace / "legacy-skills"
        target = skills / "cwk-mirror-workflow"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("hand-written\n", encoding="utf-8")

        result = self.fixture.run("--install-skill", "--skills-dir", str(skills))
        self.assertEqual(result.returncode, 3)
        self.assertIn("Refusing to overwrite non-link skill path", result.stderr)
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "hand-written\n")

    def test_legacy_install_skill_refuses_foreign_link(self) -> None:
        skills = self.workspace / "legacy-skills"
        skills.mkdir(parents=True)
        other = self.tmp / "other-skill"
        other.mkdir()
        target = skills / "cwk-mirror-workflow"
        target.symlink_to(other)

        result = self.fixture.run("--install-skill", "--skills-dir", str(skills))
        self.assertEqual(result.returncode, 3)
        self.assertIn("Refusing to replace an existing skill link", result.stderr)
        self.assertEqual(os.readlink(target), str(other))

    def test_legacy_flag_cannot_be_combined_with_integration(self) -> None:
        result = self.fixture.run("--install-skill", "--integration", "router")
        self.assertEqual(result.returncode, 2)
        self.assertIn("do not combine it with --integration", result.stderr)


class DoctorEnvLoadingTests(unittest.TestCase):
    """Project .env is parsed, never executed, and never disclosed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "CWK"
        (self.project / "scripts").mkdir(parents=True)
        for name in ("cwk_nightly_pipeline.py", "cwk_collect_live.py", "cwk_doctor.py"):
            (self.project / "scripts" / name).write_text("# stub\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_env(self, text: str) -> None:
        (self.project / ".env").write_text(text, encoding="utf-8")

    def test_dotenv_parsing_handles_common_shapes(self) -> None:
        parsed = cwk_doctor.parse_env_file(
            "\n".join(
                [
                    "# comment",
                    "",
                    "CWORK_APP_KEY=plain",
                    'QUOTED="double"',
                    "SINGLE='single'",
                    "export EXPORTED=value",
                    "  SPACED = spaced ",
                    "1INVALID=nope",
                    "no_equals_here",
                ]
            )
        )
        self.assertEqual(parsed["CWORK_APP_KEY"], "plain")
        self.assertEqual(parsed["QUOTED"], "double")
        self.assertEqual(parsed["SINGLE"], "single")
        self.assertEqual(parsed["SPACED"], "spaced")
        self.assertNotIn("1INVALID", parsed)
        self.assertNotIn("no_equals_here", parsed)
        # `export KEY=value` is a shell statement, not dotenv syntax. The nightly
        # runtime drops it, so doctor must drop it too (see the parity test below).
        self.assertNotIn("EXPORTED", parsed)
        self.assertNotIn("export EXPORTED", parsed)

    def test_exported_key_is_not_reported_as_configured(self) -> None:
        """Doctor must not claim readiness for a line the runtime ignores."""
        self._write_env("export CWORK_APP_KEY=%s\n" % FAKE_KEY)
        env = cwk_doctor.build_env(self.project, base_env={})
        self.assertNotIn("CWORK_APP_KEY", env)

        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        auth = next(c for c in result["checks"] if c["name"] == "live_auth_configured")
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["value"], "missing")
        self.assertNotIn(FAKE_KEY, json.dumps(result, ensure_ascii=False))

    def test_doctor_and_runtime_dotenv_parsers_stay_aligned(self) -> None:
        """Guard the parser parity by reading source text; never import or run it.

        Importing cwk_nightly_pipeline would execute a module-level
        load_local_env(PROJECT / ".env") against the real repository, so the
        parity guard only inspects text.
        """
        runtime_body = _top_level_function_source(
            PROJECT / "scripts" / "cwk_nightly_pipeline.py", "load_local_env"
        )
        self.assertNotIn(
            "export ",
            runtime_body,
            "the runtime loader gained export handling; doctor must match it",
        )

        doctor_body = _top_level_function_source(
            PROJECT / "scripts" / "cwk_doctor.py", "parse_env_file"
        )
        self.assertNotIn(
            'startswith("export ")',
            doctor_body,
            "doctor must not accept a dotenv shape the runtime rejects",
        )

    def test_dotenv_never_executes_shell(self) -> None:
        canary = self.project / "pwned"
        self._write_env(
            "CWORK_APP_KEY=$(touch %s)\nOTHER=`touch %s`\n" % (canary, canary)
        )
        env = cwk_doctor.build_env(self.project, base_env={})
        self.assertFalse(canary.exists(), "dotenv parsing must never execute shell")
        # The raw text is kept verbatim; it is a value, not a command.
        self.assertIn("touch", env["CWORK_APP_KEY"])

    def test_process_environment_wins_over_dotenv(self) -> None:
        self._write_env("CWORK_APP_KEY=from-dotenv\n")
        env = cwk_doctor.build_env(self.project, base_env={"CWORK_APP_KEY": "from-process"})
        self.assertEqual(env["CWORK_APP_KEY"], "from-process")

    def test_build_env_does_not_mutate_os_environ(self) -> None:
        self._write_env("CWK_DOCTOR_CANARY=leaked\n")
        cwk_doctor.build_env(self.project, base_env={})
        self.assertNotIn("CWK_DOCTOR_CANARY", os.environ)

    def test_dotenv_key_is_reported_as_configured_without_disclosure(self) -> None:
        self._write_env("CWORK_APP_KEY=%s\n" % FAKE_KEY)
        env = cwk_doctor.build_env(self.project, base_env={"CMS_AUTH_LOGIN": "1"})
        skill_root = Path(self._tmp.name) / "materialized"
        _install_fake_company_skills(skill_root)
        env["CWK_SKILL_ROOTS"] = str(skill_root)

        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        auth = next(c for c in result["checks"] if c["name"] == "live_auth_configured")
        self.assertTrue(auth["ok"])
        self.assertEqual(auth["value"], "configured")

        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, blob)
        # No prefix or reversible fragment either.
        for size in (6, 8, 12):
            self.assertNotIn(FAKE_KEY[:size], blob)

    def test_missing_key_is_reported_as_missing_only(self) -> None:
        self._write_env("CWK_OWNER_NAME=someone\n")
        env = cwk_doctor.build_env(self.project, base_env={})
        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        auth = next(c for c in result["checks"] if c["name"] == "live_auth_configured")
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["value"], "missing")

    def test_env_file_check_reports_presence_only(self) -> None:
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env={}
        )
        env_check = next(c for c in result["checks"] if c["name"] == "env_file")
        self.assertEqual(env_check["value"], "absent")

        self._write_env("CWORK_APP_KEY=%s\n" % FAKE_KEY)
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env={}
        )
        env_check = next(c for c in result["checks"] if c["name"] == "env_file")
        self.assertEqual(env_check["value"], "present")
        self.assertNotIn(FAKE_KEY, json.dumps(result, ensure_ascii=False))


def _install_fake_company_skills(root: Path) -> None:
    """Create sanitized marker files for the three company Skills."""
    for name, marker in cwk_doctor.COMPANY_SKILLS.items():
        target = root / name / marker
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# sanitized stub\n", encoding="utf-8")


class DoctorSkillDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "CWK"
        (self.project / "scripts").mkdir(parents=True)
        for name in ("cwk_nightly_pipeline.py", "cwk_collect_live.py", "cwk_doctor.py"):
            (self.project / "scripts" / name).write_text("# stub\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_home_slash_does_not_crash_skill_discovery(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "/"}, clear=False):
            roots = cwk_doctor.skill_roots({}, self.project)
        self.assertIn(Path("/.openclaw/skills"), roots)
        self.assertIn(Path("/.agents/skills"), roots)

    def test_current_openclaw_materialized_root_is_a_builtin_candidate(self) -> None:
        roots = cwk_doctor.skill_roots({}, self.project)
        self.assertIn(
            Path("/workspace/.openclaw/sandbox-skills/skills"),
            roots,
        )

    def test_missing_home_does_not_crash_skill_discovery(self) -> None:
        env_without_home = {k: v for k, v in os.environ.items() if k != "HOME"}
        with mock.patch.dict(os.environ, env_without_home, clear=True):
            roots = cwk_doctor.skill_roots({}, self.project)
        self.assertTrue(roots)

    def test_materialized_root_is_found_with_home_slash(self) -> None:
        materialized = self.tmp / "materialized"
        _install_fake_company_skills(materialized)
        env = {"CWK_SKILL_ROOTS": str(materialized), "CWORK_APP_KEY": FAKE_KEY}

        with mock.patch.dict(os.environ, {"HOME": "/"}, clear=False):
            result = cwk_doctor.run_checks(
                {}, require_live=True, require_docdb=True, project=self.project, env=env
            )

        self.assertTrue(result["passed"], result["errors"])
        cwork = next(c for c in result["checks"] if c["name"] == "cms_cwork_workflow")
        self.assertTrue(cwork["ok"])
        self.assertEqual(cwork["value"], str(materialized / "cms-cwork-workflow"))

    def test_workspace_skill_root_is_discovered(self) -> None:
        workspace = self.tmp / "workspace"
        _install_fake_company_skills(workspace / "skills")
        env = {"CWK_WORKSPACE_DIR": str(workspace), "CWORK_APP_KEY": FAKE_KEY}
        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        self.assertTrue(result["passed"], result["errors"])

    def test_explicit_skill_dir_override_is_authoritative(self) -> None:
        good = self.tmp / "good"
        _install_fake_company_skills(good)
        env = {
            "CWK_SKILL_ROOTS": str(good),
            "CMS_CWORK_WORKFLOW_DIR": str(self.tmp / "does-not-exist"),
            "CWORK_APP_KEY": FAKE_KEY,
        }
        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        cwork = next(c for c in result["checks"] if c["name"] == "cms_cwork_workflow")
        self.assertFalse(cwork["ok"], "an explicit override must not silently fall back")

    def test_multiple_roots_are_searched_in_order(self) -> None:
        empty = self.tmp / "empty-root"
        empty.mkdir()
        real = self.tmp / "real-root"
        _install_fake_company_skills(real)
        env = {
            "CWK_SKILL_ROOTS": os.pathsep.join([str(empty), str(real)]),
            "CWORK_APP_KEY": FAKE_KEY,
        }
        result = cwk_doctor.run_checks(
            {}, require_live=True, require_docdb=False, project=self.project, env=env
        )
        self.assertTrue(result["passed"], result["errors"])

    def test_not_found_message_carries_no_secret(self) -> None:
        # Isolate HOME and the workspace so a real company Skill on the test
        # host cannot satisfy the lookup and mask the not-found path.
        empty_home = self.tmp / "empty-home"
        empty_home.mkdir()
        env = {
            "CWORK_APP_KEY": FAKE_KEY,
            "CWK_WORKSPACE_DIR": str(empty_home),
        }
        with mock.patch.dict(os.environ, {"HOME": str(empty_home)}, clear=False):
            result = cwk_doctor.run_checks(
                {}, require_live=True, require_docdb=False, project=self.project, env=env
            )
        self.assertFalse(result["passed"])
        cwork = next(c for c in result["checks"] if c["name"] == "cms_cwork_workflow")
        self.assertFalse(cwork["ok"])
        self.assertIn("not found", cwork["value"])
        self.assertNotIn(FAKE_KEY, json.dumps(result, ensure_ascii=False))


class DoctorProjectRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_project(self, name: str) -> Path:
        root = self.tmp / name
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "cwk_doctor.py").write_text("# stub\n", encoding="utf-8")
        return root

    def test_explicit_argument_wins(self) -> None:
        root = self._fake_project("explicit")
        self.assertEqual(cwk_doctor.find_project_root(str(root)), root.resolve())

    def test_env_var_is_used_when_valid(self) -> None:
        root = self._fake_project("from-env")
        with mock.patch.dict(os.environ, {"CWK_PROJECT_DIR": str(root)}, clear=False):
            self.assertEqual(cwk_doctor.find_project_root(), root.resolve())

    def test_invalid_env_var_falls_back_to_real_package(self) -> None:
        bogus = self.tmp / "not-a-project"
        bogus.mkdir()
        with mock.patch.dict(os.environ, {"CWK_PROJECT_DIR": str(bogus)}, clear=False):
            resolved = cwk_doctor.find_project_root()
        self.assertEqual(resolved, PROJECT)

    def test_real_package_is_detected_by_default(self) -> None:
        env_without = {k: v for k, v in os.environ.items() if k != "CWK_PROJECT_DIR"}
        with mock.patch.dict(os.environ, env_without, clear=True):
            self.assertEqual(cwk_doctor.find_project_root(), PROJECT)


class DoctorIntegrationDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "CWK"
        (self.project / "scripts").mkdir(parents=True)
        for name in ("cwk_nightly_pipeline.py", "cwk_collect_live.py", "cwk_doctor.py"):
            (self.project / "scripts" / name).write_text("# stub\n", encoding="utf-8")
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _env(self) -> dict:
        return {"CWK_WORKSPACE_DIR": str(self.workspace)}

    def test_reports_none_when_nothing_is_installed(self) -> None:
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env=self._env()
        )
        check = next(c for c in result["checks"] if c["name"] == "openclaw_integration")
        self.assertEqual(check["value"], "NONE")

    def test_reports_formal_skill(self) -> None:
        target = self.workspace / "skills" / "cwk-mirror-workflow"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env=self._env()
        )
        check = next(c for c in result["checks"] if c["name"] == "openclaw_integration")
        self.assertEqual(check["value"], "FORMAL_SKILL")

    def test_reports_router(self) -> None:
        (self.workspace / "AGENTS.md").write_text(
            "# a\n%s\nbody\n%s\n" % (BEGIN_MARKER, END_MARKER), encoding="utf-8"
        )
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env=self._env()
        )
        check = next(c for c in result["checks"] if c["name"] == "openclaw_integration")
        self.assertEqual(check["value"], "AGENTS_ROUTER")

    def test_warns_when_both_integrations_are_present(self) -> None:
        target = self.workspace / "skills" / "cwk-mirror-workflow"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text(
            "# a\n%s\nbody\n%s\n" % (BEGIN_MARKER, END_MARKER), encoding="utf-8"
        )
        result = cwk_doctor.run_checks(
            {}, require_live=False, require_docdb=False, project=self.project, env=self._env()
        )
        self.assertTrue(
            any("exactly one CWK integration" in w for w in result["warnings"]),
            result["warnings"],
        )


if __name__ == "__main__":
    unittest.main()
