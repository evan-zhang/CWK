"""RT-032: the execution contract must describe the nightly run *exactly*.

The contract is the document a user reads before answering "may this run every
night?". If it says "publishing: off" and the run publishes, the answer they
gave was to a different question — the consent is void even though every gate
was walked correctly. So "close enough" is not a passing grade here.

``cwk_activation_contract`` cannot simply call into ``cwk_nightly_pipeline``:
importing that module executes ``load_local_env(PROJECT/'.env')`` at import
time, which would pull credentials into the wizard's process merely because
someone asked to render a contract. It also lives under PR-001 script-evolution
governance and is owned by another RT. So the contract module re-implements the
resolution, and this file is the thing that keeps the copy honest, two ways:

1. **Behavioural cross-validation** — drive the contract's resolver and a
   re-composition built from the pipeline's *own* ``env_bool`` /
   ``config_value`` over the same (config, environment) pairs, and require
   identical answers. This catches a wrong vocabulary or a wrong precedence.
2. **Source pinning** — assert the pipeline's ``main`` still composes those
   primitives the way the copy assumes. This catches the case behavioural
   testing cannot: upstream changing the composition itself.

Importing the pipeline here is safe in a way it is not in the wizard: this file
scrubs the environment first, and a test process has no scheduled run to
misdescribe.

Refs: RT-032
"""

from __future__ import annotations

import argparse
import ast
import inspect
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_activation_contract as C  # noqa: E402
import cwk_nightly_pipeline as N  # noqa: E402

PIPELINE_SOURCE = (PROJECT / "scripts" / "cwk_nightly_pipeline.py").read_text(encoding="utf-8")

# The five integer settings the scheduled command resolves, with the
# environment variable each one consults.
INT_SETTINGS = (
    ("detail_cap", "CWK_DETAIL_CAP"),
    ("continuation_cap", "CWK_CONTINUATION_CAP"),
    ("backfill_cap", "CWK_BACKFILL_CAP"),
    ("backfill_page_size", "CWK_BACKFILL_PAGE_SIZE"),
    ("source_completeness_lookback_days", "CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS"),
)

# The scheduler handoff pins the argv to exactly this shape, so every optional
# flag is absent and the command-line layer never wins a conflict.
SCHEDULED_ARGV_DEFAULTS = {
    "detail_cap": None,
    "continuation_cap": None,
    "backfill_cap": None,
    "backfill_page_size": None,
    "backfill_enabled": None,
    "source_completeness": None,
    "source_completeness_lookback_days": None,
    "sync_docdb": False,
    "no_publish_mirror": False,
}


def env_default_from_pipeline(env_key: str) -> int:
    """Read ``os.environ.get("<env_key>", <int>)``'s literal out of the source.

    Parsed, never executed: the point of this whole file is that the pipeline
    is not run or trusted to run, only read.
    """

    tree = ast.parse(PIPELINE_SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) != 2:
            continue
        first, second = node.args
        if (
            isinstance(first, ast.Constant)
            and first.value == env_key
            and isinstance(second, ast.Constant)
            and isinstance(second.value, int)
        ):
            return second.value
    raise AssertionError(f"no integer default found for {env_key} in the pipeline")


def upstream_resolution(config: dict, env: dict) -> dict:
    """Re-compose the pipeline's own primitives under a scrubbed environment.

    ``env_bool`` reads ``os.environ`` directly, so the environment has to be
    replaced rather than passed. ``clear=True`` matters twice over: it keeps a
    stray ``CWK_*`` from the developer's shell out of the comparison, and it
    keeps ``CWORK_APP_KEY`` out of this process entirely.
    """

    with mock.patch.dict(os.environ, env, clear=True):
        args = argparse.Namespace(**SCHEDULED_ARGV_DEFAULTS)
        settings = {}
        for name, env_key in INT_SETTINGS:
            settings[name] = int(
                N.config_value(
                    args, config, name, os.environ.get(env_key, env_default_from_pipeline(env_key))
                )
            )

        env_backfill = N.env_bool("CWK_BACKFILL_ENABLED")
        settings["backfill_enabled"] = (
            env_backfill if env_backfill is not None else bool(config.get("backfill_enabled", True))
        )

        env_completeness = N.env_bool("CWK_SOURCE_COMPLETENESS")
        settings["source_completeness"] = (
            env_completeness
            if env_completeness is not None
            else bool(config.get("source_completeness", True))
        )

        settings["sync_docdb"] = bool(
            config.get("sync_docdb", N.env_bool("CWK_SYNC_DOCDB") or False)
        )
        return settings


