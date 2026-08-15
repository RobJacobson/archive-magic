"""Shared archive descriptor and publication-manifest parsing for Archive Magic."""

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
from .manifest import (
    MANIFEST_NAME,
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)

__all__ = [
    "DEFAULT_WARC_TARGET_BYTES",
    "DESCRIPTOR_NAME",
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "ArchiveDescriptor",
    "CollectionsManifest",
    "ManifestArtifact",
    "ManifestCollection",
    "RemoteConfig",
    "StorageConfig",
    "descriptor_path",
    "load_descriptor",
    "parse_manifest",
]
