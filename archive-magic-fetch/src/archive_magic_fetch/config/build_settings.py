"""Combine configuration and date overrides into effective run settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from archive_magic_fetch.archive.dates import parse_date_bound, validate_date_range
from archive_magic_fetch.archive.identity import current_utc_cdx_timestamp
from archive_magic_fetch.archive.layout import normalize_archive_id
from archive_magic_fetch.config.models import (
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
    DEFAULT_PLAYBACK_RETRIES,
    DEFAULT_PLAYBACK_STARTS_PER_SECOND,
    DEFAULT_PLAYBACK_WORKERS,
    DEFAULT_WARC_TARGET_BYTES,
    FetchOutput,
)


@dataclass(frozen=True)
class FetchSettings:
    """Validated CLI inputs for one fetch run."""

    url_pattern: str
    date_start: str
    date_end: str
    archive_id: str
    output: FetchOutput
    reset_data: bool = False
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    playback_workers: int = DEFAULT_PLAYBACK_WORKERS
    playback_starts_per_second: float = DEFAULT_PLAYBACK_STARTS_PER_SECOND
    retries: int = DEFAULT_PLAYBACK_RETRIES
    trace_requests: bool = False
    index_directory: Path | None = None

    def __post_init__(self) -> None:
        data_directory = Path(self.output.data_directory).expanduser().resolve()
        index_directory = (
            (
                Path(self.index_directory)
                if self.index_directory is not None
                else data_directory.parent / "index"
            )
            .expanduser()
            .resolve()
        )
        if (
            index_directory == data_directory
            or data_directory in index_directory.parents
        ):
            raise ValueError("index_directory must be outside data_directory")
        object.__setattr__(self, "index_directory", index_directory)


def build_settings(
    url_pattern: str,
    archive_id: str | None = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    *,
    reset_data: bool = False,
    output: FetchOutput,
    index_directory: Path | None = None,
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES,
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS,
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT,
    playback_workers: int = DEFAULT_PLAYBACK_WORKERS,
    playback_starts_per_second: float = DEFAULT_PLAYBACK_STARTS_PER_SECOND,
    retries: int = DEFAULT_PLAYBACK_RETRIES,
    trace_requests: bool = False,
    default_start: str = "1995-01-01",
    default_end: str | None = None,
) -> FetchSettings:
    """Validate CLI-facing inputs into settings."""

    if cdx_window_days <= 0:
        raise ValueError("cdx_window_days must be positive")
    if cdx_page_limit <= 0:
        raise ValueError("cdx_page_limit must be positive")
    project_start = parse_date_bound(None, default=default_start, bound="start")
    project_end = parse_date_bound(
        None,
        default=default_end or current_utc_cdx_timestamp(),
        bound="end",
    )
    validate_date_range(project_start, project_end)
    start = parse_date_bound(date_start, default=project_start, bound="start")
    end = parse_date_bound(date_end, default=project_end, bound="end")
    if start < project_start:
        raise ValueError(
            f"CLI start {start} is before the project start {project_start}"
        )
    if end > project_end:
        raise ValueError(f"CLI end {end} is after the project end {project_end}")
    validate_date_range(start, end)
    return FetchSettings(
        url_pattern=url_pattern.strip(),
        archive_id=archive_id or normalize_archive_id(url_pattern),
        date_start=start,
        date_end=end,
        reset_data=reset_data,
        output=output,
        index_directory=index_directory,
        warc_target_bytes=warc_target_bytes,
        cdx_window_days=cdx_window_days,
        cdx_page_limit=cdx_page_limit,
        playback_workers=playback_workers,
        playback_starts_per_second=playback_starts_per_second,
        retries=retries,
        trace_requests=trace_requests,
    )
