"""Loose website-file export under ``website/``."""

from __future__ import annotations

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

from .paths import WebsitePlan
from .retrieval import (
    DEFAULT_CONCURRENCY,
    MalformedContentEncodingError,
    MementoFetchPool,
    RateLimitGate,
    TruncatedWaybackResponseError,
    format_playback_failure,
    format_playback_failure_summary,
    print_fetched,
    print_progress,
    retrieve_memento,
)


@dataclass
class FilesSummary:
    """Aggregate outcomes for one loose-file export operation."""

    selected: int = 0
    written: int = 0
    redirects_omitted: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0

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


def _cdx_timestamp(timestamp) -> str:
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _is_redirect(status: Optional[int]) -> bool:
    return status is not None and 300 <= status < 400


def _warn_skip(capture: CdxRecord, error: Exception) -> None:
    reason = format_playback_failure(error)
    print(
        f"WARNING skipped {_cdx_timestamp(capture.timestamp)} "
        f"{capture.original}: {reason}",
        file=sys.stderr,
    )


def _warn_status_substitution(capture: CdxRecord, actual_status: int) -> None:
    print(
        f"WARNING skipped {_cdx_timestamp(capture.timestamp)} "
        f"{capture.original}: CDX status {capture.statuscode} but "
        f"playback returned {actual_status}",
        file=sys.stderr,
    )


def _find_file_blocker(directory: Path) -> Optional[Path]:
    """Return the nearest existing non-directory ancestor, if any."""

    candidate = directory
    while True:
        if candidate.exists() and not candidate.is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        if candidate.exists() and candidate.is_dir():
            return None
        candidate = parent


def _ensure_parent_directory(path: Path) -> None:
    """Create parents, reshaping an existing file into ``index.html`` if needed."""

    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError as error:
            blocker = _find_file_blocker(path.parent)
            if blocker is None:
                raise OSError(
                    f"cannot create website directory for: {path}"
                ) from error
            reshaped = blocker / "index.html"
            if reshaped.exists():
                raise FileExistsError(
                    "cannot reshape website file to directory without "
                    f"clobbering: {reshaped}"
                ) from error
            temporary = blocker.with_name(blocker.name + ".tmp-reshape")
            if temporary.exists():
                raise FileExistsError(
                    f"website reshape temporary exists: {temporary}"
                ) from error
            blocker.rename(temporary)
            blocker.mkdir(parents=True, exist_ok=False)
            temporary.rename(reshaped)


def _write_body(path: Path, body: bytes) -> None:
    """Exclusively create one loose file with a non-empty body."""

    if not body:
        raise ValueError("refusing to write an empty website file")
    _ensure_parent_directory(path)
    with path.open("xb") as handle:
        handle.write(body)


def write_website_files(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    plan: WebsitePlan,
    client,
    *,
    gate: Optional[RateLimitGate] = None,
    client_factory=None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> FilesSummary:
    """Write selected capture bodies to preflighted website paths."""

    summary = FilesSummary()
    targets = list(plan.targets)
    active_gate = gate or RateLimitGate(max_concurrency=concurrency)
    pool = None
    fetch_window = None
    if (
        client_factory is not None
        and concurrency > 1
        and targets
    ):
        to_fetch = []
        for target in targets:
            captures = capture_groups[target.urlkey]
            capture = captures[target.capture_index]
            if not _is_redirect(capture.statuscode):
                to_fetch.append(capture)
        pool = MementoFetchPool(
            gate=active_gate,
            client_factory=client_factory,
            max_workers=concurrency,
            on_fetched=print_fetched,
        )
        fetch_window = pool.window(to_fetch)

    try:
        for target in targets:
            captures = capture_groups[target.urlkey]
            capture = captures[target.capture_index]
            summary.selected += 1

            if _is_redirect(capture.statuscode):
                summary.redirects_omitted += 1
                continue

            try:
                retrieved = (
                    fetch_window.wait(capture)
                    if fetch_window is not None
                    else None
                )
                if retrieved is None:
                    retrieved = retrieve_memento(
                        client,
                        capture,
                        gate=active_gate,
                    )
                    print_fetched(capture)
            except (
                MementoPlaybackError,
                BlockedByRobotsError,
                BlockedSiteError,
                WaybackRetryError,
            ) as error:
                _warn_skip(capture, error)
                summary.record_playback_failure(error)
                continue

            if (
                capture.statuscode is not None
                and retrieved.status_code != capture.statuscode
            ):
                _warn_status_substitution(capture, retrieved.status_code)
                summary.record_playback_failure()
                continue

            if _is_redirect(retrieved.status_code):
                summary.redirects_omitted += 1
                continue

            if not retrieved.body:
                _warn_skip(capture, ValueError("empty playback body"))
                summary.record_playback_failure()
                continue

            _write_body(target.path, retrieved.body)
            summary.written += 1
            print_progress(
                f"Wrote {_cdx_timestamp(capture.timestamp)} "
                f"{target.path.relative_to(plan.layout.collection_root)}"
            )
    finally:
        if pool is not None:
            pool.close()

    return summary


def print_files_summary(summary: FilesSummary, *, files_mode: str) -> None:
    """Print the loose-file aggregate summary."""

    if files_mode == "none":
        print("Files: disabled (none)")
        return

    failures = format_playback_failure_summary(
        summary.playback_failures,
        invalid_content_encoding=(
            summary.invalid_content_encoding_failures
        ),
        truncated_response=summary.truncated_response_failures,
    )
    print(
        f"Files: {summary.written} written ({files_mode}); "
        f"{failures}; "
        f"{summary.redirects_omitted} redirects omitted"
    )
