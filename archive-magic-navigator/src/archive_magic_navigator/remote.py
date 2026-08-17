"""Private S3 manifest synchronization and local CDXJ caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path, PurePosixPath

import boto3
from archive_magic_format import (
    MANIFEST_NAME,
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest as parse_collections_manifest,
)
from botocore.exceptions import ClientError

from .settings import RemoteSource

from .collections import (
    Archive,
    ReplayCollection,
    validate_archive_id,
)
from .errors import ValidationError

_CDX_TIMESTAMP = re.compile(r"^\d{14}$")


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
        self._states: dict[str, tuple[CollectionsManifest, str]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def load_archive(self, archive_id: str) -> Archive:
        archive_id = validate_archive_id(archive_id)
        try:
            manifest, etag = self._get_manifest()
            archive = self._accept_manifest(archive_id, manifest)
            self._states[archive_id] = (manifest, etag)
            self._write_cached_manifest(archive_id, manifest)
            return archive
        except Exception as error:
            cached = self._read_cached_manifest(archive_id)
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
            archive = self._archive_from_manifest(archive_id, cached)
            self._validate_cached_indexes(archive_id, cached)
            self._states[archive_id] = (cached, "")
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
        current, etag = self._states[archive_id]
        try:
            manifest, new_etag = self._get_manifest(if_none_match=etag or None)
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code", "")) in {
                "304",
                "NotModified",
            }:
                return
            raise
        old_ids = set(current.collections)
        new_ids = set(manifest.collections)
        if old_ids != new_ids:
            print(
                f"WARNING: collection membership changed for {archive_id}; "
                "restart Navigator to apply it",
                file=sys.stderr,
            )
        for collection_id in sorted(old_ids & new_ids):
            old = current.collections[collection_id]
            new = manifest.collections[collection_id]
            if old.index != new.index:
                self._sync_index(archive_id, collection_id, new)
        accepted = CollectionsManifest(
            manifest.published_at,
            {
                collection_id: manifest.collections[collection_id]
                for collection_id in current.collections
                if collection_id in manifest.collections
            }
            | {
                collection_id: current.collections[collection_id]
                for collection_id in current.collections
                if collection_id not in manifest.collections
            },
        )
        self._states[archive_id] = (accepted, new_etag)
        self._write_cached_manifest(archive_id, accepted)

    def _get_manifest(self, *, if_none_match: str | None = None):
        params = {"Bucket": self.config.bucket, "Key": self._object_key(MANIFEST_NAME)}
        if if_none_match:
            params["IfNoneMatch"] = if_none_match
        response = self.client.get_object(**params)
        try:
            return parse_manifest(response["Body"].read()), response["ETag"]
        finally:
            response["Body"].close()

    def _accept_manifest(self, archive_id: str, manifest: CollectionsManifest) -> Archive:
        staged: list[tuple[Path, Path]] = []
        try:
            for collection_id, collection in manifest.collections.items():
                item = self._stage_index(archive_id, collection_id, collection)
                if item is not None:
                    staged.append(item)
            for destination, temporary in staged:
                os.replace(temporary, destination)
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)
        return self._archive_from_manifest(archive_id, manifest)

    def _sync_index(
        self, archive_id: str, collection_id: str, collection: ManifestCollection
    ) -> None:
        staged = self._stage_index(archive_id, collection_id, collection)
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
        collection: ManifestCollection,
    ) -> tuple[Path, Path] | None:
        destination = self._index_path(archive_id, collection_id, collection.index)
        if (
            destination.is_file()
            and destination.stat().st_size == collection.index.size_bytes
            and _sha256(destination) == collection.index.sha256
        ):
            _validate_index(destination, collection)
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
            tmp.write_bytes(data)
            if (
                len(data) != collection.index.size_bytes
                or hashlib.sha256(data).hexdigest() != collection.index.sha256
            ):
                raise ValidationError(
                    f"downloaded index does not match manifest: {collection.index.key}"
                )
            _validate_index(tmp, collection)
            return destination, tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            response["Body"].close()

    def _archive_from_manifest(
        self, archive_id: str, manifest: CollectionsManifest
    ) -> Archive:
        root = (self.cache_directory / archive_id).resolve()
        collections = tuple(
            ReplayCollection(
                collection_id,
                root,
                self._index_path(archive_id, collection_id, collection.index).resolve(),
                self._archive_path(),
            )
            for collection_id, collection in sorted(manifest.collections.items())
        )
        if not collections:
            raise ValidationError(
                f"remote archive {archive_id!r} has no playable collections"
            )
        return Archive(archive_id, root, collections)

    def _validate_cached_indexes(
        self, archive_id: str, manifest: CollectionsManifest
    ) -> None:
        for collection_id, collection in manifest.collections.items():
            path = self._index_path(archive_id, collection_id, collection.index)
            if (
                not path.is_file()
                or path.stat().st_size != collection.index.size_bytes
                or _sha256(path) != collection.index.sha256
            ):
                raise ValidationError(f"cached index is missing or invalid: {path}")
            _validate_index(path, collection)

    def _write_cached_manifest(self, archive_id: str, manifest: CollectionsManifest) -> None:
        path = self.cache_directory / archive_id / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        data = manifest.to_bytes()
        fd, name = tempfile.mkstemp(prefix=".tmp-manifest-", dir=path.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _read_cached_manifest(self, archive_id: str) -> CollectionsManifest | None:
        path = self.cache_directory / archive_id / MANIFEST_NAME
        return parse_manifest(path.read_bytes()) if path.is_file() else None

    def _index_path(
        self, archive_id: str, collection_id: str, artifact: ManifestArtifact
    ) -> Path:
        return (
            self.cache_directory
            / archive_id
            / PurePosixPath(artifact.key).name
        )

    def _archive_path(self) -> str:
        key = self._object_key("").rstrip("/")
        return f"s3://{self.config.bucket}/{key + '/' if key else ''}"

    def _object_key(self, relative: str) -> str:
        return "/".join(part for part in (self.config.prefix, relative) if part)


def parse_manifest(data: bytes) -> CollectionsManifest:
    try:
        return parse_collections_manifest(data)
    except ValueError as error:
        raise ValidationError(str(error)) from error


def _validate_index(path: Path, collection: ManifestCollection) -> None:
    sizes = {PurePosixPath(item.key).name: item.size_bytes for item in collection.warcs}
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
                or filename not in sizes
            ):
                raise ValidationError(
                    f"{path}, line {number}: unknown WARC {filename!r}"
                )
            if offset < 0 or length <= 0 or offset + length > sizes[filename]:
                raise ValidationError(
                    f"{path}, line {number}: WARC range out of bounds"
                )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
