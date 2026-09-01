import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_doctor  # noqa: E402


class DistributionTests(unittest.TestCase):
    def test_portable_template_passes_local_doctor_checks(self):
        config = cwk_doctor.read_config(str(PROJECT / "skill" / "templates" / "CONFIG.example.json"))
        result = cwk_doctor.run_checks(config, require_live=False, require_docdb=False)
        self.assertTrue(result["passed"])
        mirror = next(item for item in result["checks"] if item["name"] == "mirror_root")
        self.assertTrue(mirror["ok"])
        self.assertNotIn("CWK-20260708-001", mirror["configured_value"])

    def test_legacy_machine_path_is_rejected(self):
        result = cwk_doctor.run_checks(
            {"mirror_root": "projects/CWK-20260708-001/knowledge/工作协同镜像"},
            require_live=False,
            require_docdb=False,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("Evan-specific" in error for error in result["errors"]))

    def test_env_template_declares_portable_workspace_hints(self):
        """RT-031: a clone outside its Agent Workspace remains discoverable."""
        values = cwk_doctor.parse_env_file(
            (PROJECT / ".env.example").read_text(encoding="utf-8")
        )
        for name in ("CWK_WORKSPACE_DIR", "CWK_SKILL_ROOTS"):
            self.assertIn(name, values)
            self.assertEqual(values[name], "")

    def test_doctor_reports_env_file_and_integration_status(self):
        """RT-031: doctor exposes a stable, non-secret integration status."""
        result = cwk_doctor.run_checks({}, require_live=False, require_docdb=False)
        names = {item["name"] for item in result["checks"]}
        self.assertIn("env_file", names)
        self.assertIn("openclaw_integration", names)

        env_check = next(i for i in result["checks"] if i["name"] == "env_file")
        self.assertIn(env_check["value"], {"present", "absent"})

        integration = next(i for i in result["checks"] if i["name"] == "openclaw_integration")
        self.assertIn(
            integration["value"], {"NONE", "FORMAL_SKILL", "AGENTS_ROUTER"}
        )

    def test_installer_exposes_the_four_explicit_integration_modes(self):
        """Core install and OpenClaw integration stay decoupled and explicit."""
        text = (PROJECT / "install.sh").read_text(encoding="utf-8")
        for mode in ("none", "host-skill", "workspace-skill", "router"):
            self.assertIn(mode, text)
        self.assertIn("CWK_CORE_READY", text)
        self.assertIn("SKILL_REGISTRATION_REQUIRES_HOST_ADMIN", text)
        self.assertIn("OPENCLAW_INTEGRATION=NONE", text)
        self.assertIn("OPENCLAW_INTEGRATION=FORMAL_SKILL", text)
        self.assertIn("OPENCLAW_INTEGRATION=AGENTS_ROUTER", text)
