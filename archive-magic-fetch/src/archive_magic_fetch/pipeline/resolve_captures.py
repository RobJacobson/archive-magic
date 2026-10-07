"""Resolve chronological URL groups into stored, revisit, or acquired outcomes."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event
from typing import Callable, Sequence

from archive_magic_fetch.archive.identity import revisit_group_key
from archive_magic_fetch.archive.inventory_collection import (
    CollectionInventory,
    stored_from_capture,
)
from archive_magic_fetch.models import (
    CaptureIdentity,
    CaptureKind,
    CaptureOutcome,
    CaptureRef,
    DownloadOutcome,
    StoredResponse,
    UrlOutcome,
)
from archive_magic_fetch.runtime.pace_requests import check_cancelled
from archive_magic_fetch.runtime.manage_capture_workers import CaptureBatch


@dataclass(frozen=True)
class PayloadData:
    """Lazy playback results for one collection update."""

    url_count: int
    outcomes: Iterator[UrlOutcome]


def resolve_captures(
    captures: Sequence[CaptureRef],
    *,
    inventory: CollectionInventory,
    batch: CaptureBatch,
    download: Callable[[CaptureRef], DownloadOutcome],
) -> PayloadData:
    """Resolve selected CDX captures into a lazy stream of payload outcomes."""

    grouped: dict[str, list[CaptureRef]] = defaultdict(list)
    for capture in captures:
        grouped[capture.identity.urlkey].append(capture)
    groups = list(grouped.values())
    identities = frozenset(inventory.identities)
    representatives_by_url = defaultdict(dict)
    for key, stored in inventory.by_url_digest.items():
        representatives_by_url[key[0]][key] = stored

    def process(group: Sequence[CaptureRef]) -> UrlOutcome:
        return _process_url_group(
            group,
            download=download,
            existing_identities=identities,
            existing_representatives=representatives_by_url.get(
                group[0].identity.urlkey, {}
            ),
            cancelled=batch.cancelled,
        )

    outcomes = batch.map(process, groups)
    return PayloadData(url_count=len(groups), outcomes=outcomes)


def _process_url_group(
    captures: Sequence[CaptureRef],
    *,
    download: Callable[[CaptureRef], DownloadOutcome],
    existing_identities: frozenset[CaptureIdentity],
    existing_representatives: dict[tuple[str, str, str], StoredResponse],
    cancelled: Event | None = None,
) -> UrlOutcome:
    """Walk one URL chronologically, remembering only successful digests.

    The map acts as a set of acquired digests while retaining the reference
    needed to write revisits. Existing responses from this year seed it for
    resumed runs. No candidate selection or failure cache is needed.
    """

    successful_downloads = dict(existing_representatives)
    outcomes: list[CaptureOutcome] = []
    attempts = 0
    playback_bytes = 0
    categories: list[str] = []

    for capture in captures:
        check_cancelled(cancelled)
        outcome, downloaded = _resolve_capture(
            capture,
            download=download,
            existing_identities=existing_identities,
            successful_downloads=successful_downloads,
        )
        outcomes.append(outcome)
        if downloaded is not None:
            attempts += downloaded.attempts
            categories.extend(downloaded.categories)
            if downloaded.result is not None:
                playback_bytes += len(downloaded.result.body)

    return UrlOutcome(
        url=captures[0].identity.original_url,
        captures=tuple(outcomes),
        attempts=attempts,
        playback_bytes=playback_bytes,
        categories=tuple(categories),
    )


def _resolve_capture(
    capture: CaptureRef,
    *,
    download: Callable[[CaptureRef], DownloadOutcome],
    existing_identities: frozenset[CaptureIdentity],
    successful_downloads: dict[tuple[str, str, str], StoredResponse],
) -> tuple[CaptureOutcome, DownloadOutcome | None]:
    identity = capture.identity
    if identity in existing_identities:
        return CaptureOutcome(capture, CaptureKind.EXISTING), None

    key = revisit_group_key(identity)
    representative = successful_downloads.get(key) if key is not None else None
    if (
        representative is not None
        and representative.identity.timestamp <= identity.timestamp
    ):
        return CaptureOutcome(
            capture, CaptureKind.REVISIT, representative=representative
        ), None

    downloaded = download(capture)
    if downloaded.failure is not None:
        return CaptureOutcome(
            capture,
            CaptureKind.FAILURE,
            failure=downloaded.failure,
            attempts=downloaded.attempts,
            elapsed_s=downloaded.elapsed_s,
        ), downloaded

    result = downloaded.result
    assert result is not None
    if result.digest_matched and key is not None:
        successful_downloads[key] = stored_from_capture(result)
    return CaptureOutcome(
        capture,
        CaptureKind.DOWNLOADED,
        playback=result,
        attempts=downloaded.attempts,
        elapsed_s=downloaded.elapsed_s,
    ), downloaded
