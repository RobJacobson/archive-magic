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
from .collection_coverage import (
    coverage_after_run,
    merge_search_window,
    resolve_prior_coverage,
    save_coverage,
)
from .source_files import save_search_results
from .replay_index import build_replay_index, list_collection_warcs
from .redirects import write_redirect_report
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
    build_warc: bool
    files_mode: str
    rewrite_local: bool
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
    """Select captures and build requested outputs."""

    captures_by_url = group_by_url(captures)
    warc_captures_by_url = captures_by_url if settings.build_warc else {}
    file_captures_by_url = select_captures(
        captures_by_url,
        settings.files_mode,
    )
    include_timestamps = settings.files_mode in {"unique", "all"}

    website_files = None
    if settings.files_mode != "none":
        website_files = prepare_website_files(
            file_captures_by_url,
            paths,
            include_timestamps=include_timestamps,
        )

    return build_warc_files(
        warc_captures_by_url,
        client,
        layout=paths,
        file_captures_by_url=file_captures_by_url,
        website_files=website_files,
        files_mode=settings.files_mode,
        client_factory=client_factory,
        worker_count=settings.worker_count,
        retries=settings.retries,
    )


def _finalize_outputs(
    settings: FetchSettings,
    result: BuiltFiles,
    paths: CollectionPaths,
    source_path: Path,
) -> bool:
    """Finalize derived outputs, report results, and return request success."""

    if settings.build_warc:
        warcs = list_collection_warcs(paths)
        if not warcs and result.built_warcs:
            warcs = list(result.built_warcs)
        replay_index = build_replay_index(warcs, layout=paths)
        if replay_index is not None:
            relative = replay_index.relative_to(paths.collection_root)
            print(
                f"Replay index: {relative.as_posix()} from "
                f"{len(warcs)} WARC files"
            )
        redirect_report = write_redirect_report(
            warcs,
            source_path / "redirects.json",
        )
        print(
            f"Redirects: {redirect_report.skipped} targets skipped, "
            f"{redirect_report.covered} already captured, "
            f"{redirect_report.unresolved} unresolved; "
            f"{redirect_report.path.relative_to(paths.collection_root)}"
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
        f"build WARC {str(settings.build_warc).lower()}, "
        f"files {settings.files_mode}, {settings.worker_count} workers"
    )

    if not settings.build_warc and settings.files_mode == "none":
        print("Nothing to do: --build-warc is false and --files is none")
        return True

    paths = collection_paths(
        settings.url_pattern,
        root=_DEFAULT_OUTPUT_ROOT,
    )
    prior = resolve_prior_coverage(paths)
    window = merge_search_window(
        url_pattern=settings.url_pattern,
        date_start=settings.date_start,
        date_end=settings.date_end,
        files_mode=settings.files_mode,
        prior=prior,
    )
    if window.expanded and window.prior is not None:
        print(
            f"Merge: expanding search {settings.date_start}-{settings.date_end} "
            f"using prior coverage {window.prior.date_start}-"
            f"{window.prior.date_end} -> "
            f"{window.date_start}-{window.date_end}"
        )

    client_factory = make_client_factory(USER_AGENT)
    with client_factory() as client:
        captures = search_captures(
            client,
            settings.url_pattern,
            window.date_start,
            window.date_end,
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
            date_start=window.date_start,
            date_end=window.date_end,
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
        succeeded = _finalize_outputs(
            settings,
            result,
            paths,
            source_files.path,
        )
        save_coverage(
            paths,
            coverage_after_run(
                url_pattern=settings.url_pattern,
                date_start=window.date_start,
                date_end=window.date_end,
                files_mode=settings.files_mode,
            ),
        )
        failed = len(result.failed_capture_urls)
        print(
            f"Done in {(time.monotonic() - started_at) / 60:.1f} minutes: "
            f"{result.warc_counts.selected} selected, "
            f"{result.warc_counts.responses} responses, "
            f"{result.warc_counts.revisits} revisits, "
            f"{failed} failed"
        )
        return succeeded
