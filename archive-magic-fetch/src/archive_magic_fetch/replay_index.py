"""Build the collection-wide replay index from final WARC files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Sequence

from cdxj_indexer.main import CDXJIndexer

from .collection_paths import CollectionPaths, validate_path_limits


def list_collection_warcs(layout: CollectionPaths) -> list[Path]:
    """Return every final WARC under archive/, sorted by relative path."""

    archive_root = layout.archive_root
    if not archive_root.is_dir():
        return []
    warcs = [
        path
        for path in archive_root.rglob("*.warc.gz")
        if path.is_file() and not path.name.endswith(".warc.gz.tmp")
    ]
    return sorted(
        warcs,
        key=lambda path: path.relative_to(layout.collection_root).as_posix(),
    )


def build_replay_index(
    built_warcs: Sequence[Path],
    *,
    layout: CollectionPaths,
) -> Path | None:
    """Build and atomically publish one sorted site-level CDXJ."""

    inputs = list(built_warcs) if built_warcs else []
    if not inputs:
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
            inputs=[str(path) for path in inputs],
            sort=True,
            records="response,revisit",
            dir_root=str(layout.collection_root),
        ).process_all()
        os.replace(temporary, layout.replay_index)
        return layout.replay_index
    finally:
        if temporary.exists():
            temporary.unlink()
