"""Load and validate one archive configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from archive_magic_fetch.config.models import (
    CONFIG_NAME,
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
    DEFAULT_START,
    DEFAULT_WARC_TARGET_BYTES,
    FetchConfig,
    FetchOutput,
)
from archive_magic_fetch.config.toml import read_section


@dataclass(frozen=True)
class _Archive:
    id: str
    url_pattern: str


@dataclass(frozen=True)
class _FetchOptions:
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    start: str = DEFAULT_START
    end: str | None = None


def load_config(value: Path | str) -> FetchConfig:
    """Load one explicit Fetch configuration."""

    source = _config_path(value)
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
        archive = _Archive(**read_section(document, "archive"))
        output_data = dict(read_section(document, "output"))
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
        options = _FetchOptions(**read_section(document, "fetch", required=False))
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


def _config_path(value: Path | str) -> Path:
    """Resolve a Fetch configuration path or its containing directory."""

    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / CONFIG_NAME
    if not candidate.is_file():
        raise ValueError(f"fetch configuration does not exist: {candidate}")
    return candidate.resolve()


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
