"""Command-line entry point for Archive Magic Navigator."""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import build_config, write_config
from .errors import NavigatorError
from .process import is_loopback_bind, run_wayback
from .settings import load_catalog
from .catalog import CatalogStore



@dataclass(frozen=True)
class NavigatorRequest:
    catalog: Path
    cache: Path | None
    poll_interval_seconds: float
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


def _positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _bind(value: str) -> str:
    if not value or "\x00" in value:
        raise argparse.ArgumentTypeError("must be a non-empty address")
    return value


def parse_args(argv: Sequence[str] | None = None) -> NavigatorRequest:
    parser = argparse.ArgumentParser(prog="archive-magic-navigator")
    parser.add_argument("archive", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--catalog", type=Path, metavar="PATH")
    parser.add_argument("--cache", type=Path, metavar="PATH")
    parser.add_argument(
        "--poll-interval",
        type=_positive_number,
        default=300.0,
        help="catalog refresh interval (default: 300)",
        metavar="SECONDS",
    )
    parser.add_argument("--bind", type=_bind, default="127.0.0.1", metavar="ADDRESS")
    parser.add_argument("--port", type=_port, default=8080, metavar="PORT")
    parser.add_argument("--wayback-fallback", choices=("on", "off"), default="on")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.archive is not None:
        parser.error("positional archive configuration and navigator.toml were removed; use --catalog catalog.json")
    if args.catalog is None:
        parser.error("--catalog catalog.json is required")
    return NavigatorRequest(
        args.catalog,
        args.cache,
        args.poll_interval,
        args.bind,
        args.port,
        args.wayback_fallback == "on",
        args.open_browser,
        args.debug,
    )


def main(argv: Sequence[str] | None = None) -> int:
    request = parse_args(argv)
    try:
        config = load_catalog(request.catalog)
        cache = (request.cache.expanduser().resolve() if request.cache else config.path.parent / "navigator-cache")
        if not is_loopback_bind(request.bind):
            print("WARNING: non-loopback binding exposes an unauthenticated development archive server; TLS and hostile-content hardening are not provided.", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="archive-magic-navigator-") as name:
            runtime = Path(name).resolve()
            write_config(runtime, build_config([], wayback_fallback=request.wayback_fallback))
            store = CatalogStore(config, cache, runtime, request.poll_interval_seconds, request.wayback_fallback)
            store.refresh(startup=True)
            environment = os.environ.copy()
            if config.archives:
                source = config.archives[0]
                if source.endpoint_url:
                    environment["AWS_ENDPOINT_URL_S3"] = source.endpoint_url
                environment["AWS_REGION"] = source.region
                environment["AWS_DEFAULT_REGION"] = source.region

            def ready(url: str) -> None:
                count = sum(entry.archive is not None for entry in store.entries)
                print(f"Archive Magic Navigator: {count}/{len(store.entries)} archives available", flush=True)
                print(f"Open {url}", flush=True)
                print("Press Ctrl-C to stop.", flush=True)
                if request.open_browser:
                    webbrowser.open(url)

            store.start_polling()
            try:
                return run_wayback(runtime, request.bind, request.port, debug=request.debug,
                                   on_ready=ready, child_environment=environment)
            finally:
                store.stop_polling()
    except (NavigatorError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
