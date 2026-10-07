"""Coordinate annual discovery, acquisition, archive commit, and publication."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from functools import partial
from pathlib import Path

from archive_magic_fetch.archive.dates import year_ranges
from archive_magic_fetch.archive.identity import current_utc_cdx_timestamp
from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search
from archive_magic_fetch.archive.layout import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    reject_legacy_layout,
)
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.contracts import DiscoveryRequest, SourceAdapter
from archive_magic_fetch.models import (
    RunMetrics,
    UnresolvedFailure,
)
from archive_magic_fetch.pipeline.stage_year import YearChanges, YearStage
from archive_magic_fetch.pipeline.discovery.discover_captures import discover_captures
from archive_magic_fetch.pipeline.publication.purge_remote import purge_remote
from archive_magic_fetch.pipeline.publication.storage import BucketStorage
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
    changes: YearChanges
    query: dict[str, object]


def run_fetch(
    settings: FetchSettings,
    *,
    source: SourceAdapter,
    publisher: BucketStorage | None = None,
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
                if publisher is None and settings.output.type == "remote":
                    publisher = BucketStorage(settings.output, settings.archive_id)
                YearStage.recover(layout)
                if publisher is not None:
                    publisher.preflight(reset=settings.reset_data)
                return _run_fetch(
                    settings, layout=layout, run_id=run_id, workers=workers,
                    current_year=current_year, source=source, download=download,
                    clock=clock, publisher=publisher,
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
    publisher: BucketStorage | None,
) -> FetchResult:
    """Execute serial years with parallel playback and one WARC writer."""

    if settings.reset_data and settings.output.type == "remote":
        purge_remote(settings.output, layout.archive_id)
        if publisher is not None:
            publisher.reset_receipt()
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
                on_cache_complete=publisher.publish_discovery if publisher is not None else None,
            )
            if publisher is not None:
                publisher.record_generation(result.changes.artifacts)
            stage.commit(result.changes)
            if publisher is not None:
                publisher.finish_generation()
        except Exception as error:  # noqa: BLE001 - isolate years
            if stage is not None and stage.promotion_ready:
                raise
            if stage is not None:
                stage.abort()
            if publisher is not None:
                publisher.recover_generation()
            emit(f"year {year}: failed ({error}); continuing with remaining years")
            failed_years.append(year)
            continue
        except BaseException:
            if stage is not None:
                stage.abort()
            raise
        if publisher is not None and result.changes.warcs:
            publisher.publish(year=f"{year:04d}")
        write_run_record(
            layout,
            collection_id=f"{year:04d}",
            run_id=run_id,
            url_pattern=settings.url_pattern,
            date_start=year_start,
            date_end=year_end,
            query=result.query,
            warcs=result.changes.warcs,
            index=result.changes.index,
            metrics=result.metrics,
            failures=result.failures,
        )
        _accumulate_metrics(metrics, result.metrics)
        all_failures.extend(result.failures)
        emit(
            f"year {year} done: downloads={result.metrics.downloads} "
            f"source-recovered={result.metrics.source_recovered} "
            f"revisits={result.metrics.revisits} "
            f"already-represented={result.metrics.local_reuses} "
            f"skips/errors={result.metrics.unresolved}"
        )
        emit(f"elapsed {format_elapsed(clock() - year_started)}")

    emit(
        f"done: downloads={metrics.downloads} revisits={metrics.revisits} "
        f"source-recovered={metrics.source_recovered} "
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
    on_cache_complete: Callable[[Path], None] | None = None,
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
        DiscoveryRequest(settings.url_pattern, year, current_year, on_cache_complete),
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
    changes = stage.prepare_commit(built.changed_warcs)
    year_metrics.index_s += clock() - idx_started
    for artifact in changes.changed_warcs:
        emit(f"  staged {artifact.relative_key}")

    return _YearResult(
        metrics=year_metrics,
        failures=tuple(year_failures),
        changes=changes,
        query=query,
    )


def _accumulate_metrics(total: RunMetrics, current: RunMetrics) -> None:
    """Add one collection's metrics to the invocation totals.

    Every numeric field is a sum. A gauge or maximum needs its own case;
    any other field type fails here instead of disappearing from the total.
    """

    if type(total) is not type(current):
        raise TypeError("metric records do not match")
    for field in fields(total):
        incoming = getattr(current, field.name)
        if field.name == "attempts_by_category":
            if not isinstance(incoming, dict):
                raise TypeError("run metric attempts_by_category is not a mapping")
            for category, count in incoming.items():
                if isinstance(count, bool) or not isinstance(count, int):
                    raise TypeError(
                        f"run metric attempts_by_category[{category!r}] is not an additive count"
                    )
                total.attempts_by_category[category] = (
                    total.attempts_by_category.get(category, 0) + count
                )
            continue
        if isinstance(incoming, bool) or not isinstance(incoming, (int, float)):
            raise TypeError(f"run metric {field.name} is not an additive number")
        setattr(total, field.name, getattr(total, field.name) + incoming)
