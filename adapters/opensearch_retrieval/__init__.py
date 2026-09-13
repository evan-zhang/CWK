"""Production OpenSearch retrieval adapter.

The package is deliberately standard-library-only. It owns source projection,
the exact/lexical retrieval contract, and the small HTTP service; it does not
import the RT-055 experiment scripts.
"""

__version__ = "1.0.0"

from .config import Settings

__all__ = ["Settings", "__version__"]
