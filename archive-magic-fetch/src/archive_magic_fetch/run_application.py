"""Assemble and execute a fetch or sync-only command."""

from __future__ import annotations

import sys

from archive_magic_fetch.adapters.build_wayback_source import build_source as build_wayback_source
from archive_magic_fetch.adapters.build_common_crawl_source import build_source as build_common_crawl_source
from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.config.load_archive_config import load_config
from archive_magic_fetch.config.load_playback_policy import load_playback_policy
from archive_magic_fetch.config.build_settings import build_settings
from archive_magic_fetch.pipeline.stage_year import YearStage
from archive_magic_fetch.pipeline.publication.sync_archive import sync_archive
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from archive_magic_fetch.runtime.manage_archive_files import archive_lock


def run_application(args) -> int:
    """Run fetch and return a process exit status."""

    try:
        config = load_config(args.archive)
        if args.sync_only:
            if config.output.type != "remote":
                raise ValueError("--sync-only requires remote output")
            if args.start is not None or args.end is not None or args.reset_data:
                raise ValueError(
                    "--sync-only cannot be combined with dates or --reset-data"
                )
        if config.output.type == "remote" and args.reset_data:
            if args.start is not None or args.end is not None:
                raise ValueError(
                    "remote --reset-data requires the configuration's complete configured date range"
                )
            print(
                "WARNING: --reset-data will delete and rebuild managed remote archive data (metadata and assets are preserved); "
                "playback will be unavailable during the rebuild.",
                file=sys.stderr,
            )
        policy = load_playback_policy(args.config)
        settings = (
            None
            if args.sync_only
            else build_settings(
                config.url_pattern,
                archive_id=config.archive_id,
                date_start=args.start,
                date_end=args.end,
                reset_data=args.reset_data,
                output=config.output,
                index_directory=config.index_directory,
                warc_target_bytes=config.warc_target_bytes,
                cdx_window_days=config.cdx_window_days,
                cdx_page_limit=config.cdx_page_limit,
                playback_workers=args.workers
                if args.workers is not None
                else policy.workers,
                playback_starts_per_second=(
                    args.starts_per_second
                    if args.starts_per_second is not None
                    else policy.starts_per_second
                ),
                retries=args.retries if args.retries is not None else policy.retries,
                trace_requests=args.trace_requests,
                default_start=config.start,
                default_end=config.end,
            )
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        if args.sync_only:
            layout = ArchiveLayout(config.output.data_directory, config.archive_id)
            with archive_lock(layout):
                YearStage.recover(layout)
                sync_archive(layout, config.output)
            return 0
        assert settings is not None
        result = run_fetch(
            settings,
            source=build_common_crawl_source(index_directory=settings.index_directory)
            if config.source == "common-crawl"
            else build_wayback_source(
                index_directory=settings.index_directory,
                cdx_window_days=settings.cdx_window_days,
                cdx_page_limit=settings.cdx_page_limit,
            ),
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"error: {error}", file=sys.stderr)
        return 1
    return result.exit_code