# Every case here is a (config, environment) pair whose answer a reasonable
# person could get wrong. The name is the claim being made.
CASES = {
    "nothing set at all": ({}, {}),
    # The reported defect: "y" is true upstream. A truth table missing it makes
    # the contract say "no DocDB publishing" on a night that publishes.
    "sync_docdb from CWK_SYNC_DOCDB=y": ({}, {"CWK_SYNC_DOCDB": "y"}),
    "sync_docdb from CWK_SYNC_DOCDB=Y with padding": ({}, {"CWK_SYNC_DOCDB": "  Y "}),
    "sync_docdb from every accepted spelling": ({}, {"CWK_SYNC_DOCDB": "ON"}),
    "sync_docdb from an unrecognised word": ({}, {"CWK_SYNC_DOCDB": "maybe"}),
    "sync_docdb empty string": ({}, {"CWK_SYNC_DOCDB": ""}),
    # config-versus-env conflicts, in both directions, because the two
    # directions really do resolve differently upstream.
    "sync_docdb config false beats env 1": ({"sync_docdb": False}, {"CWK_SYNC_DOCDB": "1"}),
    "sync_docdb config true with env 0": ({"sync_docdb": True}, {"CWK_SYNC_DOCDB": "0"}),
    # Python truthiness, not parsing: the string "false" is true upstream.
    "sync_docdb config is the string false": ({"sync_docdb": "false"}, {}),
    "backfill env 1 beats config false": (
        {"backfill_enabled": False},
        {"CWK_BACKFILL_ENABLED": "1"},
    ),
    "backfill env 0 beats config true": (
        {"backfill_enabled": True},
        {"CWK_BACKFILL_ENABLED": "0"},
    ),
    # Not "unrecognised, so fall back to the default" — unrecognised is false.
    "backfill env is an unrecognised word": ({}, {"CWK_BACKFILL_ENABLED": "maybe"}),
    "backfill config is the string false": ({"backfill_enabled": "false"}, {}),
    "completeness env off beats config true": (
        {"source_completeness": True},
        {"CWK_SOURCE_COMPLETENESS": "off"},
    ),
    "completeness env y": ({}, {"CWK_SOURCE_COMPLETENESS": "y"}),
    # Integers resolve the other way round: config wins over env.
    "detail cap config beats env": ({"detail_cap": 7}, {"CWK_DETAIL_CAP": "99"}),
    "detail cap from env only": ({}, {"CWK_DETAIL_CAP": "99"}),
    "caps from env only": (
        {},
        {
            "CWK_CONTINUATION_CAP": "3",
            "CWK_BACKFILL_CAP": "4",
            "CWK_BACKFILL_PAGE_SIZE": "5",
        },
    ),
    "lookback config beats env": (
        {"source_completeness_lookback_days": 0},
        {"CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS": "31"},
    ),
    "lookback from env only": ({}, {"CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS": "31"}),
    "a numeric string in config": ({"detail_cap": "7"}, {}),
    "everything at once": (
        {"detail_cap": 11, "sync_docdb": True, "backfill_enabled": False},
        {"CWK_DETAIL_CAP": "99", "CWK_SYNC_DOCDB": "no", "CWK_BACKFILL_ENABLED": "y"},
    ),
}


