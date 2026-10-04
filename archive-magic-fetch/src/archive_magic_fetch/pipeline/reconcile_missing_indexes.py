"""Repair missing or outdated local indexes through the shared indexer."""

from __future__ import annotations

from archive_magic_fetch.archive.format import cdxj_filenames
from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs
from archive_magic_fetch.pipeline.build_collection_index import build_collection_index


def reconcile_missing_indexes(
    layout: ArchiveLayout, *, exclude_collections: set[str] | None = None,
) -> list[str]:
    """Rebuild missing indexes; incrementally replace lines for new or newer WARCs."""

    updated: list[str] = []
    if not layout.root.is_dir():
        return updated
    prefix = f"{layout.archive_id}-"
    suffix = "-001.warc.gz"
    collection_ids = sorted(
        {
            path.name[len(prefix) : -len(suffix)]
            for path in layout.root.glob(f"{layout.archive_id}-*-001.warc.gz")
            if path.is_file()
            and path.name.startswith(prefix)
            and path.name.endswith(suffix)
        }
    )
    for collection_id in collection_ids:
        if exclude_collections and collection_id in exclude_collections:
            continue
        layout.validate_collection_id(collection_id)
        warcs = list_collection_warcs(layout, collection_id)
        if not warcs:
            continue
        index = layout.collection_index(collection_id)
        if not index.is_file():
            build_collection_index(layout, collection_id)
            updated.append(collection_id)
            continue
        known = cdxj_filenames(index)
        names = {path.name for path in warcs}
        if known - names:
            build_collection_index(layout, collection_id)
            updated.append(collection_id)
            continue
        index_mtime = index.stat().st_mtime_ns
        changed = [
            path
            for path in warcs
            if path.name not in known or path.stat().st_mtime_ns > index_mtime
        ]
        if not changed:
            continue
        build_collection_index(layout, collection_id, changed_warcs=changed)
        updated.append(collection_id)
    return updated
