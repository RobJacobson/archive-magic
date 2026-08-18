"""Local transformation and remote-authoritative publication helpers."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import boto3
from botocore.exceptions import ClientError

from .collection import (
    ArchiveLayout,
    exclusive_temp_path,
    file_sha256,
    list_collection_warcs,
    publish_file_atomically,
)
from .config import FetchOutput
from .index import parse_cdxj_line


@dataclass(frozen=True)
class LocalArtifact:
    path: Path
    key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size_bytes: int
    etag: str
    sha256: str | None


class PublicationManager:
    """Materialize remote work and publish completed local transformations."""

    def __init__(self, config: FetchOutput) -> None:
        self.config = config
        self.inventory: dict[str, RemoteObject] = {}
        self.client = None
        if config.type == "remote":
            self.client = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url,
                region_name=config.region,
            )

    def prepare(self, layout: ArchiveLayout) -> None:
        """Load committed remote object metadata without mirroring artifacts."""

        if self.config.type == "local":
            return
        self.inventory = self._list_inventory()

    def materialize_index(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Download the committed CDXJ when no local copy exists."""

        collection_id = layout.validate_collection_id(collection_id)
        if self.config.type == "local":
            return
        index_key = layout.index_filename(collection_id)
        if index_key not in self.inventory:
            return
        index_path = layout.collection_index(collection_id)
        if index_path.is_file():
            return
        layout.collection_dir(collection_id).mkdir(parents=True, exist_ok=True)
        self._download_object(index_key, index_path)

    def materialize_tail(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Download the committed final WARC when no usable local tail exists."""

        collection_id = layout.validate_collection_id(collection_id)
        if self.config.type == "local":
            return
        index_path = layout.collection_index(collection_id)
        if not index_path.is_file():
            self.materialize_index(layout, collection_id)
        if not index_path.is_file():
            return
        tail_name = _cdxj_tail_filename(index_path, layout.archive_id, collection_id)
        if tail_name is None:
            return
        remote = self.inventory.get(tail_name)
        if remote is None:
            return
        tail_path = layout.root / tail_name
        if tail_path.is_file() and tail_path.stat().st_size >= remote.size_bytes:
            return
        if tail_path.is_file():
            tail_path.unlink()
        layout.collection_dir(collection_id).mkdir(parents=True, exist_ok=True)
        self._download_object(tail_name, tail_path)

    def collection_warc_sizes(
        self,
        layout: ArchiveLayout,
        collection_id: str,
    ) -> dict[str, int]:
        """Return committed WARC sizes overlaid with local working updates."""

        collection_id = layout.validate_collection_id(collection_id)
        sizes = _inventory_warc_sizes(layout, collection_id, self.inventory)
        index_path = layout.collection_index(collection_id)
        if index_path.is_file():
            for name in _cdxj_warc_filenames(index_path):
                if name not in sizes and (layout.root / name).is_file():
                    sizes[name] = (layout.root / name).stat().st_size
        for path in list_collection_warcs(layout, collection_id):
            sizes[path.name] = path.stat().st_size
        return sizes

    def publish_collection(
        self,
        layout: ArchiveLayout,
        collection_id: str,
        *,
        reset: bool = False,
    ) -> bool:
        """Publish local working WARCs, then atomically replace the CDXJ."""

        collection_id = layout.validate_collection_id(collection_id)
        index_path = layout.collection_index(collection_id)
        warc_paths = list_collection_warcs(layout, collection_id)
        if not index_path.is_file() or not warc_paths:
            return False

        local_warcs = [_local_artifact(layout, path) for path in warc_paths]
        index_local = _local_artifact(layout, index_path)
        if self.config.type == "local":
            return False

        changed_warcs = (
            local_warcs
            if reset
            else [item for item in local_warcs if self._remote_changed(item)]
        )
        index_changed = reset or self._remote_changed(index_local)
        if not changed_warcs and not index_changed:
            return False

        for item in changed_warcs:
            with item.path.open("rb") as body:
                self._put(item.key, body, item.sha256)
            self.inventory[item.key] = RemoteObject(
                key=item.key,
                size_bytes=item.size_bytes,
                etag=self._head_etag(item.key),
                sha256=item.sha256,
            )

        if index_changed:
            with index_path.open("rb") as body:
                response = self._put(
                    index_local.key,
                    body,
                    index_local.sha256,
                    content_type="application/x-cdxj",
                )
            self.inventory[index_local.key] = RemoteObject(
                key=index_local.key,
                size_bytes=index_local.size_bytes,
                etag=response["ETag"],
                sha256=index_local.sha256,
            )

        return True

    def evict_collection(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Remove remote-output working copies after a confirmed commit."""

        if self.config.type != "remote":
            return
        for path in list_collection_warcs(layout, collection_id):
            path.unlink()
        layout.collection_index(collection_id).unlink(missing_ok=True)

    def reset_archive(self, layout: ArchiveLayout) -> None:
        """Delete exactly this archive prefix and clear its local data directory."""

        if self.config.type != "remote":
            raise ValueError("remote archive reset requires remote output")
        for key in self._iter_keys():
            self.client.delete_object(Bucket=self._remote().bucket, Key=key)
        if layout.root.exists():
            shutil.rmtree(layout.root)
        layout.root.mkdir(parents=True, exist_ok=True)
        self.inventory = {}

    def _remote_changed(self, item: LocalArtifact) -> bool:
        remote = self.inventory.get(item.key)
        if remote is None:
            return True
        if remote.size_bytes != item.size_bytes:
            return True
        if remote.sha256 == item.sha256:
            return False
        head_digest = self._head_sha256(item.key)
        return head_digest != item.sha256

    def _list_inventory(self) -> dict[str, RemoteObject]:
        inventory: dict[str, RemoteObject] = {}
        prefix = self._key("")
        token = None
        while True:
            kwargs = {"Bucket": self._remote().bucket, "Prefix": prefix}
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for entry in response.get("Contents", []):
                relative = _relative_key(prefix, entry["Key"])
                if relative is None or not _is_archive_object(relative):
                    continue
                metadata = self._head_metadata(relative)
                inventory[relative] = RemoteObject(
                    key=relative,
                    size_bytes=int(entry["Size"]),
                    etag=entry["ETag"],
                    sha256=metadata.get("sha256"),
                )
            if not response.get("IsTruncated"):
                return inventory
            token = response["NextContinuationToken"]

    def _download_object(self, relative_key: str, destination: Path) -> None:
        data = self._read_object(relative_key)
        remote = self.inventory.get(relative_key)
        if remote is not None:
            if len(data) != remote.size_bytes:
                raise ValueError(
                    f"downloaded artifact failed validation: {relative_key}"
                )
            if remote.sha256 is not None:
                digest = hashlib.sha256(data).hexdigest()
                if digest != remote.sha256:
                    raise ValueError(
                        f"downloaded artifact failed validation: {relative_key}"
                    )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = exclusive_temp_path(destination.parent, suffix=destination.suffix)
        try:
            temporary.write_bytes(data)
            publish_file_atomically(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _read_object(self, relative_key: str) -> bytes:
        response = self.client.get_object(
            Bucket=self._remote().bucket,
            Key=self._key(relative_key),
        )
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def _put(
        self,
        relative_key: str,
        body,
        digest: str,
        *,
        content_type: str = "application/warc",
    ):
        return self.client.put_object(
            Bucket=self._remote().bucket,
            Key=self._key(relative_key),
            Body=body,
            Metadata={"sha256": digest},
            ContentType=content_type,
        )

    def _head_sha256(self, relative_key: str) -> str | None:
        return self._head_metadata(relative_key).get("sha256")

    def _head_etag(self, relative_key: str) -> str:
        response = self.client.head_object(
            Bucket=self._remote().bucket,
            Key=self._key(relative_key),
        )
        return response["ETag"]

    def _head_metadata(self, relative_key: str) -> dict[str, str]:
        try:
            response = self.client.head_object(
                Bucket=self._remote().bucket,
                Key=self._key(relative_key),
            )
        except ClientError as error:
            if _not_found(error):
                return {}
            raise
        metadata = response.get("Metadata", {})
        return {key.lower(): value for key, value in metadata.items()}

    def _iter_keys(self):
        token = None
        while True:
            kwargs = {"Bucket": self._remote().bucket, "Prefix": self._key("")}
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                yield item["Key"]
            if not response.get("IsTruncated"):
                return
            token = response["NextContinuationToken"]

    def _key(self, relative_key: str) -> str:
        prefix = self._remote().prefix.strip("/")
        relative_key = relative_key.lstrip("/")
        if prefix and relative_key:
            return f"{prefix}/{relative_key}"
        if prefix:
            return prefix + "/"
        return relative_key

    def _remote(self):
        assert self.config.type == "remote"
        assert self.config.bucket is not None
        return self.config


def _local_artifact(layout: ArchiveLayout, path: Path) -> LocalArtifact:
    return LocalArtifact(
        path=path,
        key=path.relative_to(layout.root).as_posix(),
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
    )


def _inventory_warc_sizes(
    layout: ArchiveLayout,
    collection_id: str,
    inventory: dict[str, RemoteObject],
) -> dict[str, int]:
    prefix = f"{layout.archive_id}-{collection_id}-"
    return {
        Path(item.key).name: item.size_bytes
        for item in inventory.values()
        if item.key.endswith(".warc.gz") and Path(item.key).name.startswith(prefix)
    }


def _cdxj_warc_filenames(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            filename = parse_cdxj_line(line)[2]["filename"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(filename, str):
            names.add(filename)
    return names


def _cdxj_tail_filename(
    path: Path, archive_id: str, collection_id: str
) -> str | None:
    pattern = re.compile(
        rf"{re.escape(archive_id)}-{re.escape(collection_id)}-(\d{{3,}})\.warc\.gz"
    )
    tail: str | None = None
    max_sequence = -1
    for name in _cdxj_warc_filenames(path):
        match = pattern.fullmatch(name)
        if match is None:
            continue
        sequence = int(match.group(1))
        if sequence > max_sequence:
            max_sequence = sequence
            tail = name
    return tail


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


def _not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "404",
        "NoSuchKey",
        "NotFound",
    }

