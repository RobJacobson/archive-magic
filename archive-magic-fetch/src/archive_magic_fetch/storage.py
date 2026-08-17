"""Local transformation and remote-authoritative publication helpers."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
from archive_magic_format import (
    MANIFEST_NAME,
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)
from botocore.exceptions import ClientError

from .collection import (
    ArchiveLayout,
    exclusive_temp_path,
    file_sha256,
    list_collection_warcs,
    publish_file_atomically,
)
from .config import FetchOutput


@dataclass(frozen=True)
class LocalArtifact:
    path: Path
    key: str
    sha256: str
    size_bytes: int


class PublicationManager:
    """Materialize remote work and publish completed local transformations."""

    def __init__(self, config: FetchOutput) -> None:
        self.config = config
        self.manifest = CollectionsManifest("", {})
        self.client = None
        if config.type == "remote":
            self.client = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url,
                region_name=config.region,
            )

    def prepare(self, layout: ArchiveLayout) -> None:
        """Load committed publication state without mirroring its artifacts."""

        if self.config.type == "local":
            path = layout.root / MANIFEST_NAME
            if path.is_file():
                self.manifest = parse_manifest(path.read_bytes())
            return

        try:
            response = self.client.get_object(
                Bucket=self._remote().bucket,
                Key=self._key(MANIFEST_NAME),
            )
        except ClientError as error:
            if not _not_found(error):
                raise
            self.manifest = CollectionsManifest("", {})
            (layout.root / MANIFEST_NAME).unlink(missing_ok=True)
            return

        body = response["Body"]
        try:
            self.manifest = parse_manifest(body.read())
        finally:
            body.close()
        self._write_manifest(layout)

    def materialize_index(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Download the committed CDXJ when no local copy exists."""

        collection_id = layout.validate_collection_id(collection_id)
        if self.config.type == "local":
            return
        previous = self.manifest.collections.get(collection_id)
        if previous is None:
            return
        index_path = layout.root / previous.index.key
        if index_path.is_file():
            return
        layout.collection_dir(collection_id).mkdir(parents=True, exist_ok=True)
        self._download_artifact(previous.index, index_path)

    def materialize_tail(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Download the committed final WARC when no usable local tail exists."""

        collection_id = layout.validate_collection_id(collection_id)
        if self.config.type == "local":
            return
        previous = self.manifest.collections.get(collection_id)
        if previous is None:
            return
        final = previous.warcs[-1]
        final_path = layout.root / final.key
        if final_path.is_file() and final_path.stat().st_size >= final.size_bytes:
            return
        if final_path.is_file():
            final_path.unlink()
        layout.collection_dir(collection_id).mkdir(parents=True, exist_ok=True)
        self._download_artifact(final, final_path)

    def collection_warc_sizes(
        self,
        layout: ArchiveLayout,
        collection_id: str,
    ) -> dict[str, int]:
        """Return committed WARC sizes overlaid with local working updates."""

        previous = self.manifest.collections.get(collection_id)
        sizes = {
            Path(item.key).name: item.size_bytes
            for item in (() if previous is None else previous.warcs)
        }
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
        """Publish local working WARCs, the stable index, then the manifest."""

        collection_id = layout.validate_collection_id(collection_id)
        index_path = layout.collection_index(collection_id)
        warc_paths = list_collection_warcs(layout, collection_id)
        if not index_path.is_file() or not warc_paths:
            return False

        previous = self.manifest.collections.get(collection_id)
        previous_by_key = (
            {} if previous is None else {item.key: item for item in previous.warcs}
        )
        local_warcs = [_local_artifact(layout, path) for path in warc_paths]
        changed = (
            local_warcs
            if reset
            else [
                item
                for item in local_warcs
                if (old := previous_by_key.get(item.key)) is None
                or not _same(old, item)
            ]
        )
        index_local = _local_artifact(layout, index_path)
        index_changed = previous is None or not _same(previous.index, index_local)
        if not changed and not index_changed and not reset:
            return False

        published = {} if reset else dict(previous_by_key)
        if self.config.type == "local":
            for item in changed:
                published[item.key] = _manifest_artifact(
                    item, _local_etag(item.sha256)
                )
            index = (
                previous.index
                if not index_changed and previous is not None
                else _manifest_artifact(index_local, _local_etag(index_local.sha256))
            )
        else:
            for item in changed:
                with item.path.open("rb") as body:
                    response = self._put(item.key, body, item.sha256)
                published[item.key] = _manifest_artifact(item, response["ETag"])
            if index_changed:
                with index_path.open("rb") as body:
                    response = self._put(
                        index_local.key,
                        body,
                        index_local.sha256,
                        content_type="application/x-cdxj",
                    )
                index = _manifest_artifact(index_local, response["ETag"])
            else:
                assert previous is not None
                index = previous.index

        collection = ManifestCollection(
            updated_at=_now(),
            index=index,
            warcs=tuple(sorted(published.values(), key=lambda item: item.key)),
        )
        next_manifest = self._manifest_with(collection_id, collection)

        if self.config.type == "local":
            self.manifest = next_manifest
            self._write_manifest(layout)
            return True

        manifest_data = next_manifest.to_bytes()
        self._put(
            MANIFEST_NAME,
            manifest_data,
            hashlib.sha256(manifest_data).hexdigest(),
            content_type="application/json",
        )
        self.manifest = next_manifest
        self._write_manifest(layout)
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
        self.manifest = CollectionsManifest("", {})

    def _manifest_with(
        self,
        collection_id: str,
        collection: ManifestCollection,
    ) -> CollectionsManifest:
        collections = dict(self.manifest.collections)
        collections[collection_id] = collection
        return CollectionsManifest(_now(), collections)

    def _write_manifest(self, layout: ArchiveLayout) -> None:
        if not self.manifest.collections:
            return
        destination = layout.root / MANIFEST_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = exclusive_temp_path(destination.parent, suffix=".json")
        try:
            temporary.write_bytes(self.manifest.to_bytes())
            publish_file_atomically(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _download_artifact(
        self,
        artifact: ManifestArtifact,
        destination: Path,
    ) -> None:
        data = self._read_object(artifact.key)
        if (
            len(data) != artifact.size_bytes
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise ValueError(f"downloaded artifact failed validation: {artifact.key}")
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


def _manifest_artifact(item: LocalArtifact, etag: str) -> ManifestArtifact:
    return ManifestArtifact(
        key=item.key,
        etag=etag,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
    )


def _same(artifact: ManifestArtifact, item: LocalArtifact) -> bool:
    return artifact.sha256 == item.sha256 and artifact.size_bytes == item.size_bytes


def _local_etag(digest: str) -> str:
    return f'"sha256:{digest}"'


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "404",
        "NoSuchKey",
        "NotFound",
    }
