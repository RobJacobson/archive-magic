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
    MementoFetchPool,
    RetrievalCache,
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


def _cdx_timestamp(timestamp) -> str:
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _is_redirect(status: Optional[int]) -> bool:
    return status is not None and 300 <= status < 400


def _warn_skip(capture: CdxRecord, error: Exception) -> None:
    reason = str(error) or type(error).__name__
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
    cache: Optional[RetrievalCache] = None,
    client_factory=None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> FilesSummary:
    """Write selected capture bodies to preflighted website paths."""

    summary = FilesSummary()
    targets = list(plan.targets)
    pool = None
    fetch_window = None
    if (
        cache is not None
        and client_factory is not None
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
            cache=cache,
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
                if cache is not None:
                    cache.discard(capture, force=True)
                continue

            fetched_by_worker = False
            if fetch_window is not None:
                fetched_by_worker = fetch_window.wait(capture)
            was_cached = (
                cache is not None and cache.get(capture) is not None
            )

            try:
                if cache is None:
                    retrieved = retrieve_memento(client, capture)
                else:
                    retrieved = cache.retrieve(client, capture)
            except (
                MementoPlaybackError,
                BlockedByRobotsError,
                BlockedSiteError,
                WaybackRetryError,
            ) as error:
                _warn_skip(capture, error)
                summary.playback_failures += 1
                if cache is not None:
                    cache.discard(capture, force=True)
                continue

            if not fetched_by_worker and not was_cached:
                print_fetched(capture)

            if (
                capture.statuscode is not None
                and retrieved.status_code != capture.statuscode
            ):
                _warn_status_substitution(capture, retrieved.status_code)
                summary.playback_failures += 1
                if cache is not None:
                    cache.discard(capture, force=True)
                continue

            if _is_redirect(retrieved.status_code):
                summary.redirects_omitted += 1
                if cache is not None:
                    cache.discard(capture, force=True)
                continue

            if not retrieved.body:
                _warn_skip(capture, ValueError("empty playback body"))
                summary.playback_failures += 1
                if cache is not None:
                    cache.discard(capture, force=True)
                continue

            _write_body(target.path, retrieved.body)
            summary.written += 1
            print_progress(
                f"Wrote {_cdx_timestamp(capture.timestamp)} "
                f"{target.path.relative_to(plan.layout.collection_root)}"
            )
            if cache is not None:
                cache.discard(capture, force=True)
    finally:
        if pool is not None:
            pool.close()

    return summary


def print_files_summary(summary: FilesSummary, *, files_mode: str) -> None:
    """Print the loose-file aggregate summary."""

    if files_mode == "none":
        print("Files: disabled (none)")
        return

    print(
        f"Files: {summary.written} written ({files_mode}); "
        f"{summary.playback_failures} playback failures; "
        f"{summary.redirects_omitted} redirects omitted"
    )
