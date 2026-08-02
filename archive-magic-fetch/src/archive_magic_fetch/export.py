"""Concurrent per-URL-group WARC and loose-file export."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timezone
from heapq import merge
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Sequence

from wayback import CdxRecord
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    WaybackRetryError,
)

from .console import GroupReporter, capture_result_line, print_progress
from .files import FilesSummary, write_body
from .paths import (
    CollectionLayout,
    WebsitePlan,
    allocate_warc_paths,
    website_relative_parts,
)
from .retrieval import (
    DEFAULT_CONCURRENCY,
    MalformedContentEncodingError,
    RetrievedMemento,
    TruncatedWaybackResponseError,
    format_playback_failure,
    format_playback_failure_summary,
    normalize_cdx_digest,
    retrieve_memento,
)
from .retry import DEFAULT_RETRIES, RetryExhaustedError
from .warc import (
    ExistingWarcCache,
    RevisitReference,
    open_new_warc,
    response_reference,
    timestamp_to_warc_date,
    validate_warc,
    write_response,
    write_revisit,
)


_EMPTY_PAYLOAD_DIGEST = "sha1:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"
_PLAYBACK_ERRORS = (
    MementoPlaybackError,
    RetryExhaustedError,
    BlockedByRobotsError,
    BlockedSiteError,
    WaybackRetryError,
)


@dataclass
class _WarcRebuilder:
    """Own one exclusively reserved temporary WARC and final publication."""

    final_path: Path
    temporary_path: Path
    stream: Optional[BinaryIO] = None
    writer: object = None
    owns_temporary: bool = False

    @classmethod
    def open(cls, final_path: Path) -> _WarcRebuilder:
        temporary_path = final_path.with_name(final_path.name + ".tmp")
        return cls(final_path, temporary_path)

    def get_writer(self):
        if self.writer is None:
            self.stream, self.writer = open_new_warc(
                self.temporary_path,
                self.final_path.name,
            )
            self.owns_temporary = True
        return self.writer

    def publish(self, *, has_records: bool) -> Optional[Path]:
        if self.stream is not None:
            self.stream.close()
        if not has_records:
            if self.owns_temporary and self.temporary_path.exists():
                self.temporary_path.unlink()
            return self.final_path if self.final_path.is_file() else None
        validate_warc(self.temporary_path)
        os.replace(self.temporary_path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        if self.stream is not None and not self.stream.closed:
            self.stream.close()
        if self.owns_temporary and self.temporary_path.exists():
            self.temporary_path.unlink()


@dataclass
class ExportSummary:
    """Aggregate WARC outcomes for one export operation."""

    selected: int = 0
    responses: int = 0
    revisits: int = 0
    redirects_omitted: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0

    def add(self, other: ExportSummary) -> None:
        self.selected += other.selected
        self.responses += other.responses
        self.revisits += other.revisits
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
        self.playback_failures += 1
        if isinstance(error, MalformedContentEncodingError):
            self.invalid_content_encoding_failures += 1
        elif isinstance(error, TruncatedWaybackResponseError):
            self.truncated_response_failures += 1


@dataclass(frozen=True)
class ExportResult:
    """Aggregate outcomes and final WARCs from the combined exporter."""

    summary: ExportSummary
    final_warcs: tuple[Path, ...]
    files_summary: FilesSummary = field(default_factory=FilesSummary)
    failed_capture_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ExportJob:
    """One exclusive WARC owner, or one standalone file-only URL group."""

    warc_path: Optional[Path]
    urlkeys: tuple[str, ...]


@dataclass
class _GroupResult:
    warc: ExportSummary
    files: FilesSummary
    failed_capture_urls: list[str]


@dataclass
class _GroupState:
    """All mutable state whose lifetime is exactly one URL group."""

    warc_ids: set[int]
    writer_factory: Optional[Callable[[], object]]
    existing_warc: Optional[ExistingWarcCache]
    files_mode: str
    collection_root: Optional[Path]
    warc: ExportSummary
    files: FilesSummary
    representatives: dict[str, Optional[RevisitReference]]
    targets_by_digest: dict[str, list[tuple[CdxRecord, Path]]]
    materialized_files: set[Path]
    failed_files: set[Path]
    events: dict[int, list[str]]
    failed_capture_urls: list[str]


@dataclass
class _JobResult:
    warc: ExportSummary
    files: FilesSummary
    final_warc: Optional[Path]
    failed_capture_urls: list[str]


class _ThreadClientPool:
    """Lazily own and reuse one Wayback client per executor thread."""

    def __init__(self, factory: Callable) -> None:
        self._factory = factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients: list[object] = []

    def get(self):
        active = getattr(self._local, "active", None)
        if active is not None:
            return active

        client = self._factory()
        enter = getattr(client, "__enter__", None)
        active = enter() if callable(enter) else client
        if active is None:
            active = client
        self._local.active = active
        with self._lock:
            self._clients.append(client)
        return active

    def close(self) -> None:
        """Close every client after all executor threads have stopped."""

        for client in self._clients:
            exit_fn = getattr(client, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
            else:
                close = getattr(client, "close", None)
                if callable(close):
                    close()


def _is_redirect(status: Optional[int]) -> bool:
    return status is not None and 300 <= status < 400


def _cdx_timestamp(timestamp) -> str:
    """Format an aware timestamp as a 14-digit UTC CDX value."""

    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _failure_code(error: BaseException) -> Optional[int]:
    """Find an HTTP response code in a structured exception chain."""

    pending: list[object] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        direct_code = getattr(current, "status_code", None)
        if isinstance(direct_code, int):
            return direct_code
        response = getattr(current, "response", None)
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
        cause = getattr(current, "cause", None)
        if cause is not None:
            pending.append(cause)
        if isinstance(current, BaseException):
            pending.extend(current.args)
            for name in ("__cause__", "__context__"):
                nested = getattr(current, name, None)
                if nested is not None:
                    pending.append(nested)
    return None


def _failure_lines(capture: CdxRecord, error: Exception) -> list[str]:
    code = _failure_code(error)
    outcome = (
        f"failed with code {code}"
        if code is not None
        else "failed during playback"
    )
    return [
        capture_result_line(capture, outcome),
        f"  WARNING: {format_playback_failure(error)}",
    ]


def _status_failure_lines(
    capture: CdxRecord,
    actual_status: int,
) -> list[str]:
    return [
        capture_result_line(capture, f"failed with code {actual_status}"),
        (
            f"  WARNING: CDX status {capture.statuscode} but playback "
            f"returned {actual_status}"
        ),
    ]


def _file_targets(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    plan: Optional[WebsitePlan],
) -> dict[str, dict[int, tuple[CdxRecord, list[Path]]]]:
    """Index preflighted loose-file targets by URL key and capture."""

    indexed: dict[str, dict[int, tuple[CdxRecord, list[Path]]]] = {}
    if plan is None:
        return indexed
    for target in plan.targets:
        capture = capture_groups[target.urlkey][target.capture_index]
        identity = id(capture)
        group_targets = indexed.setdefault(target.urlkey, {})
        entry = group_targets.get(identity)
        if entry is None:
            entry = (capture, [])
            group_targets[identity] = entry
        entry[1].append(target.path)
    return indexed


def _ordered_union(
    warc_captures: Sequence[CdxRecord],
    file_captures: Sequence[CdxRecord],
) -> list[CdxRecord]:
    """Merge two sorted selections, coalescing only shared objects."""

    ordered: list[CdxRecord] = []
    seen: set[int] = set()
    captures = merge(
        warc_captures,
        file_captures,
        key=lambda capture: capture.timestamp,
    )
    for capture in captures:
        identity = id(capture)
        if identity not in seen:
            seen.add(identity)
            ordered.append(capture)
    return ordered


def _group_summary_line(
    warc: ExportSummary,
    files: FilesSummary,
    *,
    warc_enabled: bool,
    files_mode: str,
) -> str:
    parts = []
    if warc_enabled:
        parts.append(
            "warc "
            f"{warc.responses} response"
            f"{'' if warc.responses == 1 else 's'}, "
            f"{warc.revisits} revisit"
            f"{'' if warc.revisits == 1 else 's'}, "
            f"{warc.playback_failures} failed"
        )
    if files_mode != "none":
        parts.append(
            f"files {files.written} written ({files_mode}), "
            f"{files.playback_failures} failed, "
            f"{files.content_type_mismatches} content-type mismatches"
        )
    return f"Summary: {'; '.join(parts)}"


def _count_failure(
    capture: CdxRecord,
    error: Exception,
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
    warc_summary: ExportSummary,
    files_summary: FilesSummary,
    failed_files: set[Path],
    failed_capture_urls: list[str],
) -> None:
    """Count one failed capture for every output that selected it."""

    if wants_warc:
        warc_summary.record_playback_failure(error)
    if capture_paths:
        files_summary.record_playback_failure(error)
        failed_files.update(capture_paths)
    if wants_warc or capture_paths:
        failed_capture_urls.append(capture.view_url)


def _write_response_record(
    capture: CdxRecord,
    retrieved,
    writer_factory: Optional[Callable[[], object]],
) -> RevisitReference:
    """Write one full response and return its revisit metadata."""

    writer = writer_factory() if writer_factory is not None else None
    if writer is None:
        raise RuntimeError("response has no WARC writer")
    response = retrieved.to_warc_record(target_url=capture.original)
    write_response(writer, response)
    return response_reference(response)


def _write_revisit_record(
    capture: CdxRecord,
    reference: Optional[RevisitReference],
    writer_factory: Optional[Callable[[], object]],
) -> None:
    """Write one capture as a revisit of its URL-group representative."""

    writer = writer_factory() if writer_factory is not None else None
    if writer is None or reference is None:
        raise RuntimeError("revisit has no WARC representative")
    write_revisit(
        writer,
        target_uri=capture.original,
        capture_date=capture.timestamp,
        source_uri=capture.raw_url,
        mimetype=capture.mimetype,
        status_code=capture.statuscode,
        reference=reference,
    )


def _write_group_files(
    capture: CdxRecord,
    body: bytes,
    capture_paths: Sequence[Path],
    digest: Optional[str],
    *,
    actual_mimetype: Optional[str],
    files_mode: str,
    targets_by_digest: Mapping[
        str,
        Sequence[tuple[CdxRecord, Path]],
    ],
    materialized_files: set[Path],
    failed_files: set[Path],
    files_summary: FilesSummary,
    events: dict[int, list[str]],
    collection_root: Optional[Path],
) -> None:
    """Materialize every loose-file target served by one downloaded body."""

    if files_mode == "unique":
        targets = [(capture, path) for path in capture_paths]
    elif digest is not None:
        targets = targets_by_digest.get(digest, ())
    else:
        targets = [(capture, path) for path in capture_paths]

    for target_capture, path in targets:
        if path in materialized_files or path in failed_files:
            continue
        timestamp = (
            target_capture.timestamp
            if files_mode in {"unique", "all"}
            else None
        )
        planned_parts = website_relative_parts(
            target_capture.original,
            mimetype=target_capture.mimetype,
            timestamp=timestamp,
        )
        actual_parts = website_relative_parts(
            target_capture.original,
            mimetype=actual_mimetype,
            timestamp=timestamp,
        )
        if planned_parts != actual_parts:
            failed_files.add(path)
            files_summary.content_type_mismatches += 1
            events.setdefault(id(target_capture), []).append(
                capture_result_line(
                    target_capture,
                    "skipped file: response Content-Type changes path",
                )
            )
            continue
        write_body(path, body)
        materialized_files.add(path)
        files_summary.written += 1
        relative = (
            path.relative_to(collection_root)
            if collection_root is not None
            else path
        )
        events.setdefault(id(target_capture), []).append(
            capture_result_line(target_capture, f"wrote file {relative}")
        )


def _targets_by_digest(
    file_paths: Mapping[int, tuple[CdxRecord, Sequence[Path]]],
) -> dict[str, list[tuple[CdxRecord, Path]]]:
    """Group non-redirect file destinations by valid CDX digest."""

    grouped: dict[str, list[tuple[CdxRecord, Path]]] = {}
    for capture, paths in file_paths.values():
        if _is_redirect(capture.statuscode):
            continue
        digest = normalize_cdx_digest(capture.digest)
        if digest is None:
            continue
        for path in paths:
            grouped.setdefault(digest, []).append((capture, path))
    return grouped


def _response_mimetype(
    headers: Sequence[tuple[str, str]],
) -> Optional[str]:
    for name, value in headers:
        if name.lower() == "content-type":
            return value
    return None


def _new_group_state(
    union: Sequence[CdxRecord],
    warc_captures: Sequence[CdxRecord],
    file_paths: Mapping[int, tuple[CdxRecord, Sequence[Path]]],
    writer_factory: Optional[Callable[[], object]],
    existing_warc: Optional[ExistingWarcCache],
    *,
    files_mode: str,
    collection_root: Optional[Path],
) -> _GroupState:
    """Initialize isolated mutable state for one URL group."""

    return _GroupState(
        warc_ids={id(capture) for capture in warc_captures},
        writer_factory=writer_factory,
        existing_warc=existing_warc,
        files_mode=files_mode,
        collection_root=collection_root,
        warc=ExportSummary(selected=len(warc_captures)),
        files=FilesSummary(
            selected=sum(len(paths) for _capture, paths in file_paths.values())
        ),
        representatives={},
        targets_by_digest=_targets_by_digest(file_paths),
        materialized_files=set(),
        failed_files=set(),
        events={id(capture): [] for capture in union},
        failed_capture_urls=[],
    )


def _omit_redirect(
    state: _GroupState,
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
) -> None:
    """Count one known or played redirect for selected outputs."""

    if wants_warc:
        state.warc.redirects_omitted += 1
    state.files.redirects_omitted += len(capture_paths)


def _use_known_representative(
    state: _GroupState,
    capture: CdxRecord,
    digest: Optional[str],
    *,
    wants_warc: bool,
) -> bool:
    """Write a revisit when possible and report whether playback is skipped."""

    if digest is None or digest not in state.representatives:
        return False
    reference = state.representatives[digest]
    if wants_warc and reference is None:
        return False
    if wants_warc:
        _write_revisit_record(
            capture,
            reference,
            state.writer_factory,
        )
        state.warc.revisits += 1
        state.events[id(capture)].append(
            capture_result_line(capture, "wrote revisit")
        )
    return True


def _retrieve_validated(
    state: _GroupState,
    capture: CdxRecord,
    client,
    *,
    retries: int,
    wants_warc: bool,
    capture_paths: Sequence[Path],
):
    """Load one capture locally or record an expected playback failure."""

    reused = False
    retrieved = None
    if wants_warc and state.existing_warc is not None:
        try:
            cached = state.existing_warc.get(capture)
        except ValueError as error:
            state.events[id(capture)].append(
                "  WARNING: existing WARC cache entry could not be reused; "
                f"fetching from Wayback: {error}"
            )
        else:
            if cached is not None:
                if (
                    not cached.body
                    and normalize_cdx_digest(capture.digest)
                    != _EMPTY_PAYLOAD_DIGEST
                ):
                    state.events[id(capture)].append(
                        "  WARNING: existing WARC cache entry has an "
                        "unexpected empty payload; fetching from Wayback"
                    )
                else:
                    retrieved = RetrievedMemento(
                        body=cached.body,
                        url=capture.original,
                        capture_date=timestamp_to_warc_date(
                            capture.timestamp
                        ),
                        source_uri=capture.raw_url,
                        status_code=cached.status_code,
                        headers=cached.headers,
                    )
                    reused = True

    if retrieved is None:
        try:
            retrieved = retrieve_memento(
                client,
                capture,
                retries=retries,
            )
        except _PLAYBACK_ERRORS as error:
            state.events[id(capture)].extend(_failure_lines(capture, error))
            _count_failure(
                capture,
                error,
                wants_warc=wants_warc,
                capture_paths=capture_paths,
                warc_summary=state.warc,
                files_summary=state.files,
                failed_files=state.failed_files,
                failed_capture_urls=state.failed_capture_urls,
            )
            return None

    if (
        capture.statuscode is not None
        and retrieved.status_code != capture.statuscode
    ):
        state.events[id(capture)].extend(
            _status_failure_lines(capture, retrieved.status_code)
        )
        _count_failure(
            capture,
            ValueError("playback status differs from CDX"),
            wants_warc=wants_warc,
            capture_paths=capture_paths,
            warc_summary=state.warc,
            files_summary=state.files,
            failed_files=state.failed_files,
            failed_capture_urls=state.failed_capture_urls,
        )
        return None

    if _is_redirect(retrieved.status_code):
        _omit_redirect(
            state,
            wants_warc=False,
            capture_paths=capture_paths,
        )
        if not wants_warc:
            return None

    if (
        not retrieved.body
        and normalize_cdx_digest(capture.digest)
        != _EMPTY_PAYLOAD_DIGEST
    ):
        error = ValueError("empty playback body")
        state.events[id(capture)].extend(_failure_lines(capture, error))
        _count_failure(
            capture,
            error,
            wants_warc=wants_warc,
            capture_paths=capture_paths,
            warc_summary=state.warc,
            files_summary=state.files,
            failed_files=state.failed_files,
            failed_capture_urls=state.failed_capture_urls,
        )
        return None
    return retrieved, reused


def _commit_representative(
    state: _GroupState,
    capture: CdxRecord,
    retrieved,
    digest: Optional[str],
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
    reused: bool,
) -> None:
    """Commit one successful representative to every selected output."""

    if retrieved.recovered_content_encoding:
        state.events[id(capture)].append(
            capture_result_line(
                capture,
                "recovered invalid content encoding via CDX digest",
            )
        )
    reference = None
    if wants_warc:
        reference = _write_response_record(
            capture,
            retrieved,
            state.writer_factory,
        )
        state.warc.responses += 1
        state.events[id(capture)].append(
            capture_result_line(
                capture,
                (
                    "reused response from existing WARC"
                    if reused
                    else "wrote response"
                ),
            )
        )
    if digest is not None and not _is_redirect(retrieved.status_code):
        state.representatives[digest] = reference
    if _is_redirect(retrieved.status_code):
        capture_paths = ()
    _write_group_files(
        capture,
        retrieved.body,
        capture_paths,
        digest,
        actual_mimetype=_response_mimetype(retrieved.headers),
        files_mode=state.files_mode,
        targets_by_digest=state.targets_by_digest,
        materialized_files=state.materialized_files,
        failed_files=state.failed_files,
        files_summary=state.files,
        events=state.events,
        collection_root=state.collection_root,
    )


def _export_group(
    urlkey: str,
    warc_captures: Sequence[CdxRecord],
    file_capture_paths: Mapping[
        int,
        tuple[CdxRecord, Sequence[Path]],
    ],
    client,
    writer_factory: Optional[Callable[[], object]],
    existing_warc: Optional[ExistingWarcCache],
    *,
    retries: int,
    reporter: GroupReporter,
    warc_mode: str,
    files_mode: str,
    collection_root: Optional[Path],
) -> _GroupResult:
    """Process one URL group with a private representative dictionary."""

    chronological = _ordered_union(
        warc_captures,
        tuple(capture for capture, _paths in file_capture_paths.values()),
    )
    if not chronological:
        raise ValueError(f"capture group is empty: {urlkey}")
    processing = chronological
    if warc_mode == "latest" and warc_captures:
        selected = warc_captures[0]
        processing = [
            selected,
            *(
                capture
                for capture in chronological
                if capture is not selected
            ),
        ]

    state = _new_group_state(
        chronological,
        warc_captures,
        file_capture_paths,
        writer_factory,
        existing_warc,
        files_mode=files_mode,
        collection_root=collection_root,
    )

    for capture in processing:
        wants_warc = id(capture) in state.warc_ids
        target = file_capture_paths.get(id(capture))
        capture_paths = list(target[1] if target is not None else ())
        known_redirect = _is_redirect(capture.statuscode)
        if known_redirect and not wants_warc:
            _omit_redirect(
                state,
                wants_warc=False,
                capture_paths=capture_paths,
            )
            continue

        digest = normalize_cdx_digest(capture.digest)
        if not known_redirect and _use_known_representative(
            state,
            capture,
            digest,
            wants_warc=wants_warc,
        ):
            continue

        retrieved_result = _retrieve_validated(
            state,
            capture,
            client,
            retries=retries,
            wants_warc=wants_warc,
            capture_paths=capture_paths,
        )
        if retrieved_result is not None:
            retrieved, reused = retrieved_result
            _commit_representative(
                state,
                capture,
                retrieved,
                digest,
                wants_warc=wants_warc,
                capture_paths=capture_paths,
                reused=reused,
            )

    lines = []
    for capture in chronological:
        lines.extend(state.events.get(id(capture), ()))
    reporter.emit(
        chronological[0].original,
        lines,
        _group_summary_line(
            state.warc,
            state.files,
            warc_enabled=bool(warc_captures),
            files_mode=files_mode,
        ),
    )
    return _GroupResult(
        state.warc,
        state.files,
        state.failed_capture_urls,
    )


def _export_job(
    job: _ExportJob,
    warc_groups: Mapping[str, Sequence[CdxRecord]],
    file_targets: Mapping[
        str,
        Mapping[int, tuple[CdxRecord, Sequence[Path]]],
    ],
    client,
    *,
    retries: int,
    reporter: GroupReporter,
    warc_mode: str,
    files_mode: str,
    collection_root: Optional[Path],
) -> _JobResult:
    owner = (
        _WarcRebuilder.open(job.warc_path)
        if job.warc_path is not None
        else None
    )
    existing_warc = None
    if owner is not None and owner.final_path.is_file():
        try:
            existing_warc = ExistingWarcCache.inventory(owner.final_path)
        except Exception as error:
            print_progress(
                f"{owner.final_path} : WARNING: existing WARC cache "
                f"could not be inventoried; fetching from Wayback: {error}"
            )
    warc_summary = ExportSummary()
    files_summary = FilesSummary()
    failed_capture_urls = []
    try:
        for urlkey in job.urlkeys:
            result = _export_group(
                urlkey,
                warc_groups.get(urlkey, ()),
                file_targets.get(urlkey, {}),
                client,
                owner.get_writer if owner is not None else None,
                existing_warc,
                retries=retries,
                reporter=reporter,
                warc_mode=warc_mode,
                files_mode=files_mode,
                collection_root=collection_root,
            )
            warc_summary.add(result.warc)
            files_summary.add(result.files)
            failed_capture_urls.extend(result.failed_capture_urls)
        final_warc = (
            owner.publish(
                has_records=bool(
                    warc_summary.responses + warc_summary.revisits
                )
            )
            if owner is not None
            else None
        )
    except Exception:
        if owner is not None:
            owner.abort()
        raise
    return _JobResult(
        warc_summary,
        files_summary,
        final_warc,
        failed_capture_urls,
    )


def export_all(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    client,
    *,
    layout: CollectionLayout,
    file_capture_groups: Optional[
        Mapping[str, Sequence[CdxRecord]]
    ] = None,
    website_plan: Optional[WebsitePlan] = None,
    warc_mode: str = "all",
    files_mode: str = "none",
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
) -> ExportResult:
    """Export enabled WARC and file outputs through one bounded worker pool."""

    if retries < 0:
        raise ValueError("retries cannot be negative")
    selected_file_groups = file_capture_groups or {}
    targets = _file_targets(selected_file_groups, website_plan)
    warc_paths = allocate_warc_paths(capture_groups, layout)
    assigned_urlkeys = {
        urlkey for urlkeys in warc_paths.values() for urlkey in urlkeys
    }
    jobs = [
        _ExportJob(path, urlkeys)
        for path, urlkeys in warc_paths.items()
    ]
    jobs.extend(
        _ExportJob(None, (urlkey,))
        for urlkey in sorted(set(targets) - assigned_urlkeys)
    )
    total_groups = len(
        set(capture_groups).union(targets)
    )
    reporter = GroupReporter(total_groups)

    def run(job: _ExportJob, active_client):
        return _export_job(
            job,
            capture_groups,
            targets,
            active_client,
            retries=retries,
            reporter=reporter,
            warc_mode=warc_mode,
            files_mode=files_mode,
            collection_root=(
                website_plan.layout.collection_root
                if website_plan is not None
                else None
            ),
        )

    results = []
    if client_factory is not None and concurrency > 1 and len(jobs) > 1:
        clients = _ThreadClientPool(client_factory)

        def run_with_thread_client(job: _ExportJob):
            return run(job, clients.get())

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [
                    executor.submit(run_with_thread_client, job)
                    for job in jobs
                ]
                for future in as_completed(futures):
                    results.append(future.result())
        finally:
            clients.close()
    else:
        results = [run(job, client) for job in jobs]

    summary = ExportSummary()
    files_summary = FilesSummary()
    available_warcs = set()
    failed_capture_urls = []
    seen_failed_urls = set()
    for result in results:
        summary.add(result.warc)
        files_summary.add(result.files)
        if result.final_warc is not None:
            available_warcs.add(result.final_warc)
        for url in result.failed_capture_urls:
            if url not in seen_failed_urls:
                seen_failed_urls.add(url)
                failed_capture_urls.append(url)
    return ExportResult(
        summary,
        tuple(
            path
            for path in warc_paths
            if path in available_warcs
        ),
        files_summary,
        tuple(failed_capture_urls),
    )


def export_group(
    urlkey: str,
    captures: Sequence[CdxRecord],
    path: Path,
    client,
    *,
    client_factory: Optional[Callable] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
) -> ExportSummary:
    """Rebuild one URL group into one atomically replaced WARC."""

    result = _export_job(
        _ExportJob(path, (urlkey,)),
        {urlkey: captures},
        {},
        client,
        reporter=GroupReporter(1),
        warc_mode="all",
        files_mode="none",
        collection_root=None,
        retries=retries,
    )
    return result.warc


def print_summary(
    summary: ExportSummary,
    *,
    warc_mode: str = "all",
) -> None:
    """Print the WARC aggregate summary after output is complete."""

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
        f"{summary.redirects_omitted} redirects omitted; "
        f"{failures}"
    )
