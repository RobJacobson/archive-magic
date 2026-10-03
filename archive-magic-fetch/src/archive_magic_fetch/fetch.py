"""Year-by-year fetch orchestration."""

from __future__ import annotations

import time
import shutil
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .cdx import (
    load_or_fetch_year_cdx,
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
    write_run_record,
)
from .config import (
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
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
from .staging import YearStage, recover_stages
from .storage import archive_lock, purge_remote, sync_archive

DEFAULT_RETRIES = 4


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
    playback_workers: int = 4
    playback_starts_per_second: float = 16.0
    retries: int = DEFAULT_RETRIES
    trace_requests: bool = False
    index_directory: Path | None = None

    def __post_init__(self) -> None:
        data_directory = Path(self.output.data_directory).expanduser().resolve()
        index_directory = (
            Path(self.index_directory)
            if self.index_directory is not None else data_directory.parent / "index"
        ).expanduser().resolve()
        if index_directory == data_directory or data_directory in index_directory.parents:
            raise ValueError("index_directory must be outside data_directory")
        object.__setattr__(self, "index_directory", index_directory)


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
    query: dict[str, object] | None = None
    representatives: dict[tuple[str, str, str], StoredResponse] | None = None


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

    current_year = int(current_utc_cdx_timestamp()[:4])
    layout = ArchiveLayout(settings.output.data_directory, settings.archive_id)
    layout.logs_root.mkdir(parents=True, exist_ok=True)
    run_id = init_run_id(layout)
    init_run_record(layout, run_id)
    factory = client_factory or make_client
    trace_path = (
        layout.run_log(run_id).with_suffix(".requests.jsonl")
        if settings.trace_requests else None
    )
    with mirror_output(layout.run_log(run_id)):
        workers = PlaybackWorkers(
            factory,
            download_fn or download_exact,
            sleep=sleep,
            pace=download_fn is None,
            report=emit,
            max_workers=settings.playback_workers,
            starts_per_second=settings.playback_starts_per_second,
            retries=settings.retries,
            trace_path=trace_path,
        )
        try:
            if trace_path is not None:
                emit(f"playback HTTP trace: {trace_path}")
            with archive_lock(layout):
                recover_stages(layout)
                return _run_fetch(
                    settings,
                    layout=layout,
                    run_id=run_id,
                    workers=workers,
                    current_year=current_year,
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
    current_year: int,
    sleep,
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
        f"retries={settings.retries} (HTTP sends, including retries/recovery; per process)"
    )

    run_skips_errors = 0
    failed_years: list[int] = []
    representatives: dict[tuple[str, str, str], StoredResponse] = {}
    for year, year_start, year_end in year_ranges(
        settings.date_start, settings.date_end
    ):
        if year > current_year:
            emit(f"skipping future CDX year {year}")
            continue
        year_started = time.monotonic()
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
                representatives=representatives,
                sleep=sleep,
            )
            stage.commit(
                result.warcs,
                index_changed=bool(result.warcs) or (
                    result.index is not None
                    and not layout.collection_index(f"{year:04d}").is_file()
                ),
            )
        except Exception as error:  # noqa: BLE001 - isolate years
            if (stage.path / "ready.json").is_file():
                raise
            stage.abort()
            emit(
                f"year {year}: failed ({error}); continuing with remaining years"
            )
            failed_years.append(year)
            continue
        except BaseException:
            stage.abort()
            raise
        if settings.output.type == "remote" and list_collection_warcs(
            layout, f"{year:04d}"
        ):
            sync_archive(layout, settings.output, year=f"{year:04d}")
        year_warcs = _published_warc_artifacts(layout, f"{year:04d}")
        index_path = layout.collection_index(f"{year:04d}")
        collection_index = (
            index_artifact_from_path(layout, index_path)
            if index_path.is_file() else None
        )
        write_run_record(
            layout,
            collection_id=f"{year:04d}",
            run_id=run_id,
            url_pattern=settings.url_pattern,
            date_start=year_start,
            date_end=year_end,
            query=result.query or {},
            warcs=year_warcs,
            index=collection_index,
            metrics=result.metrics,
            failures=result.failures,
        )
        representatives.clear()
        representatives.update(result.representatives or {})
        _accumulate_metrics(metrics, result.metrics)
        all_failures.extend(result.failures)
        run_skips_errors += result.skip_errors
        emit(
            f"year {year} done: downloads={result.metrics.downloads} "
            f"payload-reuses={result.metrics.payload_reuses} "
            f"revisits={result.metrics.revisits} "
            f"already-represented={result.metrics.local_reuses} "
            f"skips/errors={result.skip_errors}"
        )
        emit(f"elapsed {format_elapsed(time.monotonic() - year_started)}")

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
    stage: YearStage,
    year: int,
    current_year: int,
    date_start: str,
    date_end: str,
    workers: PlaybackWorkers,
    representatives: dict[tuple[str, str, str], StoredResponse],
    sleep,
) -> _YearResult:
    """Acquire captures and build one yearly collection in staging."""

    collection_id = f"{year:04d}"
    year_metrics = RunMetrics()
    cdx_started = time.monotonic()
    assert settings.index_directory is not None
    acquisition = load_or_fetch_year_cdx(
        index_directory=settings.index_directory,
        year=year,
        current_year=current_year,
        url_pattern=settings.url_pattern,
        cdx_window_days=settings.cdx_window_days,
        cdx_page_limit=settings.cdx_page_limit,
        sleep=sleep,
    )
    year_metrics.cdx_duration_s += time.monotonic() - cdx_started
    captures = acquisition.captures
    selected = _dedupe_captures(
        sorted(
            (item for item in captures if date_start <= item.identity.timestamp <= date_end),
            key=lambda item: item.identity.sort_key(),
        )
    )
    year_metrics.selected += len(selected)

    query: dict[str, object] = {
        "url_pattern": settings.url_pattern,
        "search_url": acquisition.search_url,
        "match_type": acquisition.match_type,
        "result_count": len(captures),
        "cdx_page_limit": settings.cdx_page_limit,
    }

    inventory = inventory_collection(layout, collection_id)
    for stored in representatives.values():
        inventory.remember_representative(stored)
    if any(capture.identity not in inventory.identities for capture in selected):
        stage.prepare_mutable_tail(settings.warc_target_bytes)
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
    )
    _accumulate_metrics(year_metrics, built.metrics)
    year_failures = list(built.failures)
    year_skips_errors = len(year_failures)
    new_warcs = list(built.warcs)

    for artifact in new_warcs:
        emit(f"  staged {artifact.relative_key}")

    idx_started = time.monotonic()
    collection_index = build_cdxj(
        layout,
        collection_id,
        new_warcs,
    )
    year_metrics.index_s += time.monotonic() - idx_started

    year_metrics.unresolved = len(year_failures)
    return _YearResult(
        metrics=year_metrics,
        failures=tuple(year_failures),
        warcs=tuple(new_warcs),
        index=collection_index,
        skip_errors=year_skips_errors,
        query=query,
        representatives=dict(inventory.by_url_digest),
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
) -> WarcBuild:
    """Append resolved payloads, validating each member before it reaches disk."""

    writer = CollectionWarcWriter(layout, collection_id, target_bytes=target_bytes)
    metrics = RunMetrics()
    failures: list[UnresolvedFailure] = []
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
    artifacts: list[WarcArtifact] = []
    for filename in sorted(warc_names):
        path = layout.root / filename
        size_bytes = path.stat().st_size
        sha256 = file_sha256(path)
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
    index_directory: Path | None = None,
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES,
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS,
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT,
    playback_workers: int = 4,
    playback_starts_per_second: float = 16.0,
    retries: int = DEFAULT_RETRIES,
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
        raise ValueError(
            f"CLI end {end} is after the project end {project_end}"
        )
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
