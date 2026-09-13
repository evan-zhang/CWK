"""Exact and ICU lexical retrieval primitives."""

from .engine import BackendQueryError, RetrievalHit, RetrievalQuery, UnknownBank, is_no_answer
from .exact import ExactResolution, extract_exact_fields, resolve_exact
from .merge import collapse_hits, merge_channel_hits

# ``exact_fields`` is the experiment's public helper name; retain it as a
# harmless compatibility alias while keeping the implementation independent.
exact_fields = extract_exact_fields
merge_hits = merge_channel_hits

__all__ = [
    "BackendQueryError",
    "ExactResolution",
    "RetrievalHit",
    "RetrievalQuery",
    "UnknownBank",
    "collapse_hits",
    "exact_fields",
    "extract_exact_fields",
    "is_no_answer",
    "merge_channel_hits",
    "merge_hits",
    "resolve_exact",
]
