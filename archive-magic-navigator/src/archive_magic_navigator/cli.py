"""Command-line entry point for Archive Magic Navigator."""

from __future__ import annotations

import argparse
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .collections import Archive, select_archive_root
from .config import build_config, write_config
from .errors import NavigatorError, ValidationError
from .process import is_loopback_bind, run_wayback
from .remote import RemoteArchiveStore
from .settings import NavigatorConfig, discover_descriptors, load_config
from .validation import validate_archive


@dataclass(frozen=True)
class NavigatorRequest:
    archive: Path | None
    catalog: Path | None
    source: str
    cache: Path | None
    poll_interval_seconds: float
    bind: str
    port: int
    wayback_fallback: bool | None
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
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _bind(value: str) -> str:
    if not value or "\x00" in value:
        raise argparse.ArgumentTypeError("must be a non-empty address")
    return value


def parse_args(argv: Sequence[str] | None = None) -> NavigatorRequest:
    parser = argparse.ArgumentParser(prog="archive-magic-navigator")
    parser.add_argument("archive", nargs="?", type=Path, metavar="ARCHIVE")
    parser.add_argument("--catalog", type=Path, metavar="PATH")
    parser.add_argument(
        "--source",
        choices=("auto", "local", "remote"),
        default="auto",
        help="serve each archive from its authority, local workspace, or remote bucket",
    )
    parser.add_argument("--cache", type=Path, metavar="PATH")
    parser.add_argument(
        "--poll-interval",
        type=_positive_number,
        default=60.0,
        metavar="SECONDS",
    )
    parser.add_argument("--bind", type=_bind, default="127.0.0.1", metavar="ADDRESS")
    parser.add_argument("--port", type=_port, default=8080, metavar="PORT")
    parser.add_argument("--wayback-fallback", choices=("on", "off"), default=None)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if (args.archive is None) == (args.catalog is None):
        parser.error("exactly one of ARCHIVE and --catalog is required")
    return NavigatorRequest(
        args.archive,
        args.catalog,
        args.source,
        args.cache,
        args.poll_interval,
        args.bind,
        args.port,
        None if args.wayback_fallback is None else args.wayback_fallback == "on",
        args.open_browser,
        args.debug,
    )


def main(argv: Sequence[str] | None = None) -> int:
    request = parse_args(argv)
    remotes: list[RemoteArchiveStore] = []
    try:
        descriptors = (
            discover_descriptors(request.catalog)
            if request.catalog is not None
            else (request.archive,)
        )
        settings = _load_settings(descriptors)
        _validate_unique_ids(settings)
        cache = _cache_directory(request, settings)
        _validate_remote_environment(settings, request.source)

        archives: list[Archive] = []
        fallbacks: dict[str, bool] = {}
        labels: list[str] = []
        child_environment = None
        archive_errors: list[str] = []
        for item in settings:
            try:
                use_remote = request.source == "remote" or (
                    request.source == "auto"
                    and item.storage.authority == "remote"
                )
                remote = None
                if use_remote:
                    if item.storage.remote is None:
                        raise ValidationError(
                            f"archive {item.archive_id!r} has no remote storage"
                        )
                    remote = RemoteArchiveStore(
                        item.storage.remote,
                        cache,
                        request.poll_interval_seconds,
                    )
                    archive = remote.load_archive(item.archive_id)
                    remote_cfg = item.storage.remote
                    label = (
                        f"s3://{remote_cfg.bucket}/{remote_cfg.prefix}"
                    ).rstrip("/")
                else:
                    archive = select_archive_root(
                        item.storage.workspace_directory,
                        item.archive_id,
                    )
                    label = str(item.storage.workspace_directory)
                validate_archive(archive)
            except (NavigatorError, ValueError) as error:
                archive_errors.append(f"{item.archive_id}: {error}")
                continue
            if remote is not None:
                remotes.append(remote)
                child_environment = remote.child_environment()
            labels.append(label)
            archives.append(archive)
            fallbacks[item.archive_id] = (
                item.wayback_fallback
                if request.wayback_fallback is None
                else request.wayback_fallback
            )
        if archive_errors:
            raise ValidationError(
                "invalid archive data:\n  - " + "\n  - ".join(archive_errors)
            )

        if not is_loopback_bind(request.bind):
            print(
                "WARNING: non-loopback binding exposes an unauthenticated "
                "development archive server; TLS and hostile-content hardening are not provided.",
                file=sys.stderr,
            )

        with tempfile.TemporaryDirectory(prefix="archive-magic-navigator-") as name:
            runtime = Path(name).resolve()
            write_config(runtime, build_config(tuple(archives), wayback_fallback=fallbacks))

            def ready(url: str) -> None:
                collection_count = sum(len(item.collections) for item in archives)
                print("Archive Magic Navigator", flush=True)
                print(
                    f"Serving {len(archives)} domain "
                    f"{'archive' if len(archives) == 1 else 'archives'} with "
                    f"{collection_count} portable "
                    f"{'collection' if collection_count == 1 else 'collections'} "
                    f"from {', '.join(labels)}",
                    flush=True,
                )
                values = set(fallbacks.values())
                label = "mixed" if len(values) > 1 else ("on" if values.pop() else "off")
                print(f"Wayback fallback: {label}", flush=True)
                print(f"Open {url}", flush=True)
                print("Press Ctrl-C to stop.", flush=True)
                if request.open_browser:
                    webbrowser.open(url)

            for remote in remotes:
                remote.start_polling()
            try:
                kwargs = {}
                if child_environment is not None:
                    kwargs["child_environment"] = child_environment
                return run_wayback(
                    runtime,
                    request.bind,
                    request.port,
                    debug=request.debug,
                    on_ready=ready,
                    **kwargs,
                )
            finally:
                for remote in remotes:
                    remote.stop_polling()
    except (NavigatorError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _cache_directory(
    request: NavigatorRequest,
    settings: tuple[NavigatorConfig, ...],
) -> Path:
    if request.cache is not None:
        return request.cache.expanduser().resolve()
    base = (
        request.catalog.expanduser().resolve()
        if request.catalog is not None
        else settings[0].source.parent
    )
    return (base / "navigator-cache").resolve()


def _validate_unique_ids(settings: tuple[NavigatorConfig, ...]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in settings:
        if item.archive_id in seen:
            duplicates.add(item.archive_id)
        seen.add(item.archive_id)
    if duplicates:
        raise ValidationError(
            "duplicate archive ID(s): " + ", ".join(sorted(duplicates))
        )


def _load_settings(descriptors: tuple[Path | None, ...]) -> tuple[NavigatorConfig, ...]:
    settings: list[NavigatorConfig] = []
    errors: list[str] = []
    for path in descriptors:
        if path is None:
            continue
        try:
            settings.append(load_config(path))
        except ValueError as error:
            errors.append(f"{path}: {error}")
    if errors:
        raise ValidationError(
            "invalid archive descriptor(s):\n  - " + "\n  - ".join(errors)
        )
    return tuple(settings)


def _validate_remote_environment(
    settings: tuple[NavigatorConfig, ...],
    source: str,
) -> None:
    signatures = {
        (item.storage.remote.endpoint_url, item.storage.remote.region)
        for item in settings
        if item.storage.remote is not None
        and (source == "remote" or (source == "auto" and item.storage.authority == "remote"))
    }
    if len(signatures) > 1:
        raise ValidationError(
            "remote catalog archives must share endpoint_url and region"
        )
