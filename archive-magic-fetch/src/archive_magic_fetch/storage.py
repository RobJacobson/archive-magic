"""Local transformation and remote-authoritative publication helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from .collection import (
    ArchiveLayout,
    file_sha256,
    list_collection_warcs,
    publish_file_atomically,
)
from .config import StorageConfig
from .index import publish_collection_index
from .manifest import (
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)

MANIFEST_NAME = "collections-manifest.json"


@dataclass(frozen=True)
class LocalArtifact:
    path: Path
    key: str
    sha256: str
    size_bytes: int


class PublicationManager:
    """Materialize remote work and publish completed local transformations."""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        self.manifest = CollectionsManifest("", {})
        self.client = None
        if config.authority == "remote":
            remote = config.remote
            if remote is None:
                raise ValueError("remote authority requires storage.remote")
            self.client = boto3.client(
                "s3",
                endpoint_url=remote.endpoint_url,
                region_name=remote.region,
            )

    def prepare(self, layout: ArchiveLayout) -> None:
        """Load committed publication state without mirroring its artifacts."""

        if self.config.authority == "local":
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
            if (
                self._remote_prefix_nonempty()
                and not _has_local_collection_work(layout)
            ):
                raise RuntimeError(
                    f"archive prefix contains objects but no {MANIFEST_NAME} and "
                    "the workspace has no recoverable collection: "
                    f"{self._remote().prefix}"
                ) from error
            self.manifest = CollectionsManifest("", {})
            (layout.root / MANIFEST_NAME).unlink(missing_ok=True)
            return

        body = response["Body"]
        try:
            self.manifest = parse_manifest(body.read())
        finally:
            body.close()
        self._write_manifest(layout)

    def materialize_collection(
        self,
        layout: ArchiveLayout,
        collection_id: str,
    ) -> None:
        """Materialize committed state and reconcile retained local WARC work."""

        collection_id = layout.validate_collection_id(collection_id)
        if self.config.authority == "local":
            return

        directory = layout.collection_dir(collection_id)
        directory.mkdir(parents=True, exist_ok=True)
        previous = self.manifest.collections.get(collection_id)
        if previous is None:
            return

        final = previous.warcs[-1]
        final_path = layout.root / final.key
        if final_path.is_file():
            valid_tail = _local_matches(final_path, final) or _local_extends(
                final_path, final
            )
            if not valid_tail:
                raise RuntimeError(
                    f"local WARC conflicts with remote manifest: {final.key}"
                )
        else:
            self._download_artifact(final, final_path)

        previous_by_key = {item.key: item for item in previous.warcs}
        changed: list[Path] = []
        new_paths: list[Path] = []
        for path in list_collection_warcs(layout, collection_id):
            key = path.relative_to(layout.root).as_posix()
            old = previous_by_key.get(key)
            if old is None:
                if key <= final.key:
                    raise RuntimeError(f"local WARC does not follow remote tail: {key}")
                new_paths.append(path)
                changed.append(path)
            elif _local_matches(path, old):
                if old.key != final.key:
                    path.unlink()
            elif old.key == final.key and _local_extends(path, old):
                changed.append(path)
            else:
                raise RuntimeError(f"local WARC conflicts with remote manifest: {key}")
        _validate_rollover_sequence(layout, collection_id, final.key, new_paths)

        index_path = layout.root / previous.index.key
        if not _local_matches(index_path, previous.index):
            try:
                self._download_artifact(previous.index, index_path)
            except (ValueError, ClientError) as error:
                if isinstance(error, ClientError) and not _not_found(error):
                    raise
                if not index_path.is_file():
                    raise RuntimeError(
                        "remote index no longer matches the manifest and no local "
                        f"recovery index exists: {previous.index.key}"
                    ) from error

        if changed:
            publish_collection_index(
                layout,
                collection_id,
                changed_warcs=changed,
                warc_sizes=self.collection_warc_sizes(layout, collection_id),
            )

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

        if previous is not None and not reset:
            _validate_warc_update(layout, collection_id, previous, changed)
        if not changed and not index_changed and not reset:
            return False

        published = {} if reset else dict(previous_by_key)
        if self.config.authority == "local":
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

        if self.config.authority == "local":
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
        self._verify_remote_manifest(next_manifest)
        self.manifest = next_manifest
        self._write_manifest(layout)

        if reset and previous is not None:
            current_keys = set(published)
            for old in previous.warcs:
                if old.key not in current_keys:
                    self.client.delete_object(
                        Bucket=self._remote().bucket,
                        Key=self._key(old.key),
                    )
        return True

    def evict_collection(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Remove remote-authority working copies after a confirmed commit."""

        if self.config.authority != "remote":
            return
        directory = layout.collection_dir(collection_id)
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file() and (
                path.name.endswith(".warc.gz") or path.name.endswith(".cdxj")
            ):
                path.unlink()

    def reset_archive(self, layout: ArchiveLayout) -> None:
        """Delete exactly this archive prefix and clear its local workspace."""

        if self.config.authority != "remote":
            raise ValueError("remote archive reset requires remote authority")
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
        fd, name = tempfile.mkstemp(
            prefix=".tmp-manifest-",
            suffix=".json",
            dir=destination.parent,
        )
        os.close(fd)
        temporary = Path(name)
        try:
            temporary.write_bytes(self.manifest.to_bytes())
            publish_file_atomically(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

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
        _write_atomically(destination, data)

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

    def _verify_remote_manifest(self, expected: CollectionsManifest) -> None:
        if parse_manifest(self._read_object(MANIFEST_NAME)) != expected:
            raise RuntimeError("published manifest did not verify")

    def _remote_prefix_nonempty(self) -> bool:
        response = self.client.list_objects_v2(
            Bucket=self._remote().bucket,
            Prefix=self._key(""),
            MaxKeys=1,
        )
        return bool(response.get("KeyCount", len(response.get("Contents", []))))

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
        remote = self.config.remote
        assert remote is not None
        return remote


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


def _validate_warc_update(
    layout: ArchiveLayout,
    collection_id: str,
    previous: ManifestCollection,
    changed: list[LocalArtifact],
) -> None:
    previous_by_key = {item.key: item for item in previous.warcs}
    changed_existing = [item for item in changed if item.key in previous_by_key]
    if len(changed_existing) > 1:
        raise RuntimeError("only the final WARC may be extended")
    if changed_existing:
        item = changed_existing[0]
        old = previous_by_key[item.key]
        if old.key != previous.warcs[-1].key:
            raise RuntimeError("only the final WARC may be extended")
        if not _local_extends(item.path, old):
            raise RuntimeError("updated WARC must be an exact prefix extension")

    new_paths = [item.path for item in changed if item.key not in previous_by_key]
    _validate_rollover_sequence(
        layout,
        collection_id,
        previous.warcs[-1].key,
        new_paths,
    )


def _validate_rollover_sequence(
    layout: ArchiveLayout,
    collection_id: str,
    tail_key: str,
    paths: list[Path],
) -> None:
    if not paths:
        return
    tail_name = Path(tail_key).name
    tail_sequence = int(tail_name.removesuffix(".warc.gz").rsplit("-", 1)[1])
    for sequence, path in enumerate(sorted(paths), tail_sequence + 1):
        expected = layout.collection_warc_filename(collection_id, sequence)
        if path.name != expected:
            raise RuntimeError(f"new WARC does not follow existing tail: {path.name}")


def _has_local_collection_work(layout: ArchiveLayout) -> bool:
    if not layout.collections_root.is_dir():
        return False
    for directory in layout.collections_root.iterdir():
        if not directory.is_dir():
            continue
        try:
            collection_id = layout.validate_collection_id(directory.name)
        except ValueError:
            continue
        if (
            list_collection_warcs(layout, collection_id)
            and layout.collection_index(collection_id).is_file()
        ):
            return True
    return False


def _local_matches(path: Path, artifact: ManifestArtifact) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == artifact.size_bytes
        and file_sha256(path) == artifact.sha256
    )


def _local_extends(path: Path, artifact: ManifestArtifact) -> bool:
    return (
        path.is_file()
        and path.stat().st_size > artifact.size_bytes
        and _prefix_sha256(path, artifact.size_bytes) == artifact.sha256
    )


def _prefix_sha256(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return "" if remaining else digest.hexdigest()


def _local_etag(digest: str) -> str:
    return f'"sha256:{digest}"'


def _write_atomically(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".tmp-download-",
        suffix=destination.suffix,
        dir=destination.parent,
    )
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_bytes(data)
        publish_file_atomically(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "404",
        "NoSuchKey",
        "NotFound",
    }
