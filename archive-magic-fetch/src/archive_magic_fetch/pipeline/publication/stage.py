"""Publish finalized WARCs and indexes in a safe order."""

from __future__ import annotations

from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.archive.validation import validate_local_archive
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.pipeline.publication import client


def sync_archive(
    layout: ArchiveLayout, output: FetchOutput, *, year: str | None = None
) -> None:
    """Mirror only managed root-level archive files, with indexes as commit points."""

    if output.type != "remote":
        return
    validate_local_archive(layout, year=year)
    client.require_aws_credentials()
    remote = client.remote_path(output)
    source = str(layout.root)
    period = year if year is not None else "????"
    warc_filter = [
        "--filter",
        f"+ /{layout.archive_id}-{period}-*.warc.gz",
        "--filter",
        "- **",
    ]
    index_filter = [
        "--filter",
        f"+ /{layout.archive_id}-{period}-index.cdxj",
        "--filter",
        "- **",
    ]
    with client.temporary_config(output) as config:
        client.reject_legacy_layout(config, output)
        client.run_rclone(config, "copy", source, remote, *warc_filter)
        client.run_rclone(config, "sync", source, remote, *index_filter)
        client.run_rclone(config, "sync", source, remote, *warc_filter)
