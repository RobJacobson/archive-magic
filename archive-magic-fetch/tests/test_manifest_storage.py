from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from archive_magic_descriptor import (
    CollectionsManifest,
    ManifestArtifact,
    ManifestCollection,
    parse_manifest,
)
from archive_magic_fetch.collection import ArchiveLayout, ensure_collection_dirs
from archive_magic_fetch.config import RemoteConfig, StorageConfig
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import parse_cdxj_line, publish_collection_index
from archive_magic_fetch.inventory import inventory_collection
from archive_magic_fetch.playback import payload_digest
from archive_magic_fetch.storage import MANIFEST_NAME, PublicationManager
from archive_magic_fetch.warc import CollectionWarcWriter
from botocore.exceptions import ClientError
from helpers import cdx_json, make_capt, patch_cdx, playback


def artifact(key, body=b"data", etag='"etag"'):
    return ManifestArtifact(key, etag, hashlib.sha256(body).hexdigest(), len(body))


def remote_config(tmp_path, prefix="example.org"):
    return StorageConfig(
        "remote",
        tmp_path / "workspace",
        RemoteConfig("bucket", prefix, "https://example.invalid", "auto"),
    )


def patch_s3(monkeypatch, fake):
    monkeypatch.setattr(
        "archive_magic_fetch.storage.boto3.client", lambda *args, **kwargs: fake
    )


class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.operations: list[tuple] = []
        self.fail_key_suffix: str | None = None

    def seed(self, key, body, etag=None, metadata=None):
        self.objects[key] = {
            "body": body,
            "etag": etag or f'"{hashlib.md5(body).hexdigest()}"',  # noqa: S324
            "metadata": metadata or {"sha256": hashlib.sha256(body).hexdigest()},
        }

    def get_object(self, *, Bucket, Key):
        self.operations.append(("get", Key))
        item = self._item(Key, "GetObject")
        return {"Body": io.BytesIO(item["body"]), "ETag": item["etag"]}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        self.operations.append(("list", prefix))
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        if "MaxKeys" in kwargs:
            keys = keys[: kwargs["MaxKeys"]]
        return {"KeyCount": len(keys), "Contents": [{"Key": key} for key in keys]}

    def put_object(self, *, Bucket, Key, Body, Metadata=None, **kwargs):
        self.operations.append(("put", Key))
        data = Body if isinstance(Body, bytes) else Body.read()
        if self.fail_key_suffix and Key.endswith(self.fail_key_suffix):
            raise OSError("simulated upload failure")
        self.seed(Key, data, metadata=Metadata)
        return {"ETag": self.objects[Key]["etag"]}

    def delete_object(self, *, Bucket, Key):
        self.operations.append(("delete", Key))
        self.objects.pop(Key, None)

    def _item(self, key, operation):
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, operation)
        return self.objects[key]


