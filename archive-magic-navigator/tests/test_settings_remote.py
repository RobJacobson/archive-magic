from __future__ import annotations
import hashlib
import io
import pytest
from archive_magic_navigator.errors import ValidationError
from archive_magic_navigator.remote import RemoteArchiveStore
from archive_magic_navigator.settings import RemoteSource
from botocore.exceptions import ClientError

def remote_config(prefix="example.org"):
    return RemoteSource("bucket", prefix, "https://endpoint", "auto")


def seed_remote(fake: FakeRemoteS3, *, index_bytes: bytes, warc_size=1000):
    index_key = "example.org/data/example.org-2004-index.cdxj"
    warc_key = "example.org/data/example.org-2004-001.warc.gz"
    fake.seed(index_key, index_bytes, etag='"i"')
    fake.seed(warc_key, b"x" * warc_size, etag='"w"')


class FakeRemoteS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_list = False

    def seed(self, key, body, *, etag=None, metadata=None):
        digest = hashlib.sha256(body).hexdigest()
        self.objects[key] = {
            "body": body,
            "etag": etag or f'"{digest[:16]}"',
            "metadata": metadata or {"sha256": digest},
        }

    def get_object(self, *, Bucket, Key, **kwargs):
        self.calls.append((Key, kwargs))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        item = self.objects[Key]
        return {
            "Body": io.BytesIO(item["body"]),
            "ETag": item["etag"],
            "Metadata": item["metadata"],
        }

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.fail_list:
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "ListObjectsV2")
        prefix = kwargs["Prefix"]
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return {
            "KeyCount": len(keys),
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key]["body"]),
                    "ETag": self.objects[key]["etag"],
                }
                for key in keys
            ],
        }


def test_remote_store_caches_index_and_builds_s3_archive_path(tmp_path, monkeypatch):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)

    archive = store.load_archive("example.org")

    collection = archive.collections[0]
    assert collection.replay_index.read_bytes() == index
    assert collection.archive_path == "s3://bucket/example.org/data/"
    environment = store.child_environment()
    assert environment["AWS_ENDPOINT_URL_S3"] == "https://endpoint"
    assert environment["AWS_DEFAULT_REGION"] == "auto"


def test_remote_rejects_index_range_outside_warc(tmp_path, monkeypatch):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"990","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=index, warc_size=1000)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)

    with pytest.raises(Exception, match="out of bounds"):
        store.load_archive("example.org")


def test_poll_atomically_replaces_a_valid_changed_index(tmp_path, monkeypatch):
    old_index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=old_index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)
    archive = store.load_archive("example.org")
    cache = archive.collections[0].replay_index
    new_index = old_index.replace(b'"offset":"10"', b'"offset":"30"')
    fake.seed(
        "example.org/data/example.org-2004-index.cdxj",
        new_index,
        etag='"i2"',
    )

    store._poll_archive("example.org")

    assert cache.read_bytes() == new_index
    assert archive.replay_index.read_bytes() == new_index
    index_calls = [
        kwargs
        for key, kwargs in fake.calls
        if isinstance(key, str) and key.endswith("index.cdxj")
    ]
    assert index_calls[-1]["IfMatch"] == '"i2"'


def test_metadata_mismatch_during_poll_retains_previous_cache(tmp_path, monkeypatch):
    old_index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=old_index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)
    archive = store.load_archive("example.org")
    cache = archive.collections[0].replay_index
    expected = old_index.replace(b'"offset":"10"', b'"offset":"30"')
    fake.seed(
        "example.org/data/example.org-2004-index.cdxj",
        b"corrupt",
        etag='"i2"',
        metadata={"sha256": hashlib.sha256(expected).hexdigest()},
    )

    with pytest.raises(Exception, match="does not match metadata"):
        store._poll_archive("example.org")

    assert cache.read_bytes() == old_index
    assert archive.replay_index.read_bytes() == old_index
    assert store._states["example.org"]["2004"].index.etag == '"i"'


def test_startup_uses_validated_cache_when_remote_sync_fails(
    tmp_path,
    monkeypatch,
):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    config = remote_config()
    first = RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")
    first.replay_index.unlink()  # Cache from before merged replay indexes existed.
    fake.fail_list = True

    archive = RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")

    assert archive.collections[0].replay_index.read_bytes() == index
    assert archive.replay_index.read_bytes() == index


