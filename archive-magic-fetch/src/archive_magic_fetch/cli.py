"""Command-line entry point for Archive Magic Fetch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .fetch import build_settings, run_fetch


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the minimal fetch command line."""

    parser = argparse.ArgumentParser(prog="archive-magic-fetch")
    parser.add_argument("url_pattern", metavar="URL_PATTERN")
    parser.add_argument("--start", metavar="DATE")
    parser.add_argument("--end", metavar="DATE")
    parser.add_argument(
        "--archives-root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--strict-digests",
        action="store_true",
        help=(
            "reject playback bodies whose digest disagrees with CDX "
            "(default: keep imperfect payloads and still allow them as "
            "revisit representatives)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run fetch and return a process exit status."""

    args = parse_args(argv)
    try:
        settings = build_settings(
            args.url_pattern,
            date_start=args.start,
            date_end=args.end,
            archives_root=args.archives_root,
            strict_digests=args.strict_digests,
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