def make_collection(workspace: Path, *, captures: int = 1, target_bytes=250_000_000):
    layout = ArchiveLayout(workspace, "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=target_bytes)
    for number in range(captures):
        identity = make_capt(
            ts=f"200406{number + 1:02d}000000",
            digest="sha1:" + ("A" * 31) + str(number),
        )
        writer.write_playback(playback(identity, body=f"body-{number}".encode()))
    warcs = writer.close()
    index = publish_collection_index(layout, "2004")
    return layout, warcs, index


def seed_manifest(fake: FakeS3, layout: ArchiveLayout, etag='"manifest"'):
    warcs = []
    for path in sorted(layout.collection_dir("2004").glob("*.warc.gz")):
        body = path.read_bytes()
        item = artifact(path.relative_to(layout.root).as_posix(), body)
        fake.seed("example.org/" + item.key, body, item.etag)
        warcs.append(item)
    index_path = layout.collection_index("2004")
    index_body = index_path.read_bytes()
    index = artifact(index_path.relative_to(layout.root).as_posix(), index_body)
    fake.seed("example.org/" + index.key, index_body, index.etag)
    timestamp = "2026-08-13T20:15:00Z"
    manifest = CollectionsManifest(
        timestamp,
        {"2004": ManifestCollection(timestamp, index, tuple(warcs))},
    )
    fake.seed("example.org/" + MANIFEST_NAME, manifest.to_bytes(), etag)
    return manifest


def append_capture(
    layout: ArchiveLayout, number=2, *, target_bytes=250_000_000, warc_sizes=None
):
    writer = CollectionWarcWriter(layout, "2004", target_bytes=target_bytes)
    identity = make_capt(
        ts=f"200406{number:02d}000000",
        digest="sha1:" + ("B" * 31) + str(number),
    )
    writer.write_playback(playback(identity, body=f"append-{number}".encode()))
    changed = writer.close()
    index = publish_collection_index(
        layout,
        "2004",
        changed_warcs=[item.path for item in changed],
        warc_sizes=warc_sizes,
    )
    return identity, changed, index


def _mark_cdxj_recovered(path: Path) -> bytes:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, timestamp, meta = parse_cdxj_line(line)
        meta = {**meta, "recovered": True}
        lines.append(f"{key} {timestamp} {json.dumps(meta, separators=(',', ':'))}")
    data = "".join(f"{line}\n" for line in lines).encode()
    path.write_bytes(data)
    return data


def published_update(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    layout, _, _ = make_collection(tmp_path / "workspace")
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    manager.publish_collection(layout, "2004")
    manager.evict_collection(layout, "2004")
    manager.materialize_index(layout, "2004")
    manager.materialize_tail(layout, "2004")
    identity, _, _ = append_capture(layout)
    return fake, layout, manager, identity


def test_filesystem_publication_uses_synthetic_etags(tmp_path):
    layout, _, _ = make_collection(tmp_path / "workspace")
    manager = PublicationManager(StorageConfig("local", layout.root))
    manager.prepare(layout)
    assert manager.publish_collection(layout, "2004")
    collection = parse_manifest((layout.root / MANIFEST_NAME).read_bytes()).collections[
        "2004"
    ]
    assert collection.index.etag.startswith('"sha256:')
    assert all(item.etag.startswith('"sha256:') for item in collection.warcs)
    assert not manager.publish_collection(layout, "2004")


def test_prepare_does_not_mirror_and_index_download_skips_warcs(
    tmp_path, monkeypatch
):
    fake = FakeS3()
    source, _, _ = make_collection(tmp_path / "source", captures=2, target_bytes=1)
    manifest = seed_manifest(fake, source)
    patch_s3(monkeypatch, fake)
    layout = ArchiveLayout(tmp_path / "workspace", "example.org")
    manager = PublicationManager(remote_config(tmp_path))

    manager.prepare(layout)
    assert not layout.collections_root.exists()
    fake.operations.clear()
    manager.materialize_index(layout, "2004")

    first, tail = manifest.collections["2004"].warcs
    assert not (layout.root / first.key).exists()
    assert not (layout.root / tail.key).exists()
    gets = {op[1] for op in fake.operations if op[0] == "get"}
    assert gets == {"example.org/" + manifest.collections["2004"].index.key}

    fake.operations.clear()
    manager.materialize_tail(layout, "2004")
    assert not (layout.root / first.key).exists()
    assert (layout.root / tail.key).is_file()
    gets = {op[1] for op in fake.operations if op[0] == "get"}
    assert gets == {"example.org/" + tail.key}


def test_materialize_tail_keeps_a_longer_local_file(tmp_path, monkeypatch):
    fake = FakeS3()
    local, _, _ = make_collection(tmp_path / "workspace")
    original_warc = local.collection_warc_path("2004", 1).read_bytes()
    original_index = local.collection_index("2004").read_bytes()
    identity, _, _ = append_capture(local)

    warc = artifact("collections/2004/example.org-2004-001.warc.gz", original_warc)
    index = artifact("collections/2004/example.org-2004-index.cdxj", original_index)
    timestamp = "2026-08-13T20:15:00Z"
    manifest = CollectionsManifest(
        timestamp,
        {"2004": ManifestCollection(timestamp, index, (warc,))},
    )
    fake.seed("example.org/" + warc.key, original_warc, warc.etag)
    fake.seed("example.org/" + index.key, original_index, index.etag)
    fake.seed("example.org/" + MANIFEST_NAME, manifest.to_bytes(), '"manifest"')
    patch_s3(monkeypatch, fake)

    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(local)
    fake.operations.clear()
    manager.materialize_index(local, "2004")
    manager.materialize_tail(local, "2004")

    assert inventory_collection(local, "2004").contains(identity)
    assert not [
        op
        for op in fake.operations
        if op[0] == "get" and op[1].endswith(".warc.gz")
    ]


def test_materialize_tail_keeps_a_new_rollover_shard(tmp_path, monkeypatch):
    fake = FakeS3()
    local, _, _ = make_collection(tmp_path / "workspace")
    original_warc = local.collection_warc_path("2004", 1).read_bytes()
    original_index = local.collection_index("2004").read_bytes()
    identity, changed, _ = append_capture(local, target_bytes=1)
    assert changed[0].sequence == 2

    warc = artifact("collections/2004/example.org-2004-001.warc.gz", original_warc)
    index = artifact("collections/2004/example.org-2004-index.cdxj", original_index)
    timestamp = "2026-08-13T20:15:00Z"
    manifest = CollectionsManifest(
        timestamp,
        {"2004": ManifestCollection(timestamp, index, (warc,))},
    )
    fake.seed("example.org/" + warc.key, original_warc, warc.etag)
    fake.seed("example.org/" + index.key, original_index, index.etag)
    fake.seed("example.org/" + MANIFEST_NAME, manifest.to_bytes(), '"manifest"')
    patch_s3(monkeypatch, fake)

    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(local)
    manager.materialize_tail(local, "2004")
    assert local.collection_warc_path("2004", 2).is_file()
    assert inventory_collection(local, "2004").contains(identity)


def test_remote_publication_is_warc_index_manifest_and_evicts(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    layout, warcs, index = make_collection(tmp_path / "workspace")
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    assert manager.publish_collection(layout, "2004")

    puts = [op[1] for op in fake.operations if op[0] == "put"]
    assert puts == [
        "example.org/" + warcs[0].relative_key,
        "example.org/" + index.relative_key,
        "example.org/" + MANIFEST_NAME,
    ]
    manager.evict_collection(layout, "2004")
    assert not list(layout.collection_dir("2004").glob("*.warc.gz"))
    assert not list(layout.collection_dir("2004").glob("*.cdxj"))


def test_remote_two_run_fetch_extends_same_tail_and_evicts_working_files(
    tmp_path, monkeypatch
):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    bodies = {
        "20040601000000": b"first remote body",
        "20040602000000": b"second remote body",
    }
    rows = []
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        storage=remote_config(tmp_path),
    )

    def download_fn(_client, identity):
        return playback(identity, body=bodies[identity.timestamp])

    for timestamp in bodies:
        rows.append(
            [
                "org,example)/",
                timestamp,
                "http://example.org/",
                "text/html",
                "200",
                payload_digest(bodies[timestamp]).split(":", 1)[1],
                str(len(bodies[timestamp])),
            ]
        )
        original, cdx_mod, fetch_mod = patch_cdx(cdx_json(list(rows)))
        try:
            result = run_fetch(
                settings,
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _seconds: None,
            )
            assert result.exit_code == 0
        finally:
            cdx_mod.fetch_year_cdx = original
            fetch_mod.fetch_year_cdx = original

    warc_keys = sorted(key for key in fake.objects if key.endswith(".warc.gz"))
    assert warc_keys == ["example.org/collections/2004/example.org-2004-001.warc.gz"]
    index_body = fake.objects[
        "example.org/collections/2004/example.org-2004-index.cdxj"
    ]["body"]
    assert len(index_body.splitlines()) == 2
    collection_dir = settings.storage.workspace_directory / "collections" / "2004"
    assert not list(collection_dir.glob("*.warc.gz"))
    assert not list(collection_dir.glob("*.cdxj"))


def test_noop_remote_year_downloads_index_not_tail(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    body = b"already stored"
    digest = payload_digest(body).split(":", 1)[1]
    rows = [
        [
            "org,example)/",
            "20040601000000",
            "http://example.org/",
            "text/html",
            "200",
            digest,
            str(len(body)),
        ]
    ]
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        storage=remote_config(tmp_path),
    )
    original, cdx_mod, fetch_mod = patch_cdx(cdx_json(rows))
    try:
        first = run_fetch(
            settings,
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: playback(identity, body=body),
            sleep=lambda _seconds: None,
        )
        assert first.exit_code == 0
        fake.operations.clear()
        second = run_fetch(
            settings,
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: playback(identity, body=body),
            sleep=lambda _seconds: None,
        )
        assert second.exit_code == 0
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    gets = [op[1] for op in fake.operations if op[0] == "get"]
    assert any(key.endswith("index.cdxj") for key in gets)
    assert not any(key.endswith(".warc.gz") for key in gets)
    assert not any(op[0] == "put" for op in fake.operations)


def test_failed_publication_retries_from_retained_workspace(tmp_path, monkeypatch):
    fake, layout, manager, identity = published_update(tmp_path, monkeypatch)
    fake.fail_key_suffix = MANIFEST_NAME

    with pytest.raises(OSError, match="simulated"):
        manager.publish_collection(layout, "2004")
    assert list(layout.collection_dir("2004").glob("*.warc.gz"))
    assert layout.collection_index("2004").is_file()

    fake.fail_key_suffix = None
    recovered = PublicationManager(remote_config(tmp_path))
    recovered.prepare(layout)
    recovered.materialize_index(layout, "2004")
    recovered.materialize_tail(layout, "2004")
    recovered.publish_collection(layout, "2004")
    assert recovered.manifest.collections["2004"].index.sha256 == hashlib.sha256(
        layout.collection_index("2004").read_bytes()
    ).hexdigest()
    assert inventory_collection(layout, "2004").contains(identity)


def test_missing_manifest_is_an_empty_archive(tmp_path, monkeypatch):
    fake = FakeS3()
    fake.seed("example.org/orphan.warc.gz", b"orphan")
    patch_s3(monkeypatch, fake)
    manager = PublicationManager(remote_config(tmp_path))
    layout = ArchiveLayout(tmp_path / "workspace", "example.org")

    manager.prepare(layout)
    assert manager.manifest.collections == {}


def test_shorter_local_tail_is_replaced_from_remote(tmp_path, monkeypatch):
    fake = FakeS3()
    source, _, _ = make_collection(tmp_path / "source")
    seed_manifest(fake, source)
    patch_s3(monkeypatch, fake)
    layout, _, _ = make_collection(tmp_path / "workspace")
    tail = layout.collection_warc_path("2004", 1)
    tail.write_bytes(b"short")
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    fake.operations.clear()

    manager.materialize_tail(layout, "2004")
    assert tail.stat().st_size > 5
    assert any(
        op[0] == "get" and op[1].endswith(".warc.gz") for op in fake.operations
    )


def test_reset_deletes_only_archive_prefix_and_workspace(tmp_path, monkeypatch):
    fake = FakeS3()
    fake.seed("example.org/a", b"a")
    fake.seed("other.org/a", b"b")
    patch_s3(monkeypatch, fake)
    layout, _, _ = make_collection(tmp_path / "workspace")
    manager = PublicationManager(remote_config(tmp_path))
    manager.reset_archive(layout)
    assert "example.org/a" not in fake.objects
    assert "other.org/a" in fake.objects
    assert layout.root.is_dir()
    assert not list(layout.root.iterdir())


def test_publish_retains_non_materialized_warcs_from_manifest(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    layout, warcs, _ = make_collection(
        tmp_path / "workspace", captures=2, target_bytes=1
    )
    assert len(warcs) == 2
    first_key = warcs[0].relative_key
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    manager.publish_collection(layout, "2004")
    first_body = fake.objects["example.org/" + first_key]["body"]
    manager.evict_collection(layout, "2004")
    manager.materialize_index(layout, "2004")
    manager.materialize_tail(layout, "2004")
    assert not (layout.root / first_key).exists()

    fake.operations.clear()
    append_capture(
        layout,
        number=3,
        warc_sizes=manager.collection_warc_sizes(layout, "2004"),
    )
    assert manager.publish_collection(layout, "2004")
    puts = [op[1] for op in fake.operations if op[0] == "put"]
    assert "example.org/" + first_key not in puts
    assert fake.objects["example.org/" + first_key]["body"] == first_body
    published = parse_manifest(fake.objects["example.org/" + MANIFEST_NAME]["body"])
    assert first_key in [item.key for item in published.collections["2004"].warcs]


def test_remote_publish_skips_unchanged_artifacts(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    layout, _, _ = make_collection(tmp_path / "workspace")
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    assert manager.publish_collection(layout, "2004")
    manager.evict_collection(layout, "2004")
    manager.materialize_index(layout, "2004")
    manager.materialize_tail(layout, "2004")
    fake.operations.clear()

    assert not manager.publish_collection(layout, "2004")
    assert not [op for op in fake.operations if op[0] == "put"]


def test_replaced_remote_cdxj_recovers_from_local_index(tmp_path, monkeypatch):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    layout, _, _ = make_collection(tmp_path / "workspace")
    manager = PublicationManager(remote_config(tmp_path))
    manager.prepare(layout)
    manager.publish_collection(layout, "2004")
    index_path = layout.collection_index("2004")
    index_key = "example.org/" + index_path.relative_to(layout.root).as_posix()
    recovered = _mark_cdxj_recovered(index_path)
    fake.seed(index_key, recovered)

    recovered_manager = PublicationManager(remote_config(tmp_path))
    recovered_manager.prepare(layout)
    recovered_manager.materialize_index(layout, "2004")
    assert index_path.read_bytes() == recovered
    fake.operations.clear()
    assert recovered_manager.publish_collection(layout, "2004")
    puts = [op[1] for op in fake.operations if op[0] == "put"]
    assert puts == [index_key, "example.org/" + MANIFEST_NAME]


def test_remote_run_record_is_written_before_working_files_are_evicted(
    tmp_path, monkeypatch
):
    fake = FakeS3()
    patch_s3(monkeypatch, fake)
    observed = {}
    original_evict = PublicationManager.evict_collection

    def evict(self, layout, collection_id):
        run_dir = layout.capture_dir(collection_id) / "runs"
        observed["run_record"] = run_dir.is_dir() and any(run_dir.glob("*/run.json"))
        collection_dir = layout.collection_dir(collection_id)
        observed["working_files"] = bool(
            list(collection_dir.glob("*.warc.gz")) or list(collection_dir.glob("*.cdxj"))
        )
        original_evict(self, layout, collection_id)

    monkeypatch.setattr(PublicationManager, "evict_collection", evict)
    body = b"ordered"
    original, cdx_mod, fetch_mod = patch_cdx(
        cdx_json(
            [
                [
                    "com,example)/",
                    "20040615000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    payload_digest(body).split(":")[1],
                    str(len(body)),
                ]
            ]
        )
    )
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040615000000",
                date_end="20040615000000",
                archive_id="example.org",
                storage=remote_config(tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: playback(identity, body=body),
            sleep=lambda _seconds: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code == 0
    assert observed["run_record"]
    assert observed["working_files"]
    collection_dir = result.layout.collection_dir("2004")
    assert not list(collection_dir.glob("*.warc.gz"))
    assert not list(collection_dir.glob("*.cdxj"))