def test_offline_restart_keeps_last_published_snapshot_after_failed_refresh(
    tmp_path, monkeypatch,
):
    import os
    from pathlib import Path

    import archive_magic_navigator.store as store_module

    old = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    updated = old.replace(b'"offset":"10"', b'"offset":"30"')
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=old)
    monkeypatch.setattr("archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake)
    store = RemoteArchiveStore(remote_config(), tmp_path, 300)
    archive = store.load_archive("example.org")
    fake.seed("example.org/data/example.org-2004-index.cdxj", updated)

    real_replace = os.replace

    def fail_snapshot(source, destination):
        if Path(destination) == archive.replay_index:
            raise OSError("snapshot publication interrupted")
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", fail_snapshot)
        with pytest.raises(OSError, match="snapshot publication interrupted"):
            store._poll_archive("example.org")

    assert archive.replay_index.read_bytes() == old
    assert archive.collections[0].replay_index.read_bytes() == updated

    fake.fail_list = True
    restarted = RemoteArchiveStore(remote_config(), tmp_path, 300)
    recovered = restarted.load_archive("example.org")
    assert recovered.replay_index.read_bytes() == old

    fake.fail_list = False
    restarted._poll_archive("example.org")
    assert recovered.replay_index.read_bytes() == updated


@pytest.mark.parametrize("failure", ["listing", "download", "validation", "publication"])
def test_remote_batch_failure_preserves_playback_and_retries(tmp_path, monkeypatch, failure):
    from pathlib import Path

    old = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=old)
    monkeypatch.setattr("archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake)
    store = RemoteArchiveStore(remote_config(), tmp_path, 300)
    archive = store.load_archive("example.org")
    state = store._states["example.org"].copy()
    updated = old.replace(b'"offset":"10"', b'"offset":"30"')
    added = old.replace(b"2004", b"2005")
    fake.seed("example.org/data/example.org-2004-index.cdxj", updated)
    fake.seed("example.org/data/example.org-2005-index.cdxj", added)
    fake.seed("example.org/data/example.org-2005-001.warc.gz", b"x" * 1000)

    with monkeypatch.context() as patch:
        if failure == "listing":
            fake.fail_list = True
        elif failure == "download":
            real_get = fake.get_object

            def fail_second(**kwargs):
                if "2005" in kwargs["Key"]:
                    raise OSError("download interrupted")
                return real_get(**kwargs)

            patch.setattr(fake, "get_object", fail_second)
        elif failure == "validation":
            fake.seed("example.org/data/example.org-2005-index.cdxj", b"incomplete")
        else:
            import os
            real_replace = os.replace

            def fail_publication(source, target):
                if Path(target) == archive.replay_index:
                    raise OSError("publication interrupted")
                real_replace(source, target)

            patch.setattr("archive_magic_navigator.store.os.replace", fail_publication)
        with pytest.raises((OSError, ClientError, ValidationError)):
            store._poll_archive("example.org")

    assert archive.replay_index.read_bytes() == old
    assert store._states["example.org"] == state
    assert not list(tmp_path.rglob(".tmp-*"))
    fake.fail_list = False
    fake.seed("example.org/data/example.org-2005-index.cdxj", added)
    store._poll_archive("example.org")
    assert archive.replay_index.read_bytes() == updated + added
    assert set(store._states["example.org"]) == {"2004", "2005"}
    assert not list(tmp_path.rglob("*.warc.gz"))
    assert all(key == "list" or key.endswith(".cdxj") for key, _ in fake.calls)

    # Missing annual indexes must not evict accepted captures. An unchanged
    # inventory also causes no index downloads or snapshot replacement.
    fake.objects.pop("example.org/data/example.org-2004-index.cdxj")
    previous = archive.replay_index.stat()
    fake.calls.clear()
    store._poll_archive("example.org")
    assert archive.replay_index.stat() == previous
    assert all(key == "list" for key, _ in fake.calls)


@pytest.mark.parametrize("invalid", [b"", b"\n", b"unsorted"])
def test_remote_rejects_empty_or_unsorted_refresh(tmp_path, monkeypatch, invalid):
    old = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    fake = FakeRemoteS3()
    seed_remote(fake, index_bytes=old)
    monkeypatch.setattr("archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake)
    store = RemoteArchiveStore(remote_config(), tmp_path, 300)
    archive = store.load_archive("example.org")
    if invalid == b"unsorted":
        invalid = old.replace(b"20040101000000", b"20040201000000") + old
    fake.seed("example.org/data/example.org-2004-index.cdxj", invalid)
    with pytest.raises(ValidationError):
        store._poll_archive("example.org")
    assert archive.replay_index.read_bytes() == old
