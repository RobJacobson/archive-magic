"""Durable archive fields and CDXJ parsing and range validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs

CDX_PAYLOAD_DIGEST_HEADER = "CDX-Payload-Digest"


CDX_STATUS_HEADER = "CDX-Status"


CDX_URLKEY_HEADER = "CDX-Urlkey"


CDX_DIGEST_MATCH_HEADER = "CDX-Digest-Match"


MISSING_CDX_PAYLOAD_DIGEST = "-"


MISSING_CDX_STATUS = "-"


EMPTY_PAYLOAD_DIGEST = "sha1:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"


def cdxj_filenames(path: Path) -> set[str]:
    """Return every filename field referenced by a CDXJ file."""

    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        _, _, meta = parse_cdxj_line(line)
        filename = meta.get("filename")
        if isinstance(filename, str):
            names.add(filename)
    return names


def parse_cdxj_line(line: str) -> tuple[str, str, dict[str, object]]:
    """Parse one CDXJ line into its key, timestamp, and metadata."""

    parts = line.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed CDXJ line: {line!r}")
    try:
        metadata = json.loads(parts[2])
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed CDXJ metadata: {line!r}") from error
    if not isinstance(metadata, dict):
        raise ValueError(f"CDXJ metadata must be an object: {line!r}")
    return parts[0], parts[1], metadata


def validate_cdxj_against_warcs(
    layout: ArchiveLayout,
    collection_id: str,
    lines: Sequence[str],
    *,
    warc_sizes: Mapping[str, int] | None = None,
) -> None:
    """Ensure every CDXJ locator points at an immutable finalized range."""

    sizes = dict(warc_sizes or {})
    if not sizes:
        sizes = {
            path.name: path.stat().st_size
            for path in list_collection_warcs(layout, collection_id)
        }
    warc_names = set(sizes)
    for line in lines:
        _, _, meta = parse_cdxj_line(line)
        filename = meta.get("filename")
        offset = meta.get("offset")
        length = meta.get("length")
        if not isinstance(filename, str):
            raise ValueError("CDXJ entry missing filename")
        if Path(filename).name != filename:
            raise ValueError(f"CDXJ filename must be a WARC basename: {filename}")
        if filename not in warc_names:
            raise ValueError(f"CDXJ references foreign WARC: {filename}")
        size = sizes[filename]
        try:
            offset_i = int(offset)
            length_i = int(length)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid CDXJ offset/length: {meta}") from error
        if offset_i < 0 or length_i <= 0 or offset_i + length_i > size:
            raise ValueError(
                f"CDXJ range out of bounds for {filename}: "
                f"offset={offset_i} length={length_i} size={size}"
            )
