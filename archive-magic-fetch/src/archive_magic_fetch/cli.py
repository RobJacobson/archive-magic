"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .config import load_config
from .fetch import build_settings, run_fetch


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the minimal fetch command line."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("archive", type=Path, metavar="ARCHIVE")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--reset-data",
        action="store_true",
        help=(
            "rebuild selected local collections, or delete and rebuild the complete "
            "configured archive prefix when remote authority is selected"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run fetch and return a process exit status."""

    args = parse_args(argv)
    try:
        config = load_config(args.archive)
        if config.storage.authority == "remote" and args.reset_data:
            if args.start is not None or args.end is not None:
                raise ValueError(
                    "remote --reset-data requires the descriptor's complete configured date range"
                )
            print(
                "WARNING: --reset-data will delete and rebuild the entire remote archive prefix; "
                "playback will be unavailable during the rebuild.",
                file=sys.stderr,
            )
        settings = build_settings(
            config.url_pattern,
            archive_id=config.archive_id,
            date_start=args.start,
            date_end=args.end,
            reset_data=args.reset_data,
            storage=config.storage,
            warc_target_bytes=config.warc_target_bytes,
            playback_workers=config.playback_workers,
            playback_starts_per_second=config.playback_starts_per_second,
            default_start=config.start,
            default_end=config.end,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
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
