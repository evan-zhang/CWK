"""RT-041 source adapter contract (v1).

Every data source that wants into the mirror implements one adapter with
four operations: ``discover`` (incremental enumeration, cursor managed by
the adapter itself), ``fetch`` (pull + normalize), ``dedupe_key`` (a
globally unique ``<source-prefix>-<native-id>`` key that can never collide
across sources), and ``watch`` (change detection, generalizing the RT-040
ord2 reply-state baseline mechanism; each source picks its own
fingerprint).  Credentials travel as call arguments and never persist
inside an adapter instance.

The normalized exit point is the existing raw frontmatter contract: a
``NormalizedDoc.body_markdown`` is byte-compatible with what
``cwk_collect_live.write_markdown`` produces today, so downstream stages
(promote / compile / refine / query) never learn that a source exists.

Contract prose lives in ``docs/ADAPTER-CONTRACT.md`` (v1, RT-041).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / "scripts") not in sys.path:  # adapter packages import scripts/* helpers
    sys.path.insert(0, str(PROJECT / "scripts"))


@dataclass
class SourceItem:
    """A discovered reference to one document on a source.

    ``native`` carries the source's own row structure untouched; adapters
    may round-trip private bookkeeping fields (leading underscore) through
    it, and ``fetch`` must accept these objects as returned by
    ``discover``/``watch``.
    """

    native_id: str
    row: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedDoc:
    """The unified output document.

    ``id`` is prefixed (``gwork-<report_id>``) and unique across sources.
    ``participants`` is the refine-relevance field (contract invariant 4);
    adapters emit it per source semantics (CWork: writer + role-labelled
    lines; email: sender/recipients; files: collaborators).
    """

    id: str
    native_id: str
    title: str
    author: str
    participants: list[str]
    created: str
    source_type: str
    body_markdown: str


class SourceAdapter(Protocol):
    """The four-operation contract every source implements."""

    source_type: str
    id_prefix: str

    def discover(
        self, app_key: str, start_date: str, end_date: str
    ) -> list[SourceItem]: ...

    def fetch(self, item: SourceItem, app_key: str) -> NormalizedDoc: ...

    def dedupe_key(self, item: SourceItem) -> str: ...

    def watch(
        self, app_key: str, baseline: dict[str, Any], start_date: str, end_date: str
    ) -> tuple[list[SourceItem], dict[str, Any]]: ...


_REGISTRY: dict[str, Any] = {}


def register(cls: Any) -> Any:
    """Class decorator: make an adapter class discoverable by source_type."""
    source_type = getattr(cls, "source_type", None)
    if not source_type:
        raise ValueError("adapter class needs a source_type attribute")
    _REGISTRY[source_type] = cls
    return cls


def get_adapter(source_type: str) -> Any:
    """Instantiate a registered adapter by source_type (imports lazily)."""
    if source_type not in _REGISTRY:
        # Lazy import so unit tests and CI never need network or credentials.
        if source_type == "gwork":
            from gwork import GWorkAdapter  # noqa: PLC0415 - lazy on purpose

            register(GWorkAdapter)
    if source_type not in _REGISTRY:
        raise KeyError(f"no registered source adapter for {source_type!r}")
    return _REGISTRY[source_type]()


def known_adapters() -> list[str]:
    """Source types with a registered adapter class right now."""
    try:
        from gwork import GWorkAdapter  # noqa: PLC0415 - lazy on purpose

        register(GWorkAdapter)
    except Exception:  # noqa: BLE001 - registry stays usable without the skill/client
        pass
    return sorted(_REGISTRY)
