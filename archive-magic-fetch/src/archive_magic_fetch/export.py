"""Per-CDX-key capture export policy."""

from __future__ import annotations

import base64
import binascii
import sys
from datetime import timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from wayback import CdxRecord
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    WaybackRetryError,
)

from .retrieval import retrieve_response
from .warc import (
    CanonicalResponse,
    open_new_warc,
    timestamp_to_warc_date,
    write_response,
    write_revisit,
)


SourceMatch = tuple[str, CanonicalResponse]


def normalize_digest(value: object) -> Optional[str]:
    """Normalize a usable SHA-1 Base32 digest to warcio's representation."""

    if not isinstance(value, str):
        return None

    digest = value.strip()
    if not digest or digest == "-":
        return None

    if ":" in digest:
        algorithm, digest = digest.split(":", 1)
        if algorithm.lower() != "sha1":
            return None

    encoded = digest.upper()
    if len(encoded) != 32:
        return None

    try:
        decoded = base64.b32decode(encoded, casefold=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 20:
        return None

    return f"sha1:{encoded}"


def _cdx_timestamp(timestamp) -> str:
    """Format a Wayback timestamp for compact progress output."""

    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _warn_skip(capture: CdxRecord, error: Exception) -> None:
    reason = str(error) or type(error).__name__
    print(
        f"WARNING skipped {_cdx_timestamp(capture.timestamp)} "
        f"{capture.original}: {reason}",
        file=sys.stderr,
    )


def _response_identity(record) -> tuple[str, str, str, int]:
    """Read the semantic identity needed for deduplication and revisits."""

    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    capture_date = record.rec_headers.get_header("WARC-Date")
    digest = normalize_digest(
        record.rec_headers.get_header("WARC-Payload-Digest")
    )
    status_text = (
        record.http_headers.get_statuscode()
        if record.http_headers is not None
        else None
    )
    if not target_uri or not capture_date:
        raise ValueError("response is missing required WARC identity headers")
    if digest is None:
        raise ValueError("response is missing a usable WARC payload digest")
    if status_text is None or not status_text.isdigit():
        raise ValueError("response is missing a numeric HTTP status")
    return target_uri, capture_date, digest, int(status_text)


def export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    path: Path,
    client,
) -> None:
    """Export one CDX URL-key group with shared payload deduplication."""

    if not captures:
        raise ValueError(f"capture group is empty: {urlkey}")

    source_by_signature: dict[tuple[str, int], SourceMatch] = {}
    source_by_digest: dict[str, SourceMatch] = {}
    content_by_signature: dict[tuple[str, int], CanonicalResponse] = {}
    stream = None
    writer = None

    representative_url = captures[0].original
    variants = len({capture.original for capture in captures})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""
    print(f"Starting {representative_url}{suffix}")

    try:
        for capture in captures:
            expected = normalize_digest(capture.digest)
            cdx_status = capture.statuscode

            source_match = None
            if expected is not None:
                if cdx_status is None:
                    source_match = source_by_digest.get(expected)
                else:
                    source_match = source_by_signature.get(
                        (expected, cdx_status)
                    )

            if source_match is not None:
                if writer is None:  # pragma: no cover - map/writer invariant
                    raise RuntimeError(
                        "canonical response exists without an open WARC"
                    )
                semantic_digest, canonical = source_match
                write_revisit(
                    writer,
                    capture.original,
                    timestamp_to_warc_date(capture.timestamp),
                    semantic_digest,
                    canonical,
                )
                continue

            try:
                response = retrieve_response(client, capture)
            except (
                MementoPlaybackError,
                BlockedByRobotsError,
                BlockedSiteError,
                WaybackRetryError,
            ) as error:
                _warn_skip(capture, error)
                continue

            (
                target_uri,
                capture_date,
                semantic_digest,
                actual_status,
            ) = _response_identity(response)

            if writer is None:
                stream, writer = open_new_warc(path)

            canonical = content_by_signature.get(
                (semantic_digest, actual_status)
            )
            if canonical is None:
                canonical = write_response(writer, response)
            else:
                write_revisit(
                    writer,
                    target_uri,
                    capture_date,
                    semantic_digest,
                    canonical,
                )

            print(
                f"Downloaded {_cdx_timestamp(capture.timestamp)} "
                f"[{semantic_digest[-8:]}]"
            )

            content_by_signature.setdefault(
                (semantic_digest, actual_status),
                canonical,
            )
            if expected is not None:
                source_match = (semantic_digest, canonical)
                source_by_digest.setdefault(expected, source_match)
                if cdx_status is not None:
                    source_by_signature.setdefault(
                        (expected, cdx_status),
                        source_match,
                    )
    finally:
        if stream is not None:
            stream.close()


def export_all(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    output_paths: Mapping[str, Path],
    client,
) -> None:
    """Export each CDX URL-key group in discovery order."""

    for urlkey, captures in capture_groups.items():
        export_group(urlkey, captures, output_paths[urlkey], client)
