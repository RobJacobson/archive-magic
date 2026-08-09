"""Year-by-year fetch orchestration."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .cdx import (
    DEFAULT_DATE_START,
    fetch_year_cdx,
    init_run_id,
    make_client,
    parse_date_bound,
    validate_date_range,
    years_in_range,
)
from .collection import (
    ArchiveLayout,
    archive_layout,
    cleanup_temps,
    ensure_collection_dirs,
    index_artifact_from_path,
    list_collection_warcs,
    reject_legacy_layout,
    warc_artifact_from_path,
    write_run_record,
)
from .index import (
    publish_collection_index,
    reconcile_missing_indexes,
)
from .models import (
    MAX_PLAYBACK_ATTEMPTS,
    MISSING_CDX_PAYLOAD_DIGEST,
    CaptureIdentity,
    FailureCategory,
    IndexArtifact,
    ParsedCapture,
    PlaybackResult,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
    current_utc_cdx_timestamp,
    is_invalid_uri_payload_digest,
    wayback_url,
)
from .retry import parse_retry_after
from .warc import (
    CollectionInventory,
    CollectionWarcWriter,
    StoredResponse,
    classify_playback_error,
    count_warc_records,
    download_exact_for_identity,
    inventory_collection,
    revisit_from_stored,
    stored_from_playback,
)


_RESULT_STYLES = {
    "success": "32",
    "revisit": "36",
    "warning": "33",
    "error": "1;31",
    "dim": "2",
}
_OSC = "\033]8;;"
_ST = "\033\\"


def _terminal_output_enabled() -> bool:
    """Return whether stdout can safely render terminal escape sequences."""

    return bool(
        getattr(sys.stdout, "isatty", lambda: False)()
        and os.environ.get("TERM", "") != "dumb"
    )


def _terminal_safe(value: str) -> str:
    """Prevent control characters in CDX data from escaping a terminal field."""

    return "".join(char if char.isprintable() else "?" for char in value)


def _capture_link(identity: CaptureIdentity, *, enabled: bool | None = None) -> str:
    """Render a compact OSC 8 link, or its plain-text label when unsupported."""

    display_url = identity.original_url
    lowered = display_url.lower()
    for prefix in ("http://www.", "https://www."):
        if lowered.startswith(prefix):
            display_url = display_url[: prefix.index("www.")] + display_url[len(prefix) :]
            break
    label = _terminal_safe(f"{identity.timestamp}/{display_url}")
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


def _log_capture(
    number: int,
    total: int,
    identity: CaptureIdentity,
    result: str,
    *,
    style: str,
) -> None:
    width = len(str(total))
    print(
        f"{number:{width}d}/{total}: {_capture_link(identity)} "
        f"{_style_result(result, style)}",
        flush=True,
    )


@dataclass(frozen=True)
class FetchSettings:
    """Validated CLI inputs for one fetch run."""

    url_pattern: str
    date_start: str
    date_end: str
    archives_root: Optional[Path] = None


@dataclass
class FetchResult:
    """Outcome of one fetch run."""

    exit_code: int
    layout: ArchiveLayout
    metrics: RunMetrics
    failures: list[UnresolvedFailure]


def run_fetch(
    settings: FetchSettings,
    *,
    client_factory: Optional[Callable] = None,
    download_fn=None,
    sleep=time.sleep,
) -> FetchResult:
    """Execute the annual fetch pipeline with one persistent playback client."""

    validate_date_range(settings.date_start, settings.date_end)
    factory = client_factory or make_client
    owner = factory()
    enter = getattr(owner, "__enter__", None)
    client = enter() if callable(enter) else owner
    if client is None:
        client = owner
    try:
        return _run_fetch(settings, client=client, download_fn=download_fn, sleep=sleep)
    finally:
        exit_fn = getattr(owner, "__exit__", None)
        if callable(exit_fn):
            exit_fn(None, None, None)
        else:
            close = getattr(owner, "close", None)
            if callable(close):
                close()


def _run_fetch(
    settings: FetchSettings,
    *,
    client,
    download_fn,
    sleep: Callable[[float], None],
) -> FetchResult:
    """Execute the serial year-by-year work with an open playback client."""

    validate_date_range(settings.date_start, settings.date_end)
    layout = archive_layout(settings.url_pattern, settings.archives_root)
    reject_legacy_layout(layout)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    run_id = init_run_id(layout)
    all_failures: list[UnresolvedFailure] = []

    years = years_in_range(settings.date_start, settings.date_end)
    print(
        f"archive {layout.archive_id}: collections {years[0]}-{years[-1]}",
        flush=True,
    )
    print("playback policy: serial, three attempts maximum", flush=True)

    run_skips_errors = 0
    for year in years:
        collection_id = f"{year:04d}"
        year_metrics = RunMetrics()
        year_failures: list[UnresolvedFailure] = []
        year_started = time.monotonic()
        print(f"year {year}: CDX query", flush=True)
        cdx_started = time.monotonic()
        year_cdx = fetch_year_cdx(
            layout,
            url_pattern=settings.url_pattern,
            year=year,
            date_start=settings.date_start,
            date_end=settings.date_end,
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
        writer = CollectionWarcWriter(layout, collection_id)
        year_download_fn = download_fn or download_exact_for_identity
        print(f"year {year}: {len(selected)} selected", flush=True)
        total = len(selected)
        for number, capture in enumerate(selected, start=1):
            identity = capture.identity
            if inventory.contains(identity):
                year_metrics.local_reuses += 1
                year_metrics.represented += 1
                _log_capture(
                    number,
                    total,
                    identity,
                    "Already represented",
                    style="dim",
                )
                continue

            key = _groupable_digest_key(identity)
            stored = (
                inventory.lookup_representative(
                    key[0], key[1], not_after_timestamp=identity.timestamp
                )
                if key is not None
                else None
            )
            if stored is not None:
                _write_revisit(
                    identity=identity,
                    stored=stored,
                    inventory=inventory,
                    writer=writer,
                    metrics=year_metrics,
                )
                _log_capture(number, total, identity, "Revisit", style="revisit")
                continue

            result, failure = _download_with_retries(
                client,
                identity,
                download_fn=year_download_fn,
                metrics=year_metrics,
                sleep=sleep,
                number=number,
                total=total,
            )
            if failure is not None:
                year_failures.append(failure)
                year_skips_errors += 1
                continue
            assert result is not None
            write_started = time.monotonic()
            writer.write_playback(result)
            year_metrics.warc_write_s += time.monotonic() - write_started
            year_metrics.downloads += 1
            year_metrics.represented += 1
            if not result.digest_matched:
                year_metrics.digest_mismatch_accepted += 1
            inventory.identities.add(identity)
            if result.digest_matched and key is not None:
                inventory.remember_representative(stored_from_playback(result))

        close_started = time.monotonic()
        new_warcs = writer.close()
        year_metrics.warc_write_s += time.monotonic() - close_started
        for artifact in new_warcs:
            print(f"  published {artifact.relative_key}", flush=True)

        collection_index: IndexArtifact | None = None
        collection_warcs = list_collection_warcs(layout, collection_id)
        if collection_warcs:
            index_path = layout.collection_index(collection_id)
            if new_warcs or not index_path.is_file():
                idx_started = time.monotonic()
                collection_index = publish_collection_index(layout, collection_id)
                year_metrics.index_s += time.monotonic() - idx_started
            else:
                collection_index = index_artifact_from_path(layout, index_path)

        year_metrics.unresolved = len(year_failures)
        year_warcs = _collect_warc_artifacts(
            layout, collection_id, new_warcs
        )
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
        _accumulate_metrics(metrics, year_metrics)
        all_failures.extend(year_failures)
        run_skips_errors += year_skips_errors
        print(
            f"year {year} done: downloads={year_metrics.downloads} "
            f"revisits={year_metrics.revisits} "
            f"already-represented={year_metrics.local_reuses} "
            f"skips/errors={year_skips_errors}",
            flush=True,
        )
        print(f"elapsed {_format_elapsed(time.monotonic() - year_started)}", flush=True)

    print(
        f"done: downloads={metrics.downloads} revisits={metrics.revisits} "
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


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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


def _groupable_digest_key(
    identity: CaptureIdentity,
) -> tuple[str, str] | None:
    """Return ``(urlkey, IA digest)`` when this capture can share a payload."""

    if identity.payload_digest == MISSING_CDX_PAYLOAD_DIGEST:
        return None
    if (
        identity.status_token.isdigit()
        and 300 <= int(identity.status_token) < 400
    ):
        return None
    return (identity.urlkey, identity.payload_digest)


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


def _download_with_retries(
    client,
    identity: CaptureIdentity,
    *,
    download_fn,
    metrics: RunMetrics,
    sleep: Callable[[float], None],
    number: int = 1,
    total: int = 1,
) -> tuple[PlaybackResult | None, UnresolvedFailure | None]:
    """Download one capture synchronously with a small bounded retry loop."""

    if is_invalid_uri_payload_digest(identity.payload_digest):
        _log_capture(
            number,
            total,
            identity,
            "Skipped (invalid URI)",
            style="warning",
        )
        return None, UnresolvedFailure(
            identity=identity,
            category=FailureCategory.UNAVAILABLE,
            message="CDX digest is IA Invalid URI stub",
        )

    for attempt in range(1, MAX_PLAYBACK_ATTEMPTS + 1):
        started = time.monotonic()
        metrics.playback_attempts += 1
        try:
            result = download_fn(client, identity)
        except Exception as error:  # noqa: BLE001 - network boundary
            category, retryable = classify_playback_error(error)
            metrics.bump_attempt(category.value)
            if retryable and attempt < MAX_PLAYBACK_ATTEMPTS:
                delay = _retry_after_from_error(error) or float(
                    5 * (2 ** (attempt - 1))
                )
                _log_capture(
                    number,
                    total,
                    identity,
                    f"Warning: {type(error).__name__}; retrying in {delay:g}s "
                    f"(attempt {attempt}/{MAX_PLAYBACK_ATTEMPTS})",
                    style="warning",
                )
                sleep(delay)
                continue
            _log_capture(
                number,
                total,
                identity,
                f"Error: {type(error).__name__}; continuing",
                style="error",
            )
            return None, UnresolvedFailure(
                identity=identity,
                category=category,
                message=str(error) or type(error).__name__,
            )
        metrics.playback_bytes += len(result.body)
        duration = time.monotonic() - started
        detail = (
            f"Downloaded ({duration:.1f}s; digest mismatch kept)"
            if not result.digest_matched
            else f"Downloaded ({duration:.1f}s)"
        )
        _log_capture(
            number,
            total,
            identity,
            detail,
            style="warning" if not result.digest_matched else "success",
        )
        return result, None
    raise AssertionError("playback retry loop did not terminate")


def _iter_error_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        nested = getattr(current, "cause", None)
        current = (
            nested
            if isinstance(nested, BaseException)
            else current.__cause__ or current.__context__
        )


def _retry_after_from_error(error: BaseException) -> float | None:
    for candidate in _iter_error_chain(error):
        values = [getattr(candidate, "retry_after", None)]
        response = getattr(candidate, "response", None)
        headers = getattr(response, "headers", None) or {}
        values.append(headers.get("Retry-After") or headers.get("retry-after"))
        for value in values:
            parsed = parse_retry_after(value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


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


def _collect_warc_artifacts(
    layout: ArchiveLayout,
    collection_id: str,
    new_warcs: Sequence[WarcArtifact],
) -> list[WarcArtifact]:
    known = {item.relative_key: item for item in new_warcs}
    artifacts: list[WarcArtifact] = []
    for path in list_collection_warcs(layout, collection_id):
        rel = path.relative_to(layout.root).as_posix()
        if rel in known:
            artifacts.append(known[rel])
        else:
            artifacts.append(
                warc_artifact_from_path(
                    layout,
                    path,
                    record_count=count_warc_records(path),
                )
            )
    return artifacts


def _accumulate_metrics(total: RunMetrics, current: RunMetrics) -> None:
    """Add one collection's metrics to the invocation totals."""

    for name in (
        "cdx_requests",
        "cdx_duration_s",
        "playback_attempts",
        "playback_bytes",
        "local_reuses",
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
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    archives_root: Optional[Path] = None,
) -> FetchSettings:
    """Validate CLI-facing inputs into settings."""

    start = parse_date_bound(
        date_start, default=DEFAULT_DATE_START, bound="start"
    )
    end = parse_date_bound(
        date_end, default=current_utc_cdx_timestamp(), bound="end"
    )
    validate_date_range(start, end)
    return FetchSettings(
        url_pattern=url_pattern.strip(),
        date_start=start,
        date_end=end,
        archives_root=archives_root,
    )
