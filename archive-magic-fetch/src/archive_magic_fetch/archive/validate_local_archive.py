"""Validate finalized local artifacts before remote publication."""

from __future__ import annotations

import re
from pathlib import Path

from archive_magic_fetch.archive.format import (
    parse_cdxj_line,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs
from archive_magic_fetch.models import PublicationError


def validate_local_archive(
    layout: ArchiveLayout, *, year: str | None = None, allow_unindexed: bool = False
) -> list[Path]:
    """Validate local index locators before allowing mirror deletions."""

    if not layout.root.is_dir():
        raise PublicationError(f"local archive does not exist: {layout.root}")
    if year is not None and not re.fullmatch(r"\d{4}", year):
        raise PublicationError(f"invalid year for publication: {year}")
    prefix = f"{layout.archive_id}-"
    artifacts = sorted(
        path
        for path in layout.root.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and (year is None or path.name.startswith(f"{prefix}{year}-"))
        and (path.name.endswith(".warc.gz") or path.name.endswith("-index.cdxj"))
    )
    if not artifacts:
        raise PublicationError("local archive is empty; refusing to mirror it")
    indexes = [path for path in artifacts if path.name.endswith("-index.cdxj")]
    if not indexes:
        raise PublicationError("local archive has no CDXJ indexes")
    referenced_all: set[str] = set()
    for index in indexes:
        index_year = index.name.removeprefix(prefix).removesuffix("-index.cdxj")
        if not re.fullmatch(r"\d{4}", index_year):
            raise PublicationError(f"unexpected index name: {index.name}")
        warcs = list_collection_warcs(layout, index_year)
        if not warcs:
            raise PublicationError(f"index has no local WARC: {index.name}")
        lines = [
            line for line in index.read_text(encoding="utf-8").splitlines() if line
        ]
        try:
            validate_cdxj_against_warcs(layout, index_year, lines)
            referenced = {parse_cdxj_line(line)[2]["filename"] for line in lines}
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationError(
                f"invalid local index {index.name}: {error}"
            ) from error
        if any(not (layout.root / name).is_file() for name in referenced):
            raise PublicationError(f"index references missing WARC: {index.name}")
        referenced_all.update(referenced)
    unindexed = [
        path.name
        for path in artifacts
        if path.name.endswith(".warc.gz") and path.name not in referenced_all
    ]
    if unindexed and not allow_unindexed:
        raise PublicationError(
            "local WARC has no CDXJ entries: " + ", ".join(sorted(unindexed))
        )
    return artifacts
