"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .console import ConsoleMirror, mirror_console_output
from .discovery import (
    FILES_MODES,
    WARC_MODES,
    apply_output_mode,
    discover,
    group_captures,
)
from .export import export_all, print_summary
from .files import print_files_summary
from .paths import (
    DEFAULT_OUTPUT_ROOT,
    collection_layout,
    preflight_layout,
    preflight_website_layout,
)
from .provenance import save_acquisition
from .replay import generate_replay_index
from .retrieval import (
    DEFAULT_CONCURRENCY,
    make_client_factory,
)
from .retry import DEFAULT_RETRIES
from .rewrite_local import rewrite_local_website


USER_AGENT = (
    "archive-magic-fetch/0.1.0 "
    "(+https://github.com/RobJacobson/archive-magic)"
)

# archive-magic-fetch/ — sibling of archives/, independent of process cwd
_FETCH_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_ROOT = (_FETCH_PROJECT_ROOT / DEFAULT_OUTPUT_ROOT).resolve()


def current_utc_cdx_timestamp() -> str:
    """Return the current UTC time as a full CDX timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the deliberately small MVP command-line interface."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("url_pattern", metavar="URL_PATTERN")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--warc",
        choices=WARC_MODES,
        default="all",
        help="WARC + replay CDXJ output mode (default: all)",
    )
    parser.add_argument(
        "--files",
        choices=FILES_MODES,
        default="none",
        help="Loose website-file output mode (default: none)",
    )
    parser.add_argument(
        "--rewrite-local",
        action="store_true",
        help=(
            "After --files writing, rewrite HTML/CSS/JS under website/ "
            "for local relative browsing"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            "Max concurrent WARC/group workers (default: "
            f"{DEFAULT_CONCURRENCY}; use 1 for serial diagnostics)"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        metavar="N",
        help=(
            "Retries after an initial IA request (default: "
            f"{DEFAULT_RETRIES}; use 0 to disable retries)"
        ),
    )
    return parser.parse_args(argv)


def _report_discovery_progress(count: int) -> None:
    """Print indeterminate discovery progress for long CDX searches."""

    print(f"  fetched {count}...")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one timed fetch job and return its process exit status."""

    args = parse_args(argv)
    started_at = _utc_now()
    started_tick = _monotonic()
    with mirror_console_output() as console_log:
        print(f"Job started: {_format_job_time(started_at)}", flush=True)
        try:
            return _run(args, console_log=console_log)
        finally:
            ended_at = _utc_now()
            duration_minutes = (_monotonic() - started_tick) / 60
            print(f"Job ended: {_format_job_time(ended_at)}", flush=True)
            print(
                f"Job duration: {duration_minutes:.1f} minutes",
                flush=True,
            )


def _format_job_time(value: datetime) -> str:
    """Format an aware time as a compact UTC ISO-8601 timestamp."""

    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _run(
    args: argparse.Namespace,
    *,
    console_log: Optional[ConsoleMirror] = None,
) -> int:
    """Discover captures and export them for already-parsed arguments."""

    date_start = args.start or "1995"
    date_end = args.end or current_utc_cdx_timestamp()
    warc_mode = args.warc
    files_mode = args.files
    rewrite_local = args.rewrite_local
    concurrency = args.concurrency
    retries = args.retries

    if concurrency < 1:
        print("ERROR: --concurrency must be at least 1", file=sys.stderr)
        return 2
    if retries < 0:
        print("ERROR: --retries cannot be negative", file=sys.stderr)
        return 2

    if rewrite_local and files_mode == "none":
        print(
            "ERROR: --rewrite-local requires --files latest, unique, or all",
            file=sys.stderr,
        )
        return 2

    if warc_mode == "none" and files_mode == "none":
        print("Nothing to do: both --warc and --files are none")
        return 0

    try:
        layout = collection_layout(args.url_pattern, root=_DEFAULT_OUTPUT_ROOT)
        client_factory = make_client_factory(USER_AGENT)
        with client_factory() as client:
            print(
                f"Discovering captures for {args.url_pattern} "
                f"({date_start}-{date_end})"
            )
            captures = discover(
                client,
                args.url_pattern,
                date_start,
                date_end,
                progress=_report_discovery_progress,
                retries=retries,
            )
            if not captures:
                print("No captures found")
                return 0

            print(f"Discovered {len(captures)} captures")
            print("Saving source acquisition...")
            acquisition = save_acquisition(
                captures,
                layout=layout,
                url_pattern=args.url_pattern,
                date_start=date_start,
                date_end=date_end,
                acquired_at=datetime.now(timezone.utc),
            )
            acquisition_path = getattr(acquisition, "path", None)
            if console_log is not None and acquisition_path is not None:
                console_log.attach(acquisition_path / "log.txt")
            print(f"Grouping {len(captures)} captures...")
            capture_groups = group_captures(captures)
            warc_groups = apply_output_mode(capture_groups, warc_mode)
            files_groups = apply_output_mode(capture_groups, files_mode)

            warc_plan = None
            if warc_mode != "none":
                warc_plan = preflight_layout(warc_groups, layout)

            website_plan = None
            if files_mode != "none":
                website_plan = preflight_website_layout(
                    files_groups,
                    layout,
                    include_timestamps=(files_mode in {"unique", "all"}),
                )

            print(
                "Exporting "
                f"{len(set(warc_groups).union(files_groups))} URL groups "
                f"(concurrency={concurrency})..."
            )
            export_result = export_all(
                warc_groups,
                warc_plan.buckets if warc_plan is not None else (),
                client,
                file_capture_groups=files_groups,
                website_plan=website_plan,
                warc_mode=warc_mode,
                files_mode=files_mode,
                client_factory=client_factory,
                concurrency=concurrency,
                retries=retries,
            )
            warc_summary = export_result.summary
            files_summary = export_result.files_summary
            if warc_plan is not None:
                print("Building replay index...")
                generate_replay_index(
                    export_result.created_warcs,
                    layout=warc_plan.layout,
                )
            if website_plan is not None:
                if rewrite_local and files_summary.written > 0:
                    rewrite_local_website(
                        layout.website_root,
                        include_timestamps=(
                            files_mode in {"unique", "all"}
                        ),
                    )

            print_summary(warc_summary, warc_mode=warc_mode)
            if files_mode != "none":
                print_files_summary(files_summary, files_mode=files_mode)
            if export_result.failed_capture_urls:
                print("Failed captures:")
                for url in export_result.failed_capture_urls:
                    print(url)
                return 1
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0
