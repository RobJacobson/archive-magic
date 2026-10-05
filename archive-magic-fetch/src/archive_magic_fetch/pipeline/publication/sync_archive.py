"""Publish complete local generations without mirroring remote deletions."""
from archive_magic_fetch.pipeline.publication.storage import BucketStorage


def sync_archive(layout, output, *, year=None, publisher: BucketStorage | None = None):
    if output.type != 'remote':
        return
    store = publisher
    if store is None:
        store = BucketStorage(output, layout.archive_id)
        store.preflight()
    store.publish(year)
