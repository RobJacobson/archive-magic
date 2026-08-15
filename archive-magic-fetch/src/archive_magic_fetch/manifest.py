"""Read and write the per-archive collection publication manifest."""

from archive_magic_descriptor import (
    MANIFEST_NAME,
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)

__all__ = [
    "MANIFEST_NAME",
    "CollectionsManifest",
    "ManifestArtifact",
    "ManifestCollection",
    "parse_manifest",
]
