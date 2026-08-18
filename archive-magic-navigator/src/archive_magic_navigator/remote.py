"""Private S3 index synchronization and local CDXJ caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import boto3
from botocore.exceptions import ClientError

from .collections import (
    Archive,
    ReplayCollection,
    validate_archive_id,
    validate_collection_id,
)
from .errors import ValidationError
from .settings import RemoteSource

_CDX_TIMESTAMP = re.compile(r"^\d{14}$")
_INDEX_SUFFIX = "-index.cdxj"


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size_bytes: int
    etag: str


@dataclass(frozen=True)
class RemoteCollection:
    collection_id: str
    index: RemoteObject


class RemoteArchiveStore:
    """Own validated cached indexes for one Navigator process."""

    def __init__(
        self, config: RemoteSource, cache_directory: Path, poll_seconds: float
    ) -> None:
        self.config = config
        self.cache_directory = cache_directory
        self.poll_seconds = poll_seconds
        self.client = boto3.client(
            "s3", endpoint_url=config.endpoint_url, region_name=config.region
        )
        self._states: dict[str, dict[str, RemoteCollection]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def load_archive(self, archive_id: str) -> Archive:
        archive_id = validate_archive_id(archive_id)
        try:
            inventory = self._list_inventory()
            collections = self._discover_collections(archive_id, inventory)
            archive = self._accept_collections(archive_id, collections, inventory)
            self._states[archive_id] = collections
            return archive
        except Exception as error:
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
            self._states[archive_id] = cached
            return archive

    def start_polling(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="archive-magic-index-sync"
        )
        self._thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(self.poll_seconds + 1, 5))

    def child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.config.endpoint_url is not None:
            environment["AWS_ENDPOINT_URL_S3"] = self.config.endpoint_url
        environment["AWS_REGION"] = self.config.region
        environment["AWS_DEFAULT_REGION"] = self.config.region
        return environment

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            for archive_id in tuple(self._states):
                try:
                    self._poll_archive(archive_id)
                except Exception as error:  # noqa: BLE001 - keep poller alive
                    print(
                        f"WARNING: index synchronization failed for {archive_id}: {error}",
                        file=sys.stderr,
                    )

    def _poll_archive(self, archive_id: str) -> None:
        current = self._states[archive_id]
        inventory = self._list_inventory()
        discovered = self._discover_collections(archive_id, inventory)
        old_ids = set(current)
        new_ids = set(discovered)
        if old_ids != new_ids:
            print(
                f"WARNING: collection membership changed for {archive_id}; "
                "restart Navigator to apply it",
                file=sys.stderr,
            )
        for collection_id in sorted(old_ids & new_ids):
            if current[collection_id].index.etag != discovered[collection_id].index.etag:
                self._sync_index(
                    archive_id,
                    collection_id,
                    discovered[collection_id],
                    inventory,
                )
        self._states[archive_id] = {
            collection_id: discovered[collection_id]
            for collection_id in current
            if collection_id in discovered
        } | {
            collection_id: current[collection_id]
            for collection_id in current
            if collection_id not in discovered
        }

    def _accept_collections(
        self,
        archive_id: str,
        collections: dict[str, RemoteCollection],
        inventory: dict[str, RemoteObject],
    ) -> Archive:
        staged: list[tuple[Path, Path]] = []
        try:
            for collection_id, collection in collections.items():
                item = self._stage_index(
                    archive_id,
                    collection_id,
                    collection,
                    inventory,
                )
                if item is not None:
                    staged.append(item)
            for destination, temporary in staged:
                os.replace(temporary, destination)
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)
        return self._archive_from_collections(archive_id, collections)

    def _sync_index(
        self,
        archive_id: str,
        collection_id: str,
        collection: RemoteCollection,
        inventory: dict[str, RemoteObject],
    ) -> None:
        staged = self._stage_index(
            archive_id,
            collection_id,
            collection,
            inventory,
            refresh=True,
        )
        if staged is not None:
            destination, temporary = staged
            try:
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _stage_index(
        self,
        archive_id: str,
        collection_id: str,
        collection: RemoteCollection,
        inventory: dict[str, RemoteObject],
        *,
        refresh: bool = False,
    ) -> tuple[Path, Path] | None:
        destination = self._index_path(archive_id, collection_id, collection.index.key)
        if (
            not refresh
            and destination.is_file()
            and destination.stat().st_size == collection.index.size_bytes
        ):
            _validate_index(destination, inventory)
            return None
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
        return Archive(archive_id, root, replay_collections)

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
        return "/".join(part for part in (self.config.prefix, relative) if part)


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
