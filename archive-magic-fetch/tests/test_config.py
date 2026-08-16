from pathlib import Path

import pytest
from archive_magic_fetch.config import (
    DEFAULT_WARC_TARGET_BYTES,
    RemoteConfig,
    StorageConfig,
    load_config,
)
from archive_magic_fetch.fetch import build_settings


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


def test_fetch_build_settings_uses_descriptor_defaults(tmp_path):
    descriptor(
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
    config = load_config(tmp_path)
    assert config.warc_target_bytes == DEFAULT_WARC_TARGET_BYTES
    assert config.retries == 5
    settings = build_settings(
        config.url_pattern,
        archive_id=config.archive_id,
        date_end="2004",
        storage=config.storage,
        default_start=config.start,
    )
    assert settings.date_start == "20000101000000"
    assert settings.date_end == "20041231235959"


def test_remote_descriptor_uses_standard_credentials_without_loading_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-key")
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=file-key\n", encoding="utf-8")
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
    config = load_config(path)
    assert config.storage.remote == RemoteConfig(
        "bucket", "archives/example.org", "https://example.invalid", "auto"
    )
    assert __import__("os").environ["AWS_ACCESS_KEY_ID"] == "process-key"
