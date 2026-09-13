"""Source adapter package."""

try:  # Package imports (``python -m adapters...``) are the production path.
    from .base import NormalizedDoc, SourceItem, SourceAdapter, get_adapter, known_adapters, register  # noqa: F401
except ImportError:  # Preserve the repository's direct ``sys.path`` test convention.
    from base import NormalizedDoc, SourceItem, SourceAdapter, get_adapter, known_adapters, register  # noqa: F401
