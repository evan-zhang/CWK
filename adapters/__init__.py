"""RT-041 source adapter package: one adapter per data source.

Contract: docs/ADAPTER-CONTRACT.md (v1).  Base interfaces and the
registry live in ``base``; ``gwork`` is adapter #1 (CWork wrapper-style).
New adapters register via ``base.register`` and ship with equivalence
tests before landing (see tests/test_rt041_gwork_adapter.py for the bar).
"""

from base import NormalizedDoc, SourceItem, SourceAdapter, get_adapter, known_adapters, register  # noqa: F401
