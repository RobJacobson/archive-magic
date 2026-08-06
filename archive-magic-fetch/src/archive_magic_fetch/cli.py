"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional, Sequence

from .console import mirror_console_output
from .search import FILES_MODES
from .fetch import FetchSettings, run_fetch
from .downloads import DEFAULT_WORKER_COUNT
from .retry import DEFAULT_RETRIES


def current_utc_cdx_timestamp() -> str:
    """Return the current UTC time as a full CDX timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _positive_int(value: str) -> int:
    """Parse an integer greater than zero for argparse."""

    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    """Parse a nonnegative integer for argparse."""

    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("cannot be negative")
    return parsed


def _boolean(value: str) -> bool:
    """Parse the explicit lowercase true/false CLI vocabulary."""

    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def parse_args(argv: Optional[Sequence[str]] = None) -> FetchSettings:
    """Parse the deliberately small MVP command-line interface."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("url_pattern", metavar="URL_PATTERN")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--build-warc",
        type=_boolean,
        choices=(True, False),
        default=True,
        metavar="true|false",
        help="Build complete WARC + replay CDXJ output (default: true)",
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
        "--workers",
        type=_positive_int,
        default=DEFAULT_WORKER_COUNT,
        metavar="N",
        help=(
            "Maximum simultaneous WARC builds "
            f"(default: {DEFAULT_WORKER_COUNT})"
        ),
    )
    parser.add_argument(
        "--retries",
        type=_nonnegative_int,
        default=DEFAULT_RETRIES,
        metavar="N",
        help=(
            "Retries after an initial IA request (default: "
            f"{DEFAULT_RETRIES}; use 0 to disable retries)"
        ),
    )
    args = parser.parse_args(argv)
    if args.rewrite_local and args.files == "none":
        parser.error(
            "--rewrite-local requires --files latest, unique, or all"
        )
    return FetchSettings(
        url_pattern=args.url_pattern,
        date_start=args.start or "1995",
        date_end=args.end or current_utc_cdx_timestamp(),
        build_warc=args.build_warc,
        files_mode=args.files,
        rewrite_local=args.rewrite_local,
        worker_count=args.workers,
        retries=args.retries,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one timed fetch request and return its process exit status."""

    settings = parse_args(argv)
    with mirror_console_output() as console_log:
        try:
            succeeded = run_fetch(settings, console_log=console_log)
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        return 0 if succeeded else 1
