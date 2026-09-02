import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "cwk_key_set.py"
FAKE_KEY = "***" + "k" * 40


def run_key_set(env_file, stdin_text):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(env_file)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=120,
    )


class KeySetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.tmp.name) / ".env"
        self.addCleanup(self.tmp.cleanup)

    def _write_env(self, data: bytes) -> None:
        self.env_file.write_bytes(data)

    def test_creates_env_from_template_when_missing(self):
        template = Path(self.tmp.name) / ".env.example"
        template.write_text("# comment\nCWORK_APP_KEY=\nOTHER=1\n", encoding="utf-8")
        result = run_key_set(self.env_file, FAKE_KEY + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        self.assertTrue(payload["replaced_existing"])
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"CWORK_APP_KEY={FAKE_KEY}", text)
        self.assertIn("OTHER=1", text)
        self.assertIn("# comment", text)

    def test_replaces_existing_value_and_preserves_other_lines(self):
        self._write_env("CWORK_APP_KEY=oldvalue\nKEEP=yes\n".encode("utf-8"))
        result = run_key_set(self.env_file, FAKE_KEY)
        self.assertEqual(result.returncode, 0)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"CWORK_APP_KEY={FAKE_KEY}", text)
        self.assertIn("KEEP=yes", text)
        self.assertNotIn("oldvalue", text)

    def test_repairs_export_prefix_and_duplicates(self):
        self._write_env(
            'export CWORK_APP_KEY="stale"\nCWORK_APP_KEY=stale2\nKEEP=1\n'.encode("utf-8")
        )
        result = run_key_set(self.env_file, FAKE_KEY)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["fixed_export_prefix"])
        self.assertEqual(payload["removed_duplicates"], 1)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("CWORK_APP_KEY="), 1)
        self.assertNotIn("stale", text)
        self.assertIn("KEEP=1", text)

    def test_strips_bom(self):
        self._write_env("\ufeffCWORK_APP_KEY=oldvalue\nKEEP=1\n".encode("utf-8"))
        result = run_key_set(self.env_file, FAKE_KEY)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["fixed_bom"])
        raw = self.env_file.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(f"CWORK_APP_KEY={FAKE_KEY}", raw.decode("utf-8"))

    def test_file_mode_is_0600(self):
        self._write_env("CWORK_APP_KEY=oldvalue\n".encode("utf-8"))
        run_key_set(self.env_file, FAKE_KEY)
        mode = stat.S_IMODE(self.env_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_rejects_empty_and_whitespace_key_without_touching_file(self):
        self._write_env("CWORK_APP_KEY=oldvalue\n".encode("utf-8"))
        result = run_key_set(self.env_file, "   \n")
        self.assertEqual(result.returncode, 2)
        result = run_key_set(self.env_file, "abc def\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CWORK_APP_KEY=oldvalue", self.env_file.read_text(encoding="utf-8"))

    def test_never_echoes_the_key(self):
        self._write_env("CWORK_APP_KEY=oldvalue\n".encode("utf-8"))
        result = run_key_set(self.env_file, FAKE_KEY)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(FAKE_KEY, result.stdout)
        self.assertNotIn(FAKE_KEY, result.stderr)

    def test_creates_standalone_env_without_template(self):
        result = run_key_set(self.env_file, FAKE_KEY)
        self.assertEqual(result.returncode, 0)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertEqual(text.strip(), f"CWORK_APP_KEY={FAKE_KEY}")


if __name__ == "__main__":
    unittest.main()
