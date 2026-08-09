"""Year-by-year fetch orchestration."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from email.utils import mktime_tz, parsedate_tz
from pathlib import Path
from typing import Callable, Optional, Sequence

from .cdx import (
    DEFAULT_DATE_START,
    fetch_year_cdx,
    init_run_source,
    make_client,
    parse_date_bound,
    validate_date_range,
    years_in_range,
)
from .collection import (
    CollectionLayout,
    cleanup_temps,
    collection_layout,
    ensure_collection_dirs,
    index_artifact_from_path,
    list_all_warcs,
    list_annual_indexes,
    list_year_warcs,
    load_failures,
    require_current_collection_schema,
    warc_artifact_from_path,
    write_failures,
    write_manifest,
)
from .index import (
    publish_annual_index,
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
from .warc import (
    AnnualInventory,
    StoredResponse,
    YearWarcWriter,
    classify_playback_error,
    count_warc_records,
    download_exact_for_identity,
    inventory_year,
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

    label = _terminal_safe(f"{identity.timestamp}/{identity.original_url}")
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
    layout: CollectionLayout
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
    layout = collection_layout(settings.url_pattern, settings.archives_root)
    require_current_collection_schema(layout)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    source_dir = init_run_source(layout)
    run_id = source_dir.name
    # Retain unresolved failures from prior runs until they are represented.
    all_failures: list[UnresolvedFailure] = list(load_failures(layout))
    all_warcs = _collect_warc_artifacts(layout, ())
    annual_indexes: list[IndexArtifact] = _existing_annual_indexes(layout)

    represented_identities = _represented_failure_identities(layout, all_failures)

    years = years_in_range(settings.date_start, settings.date_end)
    print(
        f"collection {layout.collection_id}: years {years[0]}-{years[-1]}",
        flush=True,
    )
    print("playback policy: serial, three attempts maximum", flush=True)

    for year in years:
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
        metrics.cdx_requests += int(year_cdx.query_meta.get("request_count", 1))
        metrics.cdx_duration_s += time.monotonic() - cdx_started
        all_failures.extend(year_cdx.failures)
        _report_cdx_ingest_skips(year, year_cdx.failures)

        selected = _dedupe_captures(year_cdx.captures)
        metrics.selected += len(selected)

        inventory = inventory_year(layout, year)
        represented_identities.update(inventory.identities)
        writer = YearWarcWriter(layout, year)
        year_download_fn = download_fn or download_exact_for_identity
        print(f"year {year}: {len(selected)} selected", flush=True)
        total = len(selected)
        for number, capture in enumerate(selected, start=1):
            identity = capture.identity
            if inventory.contains(identity):
                metrics.local_reuses += 1
                metrics.represented += 1
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
                    metrics=metrics,
                )
                represented_identities.add(identity)
                _log_capture(number, total, identity, "Revisit", style="revisit")
                continue

            result, failure = _download_with_retries(
                client,
                identity,
                download_fn=year_download_fn,
                metrics=metrics,
                sleep=sleep,
                number=number,
                total=total,
            )
            if failure is not None:
                all_failures.append(failure)
                continue
            assert result is not None
            write_started = time.monotonic()
            writer.write_playback(result)
            metrics.warc_write_s += time.monotonic() - write_started
            metrics.downloads += 1
            metrics.represented += 1
            if not result.digest_matched:
                metrics.digest_mismatch_accepted += 1
            inventory.identities.add(identity)
            represented_identities.add(identity)
            if result.digest_matched and key is not None:
                inventory.remember_representative(stored_from_playback(result))

        close_started = time.monotonic()
        new_warcs = writer.close()
        metrics.warc_write_s += time.monotonic() - close_started
        for artifact in new_warcs:
            all_warcs.append(artifact)
            print(f"  published {artifact.relative_key}", flush=True)
            idx_started = time.monotonic()
            annual = publish_annual_index(
                layout,
                year,
                new_warcs=[artifact.path],
            )
            metrics.index_s += time.monotonic() - idx_started
            if annual is not None:
                annual_indexes = _replace_annual_index(annual_indexes, annual)

        # Ensure annual index exists even if no new warcs this year.
        if list_year_warcs(layout, year):
            annual = publish_annual_index(layout, year)
            if annual is not None:
                annual_indexes = _replace_annual_index(annual_indexes, annual)

        coll = publish_collection_index(layout)
        _publish_state(
            layout,
            settings=settings,
            run_source=f"sources/{run_id}",
            warcs=all_warcs,
            annual_indexes=_merge_annual_indexes(layout, annual_indexes),
            collection_index=coll,
            metrics=metrics,
            failures=all_failures,
            represented_identities=represented_identities,
            final=False,
        )

    coll = publish_collection_index(layout)
    final_warcs = all_warcs
    final_annual = _merge_annual_indexes(layout, annual_indexes)
    unresolved = _publish_state(
        layout,
        settings=settings,
        run_source=f"sources/{run_id}",
        warcs=final_warcs,
        annual_indexes=final_annual,
        collection_index=coll or (
            index_artifact_from_path(layout, layout.collection_index)
            if layout.collection_index.is_file()
            else None
        ),
        metrics=metrics,
        failures=all_failures,
        represented_identities=represented_identities,
        final=True,
    )
    status = "complete" if metrics.unresolved == 0 else "partial"
    print(
        f"done: status={status} represented={metrics.represented} "
        f"unresolved={metrics.unresolved}",
        flush=True,
    )
    exit_code = 0 if status == "complete" else 1
    return FetchResult(
        exit_code=exit_code,
        layout=layout,
        metrics=metrics,
        failures=unresolved,
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
    inventory: AnnualInventory,
    writer: YearWarcWriter,
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
            parsed = _parse_retry_after(value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _parse_retry_after(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        retry_date = parsedate_tz(value)
        if retry_date is None:
            return None
        seconds = float(mktime_tz(retry_date) - time.time())
    return seconds if seconds > 0 else None


def _represented_failure_identities(
    layout: CollectionLayout,
    failures: Sequence[UnresolvedFailure],
) -> set[CaptureIdentity]:
    """Resolve stale failures by inventorying only the years they mention."""

    identities: set[CaptureIdentity] = set()
    inventories: dict[int, AnnualInventory] = {}
    for failure in failures:
        prefix = failure.identity.timestamp[:4]
        if not prefix.isdigit():
            continue
        year = int(prefix)
        inventory = inventories.get(year)
        if inventory is None:
            inventory = inventory_year(layout, year)
            inventories[year] = inventory
        if inventory.contains(failure.identity):
            identities.add(failure.identity)
    return identities


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
    layout: CollectionLayout,
    new_warcs: Sequence[WarcArtifact],
) -> list[WarcArtifact]:
    known = {item.relative_key: item for item in new_warcs}
    artifacts: list[WarcArtifact] = []
    for path in list_all_warcs(layout):
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


def _existing_annual_indexes(layout: CollectionLayout) -> list[IndexArtifact]:
    return [
        index_artifact_from_path(layout, path)
        for _, path in list_annual_indexes(layout)
    ]


def _replace_annual_index(
    annual_indexes: list[IndexArtifact],
    annual: IndexArtifact,
) -> list[IndexArtifact]:
    return [
        item
        for item in annual_indexes
        if item.relative_key != annual.relative_key
    ] + [annual]


def _merge_annual_indexes(
    layout: CollectionLayout,
    annual_indexes: Sequence[IndexArtifact],
) -> list[IndexArtifact]:
    """Prefer in-run artifacts, then include every on-disk annual index."""

    by_key = {item.relative_key: item for item in annual_indexes}
    for item in _existing_annual_indexes(layout):
        by_key.setdefault(item.relative_key, item)
    return sorted(by_key.values(), key=lambda item: item.relative_key)


def _publish_state(
    layout: CollectionLayout,
    *,
    settings: FetchSettings,
    run_source: str,
    warcs: Sequence[WarcArtifact],
    annual_indexes: Sequence[IndexArtifact],
    collection_index: Optional[IndexArtifact],
    metrics: RunMetrics,
    failures: Sequence[UnresolvedFailure],
    represented_identities: set[CaptureIdentity],
    final: bool,
) -> list[UnresolvedFailure]:
    # Collapse failures by identity; later details replace stale ones.
    # Drop any identity represented by the annual inventories processed in memory.
    by_id: dict[CaptureIdentity, UnresolvedFailure] = {}
    for failure in failures:
        by_id[failure.identity] = failure

    unresolved_list = [
        failure
        for failure in by_id.values()
        if failure.identity not in represented_identities
    ]

    metrics.unresolved = len(unresolved_list)
    write_failures(layout, unresolved_list)
    write_manifest(
        layout,
        url_pattern=settings.url_pattern,
        status="complete" if final and metrics.unresolved == 0 else "partial",
        run_source_relative=run_source,
        warcs=warcs,
        annual_indexes=annual_indexes,
        collection_index=collection_index,
        metrics=metrics,
    )
    return unresolved_list


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
