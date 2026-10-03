"""Load Fetch archive and host-instance configuration files."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "fetch.toml"
INSTANCE_CONFIG_NAME = "fetch-config.toml"
INSTANCE_CONFIG_ENV = "ARCHIVE_MAGIC_FETCH_CONFIG"
DEFAULT_WARC_TARGET_BYTES = 250_000_000
DEFAULT_START = "1995-01-01"
DEFAULT_CDX_WINDOW_DAYS = 28
DEFAULT_CDX_PAGE_LIMIT = 5000
DEFAULT_PLAYBACK_WORKERS = 4
DEFAULT_PLAYBACK_STARTS_PER_SECOND = 16.0
DEFAULT_PLAYBACK_RETRIES = 4


@dataclass(frozen=True)
class FetchOutput:
    type: str
    data_directory: Path
    bucket: str | None = None
    prefix: str = ""
    endpoint_url: str | None = None
    region: str = "auto"


@dataclass(frozen=True)
class FetchConfig:
    archive_id: str
    url_pattern: str
    output: FetchOutput
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    start: str = DEFAULT_START
    end: str | None = None
    index_directory: Path | None = None


@dataclass(frozen=True)
class _Archive:
    id: str
    url_pattern: str


@dataclass(frozen=True)
class PlaybackPolicy:
    workers: int = DEFAULT_PLAYBACK_WORKERS
    starts_per_second: float = DEFAULT_PLAYBACK_STARTS_PER_SECOND
    retries: int = DEFAULT_PLAYBACK_RETRIES


@dataclass(frozen=True)
class _FetchOptions:
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    start: str = DEFAULT_START
    end: str | None = None


@dataclass(frozen=True)
class _PlaybackOptions:
    workers: int = DEFAULT_PLAYBACK_WORKERS
    starts_per_second: float = DEFAULT_PLAYBACK_STARTS_PER_SECOND
    retries: int = DEFAULT_PLAYBACK_RETRIES


def config_path(value: Path | str) -> Path:
    """Resolve a Fetch configuration path or its containing directory."""

    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / CONFIG_NAME
    if not candidate.is_file():
        raise ValueError(f"fetch configuration does not exist: {candidate}")
    return candidate.resolve()


def load_config(value: Path | str) -> FetchConfig:
    """Load one explicit Fetch configuration."""

    source = config_path(value)
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
        archive = _Archive(**_section(document, "archive"))
        output_data = dict(_section(document, "output"))
        output_type = output_data.pop("type")
        output_data["data_directory"] = _path(
            source.parent, output_data.get("data_directory", "data")
        )
        if output_type == "remote":
            output_data["prefix"] = _prefix(output_data.get("prefix", ""))
        elif output_type != "local":
            raise ValueError("output.type must be 'local' or 'remote'")
        output = FetchOutput(output_type, **output_data)
        if output.type == "remote" and not output.bucket:
            raise ValueError("output.bucket is required for remote output")
        options = _FetchOptions(**_section(document, "fetch", required=False))
        if options.warc_target_bytes <= 0:
            raise ValueError("fetch.warc_target_bytes must be positive")
        if options.cdx_window_days <= 0:
            raise ValueError("fetch.cdx_window_days must be positive")
        if options.cdx_page_limit <= 0:
            raise ValueError("fetch.cdx_page_limit must be positive")
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
        archive_id = _safe_id(archive.id)
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "fetch configuration does not exist:"
        ):
            raise
        raise ValueError(f"invalid fetch configuration {source}: {error}") from error

    return FetchConfig(
        archive_id=archive_id,
        url_pattern=archive.url_pattern,
        output=output,
        warc_target_bytes=options.warc_target_bytes,
        cdx_window_days=options.cdx_window_days,
        cdx_page_limit=options.cdx_page_limit,
        start=options.start,
        end=options.end,
        index_directory=source.parent / "index",
    )


def default_instance_config_path() -> Path:
    """Return the default host-level Fetch policy path."""

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / "archive-magic-fetch" / INSTANCE_CONFIG_NAME).resolve()


def instance_config_path(value: Path | str | None = None) -> Path | None:
    """Resolve an explicit, env, or default instance config path."""

    if value is not None:
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            raise ValueError(f"fetch instance configuration does not exist: {candidate}")
        return candidate.resolve()
    env = os.environ.get(INSTANCE_CONFIG_ENV)
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_file():
            raise ValueError(f"fetch instance configuration does not exist: {candidate}")
        return candidate.resolve()
    default = default_instance_config_path()
    return default if default.is_file() else None


def load_playback_policy(value: Path | str | None = None) -> PlaybackPolicy:
    """Load host-level playback policy, or code defaults when no file exists."""

    source = instance_config_path(value)
    if source is None:
        return PlaybackPolicy()
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
        options = _PlaybackOptions(**_section(document, "playback", required=False))
        if options.workers <= 0:
            raise ValueError("playback.workers must be positive")
        if float(options.starts_per_second) <= 0:
            raise ValueError("playback.starts_per_second must be positive")
        if options.retries < 0:
            raise ValueError("playback.retries must be nonnegative")
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "fetch instance configuration does not exist:"
        ):
            raise
        raise ValueError(
            f"invalid fetch instance configuration {source}: {error}"
        ) from error
    return PlaybackPolicy(
        workers=options.workers,
        starts_per_second=float(options.starts_per_second),
        retries=options.retries,
    )


def _section(
    document: dict[str, object], name: str, *, required: bool = True
) -> dict[str, object]:
    value = document.pop(name) if required else document.pop(name, {})
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a TOML table")
    return value


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value in {
        ".",
        "..",
        "static",
    }:
        raise ValueError(f"invalid archive ID: {value!r}")
    return value


def _path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _prefix(value: str) -> str:
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("output.prefix must not contain '.' or '..'")
    return "/".join(parts)
