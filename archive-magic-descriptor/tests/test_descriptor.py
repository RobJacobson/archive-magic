from pathlib import Path

import pytest
from archive_magic_descriptor import (
    DEFAULT_WARC_TARGET_BYTES,
    RemoteConfig,
    StorageConfig,
    descriptor_path,
    load_descriptor,
)


def descriptor(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "archive.toml"
    path.write_text(
        """
schema_version = 1
[archive]
id = "example.org"
url_pattern = "*.example.org"
[storage]
authority = "local"
data_directory = "data"
""" + extra,
        encoding="utf-8",
    )
    return path


def test_descriptor_and_directory_shorthand_resolve_relative_workspace(tmp_path):
    path = descriptor(
        tmp_path,
        """
[fetch]
start = "2000-01-01"
playback_workers = 2
retries = 5
[playback]
wayback_fallback = false
""",
    )
    config = load_descriptor(tmp_path)
    assert config.source == path.resolve()
    assert config.archive_id == "example.org"
    assert config.url_pattern == "*.example.org"
    assert config.storage.data_directory == (tmp_path / "data").resolve()
    assert config.warc_target_bytes == DEFAULT_WARC_TARGET_BYTES
    assert config.playback_workers == 2
    assert config.retries == 5
    assert config.wayback_fallback is False


def test_remote_descriptor_parses_prefix_and_remote_block(tmp_path):
    path = tmp_path / "archive.toml"
    path.write_text(
        """
schema_version = 1
[archive]
id = "example.org"
url_pattern = "example.org"
[storage]
authority = "remote"
data_directory = "data"
[storage.remote]
bucket = "bucket"
prefix = "/archives/example.org/"
endpoint_url = "https://example.invalid"
region = "auto"
""",
        encoding="utf-8",
    )
    config = load_descriptor(path)
    assert config.storage.remote == RemoteConfig(
        "bucket", "archives/example.org", "https://example.invalid", "auto"
    )


@pytest.mark.parametrize(
    "body, message",
    [
        ("schema_version=2\n", "schema_version"),
        (
            "schema_version=1\n[archive]\nid='x'\nurl_pattern=''\n"
            "[storage]\nauthority='local'\n",
            "url_pattern",
        ),
        (
            "schema_version=1\n[archive]\nid='bad/id'\nurl_pattern='x'\n"
            "[storage]\nauthority='local'\n",
            "invalid archive ID",
        ),
        (
            "schema_version=1\n[archive]\nid='x'\nurl_pattern='x'\n"
            "[storage]\nauthority='remote'\n",
            "storage.remote is required",
        ),
        (
            "schema_version=1\n[archive]\nid='x'\nurl_pattern='x'\n"
            "[storage]\nauthority='local'\n[storage.remote]\nbucket='x'\n",
            "only valid",
        ),
        (
            "schema_version=1\n[archive]\nid='x'\nurl_pattern='x'\n"
            "[storage]\nauthority='local'\n[fetch]\nworkers=2\n",
            "unknown fetch",
        ),
        (
            "schema_version=1\nunknown=true\n[archive]\nid='x'\nurl_pattern='x'\n"
            "[storage]\nauthority='local'\n",
            "unknown archive descriptor",
        ),
    ],
)
def test_descriptor_validation(tmp_path, body, message):
    path = tmp_path / "archive.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_descriptor(path)


def test_storage_config_has_no_compatibility_aliases(tmp_path):
    local = StorageConfig("local", tmp_path / "data")
    remote = StorageConfig(
        "remote",
        tmp_path / "data",
        RemoteConfig("bucket", "prefix", "https://example.invalid"),
    )
    assert local.authority == "local"
    assert remote.authority == "remote"
    assert remote.remote is not None
    assert remote.remote.bucket == "bucket"
    assert not hasattr(local, "backend")


def test_descriptor_must_have_canonical_filename(tmp_path):
    path = tmp_path / "other.toml"
    path.write_text("schema_version = 1\n")
    with pytest.raises(ValueError, match="must be named archive.toml"):
        load_descriptor(path)


def test_descriptor_path_resolves_directory_shorthand(tmp_path):
    descriptor(tmp_path)
    assert descriptor_path(tmp_path) == (tmp_path / "archive.toml").resolve()
