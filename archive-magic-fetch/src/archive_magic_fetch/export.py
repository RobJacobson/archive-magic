"""Per-CDX-key capture export policy."""

from __future__ import annotations

import base64
import binascii
import sys
from dataclasses import dataclass
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


@dataclass
class ExportSummary:
    """Aggregate outcomes for one export operation."""

    selected: int = 0
    responses: int = 0
    revisits: int = 0
    redirects_omitted: int = 0
    playback_failures: int = 0

    def add(self, other: ExportSummary) -> None:
        """Accumulate another group's outcomes."""

        self.selected += other.selected
        self.responses += other.responses
        self.revisits += other.revisits
        self.redirects_omitted += other.redirects_omitted
        self.playback_failures += other.playback_failures


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


def _warn_status_substitution(
    capture: CdxRecord,
    actual_status: int,
) -> None:
    print(
        f"WARNING skipped {_cdx_timestamp(capture.timestamp)} "
        f"{capture.original}: CDX status {capture.statuscode} but "
        f"playback returned {actual_status}",
        file=sys.stderr,
    )


def _is_redirect(status: Optional[int]) -> bool:
    """Return whether a known HTTP status is in the 3xx class."""

    return status is not None and 300 <= status < 400


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
) -> ExportSummary:
    """Export one CDX URL-key group with shared payload deduplication."""

    if not captures:
        raise ValueError(f"capture group is empty: {urlkey}")

    eligible = [
        capture for capture in captures if not _is_redirect(capture.statuscode)
    ]
    summary = ExportSummary(
        selected=len(captures),
        redirects_omitted=len(captures) - len(eligible),
    )
    if not eligible:
        return summary

    source_by_signature: dict[tuple[str, int], SourceMatch] = {}
    source_by_digest: dict[str, SourceMatch] = {}
    content_by_signature: dict[tuple[str, int], CanonicalResponse] = {}
    stream = None
    writer = None

    representative_url = eligible[0].original
    variants = len({capture.original for capture in eligible})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""
    print(f"Starting {representative_url}{suffix}")

    try:
        for capture in eligible:
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
                summary.revisits += 1
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
                summary.playback_failures += 1
                continue

            (
                target_uri,
                capture_date,
                semantic_digest,
                actual_status,
            ) = _response_identity(response)

            if cdx_status is not None and actual_status != cdx_status:
                _warn_status_substitution(capture, actual_status)
                summary.playback_failures += 1
                continue

            if _is_redirect(actual_status):
                summary.redirects_omitted += 1
                continue

            if writer is None:
                stream, writer = open_new_warc(path)

            canonical = content_by_signature.get(
                (semantic_digest, actual_status)
            )
            if canonical is None:
                canonical = write_response(writer, response)
                summary.responses += 1
            else:
                write_revisit(
                    writer,
                    target_uri,
                    capture_date,
                    semantic_digest,
                    canonical,
                )
                summary.revisits += 1

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

    return summary


def export_all(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    output_paths: Mapping[str, Path],
    client,
) -> ExportSummary:
    """Export each CDX URL-key group in discovery order."""

    summary = ExportSummary()
    for urlkey, captures in capture_groups.items():
        summary.add(
            export_group(urlkey, captures, output_paths[urlkey], client)
        )

    print(
        f"Summary: {summary.selected} selected; "
        f"{summary.responses} responses; "
        f"{summary.revisits} revisits; "
        f"{summary.redirects_omitted} redirects omitted; "
        f"{summary.playback_failures} playback failures"
    )
    return summary
