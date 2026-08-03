"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Sequence

from .console import mirror_console_output
from .discovery import FILES_MODES, WARC_MODES
from .job import FetchRequest, run_fetch
from .redirect_capture import REDIRECT_CAPTURE_MODES
from .retrieval import DEFAULT_CONCURRENCY
from .retry import DEFAULT_RETRIES


def current_utc_cdx_timestamp() -> str:
    """Return the current UTC time as a full CDX timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


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


def parse_args(argv: Optional[Sequence[str]] = None) -> FetchRequest:
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
        "--redirect-capture",
        choices=REDIRECT_CAPTURE_MODES,
        default="website",
        help=(
            "Capture permanent redirect targets into WARC as none, exact "
            "page history, or full host history (default: website)"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            "Max concurrent WARC/group workers (default: "
            f"{DEFAULT_CONCURRENCY}; use 1 for serial diagnostics)"
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
    return FetchRequest(
        url_pattern=args.url_pattern,
        date_start=args.start or "1995",
        date_end=args.end or current_utc_cdx_timestamp(),
        warc_mode=args.warc,
        files_mode=args.files,
        rewrite_local=args.rewrite_local,
        redirect_capture=args.redirect_capture,
        concurrency=args.concurrency,
        retries=args.retries,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one timed fetch job and return its process exit status."""

    request = parse_args(argv)
    started_at = _utc_now()
    started_tick = _monotonic()
    with mirror_console_output() as console_log:
        print(f"Job started: {_format_job_time(started_at)}", flush=True)
        print(
            f"Redirect capture: {request.redirect_capture}",
            flush=True,
        )
        try:
            try:
                succeeded = run_fetch(request, console_log=console_log)
            except Exception as error:
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
            return 0 if succeeded else 1
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
        value
            .astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
    )
