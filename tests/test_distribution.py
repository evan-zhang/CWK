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
