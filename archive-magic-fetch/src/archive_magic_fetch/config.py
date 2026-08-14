"""Load and validate one versioned Archive Magic archive descriptor."""

from archive_magic_descriptor import (
    DEFAULT_WARC_TARGET_BYTES,
    ArchiveDescriptor as FetchConfig,
    RemoteConfig,
    StorageConfig,
    descriptor_path,
    load_descriptor as load_config,
)

__all__ = [
    "DEFAULT_WARC_TARGET_BYTES",
    "FetchConfig",
    "RemoteConfig",
    "StorageConfig",
    "descriptor_path",
    "load_config",
]
