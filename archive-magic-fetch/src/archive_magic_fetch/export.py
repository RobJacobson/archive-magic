"""Per-CDX-key capture export policy."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Sequence

from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import ArchiveLoadFailed
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
    RetrievalCache,
    TruncatedWaybackResponseError,
    format_playback_failure,
    format_playback_failure_summary,
    print_fetched,
    print_progress,
    retrieve_response,
)
from .warc import (
    CAPTURE_ID_HEADER,
    CanonicalResponse,
    open_append_warc,
    open_new_warc,
    timestamp_to_warc_date,
    write_response,
    write_revisit,
)


SourceMatch = tuple[str, CanonicalResponse]
_MEMENTO_TIMESTAMP = re.compile(r"/web/(\d{14})[^/]*/")


@dataclass(frozen=True)
class _StoredCapture:
    """Deduplication identity recovered from one existing WARC record."""

    semantic_digest: str
    actual_status: Optional[int]
    canonical: CanonicalResponse


def _capture_id(urlkey: str, capture: CdxRecord) -> str:
    """Return a stable identity for one selected source CDX capture."""

    identity = json.dumps(
        [
            urlkey,
            _cdx_timestamp(capture.timestamp),
            capture.original,
            capture.statuscode,
            normalize_digest(capture.digest),
            capture.mimetype,
            capture.length,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


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
        actual_status: Optional[int],
        canonical: CanonicalResponse,
    ) -> None:
        if actual_status is not None:
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
    existed: bool = False

    @property
    def available(self) -> bool:
        return self.path.exists()

    def get_writer(self):
        if self.writer is None:
            if self.existed:
                self.stream, self.writer = open_append_warc(self.path)
            else:
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
    already_present: int = 0
    redirects_omitted: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0

    def add(self, other: ExportSummary) -> None:
        """Accumulate another group's outcomes."""

        self.selected += other.selected
        self.responses += other.responses
        self.revisits += other.revisits
        self.already_present += other.already_present
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


def _source_plan_key(
    expected: Optional[str],
    status: Optional[int],
) -> Optional[tuple]:
    """Return a planning key for CDX source-signature revisit eligibility."""

    if expected is None:
        return None
    if status is None:
        return ("digest", expected)
    return ("signature", expected, status)


def plan_group_fetches(captures: Sequence[CdxRecord]) -> list[CdxRecord]:
    """Return captures that need network fetch assuming earlier CDX success.

    Later captures that share a CDX digest/status with an earlier eligible
    capture are omitted; if the earlier fetch fails at write time, the write
    loop retrieves the later capture on demand.
    """

    return _plan_group_fetches(captures, {})


def _plan_group_fetches(
    captures: Sequence[CdxRecord],
    existing: Mapping[CdxRecord, _StoredCapture],
) -> list[CdxRecord]:
    """Plan fetches while treating stored captures as successful sources."""

    planned: list[CdxRecord] = []
    seen: set[tuple] = set()
    for capture in captures:
        if _is_redirect(capture.statuscode):
            continue
        expected = normalize_digest(capture.digest)
        key = _source_plan_key(expected, capture.statuscode)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        if capture in existing:
            continue
        planned.append(capture)
    return planned


def _record_canonical(record) -> CanonicalResponse:
    """Return the response ultimately referenced by an existing record."""

    if record.rec_type == "response":
        record_id = record.rec_headers.get_header("WARC-Record-ID")
        target_uri = record.rec_headers.get_header("WARC-Target-URI")
        capture_date = record.rec_headers.get_header("WARC-Date")
    else:
        record_id = record.rec_headers.get_header("WARC-Refers-To")
        target_uri = record.rec_headers.get_header(
            "WARC-Refers-To-Target-URI"
        )
        capture_date = record.rec_headers.get_header("WARC-Refers-To-Date")
    if not record_id or not target_uri or not capture_date:
        raise ValueError("existing WARC record has incomplete canonical identity")
    return CanonicalResponse(record_id, target_uri, capture_date)


def _legacy_record_match(
    record,
    captures_by_timestamp: Mapping[str, Sequence[CdxRecord]],
) -> Optional[CdxRecord]:
    """Match records written before persistent capture IDs were introduced."""

    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    warc_date = record.rec_headers.get_header("WARC-Date")
    source_uri = record.rec_headers.get_header("WARC-Source-URI") or ""
    source_match = _MEMENTO_TIMESTAMP.search(source_uri)
    timestamp = source_match.group(1) if source_match else None
    if timestamp is None and warc_date:
        timestamp = "".join(
            character for character in warc_date if character.isdigit()
        )[:14]
    if timestamp is None:
        return None

    candidates = list(captures_by_timestamp.get(timestamp, ()))
    if target_uri:
        exact = [
            capture
            for capture in candidates
            if capture.original == target_uri
            or source_uri.endswith(capture.original)
        ]
        if exact:
            candidates = exact

    if record.http_headers is not None:
        status_text = record.http_headers.get_statuscode()
        if status_text and status_text.isdigit():
            status = int(status_text)
            matching_status = [
                capture
                for capture in candidates
                if capture.statuscode in {None, status}
            ]
            if matching_status:
                candidates = matching_status
    return candidates[0] if len(candidates) == 1 else None


