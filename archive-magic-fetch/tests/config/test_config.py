from pathlib import Path
import tomllib

import pytest
from archive_magic_fetch.config.load_archive_config import load_config
from archive_magic_fetch.config.load_playback_policy import load_playback_policy
from archive_magic_fetch.config.models import (
    CONFIG_NAME,
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
    DEFAULT_WARC_TARGET_BYTES,
    INSTANCE_CONFIG_ENV,
    FetchOutput,
    PlaybackPolicy,
)
from archive_magic_fetch.config.build_settings import build_settings


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
"""
        ),
    )
    config = load_config(tmp_path)
    assert config.archive_id == "example.org"
    assert config.url_pattern == "*.example.org"
    assert config.output == FetchOutput("local", (tmp_path / "data").resolve())
    assert config.warc_target_bytes == DEFAULT_WARC_TARGET_BYTES
    assert config.cdx_window_days == DEFAULT_CDX_WINDOW_DAYS
    assert config.cdx_page_limit == DEFAULT_CDX_PAGE_LIMIT
    assert config.start == "2000-01-01"
    assert config.end is None
    assert config.index_directory == tmp_path / "index"

    settings = build_settings(
        config.url_pattern,
        archive_id=config.archive_id,
        date_end="2004",
        output=config.output,
        default_start=config.start,
    )
    assert settings.date_start == "20000101000000"
    assert settings.date_end == "20041231235959"
    assert settings.cdx_window_days == DEFAULT_CDX_WINDOW_DAYS
    assert settings.cdx_page_limit == DEFAULT_CDX_PAGE_LIMIT
    assert settings.index_directory == tmp_path / "index"


def test_programmatic_cache_directory_override(tmp_path):
    settings = build_settings(
        "example.org",
        output=FetchOutput("local", tmp_path / "data"),
        index_directory=tmp_path / "custom-cache",
        date_end="2004",
    )
    assert settings.index_directory == tmp_path / "custom-cache"


@pytest.mark.parametrize("location", ["same", "nested", "symlink", "default_symlink"])
def test_cache_inside_data_is_rejected_before_reset(tmp_path, location):
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "preserve-me"
    sentinel.write_text("existing archive")
    if location == "same":
        cache = data
    elif location == "nested":
        cache = data / "nested" / "index"
    elif location == "symlink":
        cache = tmp_path / "linked-index"
        cache.symlink_to(data, target_is_directory=True)
    else:
        (tmp_path / "index").symlink_to(data, target_is_directory=True)
        cache = None
    with pytest.raises(ValueError, match="index_directory must be outside"):
        build_settings(
            "example.org",
            output=FetchOutput("remote", data, bucket="bucket"),
            index_directory=cache,
            reset_data=True,
            date_end="2004",
        )
    assert sentinel.read_text() == "existing archive"


def test_cdx_window_days_override_from_toml(tmp_path):
    write_config(
        tmp_path,
        local_config(
            """
[fetch]
start = "2000-01-01"
cdx_window_days = 7
"""
        ),
    )
    config = load_config(tmp_path)
    assert config.cdx_window_days == 7
    settings = build_settings(
        config.url_pattern,
        archive_id=config.archive_id,
        output=config.output,
        cdx_window_days=config.cdx_window_days,
        default_start=config.start,
        date_end="2000",
    )
    assert settings.cdx_window_days == 7


def test_cdx_page_limit_override_from_toml(tmp_path):
    write_config(
        tmp_path,
        local_config(
            """
[fetch]
start = "2000-01-01"
cdx_page_limit = 10000
"""
        ),
    )
    config = load_config(tmp_path)
    assert config.cdx_page_limit == 10000
    settings = build_settings(
        config.url_pattern,
        archive_id=config.archive_id,
        output=config.output,
        cdx_page_limit=config.cdx_page_limit,
        default_start=config.start,
        date_end="2000",
    )
    assert settings.cdx_page_limit == 10000


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
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[output]\ntype='local'\n[fetch]\nwarc_target_bytes=0\n",
            "must be positive",
        ),
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[output]\ntype='local'\n[fetch]\ncdx_window_days=0\n",
            "must be positive",
        ),
        (
            "[archive]\nid='x'\nurl_pattern='x'\n[output]\ntype='local'\n[fetch]\ncdx_page_limit=0\n",
            "must be positive",
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


def test_narrowing_rejects_out_of_range_cli_dates(tmp_path):
    write_config(
        tmp_path,
        local_config(
            """