class NightlyResolutionEquivalenceTests(unittest.TestCase):
    """The copy and the original must answer identically, case by case."""

    def test_every_case_resolves_the_same_way(self):
        for name, (config, env) in CASES.items():
            with self.subTest(case=name):
                mine = C.resolve_nightly_runtime(config, env)["settings"]
                theirs = upstream_resolution(config, env)
                self.assertEqual(mine, theirs, name)

    def test_the_two_agree_on_the_full_boolean_vocabulary(self):
        """Every spelling, and a few that are deliberately not accepted."""

        spellings = (
            "1", "true", "TRUE", "yes", "y", "Y", "on", "ON", " on ",
            "0", "false", "no", "n", "off", "", "maybe", "2", "true-ish",
        )
        for value in spellings:
            with self.subTest(value=value):
                for env_key, setting in (
                    ("CWK_SYNC_DOCDB", "sync_docdb"),
                    ("CWK_BACKFILL_ENABLED", "backfill_enabled"),
                    ("CWK_SOURCE_COMPLETENESS", "source_completeness"),
                ):
                    env = {env_key: value}
                    self.assertEqual(
                        C.resolve_nightly_runtime({}, env)["settings"][setting],
                        upstream_resolution({}, env)[setting],
                        f"{env_key}={value!r}",
                    )

    def test_the_contract_itself_reports_the_same_settings(self):
        """Not just the resolver — what lands in the signed document."""

        for name, (config, env) in CASES.items():
            with self.subTest(case=name):
                contract = C.build_execution_contract(
                    config=config,
                    env=env,
                    profile_sha256="a" * 64,
                    run_at_local="02:30",
                    timezone="Asia/Shanghai",
                    generated_at="2026-09-02T00:00:00Z",
                )
                expected = upstream_resolution(config, env)
                self.assertEqual(contract["publishing"]["sync_docdb"], expected["sync_docdb"])
                self.assertEqual(
                    contract["caps"]["detail_cap"], expected["detail_cap"]
                )
                self.assertEqual(
                    contract["caps"]["continuation_cap"], expected["continuation_cap"]
                )
                self.assertEqual(
                    contract["caps"]["backfill_cap"], expected["backfill_cap"]
                )
                self.assertEqual(
                    contract["caps"]["backfill_page_size"], expected["backfill_page_size"]
                )
                self.assertEqual(
                    contract["sources"]["backfill_enabled"], expected["backfill_enabled"]
                )
                self.assertEqual(
                    contract["sources"]["source_completeness_enabled"],
                    expected["source_completeness"],
                )
                self.assertEqual(
                    contract["late_data_lookback_days"],
                    expected["source_completeness_lookback_days"],
                )

    def test_a_sync_docdb_env_value_is_never_silently_dropped(self):
        """The named defect, stated on its own so it cannot regress quietly."""

        contract = C.build_execution_contract(
            config={},
            env={"CWK_SYNC_DOCDB": "y"},
            profile_sha256="a" * 64,
            run_at_local="02:30",
            timezone="Asia/Shanghai",
            generated_at="2026-09-02T00:00:00Z",
        )
        self.assertTrue(contract["publishing"]["sync_docdb"])
        self.assertEqual(contract["runtime_resolution"]["sources"]["sync_docdb"], "env")

    def test_an_unparsable_integer_is_refused_rather_than_defaulted(self):
        """Upstream would abort on ``int("lots")``; the contract must not invent a number."""

        for config, env in (
            ({"detail_cap": "lots"}, {}),
            ({}, {"CWK_DETAIL_CAP": "lots"}),
            ({"backfill_cap": None}, {}),
        ):
            with self.subTest(config=config, env=env):
                with self.assertRaises(C.NightlyConfigError):
                    C.resolve_nightly_runtime(config, env)

    def test_an_out_of_range_lookback_is_refused(self):
        for value in (-1, 32, 999):
            with self.subTest(value=value):
                with self.assertRaises(C.NightlyConfigError):
                    C.resolve_nightly_runtime({"source_completeness_lookback_days": value}, {})
        # And the boundaries upstream does accept.
        for value in (0, 31):
            with self.subTest(value=value):
                resolved = C.resolve_nightly_runtime(
                    {"source_completeness_lookback_days": value}, {}
                )
                self.assertEqual(
                    resolved["settings"]["source_completeness_lookback_days"], value
                )


