from __future__ import annotations

import hashlib
import io
import json

import pytest
from archive_magic_navigator.remote import RemoteArchiveStore, parse_manifest
from archive_magic_navigator.settings import RemoteConfig, load_config
from botocore.exceptions import ClientError


def test_navigator_toml_defaults_and_relative_paths(tmp_path):
    path = tmp_path / "archive.toml"
    path.write_text(
        """
schema_version = 1
[archive]
id = "example.org"
url_pattern = "example.org"
[storage]
authority = "local"
data_directory = "data"
[playback]
wayback_fallback = false
""",
        encoding="utf-8",
    )
    settings = load_config(path)
    assert settings.archive_id == "example.org"
    assert settings.storage.data_directory == (tmp_path / "data").resolve()
    assert settings.wayback_fallback is False


def test_remote_descriptor_uses_standard_credentials_without_loading_dotenv(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-key")
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=file-key\n")
    path = tmp_path / "archive.toml"
    path.write_text(
        """
schema_version = 1
[archive]
id = "example.org"
url_pattern = "*.example.org"
[storage]
authority = "remote"
data_directory = "data"
[storage.remote]
bucket = "bucket"
prefix = "/archives/example.org/"
endpoint_url = "https://endpoint"
region = "auto"
"""
    )
    settings = load_config(path)
    assert settings.storage.remote == RemoteConfig(
        "bucket",
        "archives/example.org",
        "https://endpoint",
        "auto",
    )
    assert __import__("os").environ["AWS_ACCESS_KEY_ID"] == "process-key"


def remote_config(prefix="example.org"):
    return RemoteConfig("bucket", prefix, "https://endpoint", "auto")


def manifest_and_index(index_bytes):
    warc_key = "example.org-2004-001.warc.gz"
    index_key = "example.org-2004-index.cdxj"
    payload = {
        "published_at": "2026-08-13T20:15:00Z",
        "collections": {
            "2004": {
                "updated_at": "2026-08-13T20:15:00Z",
                "index": {
                    "key": index_key,
                    "etag": '"i"',
                    "sha256": hashlib.sha256(index_bytes).hexdigest(),
                    "size_bytes": len(index_bytes),
                },
                "warcs": [
                    {
                        "key": warc_key,
                        "etag": '"w"',
                        "sha256": "0" * 64,
                        "size_bytes": 1000,
                    }
                ],
            }
        },
    }
    return (json.dumps(payload) + "\n").encode(), index_key


class FakeRemoteS3:
    def __init__(self, manifest, index_key, index):
        self.manifest = manifest
        self.manifest_etag = '"m"'
        self.index_key = index_key
        self.index = index
        self.calls = []
        self.fail_manifest = False

    def get_object(self, *, Bucket, Key, **kwargs):
        self.calls.append((Key, kwargs))
        if Key.endswith("collections-manifest.json"):
            if self.fail_manifest:
                raise ClientError(
                    {"Error": {"Code": "ServiceUnavailable"}}, "GetObject"
                )
            return {
                "Body": io.BytesIO(self.manifest),
                "ETag": self.manifest_etag,
            }
        assert Key.endswith(self.index_key)
        return {"Body": io.BytesIO(self.index), "ETag": '"i"'}


def test_remote_store_caches_index_and_builds_s3_archive_path(tmp_path, monkeypatch):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    manifest, index_key = manifest_and_index(index)
    fake = FakeRemoteS3(manifest, index_key, index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)

    archive = store.load_archive("example.org")

    collection = archive.collections[0]
    assert collection.replay_index.read_bytes() == index
    assert collection.archive_path == "s3://bucket/example.org/"
    environment = store.child_environment()
    assert environment["AWS_ENDPOINT_URL_S3"] == "https://endpoint"
    assert environment["AWS_DEFAULT_REGION"] == "auto"


def test_remote_manifest_rejects_index_range_outside_warc(tmp_path, monkeypatch):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"990","length":"20"}\n'
    )
    manifest, index_key = manifest_and_index(index)
    fake = FakeRemoteS3(manifest, index_key, index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)

    with pytest.raises(Exception, match="out of bounds"):
        store.load_archive("example.org")


def test_manifest_parser_rejects_version_dead_weight():
    manifest, _ = manifest_and_index(b"x")
    raw = json.loads(manifest)
    raw["schema_version"] = 1
    with pytest.raises(Exception, match="published_at and collections"):
        parse_manifest(json.dumps(raw).encode())


def test_poll_atomically_replaces_a_valid_changed_index(tmp_path, monkeypatch):
    old_index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    manifest, index_key = manifest_and_index(old_index)
    fake = FakeRemoteS3(manifest, index_key, old_index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)
    archive = store.load_archive("example.org")
    cache = archive.collections[0].replay_index
    new_index = old_index.replace(b'"offset":"10"', b'"offset":"30"')
    fake.manifest, _ = manifest_and_index(new_index)
    fake.manifest_etag = '"m2"'
    fake.index = new_index

    store._poll_archive("example.org")

    assert cache.read_bytes() == new_index
    manifest_calls = [
        kwargs
        for key, kwargs in fake.calls
        if key.endswith("collections-manifest.json")
    ]
    assert manifest_calls[-1]["IfNoneMatch"] == '"m"'
    index_calls = [kwargs for key, kwargs in fake.calls if key.endswith(index_key)]
    assert index_calls[-1]["IfMatch"] == '"i"'


def test_hash_mismatch_during_poll_retains_previous_cache(tmp_path, monkeypatch):
    old_index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    manifest, index_key = manifest_and_index(old_index)
    fake = FakeRemoteS3(manifest, index_key, old_index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    store = RemoteArchiveStore(remote_config(), tmp_path, 60)
    archive = store.load_archive("example.org")
    cache = archive.collections[0].replay_index
    expected = old_index.replace(b'"offset":"10"', b'"offset":"30"')
    fake.manifest, _ = manifest_and_index(expected)
    fake.manifest_etag = '"m2"'
    fake.index = b"corrupt"

    with pytest.raises(Exception, match="does not match"):
        store._poll_archive("example.org")

    assert cache.read_bytes() == old_index
    assert store._states["example.org"][1] == '"m"'


def test_startup_uses_validated_cache_when_remote_sync_fails(
    tmp_path,
    monkeypatch,
):
    index = (
        b"org,example)/ 20040101000000 "
        b'{"filename":"example.org-2004-001.warc.gz","offset":"10","length":"20"}\n'
    )
    manifest, index_key = manifest_and_index(index)
    fake = FakeRemoteS3(manifest, index_key, index)
    monkeypatch.setattr(
        "archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake
    )
    config = remote_config()
    RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")
    fake.fail_manifest = True

    archive = RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")

    assert archive.collections[0].replay_index.read_bytes() == index
