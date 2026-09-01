"""RT-032: the execution contract must describe the nightly run *exactly*.

The contract is the document a user reads before answering "may this run every
night?". If it says "publishing: off" and the run publishes, the answer they
gave was to a different question — the consent is void even though every gate
was walked correctly. So "close enough" is not a passing grade here.

``cwk_activation_contract`` cannot simply call into ``cwk_nightly_pipeline``:
that module lives under PR-001 script-evolution governance and is owned by
another RT, and importing it executes ``load_local_env(PROJECT/'.env')`` at
import time — which would pull credentials into the wizard's process merely
because someone asked to render a contract. So the contract module
re-implements the resolution, and this file is the thing that keeps the copy
honest, in four ways:

1. **Completeness** — read the pipeline's own source and enumerate every
   config key, every environment variable and every command-line option it
   honours. The contract's registry must cover all of them. This is the check
   that a hand-maintained list cannot make about itself: the expected set is
   derived from upstream, never from the registry.
2. **Precedence** — classify, again from upstream's source, *how* each key is
   resolved, and require the registry to agree. The four classes are not
   uniform and getting one backwards flips a published/not-published answer.
3. **Behavioural cross-validation** — drive the contract's resolver and a
   re-composition transcribed from the pipeline's *own* ``env_bool`` /
   ``config_value`` over the same (config, environment) pairs, and require
   identical answers.
4. **Source pinning** — assert the pipeline's ``main`` still composes those
   primitives the way the copy assumes. This catches the case behavioural
   testing cannot: upstream changing the composition itself.

**This file does not import the pipeline.** It loads a sanitized copy with the
``load_local_env`` call removed and the environment cleared, and then asserts
that the test process' own environment came through untouched. A test that
reads someone's ``.env`` to check that the product does not read ``.env`` is
not a test, it is the same defect wearing a different hat.

Refs: RT-032
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import re
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_activation_contract as C  # noqa: E402

PIPELINE_PATH = PROJECT / "scripts" / "cwk_nightly_pipeline.py"
PIPELINE_SOURCE = PIPELINE_PATH.read_text(encoding="utf-8")
CONFIG_TEMPLATE_PATH = PROJECT / "skill" / "templates" / "CONFIG.example.json"


# ── loading the pipeline without ingesting anybody's .env ───────────────────


def _forbidden_load_local_env(*_args, **_kwargs):  # pragma: no cover - guard
    raise AssertionError(
        "the sanitized pipeline module tried to load a .env file; the whole "
        "point of load_pipeline_module() is that this never happens"
    )


def load_pipeline_module() -> types.ModuleType:
    """Execute the pipeline's module body with the ``.env`` ingestion removed.

    The module is needed for its primitives (``env_bool``, ``config_value``,
    ``enforce_cloud_pause``) and its path constants. It is *not* needed for its
    one module-level side effect, which is to copy every ``KEY=value`` line of
    a gitignored ``.env`` into ``os.environ`` — including ``CWORK_APP_KEY``.

    So the call statement is deleted from the AST before compiling, the body is
    executed under a cleared environment, and the function itself is replaced
    afterwards by a stub that raises. Three independent guards, because a
    credential pulled into a test process is not something to be 90% sure about.
    """

    tree = ast.parse(PIPELINE_SOURCE, filename=str(PIPELINE_PATH))
    kept = []
    removed = 0
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "load_local_env"
        ):
            removed += 1
            continue
        kept.append(node)
    if removed != 1:
        raise AssertionError(
            f"expected exactly one module-level load_local_env() call, found {removed}; "
            "the sanitizer no longer matches upstream and must be re-derived before "
            "this file may import the pipeline at all"
        )
    tree.body = kept

    module = types.ModuleType("cwk_nightly_pipeline_sanitized")
    module.__file__ = str(PIPELINE_PATH)
    before = dict(os.environ)
    with mock.patch.dict(os.environ, {}, clear=True):
        exec(compile(tree, str(PIPELINE_PATH), "exec"), module.__dict__)  # noqa: S102
    if dict(os.environ) != before:
        raise AssertionError("loading the pipeline changed this process' environment")
    # The real function is kept under a name nothing reaches by accident. The
    # ``.env`` layer is now part of what the contract has to model, and the only
    # honest oracle for "what does load_local_env do to this file" is
    # load_local_env itself — a second transcription of the parser would just be
    # a second place to make the same mistake. It is invoked only through
    # ``upstream_load_local_env`` below, which hands it a temp path and a fake
    # ``os``, so none of the three guards is weakened: nothing reads a real
    # ``.env`` and nothing writes to this process' environment.
    module.__real_load_local_env__ = module.load_local_env
    module.load_local_env = _forbidden_load_local_env
    return module


ENVIRONMENT_AT_IMPORT = dict(os.environ)
N = load_pipeline_module()


class _EnvironShim:
    """Just enough of the ``os`` module for ``load_local_env`` to run.

    ``load_local_env`` touches exactly one thing from ``os``:
    ``os.environ.setdefault``. Handing it a shim rather than patching
    ``os.environ`` means the oracle can be driven over dozens of synthetic
    ``.env`` files without the real process environment being written to even
    transiently — there is no restore step to get wrong, and no window in which
    a ``CWORK_APP_KEY`` invented by a test is visible to anything else.
    """

    def __init__(self, environ: dict):
        self.environ = environ


def upstream_load_local_env(path: Path, shell: dict) -> dict:
    """Run upstream's real ``load_local_env`` over ``path``; return the result.

    ``shell`` is the pre-existing process environment. What comes back is what
    upstream would have left in ``os.environ`` — which, because upstream uses
    ``setdefault``, *is* the merged environment every nightly setting is then
    resolved against.
    """

    sandbox = dict(shell)
    before = dict(os.environ)
    with mock.patch.object(N, "os", _EnvironShim(sandbox)):
        N.__real_load_local_env__(path)
    if dict(os.environ) != before:  # pragma: no cover - guard
        raise AssertionError("the .env oracle escaped its shim and wrote to os.environ")
    return sandbox


# ── reading upstream's own declaration of what it honours ──────────────────


def _pipeline_main() -> ast.FunctionDef:
    tree = ast.parse(PIPELINE_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("cwk_nightly_pipeline has no main()")


MAIN_AST = _pipeline_main()


def _env_names(node: ast.AST) -> list[str]:
    """Every environment variable name read inside an AST subtree, in order."""

    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and child.args
                and isinstance(child.args[0], ast.Constant)
            ):
                found.append(child.args[0].value)
            if (
                isinstance(func, ast.Name)
                and func.id == "env_bool"
                and child.args
                and isinstance(child.args[0], ast.Constant)
            ):
                found.append(child.args[0].value)
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "environ"
            and isinstance(child.slice, ast.Constant)
        ):
            found.append(child.slice.value)
    ordered: list[str] = []
    for name in found:
        if name not in ordered:
            ordered.append(name)
    return ordered


def upstream_argparse_options() -> dict[str, dict]:
    """``{dest: {"flag":…, "default_src":…, "action":…}}`` for every option."""

    options: dict[str, dict] = {}
    for node in ast.walk(MAIN_AST):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        flag = node.args[0].value
        if not flag.startswith("--"):
            continue
        dest = flag[2:].replace("-", "_")
        entry = {"flag": flag, "default_src": "", "action": ""}
        for keyword in node.keywords:
            if keyword.arg == "default":
                entry["default_src"] = ast.unparse(keyword.value)
            elif keyword.arg == "action":
                entry["action"] = ast.unparse(keyword.value)
        options[dest] = entry
    return options


UPSTREAM_OPTIONS = upstream_argparse_options()
ARGV_DESTS = ("config", "run_name", "date")


def upstream_config_keys() -> set[str]:
    """Every config-file key the pipeline reads, straight out of its source."""

    keys: set[str] = set()
    for node in ast.walk(MAIN_AST):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "config_value"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
        ):
            keys.add(node.args[2].value)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "config"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
    return keys


def upstream_statements_for(key: str) -> list[ast.stmt]:
    """The top-level statements of ``main`` that assign ``args.<key>``."""

    hits = []
    for statement in MAIN_AST.body:
        for child in ast.walk(statement):
            if (
                isinstance(child, ast.Attribute)
                and child.attr == key
                and isinstance(child.value, ast.Name)
                and child.value.id == "args"
                and isinstance(child.ctx, ast.Store)
            ):
                hits.append(statement)
                break
    return hits


def upstream_precedence() -> dict[str, str]:
    """Classify each config key's precedence from upstream's source alone.

    The four classes exist because upstream really does resolve four different
    ways, and the difference is not cosmetic — ``config > env`` versus
    ``env > config`` is the difference between a contract that says "publishing
    off" and a night that publishes.

    The tell for each class is structural:

    * a ``bool(config.get(k, …))`` guarded by ``env_bool`` → ``env > config``;
    * except when the ``env_bool`` sits *inside* the ``config.get`` default,
      which is the single inverted case, ``sync_docdb``;
    * a ``config_value(...)`` whose *argparse* default reads the environment →
      the args value is already non-None, so ``env > config``;
    * a ``config_value(...)`` whose own default reads the environment →
      ``config > env``;
    * a ``config_value(...)`` with neither → config, or another resolved key.
    """

    classes: dict[str, str] = {}
    for node in ast.walk(MAIN_AST):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "config_value"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
        ):
            key = node.args[2].value
            own_default = ast.unparse(node.args[3]) if len(node.args) > 3 else "None"
            argparse_default = UPSTREAM_OPTIONS.get(key, {}).get("default_src", "None")
            if "os.environ" in argparse_default:
                classes[key] = "env_first_scalar"
            elif "os.environ" in own_default:
                classes[key] = "config_env_default"
            else:
                classes[key] = "config_only_derived"
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "config"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            key = node.args[0].value
            default = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
            classes[key] = "sync_docdb" if "env_bool" in default else "env_config_default"
    return classes


_MISSING = object()


def pipeline_env_default(env_key: str, fallback=_MISSING):
    """``os.environ.get("<env_key>", <literal>)``'s literal, read out of source.

    Parsed, never executed: this file reads the pipeline, it does not trust it
    to run. Non-literal defaults (``str(MIRROR)``, ``DEFAULT_HISTORY_RUN``) have
    no constant to find, so the caller supplies the equivalent.
    """

    for node in ast.walk(MAIN_AST):
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
        ):
            return second.value
    if fallback is not _MISSING:
        return fallback
    raise AssertionError(f"no literal default found for {env_key} in the pipeline")


# ── the oracle: upstream's own primitives, re-composed line by line ─────────

NONE_DEFAULT_DESTS = tuple(
    dest for dest, entry in UPSTREAM_OPTIONS.items() if entry["default_src"] == "None"
)


def scheduled_namespace(env: dict) -> argparse.Namespace:
    """What ``parser.parse_args()`` produces for the handoff's argv.

    The handoff pins argv to ``--config/--run-name/--date``, so every other
    option keeps its declared default. Most are a plain ``None`` — those are
    read straight off the parser declaration so a new option cannot be missed.
    The rest are transcribed, and there are only two kinds: ``store_true``
    switches (false, because the flag is absent) and the handful whose default
    *expression reads the environment*, which is exactly why those settings
    resolve env-first.
    """

    namespace = argparse.Namespace()
    for dest in NONE_DEFAULT_DESTS:
        setattr(namespace, dest, None)
    namespace.source_dir = []
    namespace.no_publish_mirror = False
    namespace.sync_docdb = False
    namespace.sync_dry_run = False
    namespace.experimental_cloud_first = False
    namespace.experimental_cloud_query_catalog = False
    namespace.app_key = env.get("CWORK_APP_KEY") or env.get("XG_BIZ_API_KEY") or ""
    namespace.ai_record_model = env.get("CWK_AI_RECORD_MODEL")
    namespace.ai_cluster_model = env.get("CWK_AI_CLUSTER_MODEL")
    namespace.ai_quality_model = env.get("CWK_AI_QUALITY_MODEL")
    namespace.ai_max_parallel = (
        int(env["CWK_AI_MAX_PARALLEL"]) if env.get("CWK_AI_MAX_PARALLEL") else None
    )
    namespace.ai_timeout_seconds = (
        int(env["CWK_AI_TIMEOUT_SECONDS"]) if env.get("CWK_AI_TIMEOUT_SECONDS") else None
    )
    return namespace


def upstream_resolution(config: dict, env: dict) -> dict:
    """Re-compose the pipeline's own primitives under a scrubbed environment.

    Transcribed statement by statement from ``main()``. ``env_bool`` reads
    ``os.environ`` directly, so the environment has to be replaced rather than
    passed. ``clear=True`` matters twice over: it keeps a stray ``CWK_*`` from
    the developer's shell out of the comparison, and it keeps ``CWORK_APP_KEY``
    out of the resolution entirely.
    """

    D = pipeline_env_default
    with mock.patch.dict(os.environ, dict(env), clear=True):
        args = scheduled_namespace(env)
        cv, eb = N.config_value, N.env_bool
        s: dict = {}

        s["history_run_name"] = cv(
            args, config, "history_run_name",
            os.environ.get("CWK_HISTORY_RUN_NAME", N.DEFAULT_HISTORY_RUN),
        )
        s["detail_cap"] = int(
            cv(args, config, "detail_cap", os.environ.get("CWK_DETAIL_CAP", D("CWK_DETAIL_CAP")))
        )
        s["continuation_cap"] = int(
            cv(args, config, "continuation_cap",
               os.environ.get("CWK_CONTINUATION_CAP", D("CWK_CONTINUATION_CAP")))
        )
        env_backfill = eb("CWK_BACKFILL_ENABLED")
        s["backfill_enabled"] = (
            env_backfill if env_backfill is not None else bool(config.get("backfill_enabled", True))
        )
        s["backfill_cap"] = int(
            cv(args, config, "backfill_cap",
               os.environ.get("CWK_BACKFILL_CAP", D("CWK_BACKFILL_CAP")))
        )
        s["backfill_page_size"] = int(
            cv(args, config, "backfill_page_size",
               os.environ.get("CWK_BACKFILL_PAGE_SIZE", D("CWK_BACKFILL_PAGE_SIZE")))
        )
        s["collection_state_file"] = cv(
            args, config, "collection_state_file",
            os.environ.get(
                "CWK_COLLECTION_STATE_FILE", str(N.PROJECT / "state" / "collection-state.json")
            ),
        )
        env_completeness = eb("CWK_SOURCE_COMPLETENESS")
        s["source_completeness"] = (
            env_completeness
            if env_completeness is not None
            else bool(config.get("source_completeness", True))
        )
        s["source_backfill_max_parallel"] = int(
            cv(args, config, "source_backfill_max_parallel",
               os.environ.get("CWK_SOURCE_BACKFILL_MAX_PARALLEL",
                              D("CWK_SOURCE_BACKFILL_MAX_PARALLEL")))
        )
        s["source_completeness_lookback_days"] = int(
            cv(args, config, "source_completeness_lookback_days",
               os.environ.get("CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS",
                              D("CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS")))
        )
        if (
            s["source_completeness_lookback_days"] < 0
            or s["source_completeness_lookback_days"] > 31
        ):
            raise SystemExit("--source-completeness-lookback-days must be between 0 and 31")
        s["app_key"] = cv(
            args, config, "app_key",
            os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "",
        )
        s["owner_emp_id"] = cv(
            args, config, "owner_emp_id",
            os.environ.get("CWK_OWNER_EMP_ID", D("CWK_OWNER_EMP_ID")),
        )
        s["owner_name"] = cv(
            args, config, "owner_name", os.environ.get("CWK_OWNER_NAME", D("CWK_OWNER_NAME"))
        )
        s["relation_api_base_url"] = cv(
            args, config, "relation_api_base_url",
            os.environ.get("CWK_RELATION_API_BASE_URL", D("CWK_RELATION_API_BASE_URL")),
        )
        s["relation_api_path"] = cv(
            args, config, "relation_api_path",
            os.environ.get("CWK_RELATION_API_PATH", D("CWK_RELATION_API_PATH")),
        )
        s["relation_api_timeout_seconds"] = int(
            cv(args, config, "relation_api_timeout_seconds",
               os.environ.get("CWK_RELATION_API_TIMEOUT_SECONDS",
                              D("CWK_RELATION_API_TIMEOUT_SECONDS")))
        )
        s["docdb_project_id"] = cv(
            args, config, "docdb_project_id", os.environ.get("CWK_DOCDB_PROJECT_ID")
        )
        s["docdb_root_file_id"] = cv(
            args, config, "docdb_root_file_id", os.environ.get("CWK_DOCDB_ROOT_FILE_ID")
        )
        s["mirror_root"] = cv(
            args, config, "mirror_root", os.environ.get("CWK_MIRROR_ROOT", str(N.MIRROR))
        )
        # `if not args.sync_docdb:` — the flag is absent, so always taken.
        s["sync_docdb"] = bool(config.get("sync_docdb", eb("CWK_SYNC_DOCDB") or False))
        env_ai_enabled = eb("CWK_AI_ENABLED")
        s["ai_enabled"] = (
            env_ai_enabled if env_ai_enabled is not None else bool(config.get("ai_enabled", False))
        )
        env_ai_dry_run = eb("CWK_AI_DRY_RUN")
        s["ai_dry_run"] = (
            env_ai_dry_run if env_ai_dry_run is not None else bool(config.get("ai_dry_run", False))
        )
        s["ai_record_model"] = cv(
            args, config, "ai_record_model",
            os.environ.get("CWK_AI_RECORD_MODEL", D("CWK_AI_RECORD_MODEL")),
        )
        s["ai_cluster_model"] = cv(
            args, config, "ai_cluster_model",
            os.environ.get("CWK_AI_CLUSTER_MODEL", D("CWK_AI_CLUSTER_MODEL")),
        )
        s["ai_quality_model"] = cv(
            args, config, "ai_quality_model",
            os.environ.get("CWK_AI_QUALITY_MODEL", D("CWK_AI_QUALITY_MODEL")),
        )
        s["ai_max_parallel"] = int(
            cv(args, config, "ai_max_parallel",
               os.environ.get("CWK_AI_MAX_PARALLEL", D("CWK_AI_MAX_PARALLEL")))
        )
        s["ai_timeout_seconds"] = int(
            cv(args, config, "ai_timeout_seconds",
               os.environ.get("CWK_AI_TIMEOUT_SECONDS", D("CWK_AI_TIMEOUT_SECONDS")))
        )
        env_sync_wiki = eb("CWK_SYNC_WIKI")
        s["sync_wiki"] = (
            env_sync_wiki if env_sync_wiki is not None else bool(config.get("sync_wiki", False))
        )
        env_wiki_compile = eb("CWK_WIKI_COMPILE")
        s["wiki_compile"] = (
            env_wiki_compile
            if env_wiki_compile is not None
            else bool(config.get("wiki_compile", s["sync_wiki"]))
        )
        env_wiki_te = eb("CWK_WIKI_TOPICS_ENTITIES")
        s["wiki_topics_entities"] = (
            env_wiki_te
            if env_wiki_te is not None
            else bool(config.get("wiki_topics_entities", s["sync_wiki"] or s["wiki_compile"]))
        )
        env_wiki_sync = eb("CWK_WIKI_SYNC")
        default_wiki_sync = bool(
            s["sync_docdb"] and (s["wiki_compile"] or s["wiki_topics_entities"])
        )
        s["wiki_sync"] = (
            env_wiki_sync
            if env_wiki_sync is not None
            else bool(config.get("wiki_sync", default_wiki_sync))
        )
        s["wiki_mirror_root"] = cv(args, config, "wiki_mirror_root", s["mirror_root"])
        s["wiki_model"] = cv(
            args, config, "wiki_model",
            os.environ.get("CWK_CLOUD_WIKI_MODEL", D("CWK_CLOUD_WIKI_MODEL")),
        )
        s["wiki_repair_model"] = cv(
            args, config, "wiki_repair_model",
            os.environ.get("CWK_CLOUD_WIKI_REPAIR_MODEL", D("CWK_CLOUD_WIKI_REPAIR_MODEL")),
        )
        s["wiki_limit"] = int(
            cv(args, config, "wiki_limit", os.environ.get("CWK_WIKI_LIMIT", D("CWK_WIKI_LIMIT")))
        )
        s["wiki_max_parallel"] = int(
            cv(args, config, "wiki_max_parallel",
               os.environ.get("CWK_WIKI_MAX_PARALLEL", D("CWK_WIKI_MAX_PARALLEL")))
        )
        env_wiki_refine = eb("CWK_WIKI_REFINE_FALLBACKS")
        s["wiki_refine_fallbacks"] = (
            env_wiki_refine
            if env_wiki_refine is not None
            else bool(config.get("wiki_refine_fallbacks", False))
        )
        s["wiki_timeout_seconds"] = int(
            cv(args, config, "wiki_timeout_seconds",
               os.environ.get("CWK_WIKI_TIMEOUT_SECONDS", D("CWK_WIKI_TIMEOUT_SECONDS")))
        )
        env_wiki_best_effort = eb("CWK_WIKI_BEST_EFFORT")
        s["wiki_best_effort"] = (
            env_wiki_best_effort
            if env_wiki_best_effort is not None
            else bool(config.get("wiki_best_effort", False))
        )
        env_cloud_first = eb("CWK_CLOUD_FIRST")
        s["cloud_first"] = (
            env_cloud_first
            if env_cloud_first is not None
            else bool(config.get("cloud_first", False))
        )
        env_publish_catalog = eb("CWK_PUBLISH_CLOUD_QUERY_CATALOG")
        s["publish_cloud_query_catalog"] = (
            env_publish_catalog
            if env_publish_catalog is not None
            else bool(config.get("publish_cloud_query_catalog", False))
        )
        # The scheduled argv carries neither --experimental-* unlock, so this
        # is upstream's own verdict on whether that command can start at all.
        N.enforce_cloud_pause(
            cloud_first=bool(s["cloud_first"]),
            experimental_cloud_first=False,
            publish_cloud_query_catalog=bool(s["publish_cloud_query_catalog"]),
            experimental_cloud_query_catalog=False,
        )

    # Upstream leaves the two DocDB identifiers as ``None`` when neither the
    # config nor the environment supplies one; the contract normalises absent
    # text to "". Same meaning, and only for text that upstream never compares.
    return {k: ("" if v is None else v) for k, v in s.items()}


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
    # ── the second publication channel ──
    "wiki_sync on with sync_docdb off": ({"sync_docdb": False, "wiki_sync": True}, {}),
    "wiki_sync from the environment alone": ({}, {"CWK_WIKI_SYNC": "y"}),
    "wiki_sync config false beats the derived default": (
        {"sync_docdb": True, "sync_wiki": True, "wiki_sync": False},
        {},
    ),
    "sync_wiki drags compile and topics along": ({"sync_wiki": True}, {}),
    "sync_wiki plus sync_docdb derives wiki_sync on": (
        {"sync_wiki": True, "sync_docdb": True},
        {},
    ),
    "wiki_compile alone still derives topics": ({"wiki_compile": True}, {}),
    "wiki_compile off overrides sync_wiki": ({"sync_wiki": True, "wiki_compile": False}, {}),
    "wiki_topics_entities off with compile on": (
        {"wiki_compile": True, "wiki_topics_entities": False},
        {},
    ),
    "wiki env switches beat wiki config": (
        {"sync_wiki": True, "wiki_compile": True},
        {"CWK_SYNC_WIKI": "0", "CWK_WIKI_COMPILE": "no"},
    ),
    "wiki_mirror_root follows mirror_root": ({"mirror_root": "knowledge/工作协同镜像"}, {}),
    # ── the settings whose argparse default reads the environment ──
    "ai models from the environment beat the config": (
        {"ai_record_model": "config/model"},
        {"CWK_AI_RECORD_MODEL": "env/model"},
    ),
    "ai_max_parallel env beats config": ({"ai_max_parallel": 2}, {"CWK_AI_MAX_PARALLEL": "8"}),
    "ai_max_parallel config when env is unset": ({"ai_max_parallel": 2}, {}),
    "ai_timeout_seconds env beats config": (
        {"ai_timeout_seconds": 30},
        {"CWK_AI_TIMEOUT_SECONDS": "600"},
    ),
    "ai enabled and not dry run": ({"ai_enabled": True, "ai_dry_run": False}, {}),
    "ai enabled env beats config off": ({"ai_enabled": False}, {"CWK_AI_ENABLED": "y"}),
    # ── the rest of the surface, so a silent omission shows up here too ──
    "owner and docdb identifiers from config": (
        {"owner_emp_id": "E1", "docdb_project_id": "P1", "docdb_root_file_id": "F1"},
        {},
    ),
    "relation api from the environment": (
        {},
        {"CWK_RELATION_API_BASE_URL": "https://example.test", "CWK_RELATION_API_PATH": "/a/b"},
    ),
    "wiki knobs from the environment": (
        {},
        {
            "CWK_WIKI_LIMIT": "9",
            "CWK_WIKI_MAX_PARALLEL": "3",
            "CWK_WIKI_TIMEOUT_SECONDS": "42",
            "CWK_WIKI_BEST_EFFORT": "y",
            "CWK_WIKI_REFINE_FALLBACKS": "on",
        },
    ),
    "history run name and collection state": (
        {"history_run_name": "run-a"},
        {"CWK_COLLECTION_STATE_FILE": "state/collection-state.json"},
    ),
    "everything at once": (
        {"detail_cap": 11, "sync_docdb": True, "backfill_enabled": False, "sync_wiki": True},
        {"CWK_DETAIL_CAP": "99", "CWK_SYNC_DOCDB": "no", "CWK_BACKFILL_ENABLED": "y"},
    ),
}


class SanitizedPipelineLoadTests(unittest.TestCase):
    """Finding 3: this file must not be a credential-ingestion path."""

    def test_the_test_process_environment_was_not_touched_by_the_import(self):
        self.assertEqual(dict(os.environ), ENVIRONMENT_AT_IMPORT)

    def test_the_sanitized_module_cannot_load_a_dot_env_at_all(self):
        with self.assertRaises(AssertionError):
            N.load_local_env(PROJECT / ".env")

    def test_the_module_body_no_longer_contains_the_ingestion_call(self):
        """If upstream adds a second call, the loader refuses rather than guesses."""

        calls = [
            node
            for node in ast.parse(PIPELINE_SOURCE).body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "load_local_env"
        ]
        self.assertEqual(len(calls), 1, "the sanitizer is pinned to exactly one call site")

    def test_no_credential_shaped_variable_reached_this_process(self):
        """A .env leak would show up as a key the runner never exported."""

        for name in ("CWORK_APP_KEY", "XG_BIZ_API_KEY"):
            with self.subTest(name=name):
                self.assertEqual(
                    os.environ.get(name), ENVIRONMENT_AT_IMPORT.get(name),
                    "loading the pipeline introduced or changed a credential variable",
                )


class RegistryCompletenessTests(unittest.TestCase):
    """BL-1: the registry may not silently omit a behaviour-changing key.

    Every expectation here is derived from upstream's source or from the
    shipped template — never from ``NIGHTLY_SETTINGS``. A hand-maintained list
    checked against itself is not a completeness mechanism.
    """

    def test_every_config_key_the_pipeline_reads_is_modelled(self):
        self.assertEqual(
            upstream_config_keys(),
            set(C.NIGHTLY_SETTING_KEYS),
            "cwk_nightly_pipeline reads a config key the execution contract does not "
            "model (or models one it no longer reads); add it to NIGHTLY_SETTINGS with "
            "its precedence, kind and impact before the contract may describe a run",
        )

    def test_every_environment_variable_is_attributed_to_its_setting(self):
        for setting in C.NIGHTLY_SETTINGS:
            with self.subTest(setting=setting.key):
                statements = upstream_statements_for(setting.key)
                self.assertTrue(statements, f"main() never assigns args.{setting.key}")
                upstream_env: list[str] = []
                for statement in statements:
                    for name in _env_names(statement):
                        if name not in upstream_env:
                            upstream_env.append(name)
                self.assertEqual(
                    sorted(upstream_env),
                    sorted(setting.env_keys),
                    f"the environment variables that decide {setting.key} upstream are "
                    "not the ones the registry says",
                )

    def test_every_precedence_class_matches_the_upstream_shape(self):
        self.assertEqual(
            upstream_precedence(),
            {setting.key: setting.precedence for setting in C.NIGHTLY_SETTINGS},
        )

    def test_the_environment_name_index_covers_the_whole_registry(self):
        """``NIGHTLY_ENV_KEYS`` decides which ``.env`` names are even counted.

        The contract reports *how many* modelled variables a ``.env`` sets, and
        deliberately reports nothing else about the file — no names, no values.
        If this index went stale, that count would silently under-report the
        very thing it exists to surface, and the omission would look like "the
        file contains nothing relevant".
        """

        self.assertEqual(
            set(C.NIGHTLY_ENV_KEYS),
            {key for setting in C.NIGHTLY_SETTINGS for key in setting.env_keys},
        )
        for setting in C.NIGHTLY_SETTINGS:
            for statement in upstream_statements_for(setting.key):
                for name in _env_names(statement):
                    with self.subTest(name=name):
                        self.assertIn(name, C.NIGHTLY_ENV_KEYS)

    def test_the_command_line_only_switches_are_all_pinned(self):
        """Options with no config key still change the run — they must be stated."""

        cli_only = {
            dest
            for dest in UPSTREAM_OPTIONS
            if dest not in ARGV_DESTS and dest not in set(C.NIGHTLY_SETTING_KEYS)
        }
        pinned = {flag[2:].replace("-", "_") for flag in C.NIGHTLY_CLI_ONLY_FIXED}
        self.assertEqual(
            cli_only,
            pinned,
            "cwk_nightly_pipeline has a command-line option that is neither a modelled "
            "config key nor declared in NIGHTLY_CLI_ONLY_FIXED; the contract would be "
            "silent about what the scheduled run does with it",
        )

    def test_the_shipped_template_only_contains_modelled_keys(self):
        template = json.loads(CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8"))
        allowed = set(C.NIGHTLY_SETTING_KEYS) | set(C.ACTIVATION_CONFIG_KEYS)
        self.assertEqual(
            set(template) - allowed,
            set(),
            "the shipped config template offers a key the execution contract cannot "
            "describe; a user following the template would be asked to confirm a "
            "contract that omits something they configured",
        )

    def test_the_shipped_template_resolves_without_a_refusal(self):
        """The documented example must be describable, not merely tolerated."""

        template = json.loads(CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8"))
        resolved = C.resolve_nightly_runtime(template, {})
        self.assertEqual(set(resolved["settings"]), set(C.NIGHTLY_SETTING_KEYS))

    def test_an_unmodelled_config_key_is_refused_rather_than_ignored(self):
        """The runtime half of the completeness mechanism."""

        with self.assertRaises(C.NightlyConfigError) as caught:
            C.resolve_nightly_runtime({"some_future_switch": True}, {})
        # The key name is the caller's string; it must not be echoed back into
        # a message an agent will read aloud.
        self.assertNotIn("some_future_switch", str(caught.exception))

    def test_the_wizard_only_config_keys_are_ignored_by_the_pipeline(self):
        """They are allowed precisely because upstream never reads them."""

        for key in C.ACTIVATION_CONFIG_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, upstream_config_keys())


class NightlyResolutionEquivalenceTests(unittest.TestCase):
    """The copy and the original must answer identically, case by case."""

    def test_every_case_resolves_the_same_way(self):
        for name, (config, env) in CASES.items():
            with self.subTest(case=name):
                mine = C.resolve_nightly_runtime(config, env)["settings"]
                theirs = upstream_resolution(config, env)
                self.assertEqual(mine, theirs, name)

    def test_every_case_covers_the_whole_registry(self):
        """A case set that only touched eight keys is how BL-1 stayed hidden."""

        resolved = C.resolve_nightly_runtime({}, {})["settings"]
        self.assertEqual(set(resolved), set(C.NIGHTLY_SETTING_KEYS))
        self.assertEqual(set(upstream_resolution({}, {})), set(C.NIGHTLY_SETTING_KEYS))

    def test_the_two_agree_on_the_full_boolean_vocabulary(self):
        """Every spelling, and a few that are deliberately not accepted."""

        spellings = (
            "1", "true", "TRUE", "yes", "y", "Y", "on", "ON", " on ",
            "0", "false", "no", "n", "off", "", "maybe", "2", "true-ish",
        )
        switches = (
            ("CWK_SYNC_DOCDB", "sync_docdb"),
            ("CWK_BACKFILL_ENABLED", "backfill_enabled"),
            ("CWK_SOURCE_COMPLETENESS", "source_completeness"),
            ("CWK_SYNC_WIKI", "sync_wiki"),
            ("CWK_WIKI_COMPILE", "wiki_compile"),
            ("CWK_WIKI_TOPICS_ENTITIES", "wiki_topics_entities"),
            ("CWK_WIKI_SYNC", "wiki_sync"),
            ("CWK_AI_ENABLED", "ai_enabled"),
            ("CWK_AI_DRY_RUN", "ai_dry_run"),
            ("CWK_WIKI_BEST_EFFORT", "wiki_best_effort"),
            ("CWK_WIKI_REFINE_FALLBACKS", "wiki_refine_fallbacks"),
        )
        for value in spellings:
            for env_key, setting in switches:
                with self.subTest(value=value, setting=setting):
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
                contract = build(config, env)
                expected = upstream_resolution(config, env)
                self.assertEqual(contract["publishing"]["sync_docdb"], expected["sync_docdb"])
                self.assertEqual(contract["publishing"]["wiki_sync"], expected["wiki_sync"])
                self.assertEqual(contract["caps"]["detail_cap"], expected["detail_cap"])
                self.assertEqual(
                    contract["caps"]["continuation_cap"], expected["continuation_cap"]
                )
                self.assertEqual(contract["caps"]["backfill_cap"], expected["backfill_cap"])
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
                self.assertEqual(
                    contract["wiki_pipeline"]["compile"], expected["wiki_compile"]
                )
                self.assertEqual(
                    contract["wiki_pipeline"]["topics_entities"],
                    expected["wiki_topics_entities"],
                )
                self.assertEqual(contract["ai_processing"]["enabled"], expected["ai_enabled"])

    def test_a_sync_docdb_env_value_is_never_silently_dropped(self):
        """The named defect, stated on its own so it cannot regress quietly."""

        contract = build({}, {"CWK_SYNC_DOCDB": "y"})
        self.assertTrue(contract["publishing"]["sync_docdb"])
        self.assertEqual(contract["runtime_resolution"]["sources"]["sync_docdb"], "shell")

    def test_an_unparsable_integer_is_refused_rather_than_defaulted(self):
        """Upstream would abort on ``int("lots")``; the contract must not invent a number."""

        for config, env in (
            ({"detail_cap": "lots"}, {}),
            ({}, {"CWK_DETAIL_CAP": "lots"}),
            ({"backfill_cap": None}, {}),
            ({"wiki_limit": "many"}, {}),
            ({}, {"CWK_AI_MAX_PARALLEL": "several"}),
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

    def test_the_documented_divergences_are_all_refusals(self):
        """Where the copy differs from upstream it must fail closed, never guess.

        Three places do differ on purpose. In each, upstream would happily run
        and the contract would have to describe something the user could not
        check: an integer conjured out of ``True``, a credential copied into a
        document that gets hashed and read aloud, or a value that is not
        renderable at all. Refusing is the only answer that keeps the document
        and the run the same thing.
        """

        for config in (
            {"detail_cap": True},          # upstream: int(True) == 1
            {"app_key": "sk-not-a-real-key"},  # upstream: happily uses it
            {"owner_emp_id": "a\nb"},      # upstream: passes it through
        ):
            with self.subTest(config=config):
                with self.assertRaises(C.NightlyConfigError):
                    C.resolve_nightly_runtime(config, {})


class PausedPathTests(unittest.TestCase):
    """BL-1: a path the scheduled command cannot walk must be refused up front.

    ``enforce_cloud_pause`` demands an ``--experimental-*`` unlock that the
    handoff's argv cannot supply, so a config with ``cloud_first`` on produces a
    task that exits at startup every night. Rendering a contract for it would
    ask the user to confirm an automation that never runs — and, worse, one
    whose contract claims publishing settings that never take effect.
    """

    def test_upstream_really_would_refuse_the_scheduled_command(self):
        """The premise, checked against upstream's own function."""

        for kwargs in (
            {"cloud_first": True, "publish_cloud_query_catalog": False},
            {"cloud_first": False, "publish_cloud_query_catalog": True},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(SystemExit):
                    N.enforce_cloud_pause(
                        experimental_cloud_first=False,
                        experimental_cloud_query_catalog=False,
                        **kwargs,
                    )

    def test_each_paused_path_is_refused_from_config_and_from_the_environment(self):
        for config, env in (
            ({"cloud_first": True}, {}),
            ({}, {"CWK_CLOUD_FIRST": "y"}),
            ({"publish_cloud_query_catalog": True}, {}),
            ({}, {"CWK_PUBLISH_CLOUD_QUERY_CATALOG": "on"}),
        ):
            with self.subTest(config=config, env=env):
                with self.assertRaises(C.UnschedulableNightlySetting):
                    C.resolve_nightly_runtime(config, env)
                with self.assertRaises(C.UnschedulableNightlySetting):
                    build(config, env)

    def test_the_refusal_names_the_setting_without_quoting_the_config(self):
        with self.assertRaises(C.UnschedulableNightlySetting) as caught:
            C.resolve_nightly_runtime({"cloud_first": True}, {})
        message = str(caught.exception)
        self.assertIn("cloud_first", message)
        self.assertNotIn(str(PROJECT), message)

    def test_turning_the_paused_path_off_again_is_accepted(self):
        """The refusal must be actionable, not a dead end."""

        resolved = C.resolve_nightly_runtime({"cloud_first": False}, {})
        self.assertFalse(resolved["settings"]["cloud_first"])


# An empty directory that stands in for "a project root with no .env". Without
# it every contract built here would be resolved against whatever ``.env`` the
# developer running the suite happens to have, and the expected hashes below
# would quietly become machine-dependent. Tests that are *about* the ``.env``
# layer point ``project_env_root`` at their own fixture instead.
NO_DOT_ENV = Path(tempfile.mkdtemp(prefix="rt032-no-dotenv-"))


def build(config, env, **overrides):
    kwargs = {
        "config": config,
        "env": env,
        "project_env_root": NO_DOT_ENV,
        "profile_sha256": "a" * 64,
        "run_at_local": "02:30",
        "timezone": "Asia/Shanghai",
        "generated_at": "2026-09-02T00:00:00Z",
    }
    kwargs.update(overrides)
    return C.build_execution_contract(**kwargs)


def handoff_for(contract):
    return C.build_scheduler_handoff(
        contract=contract,
        contract_sha256=contract["contract_sha256"],
        profile_sha256="a" * 64,
        pilot_receipt_sha256="b" * 64,
        config_path=PROJECT / "cwk-mirror.local.json",
        project_root=PROJECT,
        generated_at="2026-09-02T00:00:00Z",
    )


class WikiPublicationTests(unittest.TestCase):
    """BL-1's reproduction, pinned end to end.

    ``{"sync_docdb": false, "wiki_sync": true}`` used to produce a contract that
    said publishing was off, a Markdown rendering that read the same aloud, a
    valid handoff, and — the part that made it unrecoverable — *the same
    contract hash* as ``wiki_sync: false``. So the user's yes covered both, and
    flipping the switch afterwards would not even void the gate.
    """

    OFF = {"sync_docdb": False, "wiki_sync": False}
    ON = {"sync_docdb": False, "wiki_sync": True}

    def test_the_contract_states_the_second_channel(self):
        contract = build(self.ON, {})
        publishing = contract["publishing"]
        self.assertFalse(publishing["sync_docdb"])
        self.assertTrue(publishing["wiki_sync"])
        self.assertTrue(publishing["any_external_publication"])
        self.assertEqual(publishing["targets"], ["docdb:wiki"])

    def test_publishing_off_really_means_nothing_leaves_the_machine(self):
        contract = build(self.OFF, {})
        publishing = contract["publishing"]
        self.assertFalse(publishing["any_external_publication"])
        self.assertEqual(publishing["targets"], [])

    def test_the_rendered_markdown_says_it_out_loud(self):
        on = C.render_contract_markdown(build(self.ON, {}))
        off = C.render_contract_markdown(build(self.OFF, {}))
        self.assertIn("wiki/ 发布到 DocDB：开", on)
        self.assertIn("本次是否有内容离开这台机器：是", on)
        self.assertIn("wiki/ 发布到 DocDB：关", off)
        self.assertIn("本次是否有内容离开这台机器：否", off)

    def test_flipping_it_changes_the_hash_and_therefore_voids_consent(self):
        self.assertNotEqual(
            build(self.ON, {})["contract_sha256"],
            build(self.OFF, {})["contract_sha256"],
            "wiki_sync must move the contract hash, otherwise a yes given for "
            "'nothing is published' silently covers a night that publishes",
        )

    def test_every_registry_key_moves_the_hash(self):
        """Not just wiki_sync — anything the run honours has to be in the digest."""

        base_contract = build({}, {})
        baseline = base_contract["contract_sha256"]
        resolved = C.resolve_nightly_runtime({}, {})["settings"]
        for setting in C.NIGHTLY_SETTINGS:
            if setting.key in C.PAUSED_NIGHTLY_PATHS:
                continue  # refused outright; covered by PausedPathTests
            with self.subTest(setting=setting.key):
                config, env = _perturb(setting, resolved)
                self.assertNotEqual(
                    build(config, env)["contract_sha256"],
                    baseline,
                    f"changing {setting.key} left the contract hash untouched, so an "
                    "earlier confirmation would still look valid",
                )

    def test_the_handoff_is_still_produced_and_still_carries_no_extra_flag(self):
        """Publishing more must not quietly become 'unschedulable'."""

        handoff = handoff_for(build(self.ON, {}))
        self.assertEqual(handoff["command_spec"]["env_allowlist"], ["CWORK_APP_KEY"])

    def test_the_alias_chain_reaches_the_same_publication_verdict(self):
        """``sync_wiki`` alone turns on compile, topics and — with docdb — sync."""

        contract = build({"sync_wiki": True, "sync_docdb": True}, {})
        self.assertTrue(contract["wiki_pipeline"]["compile"])
        self.assertTrue(contract["wiki_pipeline"]["topics_entities"])
        self.assertTrue(contract["wiki_pipeline"]["sync_to_docdb"])
        self.assertEqual(
            contract["publishing"]["targets"], ["docdb:daily_and_runs", "docdb:wiki"]
        )

    def test_a_wiki_compile_only_config_publishes_nothing(self):
        contract = build({"sync_wiki": True, "sync_docdb": False}, {})
        self.assertTrue(contract["wiki_pipeline"]["compile"])
        self.assertFalse(contract["publishing"]["any_external_publication"])


def _perturb(setting, resolved: dict) -> tuple[dict, dict]:
    """A ``(config, env)`` pair that changes ``setting`` away from its default.

    Returned as a pair because one setting cannot be moved through the config
    file at all: a non-empty ``app_key`` there is refused by design, so the only
    honest way to change it is the environment variable the scheduled task is
    actually given.
    """

    if setting.kind == "secret":
        return {}, {setting.env_keys[0]: "value-not-echoed"}
    if setting.kind == "bool":
        return {setting.key: not resolved[setting.key]}, {}
    if setting.kind == "int":
        return {setting.key: 7 if resolved[setting.key] != 7 else 3}, {}
    if setting.kind == "url":
        return {setting.key: "https://example.test"}, {}
    if setting.kind == "path":
        return {setting.key: "knowledge"}, {}
    return {setting.key: "perturbed-value"}, {}


class ScheduledEnvironmentTests(unittest.TestCase):
    """A contract resolved from the shell describes a run that will not happen.

    The handoff's ``env_allowlist`` is ``["CWORK_APP_KEY"]``. The scheduled task
    therefore sees no ``CWK_*`` switch at all, so any setting that came from the
    current shell would resolve differently at 02:30 — the user would have
    confirmed a document describing a different run.
    """

    def contract(self, config, env):
        return build(config, env)

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

    def test_a_shell_sourced_wiki_switch_is_named_too(self):
        """The check has to cover the whole registry, not the old eight keys."""

        contract = self.contract({}, {"CWK_WIKI_SYNC": "y"})
        self.assertEqual(
            contract["runtime_resolution"]["settings_requiring_shell_environment"],
            ["wiki_sync"],
        )
        with self.assertRaises(C.ScheduledEnvironmentMismatch):
            handoff_for(contract)

    def test_the_credential_variable_is_not_mistaken_for_a_shell_dependency(self):
        """``CWORK_APP_KEY`` is on the allowlist; the scheduled task does get it."""

        contract = self.contract({}, {"CWORK_APP_KEY": "value-not-echoed"})
        self.assertTrue(contract["runtime_resolution"]["scheduled_environment_equivalent"])
        self.assertNotIn("value-not-echoed", json.dumps(contract, ensure_ascii=False))
        self.assertEqual(contract["settings"]["app_key"], {"state": "set"})

    def test_an_environment_value_matching_the_default_is_not_flagged(self):
        """Flagging every ``CWK_*`` would cry wolf; only a real difference counts."""

        contract = self.contract({}, {"CWK_DETAIL_CAP": str(C.DEFAULT_DETAIL_CAP)})
        self.assertTrue(contract["runtime_resolution"]["scheduled_environment_equivalent"])

    def test_the_handoff_is_refused_while_the_contract_depends_on_the_shell(self):
        contract = self.contract({}, {"CWK_SYNC_DOCDB": "y"})
        with self.assertRaises(C.ScheduledEnvironmentMismatch):
            handoff_for(contract)

    def test_the_same_setting_moved_into_the_config_is_accepted(self):
        """The refusal has to be actionable, not a dead end."""

        handoff = handoff_for(self.contract({"sync_docdb": True}, {}))
        self.assertEqual(handoff["command_spec"]["env_allowlist"], ["CWORK_APP_KEY"])

    def test_the_rendered_markdown_says_where_each_value_came_from(self):
        """The user is read this document; the caveat has to be in it, not only in JSON."""

        text = C.render_contract_markdown(self.contract({}, {"CWK_SYNC_DOCDB": "y"}))
        self.assertIn("## 这些取值从哪里来", text)
        self.assertIn("sync_docdb = `True` ← 当前 shell 的环境变量", text)
        self.assertIn("警告", text)
        self.assertIn("CWORK_APP_KEY", text)

    def test_the_provenance_section_lists_every_modelled_setting(self):
        text = C.render_contract_markdown(self.contract({}, {}))
        for key in C.NIGHTLY_SETTING_KEYS:
            with self.subTest(key=key):
                self.assertRegex(text, rf"(?m)^- {re.escape(key)} = ")

    def test_a_config_only_contract_carries_no_warning(self):
        text = C.render_contract_markdown(self.contract({"sync_docdb": True}, {}))
        self.assertIn("sync_docdb = `True` ← 配置文件", text)
        self.assertNotIn("警告", text)

    def test_the_markdown_never_echoes_a_person_or_a_credential(self):
        contract = self.contract(
            {"owner_name": "示例姓名"}, {"CWORK_APP_KEY": "sk-not-a-real-key"}
        )
        text = C.render_contract_markdown(contract)
        self.assertNotIn("示例姓名", text)
        self.assertNotIn("sk-not-a-real-key", text)
        self.assertIn("owner_name = `set`", text)

    def test_the_markdown_ties_the_lookback_to_the_completeness_switch(self):
        """A lookback window that never runs is a false promise."""

        text = C.render_contract_markdown(self.contract({"source_completeness": False}, {}))
        self.assertIn("来源完整性补采：关", text)
        self.assertIn("补采已关，本项不生效", text)


# ── the .env layer upstream loads before it resolves anything ───────────────
#
# ``cwk_nightly_pipeline`` runs ``load_local_env(PROJECT / ".env")`` in its
# module body, before ``main`` exists to be called. Every setting is therefore
# resolved against an environment that has already been topped up from a
# gitignored file nobody passes on the command line and nobody sees in the
# config. Until this section existed, the contract modelled the shell and
# nothing else — so a one-line edit to ``.env`` could turn on publication while
# the contract hash, the drift check and the scheduled-equivalence verdict all
# stayed exactly the same. That is the whole blind spot, stated as tests.

# Each entry is a ``.env`` body whose result a reasonable person could get
# wrong. The oracle is upstream's own parser, so "expected" is never written
# down here — only the claim in the name.
DOT_ENV_TEXTS = {
    "empty file": "",
    "one plain assignment": "CWK_SYNC_DOCDB=1\n",
    "spaces around the equals sign": "CWK_SYNC_DOCDB = 1\n",
    "export prefix (not honoured upstream)": "export CWK_SYNC_DOCDB=1\n",
    "comment lines and blanks": "# CWK_SYNC_DOCDB=1\n\n   \nCWK_DETAIL_CAP=9\n",
    "a comment after leading whitespace": "   # CWK_SYNC_DOCDB=1\n",
    "empty value is a set-but-empty name": "CWK_HISTORY_RUN_NAME=\n",
    "double quoted value": 'CWK_HISTORY_RUN_NAME="quoted"\n',
    "single quoted value": "CWK_HISTORY_RUN_NAME='quoted'\n",
    "single inside double": "CWK_HISTORY_RUN_NAME='\"mixed\"'\n",
    "double inside single": "CWK_HISTORY_RUN_NAME=\"'mixed'\"\n",
    "doubled quotes": 'CWK_HISTORY_RUN_NAME=""double""\n',
    "duplicate name, first one wins": "CWK_DETAIL_CAP=1\nCWK_DETAIL_CAP=2\n",
    "line with no equals sign": "CWK_SYNC_DOCDB\n",
    "empty key": "=value\n",
    "key that does not start with a letter": "1BAD=x\nCWK_DETAIL_CAP=9\n",
    "key with a dash": "CWK-SYNC-DOCDB=1\nCWK_DETAIL_CAP=9\n",
    "value containing more equals signs": "CWK_RELATION_API_PATH=a=b=c\n",
    "value padded with spaces": "CWK_HISTORY_RUN_NAME=  padded  \n",
    "vertical tab counts as a line break": "CWK_DETAIL_CAP=1\x0bCWK_BACKFILL_CAP=2\n",
    "unicode line separator counts too": "CWK_DETAIL_CAP=1 CWK_BACKFILL_CAP=2\n",
    "crlf line endings": "CWK_DETAIL_CAP=1\r\nCWK_BACKFILL_CAP=2\r\n",
    "no trailing newline": "CWK_DETAIL_CAP=9",
    "credentials belonging to other tools": (
        "OPENAI_API_KEY=sk-not-a-real-key\n"
        "AWS_SECRET_ACCESS_KEY=also-not-real\n"
        "DATABASE_URL=postgres://user:pw@host/db\n"
    ),
    "the app key upstream really does read": "CWORK_APP_KEY=not-a-real-key\n",
    "the whole publication chain": "CWK_SYNC_DOCDB=yes\nCWK_WIKI_SYNC=on\n",
    "an integer setting": "CWK_DETAIL_CAP=42\n",
    "an env-first scalar": "CWK_AI_MAX_PARALLEL=9\n",
    "the app key alias": "XG_BIZ_API_KEY=not-a-real-key\n",
    "a paused experimental path": "CWK_CLOUD_FIRST=1\n",
}


class ProjectEnvParserFidelityTests(unittest.TestCase):
    """The contract's ``.env`` parser must reproduce upstream's, bugs included.

    ``load_local_env`` is not a dotenv library. It drops ``export`` prefixes on
    the floor, strips quote characters one class at a time so ``'"x"'`` and
    ``"'x'"`` come out different, treats ``\\x0b`` as a line break because it
    uses ``str.splitlines()``, and keeps the *first* of two identical names
    because it uses ``setdefault``. A "cleaner" parser here would be a contract
    that describes a different night than the one that runs, which is the same
    class of defect as not reading the file at all.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rt032-dotenv-"))

    def write(self, text: str) -> Path:
        target = self.root / C.PROJECT_ENV_FILE
        target.write_text(text, encoding="utf-8")
        return target

    def test_every_shape_parses_the_way_upstream_parses_it(self):
        for name, text in DOT_ENV_TEXTS.items():
            with self.subTest(case=name):
                path = self.write(text)
                self.assertEqual(
                    C.read_project_env(self.root).values,
                    upstream_load_local_env(path, {}),
                    name,
                )

    def test_the_parser_agrees_when_it_is_handed_the_text_directly(self):
        """``parse_project_env`` and ``read_project_env`` must not diverge."""

        for name, text in DOT_ENV_TEXTS.items():
            with self.subTest(case=name):
                self.write(text)
                self.assertEqual(
                    C.parse_project_env(text), C.read_project_env(self.root).values, name
                )

    def test_the_shell_always_wins_because_upstream_uses_setdefault(self):
        shell = {"CWK_SYNC_DOCDB": "0", "CWK_DETAIL_CAP": "5"}
        path = self.write("CWK_SYNC_DOCDB=1\nCWK_DETAIL_CAP=99\nCWK_BACKFILL_CAP=7\n")
        merged, origin = C.merge_runtime_env(shell, C.read_project_env(self.root).values)
        self.assertEqual(merged, upstream_load_local_env(path, shell))
        self.assertEqual(merged["CWK_SYNC_DOCDB"], "0")
        self.assertEqual(origin["CWK_SYNC_DOCDB"], "shell")
        self.assertEqual(origin["CWK_BACKFILL_CAP"], "project_env")

    def test_the_merge_matches_upstream_over_every_shape(self):
        shell = {"CWK_DETAIL_CAP": "5", "CWORK_APP_KEY": "shell-key-not-echoed"}
        for name, text in DOT_ENV_TEXTS.items():
            with self.subTest(case=name):
                path = self.write(text)
                merged, _ = C.merge_runtime_env(
                    shell, C.read_project_env(self.root).values
                )
                self.assertEqual(merged, upstream_load_local_env(path, shell), name)

    def test_a_missing_file_is_a_normal_state_and_not_an_error(self):
        """Upstream returns immediately when the file is absent; so must this."""

        layer = C.read_project_env(self.root)
        self.assertFalse(layer.present)
        self.assertEqual(layer.values, {})

    def test_an_empty_file_is_present_but_decides_nothing(self):
        self.write("")
        layer = C.read_project_env(self.root)
        self.assertTrue(layer.present)
        self.assertEqual(layer.values, {})

    def test_a_dangling_symlink_is_absent_exactly_as_upstream_sees_it(self):
        """``path.exists()`` is false for a broken link, so upstream returns."""

        (self.root / C.PROJECT_ENV_FILE).symlink_to(self.root / "no-such-file")
        self.assertEqual(C.read_project_env(self.root), C.EMPTY_PROJECT_ENV)

    def test_a_symlink_to_a_regular_file_is_read_because_upstream_reads_it(self):
        (self.root / "real").write_text("CWK_SYNC_DOCDB=1\n", encoding="utf-8")
        (self.root / C.PROJECT_ENV_FILE).symlink_to(self.root / "real")
        self.assertEqual(C.read_project_env(self.root).values, {"CWK_SYNC_DOCDB": "1"})


class ProjectEnvLocationTests(unittest.TestCase):
    """Reading the *wrong* ``.env`` correctly is still a contract that lies.

    Everything else in this file pins what the file means. This class pins
    *which file*, because the parser being perfect buys nothing if the two
    modules disagree about where to look. The expectations are read out of
    upstream's syntax tree, so upstream moving the load — to a different
    filename, a different anchor, or ``cwd`` — fails here rather than silently
    handing the user a contract that describes a file the nightly run will
    never open.
    """

    @staticmethod
    def _module_level_call() -> ast.Call:
        for node in ast.parse(PIPELINE_SOURCE).body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "load_local_env"
            ):
                return node.value
        raise AssertionError("upstream no longer loads a .env in its module body")

    def test_upstream_loads_exactly_the_filename_this_module_looks_for(self):
        call = self._module_level_call()
        self.assertEqual(len(call.args), 1, "unexpected load_local_env() signature")
        arg = call.args[0]
        self.assertIsInstance(arg, ast.BinOp, "expected `<anchor> / <name>`")
        self.assertIsInstance(arg.op, ast.Div)
        self.assertIsInstance(arg.right, ast.Constant)
        self.assertEqual(arg.right.value, C.PROJECT_ENV_FILE)

    def test_the_anchor_is_the_module_constant_this_module_mirrors(self):
        arg = self._module_level_call().args[0]
        self.assertIsInstance(arg.left, ast.Name)
        self.assertEqual(arg.left.id, "PROJECT", "the anchor is no longer PROJECT")

    def test_the_anchor_is_still_the_script_directory_parent_not_the_cwd(self):
        """``Path(__file__).resolve().parents[1]`` — pinned shape, not a guess."""

        assignments = [
            node
            for node in ast.parse(PIPELINE_SOURCE).body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "PROJECT" for t in node.targets
            )
        ]
        self.assertEqual(len(assignments), 1, "PROJECT is assigned more than once")
        self.assertEqual(
            ast.unparse(assignments[0].value), "Path(__file__).resolve().parents[1]"
        )

    def test_both_modules_resolve_that_expression_to_the_same_directory(self):
        """The two files live side by side, so the same expression must agree."""

        self.assertEqual(C._PROJECT_ENV_ROOT, N.PROJECT)
        self.assertEqual(C._PROJECT_ENV_ROOT, PROJECT)

    def test_the_location_is_not_reachable_from_the_command_line(self):
        """A caller-steerable ``.env`` path would be the vulnerability itself.

        ``project_dir`` exists for relativising paths in the handoff. If it
        ever started steering this too, an attacker-supplied directory could
        decide which file the user is shown a contract for.
        """
        source = (PROJECT / "scripts" / "cwk_activation_wizard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("project_env_root", source)
        self.assertNotIn("PROJECT_ENV_FILE", source)


class ProjectEnvRefusalTests(unittest.TestCase):
    """Shapes where upstream cannot start — and where a wrong read would hang.

    ``load_local_env`` calls ``path.read_text()`` after ``path.exists()``.
    Against a FIFO that ``open`` never returns, so the *nightly process* hangs
    at import; against non-UTF-8 bytes it raises ``UnicodeDecodeError`` before
    ``main`` is ever reached. There is no set of settings that honestly
    describes either night, so the contract refuses instead of guessing — and,
    because the wizard must never inherit the hang, it refuses *promptly*.
    """

    #: Generous enough that a slow machine will not trip it, short enough that a
    #: blocking ``open`` cannot be mistaken for slowness.
    BUDGET_SECONDS = 10.0

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rt032-dotenv-bad-"))
        self.target = self.root / C.PROJECT_ENV_FILE

    def assert_refused_promptly(self, label: str):
        started = time.monotonic()
        with self.assertRaises(C.ProjectEnvironmentError) as caught:
            C.read_project_env(self.root)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, self.BUDGET_SECONDS, f"{label} blocked for {elapsed:.1f}s")
        return str(caught.exception)

    def test_a_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.target)
        self.assert_refused_promptly("fifo")

    def test_a_symlink_to_a_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        self.target.symlink_to(self.root / "pipe")
        self.assert_refused_promptly("symlink to fifo")

    def test_a_directory_is_refused(self):
        self.target.mkdir()
        self.assert_refused_promptly("directory")

    def test_a_socket_is_refused(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.target))
        self.assert_refused_promptly("socket")

    def test_a_character_device_is_refused_without_reading_forever(self):
        self.target.symlink_to("/dev/zero")
        self.assert_refused_promptly("character device")

    def test_non_utf8_bytes_are_refused_because_upstream_dies_on_them(self):
        self.target.write_bytes(b"CWK_SYNC_DOCDB=1\n\xff\xfe\n")
        message = self.assert_refused_promptly("non-utf8")
        self.assertIn("UTF-8", message)

    def test_upstream_really_would_die_on_those_bytes(self):
        """The refusal is only honest if upstream truly cannot survive this."""

        self.target.write_bytes(b"\xff\xfe\n")
        with self.assertRaises(UnicodeDecodeError):
            upstream_load_local_env(self.target, {})

    def test_a_refusal_leaks_no_path_no_errno_and_no_file_content(self):
        self.target.write_bytes(b"CWORK_APP_KEY=sk-not-a-real-key\n\xff")
        message = self.assert_refused_promptly("non-utf8")
        self.assertNotIn(str(self.root), message)
        self.assertNotIn("sk-not-a-real-key", message)
        self.assertNotIn("CWORK_APP_KEY", message)
        for token in ("Errno", "errno", "ENXIO", "EACCES", "Traceback"):
            self.assertNotIn(token, message)

    def test_the_refusal_reaches_the_contract_rather_than_a_default(self):
        """Fail closed: no contract at all, not a contract without the layer."""

        os.mkfifo(self.target)
        with self.assertRaises(C.ProjectEnvironmentError):
            build({}, {}, project_env_root=self.root)


# ``(config, shell, .env body)`` triples. The environment upstream resolves
# against is the merge of the last two; the point of each case is that the
# merge, not the shell, is what decides.
DOT_ENV_RESOLUTION_CASES = {
    "publication turned on by the file alone": ({}, {}, "CWK_SYNC_DOCDB=1\n"),
    "the wiki channel turned on by the file alone": (
        {},
        {},
        "CWK_SYNC_DOCDB=1\nCWK_WIKI_SYNC=1\n",
    ),
    "the file loses to the shell": (
        {},
        {"CWK_SYNC_DOCDB": "0"},
        "CWK_SYNC_DOCDB=1\n",
    ),
    "the file wins where the shell is silent": (
        {},
        {"CWK_DETAIL_CAP": "5"},
        "CWK_SYNC_DOCDB=1\n",
    ),
    # config-first keys must stay config-first over the *merged* environment.
    "a config-first integer still beats the file": (
        {"detail_cap": 7},
        {},
        "CWK_DETAIL_CAP=99\n",
    ),
    "a config-first integer still beats file and shell together": (
        {"detail_cap": 7},
        {"CWK_DETAIL_CAP": "50"},
        "CWK_DETAIL_CAP=99\n",
    ),
    "an integer from the file where the config is silent": (
        {},
        {},
        "CWK_DETAIL_CAP=99\n",
    ),
    # env-first keys must stay env-first over the merged environment.
    "an env-first boolean from the file beats the config": (
        {"backfill_enabled": True},
        {},
        "CWK_BACKFILL_ENABLED=0\n",
    ),
    "an env-first scalar from the file beats the config": (
        {"ai_max_parallel": 2},
        {},
        "CWK_AI_MAX_PARALLEL=9\n",
    ),
    "sync_docdb keeps its inverted precedence over the file": (
        {"sync_docdb": False},
        {},
        "CWK_SYNC_DOCDB=1\n",
    ),
    "the boolean vocabulary is unchanged inside the file": (
        {},
        {},
        "CWK_SYNC_DOCDB=y\nCWK_BACKFILL_ENABLED=off\nCWK_SOURCE_COMPLETENESS=ON\n",
    ),
    "an unrecognised word in the file is false, not absent": (
        {"backfill_enabled": True},
        {},
        "CWK_BACKFILL_ENABLED=maybe\n",
    ),
    "quotes are stripped before the value is used": (
        {},
        {},
        'CWK_DETAIL_CAP="42"\n',
    ),
    "a duplicate name in the file keeps the first": (
        {},
        {},
        "CWK_DETAIL_CAP=42\nCWK_DETAIL_CAP=7\n",
    ),
    "an export line changes nothing": ({}, {}, "export CWK_SYNC_DOCDB=1\n"),
    "the app key alias is honoured from the file": (
        {},
        {},
        "XG_BIZ_API_KEY=not-a-real-key\n",
    ),
    "other tools' credentials change nothing": (
        {},
        {},
        "OPENAI_API_KEY=sk-not-a-real-key\nDATABASE_URL=postgres://u:p@h/d\n",
    ),
    "the whole chain from the file": (
        {},
        {},
        "CWK_SYNC_WIKI=1\nCWK_SYNC_DOCDB=1\n",
    ),
}


class ProjectEnvResolutionEquivalenceTests(unittest.TestCase):
    """Resolution over the merged environment must match upstream, case by case.

    This is the completeness oracle extended by one input layer. Both sides get
    the same synthetic ``.env``; upstream's side gets it through its own
    ``load_local_env``, so nothing about the file's semantics is assumed here.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rt032-dotenv-res-"))

    def merged_env(self, shell: dict, text: str) -> tuple[dict, dict]:
        path = self.root / C.PROJECT_ENV_FILE
        path.write_text(text, encoding="utf-8")
        return upstream_load_local_env(path, shell), C.read_project_env(self.root).values

    def test_every_case_resolves_the_same_way_as_upstream(self):
        for name, (config, shell, text) in DOT_ENV_RESOLUTION_CASES.items():
            with self.subTest(case=name):
                upstream_env, layer = self.merged_env(shell, text)
                mine = C.resolve_nightly_runtime(config, shell, layer)["settings"]
                theirs = upstream_resolution(config, upstream_env)
                self.assertEqual(mine, theirs, name)

    def test_the_existing_case_set_is_unchanged_by_an_empty_file(self):
        """A ``.env`` with nothing in it must not move a single answer."""

        for name, (config, env) in CASES.items():
            with self.subTest(case=name):
                _, layer = self.merged_env(env, "# nothing here\n")
                self.assertEqual(
                    C.resolve_nightly_runtime(config, env, layer)["settings"],
                    C.resolve_nightly_runtime(config, env)["settings"],
                    name,
                )

    def test_a_file_sourced_value_is_attributed_to_the_file(self):
        _, layer = self.merged_env({}, "CWK_SYNC_DOCDB=1\nCWK_DETAIL_CAP=42\n")
        sources = C.resolve_nightly_runtime({}, {}, layer)["sources"]
        self.assertEqual(sources["sync_docdb"], "project_env")
        self.assertEqual(sources["detail_cap"], "project_env")

    def test_a_shadowed_value_is_attributed_to_the_shell(self):
        _, layer = self.merged_env({"CWK_SYNC_DOCDB": "1"}, "CWK_SYNC_DOCDB=1\n")
        sources = C.resolve_nightly_runtime({}, {"CWK_SYNC_DOCDB": "1"}, layer)["sources"]
        self.assertEqual(sources["sync_docdb"], "shell")

    def test_the_source_vocabulary_stays_closed(self):
        """Anything outside this set would render as raw text in the Markdown."""

        _, layer = self.merged_env({}, "CWK_SYNC_DOCDB=1\n")
        sources = C.resolve_nightly_runtime({"detail_cap": 7}, {"CWK_WIKI_LIMIT": "9"}, layer)
        self.assertLessEqual(
            set(sources["sources"].values()),
            {"config", "shell", "project_env", "default"},
        )

    def test_an_unmodelled_value_in_the_file_is_refused_not_defaulted(self):
        """``int("lots")`` aborts upstream; the contract must not invent a cap."""

        _, layer = self.merged_env({}, "CWK_DETAIL_CAP=lots\n")
        with self.assertRaises(C.NightlyConfigError):
            C.resolve_nightly_runtime({}, {}, layer)

    def test_a_paused_path_enabled_from_the_file_is_refused(self):
        _, layer = self.merged_env({}, "CWK_CLOUD_FIRST=1\n")
        with self.assertRaises(C.UnschedulableNightlySetting):
            C.resolve_nightly_runtime({}, {}, layer)


class ProjectEnvContractTests(unittest.TestCase):
    """The blind spot, closed at the level the user actually signs.

    Before this, ``.env`` could flip ``sync_docdb`` and ``wiki_sync`` on while
    the contract said "nothing leaves this machine", the hash stayed put so the
    existing confirmation still covered it, and the handoff went out clean.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rt032-dotenv-contract-"))

    def contract(self, config=None, env=None, dotenv=None):
        if dotenv is not None:
            (self.root / C.PROJECT_ENV_FILE).write_text(dotenv, encoding="utf-8")
        return build(config or {}, env or {}, project_env_root=self.root)

    def test_the_file_alone_turns_publication_on(self):
        off = self.contract()
        self.assertFalse(off["publishing"]["any_external_publication"])
        on = self.contract(dotenv="CWK_SYNC_DOCDB=1\nCWK_WIKI_SYNC=1\n")
        self.assertEqual(
            on["publishing"]["targets"], ["docdb:daily_and_runs", "docdb:wiki"]
        )
        self.assertTrue(on["wiki_pipeline"]["sync_to_docdb"])

    def test_the_file_alone_moves_the_contract_hash(self):
        """Otherwise a confirmation given before the edit still covers after it."""

        before = self.contract()["contract_sha256"]
        after = self.contract(dotenv="CWK_SYNC_DOCDB=1\n")["contract_sha256"]
        self.assertNotEqual(before, after)

    def test_editing_the_file_later_moves_the_hash_again(self):
        first = self.contract(dotenv="CWK_SYNC_DOCDB=1\n")["contract_sha256"]
        second = self.contract(dotenv="CWK_SYNC_DOCDB=1\nCWK_WIKI_SYNC=1\n")
        self.assertNotEqual(first, second["contract_sha256"])

    def test_rebuilding_over_an_unchanged_file_is_byte_stable(self):
        text = "CWK_SYNC_DOCDB=1\nCWK_DETAIL_CAP=42\n"
        self.assertEqual(
            json.dumps(self.contract(dotenv=text), sort_keys=True),
            json.dumps(self.contract(dotenv=text), sort_keys=True),
        )

    def test_the_markdown_says_the_file_is_in_play(self):
        text = C.render_contract_markdown(self.contract(dotenv="CWK_SYNC_DOCDB=1\n"))
        self.assertIn("项目根存在 `.env`", text)
        self.assertIn("sync_docdb = `True` ← 项目根的 .env 文件", text)
        self.assertIn("wiki/ 发布到 DocDB：关", text)
        self.assertIn("本次是否有内容离开这台机器：是", text)

    def test_the_markdown_says_so_even_when_the_file_decides_nothing(self):
        """A file that is loaded but currently inert is still worth disclosing."""

        text = C.render_contract_markdown(self.contract(dotenv="# nothing\n"))
        self.assertIn("项目根存在 `.env`", text)
        self.assertIn("本合同中没有取值由它决定", text)

    def test_an_absent_file_is_not_announced(self):
        text = C.render_contract_markdown(self.contract())
        self.assertNotIn("项目根存在", text)

    def test_the_disclosure_block_names_only_registry_keys(self):
        contract = self.contract(
            dotenv=(
                "CWK_SYNC_DOCDB=1\n"
                "OPENAI_API_KEY=sk-not-a-real-key\n"
                "SOME_PRIVATE_NAME=whatever\n"
            )
        )
        block = contract["runtime_resolution"]["project_env"]
        self.assertEqual(block["file"], ".env")
        self.assertTrue(block["present"])
        self.assertEqual(block["modelled_variables_present"], 1)
        self.assertEqual(block["settings_sourced_from_file"], ["sync_docdb"])
        for key in block["settings_sourced_from_file"]:
            self.assertIn(key, C.NIGHTLY_SETTING_KEYS)

    def test_no_foreign_name_value_or_path_reaches_the_artifacts(self):
        secrets = (
            "sk-not-a-real-key",
            "OPENAI_API_KEY",
            "SOME_PRIVATE_NAME",
            "whatever",
            "postgres://u:p@h/d",
            str(self.root),
        )
        contract = self.contract(
            dotenv=(
                "CWK_SYNC_DOCDB=1\n"
                "OPENAI_API_KEY=sk-not-a-real-key\n"
                "SOME_PRIVATE_NAME=whatever\n"
                "DATABASE_URL=postgres://u:p@h/d\n"
            )
        )
        blob = json.dumps(contract, ensure_ascii=False) + C.render_contract_markdown(contract)
        blob += json.dumps(handoff_for(contract), ensure_ascii=False)
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_a_credential_in_the_file_is_recorded_as_state_not_value(self):
        contract = self.contract(dotenv="CWORK_APP_KEY=not-a-real-key\n")
        self.assertEqual(contract["settings"]["app_key"], {"state": "set"})
        self.assertEqual(
            contract["runtime_resolution"]["sources"]["app_key"], "project_env"
        )
        self.assertNotIn("not-a-real-key", json.dumps(contract, ensure_ascii=False))

    def test_a_credential_in_the_file_does_not_look_like_a_shell_dependency(self):
        """The scheduled run loads the same file, so it gets the same key."""

        contract = self.contract(dotenv="CWORK_APP_KEY=not-a-real-key\n")
        self.assertTrue(
            contract["runtime_resolution"]["scheduled_environment_equivalent"]
        )
        handoff_for(contract)  # must not raise

    def test_a_setting_from_the_file_is_not_a_shell_dependency_either(self):
        """It will be reloaded at 02:30; refusing the handoff would be wrong."""

        contract = self.contract(dotenv="CWK_SYNC_DOCDB=1\nCWK_DETAIL_CAP=42\n")
        resolution = contract["runtime_resolution"]
        self.assertTrue(resolution["scheduled_environment_equivalent"])
        self.assertEqual(resolution["settings_requiring_shell_environment"], [])
        self.assertEqual(
            handoff_for(contract)["command_spec"]["env_allowlist"], ["CWORK_APP_KEY"]
        )

    def test_a_shell_only_difference_is_still_refused(self):
        contract = self.contract(
            env={"CWK_WIKI_LIMIT": "9"}, dotenv="CWK_SYNC_DOCDB=1\n"
        )
        resolution = contract["runtime_resolution"]
        self.assertFalse(resolution["scheduled_environment_equivalent"])
        self.assertEqual(resolution["settings_requiring_shell_environment"], ["wiki_limit"])
        with self.assertRaises(C.ScheduledEnvironmentMismatch):
            handoff_for(contract)

    def test_a_shell_value_masking_the_file_is_refused_too(self):
        """The nastiest shape: "off" now, on at 02:30 when the shell is gone.

        The interactive resolution says ``sync_docdb`` is false because the
        shell wins. The scheduled run has no shell, so the ``.env`` value comes
        back up and it publishes. Same contract, two different nights — the
        handoff must not go out.
        """

        contract = self.contract(env={"CWK_SYNC_DOCDB": "0"}, dotenv="CWK_SYNC_DOCDB=1\n")
        self.assertFalse(contract["publishing"]["sync_docdb"])
        resolution = contract["runtime_resolution"]
        self.assertFalse(resolution["scheduled_environment_equivalent"])
        self.assertIn("sync_docdb", resolution["settings_requiring_shell_environment"])
        with self.assertRaises(C.ScheduledEnvironmentMismatch):
            handoff_for(contract)

    def test_a_masked_value_is_visible_in_the_markdown_warning(self):
        text = C.render_contract_markdown(
            self.contract(env={"CWK_SYNC_DOCDB": "0"}, dotenv="CWK_SYNC_DOCDB=1\n")
        )
        self.assertIn("警告", text)
        self.assertIn("sync_docdb", text)

    def test_a_paused_path_enabled_by_the_file_refuses_the_whole_contract(self):
        with self.assertRaises(C.UnschedulableNightlySetting):
            self.contract(dotenv="CWK_CLOUD_FIRST=1\n")

    def test_every_registry_key_set_from_the_file_moves_the_hash(self):
        """The file has to reach *all* 41 settings, not the publication ones."""

        baseline = self.contract()
        resolved = C.resolve_nightly_runtime({}, {})["settings"]
        for setting in C.NIGHTLY_SETTINGS:
            if not setting.env_keys:
                continue  # wiki_mirror_root has no environment layer at all
            with self.subTest(setting=setting.key):
                value = _dot_env_value(setting, resolved)
                dotenv = f"{setting.env_keys[0]}={value}\n"
                if setting.key in C.PAUSED_NIGHTLY_PATHS:
                    # Stronger than "the hash moved": there is no contract at
                    # all, because that night would exit at startup.
                    with self.assertRaises(C.UnschedulableNightlySetting):
                        self.contract(dotenv=dotenv)
                    continue
                moved = self.contract(dotenv=dotenv)
                self.assertNotEqual(
                    baseline["contract_sha256"],
                    moved["contract_sha256"],
                    f"{setting.key} can be changed from .env without voiding consent",
                )


def _dot_env_value(setting, resolved: dict):
    """A ``.env`` value that moves ``setting`` away from what it resolves to now."""

    if setting.kind == "bool":
        return "0" if resolved[setting.key] else "1"
    if setting.kind == "int":
        return "7" if resolved[setting.key] != 7 else "3"
    if setting.kind == "url":
        return "https://example.test"
    if setting.kind == "path":
        return "knowledge"
    if setting.kind == "secret":
        return "not-a-real-key"
    return "value-from-dot-env"


class ScheduledArgvFidelityTests(unittest.TestCase):
    """Finding 4: the argv is the whole command-line layer, so pin it exactly.

    A flag-name set was not enough. ``["--config", "x", "--sync-docdb"]`` and
    ``["--config", "x"]`` have different behaviour and the same flag *names* if
    you only look at the first three. Order matters too: ``--config`` must
    precede its value, or the host builds a different command.
    """

    def setUp(self):
        self.contract = build({}, {})
        self.handoff = handoff_for(self.contract)
        self.argv = self.handoff["command_spec"]["argv"]

    def test_the_argv_is_exactly_this_list_in_exactly_this_order(self):
        locator = C.build_config_locator(
            config_path=PROJECT / "cwk-mirror.local.json", project_root=PROJECT
        )
        self.assertEqual(
            self.argv,
            [
                "python3",
                "scripts/cwk_nightly_pipeline.py",
                "--config",
                locator["path"],
                "--run-name",
                "nightly-{{YYYYMMDD-HHMM}}",
                "--date",
                "{{YYYY-MM-DD}}",
            ],
        )

    def test_no_other_upstream_option_appears_anywhere_in_the_argv(self):
        """Checked against every option the pipeline declares, not a copied list."""

        joined = "\x00".join(self.argv)
        for dest, entry in UPSTREAM_OPTIONS.items():
            if dest in ARGV_DESTS:
                continue
            with self.subTest(flag=entry["flag"]):
                self.assertNotIn(entry["flag"], joined)
                # BooleanOptionalAction also accepts the --no- spelling.
                self.assertNotIn("--no-" + entry["flag"][2:], joined)

    def test_the_argv_carries_no_absolute_path_and_no_credential(self):
        for item in self.argv:
            with self.subTest(item=item):
                self.assertFalse(item.startswith("/"))
                self.assertFalse(item.startswith("~"))
        self.assertNotIn("CWORK_APP_KEY", "\x00".join(self.argv))
        self.assertFalse(self.handoff["command_spec"]["secrets_included"])
        self.assertFalse(self.handoff["command_spec"]["absolute_paths_included"])

    def test_the_argv_options_the_contract_advertises_are_the_ones_emitted(self):
        """The contract tells the user what the command will be; it must be true."""

        advertised = self.contract["scheduled_invocation"]["argv_options"]
        emitted = [item for item in self.argv if item.startswith("--")]
        self.assertEqual(advertised, emitted)

    def test_the_fixed_command_line_switches_are_stated_in_the_contract(self):
        fixed = self.contract["scheduled_invocation"]["cli_only_flags_fixed"]
        self.assertEqual(set(fixed), set(C.NIGHTLY_CLI_ONLY_FIXED))
        # The one that matters most: publishing is real, not a dry run.
        self.assertIs(fixed["--sync-dry-run"], False)
        self.assertIs(self.contract["publishing"]["dry_run"], False)

    def test_the_argv_stays_identical_when_publishing_is_turned_on(self):
        """Config changes the run; it must never change the command line."""

        other = handoff_for(build({"sync_docdb": True, "sync_wiki": True}, {}))
        self.assertEqual(other["command_spec"]["argv"], self.argv)

    def test_the_environment_the_task_gets_is_names_only(self):
        allowlist = self.handoff["command_spec"]["env_allowlist"]
        self.assertEqual(allowlist, list(C.SCHEDULED_ENV_ALLOWLIST))
        self.assertEqual(
            self.contract["scheduled_invocation"]["requires_environment"], allowlist
        )


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
        for name, env_key in (
            ("detail_cap", "CWK_DETAIL_CAP"),
            ("continuation_cap", "CWK_CONTINUATION_CAP"),
            ("backfill_cap", "CWK_BACKFILL_CAP"),
            ("backfill_page_size", "CWK_BACKFILL_PAGE_SIZE"),
        ):
            with self.subTest(name=name):
                self.assert_pinned(
                    f'args.{name} = int(config_value(args, config, "{name}", '
                    f'os.environ.get("{env_key}", {pipeline_env_default(env_key)})))'
                )

    def test_the_lookback_still_takes_config_over_environment(self):
        self.assert_pinned('"source_completeness_lookback_days"')
        self.assert_pinned(
            'os.environ.get("CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS", '
            f"{pipeline_env_default('CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS')})"
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
        self.assert_pinned('env_source_completeness = env_bool("CWK_SOURCE_COMPLETENESS")')
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

    def test_the_wiki_chain_still_derives_the_way_the_contract_says(self):
        """BL-1 lives here: ``wiki_sync`` is a publication channel of its own."""

        self.assert_pinned(
            'env_wiki_compile = env_bool("CWK_WIKI_COMPILE") args.wiki_compile = '
            "env_wiki_compile if env_wiki_compile is not None "
            'else bool(config.get("wiki_compile", args.sync_wiki))'
        )
        self.assert_pinned(
            'else bool(config.get("wiki_topics_entities", args.sync_wiki or args.wiki_compile))'
        )
        self.assert_pinned(
            "default_wiki_sync = bool(args.sync_docdb and "
            "(args.wiki_compile or args.wiki_topics_entities))"
        )
        self.assert_pinned(
            'args.wiki_sync = env_wiki_sync if env_wiki_sync is not None '
            'else bool(config.get("wiki_sync", default_wiki_sync))'
        )

    def test_wiki_sync_still_publishes_without_sync_docdb(self):
        """The reproduction's mechanism, pinned at the call site itself."""

        source = " ".join(PIPELINE_SOURCE.split())
        self.assertIn("if args.wiki_sync:", source)
        self.assertIn("--only-prefix", source)
        self.assertIn("cwk_sync_mirror_to_docdb.py", source)
        # `if args.wiki_sync:` must remain a top-level branch in main(), i.e.
        # not nested under a sync_docdb test — that nesting is the only thing
        # that would make "sync_docdb off ⇒ nothing published" true.
        wiki_sync_branches = [
            node
            for node in MAIN_AST.body
            if isinstance(node, ast.If) and "args.wiki_sync" == ast.unparse(node.test)
        ]
        self.assertTrue(
            wiki_sync_branches,
            "wiki publication is no longer an independent top-level branch; "
            "re-derive the contract's publishing model",
        )

    def test_the_cloud_pause_still_requires_an_unlock_the_handoff_cannot_give(self):
        source = " ".join(inspect.getsource(N.enforce_cloud_pause).split())
        self.assertIn("cloud_first and not experimental_cloud_first", source)
        self.assertIn(
            "publish_cloud_query_catalog and not experimental_cloud_query_catalog", source
        )
        self.assert_pinned("enforce_cloud_pause(")

    def test_cloud_first_still_forces_publication_on(self):
        """Why it cannot simply be modelled as 'off': it rewrites two channels."""

        self.assert_pinned(
            "if args.cloud_first: args.sync_docdb = True args.wiki_sync = True "
            "args.wiki_best_effort = False"
        )

    def test_the_accepted_boolean_words_are_still_these_five(self):
        source = " ".join(inspect.getsource(N.env_bool).split())
        self.assertIn('{"1", "true", "yes", "y", "on"}', source)
        self.assertEqual(set(C.NIGHTLY_ENV_TRUE), {"1", "true", "yes", "y", "on"})

    def test_config_value_still_ignores_only_none_empty_and_empty_list(self):
        source = " ".join(inspect.getsource(N.config_value).split())
        self.assertIn('if value not in (None, "", []):', source)

    def test_the_contract_module_still_refuses_to_import_the_pipeline(self):
        """This file loads a sanitized copy. The wizard's own modules may not."""

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
