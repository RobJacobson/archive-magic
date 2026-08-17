"""Load one Fetch configuration file."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "fetch.toml"
DEFAULT_WARC_TARGET_BYTES = 250_000_000
DEFAULT_RETRIES = 4


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
    playback_workers: int = 4
    playback_starts_per_second: float = 20.0
    retries: int = DEFAULT_RETRIES
    start: str = "1995-01-01"
    end: str | None = None


@dataclass(frozen=True)
class _Archive:
    id: str
    url_pattern: str


@dataclass(frozen=True)
class _FetchOptions:
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    playback_workers: int = 4
    playback_starts_per_second: float = 20.0
    retries: int = DEFAULT_RETRIES
    start: str = "1995-01-01"
    end: str | None = None


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
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
        archive_id = _safe_id(archive.id)
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        raise ValueError(f"invalid fetch configuration {source}: {error}") from error

    return FetchConfig(
        archive_id=archive_id,
        url_pattern=archive.url_pattern,
        output=output,
        warc_target_bytes=options.warc_target_bytes,
        playback_workers=options.playback_workers,
        playback_starts_per_second=options.playback_starts_per_second,
        retries=options.retries,
        start=options.start,
        end=options.end,
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