[fetch]
start = "2000-01-01"
end = "2001-12-31"
"""
        ),
    )
    config = load_config(tmp_path)
    with pytest.raises(ValueError, match="before the project start"):
        build_settings(
            config.url_pattern,
            archive_id=config.archive_id,
            date_start="1999-01-01",
            output=config.output,
            default_start=config.start,
            default_end=config.end,
        )
    with pytest.raises(ValueError, match="after the project end"):
        build_settings(
            config.url_pattern,
            archive_id=config.archive_id,
            date_end="2002",
            output=config.output,
            default_start=config.start,
            default_end=config.end,
        )


def policy_document():
    return """[wayback]
workers = 2
starts_per_second = 3.5
retries = 1

[common-crawl]
workers = 6
starts_per_second = 7
retries = 5
"""


@pytest.mark.parametrize("location", ["default", "environment", "explicit"])
def test_playback_policy_creates_missing_config(tmp_path, monkeypatch, location):
    path = tmp_path / "nested" / "fetch-config.toml"
    value = None
    if location == "default":
        path = tmp_path / "xdg-config" / "archive-magic-fetch" / "fetch-config.toml"
    elif location == "environment":
        monkeypatch.setenv(INSTANCE_CONFIG_ENV, str(path))
    else:
        value = path
    assert load_playback_policy(value) == PlaybackPolicy(4, 8.0, 4)
    assert load_playback_policy(value, source="common-crawl") == PlaybackPolicy(4, 8.0, 4)
    assert tomllib.loads(path.read_text()) == {
        name: {"workers": 4, "starts_per_second": 8, "retries": 4}
        for name in ("wayback", "common-crawl")
    }


@pytest.mark.parametrize(
    "source, expected",
    [("wayback", PlaybackPolicy(2, 3.5, 1)), ("common-crawl", PlaybackPolicy(6, 7.0, 5))],
)
def test_playback_policy_selects_service_and_preserves_file(tmp_path, source, expected):
    path = tmp_path / "fetch-config.toml"
    original = "# Preserve custom settings and comments.\n" + policy_document()
    path.write_text(original)
    assert load_playback_policy(path, source=source) == expected
    assert path.read_text() == original


def test_playback_policy_path_precedence(tmp_path, monkeypatch):
    env = tmp_path / "env.toml"
    env.write_text(policy_document())
    monkeypatch.setenv(INSTANCE_CONFIG_ENV, str(env))
    assert load_playback_policy() == PlaybackPolicy(2, 3.5, 1)
    explicit = tmp_path / "explicit.toml"
    assert load_playback_policy(explicit) == PlaybackPolicy(4, 8.0, 4)
    assert env.read_text() == policy_document()


@pytest.mark.parametrize(
    "document, message",
    [
        (policy_document() + "[other]\nworkers = 2\n", "unexpected table"),
        (policy_document().replace("workers = 2", "workers = 0"), "wayback.workers"),
        (policy_document().replace("workers = 2", "workers = true"), "wayback.workers"),
        (policy_document().replace("workers = 2", "workers = 1.5"), "wayback.workers"),
        (policy_document().replace("starts_per_second = 3.5", "starts_per_second = 0"), "wayback.starts_per_second"),
        (policy_document().replace("starts_per_second = 3.5", "starts_per_second = inf"), "wayback.starts_per_second"),
        (policy_document().replace("starts_per_second = 3.5", "starts_per_second = nan"), "wayback.starts_per_second"),
        (policy_document().replace("retries = 5", "retries = -1"), "common-crawl.retries"),
        (policy_document().replace("retries = 5", "retries = true"), "common-crawl.retries"),
        (policy_document().replace("workers = 2\n", ""), "missing settings"),
        (policy_document().replace("workers = 2", "worker = 2"), "unexpected settings"),
        (policy_document().split("[common-crawl]")[0], "common-crawl"),
        ("[wayback", "invalid fetch instance configuration"),
        ("wayback = 1", "wayback must be a TOML table"),
        ("", "wayback"),
    ],
)
def test_playback_policy_rejects_invalid_without_overwriting(tmp_path, document, message):
    path = tmp_path / "fetch-config.toml"
    path.write_text(document)
    with pytest.raises(ValueError, match=message):
        load_playback_policy(path)
    assert path.read_text() == document


def test_playback_policy_reports_creation_failure(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(ValueError, match="invalid fetch instance configuration.*permission denied"):
        load_playback_policy(tmp_path / "nested" / "fetch-config.toml")


def test_playback_policy_rejects_directory(tmp_path):
    with pytest.raises(ValueError, match="invalid fetch instance configuration"):
        load_playback_policy(tmp_path)
