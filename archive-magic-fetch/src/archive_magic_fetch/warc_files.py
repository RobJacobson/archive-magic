"""Build WARC files from self-contained URL histories."""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timezone
from heapq import merge
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Sequence

from wayback import CdxRecord
from .website_files import WebsiteFileCounts, write_body
from .collection_paths import (
    CollectionPaths,
    WebsiteFile,
    WebsiteFiles,
    allocate_warc_paths,
    domain_folder,
    website_relative_parts,
)
from .downloads import (
    DEFAULT_WORKER_COUNT,
    MalformedContentEncodingError,
    PLAYBACK_ERRORS,
    DownloadedCapture,
    ThreadClientPool,
    TruncatedWaybackResponseError,
    format_playback_failure,
    normalize_cdx_digest,
    download_capture,
)
from .capture_identity import identity_for_capture, normalized_urlkey
from .retry import DEFAULT_RETRIES
from .replay_index import list_collection_warcs
from .warc_records import (
    ExistingWarcCache,
    ExistingWarcCollection,
    RevisitReference,
    open_new_warc,
    response_reference,
    timestamp_to_warc_date,
    validate_warc,
    write_response,
    write_revisit,
)


_EMPTY_PAYLOAD_DIGEST = "sha1:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"


@dataclass
class _WarcFileBuilder:
    """Own one exclusively reserved temporary WARC and final publication."""

    final_path: Path
    temporary_path: Path
    stream: Optional[BinaryIO] = None
    writer: object = None
    owns_temporary: bool = False

    @classmethod
    def open(cls, final_path: Path) -> _WarcFileBuilder:
        temporary_path = final_path.with_name(final_path.name + ".tmp")
        return cls(final_path, temporary_path)

    def get_writer(self):
        if self.writer is None:
            if self.temporary_path.exists():
                self.temporary_path.unlink()
            self.stream, self.writer = open_new_warc(
                self.temporary_path,
                self.final_path.name,
            )
            self.owns_temporary = True
        return self.writer

    def publish(
        self,
        *,
        has_records: bool,
        prior_identities=frozenset(),
    ) -> Optional[Path]:
        if self.stream is not None:
            self.stream.close()
        if not has_records:
            if self.owns_temporary and self.temporary_path.exists():
                self.temporary_path.unlink()
            return self.final_path if self.final_path.is_file() else None
        validate_warc(self.temporary_path)
        rebuilt = ExistingWarcCache.inventory(self.temporary_path)
        missing = prior_identities - rebuilt.identities
        if missing:
            raise ValueError(
                f"rebuilt WARC would lose {len(missing)} prior "
                "logical capture(s)"
            )
        os.replace(self.temporary_path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        if self.stream is not None and not self.stream.closed:
            self.stream.close()
        if self.owns_temporary and self.temporary_path.exists():
            self.temporary_path.unlink()


@dataclass
class WarcCounts:
    """Aggregate WARC construction outcomes."""

    selected: int = 0
    responses: int = 0
    revisits: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0

    def add(self, other: WarcCounts) -> None:
        self.selected += other.selected
        self.responses += other.responses
        self.revisits += other.revisits
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
class BuiltFiles:
    """Built WARCs, loose files, counts, and failed capture URLs."""

    warc_counts: WarcCounts
    built_warcs: tuple[Path, ...]
    file_counts: WebsiteFileCounts = field(default_factory=WebsiteFileCounts)
    failed_capture_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class UrlHistory:
    """Selected captures of one concrete URL across time."""

    domain: str
    urlkey: str
    warc_captures: tuple[CdxRecord, ...]
    website_files: tuple[WebsiteFile, ...]


@dataclass(frozen=True)
class WarcBatch:
    """Everything one worker needs to build exactly one WARC file."""

    path: Path
    histories: tuple[UrlHistory, ...]


@dataclass(frozen=True)
class WebsiteBatch:
    """One URL history selected only for loose website files."""

    history: UrlHistory


@dataclass
class _UrlHistoryResult:
    warc: WarcCounts
    files: WebsiteFileCounts
    failed_capture_urls: list[str]
    messages: list[str]


@dataclass
class _UrlHistoryState:
    """All mutable state whose lifetime is exactly one URL history."""

    warc_ids: set[int]
    writer_factory: Optional[Callable[[], object]]
    existing_warcs: Optional[ExistingWarcCollection]
    files_mode: str
    warc: WarcCounts
    files: WebsiteFileCounts
    response_refs_by_digest: dict[str, Optional[RevisitReference]]
    file_paths_by_digest: dict[str, list[tuple[CdxRecord, Path]]]
    written_files: set[Path]
    failed_files: set[Path]
    messages: list[str]
    failed_capture_urls: list[str]


@dataclass
class _BuiltBatch:
    warc: WarcCounts
    files: WebsiteFileCounts
    final_warc: Optional[Path]
    failed_capture_urls: list[str]
    messages: list[str]


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


def _url_messages(url: str, detail: str) -> list[str]:
    """Return a URL line plus one more-indented detail line."""

    return [url, f"  {detail}"]


def _failure_lines(capture: CdxRecord, error: Exception) -> list[str]:
    code = _failure_code(error)
    detail = format_playback_failure(error)
    if code is not None:
        detail = f"HTTP {code}; {detail}"
    return _url_messages(capture.view_url, detail)


def _status_failure_lines(
    capture: CdxRecord,
    actual_status: int,
) -> list[str]:
    return _url_messages(
        capture.view_url,
        f"CDX status {capture.statuscode} but playback returned {actual_status}",
    )


def _file_targets(
    website_files: Sequence[WebsiteFile],
) -> dict[int, tuple[CdxRecord, list[Path]]]:
    """Index one URL history's loose-file destinations by capture."""

    indexed: dict[int, tuple[CdxRecord, list[Path]]] = {}
    for target in website_files:
        capture = target.capture
        identity = id(capture)
        entry = indexed.get(identity)
        if entry is None:
            entry = (capture, [])
            indexed[identity] = entry
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


def _count_failure(
    capture: CdxRecord,
    error: Exception,
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
    warc_summary: WarcCounts,
    files_summary: WebsiteFileCounts,
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
    downloaded,
    writer_factory: Optional[Callable[[], object]],
) -> RevisitReference:
    """Write one full response and return its revisit metadata."""

    writer = writer_factory() if writer_factory is not None else None
    if writer is None:
        raise RuntimeError("response has no WARC writer")
    response = downloaded.to_warc_record(target_url=capture.original)
    write_response(writer, response)
    return response_reference(response)


def _write_revisit_record(
    capture: CdxRecord,
    reference: Optional[RevisitReference],
    writer_factory: Optional[Callable[[], object]],
) -> None:
    """Write one capture as a revisit of its URL-history representative."""

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


def _write_history_files(
    capture: CdxRecord,
    body: bytes,
    capture_paths: Sequence[Path],
    digest: Optional[str],
    *,
    actual_mimetype: Optional[str],
    files_mode: str,
    file_paths_by_digest: Mapping[
        str,
        Sequence[tuple[CdxRecord, Path]],
    ],
    written_files: set[Path],
    failed_files: set[Path],
    files_summary: WebsiteFileCounts,
    messages: list[str],
) -> None:
    """Materialize every loose-file target served by one downloaded body."""

    if files_mode == "unique":
        targets = [(capture, path) for path in capture_paths]
    elif digest is not None:
        targets = file_paths_by_digest.get(digest, ())
    else:
        targets = [(capture, path) for path in capture_paths]

    for target_capture, path in targets:
        if path in written_files or path in failed_files:
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
            messages.extend(
                _url_messages(
                    target_capture.view_url,
                    "skipped file: response Content-Type changes path",
                )
            )
            continue
        write_body(path, body)
        written_files.add(path)
        files_summary.written += 1


def _file_paths_by_digest(
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


def _new_history_state(
    warc_captures: Sequence[CdxRecord],
    file_paths: Mapping[int, tuple[CdxRecord, Sequence[Path]]],
    writer_factory: Optional[Callable[[], object]],
    existing_warcs: Optional[ExistingWarcCollection],
    *,
    files_mode: str,
) -> _UrlHistoryState:
    """Initialize isolated mutable state for one URL history."""

    return _UrlHistoryState(
        warc_ids={id(capture) for capture in warc_captures},
        writer_factory=writer_factory,
        existing_warcs=existing_warcs,
        files_mode=files_mode,
        warc=WarcCounts(selected=len(warc_captures)),
        files=WebsiteFileCounts(
            selected=sum(len(paths) for _capture, paths in file_paths.values())
        ),
        response_refs_by_digest=(
            existing_warcs.references_for_urlkey(
                normalized_urlkey(warc_captures[0].original)
            )
            if existing_warcs is not None and warc_captures
            else {}
        ),
        file_paths_by_digest=_file_paths_by_digest(file_paths),
        written_files=set(),
        failed_files=set(),
        messages=[],
        failed_capture_urls=[],
    )


def _omit_redirect(
    state: _UrlHistoryState,
    *,
    capture_paths: Sequence[Path],
) -> None:
    """Count one known or played redirect omitted from loose files."""

    state.files.redirects_omitted += len(capture_paths)


def _use_known_representative(
    state: _UrlHistoryState,
    capture: CdxRecord,
    digest: Optional[str],
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
) -> bool:
    """Write a revisit when possible and report whether playback is skipped."""

    if digest is None or digest not in state.response_refs_by_digest:
        return False
    reference = state.response_refs_by_digest[digest]
    if wants_warc and reference is None:
        return False
    if state.files_mode != "unique" and capture_paths and not all(
        path in state.written_files or path in state.failed_files
        for path in capture_paths
    ):
        return False
    if wants_warc:
        _write_revisit_record(
            capture,
            reference,
            state.writer_factory,
        )
        state.warc.revisits += 1
    return True


def _download_validated(
    state: _UrlHistoryState,
    capture: CdxRecord,
    client,
    *,
    retries: int,
    wants_warc: bool,
    capture_paths: Sequence[Path],
):
    """Load one capture locally or record an expected playback failure."""

    downloaded = None
    if (wants_warc or capture_paths) and state.existing_warcs is not None:
        cached = state.existing_warcs.get(capture)
        if cached is not None:
            if (
                not cached.body
                and normalize_cdx_digest(capture.digest)
                != _EMPTY_PAYLOAD_DIGEST
            ):
                raise ValueError(
                    f"existing WARC cache is unexpectedly empty for "
                    f"{capture.view_url}"
                )
            downloaded = DownloadedCapture(
                body=cached.body,
                url=capture.original,
                capture_date=timestamp_to_warc_date(capture.timestamp),
                source_uri=capture.raw_url,
                status_code=cached.status_code,
                headers=cached.headers,
            )

    if downloaded is None:
        try:
            downloaded = download_capture(
                client,
                capture,
                retries=retries,
            )
        except PLAYBACK_ERRORS as error:
            state.messages.extend(_failure_lines(capture, error))
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
        and downloaded.status_code != capture.statuscode
    ):
        state.messages.extend(
            _status_failure_lines(capture, downloaded.status_code)
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

    if _is_redirect(downloaded.status_code):
        _omit_redirect(
            state,
            capture_paths=capture_paths,
        )
        if not wants_warc:
            return None

    if (
        not downloaded.body
        and normalize_cdx_digest(capture.digest)
        != _EMPTY_PAYLOAD_DIGEST
    ):
        error = ValueError("empty playback body")
        state.messages.extend(_failure_lines(capture, error))
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
    return downloaded


def _commit_representative(
    state: _UrlHistoryState,
    capture: CdxRecord,
    downloaded,
    digest: Optional[str],
    *,
    wants_warc: bool,
    capture_paths: Sequence[Path],
) -> None:
    """Commit one successful representative to every selected output."""

    reference = None
    if wants_warc:
        known_reference = (
            state.response_refs_by_digest.get(digest)
            if digest is not None
            else None
        )
        if known_reference is not None and not _is_redirect(
            downloaded.status_code
        ):
            _write_revisit_record(
                capture,
                known_reference,
                state.writer_factory,
            )
            state.warc.revisits += 1
            reference = known_reference
        else:
            reference = _write_response_record(
                capture,
                downloaded,
                state.writer_factory,
            )
            state.warc.responses += 1
    if digest is not None and not _is_redirect(downloaded.status_code):
        if reference is not None:
            state.response_refs_by_digest.setdefault(digest, reference)
        else:
            state.response_refs_by_digest.setdefault(digest, None)
    if _is_redirect(downloaded.status_code):
        capture_paths = ()
    _write_history_files(
        capture,
        downloaded.body,
        capture_paths,
        digest,
        actual_mimetype=_response_mimetype(downloaded.headers),
        files_mode=state.files_mode,
        file_paths_by_digest=state.file_paths_by_digest,
        written_files=state.written_files,
        failed_files=state.failed_files,
        files_summary=state.files,
        messages=state.messages,
    )
def _write_url_history(
    urlkey: str,
    warc_captures: Sequence[CdxRecord],
    file_capture_paths: Mapping[
        int,
        tuple[CdxRecord, Sequence[Path]],
    ],
    client,
    writer_factory: Optional[Callable[[], object]],
    existing_warcs: Optional[ExistingWarcCollection],
    *,
    retries: int,
    files_mode: str,
) -> _UrlHistoryResult:
    """Write one URL history with private response references."""

    chronological = _ordered_union(
        warc_captures,
        tuple(capture for capture, _paths in file_capture_paths.values()),
    )
    if not chronological:
        raise ValueError(f"URL history is empty: {urlkey}")
    processing = chronological

    unique_warc_ids: set[int] = set()
    seen_warc_identities = set()
    for capture in warc_captures:
        identity = identity_for_capture(capture)
        if identity not in seen_warc_identities:
            seen_warc_identities.add(identity)
            unique_warc_ids.add(id(capture))

    state = _new_history_state(
        warc_captures,
        file_capture_paths,
        writer_factory,
        existing_warcs,
        files_mode=files_mode,
    )
    state.warc_ids = unique_warc_ids
    state.warc.selected = len(unique_warc_ids)
    processed_identities = set()

    for capture in processing:
        logical_identity = identity_for_capture(capture)
        wants_warc = id(capture) in state.warc_ids
        target = file_capture_paths.get(id(capture))
        capture_paths = list(target[1] if target is not None else ())
        known_redirect = _is_redirect(capture.statuscode)
        if logical_identity in processed_identities and (
            not capture_paths
            or all(
                path in state.written_files or path in state.failed_files
                for path in capture_paths
            )
        ):
            continue
        processed_identities.add(logical_identity)
        if known_redirect and not wants_warc:
            _omit_redirect(
                state,
                capture_paths=capture_paths,
            )
            continue

        already_stored = (
            wants_warc
            and existing_warcs is not None
            and existing_warcs.contains(capture)
        )
        needs_warc = wants_warc and not already_stored
        if already_stored and not capture_paths:
            continue

        digest = normalize_cdx_digest(capture.digest)
        if not known_redirect and _use_known_representative(
            state,
            capture,
            digest,
            wants_warc=needs_warc,
            capture_paths=capture_paths,
        ):
            continue

        downloaded_result = _download_validated(
            state,
            capture,
            client,
            retries=retries,
            wants_warc=needs_warc,
            capture_paths=capture_paths,
        )
        if downloaded_result is not None:
            downloaded = downloaded_result
            _commit_representative(
                state,
                capture,
                downloaded,
                digest,
                wants_warc=needs_warc,
                capture_paths=capture_paths,
            )

    return _UrlHistoryResult(
        state.warc,
        state.files,
        state.failed_capture_urls,
        state.messages,
    )


def _build_warc(
    batch: WarcBatch | WebsiteBatch,
    client,
    *,
    retries: int,
    files_mode: str,
    existing_warcs: Optional[ExistingWarcCollection] = None,
) -> _BuiltBatch:
    histories = (
        batch.histories
        if isinstance(batch, WarcBatch)
        else (batch.history,)
    )
    owner = (
        _WarcFileBuilder.open(batch.path)
        if isinstance(batch, WarcBatch)
        else None
    )
    messages = []
    local_existing = (
        existing_warcs.cache_for(owner.final_path)
        if owner is not None and existing_warcs is not None
        else None
    )
    warc_summary = WarcCounts()
    files_summary = WebsiteFileCounts()
    failed_capture_urls = []
    selected_identities = {
        identity_for_capture(capture)
        for history in histories
        for capture in history.warc_captures
    }
    if (
        owner is not None
        and existing_warcs is not None
        and not any(history.website_files for history in histories)
        and all(
            existing_warcs.contains(capture)
            for history in histories
            for capture in history.warc_captures
        )
    ):
        return _BuiltBatch(
            WarcCounts(
                selected=len(selected_identities),
                responses=(
                    local_existing.response_count
                    if local_existing is not None
                    else 0
                ),
                revisits=(
                    local_existing.revisit_count
                    if local_existing is not None
                    else 0
                ),
            ),
            files_summary,
            owner.final_path if local_existing is not None else None,
            failed_capture_urls,
            messages,
        )
    try:
        if owner is not None and local_existing is not None:
            local_existing.copy_records(owner.get_writer())
            warc_summary.responses += local_existing.response_count
            warc_summary.revisits += local_existing.revisit_count
        for history in histories:
            result = _write_url_history(
                history.urlkey,
                history.warc_captures,
                _file_targets(history.website_files),
                client,
                owner.get_writer if owner is not None else None,
                existing_warcs,
                retries=retries,
                files_mode=files_mode,
            )
            warc_summary.add(result.warc)
            files_summary.add(result.files)
            failed_capture_urls.extend(result.failed_capture_urls)
            messages.extend(result.messages)
        final_warc = (
            owner.publish(
                has_records=bool(
                    warc_summary.responses + warc_summary.revisits
                ),
                prior_identities=(
                    local_existing.identities
                    if local_existing is not None
                    else frozenset()
                ),
            )
            if owner is not None
            else None
        )
    except Exception:
        if owner is not None:
            owner.abort()
        raise
    return _BuiltBatch(
        warc_summary,
        files_summary,
        final_warc,
        failed_capture_urls,
        messages,
    )


def _wayback_calendar_url(original_url: str) -> str:
    """Return a Wayback calendar URL for one original capture URL."""

    return f"http://web.archive.org/web/*/{original_url}"


def _batch_display_url(batch: WarcBatch) -> str:
    """Return the console URL for one WARC batch."""

    for history in batch.histories:
        sample = next(iter(history.warc_captures), None)
        if sample is not None and isinstance(sample.original, str):
            return _wayback_calendar_url(sample.original)
        if history.website_files:
            capture = history.website_files[0].capture
            if isinstance(capture.original, str):
                return _wayback_calendar_url(capture.original)
    relative = batch.path.name
    return relative


def build_warc_files(
    captures_by_url: Mapping[tuple[str, str], Sequence[CdxRecord]],
    client,
    *,
    layout: CollectionPaths,
    file_captures_by_url: Optional[
        Mapping[tuple[str, str], Sequence[CdxRecord]]
    ] = None,
    website_files: Optional[WebsiteFiles] = None,
    files_mode: str = "none",
    client_factory: Optional[Callable] = None,
    worker_count: int = DEFAULT_WORKER_COUNT,
    retries: int = DEFAULT_RETRIES,
) -> BuiltFiles:
    """Build WARC and loose-file outputs through a bounded worker pool."""

    if retries < 0:
        raise ValueError("retries cannot be negative")
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")
    captures_by_url = _attach_domains(captures_by_url)
    file_captures_by_url = _attach_domains(file_captures_by_url or {})
    website_files_by_url: dict[tuple[str, str], list[WebsiteFile]] = {}
    if website_files is not None:
        for website_file in website_files.targets:
            capture = website_file.capture
            website_files_by_url.setdefault(
                (domain_folder(capture.original), capture.urlkey),
                [],
            ).append(website_file)
    warc_paths = allocate_warc_paths(captures_by_url, layout)
    assigned_histories = {
        history_key
        for history_keys in warc_paths.values()
        for history_key in history_keys
    }
    url_histories: dict[tuple[str, str], UrlHistory] = {}
    for history_key in set(captures_by_url).union(file_captures_by_url):
        domain, urlkey = history_key
        warc_captures = tuple(captures_by_url.get(history_key, ()))
        file_captures = tuple(file_captures_by_url.get(history_key, ()))
        sample = next(iter(warc_captures or file_captures), None)
        if sample is None:
            continue
        url_histories[history_key] = UrlHistory(
            domain=domain,
            urlkey=urlkey,
            warc_captures=warc_captures,
            website_files=tuple(website_files_by_url.get(history_key, ())),
        )
    warc_batches = [
        WarcBatch(
            path,
            tuple(url_histories[history_key] for history_key in history_keys),
        )
        for path, history_keys in warc_paths.items()
    ]
    website_batches = [
        WebsiteBatch(url_histories[history_key])
        for history_key in sorted(
            set(website_files_by_url) - assigned_histories
        )
    ]
    built_warc_order = list(warc_paths)
    existing_warcs = (
        ExistingWarcCollection.inventory(list_collection_warcs(layout))
        if warc_batches
        else None
    )

    def run(batch: WarcBatch | WebsiteBatch, active_client):
        return _build_warc(
            batch,
            active_client,
            retries=retries,
            files_mode=files_mode,
            existing_warcs=existing_warcs,
        )

    completed_warcs = 0
    total_warcs = len(warc_batches)

    def report_warc(batch: WarcBatch | WebsiteBatch, result: _BuiltBatch) -> None:
        nonlocal completed_warcs
        if not isinstance(batch, WarcBatch):
            return
        completed_warcs += 1
        print(
            f"[{completed_warcs}/{total_warcs}] {_batch_display_url(batch)}"
        )
        stats = (
            f"{result.warc.responses} responses, "
            f"{result.warc.revisits} revisits, "
            f"{result.warc.playback_failures} failed"
        )
        if result.files.selected:
            stats += (
                f", files {result.files.written} written, "
                f"{result.files.playback_failures} failed"
            )
        print(f"  {stats}")
        for message in result.messages:
            print(f"  {message}")

    completed_files = 0

    def report_website_files(
        batch: WarcBatch | WebsiteBatch,
        result: _BuiltBatch,
    ) -> None:
        nonlocal completed_files
        if not isinstance(batch, WebsiteBatch):
            return
        completed_files += 1
        original = batch.history.website_files[0].capture.original
        print(
            f"[{completed_files}/{len(website_batches)}] "
            f"{_wayback_calendar_url(original)}"
        )
        print(
            f"  {result.files.written} written, "
            f"{result.files.playback_failures} failed"
        )
        for message in result.messages:
            print(f"  {message}")

    def run_batches(
        selected_batches: Sequence[WarcBatch | WebsiteBatch],
        report_finished: Callable[
            [WarcBatch | WebsiteBatch, _BuiltBatch], None
        ],
    ) -> list[_BuiltBatch]:
        results: list[_BuiltBatch] = []
        pending: list[WarcBatch | WebsiteBatch] = list(selected_batches)
        if not pending:
            return results

        def finish_sync(
            batch: WarcBatch | WebsiteBatch,
            result: _BuiltBatch,
        ) -> None:
            results.append(result)
            report_finished(batch, result)

        if client_factory is None:
            while pending:
                batch = pending.pop(0)
                finish_sync(batch, run(batch, client))
            return results

        clients = ThreadClientPool(client_factory)

        def run_with_thread_client(batch: WarcBatch | WebsiteBatch):
            return run(batch, clients.get())

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                warc_futures: dict = {}
                while pending or warc_futures:
                    while pending and len(warc_futures) < worker_count:
                        batch = pending.pop(0)
                        warc_futures[
                            pool.submit(run_with_thread_client, batch)
                        ] = batch
                    done, _ = wait(
                        set(warc_futures),
                        return_when=FIRST_COMPLETED,
                    )
                    for finished in done:
                        batch = warc_futures.pop(finished)
                        result = finished.result()
                        results.append(result)
                        report_finished(batch, result)
        finally:
            clients.close()
        return results

    results: list[_BuiltBatch] = []
    if warc_batches:
        print(
            f"WARC files: building {len(warc_batches)} with "
            f"{worker_count} workers"
        )
        results.extend(run_batches(warc_batches, report_warc))
    if website_batches:
        print(
            f"Website files: building {len(website_batches)} histories with "
            f"{worker_count} workers"
        )
        results.extend(run_batches(website_batches, report_website_files))

    summary = WarcCounts()
    files_summary = WebsiteFileCounts()
    available_warcs = set()
    failed_capture_urls = []
    for result in results:
        summary.add(result.warc)
        files_summary.add(result.files)
        if result.final_warc is not None:
            available_warcs.add(result.final_warc)
        failed_capture_urls.extend(result.failed_capture_urls)
    return BuiltFiles(
        summary,
        tuple(
            path
            for path in built_warc_order
            if path in available_warcs
        ),
        files_summary,
        tuple(failed_capture_urls),
    )



def _attach_domains(
    captures_by_url: Mapping[tuple[str, str], Sequence[CdxRecord]],
) -> dict[tuple[str, str], list[CdxRecord]]:
    """Attach each selected CDX URL key to its normalized domain."""

    grouped: dict[tuple[str, str], list[CdxRecord]] = {}
    for captures in captures_by_url.values():
        for capture in captures:
            grouped.setdefault(
                (domain_folder(capture.original), capture.urlkey),
                [],
            ).append(capture)
    for captures in grouped.values():
        captures.sort(key=lambda capture: capture.timestamp)
    return grouped


def build_url_history(
    urlkey: str,
    captures: Sequence[CdxRecord],
    path: Path,
    client,
    *,
    retries: int = DEFAULT_RETRIES,
) -> WarcCounts:
    """Build one URL history into one atomically replaced WARC."""

    existing_warcs = (
        ExistingWarcCollection.inventory((path,)) if path.is_file() else None
    )
    result = _build_warc(
        WarcBatch(
            path,
            (
                UrlHistory(
                    domain_folder(captures[0].original),
                    urlkey,
                    tuple(captures),
                    (),
                ),
            ),
        ),
        client,
        files_mode="none",
        retries=retries,
        existing_warcs=existing_warcs,
    )
    for message in result.messages:
        print(f"  {message}")
    return result.warc
