from pathlib import Path

import pytest
from archive_magic_fetch.config import (
    CONFIG_NAME,
    DEFAULT_WARC_TARGET_BYTES,
    FetchOutput,
    load_config,
)
from archive_magic_fetch.fetch import build_settings


def write_config(directory: Path, body: str, name: str = CONFIG_NAME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def local_config(extra: str = "") -> str:
    return f"""
[archive]
id = "example.org"
url_pattern = "*.example.org"
[output]
type = "local"
data_directory = "data"
{extra}
"""


def test_local_config_resolves_directory_and_defaults(tmp_path):
    write_config(
        tmp_path,
        local_config(
            """
[fetch]
start = "2000-01-01"
playback_workers = 2
retries = 5
"""
        ),
    )
    config = load_config(tmp_path)
    assert config.archive_id == "example.org"
    assert config.url_pattern == "*.example.org"
    assert config.output == FetchOutput("local", (tmp_path / "data").resolve())
    assert config.warc_target_bytes == DEFAULT_WARC_TARGET_BYTES
    assert config.playback_workers == 2
    assert config.retries == 5
    assert config.start == "2000-01-01"
    assert config.end is None

    settings = build_settings(
        config.url_pattern,
        archive_id=config.archive_id,
        date_end="2004",
        output=config.output,
        default_start=config.start,
    )
    assert settings.date_start == "20000101000000"
    assert settings.date_end == "20041231235959"


def test_explicit_arbitrary_filename(tmp_path):
    path = write_config(tmp_path, local_config(), name="example.org.toml")
    assert load_config(path).archive_id == "example.org"


def test_remote_config_normalizes_prefix_without_loading_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "process-key")
    (tmp_path / ".env").write_text("AWS_ACCESS_KEY_ID=file-key\n", encoding="utf-8")
    write_config(
        tmp_path,
        """
[archive]
id = "example.org"
url_pattern = "example.org"
[output]
type = "remote"
data_directory = "data"
bucket = "bucket"
prefix = "/archives/example.org/"
endpoint_url = "https://example.invalid"
region = "auto"
""",
    )
    config = load_config(tmp_path)
    assert config.output == FetchOutput(
        "remote",
        (tmp_path / "data").resolve(),
        "bucket",
        "archives/example.org",
        "https://example.invalid",
        "auto",
    )
    assert __import__("os").environ["AWS_ACCESS_KEY_ID"] == "process-key"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "[archive]\nid='bad/id'\nurl_pattern='x'\n[output]\ntype='local'\n",
            "invalid archive ID",
        ),
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[output]\ntype='local'\n[fetch]\nworkers=2\n",
            "unexpected keyword",
        ),
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[output]\ntype='remote'\nbucket='x'\nprefix='../bad'\n",
            "must not contain",
        ),
    ],
)
def test_config_rejects_unsafe_or_unknown_values(tmp_path, body, message):
    write_config(tmp_path, body)
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path)


def test_directory_requires_fetch_toml(tmp_path):
    with pytest.raises(ValueError, match="fetch configuration does not exist"):
        load_config(tmp_path)
