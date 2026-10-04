"""Reset managed remote archive data before a complete rebuild."""

from __future__ import annotations

import re

from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.models import PublicationError
from archive_magic_fetch.pipeline.publication import run_rclone as client


def purge_remote(output: FetchOutput, archive_id: str) -> None:
    """Reset only managed archive data; never purge the bucket or archive root."""
    if output.type != "remote":
        return
    client.require_aws_credentials()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", archive_id):
        raise PublicationError("invalid archive ID for reset")
    with client.temporary_config(output) as config:
        client.reject_legacy_layout(config, output)
        client.run_rclone(
            config,
            "delete",
            client.remote_path(output),
            "--filter",
            f"+ /{archive_id}-????-*.warc.gz",
            "--filter",
            f"+ /{archive_id}-????-index.cdxj",
            "--filter",
            "- **",
        )