class ScheduledEnvironmentTests(unittest.TestCase):
    """A contract resolved from the shell describes a run that will not happen.

    The handoff's ``env_allowlist`` is ``["CWORK_APP_KEY"]``. The scheduled task
    therefore sees no ``CWK_*`` switch at all, so any setting that came from the
    current shell would resolve differently at 02:30 — the user would have
    confirmed a document describing a different run.
    """

    def contract(self, config, env):
        return C.build_execution_contract(
            config=config,
            env=env,
            profile_sha256="a" * 64,
            run_at_local="02:30",
            timezone="Asia/Shanghai",
            generated_at="2026-09-02T00:00:00Z",
        )

    def test_a_config_only_contract_is_equivalent_under_the_scheduled_environment(self):
        contract = self.contract({"detail_cap": 7, "sync_docdb": True}, {})
        resolution = contract["runtime_resolution"]
        self.assertTrue(resolution["scheduled_environment_equivalent"])
        self.assertEqual(resolution["settings_requiring_shell_environment"], [])

    def test_a_shell_sourced_setting_is_named_not_hidden(self):
        contract = self.contract({}, {"CWK_SYNC_DOCDB": "y", "CWK_DETAIL_CAP": "99"})
        resolution = contract["runtime_resolution"]
        self.assertFalse(resolution["scheduled_environment_equivalent"])
        self.assertEqual(
            sorted(resolution["settings_requiring_shell_environment"]),
            ["detail_cap", "sync_docdb"],
        )

    def test_an_environment_value_matching_the_default_is_not_flagged(self):
        """Flagging every ``CWK_*`` would cry wolf; only a real difference counts."""

        contract = self.contract({}, {"CWK_DETAIL_CAP": str(C.DEFAULT_DETAIL_CAP)})
        self.assertTrue(contract["runtime_resolution"]["scheduled_environment_equivalent"])

    def test_the_handoff_is_refused_while_the_contract_depends_on_the_shell(self):
        contract = self.contract({}, {"CWK_SYNC_DOCDB": "y"})
        with self.assertRaises(C.ScheduledEnvironmentMismatch):
            C.build_scheduler_handoff(
                contract=contract,
                contract_sha256=contract["contract_sha256"],
                profile_sha256="a" * 64,
                pilot_receipt_sha256="b" * 64,
                config_path=PROJECT / "cwk-mirror.local.json",
                project_root=PROJECT,
                generated_at="2026-09-02T00:00:00Z",
            )

    def test_the_same_setting_moved_into_the_config_is_accepted(self):
        """The refusal has to be actionable, not a dead end."""

        contract = self.contract({"sync_docdb": True}, {})
        handoff = C.build_scheduler_handoff(
            contract=contract,
            contract_sha256=contract["contract_sha256"],
            profile_sha256="a" * 64,
            pilot_receipt_sha256="b" * 64,
            config_path=PROJECT / "cwk-mirror.local.json",
            project_root=PROJECT,
            generated_at="2026-09-02T00:00:00Z",
        )
        self.assertEqual(handoff["command_spec"]["env_allowlist"], ["CWORK_APP_KEY"])

    def test_the_rendered_markdown_says_where_each_value_came_from(self):
        """The user is read this document; the caveat has to be in it, not only in JSON."""

        text = C.render_contract_markdown(self.contract({}, {"CWK_SYNC_DOCDB": "y"}))
        self.assertIn("## 这些取值从哪里来", text)
        self.assertIn("sync_docdb：当前 shell 的环境变量", text)
        self.assertIn("警告", text)
        self.assertIn("CWORK_APP_KEY", text)

    def test_a_config_only_contract_carries_no_warning(self):
        text = C.render_contract_markdown(self.contract({"sync_docdb": True}, {}))
        self.assertIn("sync_docdb：配置文件", text)
        self.assertNotIn("警告", text)

    def test_the_markdown_ties_the_lookback_to_the_completeness_switch(self):
        """A lookback window that never runs is a false promise."""

        text = C.render_contract_markdown(self.contract({"source_completeness": False}, {}))
        self.assertIn("来源完整性补采：关", text)
        self.assertIn("补采已关，本项不生效", text)