def _load_existing_captures(
    path: Path,
    groups: Mapping[str, Sequence[CdxRecord]],
) -> dict[str, dict[CdxRecord, _StoredCapture]]:
    """Validate an existing WARC and recover captures already committed."""

    recovered = {urlkey: {} for urlkey in groups}
    if not path.exists():
        return recovered

    captures_by_id = {
        _capture_id(urlkey, capture): (urlkey, capture)
        for urlkey, captures in groups.items()
        for capture in captures
    }
    captures_by_timestamp: dict[str, list[CdxRecord]] = {}
    for captures in groups.values():
        for capture in captures:
            captures_by_timestamp.setdefault(
                _cdx_timestamp(capture.timestamp),
                [],
            ).append(capture)
    saw_warcinfo = False
    try:
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream):
                if record.rec_type == "warcinfo":
                    saw_warcinfo = True
                    continue
                if record.rec_type not in {"response", "revisit"}:
                    continue

                capture_id = record.rec_headers.get_header(CAPTURE_ID_HEADER)
                selected = (
                    captures_by_id.get(capture_id) if capture_id else None
                )
                if selected is None:
                    capture = _legacy_record_match(
                        record,
                        captures_by_timestamp,
                    )
                    if capture is None:
                        continue
                    selected = (capture.urlkey, capture)
                urlkey, capture = selected

                digest = normalize_digest(
                    record.rec_headers.get_header("WARC-Payload-Digest")
                )
                if digest is None:
                    raise ValueError(
                        "existing WARC record has no usable payload digest: "
                        f"{path}"
                    )
                status = capture.statuscode
                if record.http_headers is not None:
                    status_text = record.http_headers.get_statuscode()
                    if status_text and status_text.isdigit():
                        status = int(status_text)
                recovered[urlkey][capture] = _StoredCapture(
                    digest,
                    status,
                    _record_canonical(record),
                )
    except ArchiveLoadFailed as error:
        raise ValueError(
            f"cannot resume malformed existing WARC: {path}"
        ) from error

    if not saw_warcinfo:
        raise ValueError(f"existing WARC has no warcinfo record: {path}")
    return recovered


