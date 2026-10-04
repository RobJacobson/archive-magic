"""Coordinate annual discovery, acquisition, archive commit, and publication."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from functools import partial

from archive_magic_fetch.archive.dates import year_ranges
from archive_magic_fetch.archive.identity import current_utc_cdx_timestamp
from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search
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
from archive_magic_fetch.runtime.manage_archive_files import archive_lock, mkdir_durably
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
    mkdir_durably(layout.logs_root)
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
        diagnostics_path = layout.run_log(run_id).with_suffix(".429.jsonl")
        stats = RequestStats(
            gate.wait, report=emit, trace_path=trace_path,
            diagnostics_path=diagnostics_path, clock=clock,
        )
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
                emit(f"HTTP trace: {trace_path}")
            emit(f"HTTP 429 diagnostics (written on first 429): {diagnostics_path}")
            with archive_lock(layout, settings.collection_directory):
                from archive_magic_fetch.pipeline.publication.storage import BucketStorage, active_storage
                store = BucketStorage(settings.output, settings.archive_id) if settings.output.type == 'remote' else None
                YearStage.recover(layout)
                if store is not None:
                    store.preflight(reset=settings.reset_data)
                with active_storage(store):
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
        from archive_magic_fetch.pipeline.publication.storage import ACTIVE
        if ACTIVE.get() is not None:
            ACTIVE.get().reset_receipt()
        if layout.root.exists():
            shutil.rmtree(layout.root)
    reject_legacy_layout(layout)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    # Selected years get a full, strict rebuild in their working view. Avoid
    # reading them twice or trusting a malformed canonical index beforehand.
    reconcile_missing_indexes(layout, exclude_collections={
        f"{year:04d}" for year in range(int(settings.date_start[:4]),
                                      min(int(settings.date_end[:4]), current_year) + 1)
    })

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
        stage = None
        try:
            query_url, match_type = normalize_cdx_search(settings.url_pattern)
            stage = YearStage(
                layout, f"{year:04d}", reset=settings.reset_data,
                binding={"source": source.name, "query": {"url": query_url, "matchType": match_type},
                         "date_start": year_start, "date_end": year_end},
            )
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
            from archive_magic_fetch.pipeline.publication.storage import ACTIVE
            if ACTIVE.get() is not None:
                paths = [item.path for item in result.warcs]
                if stage.index_changed():
                    paths.append(stage.layout.collection_index(f'{year:04d}'))
                ACTIVE.get().record_generation(paths)
            stage.commit(
                result.warcs,
                index_changed=stage.index_changed(),
            )
            if ACTIVE.get() is not None:
                ACTIVE.get().finish_generation()
        except Exception as error:  # noqa: BLE001 - isolate years
            if stage is not None and (stage.path / "ready.json").is_file():
                raise
            if stage is not None:
                stage.abort()
            from archive_magic_fetch.pipeline.publication.storage import ACTIVE
            if ACTIVE.get() is not None:
                ACTIVE.get().recover_generation()
            emit(f"year {year}: failed ({error}); continuing with remaining years")
            failed_years.append(year)
            continue
        except BaseException:
            if stage is not None:
                stage.abort()
            raise
        if settings.output.type == "remote" and list_collection_warcs(
            layout, f"{year:04d}"
        ):
            sync_archive(layout, settings.output, year=f"{year:04d}")
        year_warcs = published_warc_artifacts(
            layout, f"{year:04d}",
            record_counts={name: scanned.records for name, scanned in stage.scans.items()},
        )
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
    idx_started = clock()
    inventory = stage.prepare_inventory()
    year_metrics.index_s += clock() - idx_started
    cdx_started = clock()
    acquisition = discover_captures(
        source,
        DiscoveryRequest(settings.url_pattern, year, current_year),
        date_start=date_start,
        date_end=date_end,
        stats=workers.stats,
    )
    year_metrics.cdx_duration_s += clock() - cdx_started
    selected = acquisition.captures
    year_metrics.selected += len(selected)
    query = acquisition.query

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
            checkpoint=stage.checkpoint,
        )
    _accumulate_metrics(year_metrics, built.metrics)
    year_failures = list(built.failures)
    idx_started = clock()
    collection_index = build_collection_index(
        layout, collection_id, changed_warcs=[item.path for item in built.warcs],
        on_scan=stage.remember_scan,
    )
    year_metrics.index_s += clock() - idx_started
    # Index only this invocation's writes, but promote all retained changes.
    new_warcs = stage.changed_warcs()
    for artifact in new_warcs:
        emit(f"  staged {artifact.relative_key}")

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
