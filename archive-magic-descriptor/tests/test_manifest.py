import hashlib

import pytest
from archive_magic_descriptor import (
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)


def artifact(key: str, body: bytes = b"data") -> ManifestArtifact:
    return ManifestArtifact(
        key,
        '"etag"',
        hashlib.sha256(body).hexdigest(),
        len(body),
    )


def test_manifest_round_trip_sorts_warcs_and_rejects_unknown_keys():
    timestamp = "2026-08-13T20:15:00Z"
    manifest = CollectionsManifest(
        timestamp,
        {
            "2005": ManifestCollection(
                timestamp,
                artifact("example-2005-index.cdxj"),
                (
                    artifact("example-2005-002.warc.gz"),
                    artifact("example-2005-001.warc.gz"),
                ),
            )
        },
    )
    parsed = parse_manifest(manifest.to_bytes())
    assert [item.key for item in parsed.collections["2005"].warcs] == [
        "example-2005-001.warc.gz",
        "example-2005-002.warc.gz",
    ]
    extra = manifest.to_bytes().replace(
        b'"published_at"',
        b'"schema_version": 1, "published_at"',
        1,
    )
    with pytest.raises(ValueError, match="published_at and collections"):
        parse_manifest(extra)