def _export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    client,
    writer_factory: Callable[[], object],
    *,
    cache: Optional[RetrievalCache] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fetch_window: Optional[MementoFetchWindow] = None,
    existing: Optional[Mapping[CdxRecord, _StoredCapture]] = None,
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

    representative_url = eligible[0].original
    variants = len({capture.original for capture in eligible})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""

    existing = existing or {}
    if all(capture in existing for capture in eligible):
        summary.already_present = len(eligible)
        if cache is not None:
            for capture in eligible:
                cache.discard(capture)
        print_progress(
            f"Skipping {representative_url}{suffix} (already captured)"
        )
        return summary

    print_progress(f"Starting {representative_url}{suffix}")

    owned_pool = None
    if (
        fetch_window is None
        and cache is not None
        and client_factory is not None
        and concurrency > 1
    ):
        owned_pool = MementoFetchPool(
            cache=cache,
            client_factory=client_factory,
            max_workers=concurrency,
            on_fetched=print_fetched,
        )
        fetch_window = owned_pool.window(
            _plan_group_fetches(eligible, existing)
        )

    deduplication = _GroupDeduplication()
    writer = None

    try:
        for capture in eligible:
            expected = normalize_digest(capture.digest)
            cdx_status = capture.statuscode
            stored = existing.get(capture)
            if stored is not None:
                deduplication.remember(
                    expected=expected,
                    source_status=cdx_status,
                    semantic_digest=stored.semantic_digest,
                    actual_status=stored.actual_status,
                    canonical=stored.canonical,
                )
                summary.already_present += 1
                if cache is not None:
                    cache.discard(capture)
                continue

            source_match = deduplication.find_source(expected, cdx_status)

            if source_match is not None:
                if writer is None:
                    writer = writer_factory()
                semantic_digest, canonical = source_match
                write_revisit(
                    writer,
                    capture.original,
                    timestamp_to_warc_date(capture.timestamp),
                    semantic_digest,
                    canonical,
                    capture_id=_capture_id(urlkey, capture),
                )
                summary.revisits += 1
                if cache is not None:
                    cache.discard(capture)
                continue

            fetched_by_worker = False
            if fetch_window is not None:
                fetched_by_worker = fetch_window.wait(capture)
            was_cached = (
                cache is not None and cache.get(capture) is not None
            )

            try:
                response = retrieve_response(client, capture, cache=cache)
            except (
                MementoPlaybackError,
                BlockedByRobotsError,
                BlockedSiteError,
                WaybackRetryError,
            ) as error:
                _warn_skip(capture, error)
                summary.record_playback_failure(error)
                if cache is not None:
                    cache.discard(capture)
                continue

            if not fetched_by_worker and not was_cached:
                print_fetched(capture)

            (
                target_uri,
                capture_date,
                semantic_digest,
                actual_status,
            ) = _response_identity(response)

            if cdx_status is not None and actual_status != cdx_status:
                _warn_status_substitution(capture, actual_status)
                summary.record_playback_failure()
                if cache is not None:
                    cache.discard(capture)
                continue

            if _is_redirect(actual_status):
                summary.redirects_omitted += 1
                if cache is not None:
                    cache.discard(capture)
                continue

            if writer is None:
                writer = writer_factory()

            canonical = deduplication.find_content(
                semantic_digest,
                actual_status,
            )
            if canonical is None:
                response.rec_headers.add_header(
                    CAPTURE_ID_HEADER,
                    _capture_id(urlkey, capture),
                )
                canonical = write_response(writer, response)
                summary.responses += 1
            else:
                write_revisit(
                    writer,
                    target_uri,
                    capture_date,
                    semantic_digest,
                    canonical,
                    capture_id=_capture_id(urlkey, capture),
                )
                summary.revisits += 1

            print_progress(
                f"Wrote {_cdx_timestamp(capture.timestamp)} "
                f"[{semantic_digest[-8:]}]"
            )

            deduplication.remember(
                expected=expected,
                source_status=cdx_status,
                semantic_digest=semantic_digest,
                actual_status=actual_status,
                canonical=canonical,
            )
            if cache is not None:
                cache.discard(capture)
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
    cache: Optional[RetrievalCache] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fetch_window: Optional[MementoFetchWindow] = None,
) -> ExportSummary:
    """Export one group to one lazily created WARC."""

    groups = {urlkey: captures}
    recovered = _load_existing_captures(path, groups)
    owner = _LazyWarc(path, existed=path.exists())
    try:
        return _export_group(
            urlkey,
            captures,
            client,
            owner.get_writer,
            cache=cache,
            client_factory=client_factory,
            concurrency=concurrency,
            fetch_window=fetch_window,
            existing=recovered[urlkey],
        )
    finally:
        owner.close()


def _export_bucket(
    bucket: WarcBucket,
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    client,
    *,
    cache: Optional[RetrievalCache] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fetch_window: Optional[MementoFetchWindow] = None,
    existing: Optional[Mapping[str, Mapping[CdxRecord, _StoredCapture]]] = None,
) -> tuple[ExportSummary, bool]:
    """Export every URL-key group assigned to one lazy WARC owner."""

    owner = _LazyWarc(bucket.path, existed=bucket.path.exists())
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
                    client_factory=client_factory,
                    concurrency=concurrency,
                    fetch_window=fetch_window,
                    existing=(existing or {}).get(urlkey, {}),
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
    cache: Optional[RetrievalCache] = None,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> ExportResult:
    """Export ordered buckets while keeping deduplication scoped to groups."""

    summary = ExportSummary()
    created_warcs = []
    existing_by_bucket = {}
    for bucket in buckets:
        bucket_groups = {
            urlkey: capture_groups[urlkey] for urlkey in bucket.urlkeys
        }
        existing_by_bucket[bucket.path] = _load_existing_captures(
            bucket.path,
            bucket_groups,
        )
    pool = None
    fetch_window = None
    if (
        cache is not None
        and client_factory is not None
        and concurrency > 1
    ):
        ordered_fetches = []
        for bucket in buckets:
            for urlkey in bucket.urlkeys:
                ordered_fetches.extend(
                    _plan_group_fetches(
                        capture_groups[urlkey],
                        existing_by_bucket[bucket.path][urlkey],
                    )
                )
        pool = MementoFetchPool(
            cache=cache,
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
                cache=cache,
                concurrency=concurrency,
                fetch_window=fetch_window,
                existing=existing_by_bucket[bucket.path],
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
        f"{summary.revisits} revisits; "
        f"{summary.already_present} already present; "
        f"{summary.redirects_omitted} redirects omitted; "
        f"{failures}"
    )
