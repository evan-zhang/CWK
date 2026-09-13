"""Source projection and OpenSearch index construction."""

from .builder import BuildStats, IndexBuilder
from .projection import ProjectedIndex, ProjectionError, SourceDocument, coerce_documents, project_documents
from .template import INDEX_TEMPLATE_VERSION, build_index_template

__all__ = [
    "BuildStats",
    "INDEX_TEMPLATE_VERSION",
    "IndexBuilder",
    "ProjectedIndex",
    "ProjectionError",
    "SourceDocument",
    "build_index_template",
    "coerce_documents",
    "project_documents",
]
