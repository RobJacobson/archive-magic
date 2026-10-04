"""Load and validate one archive configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from archive_magic_fetch.config.models import (
    CONFIG_NAME,
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
    DEFAULT_START,
    DEFAULT_WARC_TARGET_BYTES,
    FetchConfig,
    FetchOutput,
)
from archive_magic_fetch.config.read_toml_section import read_section
from archive_magic_fetch.config.presentation import metadata, asset_path


@dataclass(frozen=True)
class _FetchOptions:
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT
    start: str = DEFAULT_START
    end: str | None = None


def load_config(value: Path | str) -> FetchConfig:
    """Load one explicit Fetch configuration."""

    source = _config_path(value)
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
        if 'archive' in document or 'output' in document:
            raise ValueError('legacy fetch.toml format; migrate to collection.toml (see docs/BUCKET-CATALOG-MIGRATION.md)')
        presentation = metadata(read_section(document, 'collection'))
        archive_id = _safe_id(presentation['id'])
        fetch = dict(read_section(document, 'fetch'))
        url_pattern = fetch.pop('url_pattern')
        if not isinstance(url_pattern, str) or not url_pattern.strip():
            raise ValueError('fetch.url_pattern must be nonempty text')
        source_name = fetch.pop('source', 'wayback')
        storage = dict(read_section(document, 'storage'))
        local = dict(storage.pop('local'))
        directory = _path(source.parent, local.pop('directory'))
        if local or directory == source.parent or directory in source.parent.parents or source.parent in directory.parents:
            raise ValueError('local output must be separate from collection inputs')
        assets = (source.parent / 'assets').resolve()
        if directory == assets or directory in assets.parents or assets in directory.parents:
            raise ValueError('local output overlaps assets')
        for role in ('logo', 'preview'):
            if role in presentation:
                asset_path(source.parent, presentation[role]['src'])
        remote = storage.pop('remote', None)
        if storage:
            raise ValueError('unknown storage settings')
        output_data = dict(remote or {})
        if remote is not None:
            output_data['prefix'] = _prefix(output_data.get('prefix', ''))
        output = FetchOutput('remote' if remote is not None else 'local', directory / 'data', **output_data)
        if output.type == 'remote' and not output.bucket:
            raise ValueError('storage.remote.bucket is required')
        options = _FetchOptions(**fetch)
        if options.warc_target_bytes <= 0:
            raise ValueError("fetch.warc_target_bytes must be positive")
        if options.cdx_window_days <= 0:
            raise ValueError("fetch.cdx_window_days must be positive")
        if options.cdx_page_limit <= 0:
            raise ValueError("fetch.cdx_page_limit must be positive")
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
        if source_name not in ("wayback", "common-crawl"):
            raise ValueError("fetch.source must be 'wayback' or 'common-crawl'")
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "fetch configuration does not exist:"
        ):
            raise
        raise ValueError(f"invalid fetch configuration {source}: {error}") from error

    return FetchConfig(
        archive_id=archive_id,
        source=source_name,
        url_pattern=url_pattern,
        output=output,
        warc_target_bytes=options.warc_target_bytes,
        cdx_window_days=options.cdx_window_days,
        cdx_page_limit=options.cdx_page_limit,
        start=options.start,
        end=options.end,
        index_directory=directory / "discovery",
        collection_directory=source.parent,
        presentation=presentation,
    )


def _config_path(value: Path | str) -> Path:
    """Resolve a Fetch configuration path or its containing directory."""

    candidate = Path(value).expanduser()
    if candidate.name == "fetch.toml":
        raise ValueError("legacy fetch.toml; migrate to collection.toml")
    if candidate.is_dir():
        if not (candidate / CONFIG_NAME).exists() and (candidate / "fetch.toml").exists():
            raise ValueError("legacy fetch.toml; migrate to collection.toml")
        candidate = candidate / CONFIG_NAME
    if not candidate.is_file():
        raise ValueError(f"fetch configuration does not exist: {candidate}")
    return candidate.resolve()


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value in {
        ".",
        "..",
        "static",
    }:
        raise ValueError(f"invalid archive ID: {value!r}")
    return value


def _path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _prefix(value: str) -> str:
    parts = [part for part in value.strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("output.prefix must not contain '.' or '..'")
    return "/".join(parts)
