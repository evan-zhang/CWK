#!/usr/bin/env python3
"""RT-012: Frozen CommandProviderV1 ABI for the tenant CLI.

Owned by RT-012.  Every downstream RT (RT-013 binding, RT-019 profile,
RT-026 release, …) contributes commands **only** by writing a provider
module that exports the frozen callables described below.  The
dispatcher in :mod:`cwk_tenant_cli` will refuse to load a provider that:

- has the wrong ABI version;
- returns a bad ``CommandSpec``;
- raises during import;
- lives outside the trusted absolute ``scripts/`` directory owned by
  RT-012.

This module contains only pure Python types (dataclasses / Protocol);
importing it must have zero side effects and must not read env vars.
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

# The dispatcher and providers agree on this version tag.  Any change is a
# breaking ABI change and is not permitted for RT-013..RT-026.
COMMAND_PROVIDER_API_VERSION = "v1"

# Stable exit codes.  These mirror RT-011 CLI codes so operators get one
# taxonomy across the whole PR-001 CLI surface.
EXIT_OK = 0
EXIT_CONTRACT = 2       # schema / policy / state-machine violation
EXIT_CONFLICT = 3       # CAS / revision conflict / duplicate
EXIT_USAGE = 4          # missing / unknown / malformed CLI usage
EXIT_IO = 5             # missing file / permission / disk error
EXIT_INTERNAL = 6       # unexpected — never re-raise traceback

STABLE_EXIT_CODES: tuple[int, ...] = (
    EXIT_OK,
    EXIT_CONTRACT,
    EXIT_CONFLICT,
    EXIT_USAGE,
    EXIT_IO,
    EXIT_INTERNAL,
)


@dataclass(frozen=True)
class CommandContext:
    """Everything a command handler is allowed to touch.

    The dispatcher constructs this object; providers MUST NOT mutate it or
    smuggle new fields via ``__dict__`` — the dataclass is frozen.  Any
    extension requires bumping ``COMMAND_PROVIDER_API_VERSION`` (which is
    a breaking ABI change).
    """

    stdout: io.TextIOBase
    stderr: io.TextIOBase
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSpec:
    """One command contributed by a provider.

    Attributes
    ----------
    name : str
        Full command name (e.g. ``"init"``, ``"doctor"``).  Must match the
        RT-012 schema regex ``^[a-z][a-z0-9-]{0,31}(:[a-z][a-z0-9-]{0,31})?$``.
    summary : str
        Short one-liner shown in ``cwk-tenant --help``.
    configure_parser : Callable
        Called once with an ``argparse.ArgumentParser`` before parsing so
        the provider can attach flags.  MUST NOT print to stdout/stderr.
    handler : Callable[[CommandContext, argparse.Namespace], int]
        Called after parse; returns a stable exit code.  MUST NOT call
        ``sys.exit``.  Uncaught exceptions are converted to
        ``EXIT_INTERNAL`` and a redacted message.
    """

    name: str
    summary: str
    configure_parser: Callable[[argparse.ArgumentParser], None]
    handler: Callable[["CommandContext", argparse.Namespace], int]


@dataclass(frozen=True)
class DoctorFinding:
    """One row of a doctor hook's report.

    Provider modules that need to contribute to ``cwk-tenant doctor`` do
    so by exporting a ``run_doctor(ctx)`` hook that returns a list of
    :class:`DoctorFinding` values.  The dispatcher aggregates all hooks
    and prints a machine-readable JSON summary.
    """

    name: str
    severity: str  # "info" | "warn" | "error"
    status: str    # "ok" | "issue"
    detail: str


class CommandProviderV1(Protocol):
    """The static shape of a provider module.

    A provider module MUST expose (at module level):

    - ``API_VERSION: str`` == ``"v1"``;
    - ``PROVIDER_NAME: str`` matching ``^[a-z][a-z0-9_]{0,63}$``;
    - ``PROVIDER_VERSION: str`` matching ``^v[0-9]{1,4}$``;
    - ``list_commands() -> Sequence[CommandSpec]``;
    - Optionally ``run_doctor(ctx: CommandContext) -> Sequence[DoctorFinding]``.

    The dispatcher checks these attributes before invoking any handler.
    Missing attributes fail closed (the provider is refused, not loaded
    partially).
    """

    API_VERSION: str
    PROVIDER_NAME: str
    PROVIDER_VERSION: str

    def list_commands(self) -> Sequence[CommandSpec]:  # pragma: no cover - protocol
        ...


class CliError(Exception):
    """Signals a stable-code CLI failure without a traceback.

    Handlers may raise :class:`CliError` to instruct the dispatcher to
    print ``message`` (redacted, no absolute paths) and exit with the
    given ``exit_code``.
    """

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        if exit_code not in STABLE_EXIT_CODES:
            raise ValueError(f"exit_code {exit_code} not in stable set")
        self.exit_code = exit_code


__all__ = [
    "COMMAND_PROVIDER_API_VERSION",
    "CliError",
    "CommandContext",
    "CommandProviderV1",
    "CommandSpec",
    "DoctorFinding",
    "EXIT_CONFLICT",
    "EXIT_CONTRACT",
    "EXIT_INTERNAL",
    "EXIT_IO",
    "EXIT_OK",
    "EXIT_USAGE",
    "STABLE_EXIT_CODES",
]
