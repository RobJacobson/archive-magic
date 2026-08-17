import hashlib
import json

import pytest
from archive_magic_format import (
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


def manifest_bytes() -> bytes:
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
    return manifest.to_bytes()


def test_manifest_round_trip_sorts_serialized_and_parsed_artifacts():
    data = manifest_bytes()
    assert data.endswith(b"\n")
    assert data.index(b"example-2005-001.warc.gz") < data.index(
        b"example-2005-002.warc.gz"
    )

    parsed = parse_manifest(data)
    assert [item.key for item in parsed.collections["2005"].warcs] == [
        "example-2005-001.warc.gz",
        "example-2005-002.warc.gz",
    ]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda body: body | {"schema_version": 1}, "published_at and collections"),
        (lambda body: body | {"published_at": "not-a-time"}, "UTC timestamp"),
        (
            lambda body: body | {"collections": {"../bad": {}}},
            "unsafe collection ID",
        ),
    ],
)
def test_manifest_rejects_invalid_structure(mutate, message):
    payload = mutate(json.loads(manifest_bytes()))
    with pytest.raises(ValueError, match=message):
        parse_manifest(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("key", "../escape.warc.gz", "unsafe manifest artifact key"),
        ("key", "wrong.cdxj", "unexpected manifest artifact type"),
        ("sha256", "bad", "invalid SHA-256"),
        ("size_bytes", 0, "invalid size"),
    ],
)
def test_manifest_rejects_invalid_warc_artifacts(field, value, message):
    payload = json.loads(manifest_bytes())
    payload["collections"]["2005"]["warcs"][0][field] = value
    with pytest.raises(ValueError, match=message):
        parse_manifest(json.dumps(payload).encode())
