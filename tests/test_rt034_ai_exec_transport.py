import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_ai_common import (  # noqa: E402
    _extract_exec_envelope,
    _parse_exec_result,
    invoke_openclaw_json,
)


def _exec_stdout(final_text: str, *, ok: bool = True) -> str:
    """Mirror real `openclaw agent exec --json` output: log noise, then envelope."""
    envelope = {
        "ok": ok,
        "status": "ok" if ok else "error",
        "final": final_text,
        "payloads": [{"text": final_text, "mediaUrl": None}],
        "usage": {"input": 24863, "output": 60, "total": 24923},
        "costUsd": 0.035,
        "codeModeEngaged": False,
        "assistantTurns": 1,
        "model": "evanModel",
        "provider": "evan-openai",
        "sessionId": "e8daaa2c-c950-476f-ac7c-ec9dd057b886",
    }
    noise = (
        "Config warnings: plugins.entries.openclaw-code-agent: duplicate plugin id\n"
        "[sqlite/transaction] slow SQLite transaction hold\n"
        "[agent/embedded] post-run auth-profile bookkeeping completed\n"
    )
    return noise + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"


class ExecEnvelopeTests(unittest.TestCase):
    def test_envelope_extracted_from_noisy_stdout(self):
        envelope = _extract_exec_envelope(_exec_stdout('{"ok":true}'))
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["final"], '{"ok":true}')
        # 信封内部的嵌套对象（usage）不得被误当成信封本身
        self.assertIn("usage", envelope)

    def test_parse_result_returns_model_json(self):
        payload = {"schema_version": "x", "answer": 42}
        self.assertEqual(_parse_exec_result(_exec_stdout(json.dumps(payload))), payload)

    def test_parse_result_strips_code_fences(self):
        self.assertEqual(
            _parse_exec_result(_exec_stdout("```json\n{\"a\": 1}\n```")), {"a": 1}
        )

    def test_parse_result_raises_on_error_envelope(self):
        with self.assertRaisesRegex(RuntimeError, "agent exec failed"):
            _parse_exec_result(_exec_stdout("", ok=False))

    def test_parse_result_raises_without_model_output(self):
        with self.assertRaisesRegex(RuntimeError, "no model output"):
            _parse_exec_result(_exec_stdout("   "))


class ExecTransportTests(unittest.TestCase):
    def _invoke(self, proc, *, transport):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with (
                patch("cwk_ai_common.PROJECT", project),
                patch("cwk_ai_common.assert_safe_ai_agent") as guard,
                patch("cwk_ai_common.subprocess.Popen", return_value=proc) as popen,
                patch("cwk_ai_common.subprocess.run") as cleanup,
                patch.dict(
                    "os.environ",
                    {"CWK_AI_TRANSPORT": transport, "CWK_AI_CALL_RETRIES": "1"},
                    clear=False,
                ),
            ):
                result = invoke_openclaw_json(
                    "safe prompt",
                    model="newapi/BD-MiniMax",
                    stage="test",
                    timeout_seconds=1,
                    prompt_dir=project,
                )
        return result, popen, cleanup, guard

    def test_exec_transport_builds_agentless_command(self):
        payload = {"schema_version": "x"}
        proc = SimpleNamespace(
            returncode=0,
            communicate=lambda timeout: (_exec_stdout(json.dumps(payload)), ""),
        )
        result, popen, cleanup, guard = self._invoke(proc, transport="exec")
        self.assertEqual(result, payload)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["openclaw", "agent", "exec"])
        self.assertNotIn("--agent", command)
        self.assertNotIn("--local", command)
        self.assertEqual(command[command.index("--model") + 1], "newapi/BD-MiniMax")
        self.assertIn("--cwd", command)
        guard.assert_not_called()
        cleanup.assert_not_called()

    def test_agent_transport_unchanged(self):
        payload = {"schema_version": "x"}
        proc = SimpleNamespace(
            returncode=0,
            communicate=lambda timeout: (json.dumps(payload), ""),
        )
        result, popen, cleanup, guard = self._invoke(proc, transport="")
        self.assertEqual(result, payload)
        command = popen.call_args.args[0]
        self.assertIn("--local", command)
        self.assertEqual(command[command.index("--agent") + 1], "cwk-ai-reviewer")
        guard.assert_called_once()
        cleanup.assert_called_once()

    def test_invalid_transport_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with (
                patch("cwk_ai_common.PROJECT", project),
                patch.dict(
                    "os.environ", {"CWK_AI_TRANSPORT": "carrier-pigeon"}, clear=False
                ),
            ):
                with self.assertRaisesRegex(ValueError, "CWK_AI_TRANSPORT"):
                    invoke_openclaw_json(
                        "safe prompt",
                        model="newapi/BD-MiniMax",
                        stage="test",
                        timeout_seconds=1,
                        prompt_dir=project,
                    )


if __name__ == "__main__":
    unittest.main()
