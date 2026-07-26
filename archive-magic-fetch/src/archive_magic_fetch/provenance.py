"""Persist normalized Wayback discovery provenance."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Sequence

from wayback import CdxRecord

from .paths import CollectionLayout
from .publication import publish_directory_noreplace


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
class AcquisitionResult:
    """Published files for one successful discovery acquisition."""

    path: Path
    captures_path: Path
    query_path: Path


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("acquisition and capture times must be timezone-aware")
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


def _cdx_bytes(captures: Sequence[CdxRecord]) -> bytes:
    lines = [CDX_HEADER]
    for capture in captures:
        timestamp = _utc_datetime(capture.timestamp).strftime("%Y%m%d%H%M%S")
        lines.append(
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
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _gzip_bytes(content: bytes, acquired_at: datetime) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=output,
        mtime=int(acquired_at.timestamp()),
    ) as compressed:
        compressed.write(content)
    return output.getvalue()


def _acquisition_id(acquired_at: datetime) -> str:
    return acquired_at.strftime("%Y%m%dT%H%M%S.%fZ")


def _iso_timestamp(acquired_at: datetime) -> str:
    return acquired_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_manifest(
    *,
    captures: Sequence[CdxRecord],
    cdx_gzip: bytes,
    url_pattern: str,
    date_start: str,
    date_end: str,
    acquired_at: datetime,
) -> dict[str, object]:
    """Build the deterministic manifest for one completed source CDX."""

    return {
        "archive_magic_fetch_version": version("archive-magic-fetch"),
        "acquired_at": _iso_timestamp(acquired_at),
        "cdx": {
            "fields": list(CDX_FIELDS),
            "file": "captures.cdx.gz",
            "format": CDX_HEADER,
            "record_count": len(captures),
            "sha256": hashlib.sha256(cdx_gzip).hexdigest(),
        },
        "date_end": date_end,
        "date_start": date_start,
        "schema_version": 1,
        "source": "internet-archive-wayback-machine",
        "url_pattern": url_pattern,
        "wayback_version": version("wayback"),
    }


def _publish_acquisition(
    temporary: Path,
    *,
    source_root: Path,
    acquired_at: datetime,
) -> Path:
    """Publish a complete acquisition, retrying concurrent ID collisions."""

    base = _acquisition_id(acquired_at)
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


def save_acquisition(
    captures: Sequence[CdxRecord],
    *,
    layout: CollectionLayout,
    url_pattern: str,
    date_start: str,
    date_end: str,
    acquired_at: datetime,
) -> AcquisitionResult:
    """Write and atomically publish one complete source acquisition."""

    if not captures:
        raise ValueError("cannot save an empty source acquisition")

    acquired_at = _utc_datetime(acquired_at)
    cdx_gzip = _gzip_bytes(_cdx_bytes(captures), acquired_at)
    manifest = _build_manifest(
        captures=captures,
        cdx_gzip=cdx_gzip,
        url_pattern=url_pattern,
        date_start=date_start,
        date_end=date_end,
        acquired_at=acquired_at,
    )
    query_json = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    source_root = layout.sources_root
    source_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".acquisition-", dir=source_root)
    )
    try:
        (temporary / "captures.cdx.gz").write_bytes(cdx_gzip)
        (temporary / "query.json").write_bytes(query_json)

        final = _publish_acquisition(
            temporary,
            source_root=source_root,
            acquired_at=acquired_at,
        )
        return AcquisitionResult(
            path=final,
            captures_path=final / "captures.cdx.gz",
            query_path=final / "query.json",
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
