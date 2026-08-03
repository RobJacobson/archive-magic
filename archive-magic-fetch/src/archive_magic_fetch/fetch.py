"""Application workflow for one Archive Magic Fetch request."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from wayback import CdxRecord

from .console import ConsoleMirror
from .search import group_by_url, search_captures, select_captures
from .warc_files import BuiltFiles, build_warc_files
from .collection_paths import (
    DEFAULT_OUTPUT_ROOT,
    CollectionPaths,
    collection_paths,
    prepare_website_files,
)
from .source_files import save_search_results
from .replay_index import build_replay_index
from .redirects import discover_redirect_captures
from .downloads import make_client_factory
from .local_links import rewrite_local_links


USER_AGENT = (
    "archive-magic-fetch/0.1.0 "
    "(+https://github.com/RobJacobson/archive-magic)"
)

# archive-magic-fetch/ — sibling of archives/, independent of process cwd
_FETCH_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_ROOT = (_FETCH_PROJECT_ROOT / DEFAULT_OUTPUT_ROOT).resolve()


@dataclass(frozen=True)
class FetchSettings:
    """Validated inputs for one fetch request."""

    url_pattern: str
    date_start: str
    date_end: str
    warc_mode: str
    files_mode: str
    rewrite_local: bool
    redirect_capture: str
    worker_count: int
    retries: int


def _report_discovery_progress(count: int) -> None:
    """Print indeterminate discovery progress for long CDX searches."""

    print(f"  fetched {count}...")


def _build_files(
    settings: FetchSettings,
    captures: Sequence[CdxRecord],
    client,
    client_factory: Callable,
    paths: CollectionPaths,
) -> BuiltFiles:
    """Select captures, discover redirects, and build requested files."""

    captures_by_url = group_by_url(captures)
    warc_captures_by_url = select_captures(
        captures_by_url,
        settings.warc_mode,
    )
    file_captures_by_url = select_captures(
        captures_by_url,
        settings.files_mode,
    )
    warc_captures = list(
        dict.fromkeys(
            capture
            for history in warc_captures_by_url.values()
            for capture in history
        )
    )
    include_timestamps = settings.files_mode in {"unique", "all"}

    website_files = None
    if settings.files_mode != "none":
        website_files = prepare_website_files(
            file_captures_by_url,
            paths,
            include_timestamps=include_timestamps,
        )

    redirect_enabled = (
        settings.warc_mode != "none" and settings.redirect_capture != "none"
    )
    failed_capture_urls: list[str] = []
    if redirect_enabled:
        redirects = discover_redirect_captures(
            warc_captures,
            client,
            client_factory,
            mode=settings.redirect_capture,
            date_start=settings.date_start,
            date_end=settings.date_end,
            worker_count=settings.worker_count,
            retries=settings.retries,
        )
        print(
            f"Redirects: {redirects.additional_domains} additional domains, "
            f"{len(redirects.captures)} additional captures"
        )
        for message in redirects.messages:
            print(f"  {message}")
        failed_capture_urls.extend(redirects.failed_capture_urls)
        warc_captures.extend(redirects.captures)
        for search in redirects.searches:
            save_search_results(
                search.captures,
                layout=paths,
                url_pattern=search.scope.url,
                date_start=settings.date_start,
                date_end=settings.date_end,
                acquired_at=datetime.now(timezone.utc),
            )

    warc_captures_by_url = group_by_url(warc_captures)
    result = build_warc_files(
        warc_captures_by_url,
        client,
        layout=paths,
        file_captures_by_url=file_captures_by_url,
        website_files=website_files,
        warc_mode=settings.warc_mode,
        files_mode=settings.files_mode,
        client_factory=client_factory,
        worker_count=settings.worker_count,
        retries=settings.retries,
    )
    failed_capture_urls.extend(result.failed_capture_urls)

    return BuiltFiles(
        result.warc_counts,
        result.built_warcs,
        result.file_counts,
        tuple(dict.fromkeys(failed_capture_urls)),
    )


def _finalize_outputs(
    settings: FetchSettings,
    result: BuiltFiles,
    paths: CollectionPaths,
) -> bool:
    """Finalize derived outputs, report results, and return request success."""

    if settings.warc_mode != "none":
        replay_index = build_replay_index(result.built_warcs, layout=paths)
        if replay_index is not None:
            relative = replay_index.relative_to(paths.collection_root)
            print(
                f"Replay index: {relative.as_posix()} from "
                f"{len(result.built_warcs)} WARC files"
            )

    if settings.rewrite_local and result.file_counts.written > 0:
        rewrite_local_links(
            paths.website_root,
            include_timestamps=settings.files_mode in {"unique", "all"},
        )

    return not result.failed_capture_urls


def run_fetch(
    settings: FetchSettings,
    *,
    console_log: Optional[ConsoleMirror] = None,
) -> bool:
    """Search captures, build enabled files, and report success."""

    started_at = time.monotonic()
    print(
        f"Fetch {settings.url_pattern} "
        f"({settings.date_start}-{settings.date_end}): "
        f"WARC {settings.warc_mode}, files {settings.files_mode}, "
        f"redirects {settings.redirect_capture}, "
        f"{settings.worker_count} workers"
    )

    if settings.warc_mode == "none" and settings.redirect_capture != "none":
        print("Redirect capture inactive: --warc none")

    if settings.warc_mode == "none" and settings.files_mode == "none":
        print("Nothing to do: both --warc and --files are none")
        return True

    paths = collection_paths(
        settings.url_pattern,
        root=_DEFAULT_OUTPUT_ROOT,
    )
    client_factory = make_client_factory(USER_AGENT)
    with client_factory() as client:
        captures = search_captures(
            client,
            settings.url_pattern,
            settings.date_start,
            settings.date_end,
            progress=_report_discovery_progress,
            retries=settings.retries,
        )
        if not captures:
            print("Search: 0 captures in 0 URL histories")
            print(f"Done in {(time.monotonic() - started_at) / 60:.1f} minutes")
            return True

        captures_by_url = group_by_url(captures)
        print(
            f"Search: {len(captures)} captures in "
            f"{len(captures_by_url)} URL histories"
        )
        source_files = save_search_results(
            captures,
            layout=paths,
            url_pattern=settings.url_pattern,
            date_start=settings.date_start,
            date_end=settings.date_end,
            acquired_at=datetime.now(timezone.utc),
        )
        if console_log is not None:
            console_log.attach(source_files.path / "log.txt")

        result = _build_files(
            settings,
            captures,
            client,
            client_factory,
            paths,
        )
        succeeded = _finalize_outputs(settings, result, paths)
        failed = len(result.failed_capture_urls)
        print(
            f"Done in {(time.monotonic() - started_at) / 60:.1f} minutes: "
            f"{result.warc_counts.selected} selected, "
            f"{result.warc_counts.responses} responses, "
            f"{result.warc_counts.revisits} revisits, "
            f"{failed} failed"
        )
        return succeeded
