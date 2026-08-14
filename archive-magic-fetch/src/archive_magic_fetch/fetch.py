"""Year-by-year fetch orchestration."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .cdx import (
    fetch_year_cdx,
    init_run_id,
    parse_date_bound,
    validate_date_range,
    year_ranges,
)
from .identity import (
    current_utc_cdx_timestamp,
    is_invalid_uri_payload_digest,
    wayback_url,
)
from .collection import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    index_artifact_from_path,
    list_collection_warcs,
    normalize_archive_id,
    reject_legacy_layout,
    reset_collection_data,
    write_run_record,
)
from .config import DEFAULT_WARC_TARGET_BYTES, StorageConfig
from .index import (
    parse_cdxj_line,
    publish_collection_index,
    reconcile_missing_indexes,
)
from .models import (
    CaptureIdentity,
    FailureCategory,
    IndexArtifact,
    ParsedCapture,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
)
from .playback import make_client
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
from .playback import download_exact
from .warc import CollectionWarcWriter, salvage_collection_partials
from .storage import PublicationManager


_RESULT_STYLES = {
    "success": "32",
    "revisit": "36",
    "warning": "33",
    "error": "1;31",
    "dim": "2",
}
_OSC = "\033]8;;"
_ST = "\033\\"
_OUTPUT_LOCK = threading.Lock()


def _terminal_output_enabled() -> bool:
    """Return whether stdout can safely render terminal escape sequences."""

    return bool(
        getattr(sys.stdout, "isatty", lambda: False)()
        and os.environ.get("TERM", "") != "dumb"
    )


def _terminal_safe(value: str) -> str:
    """Prevent control characters in CDX data from escaping a terminal field."""

    return "".join(char if char.isprintable() else "?" for char in value)


def _timestamp_link(identity: CaptureIdentity, *, enabled: bool | None = None) -> str:
    """Render a capture timestamp linked to its Wayback playback page."""

    timestamp = identity.timestamp
    label = (
        f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}T"
        f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}"
    )
    if enabled is None:
        enabled = _terminal_output_enabled()
    if not enabled:
        return label
    destination = _terminal_safe(wayback_url(identity.timestamp, identity.original_url))
    return f"{_OSC}{destination}{_ST}{label}{_OSC}{_ST}"


def _style_result(text: str, style: str, *, enabled: bool | None = None) -> str:
    """Apply an ANSI result style when color output is appropriate."""

    if enabled is None:
        enabled = _terminal_output_enabled() and "NO_COLOR" not in os.environ
    if not enabled:
        return text
    return f"\033[{_RESULT_STYLES[style]}m{text}\033[0m"


def _print_line(text: str) -> None:
    with _OUTPUT_LOCK:
        print(text, flush=True)


@dataclass(frozen=True)
class FetchSettings:
    """Validated CLI inputs for one fetch run."""

    url_pattern: str
    date_start: str
    date_end: str
    archive_id: str
    storage: StorageConfig
    reset_data: bool = False
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    playback_workers: int = 4
    playback_starts_per_second: float = 20.0

@dataclass
class FetchResult:
    """Outcome of one fetch run."""

    exit_code: int
    layout: ArchiveLayout
    metrics: RunMetrics
    failures: list[UnresolvedFailure]


@dataclass(frozen=True)
class _YearResult:
    metrics: RunMetrics
    failures: tuple[UnresolvedFailure, ...]
    warcs: tuple[WarcArtifact, ...]
    index: IndexArtifact | None
    skip_errors: int


def run_fetch(
    settings: FetchSettings,
    *,
    client_factory: Optional[Callable] = None,
    download_fn=None,
    sleep=time.sleep,
) -> FetchResult:
    """Execute the annual fetch pipeline with bounded playback workers."""

    validate_date_range(settings.date_start, settings.date_end)
    factory = client_factory or make_client
    workers = PlaybackWorkers(
        factory,
        download_fn or download_exact,
        sleep=sleep,
        pace=download_fn is None,
        report=_print_line,
        max_workers=settings.playback_workers,
        starts_per_second=settings.playback_starts_per_second,
    )
    try:
        return _run_fetch(settings, workers=workers, sleep=sleep)
    finally:
        workers.close()


def _run_fetch(
    settings: FetchSettings,
    *,
    workers: PlaybackWorkers,
    sleep: Callable[[float], None],
) -> FetchResult:
    """Execute serial years with parallel playback and one WARC writer."""

    validate_date_range(settings.date_start, settings.date_end)
    layout = ArchiveLayout(settings.storage.workspace_directory, settings.archive_id)
    publisher = PublicationManager(settings.storage)
    if settings.reset_data and settings.storage.authority == "remote":
        publisher.reset_archive(layout)
    publisher.prepare(layout)
    reject_legacy_layout(layout)
    ensure_collection_dirs(layout)
    salvaged = salvage_collection_partials(layout)
    for item in salvaged:
        print(
            f"year {item.collection_id}: salvaged {item.path.name} "
            f"({item.record_count} records)",
            flush=True,
        )
    cleanup_temps(layout)
    if settings.storage.authority == "local":
        reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    run_id = init_run_id(layout)
    all_failures: list[UnresolvedFailure] = []
    first_year = int(settings.date_start[:4])
    last_year = int(settings.date_end[:4])
    print(
        f"archive {layout.archive_id}: collections {first_year}-{last_year}",
        flush=True,
    )
    print(
        f"playback policy: workers={settings.playback_workers}, "
        f"starts/second={settings.playback_starts_per_second:g}",
        flush=True,
    )

    run_skips_errors = 0
    for year, year_start, year_end in year_ranges(
        settings.date_start, settings.date_end
    ):
        result = _run_year(
            settings,
            layout=layout,
            year=year,
            date_start=year_start,
            date_end=year_end,
            run_id=run_id,
            workers=workers,
            sleep=sleep,
            publisher=publisher,
        )
        _accumulate_metrics(metrics, result.metrics)
        all_failures.extend(result.failures)
        run_skips_errors += result.skip_errors

    print(
        f"done: downloads={metrics.downloads} revisits={metrics.revisits} "
        f"payload-reuses={metrics.payload_reuses} "
        f"already-represented={metrics.local_reuses} "
        f"skips/errors={run_skips_errors}",
        flush=True,
    )
    return FetchResult(
        exit_code=0,
        layout=layout,
        metrics=metrics,
        failures=all_failures,
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
    sleep: Callable[[float], None],
    publisher: PublicationManager,
) -> _YearResult:
    """Acquire, resolve, publish, and record one yearly collection."""

    collection_id = f"{year:04d}"
    if settings.reset_data:
        reset_collection_data(layout, collection_id)
        print(f"year {year}: reset existing collection data", flush=True)
    else:
        publisher.materialize_collection(layout, collection_id)
    year_metrics = RunMetrics()
    year_failures: list[UnresolvedFailure] = []
    year_started = time.monotonic()
    print(f"year {year}: CDX query", flush=True)
    cdx_started = time.monotonic()
    year_cdx = fetch_year_cdx(
        layout,
        url_pattern=settings.url_pattern,
        year=year,
        date_start=date_start,
        date_end=date_end,
        run_id=run_id,
        sleep=sleep,
    )
    year_metrics.cdx_requests += int(year_cdx.query_meta.get("request_count", 1))
    year_metrics.cdx_duration_s += time.monotonic() - cdx_started
    year_failures.extend(year_cdx.failures)
    year_skips_errors = len(year_cdx.failures)
    _report_cdx_ingest_skips(year, year_cdx.failures)

    selected = _dedupe_captures(year_cdx.captures)
    year_metrics.selected += len(selected)

    inventory = inventory_collection(layout, collection_id)
    writer = CollectionWarcWriter(
        layout,
        collection_id,
        target_bytes=settings.warc_target_bytes,
    )
    grouped: dict[str, list[ParsedCapture]] = defaultdict(list)
    for capture in selected:
        grouped[capture.identity.urlkey].append(capture)
    groups = list(grouped.values())
    print(
        f"year {year}: {len(selected)} captures across {len(groups)} URLs",
        flush=True,
    )
    existing_identities = frozenset(inventory.identities)
    existing_representatives = dict(inventory.by_url_digest)
    skip_workers = tuple(
        not group_needs_playback(group, existing_identities)
        for group in groups
    )

    def process(group: Sequence[ParsedCapture]) -> UrlOutcome:
        return process_url_group(
            group,
            workers=workers,
            existing_identities=existing_identities,
            existing_representatives=existing_representatives,
        )

    try:
        for group_number, outcome in enumerate(
            iter_url_outcomes(groups, process, workers, skip_workers),
            start=1,
        ):
            year_metrics.playback_attempts += outcome.attempts
            year_metrics.playback_bytes += outcome.playback_bytes
            for category in outcome.categories:
                year_metrics.bump_attempt(category)
            for capture_outcome in outcome.captures:
                failure = _commit_capture_outcome(
                    capture_outcome,
                    inventory=inventory,
                    writer=writer,
                    metrics=year_metrics,
                )
                if failure is not None:
                    year_failures.append(failure)
                    year_skips_errors += 1
            _log_url_outcome(group_number, len(groups), outcome)
        close_started = time.monotonic()
        new_warcs = writer.close()
        year_metrics.warc_write_s += time.monotonic() - close_started
    except (KeyboardInterrupt, Exception):
        _finalize_interrupted_year(
            layout,
            collection_id,
            writer,
            warc_sizes=publisher.collection_warc_sizes(layout, collection_id),
        )
        raise

    for artifact in new_warcs:
        print(f"  published {artifact.relative_key}", flush=True)

    collection_index: IndexArtifact | None = None
    collection_warcs = list_collection_warcs(layout, collection_id)
    if collection_warcs:
        index_path = layout.collection_index(collection_id)
        if new_warcs or not index_path.is_file():
            idx_started = time.monotonic()
            collection_index = publish_collection_index(
                layout,
                collection_id,
                changed_warcs=[item.path for item in new_warcs],
                warc_sizes=publisher.collection_warc_sizes(layout, collection_id),
            )
            year_metrics.index_s += time.monotonic() - idx_started
        else:
            collection_index = index_artifact_from_path(layout, index_path)

    year_metrics.unresolved = len(year_failures)
    publisher.publish_collection(
        layout,
        collection_id,
        reset=settings.reset_data,
    )
    year_warcs = _manifest_warc_artifacts(layout, collection_id, publisher)
    write_run_record(
        layout,
        collection_id=collection_id,
        run_id=run_id,
        url_pattern=settings.url_pattern,
        date_start=str(year_cdx.query_meta["from"]),
        date_end=str(year_cdx.query_meta["to"]),
        query=year_cdx.query_meta,
        warcs=year_warcs,
        index=collection_index,
        metrics=year_metrics,
        failures=year_failures,
    )
    publisher.evict_collection(layout, collection_id)
    print(
        f"year {year} done: downloads={year_metrics.downloads} "
        f"payload-reuses={year_metrics.payload_reuses} "
        f"revisits={year_metrics.revisits} "
        f"already-represented={year_metrics.local_reuses} "
        f"skips/errors={year_skips_errors}",
        flush=True,
    )
    print(f"elapsed {_format_elapsed(time.monotonic() - year_started)}", flush=True)

    return _YearResult(
        metrics=year_metrics,
        failures=tuple(year_failures),
        warcs=tuple(year_warcs),
        index=collection_index,
        skip_errors=year_skips_errors,
    )


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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
            print(f"  published {artifact.relative_key}", flush=True)
    except Exception as error:  # noqa: BLE001 - best-effort crash salvage
        print(
            f"year {collection_id}: failed to finalize open WARC ({error})",
            flush=True,
        )
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
            print(f"  published {index.relative_key}", flush=True)
    except Exception as error:  # noqa: BLE001 - next run reconciles
        print(
            f"year {collection_id}: failed to rebuild index ({error})",
            flush=True,
        )


def _report_cdx_ingest_skips(
    year: int, failures: Sequence[UnresolvedFailure]
) -> None:
    """Print malformed CDX rows skipped at ingest."""

    malformed = [
        item for item in failures if item.category == FailureCategory.MALFORMED_CDX
    ]
    if not malformed:
        return
    print(
        f"year {year}: skipping {len(malformed)} malformed CDX row(s)",
        flush=True,
    )
    preview = 5
    for item in malformed[:preview]:
        url = item.identity.original_url
        if url and url != "-":
            print(f"  skip: {url}", flush=True)
            print(f"        {item.message}", flush=True)
        else:
            print(f"  skip: {item.message}", flush=True)
    remaining = len(malformed) - preview
    if remaining > 0:
        print(f"  ... and {remaining} more", flush=True)


def _playback_timing(outcome: CaptureOutcome) -> str:
    """Format playback elapsed time and retry count for a result row."""

    text = f"{outcome.elapsed_s:.1f}s"
    if outcome.attempts > 1:
        text += f", {outcome.attempts} attempts"
    return text


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


def _log_url_outcome(number: int, total: int, outcome: UrlOutcome) -> None:
    lines = [
        f"{number}/{total} {_terminal_safe(outcome.url)}",
        "  Capture              Digest  Result",
    ]
    for capture in outcome.captures:
        detail, style = _format_capture_outcome(capture)
        lines.append(
            f"  {_timestamp_link(capture.identity)}  "
            f"{_terminal_safe(capture.identity.payload_digest[-6:]):>6}  "
            f"{_style_result(detail, style)}"
        )
    _print_line("\n".join(lines))


def _format_capture_outcome(outcome: CaptureOutcome) -> tuple[str, str]:
    """Derive terminal presentation from a semantic capture result."""

    if outcome.kind is CaptureKind.EXISTING:
        return "Ignored [already represented]", "dim"
    if outcome.kind is CaptureKind.REVISIT:
        return "Revisit", "revisit"
    if outcome.kind is CaptureKind.EMPTY:
        return "Empty payload", "revisit"
    if outcome.kind is CaptureKind.FAILURE:
        assert outcome.failure is not None
        reason = outcome.failure.category.value.replace("_", " ")
        if is_invalid_uri_payload_digest(outcome.identity.payload_digest):
            reason = "invalid URI"
        detail = f"Ignored [{reason}]"
        if outcome.attempts:
            detail += f" ({_playback_timing(outcome)})"
        return detail, "warning"
    if outcome.kind is CaptureKind.SLASH_REDIRECT:
        detail = "Slash redirect"
        if outcome.attempts:
            detail += f" ({_playback_timing(outcome)})"
        return detail, "revisit"

    assert outcome.kind is CaptureKind.DOWNLOADED
    assert outcome.playback is not None
    extra = _playback_timing(outcome)
    if outcome.playback.substituted:
        extra += ", substituted"
    if not outcome.playback.digest_matched:
        extra += ", digest mismatch kept"
    style = (
        "warning"
        if outcome.playback.substituted or not outcome.playback.digest_matched
        else "success"
    )
    return f"Downloaded ({extra})", style


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


def _manifest_warc_artifacts(
    layout: ArchiveLayout,
    collection_id: str,
    publisher: PublicationManager,
) -> list[WarcArtifact]:
    """Summarize committed WARCs from manifest metadata and CDXJ counts."""

    collection = publisher.manifest.collections.get(collection_id)
    index_path = layout.collection_index(collection_id)
    if collection is None or not index_path.is_file():
        return []
    capture_counts: dict[str, int] = defaultdict(int)
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            filename = parse_cdxj_line(line)[2]["filename"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(filename, str):
            capture_counts[filename] += 1
    return [
        WarcArtifact(
            relative_key=item.key,
            collection_id=collection_id,
            sequence=int(
                Path(item.key).name.removesuffix(".warc.gz").rsplit("-", 1)[1]
            ),
            path=layout.root / item.key,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            record_count=capture_counts[Path(item.key).name] + 1,
        )
        for item in collection.warcs
    ]


def _accumulate_metrics(total: RunMetrics, current: RunMetrics) -> None:
    """Add one collection's metrics to the invocation totals."""

    for name in (
        "cdx_requests",
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
    storage: StorageConfig,
    warc_target_bytes: int = DEFAULT_WARC_TARGET_BYTES,
    playback_workers: int = 4,
    playback_starts_per_second: float = 20.0,
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
        storage=storage,
        warc_target_bytes=warc_target_bytes,
        playback_workers=playback_workers,
        playback_starts_per_second=playback_starts_per_second,
    )
