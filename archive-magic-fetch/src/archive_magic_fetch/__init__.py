"""Archive Magic Fetch: build portable WARC/CDXJ collections from Wayback."""

from __future__ import annotations

import warnings

# cdxj-indexer imports PyAMF, which still imports pkg_resources. Setuptools
# warns on that import at process start; the warning is upstream of this tool.
warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API\.",
    category=UserWarning,
)

__version__ = "0.1.0"
