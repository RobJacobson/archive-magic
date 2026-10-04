"""Configuration records and shared defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "collection.toml"


INSTANCE_CONFIG_NAME = "fetch-config.toml"


INSTANCE_CONFIG_ENV = "ARCHIVE_MAGIC_FETCH_CONFIG"


DEFAULT_WARC_TARGET_BYTES = 250_000_000


DEFAULT_START = "1995-01-01"


DEFAULT_CDX_WINDOW_DAYS = 28


DEFAULT_CDX_PAGE_LIMIT = 5000


DEFAULT_PLAYBACK_WORKERS = 4


DEFAULT_PLAYBACK_STARTS_PER_SECOND = 8.0


DEFAULT_PLAYBACK_RETRIES = 4


@dataclass(frozen=True)
class FetchOutput:
    type: str
    data_directory: Path
    bucket: str | None = None
    prefix: str = ""
    endpoint_url: str | None = None
    region: str = "auto"


@dataclass(frozen=True)
class FetchConfig:
    archive_id: str
    url_pattern: str
    output: FetchOutput
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    start: str = DEFAULT_START
    end: str | None = None
    index_directory: Path | None = None
    source: str = "wayback"
    collection_directory: Path | None = None
    presentation: dict | None = None


@dataclass(frozen=True)
class PlaybackPolicy:
    workers: int = DEFAULT_PLAYBACK_WORKERS
    starts_per_second: float = DEFAULT_PLAYBACK_STARTS_PER_SECOND
    retries: int = DEFAULT_PLAYBACK_RETRIES
