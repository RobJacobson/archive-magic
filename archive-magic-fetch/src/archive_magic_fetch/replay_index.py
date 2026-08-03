"""Build the collection-wide replay index from final WARC files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Sequence

from cdxj_indexer.main import CDXJIndexer

from .collection_paths import CollectionPaths, validate_path_limits


def build_replay_index(
    built_warcs: Sequence[Path],
    *,
    layout: CollectionPaths,
) -> Path | None:
    """Build and atomically publish one sorted site-level CDXJ."""

    if not built_warcs:
        return None

    replay_dir = layout.replay_index.parent
    replay_dir.mkdir(parents=True, exist_ok=True)
    validate_path_limits(layout.replay_index)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".index-",
        suffix=".cdxj.tmp",
        dir=replay_dir,
    )
    temporary = Path(temporary_name)
    try:
        # The indexer owns opening the output path.
        os.close(descriptor)
        CDXJIndexer(
            output=str(temporary),
            inputs=[str(path) for path in built_warcs],
            sort=True,
            records="response,revisit",
            dir_root=str(layout.collection_root),
        ).process_all()
        os.replace(temporary, layout.replay_index)
        return layout.replay_index
    finally:
        if temporary.exists():
            temporary.unlink()
