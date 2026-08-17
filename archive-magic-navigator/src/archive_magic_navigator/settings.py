"""Load one Navigator configuration file and discover catalogs."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "navigator.toml"


@dataclass(frozen=True)
class LocalSource:
    directory: Path


@dataclass(frozen=True)
class RemoteSource:
    bucket: str
    prefix: str = ""
    endpoint_url: str | None = None
    region: str = "auto"


@dataclass(frozen=True)
class NavigatorConfig:
    archive_id: str
    source: LocalSource | RemoteSource
    config_path: Path
    wayback_fallback: bool = True


@dataclass(frozen=True)
class _Archive:
    id: str


@dataclass(frozen=True)
class _Playback:
    wayback_fallback: bool = True


def config_path(value: Path | str) -> Path:
    """Resolve a Navigator configuration path or its containing directory."""

    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / CONFIG_NAME
    if not candidate.is_file():
        raise ValueError(f"navigator configuration does not exist: {candidate}")
    return candidate.resolve()


def load_config(value: Path | str) -> NavigatorConfig:
    """Load one explicit Navigator configuration."""

    path = config_path(value)
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        archive = _Archive(**_section(document, "archive"))
        source_data = dict(_section(document, "source"))
        source_type = source_data.pop("type")
        if source_type == "local":
            source_data["directory"] = _path(
                path.parent, source_data.get("directory", "data")
            )
            source = LocalSource(**source_data)
        elif source_type == "remote":
            source_data["prefix"] = _prefix(source_data.get("prefix", ""))
            source = RemoteSource(**source_data)
        else:
            raise ValueError("source.type must be 'local' or 'remote'")
        playback = _Playback(**_section(document, "playback", required=False))
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
        archive_id = _safe_id(archive.id)
        if not isinstance(playback.wayback_fallback, bool):
            raise ValueError("playback.wayback_fallback must be a boolean")
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        raise ValueError(f"invalid navigator configuration {path}: {error}") from error

    return NavigatorConfig(
        archive_id=archive_id,
        source=source,
        config_path=path,
        wayback_fallback=playback.wayback_fallback,
    )


def discover_configs(value: Path | str) -> tuple[Path, ...]:
    """Discover immediate non-hidden */navigator.toml catalog entries."""

    catalog = Path(value).expanduser()
    try:
        catalog = catalog.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            f"catalog does not exist or cannot be resolved: {catalog}"
        ) from error
    if not catalog.is_dir():
        raise ValueError(f"catalog is not a directory: {catalog}")
    paths = tuple(
        child / CONFIG_NAME
        for child in sorted(catalog.iterdir(), key=lambda item: item.name)
        if child.is_dir()
        and not child.name.startswith(".")
        and (child / CONFIG_NAME).is_file()
    )
    if not paths:
        raise ValueError(f"catalog contains no */{CONFIG_NAME} configurations: {catalog}")
    return paths


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
        raise ValueError("source.prefix must not contain '.' or '..'")
    return "/".join(parts)
