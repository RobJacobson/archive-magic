"""Application workflow for one Archive Magic Fetch job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from wayback import CdxRecord

from .console import ConsoleMirror
from .discovery import apply_output_mode, discover, group_captures
from .export import ExportResult, export_all, print_summary
from .files import print_files_summary
from .paths import (
    DEFAULT_OUTPUT_ROOT,
    CollectionLayout,
    collection_layout,
    preflight_website_layout,
)
from .provenance import save_acquisition
from .replay import generate_replay_index
from .redirect_capture import redirect_scope
from .retrieval import make_client_factory
from .rewrite_local import rewrite_local_website


USER_AGENT = (
    "archive-magic-fetch/0.1.0 "
    "(+https://github.com/RobJacobson/archive-magic)"
)

# archive-magic-fetch/ — sibling of archives/, independent of process cwd
_FETCH_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_ROOT = (_FETCH_PROJECT_ROOT / DEFAULT_OUTPUT_ROOT).resolve()


@dataclass(frozen=True)
class FetchRequest:
    """Validated inputs for one fetch job."""

    url_pattern: str
    date_start: str
    date_end: str
    warc_mode: str
    files_mode: str
    rewrite_local: bool
    redirect_capture: str
    concurrency: int
    retries: int


def _report_discovery_progress(count: int) -> None:
    """Print indeterminate discovery progress for long CDX searches."""

    print(f"  fetched {count}...")


def _export_captures(
    request: FetchRequest,
    captures: Sequence[CdxRecord],
    client,
    client_factory: Callable,
    layout: CollectionLayout,
) -> ExportResult:
    """Select, plan, and export all enabled capture outputs."""

    print(f"Grouping {len(captures)} captures...")
    capture_groups = group_captures(captures)
    primary_warc_groups = apply_output_mode(
        capture_groups,
        request.warc_mode,
    )
    files_groups = apply_output_mode(capture_groups, request.files_mode)
    warc_captures = list(
        dict.fromkeys(
            capture
            for group in primary_warc_groups.values()
            for capture in group
        )
    )
    warc_groups = group_captures(warc_captures)
    include_timestamps = request.files_mode in {"unique", "all"}

    website_plan = None
    if request.files_mode != "none":
        print("Planning website files...")
        website_plan = preflight_website_layout(
            files_groups,
            layout,
            include_timestamps=include_timestamps,
        )

    redirect_enabled = (
        request.warc_mode != "none" and request.redirect_capture != "none"
    )
    if not redirect_enabled:
        print(
            "Exporting "
            f"{len(set(warc_groups).union(files_groups))} URL groups "
            f"(concurrency={request.concurrency})..."
        )
        return export_all(
            warc_groups,
            client,
            layout=layout,
            file_capture_groups=files_groups,
            website_plan=website_plan,
            warc_mode=request.warc_mode,
            files_mode=request.files_mode,
            client_factory=client_factory,
            concurrency=request.concurrency,
            retries=request.retries,
        )

    seen_scopes = set()
    known_captures = set(warc_captures)
    failed_capture_urls = []
    first_pass = True
    while True:
        print(
            "Exporting "
            f"{len(warc_groups)} WARC URL groups for redirect discovery "
            f"(concurrency={request.concurrency})..."
        )
        result = export_all(
            warc_groups,
            client,
            layout=layout,
            file_capture_groups=files_groups if first_pass else None,
            website_plan=website_plan if first_pass else None,
            warc_mode=request.warc_mode,
            files_mode=request.files_mode if first_pass else "none",
            client_factory=client_factory,
            concurrency=request.concurrency,
            retries=request.retries,
        )
        if first_pass:
            files_summary = result.files_summary
            first_pass = False
        failed_capture_urls.extend(result.failed_capture_urls)
        added = False
        for target in result.redirect_targets:
            scope = redirect_scope(target, request.redirect_capture)
            if scope.key in seen_scopes:
                continue
            seen_scopes.add(scope.key)
            print(
                f"Discovering redirect {request.redirect_capture} captures "
                f"for {scope.url} ({request.date_start}-{request.date_end})"
            )
            target_captures = discover(
                client,
                scope.url,
                request.date_start,
                request.date_end,
                match_type=scope.match_type,
                progress=_report_discovery_progress,
                retries=request.retries,
            )
            if not target_captures:
                print(f"No redirect captures found for {scope.url}")
                continue
            print(
                f"Discovered {len(target_captures)} redirect captures "
                f"for {scope.url}"
            )
            print(f"Saving redirect source acquisition for {scope.url}...")
            save_acquisition(
                target_captures,
                layout=layout,
                url_pattern=scope.url,
                date_start=request.date_start,
                date_end=request.date_end,
                acquired_at=datetime.now(timezone.utc),
            )
            for capture in target_captures:
                if capture not in known_captures:
                    known_captures.add(capture)
                    warc_captures.append(capture)
                    added = True
        if not added:
            break
        warc_groups = group_captures(warc_captures)

    return ExportResult(
        result.summary,
        result.final_warcs,
        files_summary,
        tuple(dict.fromkeys(failed_capture_urls)),
        result.redirect_targets,
    )


def _finalize_outputs(
    request: FetchRequest,
    result: ExportResult,
    layout: CollectionLayout,
) -> bool:
    """Finalize derived outputs, report results, and return job success."""

    if request.warc_mode != "none":
        print("Building replay index...")
        generate_replay_index(result.final_warcs, layout=layout)

    if request.rewrite_local and result.files_summary.written > 0:
        rewrite_local_website(
            layout.website_root,
            include_timestamps=request.files_mode in {"unique", "all"},
        )

    print_summary(result.summary, warc_mode=request.warc_mode)
    if request.files_mode != "none":
        print_files_summary(
            result.files_summary,
            files_mode=request.files_mode,
        )
    if result.failed_capture_urls:
        print("Failed captures:")
        for url in result.failed_capture_urls:
            print(url)
        return False
    return True


def run_fetch(
    request: FetchRequest,
    *,
    console_log: Optional[ConsoleMirror] = None,
) -> bool:
    """Discover captures, export enabled outputs, and report job success."""

    if request.warc_mode == "none" and request.redirect_capture != "none":
        print("Redirect capture inactive: --warc none")

    if request.warc_mode == "none" and request.files_mode == "none":
        print("Nothing to do: both --warc and --files are none")
        return True

    layout = collection_layout(
        request.url_pattern,
        root=_DEFAULT_OUTPUT_ROOT,
    )
    client_factory = make_client_factory(USER_AGENT)
    with client_factory() as client:
        print(
            f"Discovering captures for {request.url_pattern} "
            f"({request.date_start}-{request.date_end})"
        )
        captures = discover(
            client,
            request.url_pattern,
            request.date_start,
            request.date_end,
            progress=_report_discovery_progress,
            retries=request.retries,
        )
        if not captures:
            print("No captures found")
            return True

        print(f"Discovered {len(captures)} captures")
        print("Saving source acquisition...")
        acquisition = save_acquisition(
            captures,
            layout=layout,
            url_pattern=request.url_pattern,
            date_start=request.date_start,
            date_end=request.date_end,
            acquired_at=datetime.now(timezone.utc),
        )
        if console_log is not None:
            console_log.attach(acquisition.path / "log.txt")

        result = _export_captures(
            request,
            captures,
            client,
            client_factory,
            layout,
        )
        return _finalize_outputs(request, result, layout)
