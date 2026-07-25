"""Per-CDX-key capture export policy."""

from __future__ import annotations

import base64
import binascii
import sys
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Sequence

from wayback import CdxRecord
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    WaybackRetryError,
)

from .paths import WarcBucket
from .retrieval import RetrievalCache, retrieve_response
from .warc import (
    CanonicalResponse,
    open_new_warc,
    timestamp_to_warc_date,
    write_response,
    write_revisit,
)


SourceMatch = tuple[str, CanonicalResponse]


@dataclass
class _GroupDeduplication:
    """Deduplication state that is intentionally discarded per URL key."""

    source_by_signature: dict[tuple[str, int], SourceMatch] = field(
        default_factory=dict
    )
    source_by_digest: dict[str, SourceMatch] = field(default_factory=dict)
    content_by_signature: dict[
        tuple[str, int],
        CanonicalResponse,
    ] = field(default_factory=dict)

    def find_source(
        self,
        expected: Optional[str],
        status: Optional[int],
    ) -> Optional[SourceMatch]:
        if expected is None:
            return None
        if status is None:
            return self.source_by_digest.get(expected)
        return self.source_by_signature.get((expected, status))

    def find_content(
        self,
        digest: str,
        status: int,
    ) -> Optional[CanonicalResponse]:
        return self.content_by_signature.get((digest, status))

    def remember(
        self,
        *,
        expected: Optional[str],
        source_status: Optional[int],
        semantic_digest: str,
        actual_status: int,
        canonical: CanonicalResponse,
    ) -> None:
        self.content_by_signature.setdefault(
            (semantic_digest, actual_status),
            canonical,
        )
        if expected is None:
            return
        source_match = (semantic_digest, canonical)
        self.source_by_digest.setdefault(expected, source_match)
        if source_status is not None:
            self.source_by_signature.setdefault(
                (expected, source_status),
                source_match,
            )


@dataclass
class _LazyWarc:
    """Own one WARC stream that opens only when the first record is written."""

    path: Path
    stream: Optional[BinaryIO] = None
    writer: object = None

    @property
    def created(self) -> bool:
        return self.stream is not None

    def get_writer(self):
        if self.writer is None:
            self.stream, self.writer = open_new_warc(self.path)
        return self.writer

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


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


@dataclass(frozen=True)
class ExportResult:
    """Aggregate export outcome and WARCs closed by this command."""

    summary: ExportSummary
    created_warcs: tuple[Path, ...]


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


def _export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    client,
    writer_factory: Callable[[], object],
    *,
    cache: Optional[RetrievalCache] = None,
) -> ExportSummary:
    """Export one URL-key group using fresh payload-deduplication state."""

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

    deduplication = _GroupDeduplication()
    writer = None

    representative_url = eligible[0].original
    variants = len({capture.original for capture in eligible})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""
    print(f"Starting {representative_url}{suffix}")

    for capture in eligible:
        expected = normalize_digest(capture.digest)
        cdx_status = capture.statuscode

        source_match = deduplication.find_source(expected, cdx_status)

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
            response = retrieve_response(client, capture, cache=cache)
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
            writer = writer_factory()

        canonical = deduplication.find_content(
            semantic_digest,
            actual_status,
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

        deduplication.remember(
            expected=expected,
            source_status=cdx_status,
            semantic_digest=semantic_digest,
            actual_status=actual_status,
            canonical=canonical,
        )

    return summary


def export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    path: Path,
    client,
    *,
    cache: Optional[RetrievalCache] = None,
) -> ExportSummary:
    """Export one group to one lazily created WARC."""

    owner = _LazyWarc(path)
    try:
        return _export_group(
            urlkey,
            captures,
            client,
            owner.get_writer,
            cache=cache,
        )
    finally:
        owner.close()


def _export_bucket(
    bucket: WarcBucket,
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    client,
    *,
    cache: Optional[RetrievalCache] = None,
) -> tuple[ExportSummary, bool]:
    """Export every URL-key group assigned to one lazy WARC owner."""

    owner = _LazyWarc(bucket.path)
    summary = ExportSummary()
    try:
        for urlkey in bucket.urlkeys:
            summary.add(
                _export_group(
                    urlkey,
                    capture_groups[urlkey],
                    client,
                    owner.get_writer,
                    cache=cache,
                )
            )
    finally:
        owner.close()
    return summary, owner.created


def export_all(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    buckets: Sequence[WarcBucket],
    client,
    *,
    cache: Optional[RetrievalCache] = None,
) -> ExportResult:
    """Export ordered buckets while keeping deduplication scoped to groups."""

    summary = ExportSummary()
    created_warcs = []
    for bucket in buckets:
        bucket_summary, created = _export_bucket(
            bucket,
            capture_groups,
            client,
            cache=cache,
        )
        summary.add(bucket_summary)
        if created:
            created_warcs.append(bucket.path)

    return ExportResult(summary, tuple(created_warcs))


def print_summary(
    summary: ExportSummary,
    *,
    warc_mode: str = "all",
) -> None:
    """Print the WARC aggregate summary after WARC output is complete."""

    if warc_mode == "none":
        print("Summary: warc disabled (none)")
        return

    print(
        f"Summary: {summary.selected} selected for warc ({warc_mode}); "
        f"{summary.responses} responses; "
        f"{summary.revisits} revisits; "
        f"{summary.redirects_omitted} redirects omitted; "
        f"{summary.playback_failures} playback failures"
    )
