"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional, Sequence

from .discovery import discover, group_captures
from .export import export_all
from .paths import preflight_paths


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Discover captures and export them, returning a process exit status."""

    args = parse_args(argv)
    date_start = args.start or "1995"
    date_end = args.end or current_utc_cdx_timestamp()

    try:
        captures = discover(args.url_pattern, date_start, date_end)
        if not captures:
            print("No captures found")
            return 0

        captures_by_url = group_captures(captures)
        output_paths = preflight_paths(captures_by_url)
        export_all(captures_by_url, output_paths)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0

