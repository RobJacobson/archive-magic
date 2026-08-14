"""Shared archive.toml descriptor parsing for Archive Magic."""

from .descriptor import (
    DEFAULT_WARC_TARGET_BYTES,
    DESCRIPTOR_NAME,
    SCHEMA_VERSION,
    ArchiveDescriptor,
    RemoteConfig,
    StorageConfig,
    descriptor_path,
    load_descriptor,
)

__all__ = [
    "DEFAULT_WARC_TARGET_BYTES",
    "DESCRIPTOR_NAME",
    "SCHEMA_VERSION",
    "ArchiveDescriptor",
    "RemoteConfig",
    "StorageConfig",
    "descriptor_path",
    "load_descriptor",
]
