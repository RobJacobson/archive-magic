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
    MISSING_CDX_PAYLOAD_DIGEST,
    CaptureIdentity,
    FailureCategory,
    IndexArtifact,
    ParsedCapture,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
    current_run_id,
    current_utc_cdx_timestamp,
)
from .scheduler import JobFailure, JobSuccess, PlaybackScheduler, failure_from_job
from .warc import (
    CollectionInventory,
    StoredResponse,
    YearWarcWriter,
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
    run_id = current_run_id()
    source_dir = init_run_source(layout, run_id)
    all_failures: list[UnresolvedFailure] = []
    all_warcs: list[WarcArtifact] = []
    annual_indexes: list[IndexArtifact] = []

    # Baseline inventory of previously published WARCs.
    inventory = inventory_collection(layout)
    client_factory = client_factory or make_client

    years = years_in_range(settings.date_start, settings.date_end)
    print(
        f"collection {layout.collection_id}: years {years[0]}-{years[-1]}",
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

        work_plan = _plan_year_work(to_schedule, inventory, year)
        print(
            f"year {year}: {len(selected)} selected, "
            f"{len(work_plan.network_jobs)} to download, "
            f"{len(work_plan.revisit_jobs)} revisits ready",
            flush=True,
        )

        writer = YearWarcWriter(layout, year)
        year_index = inventory.year_index(year)

        # Write revisits that already have an in-year representative.
        for identity, stored in work_plan.revisit_jobs:
            writer.write_revisit(revisit_from_stored(identity, stored))
            inventory.identities.add(identity)
            metrics.revisits += 1
            metrics.represented += 1

        # Schedule remaining network jobs via central scheduler.
        if work_plan.network_jobs:
            year_failures = _run_year_downloads(
                layout=layout,
                year=year,
                jobs=work_plan.network_jobs,
                inventory=inventory,
                year_index=year_index,
                writer=writer,
                metrics=metrics,
                client_factory=client_factory,
                download_fn=download_fn,
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
                # replace annual entry for this year
                annual_indexes = [
                    item
                    for item in annual_indexes
                    if not item.relative_key.endswith(f"/{year:04d}.cdxj")
                ]
                annual_indexes.append(annual)

        # Ensure annual index exists even if no new warcs this year.
        if list_year_warcs(layout, year):
            annual = publish_annual_index(layout, year)
            if annual is not None:
                annual_indexes = [
                    item
                    for item in annual_indexes
                    if not item.relative_key.endswith(f"/{year:04d}.cdxj")
                ] + [annual]

        coll = publish_collection_index(layout)
        _publish_state(
            layout,
            settings=settings,
            run_source=f"sources/{run_id}",
            warcs=_collect_warc_artifacts(layout, all_warcs),
            annual_indexes=annual_indexes,
            collection_index=coll,
            metrics=metrics,
            failures=all_failures,
            final=False,
        )

    coll = publish_collection_index(layout)
    final_warcs = _collect_warc_artifacts(layout, all_warcs)
    metrics.unresolved = len({f.identity for f in all_failures})
    status = "complete" if metrics.unresolved == 0 else "partial"
    _publish_state(
        layout,
        settings=settings,
        run_source=f"sources/{run_id}",
        warcs=final_warcs,
        annual_indexes=annual_indexes or _existing_annual_indexes(layout),
        collection_index=coll or (
            index_artifact_from_path(layout, layout.collection_index)
            if layout.collection_index.is_file()
            else None
        ),
        metrics=metrics,
        failures=all_failures,
        final=True,
    )
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
        failures=all_failures,
    )


@dataclass
class _YearPlan:
    network_jobs: list[CaptureIdentity]
    revisit_jobs: list[tuple[CaptureIdentity, StoredResponse]]
    # group remaining candidates after a representative is chosen:
    pending_revisits: dict[tuple[str, str], list[CaptureIdentity]]


def _plan_year_work(
    captures: Sequence[ParsedCapture],
    inventory: CollectionInventory,
    year: int,
) -> _YearPlan:
    """Apply same-year representative/revisit grouping rules."""

    year_index = inventory.year_index(year)
    network: list[CaptureIdentity] = []
    revisits: list[tuple[CaptureIdentity, StoredResponse]] = []

    # Redirects and digests-missing always individual.
    redirect_or_nodigest: list[ParsedCapture] = []
    groups: dict[tuple[str, str], list[ParsedCapture]] = defaultdict(list)

    for capture in captures:
        if capture.is_redirect or not capture.has_usable_digest:
            redirect_or_nodigest.append(capture)
            continue
        key = (capture.identity.urlkey, capture.identity.payload_digest)
        groups[key].append(capture)

    for key, group in groups.items():
        group = sorted(group, key=lambda c: c.identity.timestamp)
        stored = year_index.by_url_digest.get(key)
        if stored is not None:
            for capture in group:
                if not inventory.contains(capture.identity):
                    revisits.append((capture.identity, stored))
            continue
        # No in-year representative yet: schedule earliest as network job;
        # remaining stay as deferred revisits after success (handled live).
        representative = group[0]
        network.append(representative.identity)
        # Stash later members by attaching to network pipeline via pending map
        # encoded as pseudo sequence: we'll handle group members after download.
        for capture in group[1:]:
            # They need the representative first; if representative fails, try next.
            # Store as additional network candidates (retry order).
            network.append(capture.identity)

    for capture in redirect_or_nodigest:
        network.append(capture.identity)

    # Deterministic network order.
    network = sorted(network, key=lambda identity: identity.sort_key())
    revisits.sort(key=lambda item: item[0].sort_key())
    return _YearPlan(
        network_jobs=network,
        revisit_jobs=revisits,
        pending_revisits={},
    )


def _run_year_downloads(
    *,
    layout: CollectionLayout,
    year: int,
    jobs: Sequence[CaptureIdentity],
    inventory: CollectionInventory,
    year_index,
    writer: YearWarcWriter,
    metrics: RunMetrics,
    client_factory: Callable,
    download_fn,
) -> list[UnresolvedFailure]:
    """Download scheduled identities and write responses/revisits."""

    # Build digest groups for post-success revisits.
    by_key: dict[tuple[str, str], list[CaptureIdentity]] = defaultdict(list)
    singles: list[CaptureIdentity] = []
    for identity in jobs:
        if (
            identity.payload_digest == MISSING_CDX_PAYLOAD_DIGEST
            or (
                identity.status_token.isdigit()
                and 300 <= int(identity.status_token) < 400
            )
        ):
            singles.append(identity)
            continue
        by_key[(identity.urlkey, identity.payload_digest)].append(identity)

    # Only the first pending member of each group is an actual download job at
    # a time; others wait. For simplicity/KISS: schedule first of each group +
    # all singles. On success, write revisits for remaining. On failure of first,
    # try next candidate for that group.
    active: list[CaptureIdentity] = []
    remaining_groups: dict[tuple[str, str], list[CaptureIdentity]] = {}
    for key, members in by_key.items():
        members = sorted(members, key=lambda i: i.timestamp)
        # Skip already represented mid-run.
        members = [m for m in members if not inventory.contains(m)]
        if not members:
            continue
        stored = year_index.by_url_digest.get(key)
        if stored is not None:
            for identity in members:
                writer.write_revisit(revisit_from_stored(identity, stored))
                inventory.identities.add(identity)
                metrics.revisits += 1
                metrics.represented += 1
            continue
        remaining_groups[key] = members[1:]
        active.append(members[0])
    active.extend(m for m in singles if not inventory.contains(m))
    active = sorted(active, key=lambda i: i.sort_key())

    if not active:
        return []

    scheduler_kwargs = {
        "client_factory": client_factory,
        "identities": active,
        "metrics": metrics,
    }
    if download_fn is not None:
        scheduler_kwargs["download_fn"] = download_fn
    scheduler = PlaybackScheduler(**scheduler_kwargs)

    failures: list[UnresolvedFailure] = []
    # Run scheduler on a background thread so the writer can consume results.
    import threading

    thread = threading.Thread(target=scheduler.run, name="playback-scheduler")
    thread.start()
    try:
        for item in scheduler.results():
            if isinstance(item, JobSuccess):
                result = item.result
                if inventory.contains(result.identity):
                    metrics.local_reuses += 1
                    continue
                key = (
                    result.identity.urlkey,
                    result.identity.payload_digest,
                )
                # Same-year digest revisit if representative already stored.
                stored = year_index.by_url_digest.get(key)
                if (
                    stored is not None
                    and result.identity.payload_digest != MISSING_CDX_PAYLOAD_DIGEST
                    and not (
                        result.identity.status_token.isdigit()
                        and 300 <= int(result.identity.status_token) < 400
                    )
                ):
                    writer.write_revisit(
                        revisit_from_stored(
                            result.identity,
                            stored,
                            http_status_code=result.status_code,
                            http_headers=result.headers,
                        )
                    )
                    inventory.identities.add(result.identity)
                    metrics.revisits += 1
                    metrics.represented += 1
                    continue

                writer.write_playback(result)
                metrics.downloads += 1
                metrics.represented += 1
                inventory.identities.add(result.identity)
                # Update year response index with provisional relative key.
                # Sequence may rotate; use last finalized or current open sequence.
                relative = _current_relative_key(layout, year, writer)
                stored_resp = stored_from_playback(
                    layout, year, result, relative
                )
                year_index.by_identity[result.identity] = stored_resp
                if result.identity.payload_digest != MISSING_CDX_PAYLOAD_DIGEST:
                    year_index.by_url_digest[key] = stored_resp

                # Write pending same-group members as revisits.
                for identity in remaining_groups.pop(key, []):
                    if inventory.contains(identity):
                        continue
                    writer.write_revisit(
                        revisit_from_stored(
                            identity,
                            stored_resp,
                            http_status_code=result.status_code,
                            http_headers=result.headers,
                        )
                    )
                    inventory.identities.add(identity)
                    metrics.revisits += 1
                    metrics.represented += 1
            else:
                assert isinstance(item, JobFailure)
                identity = item.identity
                key = (identity.urlkey, identity.payload_digest)
                # Try next candidate in group if any.
                candidates = remaining_groups.get(key)
                if candidates:
                    next_id = candidates.pop(0)
                    remaining_groups[key] = candidates
                    # Inline retry of next representative is done by folding into
                    # failure list only if we can't schedule more dynamically.
                    # Simple approach: download next synchronously.
                    try:
                        client = client_factory()
                        enter = getattr(client, "__enter__", None)
                        active_client = enter() if callable(enter) else client
                        if active_client is None:
                            active_client = client
                        fn = download_fn
                        if fn is None:
                            from .warc import download_exact_for_identity as fn
                        result = fn(active_client, next_id)
                        # Re-inject as success path.
                        success = JobSuccess(identity=next_id, result=result)
                        # process by recursive tail via queue simulation:
                        writer.write_playback(success.result)
                        metrics.downloads += 1
                        metrics.represented += 1
                        inventory.identities.add(next_id)
                        relative = _current_relative_key(layout, year, writer)
                        stored_resp = stored_from_playback(
                            layout, year, success.result, relative
                        )
                        year_index.by_identity[next_id] = stored_resp
                        year_index.by_url_digest[key] = stored_resp
                        for identity2 in remaining_groups.pop(key, []):
                            if inventory.contains(identity2):
                                continue
                            writer.write_revisit(
                                revisit_from_stored(identity2, stored_resp)
                            )
                            inventory.identities.add(identity2)
                            metrics.revisits += 1
                            metrics.represented += 1
                        close = getattr(client, "close", None)
                        if callable(close):
                            close()
                        continue
                    except Exception as error:  # noqa: BLE001
                        failures.append(
                            UnresolvedFailure(
                                identity=next_id,
                                category=FailureCategory.UNAVAILABLE,
                                message=str(error),
                            )
                        )
                failures.append(failure_from_job(item))
    finally:
        thread.join()

    # Mark unresolved remaining group members.
    for key, members in remaining_groups.items():
        for identity in members:
            if inventory.contains(identity):
                continue
            failures.append(
                UnresolvedFailure(
                    identity=identity,
                    category=FailureCategory.UNAVAILABLE,
                    message="representative capture failed; no same-year payload",
                )
            )
    return failures


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
                warc_artifact_from_path(layout, path, record_count=0)
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
) -> None:
    # Collapse failures by identity; drop any now represented in inventory.
    by_id: dict[CaptureIdentity, UnresolvedFailure] = {}
    for failure in failures:
        by_id.setdefault(failure.identity, failure)

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


def build_settings(
    url_pattern: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    archives_root: Optional[Path] = None,
) -> FetchSettings:
    """Validate CLI-facing inputs into settings."""

    start = parse_date_bound(date_start, default=DEFAULT_DATE_START)
    end = parse_date_bound(date_end, default=current_utc_cdx_timestamp())
    validate_date_range(start, end)
    return FetchSettings(
        url_pattern=url_pattern.strip(),
        date_start=start,
        date_end=end,
        archives_root=archives_root,
    )
