"""Command-line entry point for Archive Magic Navigator."""

from __future__ import annotations

import argparse
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .collections import (
    Archive,
    discover_archives,
    resolve_archives_root,
    select_archive,
)
from .config import build_config, write_config
from .errors import NavigatorError, ValidationError
from .process import is_loopback_bind, run_wayback
from .validation import validate_archive


@dataclass(frozen=True)
class NavigatorRequest:
    """Normalized public CLI request."""

    archive_id: str | None
    archives: Path
    bind: str
    port: int
    wayback_fallback: bool
    open_browser: bool
    debug: bool


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def _bind(value: str) -> str:
    if not value or "\x00" in value:
        raise argparse.ArgumentTypeError("must be a non-empty address")
    return value


def parse_args(
    argv: Sequence[str] | None = None,
) -> NavigatorRequest:
    """Parse the intentionally small Navigator interface."""

    parser = argparse.ArgumentParser(prog="archive-magic-navigator")
    parser.add_argument(
        "archive",
        nargs="?",
        metavar="ARCHIVE",
        help="immediate domain archive name beneath --archives",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="serve every immediate domain archive beneath --archives",
    )
    parser.add_argument(
        "--archives",
        type=Path,
        default=Path("./archives"),
        metavar="PATH",
        help="domain archives root (default: ./archives)",
    )
    parser.add_argument(
        "--bind",
        type=_bind,
        default="127.0.0.1",
        metavar="ADDRESS",
        help="listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=8080,
        metavar="PORT",
        help="listen port (default: 8080)",
    )
    parser.add_argument(
        "--wayback-fallback",
        choices=("on", "off"),
        default="on",
        help=(
            "load missing resources from the Internet Archive "
            "(default: on)"
        ),
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="open the landing page after the server is ready",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show Navigator and pywb diagnostic output",
    )
    args = parser.parse_args(argv)
    if (args.archive is None) == (not args.all):
        parser.error("exactly one of ARCHIVE and --all is required")
    return NavigatorRequest(
        archive_id=args.archive,
        archives=args.archives,
        bind=args.bind,
        port=args.port,
        wayback_fallback=args.wayback_fallback == "on",
        open_browser=args.open_browser,
        debug=args.debug,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate collections, then own one constrained pywb child."""

    request = parse_args(argv)
    try:
        archives_root = resolve_archives_root(request.archives)
        archives = _select_archives(request, archives_root)
        _validate_archives(
            archives,
            aggregate=request.archive_id is None,
        )

        if not is_loopback_bind(request.bind):
            print(
                "WARNING: non-loopback binding exposes an unauthenticated "
                "development archive server; TLS and hostile-content "
                "hardening are not provided.",
                file=sys.stderr,
            )

        with tempfile.TemporaryDirectory(
            prefix="archive-magic-navigator-"
        ) as runtime_name:
            runtime_directory = Path(runtime_name).resolve()
            try:
                runtime_directory.relative_to(archives_root)
            except ValueError:
                pass
            else:
                raise ValidationError(
                    "temporary runtime directory must be outside archives root"
                )
            config = build_config(
                archives,
                wayback_fallback=request.wayback_fallback,
            )
            write_config(runtime_directory, config)

            def ready(url: str) -> None:
                print("Archive Magic Navigator", flush=True)
                print(
                    f"Serving {len(archives)} domain "
                    f"{'archive' if len(archives) == 1 else 'archives'} "
                    f"with {sum(len(item.collections) for item in archives)} "
                    f"portable collections from {archives_root}",
                    flush=True,
                )
                print(
                    "Wayback fallback: "
                    f"{'on' if request.wayback_fallback else 'off'}",
                    flush=True,
                )
                print(f"Open {url}", flush=True)
                print("Press Ctrl-C to stop.", flush=True)
                if request.open_browser:
                    webbrowser.open(url)

            return run_wayback(
                runtime_directory,
                request.bind,
                request.port,
                debug=request.debug,
                on_ready=ready,
            )
    except NavigatorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _select_archives(
    request: NavigatorRequest,
    archives_root: Path,
) -> tuple[Archive, ...]:
    if request.archive_id is None:
        return discover_archives(archives_root)
    return (select_archive(archives_root, request.archive_id),)


def _validate_archives(
    archives: tuple[Archive, ...],
    *,
    aggregate: bool,
) -> None:
    failures: list[str] = []
    for archive in archives:
        try:
            validate_archive(archive)
        except ValidationError as error:
            if not aggregate:
                raise
            failures.append(str(error))
    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise ValidationError(f"invalid archives:\n{details}")
