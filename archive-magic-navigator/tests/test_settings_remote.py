from __future__ import annotations

import hashlib
import io

import pytest
from archive_magic_navigator.remote import RemoteArchiveStore
from archive_magic_navigator.settings import (
    CONFIG_NAME,
    LocalSource,
    RemoteSource,
    discover_configs,
    load_config,
)
from botocore.exceptions import ClientError


def write_navigator_toml(directory, body, name=CONFIG_NAME):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_directory_shorthand_and_relative_paths(tmp_path):
    path = write_navigator_toml(
        tmp_path,
        """
[archive]
id = "example.org"
[source]
type = "local"
directory = "data"
[playback]
wayback_fallback = false
""",
    )
    settings = load_config(tmp_path)
    assert settings.config_path == path.resolve()
    assert settings.archive_id == "example.org"
    assert settings.source == LocalSource((tmp_path / "data").resolve())
    assert settings.wayback_fallback is False


def test_explicit_arbitrary_filename(tmp_path):
    path = write_navigator_toml(
        tmp_path,
        """
[archive]
id = "example.org"
[source]
type = "local"
""",
        name="other.toml",
    )
    settings = load_config(path)
    assert settings.config_path == path.resolve()
    assert settings.source == LocalSource((tmp_path / "data").resolve())


def test_missing_canonical_file(tmp_path):
    with pytest.raises(ValueError, match="navigator configuration does not exist"):
        load_config(tmp_path)


def test_defaults_when_playback_omitted(tmp_path):
    write_navigator_toml(
        tmp_path,
        """
[archive]
id = "example.org"
[source]
type = "local"
""",
    )
    settings = load_config(tmp_path)
    assert settings.wayback_fallback is True
    assert settings.source.directory == (tmp_path / "data").resolve()


def test_remote_source_parses_prefix_and_flattened_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-key")
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=file-key\n")
    path = write_navigator_toml(
        tmp_path,
        """
[archive]
id = "example.org"
[source]
type = "remote"
bucket = "bucket"
prefix = "/archives/example.org/"
endpoint_url = "https://endpoint"
region = "auto"
""",
    )
    settings = load_config(path)
    assert settings.source == RemoteSource(
        "bucket",
        "archives/example.org",
        "https://endpoint",
        "auto",
    )
    assert __import__("os").environ["AWS_ACCESS_KEY_ID"] == "process-key"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "[archive]\nid='bad/id'\n[source]\ntype='local'\n",
            "invalid archive ID",
        ),
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[source]\ntype='local'\n",
            "unexpected keyword",
        ),
        (
            "[archive]\nid='x'\n[source]\ntype='remote'\n",
            "bucket",
        ),
        (
            "[archive]\nid='x'\n[source]\ntype='remote'\nbucket='x'\nprefix='../bad'\n",
            "must not contain",
        ),
        (
            "[archive]\nid='x'\n[source]\ntype='local'\n[playback]\nwayback_fallback='yes'\n",
            "must be a boolean",
        ),
    ],
)
def test_configuration_validation(tmp_path, body, message):
    write_navigator_toml(tmp_path, body)
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path)


def test_catalog_discovers_navigator_toml_only(tmp_path):
    write_navigator_toml(
        tmp_path / "b",
        "[archive]\nid='b.example'\n[source]\ntype='local'\n",
    )
    write_navigator_toml(
        tmp_path / "a",
        "[archive]\nid='a.example'\n[source]\ntype='local'\n",
    )
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / CONFIG_NAME).write_text(
        "[archive]\nid='hidden'\n[source]\ntype='local'\n",
        encoding="utf-8",
    )
    (tmp_path / "ignored.toml").write_text("id='root'\n", encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "fetch.toml").write_text(
        "[archive]\nid='old'\n",
        encoding="utf-8",
    )
    paths = discover_configs(tmp_path)
    assert [path.parent.name for path in paths] == ["a", "b"]


def remote_config(prefix="example.org"):
    return RemoteSource("bucket", prefix, "https://endpoint", "auto")


def seed_remote(fake: FakeRemoteS3, *, index_bytes: bytes, warc_size=1000):
    index_key = "example.org/example.org-2004-index.cdxj"
    warc_key = "example.org/example.org-2004-001.warc.gz"
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
    assert collection.archive_path == "s3://bucket/example.org/"
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
        "example.org/example.org-2004-index.cdxj",
        new_index,
        etag='"i2"',
    )

    store._poll_archive("example.org")

    assert cache.read_bytes() == new_index
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
        "example.org/example.org-2004-index.cdxj",
        b"corrupt",
        etag='"i2"',
        metadata={"sha256": hashlib.sha256(expected).hexdigest()},
    )

    with pytest.raises(Exception, match="does not match metadata"):
        store._poll_archive("example.org")

    assert cache.read_bytes() == old_index
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
    RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")
    fake.fail_list = True

    archive = RemoteArchiveStore(config, tmp_path, 60).load_archive("example.org")

    assert archive.collections[0].replay_index.read_bytes() == index