class UpstreamCompositionPinTests(unittest.TestCase):
    """Behaviour tests cannot see a *composition* change. These can."""

    @classmethod
    def setUpClass(cls):
        cls.main_source = " ".join(inspect.getsource(N.main).split())

    def assert_pinned(self, fragment: str):
        self.assertIn(
            " ".join(fragment.split()),
            self.main_source,
            "the nightly pipeline no longer resolves this setting the way the "
            "execution contract assumes; re-derive the copy in "
            "cwk_activation_contract.resolve_nightly_runtime before touching this pin",
        )

    def test_integer_settings_still_take_config_over_environment(self):
        for name, env_key in INT_SETTINGS[:4]:
            with self.subTest(name=name):
                self.assert_pinned(
                    f'args.{name} = int(config_value(args, config, "{name}", '
                    f'os.environ.get("{env_key}", {env_default_from_pipeline(env_key)})))'
                )

    def test_the_lookback_still_takes_config_over_environment(self):
        self.assert_pinned('"source_completeness_lookback_days"')
        self.assert_pinned(
            'os.environ.get("CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS", '
            f"{env_default_from_pipeline('CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS')})"
        )
        self.assert_pinned(
            "if args.source_completeness_lookback_days < 0 "
            "or args.source_completeness_lookback_days > 31:"
        )

    def test_the_boolean_switches_still_take_environment_over_config(self):
        self.assert_pinned(
            'env_backfill = env_bool("CWK_BACKFILL_ENABLED") '
            "args.backfill_enabled = env_backfill if env_backfill is not None "
            'else bool(config.get("backfill_enabled", True))'
        )
        self.assert_pinned(
            'env_source_completeness = env_bool("CWK_SOURCE_COMPLETENESS")'
        )
        self.assert_pinned(
            "args.source_completeness = ( env_source_completeness "
            "if env_source_completeness is not None "
            'else bool(config.get("source_completeness", True)) )'
        )

    def test_sync_docdb_still_takes_config_over_environment(self):
        """The one that goes the other way. Losing this is how the copy rots."""

        self.assert_pinned(
            'args.sync_docdb = bool(config.get("sync_docdb", env_bool("CWK_SYNC_DOCDB") or False))'
        )

    def test_the_accepted_boolean_words_are_still_these_five(self):
        source = " ".join(inspect.getsource(N.env_bool).split())
        self.assertIn('{"1", "true", "yes", "y", "on"}', source)
        self.assertEqual(set(C.NIGHTLY_ENV_TRUE), {"1", "true", "yes", "y", "on"})

    def test_config_value_still_ignores_only_none_empty_and_empty_list(self):
        source = " ".join(inspect.getsource(N.config_value).split())
        self.assertIn("if value not in (None, \"\", []):", source)

    def test_the_scheduled_argv_still_carries_no_switch_that_could_win(self):
        """If the handoff ever grows a flag, the command-line layer starts mattering."""

        contract = C.build_execution_contract(
            config={},
            env={},
            profile_sha256="a" * 64,
            run_at_local="02:30",
            timezone="Asia/Shanghai",
            generated_at="2026-09-02T00:00:00Z",
        )
        handoff = C.build_scheduler_handoff(
            contract=contract,
            contract_sha256=contract["contract_sha256"],
            profile_sha256="a" * 64,
            pilot_receipt_sha256="b" * 64,
            config_path=PROJECT / "cwk-mirror.local.json",
            project_root=PROJECT,
            generated_at="2026-09-02T00:00:00Z",
        )
        argv = handoff["command_spec"]["argv"]
        flags = {item for item in argv if item.startswith("--")}
        self.assertEqual(flags, {"--config", "--run-name", "--date"})

    def test_the_contract_module_still_refuses_to_import_the_pipeline(self):
        """This file may import it. The wizard's own modules may not."""

        for name in ("cwk_activation_contract", "cwk_activation_state", "cwk_activation_wizard"):
            with self.subTest(module=name):
                source = (PROJECT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"^\s*(import|from)\s+cwk_nightly_pipeline", source, re.M),
                    f"{name} must not import the pipeline: doing so would execute "
                    "load_local_env and pull credentials into the wizard",
                )


if __name__ == "__main__":
    unittest.main()
