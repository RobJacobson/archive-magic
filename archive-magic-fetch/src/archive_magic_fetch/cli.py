"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from wayback import WaybackClient, WaybackSession

from .discovery import OUTPUT_MODES, apply_output_mode, discover, group_captures
from .export import ExportSummary, export_all, print_summary
from .files import FilesSummary, print_files_summary, write_website_files
from .paths import (
    DEFAULT_OUTPUT_ROOT,
    collection_layout,
    preflight_layout,
    preflight_website_layout,
)
from .provenance import save_acquisition
from .replay import generate_replay_index
from .retrieval import RetrievalCache


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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the deliberately small MVP command-line interface."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("url_pattern", metavar="URL_PATTERN")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--warc",
        choices=OUTPUT_MODES,
        default="all",
        help="WARC + replay CDXJ output mode (default: all)",
    )
    parser.add_argument(
        "--files",
        choices=OUTPUT_MODES,
        default="none",
        help="Loose website-file output mode (default: none)",
    )
    return parser.parse_args(argv)


def _report_discovery_progress(count: int) -> None:
    """Print indeterminate discovery progress for long CDX searches."""

    print(f"  fetched {count}...")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Discover captures and export them, returning a process exit status."""

    args = parse_args(argv)
    date_start = args.start or "1995"
    date_end = args.end or current_utc_cdx_timestamp()
    warc_mode = args.warc
    files_mode = args.files

    if warc_mode == "none" and files_mode == "none":
        print("Nothing to do: both --warc and --files are none")
        return 0

    try:
        layout = collection_layout(args.url_pattern, root=_DEFAULT_OUTPUT_ROOT)
        session = WaybackSession(user_agent=USER_AGENT)
        with WaybackClient(session=session) as client:
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
            )
            if not captures:
                print("No captures found")
                return 0

            print(f"Discovered {len(captures)} captures")
            print("Saving source acquisition...")
            save_acquisition(
                captures,
                layout=layout,
                url_pattern=args.url_pattern,
                date_start=date_start,
                date_end=date_end,
                acquired_at=datetime.now(timezone.utc),
            )
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
                    include_timestamps=(files_mode == "all"),
                )

            cache = RetrievalCache()
            warc_summary = ExportSummary()
            if warc_plan is not None:
                print(f"Exporting {len(warc_groups)} URL groups to WARC...")
                warc_result = export_all(
                    warc_groups,
                    warc_plan.buckets,
                    client,
                    cache=cache,
                )
                warc_summary = warc_result.summary
                print("Building replay index...")
                generate_replay_index(
                    warc_result.created_warcs,
                    layout=warc_plan.layout,
                )

            files_summary = FilesSummary()
            if website_plan is not None:
                print(
                    f"Writing {len(website_plan.targets)} website files..."
                )
                files_summary = write_website_files(
                    files_groups,
                    website_plan,
                    client,
                    cache=cache,
                )

            print_summary(warc_summary, warc_mode=warc_mode)
            if files_mode != "none":
                print_files_summary(files_summary, files_mode=files_mode)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0
