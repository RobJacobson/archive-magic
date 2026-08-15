"""Load and validate one versioned Archive Magic archive descriptor."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DESCRIPTOR_NAME = "archive.toml"
SCHEMA_VERSION = 1
DEFAULT_WARC_TARGET_BYTES = 250_000_000


@dataclass(frozen=True)
class RemoteConfig:
    bucket: str
    prefix: str = ""
    endpoint_url: str | None = None
    region: str = "auto"


@dataclass(frozen=True)
class StorageConfig:
    authority: str
    workspace_directory: Path
    remote: RemoteConfig | None = None


@dataclass(frozen=True)
class ArchiveDescriptor:
    archive_id: str
    url_pattern: str
    storage: StorageConfig
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    playback_workers: int = 4
    playback_starts_per_second: float = 20.0
    start: str = "1995-01-01"
    end: str | None = None
    wayback_fallback: bool = True
    source: Path | None = None


def descriptor_path(value: Path | str) -> Path:
    """Resolve an archive descriptor path or its containing directory."""

    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / DESCRIPTOR_NAME
    if not candidate.is_file():
        raise ValueError(f"archive descriptor does not exist: {candidate}")
    if candidate.name != DESCRIPTOR_NAME:
        raise ValueError(f"archive descriptor must be named {DESCRIPTOR_NAME}")
    return candidate.resolve()


def load_descriptor(value: Path | str) -> ArchiveDescriptor:
    """Load one explicit archive descriptor."""

    source = descriptor_path(value)
    try:
        with source.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot load archive descriptor {source}: {error}") from error

    _reject_unknown(
        data,
        {"schema_version", "archive", "storage", "fetch", "playback"},
        "archive descriptor",
    )
    version = data.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")

    archive = _required_table(data, "archive")
    storage_data = _required_table(data, "storage")
    fetch = _table(data, "fetch")
    playback = _table(data, "playback")
    _reject_unknown(archive, {"id", "url_pattern"}, "archive")
    _reject_unknown(
        storage_data,
        {"authority", "workspace_directory", "remote"},
        "storage",
    )
    _reject_unknown(
        fetch,
        {
            "warc_target_bytes",
            "playback_workers",
            "playback_starts_per_second",
            "start",
            "end",
        },
        "fetch",
    )
    _reject_unknown(playback, {"wayback_fallback"}, "playback")

    archive_id = _safe_id(_required_string(archive, "id", "archive.id"))
    url_pattern = _required_string(archive, "url_pattern", "archive.url_pattern")
    authority = _string(storage_data.get("authority", "local"), "storage.authority")
    if authority not in {"local", "remote"}:
        raise ValueError("storage.authority must be 'local' or 'remote'")
    base = source.parent
    workspace = _path(
        base,
        storage_data.get("workspace_directory", "workspace"),
        "storage.workspace_directory",
    )

    remote_value = storage_data.get("remote")
    remote: RemoteConfig | None = None
    if authority == "remote":
        if not isinstance(remote_value, dict):
            raise ValueError("storage.remote is required for remote authority")
        remote = _remote(remote_value)
    elif remote_value is not None:
        raise ValueError("storage.remote is only valid for remote authority")

    fallback = playback.get("wayback_fallback", True)
    if not isinstance(fallback, bool):
        raise ValueError("playback.wayback_fallback must be a boolean")
    end_value = fetch.get("end")
    return ArchiveDescriptor(
        archive_id=archive_id,
        url_pattern=url_pattern,
        storage=StorageConfig(authority, workspace, remote),
        warc_target_bytes=_positive_int(
            fetch.get("warc_target_bytes", DEFAULT_WARC_TARGET_BYTES),
            "fetch.warc_target_bytes",
        ),
        playback_workers=_positive_int(
            fetch.get("playback_workers", 4), "fetch.playback_workers"
        ),
        playback_starts_per_second=_positive_number(
            fetch.get("playback_starts_per_second", 20.0),
            "fetch.playback_starts_per_second",
        ),
        start=_string(fetch.get("start", "1995-01-01"), "fetch.start"),
        end=None if end_value is None else _string(end_value, "fetch.end"),
        wayback_fallback=fallback,
        source=source,
    )


def _remote(data: dict[str, object]) -> RemoteConfig:
    _reject_unknown(data, {"bucket", "prefix", "endpoint_url", "region"}, "storage.remote")
    endpoint = data.get("endpoint_url")
    return RemoteConfig(
        bucket=_required_string(data, "bucket", "storage.remote.bucket"),
        prefix=_prefix(data.get("prefix", "")),
        endpoint_url=(
            None
            if endpoint is None
            else _string(endpoint, "storage.remote.endpoint_url")
        ),
        region=_string(data.get("region", "auto"), "storage.remote.region"),
    )


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def _required_table(data: dict[str, object], name: str) -> dict[str, object]:
    if name not in data:
        raise ValueError(f"{name} table is required")
    return _table(data, name)


def _reject_unknown(data: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} setting(s): {', '.join(unknown)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _required_string(data: dict[str, object], key: str, label: str) -> str:
    if key not in data:
        raise ValueError(f"{label} is required")
    return _string(data[key], label)


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value in {".", "..", "static"}:
        raise ValueError(f"invalid archive ID: {value!r}")
    return value


def _path(base: Path, value: object, label: str) -> Path:
    path = Path(_string(value, label)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return float(value)


def _prefix(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("storage.remote.prefix must be a string")
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("storage.remote.prefix must not contain '.' or '..'")
    return "/".join(parts)
