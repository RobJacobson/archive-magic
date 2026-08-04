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
from .warc_files import BuiltFiles, UrlHistory, WarcBatch, build_warc_files
from .collection_paths import (
    DEFAULT_OUTPUT_ROOT,
    CollectionPaths,
    allocate_warc_paths,
    collection_paths,
    prepare_website_files,
    same_site,
)
from .source_files import save_search_results
from .replay_index import build_replay_index
from .redirects import expand_redirect_target
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


def _redirect_expand(
    client,
    *,
    seed_pattern: str,
    mode: str,
    date_start: str,
    date_end: str,
    layout: CollectionPaths,
    known_history_keys: set[tuple[str, str]],
    reserved_paths: set[Path],
    seen_searches: set[tuple[object, ...]],
    retries: int,
) -> Callable[[Sequence[str]], list[WarcBatch]]:
    """Build a callback that turns Location targets into new WARC batches.

    Same-site targets (subdomain/apex siblings of the seed pattern) keep
    ``expand=True`` so intra-site permanent redirects can still expand.
    Off-site targets use ``expand=False`` (one hop from the seed site).
    """

    def expand(targets: Sequence[str]) -> list[WarcBatch]:
        added: list[WarcBatch] = []
        for target in dict.fromkeys(targets):
            expansion = expand_redirect_target(
                client,
                target,
                mode=mode,
                date_start=date_start,
                date_end=date_end,
                seen_searches=seen_searches,
                known_history_keys=known_history_keys,
                retries=retries,
                progress=_report_discovery_progress,
            )
            if expansion is None:
                continue
            save_search_results(
                expansion.search.captures,
                layout=layout,
                url_pattern=expansion.search.scope.url,
                date_start=date_start,
                date_end=date_end,
                acquired_at=datetime.now(timezone.utc),
            )
            if not expansion.histories:
                continue
            new_paths = allocate_warc_paths(expansion.histories, layout)
            queued_keys: list[tuple[str, str]] = []
            keep_expanding = same_site(seed_pattern, target)
            for path, history_keys in new_paths.items():
                if path in reserved_paths:
                    relative = path.relative_to(layout.collection_root).as_posix()
                    print(
                        "  WARNING: skipping redirect histories at "
                        f"{relative}: WARC path already reserved"
                    )
                    known_history_keys.update(history_keys)
                    continue
                histories = []
                for history_key in history_keys:
                    domain, urlkey = history_key
                    known_history_keys.add(history_key)
                    queued_keys.append(history_key)
                    histories.append(
                        UrlHistory(
                            domain=domain,
                            urlkey=urlkey,
                            warc_captures=tuple(expansion.histories[history_key]),
                            website_files=(),
                        )
                    )
                reserved_paths.add(path)
                added.append(
                    WarcBatch(path, tuple(histories), expand=keep_expanding)
                )
            if queued_keys:
                print(f"Redirect: +{len(queued_keys)} histories from {target}")
        return added

    return expand


def _build_files(
    settings: FetchSettings,
    captures: Sequence[CdxRecord],
    client,
    client_factory: Callable,
    paths: CollectionPaths,
) -> BuiltFiles:
    """Select captures and build requested files with inline redirect expansion."""

    captures_by_url = group_by_url(captures)
    warc_captures_by_url = select_captures(
        captures_by_url,
        settings.warc_mode,
    )
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

    expand_redirects = None
    collect_redirects = False
    if settings.warc_mode != "none" and settings.redirect_capture != "none":
        collect_redirects = True
        known_history_keys = set(warc_captures_by_url).union(file_captures_by_url)
        reserved_paths = set(
            allocate_warc_paths(warc_captures_by_url, paths)
        )
        expand_redirects = _redirect_expand(
            client,
            seed_pattern=settings.url_pattern,
            mode=settings.redirect_capture,
            date_start=settings.date_start,
            date_end=settings.date_end,
            layout=paths,
            known_history_keys=known_history_keys,
            reserved_paths=reserved_paths,
            seen_searches=set(),
            retries=settings.retries,
        )

    return build_warc_files(
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
        collect_redirects=collect_redirects,
        expand_redirects=expand_redirects,
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
