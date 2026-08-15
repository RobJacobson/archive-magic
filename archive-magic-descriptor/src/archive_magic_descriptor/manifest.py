"""Read and write the per-archive collection publication manifest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

MANIFEST_NAME = "collections-manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class ManifestArtifact:
    key: str
    etag: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "etag": self.etag,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class ManifestCollection:
    updated_at: str
    index: ManifestArtifact
    warcs: tuple[ManifestArtifact, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "updated_at": self.updated_at,
            "index": self.index.to_dict(),
            "warcs": [
                item.to_dict() for item in sorted(self.warcs, key=lambda item: item.key)
            ],
        }


@dataclass(frozen=True)
class CollectionsManifest:
    published_at: str
    collections: dict[str, ManifestCollection]

    def to_bytes(self) -> bytes:
        payload = {
            "published_at": self.published_at,
            "collections": {
                key: self.collections[key].to_dict() for key in sorted(self.collections)
            },
        }
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )


def parse_manifest(data: bytes) -> CollectionsManifest:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid collections manifest JSON: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"published_at", "collections"}:
        raise ValueError(
            "collections manifest must contain published_at and collections"
        )
    published_at = _timestamp(payload["published_at"], "published_at")
    raw_collections = payload["collections"]
    if not isinstance(raw_collections, dict):
        raise ValueError("collections must be an object")  # noqa: TRY004
    collections: dict[str, ManifestCollection] = {}
    for collection_id, raw in raw_collections.items():
        if not isinstance(collection_id, str) or not _SAFE_ID.fullmatch(collection_id):
            raise ValueError(f"unsafe collection ID in manifest: {collection_id!r}")
        if not isinstance(raw, dict) or set(raw) != {"updated_at", "index", "warcs"}:
            raise ValueError(f"invalid manifest collection: {collection_id}")
        warcs = raw["warcs"]
        if not isinstance(warcs, list) or not warcs:
            raise ValueError(f"manifest collection {collection_id} has no WARCs")
        parsed_warcs = tuple(
            _artifact(item, collection_id, warc=True) for item in warcs
        )
        if len({item.key for item in parsed_warcs}) != len(parsed_warcs):
            raise ValueError(f"duplicate WARC key in collection {collection_id}")
        collections[collection_id] = ManifestCollection(
            updated_at=_timestamp(
                raw["updated_at"], f"collections.{collection_id}.updated_at"
            ),
            index=_artifact(raw["index"], collection_id, warc=False),
            warcs=tuple(sorted(parsed_warcs, key=lambda item: item.key)),
        )
    return CollectionsManifest(published_at=published_at, collections=collections)


def _artifact(value: object, collection_id: str, *, warc: bool) -> ManifestArtifact:
    if not isinstance(value, dict) or set(value) != {
        "key",
        "etag",
        "sha256",
        "size_bytes",
    }:
        raise ValueError("invalid manifest artifact")
    key = _text(value["key"], "artifact.key")
    path = PurePosixPath(key)
    expected = PurePosixPath("collections") / collection_id
    if path.is_absolute() or ".." in path.parts or path.parent != expected:
        raise ValueError(f"unsafe manifest artifact key: {key}")
    if warc != key.endswith(".warc.gz"):
        raise ValueError(f"unexpected manifest artifact type: {key}")
    digest = _text(value["sha256"], "artifact.sha256")
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"invalid SHA-256 for {key}")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"invalid size for {key}")
    return ManifestArtifact(
        key=key,
        etag=_text(value["etag"], "artifact.etag"),
        sha256=digest,
        size_bytes=size,
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    if not _TIMESTAMP.fullmatch(text):
        raise ValueError(f"{label} must be a UTC timestamp ending in Z")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError as error:
        raise ValueError(f"{label} is not a valid timestamp") from error
    return text
