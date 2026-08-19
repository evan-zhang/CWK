#!/usr/bin/env python3
"""RT-012: ``cwk-tenant`` CLI dispatcher — owned exclusively by RT-012.

Every downstream RT (RT-013, RT-019, RT-026) MUST NOT modify this
dispatcher; they add commands only via new provider modules registered
in :data:`FROZEN_PROVIDER_SLOTS`.  The dispatcher intentionally does
NOT scan ``CWD`` / ``PYTHONPATH`` / an env-var plugin list; instead the
allowed provider modules are hard-coded, loaded from the trusted absolute
``scripts/`` directory, ABI-checked, and fail-closed if any check fails.

Design invariants (frozen for the life of PR-001):

- Providers must live at ``<repo>/scripts/<name>.py`` — the RT-012
  worktree's own ``scripts/`` directory.
- Provider imports run inside a try/except; any failure logs a redacted
  message and refuses the provider (never returns partial results).
- ``allow_abbrev=False`` on every parser.
- Absolute paths are redacted from CLI output; no traceback is ever
  printed on user-visible failures.
- Stable exit codes: :data:`cwk_tenant_cli_api.STABLE_EXIT_CODES`.

RT-012 currently ships only one provider: ``cwk_tenant_cmd_core`` (see
:mod:`cwk_tenant_cmd_core`).  Slots for RT-013, RT-019, RT-026 are
declared *empty* here so the dispatcher can accept them when they land
without any dispatcher edits.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import cwk_tenant_cli_api as API

# Repository ``scripts/`` directory — resolved from *this file*, not from
# ``os.getcwd()`` and not from ``sys.path``.  This is the only trusted
# location we permit provider modules to live in.
_TRUSTED_SCRIPTS_DIR = Path(__file__).resolve().parent

# Frozen provider slot list.  Add downstream provider module names here
# only when the corresponding RT ships; the dispatcher itself is not to
# be modified beyond the addition.
FROZEN_PROVIDER_SLOTS: tuple[str, ...] = (
    "cwk_tenant_cmd_core",
    # RT-013 will ship: "cwk_tenant_cmd_binding",
    # RT-019 will ship: "cwk_tenant_cmd_profile",
    # RT-026 will ship: "cwk_tenant_cmd_release",
)

_PROVIDER_NAME_REGEX = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVIDER_VERSION_REGEX = re.compile(r"^v[0-9]{1,4}$")
_COMMAND_NAME_REGEX = re.compile(r"^[a-z][a-z0-9-]{0,31}(:[a-z][a-z0-9-]{0,31})?$")


class _ProviderLoadError(Exception):
    """Internal: a provider failed strict ABI validation."""


def _safe_path(display: str) -> str:
    """Redact any absolute host path down to its last two segments."""

    p = Path(display)
    parts = [part for part in p.parts if part not in ("/", "\\")]
    if not parts:
        return "<input>"
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1]


def _load_provider(module_name: str) -> Any:
    """Import a provider by absolute file path only.

    We do NOT rely on ``sys.path`` because that would let a malicious CWD
    ship a same-named module.  Instead, we resolve
    ``<TRUSTED>/scripts/<module_name>.py`` and load via
    ``importlib.util.spec_from_file_location``.
    """

    if not _PROVIDER_NAME_REGEX.match(module_name):
        raise _ProviderLoadError(f"provider name {module_name!r} out of grammar")

    module_path = _TRUSTED_SCRIPTS_DIR / f"{module_name}.py"
    if not module_path.is_file():
        raise _ProviderLoadError(f"provider file missing: {module_name}.py")

    # Defense in depth: reject if the on-disk file is a symlink.  The
    # trusted repository does not ship symlinked scripts.
    if module_path.is_symlink():
        raise _ProviderLoadError(f"provider file is a symlink: {module_name}.py")

    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise _ProviderLoadError(f"cannot build spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - intentional catch-all: fail-closed
        raise _ProviderLoadError(f"provider {module_name} raised on import: {exc.__class__.__name__}") from exc

    # ABI check.
    for attr in ("API_VERSION", "PROVIDER_NAME", "PROVIDER_VERSION"):
        if not hasattr(module, attr):
            raise _ProviderLoadError(f"provider {module_name} missing attr {attr}")

    if module.API_VERSION != API.COMMAND_PROVIDER_API_VERSION:
        raise _ProviderLoadError(
            f"provider {module_name} API_VERSION={module.API_VERSION!r} != {API.COMMAND_PROVIDER_API_VERSION!r}"
        )
    if not isinstance(module.PROVIDER_NAME, str) or not _PROVIDER_NAME_REGEX.match(module.PROVIDER_NAME):
        raise _ProviderLoadError(f"provider {module_name} PROVIDER_NAME invalid")
    if not isinstance(module.PROVIDER_VERSION, str) or not _PROVIDER_VERSION_REGEX.match(module.PROVIDER_VERSION):
        raise _ProviderLoadError(f"provider {module_name} PROVIDER_VERSION invalid")
    if not hasattr(module, "list_commands"):
        raise _ProviderLoadError(f"provider {module_name} missing list_commands()")
    return module


def _collect_commands() -> dict[str, tuple[str, API.CommandSpec]]:
    """Load every declared provider slot; refuse duplicates.

    Returns a dict ``name -> (provider_name, spec)``.  Two providers
    declaring the same command name is a fatal ABI error.
    """

    registry: dict[str, tuple[str, API.CommandSpec]] = {}
    for module_name in FROZEN_PROVIDER_SLOTS:
        module = _load_provider(module_name)
        specs = module.list_commands()
        if not isinstance(specs, (list, tuple)):
            raise _ProviderLoadError(f"provider {module_name} list_commands() must be sequence")
        for spec in specs:
            if not isinstance(spec, API.CommandSpec):
                raise _ProviderLoadError(f"provider {module_name} yielded non-CommandSpec")
            if not _COMMAND_NAME_REGEX.match(spec.name):
                raise _ProviderLoadError(f"provider {module_name} command name {spec.name!r} invalid")
            if spec.name in registry:
                raise _ProviderLoadError(
                    f"duplicate command name {spec.name!r} (providers {registry[spec.name][0]}, {module.PROVIDER_NAME})"
                )
            registry[spec.name] = (module.PROVIDER_NAME, spec)
    return registry


def _collect_doctor_hooks() -> list[tuple[str, Any]]:
    """Return ``(provider_name, callable)`` for every declared provider
    that exposes ``run_doctor(ctx)``."""

    hooks: list[tuple[str, Any]] = []
    for module_name in FROZEN_PROVIDER_SLOTS:
        try:
            module = _load_provider(module_name)
        except _ProviderLoadError:
            # We surface load errors at command-invocation time; doctor
            # collection stays silent so we don't crash on a partially
            # broken tree.
            continue
        run_doctor = getattr(module, "run_doctor", None)
        if run_doctor is not None:
            hooks.append((module.PROVIDER_NAME, run_doctor))
    return hooks


def _print_stable_error(stderr, message: str) -> None:
    stderr.write(f"error: {message}\n")


def build_parser(registry: dict[str, tuple[str, API.CommandSpec]] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cwk-tenant",
        description="RT-012 multitenant CLI.  Dispatcher only; commands are contributed via frozen provider modules.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    if registry:
        for name, (_provider, spec) in sorted(registry.items()):
            child = sub.add_parser(name, help=spec.summary, allow_abbrev=False)
            spec.configure_parser(child)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    stderr = sys.stderr
    stdout = sys.stdout
    argv_tuple = tuple(argv) if argv is not None else tuple(sys.argv[1:])

    # Load providers up-front so we can render `--help` with the real
    # command list.  A provider load failure prints a stable error and
    # exits EXIT_USAGE (missing/broken CLI configuration).
    try:
        registry = _collect_commands()
    except _ProviderLoadError as exc:
        _print_stable_error(stderr, f"provider registry: {exc}")
        return API.EXIT_USAGE

    parser = build_parser(registry)
    try:
        args = parser.parse_args(argv_tuple)
    except SystemExit as exc:
        # argparse already printed its message; normalise to EXIT_USAGE.
        code = exc.code if isinstance(exc.code, int) else API.EXIT_USAGE
        if code == 0:
            return API.EXIT_OK
        # argparse's default parse-error exit code is 2, but we reserve
        # 2 for contract errors.  Downgrade to EXIT_USAGE.
        return API.EXIT_USAGE if code != 0 else API.EXIT_OK

    if args.command is None:
        parser.print_help(stderr)
        return API.EXIT_USAGE

    _, spec = registry[args.command]
    ctx = API.CommandContext(
        stdout=stdout,
        stderr=stderr,
        argv=argv_tuple,
        env=_safe_env_snapshot(),
    )
    try:
        code = spec.handler(ctx, args)
    except API.CliError as exc:
        _print_stable_error(stderr, str(exc))
        return exc.exit_code
    except SystemExit as exc:  # pragma: no cover - handler bug
        _print_stable_error(stderr, "handler called sys.exit; treated as internal failure")
        return API.EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - fail-closed
        _print_stable_error(stderr, f"internal failure ({exc.__class__.__name__})")
        return API.EXIT_INTERNAL

    if code not in API.STABLE_EXIT_CODES:
        _print_stable_error(stderr, f"handler returned non-stable exit code {code}")
        return API.EXIT_INTERNAL
    return code


def _safe_env_snapshot() -> dict[str, str]:
    """Return only the env vars the tenant CLI is allowed to consume.

    Notably ``CWK_APP_KEY``/other credential-looking vars are not
    propagated so a broken provider cannot accidentally log them.
    """

    keys = ("CWK_INSTANCE_ROOT",)
    return {k: os.environ.get(k, "") for k in keys if k in os.environ}


__all__ = [
    "FROZEN_PROVIDER_SLOTS",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
