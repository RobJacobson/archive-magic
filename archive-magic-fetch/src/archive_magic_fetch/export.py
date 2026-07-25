"""Per-CDX-key capture export policy."""

from __future__ import annotations

import sys
from dataclasses import dataclass
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
from .retrieval import (
    DEFAULT_CONCURRENCY,
    MalformedContentEncodingError,
    MementoFetchPool,
    MementoFetchWindow,
    RateLimitCooldown,
    TruncatedWaybackResponseError,
    format_playback_failure,
    format_playback_failure_summary,
    print_fetched,
    print_progress,
    retrieve_response,
)
from .warc import open_new_warc, write_response


@dataclass
class _LazyWarc:
    """Own one WARC stream that opens only when the first record is written."""

    path: Path
    stream: Optional[BinaryIO] = None
    writer: object = None

    @property
    def available(self) -> bool:
        return self.writer is not None

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
    redirects_omitted: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0

    def add(self, other: ExportSummary) -> None:
        """Accumulate another group's outcomes."""

        self.selected += other.selected
        self.responses += other.responses
        self.redirects_omitted += other.redirects_omitted
        self.playback_failures += other.playback_failures
        self.invalid_content_encoding_failures += (
            other.invalid_content_encoding_failures
        )
        self.truncated_response_failures += (
            other.truncated_response_failures
        )

    def record_playback_failure(
        self,
        error: Optional[Exception] = None,
    ) -> None:
        """Count one playback failure and its actionable category."""

        self.playback_failures += 1
        if isinstance(error, MalformedContentEncodingError):
            self.invalid_content_encoding_failures += 1
        elif isinstance(error, TruncatedWaybackResponseError):
            self.truncated_response_failures += 1


@dataclass(frozen=True)
class ExportResult:
    """Aggregate export outcome and validated WARCs in the current plan."""

    summary: ExportSummary
    created_warcs: tuple[Path, ...]


def _cdx_timestamp(timestamp) -> str:
    """Format a Wayback timestamp for compact progress output."""

    value = timestamp.astimezone(timezone.utc)
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}"
    )


def _warn_skip(capture: CdxRecord, error: Exception) -> None:
    reason = format_playback_failure(error)
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


def _response_status_and_digest(record) -> tuple[int, str]:
    """Return the status and payload digest of a retrieved response."""

    digest = record.rec_headers.get_header("WARC-Payload-Digest")
    status_text = (
        record.http_headers.get_statuscode()
        if record.http_headers is not None
        else None
    )
    if not digest:
        raise ValueError("response is missing a payload digest")
    if status_text is None or not status_text.isdigit():
        raise ValueError("response is missing a numeric HTTP status")
    return int(status_text), digest


def _export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    client,
    writer_factory: Callable[[], object],
    *,
    cooldown: RateLimitCooldown,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fetch_window: Optional[MementoFetchWindow] = None,
) -> ExportSummary:
    """Fetch and write every eligible capture in one URL-key group."""

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

    representative_url = eligible[0].original
    variants = len({capture.original for capture in eligible})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""

    print_progress(f"Starting {representative_url}{suffix}")

    owned_pool = None
    if (
        fetch_window is None
        and client_factory is not None
        and concurrency > 1
    ):
        owned_pool = MementoFetchPool(
            cooldown=cooldown,
            client_factory=client_factory,
            max_workers=concurrency,
            on_fetched=print_fetched,
        )
        fetch_window = owned_pool.window(eligible)

    writer = None

    try:
        for capture in eligible:
            cdx_status = capture.statuscode

            try:
                retrieved = (
                    fetch_window.wait(capture)
                    if fetch_window is not None
                    else None
                )
                if retrieved is None:
                    response = retrieve_response(
                        client,
                        capture,
                        cooldown=cooldown,
                    )
                else:
                    response = retrieved.to_warc_record()
            except (
                MementoPlaybackError,
                BlockedByRobotsError,
                BlockedSiteError,
                WaybackRetryError,
            ) as error:
                _warn_skip(capture, error)
                summary.record_playback_failure(error)
                continue

            if retrieved is None:
                print_fetched(capture)

            actual_status, payload_digest = _response_status_and_digest(
                response
            )

            if cdx_status is not None and actual_status != cdx_status:
                _warn_status_substitution(capture, actual_status)
                summary.record_playback_failure()
                continue

            if _is_redirect(actual_status):
                summary.redirects_omitted += 1
                continue

            if writer is None:
                writer = writer_factory()

            write_response(writer, response)
            summary.responses += 1

            print_progress(
                f"Wrote {_cdx_timestamp(capture.timestamp)} "
                f"[{payload_digest[-8:]}]"
            )

    finally:
        if owned_pool is not None:
            owned_pool.close()

    return summary


def export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    path: Path,
    client,
    *,
    cooldown: Optional[RateLimitCooldown] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fetch_window: Optional[MementoFetchWindow] = None,
) -> ExportSummary:
    """Export one group to one exclusively created WARC."""

    active_cooldown = cooldown or RateLimitCooldown()
    owner = _LazyWarc(path)
    try:
        return _export_group(
            urlkey,
            captures,
            client,
            owner.get_writer,
            cooldown=active_cooldown,
            client_factory=client_factory,
            concurrency=concurrency,
            fetch_window=fetch_window,
        )
    finally:
        owner.close()


def _export_bucket(
    bucket: WarcBucket,
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    client,
    *,
    cooldown: RateLimitCooldown,
    fetch_window: Optional[MementoFetchWindow] = None,
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
                    cooldown=cooldown,
                    fetch_window=fetch_window,
                )
            )
    finally:
        owner.close()
    return summary, owner.available


def export_all(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    buckets: Sequence[WarcBucket],
    client,
    *,
    cooldown: Optional[RateLimitCooldown] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> ExportResult:
    """Fetch and write every eligible capture in ordered WARC buckets."""

    summary = ExportSummary()
    created_warcs = []
    active_cooldown = cooldown or RateLimitCooldown()
    pool = None
    fetch_window = None
    if (
        client_factory is not None
        and concurrency > 1
    ):
        ordered_fetches = []
        for bucket in buckets:
            for urlkey in bucket.urlkeys:
                ordered_fetches.extend(
                    capture
                    for capture in capture_groups[urlkey]
                    if not _is_redirect(capture.statuscode)
                )
        pool = MementoFetchPool(
            cooldown=active_cooldown,
            client_factory=client_factory,
            max_workers=concurrency,
            on_fetched=print_fetched,
        )
        fetch_window = pool.window(ordered_fetches)

    try:
        for bucket in buckets:
            bucket_summary, created = _export_bucket(
                bucket,
                capture_groups,
                client,
                cooldown=active_cooldown,
                fetch_window=fetch_window,
            )
            summary.add(bucket_summary)
            if created:
                created_warcs.append(bucket.path)
    finally:
        if pool is not None:
            pool.close()

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

    failures = format_playback_failure_summary(
        summary.playback_failures,
        invalid_content_encoding=(
            summary.invalid_content_encoding_failures
        ),
        truncated_response=summary.truncated_response_failures,
    )
    print(
        f"Summary: {summary.selected} selected for warc ({warc_mode}); "
        f"{summary.responses} responses; "
        f"{summary.redirects_omitted} redirects omitted; "
        f"{failures}"
    )
