"""RT-026 acceptance tests for the nightly pipeline wiki-model evolution.

Stage 8 of the PR-001 script-evolution policy covers
``scripts/cwk_nightly_pipeline.py``.  The byte change this stage records is the
switch of the nightly wiki-compile defaults from the MiniMax pair to
``evan-openai/glm-5.3-flash`` (primary) and ``deepseek/deepseek-v4-flash``
(JSON repair channel), landed as commit ``c26c7ad``'s sibling ``dc96c28`` on
2026-08-30 after a live probe.

The defaults are read out of the production source with ``ast`` rather than
re-declared here: a revert of the pipeline back to the MiniMax defaults must
fail this file, otherwise the receipt would not be evidence of anything.
"""

from __future__ import annotations

import argparse
import ast
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PIPELINE = PROJECT / "scripts" / "cwk_nightly_pipeline.py"
if str(PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_ai_common  # noqa: E402
import cwk_nightly_pipeline  # noqa: E402

EXPECTED_PRIMARY = "evan-openai/glm-5.3-flash"
EXPECTED_REPAIR = "deepseek/deepseek-v4-flash"


def _environ_fallback(node: ast.AST) -> str | None:
    """Return the literal fallback of an ``os.environ.get(NAME, literal)`` call."""

    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return None
    if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant):
        return None
    value = node.args[1].value
    return value if isinstance(value, str) else None


def _declared_default(setting: str) -> str | None:
    """Extract the shipped default for a ``config_value(..., setting, default)`` call."""

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "config_value":
            continue
        if len(node.args) != 4:
            continue
        name_arg = node.args[2]
        if not isinstance(name_arg, ast.Constant) or name_arg.value != setting:
            continue
        return _environ_fallback(node.args[3])
    return None


class NightlyPipelineModelEvolutionTests(unittest.TestCase):
    def test_wiki_model_defaults_match_the_documented_roles(self) -> None:
        self.assertEqual(_declared_default("wiki_model"), EXPECTED_PRIMARY)
        self.assertEqual(_declared_default("wiki_repair_model"), EXPECTED_REPAIR)

    def test_wiki_model_defaults_pass_the_production_model_gate(self) -> None:
        for model in (EXPECTED_PRIMARY, EXPECTED_REPAIR):
            cwk_ai_common.assert_cwk_model(model)
            self.assertIn(model, cwk_ai_common.allowed_cwk_models())

    def test_model_roles_documents_the_evolved_defaults(self) -> None:
        roles = (PROJECT / "MODEL_ROLES.md").read_text(encoding="utf-8")
        self.assertIn(EXPECTED_PRIMARY, roles)
        self.assertIn(EXPECTED_REPAIR, roles)

    def test_explicit_argument_and_config_still_outrank_the_default(self) -> None:
        args = argparse.Namespace(wiki_model="newapi/BD-MiniMax")
        self.assertEqual(
            cwk_nightly_pipeline.config_value(args, {}, "wiki_model", EXPECTED_PRIMARY),
            "newapi/BD-MiniMax",
        )
        blank = argparse.Namespace(wiki_model=None)
        self.assertEqual(
            cwk_nightly_pipeline.config_value(
                blank, {"wiki_model": "newapi/BD-glm"}, "wiki_model", EXPECTED_PRIMARY
            ),
            "newapi/BD-glm",
        )
        self.assertEqual(
            cwk_nightly_pipeline.config_value(blank, {}, "wiki_model", EXPECTED_PRIMARY),
            EXPECTED_PRIMARY,
        )
