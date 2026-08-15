"""Load and validate one versioned Archive Magic archive descriptor."""

from __future__ import annotations

from pathlib import Path

from archive_magic_descriptor import (
    ArchiveDescriptor as NavigatorConfig,
    DESCRIPTOR_NAME,
    RemoteConfig,
    StorageConfig,
    descriptor_path,
    load_descriptor as load_config,
)

__all__ = [
    "DESCRIPTOR_NAME",
    "NavigatorConfig",
    "RemoteConfig",
    "StorageConfig",
    "descriptor_path",
    "discover_descriptors",
    "load_config",
]


def discover_descriptors(value: Path | str) -> tuple[Path, ...]:
    catalog = Path(value).expanduser()
    try:
        catalog = catalog.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"catalog does not exist or cannot be resolved: {catalog}") from error
    if not catalog.is_dir():
        raise ValueError(f"catalog is not a directory: {catalog}")
    paths = tuple(
        child / DESCRIPTOR_NAME
        for child in sorted(catalog.iterdir(), key=lambda item: item.name)
        if child.is_dir()
        and not child.name.startswith(".")
        and (child / DESCRIPTOR_NAME).is_file()
    )
    if not paths:
        raise ValueError(f"catalog contains no */{DESCRIPTOR_NAME} descriptors: {catalog}")
    return paths
