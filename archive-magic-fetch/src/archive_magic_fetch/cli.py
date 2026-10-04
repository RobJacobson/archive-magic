"""cli."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

from archive_magic_fetch.app import run_application


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the command and dispatch the application."""
    return run_application(_parse_args(argv))


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the fetch command line."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("archive", type=Path, metavar="ARCHIVE")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="reconcile the local WARC/CDXJ archive to the configured bucket without fetching",
    )
    parser.add_argument(
        "--reset-data",
        action="store_true",
        help=(
            "rebuild selected local collections, or delete and rebuild the complete "
            "managed archive files under data/ when remote output is selected"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="host-level fetch-config.toml (default: ~/.config/archive-magic-fetch/fetch-config.toml)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        metavar="N",
        help="playback workers (default: 4, or fetch-config.toml)",
    )
    parser.add_argument(
        "--starts-per-second",
        type=_positive_float,
        default=None,
        metavar="N",
        help="playback HTTP requests per second, including retries/redirects (default: 16, or fetch-config.toml)",
    )
    parser.add_argument(
        "--trace-requests",
        action="store_true",
        help="write one CSV row per playback HTTP request with millisecond timing and rolling rates to logs/<run>.requests.csv",
    )
    parser.add_argument(
        "--retries",
        type=_nonnegative_int,
        default=None,
        metavar="N",
        help="playback retries (default: 4, or fetch-config.toml)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
