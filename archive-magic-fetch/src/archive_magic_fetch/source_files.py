"""Persist normalized Internet Archive search results."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Sequence

from wayback import CdxRecord

from .collection_paths import CollectionPaths
from .atomic_files import publish_directory_noreplace


CDX_HEADER = "CDX N b a m s k S"
CDX_FIELDS = (
    "urlkey",
    "timestamp",
    "original",
    "mimetype",
    "statuscode",
    "digest",
    "length",
)


@dataclass(frozen=True)
class SearchFiles:
    """Published source files for one successful CDX search."""

    path: Path
    captures_path: Path
    query_path: Path


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("search and capture times must be timezone-aware")
    return value.astimezone(timezone.utc)


def _token(value: object, *, field: str) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"CDX {field} must be a non-empty token")
    if any(character.isspace() for character in value):
        raise ValueError(f"CDX {field} cannot contain whitespace")
    return value


def _cdx_line(capture: CdxRecord) -> bytes:
    timestamp = _utc_datetime(capture.timestamp).strftime("%Y%m%d%H%M%S")
    return (
        " ".join(
            (
                _token(capture.urlkey, field="urlkey"),
                timestamp,
                _token(capture.original, field="original"),
                _token(capture.mimetype, field="mimetype"),
                _token(capture.statuscode, field="statuscode"),
                _token(capture.digest, field="digest"),
                _token(capture.length, field="length"),
            )
        )
        + "\n"
    ).encode("utf-8")


def _write_cdx_gzip(
    path: Path,
    captures: Sequence[CdxRecord],
    acquired_at: datetime,
) -> int:
    """Stream one deterministic CDX gzip and return its record count."""

    count = 0
    with path.open("xb") as output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=output,
            mtime=int(acquired_at.timestamp()),
        ) as compressed:
            compressed.write(f"{CDX_HEADER}\n".encode("ascii"))
            for capture in captures:
                compressed.write(_cdx_line(capture))
                count += 1
    return count


def _search_id(acquired_at: datetime) -> str:
    return acquired_at.strftime("%Y%m%dT%H%M%S.%fZ")


def _iso_timestamp(acquired_at: datetime) -> str:
    return acquired_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_manifest(
    *,
    record_count: int,
    cdx_sha256: str,
    url_pattern: str,
    date_start: str,
    date_end: str,
    acquired_at: datetime,
) -> dict[str, object]:
    """Build the deterministic manifest for one saved source CDX."""

    return {
        "archive_magic_fetch_version": version("archive-magic-fetch"),
        "acquired_at": _iso_timestamp(acquired_at),
        "cdx": {
            "fields": list(CDX_FIELDS),
            "file": "captures.cdx.gz",
            "format": CDX_HEADER,
            "record_count": record_count,
            "sha256": cdx_sha256,
        },
        "date_end": date_end,
        "date_start": date_start,
        "schema_version": 1,
        "source": "internet-archive-wayback-machine",
        "url_pattern": url_pattern,
        "wayback_version": version("wayback"),
    }


def _publish_search(
    temporary: Path,
    *,
    source_root: Path,
    acquired_at: datetime,
) -> Path:
    """Publish complete search files, retrying concurrent ID collisions."""

    base = _search_id(acquired_at)
    suffix = 1
    while True:
        identifier = base if suffix == 1 else f"{base}-{suffix}"
        final = source_root / identifier
        try:
            publish_directory_noreplace(temporary, final)
        except FileExistsError:
            suffix += 1
            continue
        return final


def save_search_results(
    captures: Sequence[CdxRecord],
    *,
    layout: CollectionPaths,
    url_pattern: str,
    date_start: str,
    date_end: str,
    acquired_at: datetime,
) -> SearchFiles:
    """Write and atomically publish one complete set of search files."""

    if not captures:
        raise ValueError("cannot save empty search results")

    acquired_at = _utc_datetime(acquired_at)
    source_root = layout.sources_root
    source_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".search-", dir=source_root)
    )
    try:
        captures_path = temporary / "captures.cdx.gz"
        record_count = _write_cdx_gzip(
            captures_path,
            captures,
            acquired_at,
        )
        with captures_path.open("rb") as capture_file:
            cdx_sha256 = hashlib.file_digest(capture_file, "sha256").hexdigest()
        manifest = _build_manifest(
            record_count=record_count,
            cdx_sha256=cdx_sha256,
            url_pattern=url_pattern,
            date_start=date_start,
            date_end=date_end,
            acquired_at=acquired_at,
        )
        query_json = (
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        (temporary / "query.json").write_bytes(query_json)

        final = _publish_search(
            temporary,
            source_root=source_root,
            acquired_at=acquired_at,
        )
        return SearchFiles(
            path=final,
            captures_path=final / "captures.cdx.gz",
            query_path=final / "query.json",
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
