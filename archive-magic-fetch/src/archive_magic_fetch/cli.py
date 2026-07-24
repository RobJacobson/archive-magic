"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from wayback import WaybackClient, WaybackSession

from .discovery import discover, group_captures
from .export import export_all, print_summary
from .paths import DEFAULT_OUTPUT_ROOT, collection_layout, preflight_layout
from .provenance import save_acquisition
from .replay import generate_replay_index


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
    return parser.parse_args(argv)


def _report_discovery_progress(count: int) -> None:
    """Print indeterminate discovery progress for long CDX searches."""

    print(f"  fetched {count}...")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Discover captures and export them, returning a process exit status."""

    args = parse_args(argv)
    date_start = args.start or "1995"
    date_end = args.end or current_utc_cdx_timestamp()

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
            plan = preflight_layout(capture_groups, layout)
            print(f"Exporting {len(capture_groups)} URL groups...")
            result = export_all(capture_groups, plan.buckets, client)
            print("Building replay index...")
            generate_replay_index(
                result.created_warcs,
                layout=plan.layout,
            )
            print_summary(result.summary)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0
