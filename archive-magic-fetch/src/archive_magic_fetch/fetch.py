"""Year-by-year fetch orchestration."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .cdx import (
    fetch_cdx,
    parse_date_bound,
    validate_date_range,
    year_ranges,
)
from .identity import current_utc_cdx_timestamp
from .collection import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    file_sha256,
    index_artifact_from_path,
    init_run_id,
    init_run_record,
    list_collection_warcs,
    normalize_archive_id,
    reject_legacy_layout,
    reset_collection_data,
    write_run_record,
)
from .config import (
    DEFAULT_RETRIES,
    DEFAULT_WARC_TARGET_BYTES,
    FetchOutput,
)
from .console import emit, format_elapsed, log_url_outcome, mirror_output
from .index import (
    parse_cdxj_line,
    publish_collection_index,
    reconcile_missing_indexes,
)
from .models import (
    CaptureIdentity,
    IndexArtifact,
    ParsedCapture,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
)
from .playback import download_exact, make_client
from .workers import PlaybackWorkers
from .resolution import (
    CaptureKind,
    CaptureOutcome,
    UrlOutcome,
    group_needs_playback,
    iter_url_outcomes,
    process_url_group,
)
from .inventory import (
    CollectionInventory,
    StoredResponse,
    inventory_collection,
    revisit_from_stored,
    stored_from_playback,
)
from .warc import CollectionWarcWriter
from .storage import PublicationManager


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
    playback_workers: int = 4
    playback_starts_per_second: float = 20.0
    retries: int = DEFAULT_RETRIES


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
    skip_errors: int


@dataclass(frozen=True)
class PayloadData:
    """Lazy playback results for one collection update."""

    url_count: int
    outcomes: Iterator[UrlOutcome]


@dataclass(frozen=True)
class WarcBuild:
    """Result of appending resolved payloads to WARC shards."""

    metrics: RunMetrics
    failures: tuple[UnresolvedFailure, ...]
    warcs: tuple[WarcArtifact, ...]


def run_fetch(
    settings: FetchSettings,
    *,
    client_factory: Optional[Callable] = None,
    download_fn=None,
    sleep=time.sleep,
) -> FetchResult:
    """Execute the annual fetch pipeline with bounded playback workers."""

    layout = ArchiveLayout(settings.output.data_directory, settings.archive_id)
    layout.logs_root.mkdir(parents=True, exist_ok=True)
    run_id = init_run_id(layout)
    init_run_record(layout, run_id)
    factory = client_factory or make_client
    workers = PlaybackWorkers(
        factory,
        download_fn or download_exact,
        sleep=sleep,
        pace=download_fn is None,
        report=emit,
        max_workers=settings.playback_workers,
        starts_per_second=settings.playback_starts_per_second,
        retries=settings.retries,
    )
    try:
        with mirror_output(layout.run_log(run_id)):
            return _run_fetch(
                settings,
                layout=layout,
                run_id=run_id,
                workers=workers,
                sleep=sleep,
            )
    finally:
        workers.close()


def _run_fetch(
    settings: FetchSettings,
    *,
    layout: ArchiveLayout,
    run_id: str,
    workers: PlaybackWorkers,
    sleep,
) -> FetchResult:
    """Execute serial years with parallel playback and one WARC writer."""

    publisher = PublicationManager(settings.output)
    if settings.reset_data and settings.output.type == "remote":
        publisher.reset_archive(layout)
    publisher.prepare(layout)
    reject_legacy_layout(layout)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    if settings.output.type == "local":
        reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    all_failures: list[UnresolvedFailure] = []
    first_year = int(settings.date_start[:4])
    last_year = int(settings.date_end[:4])
    emit(f"archive {layout.archive_id}: collections {first_year}-{last_year}")
    emit(
        f"playback policy: workers={settings.playback_workers}, "
        f"starts/second={settings.playback_starts_per_second:g}"
    )

    run_skips_errors = 0
    failed_years: list[int] = []
    representatives: dict[tuple[str, str, str], StoredResponse] = {}
    for year, year_start, year_end in year_ranges(
        settings.date_start, settings.date_end
    ):
        try:
            result = _run_year(
                settings,
                layout=layout,
                year=year,
                date_start=year_start,
                date_end=year_end,
                run_id=run_id,
                workers=workers,
                publisher=publisher,
                representatives=representatives,
                sleep=sleep,
            )
        except Exception as error:  # noqa: BLE001 - isolate years
            emit(
                f"year {year}: failed ({error}); continuing with remaining years"
            )
            failed_years.append(year)
            continue
        _accumulate_metrics(metrics, result.metrics)
        all_failures.extend(result.failures)
        run_skips_errors += result.skip_errors

    emit(
        f"done: downloads={metrics.downloads} revisits={metrics.revisits} "
        f"payload-reuses={metrics.payload_reuses} "
        f"already-represented={metrics.local_reuses} "
        f"skips/errors={run_skips_errors}"
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
    year: int,
    date_start: str,
    date_end: str,
    run_id: str,
    workers: PlaybackWorkers,
    publisher: PublicationManager,
    representatives: dict[tuple[str, str, str], StoredResponse],
    sleep,
) -> _YearResult:
    """Acquire, resolve, publish, and record one yearly collection."""

    collection_id = f"{year:04d}"
    if settings.reset_data:
        reset_collection_data(layout, collection_id)
        emit(f"year {year}: reset existing collection data")
    else:
        publisher.materialize_index(layout, collection_id)
        leftover = list_collection_warcs(layout, collection_id)
        if leftover:
            publish_collection_index(
                layout,
                collection_id,
                changed_warcs=leftover,
                warc_sizes=publisher.collection_warc_sizes(layout, collection_id),
            )
    year_metrics = RunMetrics()
    year_started = time.monotonic()
    emit(f"year {year}: CDX query")
    cdx_started = time.monotonic()
    year_cdx = fetch_cdx(
        url_pattern=settings.url_pattern,
        date_start=date_start,
        date_end=date_end,
        retries=settings.retries,
        sleep=sleep,
    )
    year_metrics.cdx_duration_s += time.monotonic() - cdx_started
    selected = _dedupe_captures(year_cdx.captures)
    year_metrics.selected += len(selected)

    inventory = inventory_collection(layout, collection_id)
    for stored in representatives.values():
        inventory.remember_representative(stored)
    if any(capture.identity not in inventory.identities for capture in selected):
        publisher.materialize_tail(layout, collection_id)
    payloads = fetch_payload_data(selected, inventory=inventory, workers=workers)
    emit(
        f"year {year}: {len(selected)} captures across {payloads.url_count} URLs"
    )
    built = append_to_warc(
        payloads,
        layout=layout,
        collection_id=collection_id,
        target_bytes=settings.warc_target_bytes,
        inventory=inventory,
        warc_sizes=publisher.collection_warc_sizes(layout, collection_id),
    )
    _accumulate_metrics(year_metrics, built.metrics)
    year_failures = list(built.failures)
    year_skips_errors = len(year_failures)
    new_warcs = list(built.warcs)

    for artifact in new_warcs:
        emit(f"  published {artifact.relative_key}")

    idx_started = time.monotonic()
    collection_index = build_cdxj(
        layout,
        collection_id,
        new_warcs,
        warc_sizes=publisher.collection_warc_sizes(layout, collection_id),
    )
    year_metrics.index_s += time.monotonic() - idx_started

    year_metrics.unresolved = len(year_failures)
    publisher.publish_collection(
        layout,
        collection_id,
        reset=settings.reset_data,
    )
    year_warcs = _published_warc_artifacts(layout, collection_id, publisher)
    write_run_record(
        layout,
        collection_id=collection_id,
        run_id=run_id,
        url_pattern=settings.url_pattern,
        date_start=date_start,
        date_end=date_end,
        query={
            "url_pattern": settings.url_pattern,
            "search_url": year_cdx.search_url,
            "match_type": year_cdx.match_type,
            "result_count": len(year_cdx.captures),
        },
        warcs=year_warcs,
        index=collection_index,
        metrics=year_metrics,
        failures=year_failures,
    )
    publisher.evict_collection(layout, collection_id)
    emit(
        f"year {year} done: downloads={year_metrics.downloads} "
        f"payload-reuses={year_metrics.payload_reuses} "
        f"revisits={year_metrics.revisits} "
        f"already-represented={year_metrics.local_reuses} "
        f"skips/errors={year_skips_errors}"
    )
    emit(f"elapsed {format_elapsed(time.monotonic() - year_started)}")
    representatives.clear()
    representatives.update(inventory.by_url_digest)

    return _YearResult(
        metrics=year_metrics,
        failures=tuple(year_failures),
        warcs=tuple(year_warcs),
        index=collection_index,
        skip_errors=year_skips_errors,
    )


def fetch_payload_data(
    captures: Sequence[ParsedCapture],
    *,
    inventory: CollectionInventory,
    workers: PlaybackWorkers,
) -> PayloadData:
    """Resolve selected CDX captures into a lazy stream of payload outcomes."""

    grouped: dict[str, list[ParsedCapture]] = defaultdict(list)
    for capture in captures:
        grouped[capture.identity.urlkey].append(capture)
    groups = list(grouped.values())
    identities = frozenset(inventory.identities)
    representatives = dict(inventory.by_url_digest)

    def process(group: Sequence[ParsedCapture]) -> UrlOutcome:
        return process_url_group(
            group,
            workers=workers,
            existing_identities=identities,
            existing_representatives=representatives,
        )

    outcomes = iter_url_outcomes(
        groups,
        process,
        workers,
        tuple(
            not group_needs_playback(group, identities, representatives)
            for group in groups
        ),
    )
    return PayloadData(url_count=len(groups), outcomes=outcomes)


def append_to_warc(
    payloads: PayloadData,
    *,
    layout: ArchiveLayout,
    collection_id: str,
    target_bytes: int,
    inventory: CollectionInventory,
    warc_sizes: Mapping[str, int] | None = None,
) -> WarcBuild:
    """Append resolved payloads, validating each member before it reaches disk."""

    writer = CollectionWarcWriter(layout, collection_id, target_bytes=target_bytes)
    metrics = RunMetrics()
    failures: list[UnresolvedFailure] = []
    try:
        for number, outcome in enumerate(payloads.outcomes, start=1):
            metrics.playback_attempts += outcome.attempts
            metrics.playback_bytes += outcome.playback_bytes
            for category in outcome.categories:
                metrics.bump_attempt(category)
            for capture in outcome.captures:
                failure = _commit_capture_outcome(
                    capture,
                    inventory=inventory,
                    writer=writer,
                    metrics=metrics,
                )
                if failure is not None:
                    failures.append(failure)
            log_url_outcome(number, payloads.url_count, outcome)
        started = time.monotonic()
        warcs = writer.close()
        metrics.warc_write_s += time.monotonic() - started
    except BaseException:
        _finalize_interrupted_year(
            layout,
            collection_id,
            writer,
            warc_sizes=warc_sizes,
        )
        raise
    metrics.unresolved = len(failures)
    return WarcBuild(metrics, tuple(failures), tuple(warcs))


def build_cdxj(
    layout: ArchiveLayout,
    collection_id: str,
    changed_warcs: Sequence[WarcArtifact],
    *,
    warc_sizes: Mapping[str, int] | None = None,
) -> IndexArtifact | None:
    """Build or reuse the collection CDXJ after WARC append completes."""

    if not list_collection_warcs(layout, collection_id):
        return None
    index_path = layout.collection_index(collection_id)
    if not changed_warcs and index_path.is_file():
        return index_artifact_from_path(layout, index_path)
    return publish_collection_index(
        layout,
        collection_id,
        changed_warcs=[item.path for item in changed_warcs],
        warc_sizes=warc_sizes,
    )


def _finalize_interrupted_year(
    layout: ArchiveLayout,
    collection_id: str,
    writer: CollectionWarcWriter,
    *,
    warc_sizes: Mapping[str, int] | None = None,
) -> None:
    """Finalize any open shard and rebuild CDXJ; do not publish remotely."""

    artifacts: list[WarcArtifact] = []
    try:
        artifacts = writer.close()
        for artifact in artifacts:
            emit(f"  published {artifact.relative_key}")
    except Exception as error:  # noqa: BLE001 - best-effort interrupt finalization
        emit(f"year {collection_id}: failed to finalize open WARC ({error})")
    if not list_collection_warcs(layout, collection_id):
        return
    try:
        index = publish_collection_index(
            layout,
            collection_id,
            changed_warcs=[item.path for item in artifacts],
            warc_sizes=warc_sizes,
        )
        if index is not None:
            emit(f"  published {index.relative_key}")
    except Exception as error:  # noqa: BLE001 - next run reconciles
        emit(f"year {collection_id}: failed to rebuild index ({error})")


def _commit_capture_outcome(
    outcome: CaptureOutcome,
    *,
    inventory: CollectionInventory,
    writer: CollectionWarcWriter,
    metrics: RunMetrics,
) -> UnresolvedFailure | None:
    """Apply one worker result on the single writer thread."""

    if outcome.kind is CaptureKind.EXISTING:
        metrics.local_reuses += 1
        metrics.represented += 1
        return None
    if outcome.kind is CaptureKind.FAILURE:
        assert outcome.failure is not None
        return outcome.failure
    if outcome.kind is CaptureKind.REVISIT:
        assert outcome.representative is not None
        _write_revisit(
            identity=outcome.identity,
            stored=outcome.representative,
            inventory=inventory,
            writer=writer,
            metrics=metrics,
        )
        return None

    result = outcome.playback
    assert result is not None
    started = time.monotonic()
    writer.write_playback(result)
    metrics.warc_write_s += time.monotonic() - started
    metrics.represented += 1
    inventory.identities.add(outcome.identity)
    if outcome.kind in {
        CaptureKind.EMPTY,
        CaptureKind.SLASH_REDIRECT,
    }:
        metrics.payload_reuses += 1
    else:
        metrics.downloads += 1
        if not result.digest_matched:
            metrics.digest_mismatch_accepted += 1
    if result.digest_matched or outcome.kind is CaptureKind.SLASH_REDIRECT:
        inventory.remember_representative(stored_from_playback(result))
    return None


def _write_revisit(
    *,
    identity: CaptureIdentity,
    stored: StoredResponse,
    inventory: CollectionInventory,
    writer: CollectionWarcWriter,
    metrics: RunMetrics,
) -> None:
    started = time.monotonic()
    writer.write_revisit(revisit_from_stored(identity, stored))
    metrics.warc_write_s += time.monotonic() - started
    inventory.identities.add(identity)
    metrics.revisits += 1
    metrics.represented += 1


def _dedupe_captures(
    captures: Sequence[ParsedCapture],
) -> list[ParsedCapture]:
    seen: set[CaptureIdentity] = set()
    result: list[ParsedCapture] = []
    for capture in captures:
        if capture.identity in seen:
            continue
        seen.add(capture.identity)
        result.append(capture)
    return result


def _published_warc_artifacts(
    layout: ArchiveLayout,
    collection_id: str,
    publisher: PublicationManager,
) -> list[WarcArtifact]:
    """Summarize committed WARCs from the CDXJ and size inventory."""

    index_path = layout.collection_index(collection_id)
    if not index_path.is_file():
        return []
    capture_counts: dict[str, int] = defaultdict(int)
    warc_names: set[str] = set()
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            filename = parse_cdxj_line(line)[2]["filename"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(filename, str):
            warc_names.add(filename)
            capture_counts[filename] += 1
    sizes = publisher.collection_warc_sizes(layout, collection_id)
    artifacts: list[WarcArtifact] = []
    for filename in sorted(warc_names):
        path = layout.root / filename
        size_bytes = sizes.get(filename, path.stat().st_size if path.is_file() else 0)
        sha256 = file_sha256(path) if path.is_file() else ""
        remote = publisher.inventory.get(filename)
        if not sha256 and remote is not None and remote.sha256 is not None:
            sha256 = remote.sha256
        artifacts.append(
            WarcArtifact(
                relative_key=filename,
                collection_id=collection_id,
                sequence=int(
                    Path(filename).name.removesuffix(".warc.gz").rsplit("-", 1)[1]
                ),
                path=path,
                size_bytes=size_bytes,
                sha256=sha256,
                record_count=capture_counts[filename] + 1,
            )
        )
    return artifacts


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


def build_settings(
    url_pattern: str,
    archive_id: str | None = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    *,
    reset_data: bool = False,
    output: FetchOutput,
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES,
    playback_workers: int = 4,
    playback_starts_per_second: float = 20.0,
    retries: int = DEFAULT_RETRIES,
    default_start: str = "1995-01-01",
    default_end: str | None = None,
) -> FetchSettings:
    """Validate CLI-facing inputs into settings."""

    start = parse_date_bound(
        date_start, default=default_start, bound="start"
    )
    end = parse_date_bound(
        date_end,
        default=default_end or current_utc_cdx_timestamp(),
        bound="end",
    )
    validate_date_range(start, end)
    return FetchSettings(
        url_pattern=url_pattern.strip(),
        archive_id=archive_id or normalize_archive_id(url_pattern),
        date_start=start,
        date_end=end,
        reset_data=reset_data,
        output=output,
        warc_target_bytes=warc_target_bytes,
        playback_workers=playback_workers,
        playback_starts_per_second=playback_starts_per_second,
        retries=retries,
    )
