"""Private S3 index synchronization and local CDXJ caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import boto3
from botocore.config import Config

from .collections import (
    Archive,
    ReplayCollection,
    validate_archive_id,
    validate_collection_id,
)
from .errors import ValidationError
from .settings import RemoteSource
from .store import IndexStore, publish_indexes

_CDX_TIMESTAMP = re.compile(r"^\d{14}$")
_INDEX_SUFFIX = "-index.cdxj"


class LegacyLayoutError(ValidationError):
    """Requires explicit migration; never masked by a cached index."""


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size_bytes: int
    etag: str


@dataclass(frozen=True)
class RemoteCollection:
    collection_id: str
    index: RemoteObject


class RemoteArchiveStore(IndexStore):
    """Own validated cached indexes for one Navigator process."""

    def __init__(
        self, config: RemoteSource, cache_directory: Path, poll_seconds: float
    ) -> None:
        super().__init__(cache_directory / config.cache_key, poll_seconds)
        self.config = config
        self.client = boto3.client(
            "s3", endpoint_url=config.endpoint_url, region_name=config.region,
            config=Config(connect_timeout=5, read_timeout=10,
                          retries={"mode": "standard", "total_max_attempts": 2})
        )
        self._states: dict[str, dict[str, RemoteCollection]] = {}
        self.stale = False

    def load_archive(self, archive_id: str) -> Archive:
        archive_id = validate_archive_id(archive_id)
        self.stale = False
        try:
            self.check_legacy_layout()
            inventory = self._list_inventory()
            collections = self._discover_collections(archive_id, inventory)
            archive = self._accept_collections(archive_id, collections, inventory)
            self._states[archive_id] = collections
            return archive
        except Exception as error:
            if isinstance(error, LegacyLayoutError):
                raise
            self.stale = True
            cached = self._read_cached_indexes(archive_id)
            if cached is None:
                if isinstance(error, ValidationError):
                    raise
                raise ValidationError(
                    f"cannot synchronize remote archive {archive_id!r}: {error}"
                ) from error
            print(
                f"WARNING: using cached index for {archive_id}: {error}",
                file=sys.stderr,
            )
            archive = self._archive_from_cached(archive_id, cached)
            self._validate_cached_indexes(archive_id, cached)
            snapshot = archive.replay_index
            assert snapshot is not None
            if snapshot.exists():
                # Annual cache files can advance before a failed snapshot publish.
                # The merged file is the last version accepted for playback.
                _validate_index(snapshot)
            else:
                # Support caches created before merged replay indexes existed.
                publish_indexes(archive, [])
            self._states[archive_id] = cached
            return archive

    def child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.config.endpoint_url is not None:
            environment["AWS_ENDPOINT_URL_S3"] = self.config.endpoint_url
        environment["AWS_REGION"] = self.config.region
        environment["AWS_DEFAULT_REGION"] = self.config.region
        return environment

    def _poll_archive(self, archive_id: str) -> None:
        self.check_legacy_layout()
        current = self._states[archive_id]
        inventory = self._list_inventory()
        discovered = self._discover_collections(archive_id, inventory)
        # A missing object may be a transient/incomplete publication. Never remove
        # a previously accepted annual collection during a running session.
        accepted = current | discovered
        changed = {
            key: item
            for key, item in discovered.items()
            if current.get(key) != item
        }
        if changed:
            self._accept_collections(archive_id, accepted, inventory, changed=changed)
            self._states[archive_id] = accepted
        self.stale = False

    def _accept_collections(
        self,
        archive_id: str,
        collections: dict[str, RemoteCollection],
        inventory: dict[str, RemoteObject],
        *,
        changed: dict[str, RemoteCollection] | None = None,
    ) -> Archive:
        staged: list[tuple[Path, Path]] = []
        try:
            for collection_id, collection in (
                collections if changed is None else changed
            ).items():
                staged.append(
                    self._stage_index(archive_id, collection_id, collection, inventory)
                )
            archive = self._archive_from_collections(archive_id, collections)
            publish_indexes(archive, staged)
            return archive
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)

    def _stage_index(
        self,
        archive_id: str,
        collection_id: str,
        collection: RemoteCollection,
        inventory: dict[str, RemoteObject],
    ) -> tuple[Path, Path]:
        destination = self._index_path(archive_id, collection_id, collection.index.key)
        response = self.client.get_object(
            Bucket=self.config.bucket,
            Key=self._object_key(collection.index.key),
            IfMatch=collection.index.etag,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".tmp-index-", dir=destination.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            data = response["Body"].read()
            metadata = response.get("Metadata", {})
            metadata = {key.lower(): value for key, value in metadata.items()}
            tmp.write_bytes(data)
            if len(data) != collection.index.size_bytes:
                raise ValidationError(
                    f"downloaded index size mismatch: {collection.index.key}"
                )
            declared = metadata.get("sha256")
            if declared is not None:
                digest = hashlib.sha256(data).hexdigest()
                if digest != declared:
                    raise ValidationError(
                        f"downloaded index does not match metadata: {collection.index.key}"
                    )
            _validate_index(tmp, inventory)
            return destination, tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            response["Body"].close()

    def _archive_from_collections(
        self, archive_id: str, collections: dict[str, RemoteCollection]
    ) -> Archive:
        root = (self.cache_directory / archive_id).resolve()
        replay_collections = tuple(
            ReplayCollection(
                collection_id,
                root,
                self._index_path(
                    archive_id, collection_id, collection.index.key
                ).resolve(),
                self._archive_path(),
            )
            for collection_id, collection in sorted(collections.items())
        )
        if not replay_collections:
            raise ValidationError(
                f"remote archive {archive_id!r} has no playable collections"
            )
        return Archive(archive_id, root, replay_collections, root / ".replay.cdxj")

    def _archive_from_cached(
        self, archive_id: str, collections: dict[str, RemoteCollection]
    ) -> Archive:
        return self._archive_from_collections(archive_id, collections)

    def _validate_cached_indexes(
        self, archive_id: str, collections: dict[str, RemoteCollection]
    ) -> None:
        for collection_id, collection in collections.items():
            path = self._index_path(archive_id, collection_id, collection.index.key)
            if (
                not path.is_file()
                or path.stat().st_size != collection.index.size_bytes
            ):
                raise ValidationError(f"cached index is missing or invalid: {path}")
            _validate_index(path)

    def _read_cached_indexes(
        self, archive_id: str
    ) -> dict[str, RemoteCollection] | None:
        root = self.cache_directory / archive_id
        if not root.is_dir():
            return None
        collections: dict[str, RemoteCollection] = {}
        pattern = f"{archive_id}-*{_INDEX_SUFFIX}"
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            prefix, suffix = f"{archive_id}-", _INDEX_SUFFIX
            collection_id = validate_collection_id(
                path.name[len(prefix) : -len(suffix)]
            )
            collections[collection_id] = RemoteCollection(
                collection_id,
                RemoteObject(
                    key=path.name,
                    size_bytes=path.stat().st_size,
                    etag="",
                ),
            )
        return collections or None

    def _discover_collections(
        self, archive_id: str, inventory: dict[str, RemoteObject]
    ) -> dict[str, RemoteCollection]:
        collections: dict[str, RemoteCollection] = {}
        prefix = f"{archive_id}-"
        for key, item in inventory.items():
            name = PurePosixPath(key).name
            if not name.endswith(_INDEX_SUFFIX) or not name.startswith(prefix):
                continue
            collection_id = validate_collection_id(
                name[len(prefix) : -len(_INDEX_SUFFIX)]
            )
            collections[collection_id] = RemoteCollection(collection_id, item)
        return collections

    def check_legacy_layout(self) -> None:
        prefix = self.config.key("")
        if prefix:
            prefix += "/"
        token = None
        while True:
            kwargs = {"Bucket": self.config.bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            result = self.client.list_objects_v2(**kwargs)
            if any(_is_archive_object(_relative_key(prefix, item["Key"]) or "") for item in result.get("Contents", [])):
                raise LegacyLayoutError("legacy flat archive data: copy and verify WARC/CDXJ files under data/, then explicitly remove the old root objects before using Navigator")
            if not result.get("IsTruncated"):
                return
            token = result["NextContinuationToken"]

    def _list_inventory(self) -> dict[str, RemoteObject]:
        inventory: dict[str, RemoteObject] = {}
        prefix = self._object_key("")
        token = None
        while True:
            kwargs = {"Bucket": self.config.bucket, "Prefix": prefix}
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for entry in response.get("Contents", []):
                relative = _relative_key(prefix, entry["Key"])
                if relative is None or not _is_archive_object(relative):
                    continue
                inventory[relative] = RemoteObject(
                    key=relative,
                    size_bytes=int(entry["Size"]),
                    etag=entry["ETag"],
                )
            if not response.get("IsTruncated"):
                return inventory
            token = response["NextContinuationToken"]

    def _index_path(
        self, archive_id: str, collection_id: str, index_key: str
    ) -> Path:
        return self.cache_directory / archive_id / PurePosixPath(index_key).name

    def _archive_path(self) -> str:
        key = self._object_key("").rstrip("/")
        return f"s3://{self.config.bucket}/{key + '/' if key else ''}"

    def _object_key(self, relative: str) -> str:
        return self.config.key("data/" + relative)


def _validate_index(
    path: Path,
    inventory: dict[str, RemoteObject] | None = None,
) -> None:
    warc_sizes: dict[str, int] = {}
    if inventory is not None:
        warc_sizes = {
            PurePosixPath(item.key).name: item.size_bytes
            for item in inventory.values()
            if item.key.endswith(".warc.gz")
        }
    previous = None
    records = 0
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            parts = line.rstrip("\n").split(" ", 2)
            if (
                len(parts) != 3
                or not parts[0]
                or not _CDX_TIMESTAMP.fullmatch(parts[1])
            ):
                raise ValidationError(f"{path}, line {number}: malformed CDXJ")
            try:
                datetime.strptime(parts[1], "%Y%m%d%H%M%S")
            except ValueError as error:
                raise ValidationError(f"{path}, line {number}: invalid timestamp") from error
            current = (parts[0], parts[1])
            if previous is not None and current < previous:
                raise ValidationError(f"{path}, line {number}: CDXJ is not sorted")
            previous = current
            records += 1
            try:
                payload = json.loads(parts[2])
                filename = payload["filename"]
                offset = int(payload["offset"])
                length = int(payload["length"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValidationError(
                    f"{path}, line {number}: invalid CDXJ locator"
                ) from error
            if (
                not isinstance(filename, str)
                or PurePosixPath(filename).name != filename
            ):
                raise ValidationError(
                    f"{path}, line {number}: unknown WARC {filename!r}"
                )
            if offset < 0 or length <= 0:
                raise ValidationError(f"{path}, line {number}: invalid WARC range")
            if inventory is not None:
                size = warc_sizes.get(filename)
                if size is None:
                    raise ValidationError(
                        f"{path}, line {number}: unknown WARC {filename!r}"
                    )
                if offset < 0 or length <= 0 or offset + length > size:
                    raise ValidationError(
                        f"{path}, line {number}: WARC range out of bounds"
                    )

    if not records:
        raise ValidationError(f"{path}: replay index is empty")


def _is_archive_object(relative: str) -> bool:
    name = PurePosixPath(relative).name
    return relative == name and (
        name.endswith(".warc.gz") or name.endswith(".cdxj")
    )


def _relative_key(prefix: str, key: str) -> str | None:
    normalized = prefix.rstrip("/")
    if normalized and not key.startswith(normalized + "/") and key != normalized:
        return None
    if normalized:
        return key[len(normalized) + 1 :]
    return key
