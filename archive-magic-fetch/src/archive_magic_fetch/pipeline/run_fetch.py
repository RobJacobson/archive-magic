"""Coordinate annual discovery, acquisition, archive commit, and publication."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from functools import partial

from archive_magic_fetch.archive.dates import year_ranges
from archive_magic_fetch.archive.identity import current_utc_cdx_timestamp
from archive_magic_fetch.archive.inventory_collection import inventory_collection
from archive_magic_fetch.archive.layout import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    index_artifact_from_path,
    list_collection_warcs,
    reject_legacy_layout,
)
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.contracts import DiscoveryRequest, SourceAdapter
from archive_magic_fetch.models import (
    IndexArtifact,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
)
from archive_magic_fetch.pipeline.stage_year import YearStage
from archive_magic_fetch.pipeline.discovery.discover_captures import discover_captures
from archive_magic_fetch.pipeline.build_collection_index import build_collection_index
from archive_magic_fetch.pipeline.publication.purge_remote import purge_remote
from archive_magic_fetch.pipeline.publication.sync_archive import sync_archive
from archive_magic_fetch.pipeline.reconcile_missing_indexes import reconcile_missing_indexes
from archive_magic_fetch.pipeline.resolve_captures import resolve_captures
from archive_magic_fetch.pipeline.retrieval.fetch_capture import fetch_capture
from archive_magic_fetch.pipeline.write_captures import write_captures
from archive_magic_fetch.runtime.manage_archive_files import archive_lock
from archive_magic_fetch.runtime.track_http_requests import RequestStats
from archive_magic_fetch.runtime.pace_requests import StartGate
from archive_magic_fetch.runtime.write_run_record import (
    init_run_id,
    init_run_record,
    published_warc_artifacts,
    write_run_record,
)
from archive_magic_fetch.runtime.report_progress import emit, format_elapsed, mirror_output
from archive_magic_fetch.runtime.manage_capture_workers import CaptureWorkers


@dataclass
class FetchResult:
    """Outcome of one fetch run."""

    exit_code: int
    layout: ArchiveLayout
    metrics: RunMetrics
    failures: list[UnresolvedFailure]
    failed_years: tuple[int, ...] = ()


@dataclass(frozen=True)
class _YearResult:
    metrics: RunMetrics
    failures: tuple[UnresolvedFailure, ...]
    warcs: tuple[WarcArtifact, ...]
    index: IndexArtifact | None
    query: dict[str, object]


def run_fetch(
    settings: FetchSettings,
    *,
    source: SourceAdapter,
    sleep=time.sleep,
    clock=time.monotonic,
) -> FetchResult:
    """Execute the annual fetch pipeline with bounded playback workers."""

    current_year = int(current_utc_cdx_timestamp()[:4])
    layout = ArchiveLayout(settings.output.data_directory, settings.archive_id)
    layout.logs_root.mkdir(parents=True, exist_ok=True)
    run_id = init_run_id(layout)
    init_run_record(layout, run_id)
    trace_path = (
        layout.run_log(run_id).with_suffix(".requests.csv")
        if settings.trace_requests
        else None
    )
    with mirror_output(layout.run_log(run_id)):
        gate = StartGate(
            settings.playback_starts_per_second, report=emit, clock=clock, sleep=sleep
        )
        stats = RequestStats(gate.wait, report=emit, trace_path=trace_path, clock=clock)
        workers = CaptureWorkers(source, stats, max_workers=settings.playback_workers)
        download = partial(
            fetch_capture,
            source=source,
            client=workers.client,
            gate=gate,
            stats=stats,
            max_attempts=settings.retries + 1,
            clock=clock,
            sleep=sleep,
        )
        try:
            if trace_path is not None:
                emit(f"playback HTTP trace: {trace_path}")
            with archive_lock(layout):
                YearStage.recover(layout)
                return _run_fetch(
                    settings,
                    layout=layout,
                    run_id=run_id,
                    workers=workers,
                    current_year=current_year,
                    source=source,
                    download=download,
                    clock=clock,
                )
        finally:
            try:
                workers.close()
            finally:
                stats.close()


def _run_fetch(
    settings: FetchSettings,
    *,
    layout: ArchiveLayout,
    run_id: str,
    workers: CaptureWorkers,
    current_year: int,
    source: SourceAdapter,
    download,
    clock,
) -> FetchResult:
    """Execute serial years with parallel playback and one WARC writer."""

    if settings.reset_data and settings.output.type == "remote":
        purge_remote(settings.output, layout.archive_id)
        if layout.root.exists():
            shutil.rmtree(layout.root)
    reject_legacy_layout(layout)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    all_failures: list[UnresolvedFailure] = []
    first_year = int(settings.date_start[:4])
    last_year = int(settings.date_end[:4])
    emit(f"archive {layout.archive_id}: collections {first_year}-{last_year}")
    emit(
        f"download: workers={settings.playback_workers}, "
        f"starts/second={settings.playback_starts_per_second:g}, "
        f"retries={settings.retries} (HTTP sends, including retries/redirects; per process)"
    )

    failed_years: list[int] = []
    for year, year_start, year_end in year_ranges(
        settings.date_start, settings.date_end
    ):
        if year > current_year:
            emit(f"skipping future CDX year {year}")
            continue
        year_started = clock()
        stage = YearStage(
            layout,
            f"{year:04d}",
            reset=settings.reset_data and settings.output.type == "local",
        )
        try:
            result = _run_year(
                settings,
                layout=stage.layout,
                stage=stage,
                year=year,
                current_year=current_year,
                date_start=year_start,
                date_end=year_end,
                workers=workers,
                source=source,
                download=download,
                clock=clock,
            )
            stage.commit(
                result.warcs,
                index_changed=bool(result.warcs)
                or (
                    result.index is not None
                    and not layout.collection_index(f"{year:04d}").is_file()
                ),
            )
        except Exception as error:  # noqa: BLE001 - isolate years
            if (stage.path / "ready.json").is_file():
                raise
            stage.abort()
            emit(f"year {year}: failed ({error}); continuing with remaining years")
            failed_years.append(year)
            continue
        except BaseException:
            stage.abort()
            raise
        if settings.output.type == "remote" and list_collection_warcs(
            layout, f"{year:04d}"
        ):
            sync_archive(layout, settings.output, year=f"{year:04d}")
        year_warcs = published_warc_artifacts(layout, f"{year:04d}")
        index_path = layout.collection_index(f"{year:04d}")
        collection_index = (
            index_artifact_from_path(layout, index_path)
            if index_path.is_file()
            else None
        )
        write_run_record(
            layout,
            collection_id=f"{year:04d}",
            run_id=run_id,
            url_pattern=settings.url_pattern,
            date_start=year_start,
            date_end=year_end,
            query=result.query,
            warcs=year_warcs,
            index=collection_index,
            metrics=result.metrics,
            failures=result.failures,
        )
        _accumulate_metrics(metrics, result.metrics)
        all_failures.extend(result.failures)
        emit(
            f"year {year} done: downloads={result.metrics.downloads} "
            f"payload-reuses={result.metrics.payload_reuses} "
            f"revisits={result.metrics.revisits} "
            f"already-represented={result.metrics.local_reuses} "
            f"skips/errors={result.metrics.unresolved}"
        )
        emit(f"elapsed {format_elapsed(clock() - year_started)}")

    emit(
        f"done: downloads={metrics.downloads} revisits={metrics.revisits} "
        f"payload-reuses={metrics.payload_reuses} "
        f"already-represented={metrics.local_reuses} "
        f"skips/errors={metrics.unresolved}"
    )
    if failed_years:
        emit("failed years: " + ", ".join(str(year) for year in failed_years))
    return FetchResult(
        exit_code=1 if failed_years else 0,
        layout=layout,
        metrics=metrics,
        failures=all_failures,
        failed_years=tuple(failed_years),
    )


def _run_year(
    settings: FetchSettings,
    *,
    layout: ArchiveLayout,
    stage: YearStage,
    year: int,
    current_year: int,
    date_start: str,
    date_end: str,
    workers: CaptureWorkers,
    source: SourceAdapter,
    download,
    clock,
) -> _YearResult:
    """Acquire captures and build one yearly collection in staging."""

    collection_id = f"{year:04d}"
    year_metrics = RunMetrics()
    cdx_started = clock()
    acquisition = discover_captures(
        source,
        DiscoveryRequest(settings.url_pattern, year, current_year),
        date_start=date_start,
        date_end=date_end,
    )
    year_metrics.cdx_duration_s += clock() - cdx_started
    selected = acquisition.captures
    year_metrics.selected += len(selected)
    query = acquisition.query

    inventory = inventory_collection(layout, collection_id)
    if any(capture.identity not in inventory.identities for capture in selected):
        stage.prepare_mutable_tail(settings.warc_target_bytes)
    with workers.batch() as batch:
        payloads = resolve_captures(
            selected,
            inventory=inventory,
            batch=batch,
            download=partial(download, cancelled=batch.cancelled),
        )
        emit(f"year {year}: {len(selected)} captures across {payloads.url_count} URLs")
        built = write_captures(
            payloads,
            layout=layout,
            collection_id=collection_id,
            target_bytes=settings.warc_target_bytes,
            inventory=inventory,
            capture_link=source.capture_link,
        )
    _accumulate_metrics(year_metrics, built.metrics)
    year_failures = list(built.failures)
    new_warcs = list(built.warcs)

    for artifact in new_warcs:
        emit(f"  staged {artifact.relative_key}")

    idx_started = clock()
    collection_index = build_collection_index(
        layout, collection_id, changed_warcs=[item.path for item in new_warcs]
    )
    year_metrics.index_s += clock() - idx_started

    return _YearResult(
        metrics=year_metrics,
        failures=tuple(year_failures),
        warcs=tuple(new_warcs),
        index=collection_index,
        query=query,
    )


def _accumulate_metrics(total: RunMetrics, current: RunMetrics) -> None:
    """Add one collection's metrics to the invocation totals."""

    for name in (
        "cdx_duration_s",
        "playback_attempts",
        "playback_bytes",
        "local_reuses",
        "payload_reuses",
        "downloads",
        "revisits",
        "digest_mismatch_accepted",
        "selected",
        "represented",
        "unresolved",
        "warc_write_s",
        "index_s",
    ):
        setattr(total, name, getattr(total, name) + getattr(current, name))
    for category, count in current.attempts_by_category.items():
        total.attempts_by_category[category] = (
            total.attempts_by_category.get(category, 0) + count
        )
