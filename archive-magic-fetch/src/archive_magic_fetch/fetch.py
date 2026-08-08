"""Year-by-year fetch orchestration."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
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
    list_year_warcs,
    load_failures,
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
    MAX_CONNECTIONS,
    MISSING_CDX_PAYLOAD_DIGEST,
    PLAYBACK_REQUESTS_PER_SECOND,
    CaptureIdentity,
    FailureCategory,
    IndexArtifact,
    ParsedCapture,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
    current_utc_cdx_timestamp,
)
from .scheduler import (
    JobFailure,
    JobSuccess,
    PlaybackProgress,
    PlaybackScheduler,
    failure_from_job,
)
from .warc import (
    CollectionInventory,
    StoredResponse,
    YearWarcWriter,
    count_warc_records,
    download_exact_for_identity,
    inventory_collection,
    revisit_from_stored,
    stored_from_playback,
)


@dataclass(frozen=True)
class FetchSettings:
    """Validated CLI inputs for one fetch run."""

    url_pattern: str
    date_start: str
    date_end: str
    archives_root: Optional[Path] = None
    # When True, reject bodies whose digest disagrees with CDX. Default False:
    # keep imperfect IA payloads and still allow them as revisit representatives.
    strict_digests: bool = False


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
    """Execute the five-stage annual fetch pipeline."""

    validate_date_range(settings.date_start, settings.date_end)
    layout = collection_layout(settings.url_pattern, settings.archives_root)
    ensure_collection_dirs(layout)
    cleanup_temps(layout)
    reconcile_missing_indexes(layout)

    metrics = RunMetrics()
    source_dir = init_run_source(layout)
    run_id = source_dir.name
    # Retain unresolved failures from prior runs until they are represented.
    all_failures: list[UnresolvedFailure] = list(load_failures(layout))
    all_warcs: list[WarcArtifact] = []
    annual_indexes: list[IndexArtifact] = _existing_annual_indexes(layout)

    # Baseline inventory of previously published WARCs.
    inventory = inventory_collection(layout)
    client_factory = client_factory or make_client

    years = years_in_range(settings.date_start, settings.date_end)
    print(
        f"collection {layout.collection_id}: years {years[0]}-{years[-1]}",
        flush=True,
    )
    digest_policy = "strict" if settings.strict_digests else "permissive"
    print(
        f"playback policy: PLAYBACK_REQUESTS_PER_SECOND={PLAYBACK_REQUESTS_PER_SECOND} "
        f"MAX_CONNECTIONS={MAX_CONNECTIONS} digests={digest_policy}",
        flush=True,
    )

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

        # Existing exact captures: no network.
        to_schedule: list[ParsedCapture] = []
        for capture in selected:
            if inventory.contains(capture.identity):
                metrics.local_reuses += 1
                metrics.represented += 1
            else:
                to_schedule.append(capture)

        work_plan = _plan_year_work(to_schedule, inventory)
        print(
            f"year {year}: {len(selected)} selected, "
            f"{len(work_plan.network_jobs)} to download, "
            f"{len(work_plan.revisit_jobs)} revisits ready",
            flush=True,
        )

        writer = YearWarcWriter(layout, year)
        year_index = inventory.year_index(year)

        # Write revisits that already have an earlier successful representative.
        for identity, stored in work_plan.revisit_jobs:
            writer.write_revisit(revisit_from_stored(identity, stored))
            inventory.identities.add(identity)
            metrics.revisits += 1
            metrics.represented += 1

        # Schedule remaining network jobs via central scheduler.
        if work_plan.network_jobs:
            if download_fn is not None:
                year_download_fn = download_fn
            else:
                strict = settings.strict_digests

                def year_download_fn(client, identity, _strict=strict):
                    return download_exact_for_identity(
                        client, identity, strict_digests=_strict
                    )

            year_failures = _run_year_downloads(
                layout=layout,
                year=year,
                jobs=work_plan.network_jobs,
                pending_candidates=work_plan.pending_candidates,
                inventory=inventory,
                year_index=year_index,
                writer=writer,
                metrics=metrics,
                client_factory=client_factory,
                download_fn=year_download_fn,
            )
            all_failures.extend(year_failures)

        new_warcs = writer.close()
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
            warcs=_collect_warc_artifacts(layout, all_warcs),
            annual_indexes=_merge_annual_indexes(layout, annual_indexes),
            collection_index=coll,
            metrics=metrics,
            failures=all_failures,
            final=False,
        )

    coll = publish_collection_index(layout)
    final_warcs = _collect_warc_artifacts(layout, all_warcs)
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


@dataclass
class _YearPlan:
    network_jobs: list[CaptureIdentity]
    revisit_jobs: list[tuple[CaptureIdentity, StoredResponse]]
    # Same-key candidates waiting behind the oldest network/active capture:
    pending_candidates: dict[tuple[str, str], list[CaptureIdentity]]


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


def _plan_year_work(
    captures: Sequence[ParsedCapture],
    inventory: CollectionInventory,
) -> _YearPlan:
    """Plan network and revisit work using the collection-wide representative map.

    Groupable captures share ``(urlkey, IA digest)``. The oldest unresolved
    member of each group becomes the only network job; later members become
    revisit candidates after that representative succeeds. When an older
    successful representative already exists (possibly from a prior year),
    every member is an immediate revisit. Redirects and digest-less rows always
    download individually.
    """

    network: list[CaptureIdentity] = []
    revisits: list[tuple[CaptureIdentity, StoredResponse]] = []
    pending: dict[tuple[str, str], list[CaptureIdentity]] = {}

    redirect_or_nodigest: list[ParsedCapture] = []
    groups: dict[tuple[str, str], list[ParsedCapture]] = defaultdict(list)

    for capture in captures:
        key = _groupable_digest_key(capture.identity)
        if key is None:
            redirect_or_nodigest.append(capture)
            continue
        groups[key].append(capture)

    for key, group in groups.items():
        group = sorted(group, key=lambda c: c.identity.sort_key())
        earliest = group[0].identity
        stored = inventory.lookup_representative(
            key[0],
            key[1],
            not_after_timestamp=earliest.timestamp,
        )
        if stored is not None:
            for capture in group:
                if not inventory.contains(capture.identity):
                    revisits.append((capture.identity, stored))
            continue
        # No reusable prior payload yet: download earliest; rest wait for
        # success or failure of that representative (then promote).
        network.append(earliest)
        deferred = [
            capture.identity
            for capture in group[1:]
            if not inventory.contains(capture.identity)
        ]
        if deferred:
            pending[key] = deferred

    for capture in redirect_or_nodigest:
        network.append(capture.identity)

    network = sorted(network, key=lambda identity: identity.sort_key())
    revisits.sort(key=lambda item: item[0].sort_key())
    return _YearPlan(
        network_jobs=network,
        revisit_jobs=revisits,
        pending_candidates=pending,
    )


def _write_revisit(
    *,
    identity: CaptureIdentity,
    stored: StoredResponse,
    inventory: CollectionInventory,
    writer: YearWarcWriter,
    metrics: RunMetrics,
    http_status_code: int | None = None,
    http_headers: tuple[tuple[str, str], ...] | None = None,
) -> None:
    writer.write_revisit(
        revisit_from_stored(
            identity,
            stored,
            http_status_code=http_status_code,
            http_headers=http_headers,
        )
    )
    inventory.identities.add(identity)
    metrics.revisits += 1
    metrics.represented += 1


def _run_year_downloads(
    *,
    layout: CollectionLayout,
    year: int,
    jobs: Sequence[CaptureIdentity],
    pending_candidates: dict[tuple[str, str], list[CaptureIdentity]],
    inventory: CollectionInventory,
    year_index,
    writer: YearWarcWriter,
    metrics: RunMetrics,
    client_factory: Callable,
    download_fn,
) -> list[UnresolvedFailure]:
    """Download ordered candidate identities and write responses/revisits."""

    remaining_groups: dict[tuple[str, str], list[CaptureIdentity]] = {
        key: list(members) for key, members in pending_candidates.items()
    }

    # Only one active job per groupable key may run: the oldest unresolved
    # candidate. Deferred same-key members wait in remaining_groups.
    active: list[CaptureIdentity] = []
    for identity in jobs:
        if inventory.contains(identity):
            continue
        key = _groupable_digest_key(identity)
        if key is not None:
            stored = inventory.lookup_representative(
                key[0],
                key[1],
                not_after_timestamp=identity.timestamp,
            )
            if stored is not None:
                _write_revisit(
                    identity=identity,
                    stored=stored,
                    inventory=inventory,
                    writer=writer,
                    metrics=metrics,
                )
                for deferred in remaining_groups.pop(key, []):
                    if inventory.contains(deferred):
                        continue
                    _write_revisit(
                        identity=deferred,
                        stored=stored,
                        inventory=inventory,
                        writer=writer,
                        metrics=metrics,
                    )
                continue
        active.append(identity)
    active = sorted(active, key=lambda i: i.sort_key())

    if not active:
        return []

    scheduler_kwargs = {
        "client_factory": client_factory,
        "identities": active,
        "metrics": metrics,
        "progress": PlaybackProgress(total=len(active)),
    }
    if download_fn is not None:
        scheduler_kwargs["download_fn"] = download_fn
    scheduler = PlaybackScheduler(**scheduler_kwargs)

    failures: list[UnresolvedFailure] = []
    import threading

    thread = threading.Thread(target=scheduler.run, name="playback-scheduler")
    thread.start()
    writer_error: BaseException | None = None
    try:
        for item in scheduler.results():
            try:
                if isinstance(item, JobSuccess):
                    to_enqueue = _handle_download_success(
                        item=item,
                        layout=layout,
                        year=year,
                        inventory=inventory,
                        year_index=year_index,
                        writer=writer,
                        metrics=metrics,
                        remaining_groups=remaining_groups,
                    )
                    for next_id in to_enqueue:
                        scheduler.note_additional_work()
                        scheduler.enqueue(next_id)
                else:
                    assert isinstance(item, JobFailure)
                    identity = item.identity
                    key = _groupable_digest_key(identity)
                    if key is not None:
                        candidates = remaining_groups.get(key)
                        if candidates:
                            next_id = candidates.pop(0)
                            remaining_groups[key] = candidates
                            scheduler.note_additional_work()
                            scheduler.enqueue(next_id)
                    failures.append(failure_from_job(item))
            except BaseException as error:  # noqa: BLE001 - stop scheduler
                writer_error = error
                scheduler.stop()
                scheduler.acknowledge()
                break
            else:
                scheduler.acknowledge()
    finally:
        scheduler.stop()
        # Drain results so workers blocked on the bounded queue can exit.
        while thread.is_alive():
            try:
                while True:
                    scheduler._results.get_nowait()
            except Exception:  # noqa: BLE001 - Empty or shutdown
                pass
            thread.join(timeout=0.05)
        thread.join(timeout=5)

    if writer_error is not None:
        raise writer_error

    # Remaining deferred members never obtained a representative.
    for key, members in remaining_groups.items():
        for identity in members:
            if inventory.contains(identity):
                continue
            failures.append(
                UnresolvedFailure(
                    identity=identity,
                    category=FailureCategory.UNAVAILABLE,
                    message="representative capture failed; no reusable payload",
                )
            )
    return failures


def _handle_download_success(
    *,
    item: JobSuccess,
    layout: CollectionLayout,
    year: int,
    inventory: CollectionInventory,
    year_index,
    writer: YearWarcWriter,
    metrics: RunMetrics,
    remaining_groups: dict[tuple[str, str], list[CaptureIdentity]],
) -> list[CaptureIdentity]:
    """Write a successful download. Return identities that still need network."""

    result = item.result
    if inventory.contains(result.identity):
        metrics.local_reuses += 1
        return []
    key = _groupable_digest_key(result.identity)
    # Another capture may already have established a reusable representative
    # (for example a prior-year success loaded from inventory after this job
    # was scheduled). Prefer a revisit over a duplicate full payload.
    if key is not None:
        stored = inventory.lookup_representative(
            key[0],
            key[1],
            not_after_timestamp=result.identity.timestamp,
        )
        if stored is not None:
            _write_revisit(
                identity=result.identity,
                stored=stored,
                inventory=inventory,
                writer=writer,
                metrics=metrics,
                http_status_code=result.status_code,
                http_headers=(),
            )
            return []

    writer.write_playback(result)
    metrics.downloads += 1
    metrics.represented += 1
    if not result.digest_matched:
        metrics.digest_mismatch_accepted += 1
    inventory.identities.add(result.identity)
    relative = _current_relative_key(layout, year, writer)
    stored_resp = stored_from_playback(layout, year, result, relative)
    year_index.by_identity[result.identity] = stored_resp

    if key is not None:
        deferred = [
            identity
            for identity in remaining_groups.pop(key, [])
            if not inventory.contains(identity)
        ]
        # Successful exact playback seeds the representative map — including
        # permissive IA/local digest mismatches — so later same-key captures
        # become revisits. Terminal failures never reach this path.
        inventory.remember_representative(stored_resp)
        for identity in deferred:
            _write_revisit(
                identity=identity,
                stored=stored_resp,
                inventory=inventory,
                writer=writer,
                metrics=metrics,
            )
    return []


def _current_relative_key(
    layout: CollectionLayout,
    year: int,
    writer: YearWarcWriter,
) -> str:
    seq = writer.sequence
    if writer.stream is None and writer.finalized:
        # Just rotated or never opened: last finalized sequence
        return writer.finalized[-1].relative_key
    return layout.warc_relative_key(year, seq)


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
    if not layout.years_index_root.is_dir():
        return []
    result = []
    for path in sorted(layout.years_index_root.glob("*.cdxj")):
        if path.name.startswith(".tmp-"):
            continue
        result.append(index_artifact_from_path(layout, path))
    return result


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
    final: bool,
) -> list[UnresolvedFailure]:
    # Collapse failures by identity; later details replace stale ones.
    # Drop any identity now represented in the collection inventory.
    by_id: dict[CaptureIdentity, UnresolvedFailure] = {}
    for failure in failures:
        by_id[failure.identity] = failure

    inv = inventory_collection(layout)
    unresolved_list = [
        failure
        for failure in by_id.values()
        if not inv.contains(failure.identity)
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
    *,
    strict_digests: bool = False,
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
        strict_digests=strict_digests,
    )
