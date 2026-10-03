"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .config import load_config, load_playback_policy
from .fetch import build_settings, run_fetch
from .collection import ArchiveLayout
from .staging import recover_stages
from .storage import archive_lock, sync_archive

DEFAULT_WORKERS = 4
DEFAULT_STARTS_PER_SECOND = 16.0
DEFAULT_RETRIES = 4


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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
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
        help="playback HTTP requests per second, including retries/recovery (default: 16, or fetch-config.toml)",
    )
    parser.add_argument(
        "--trace-requests",
        action="store_true",
        help="write timestamped playback HTTP requests and rolling rates to logs/<run>.requests.jsonl",
    )
    parser.add_argument(
        "--retries",
        type=_nonnegative_int,
        default=None,
        metavar="N",
        help="playback retries (default: 4, or fetch-config.toml)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run fetch and return a process exit status."""

    args = parse_args(argv)
    try:
        config = load_config(args.archive)
        if args.sync_only:
            if config.output.type != "remote":
                raise ValueError("--sync-only requires remote output")
            if args.start is not None or args.end is not None or args.reset_data:
                raise ValueError("--sync-only cannot be combined with dates or --reset-data")
        if config.output.type == "remote" and args.reset_data:
            if args.start is not None or args.end is not None:
                raise ValueError(
                    "remote --reset-data requires the configuration's complete configured date range"
                )
            print(
                "WARNING: --reset-data will delete and rebuild managed remote archive data (metadata and assets are preserved); "
                "playback will be unavailable during the rebuild.",
                file=sys.stderr,
            )
        policy = load_playback_policy(args.config)
        settings = None if args.sync_only else build_settings(
            config.url_pattern,
            archive_id=config.archive_id,
            date_start=args.start,
            date_end=args.end,
            reset_data=args.reset_data,
            output=config.output,
            index_directory=config.index_directory,
            warc_target_bytes=config.warc_target_bytes,
            cdx_window_days=config.cdx_window_days,
            cdx_page_limit=config.cdx_page_limit,
            playback_workers=args.workers if args.workers is not None else policy.workers,
            playback_starts_per_second=(
                args.starts_per_second
                if args.starts_per_second is not None
                else policy.starts_per_second
            ),
            retries=args.retries if args.retries is not None else policy.retries,
            trace_requests=args.trace_requests,
            default_start=config.start,
            default_end=config.end,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        if args.sync_only:
            layout = ArchiveLayout(config.output.data_directory, config.archive_id)
            with archive_lock(layout):
                recover_stages(layout)
                sync_archive(layout, config.output)
            return 0
        assert settings is not None
        result = run_fetch(settings)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"error: {error}", file=sys.stderr)
        return 1
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
