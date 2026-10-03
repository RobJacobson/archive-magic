"""Pure capture-resolution policy for chronological URL groups."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Sequence

from .identity import revisit_group_key
from .inventory import StoredResponse, stored_from_playback
from .models import CaptureIdentity, ParsedCapture, PlaybackResult, UnresolvedFailure
from .workers import DownloadOutcome, PlaybackWorkers


class CaptureKind(str, Enum):
    EXISTING = "existing"
    REVISIT = "revisit"
    DOWNLOADED = "downloaded"
    FAILURE = "failure"


@dataclass(frozen=True)
class CaptureOutcome:
    identity: CaptureIdentity
    kind: CaptureKind
    playback: PlaybackResult | None = None
    representative: StoredResponse | None = None
    failure: UnresolvedFailure | None = None
    attempts: int = 0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class UrlOutcome:
    url: str
    captures: tuple[CaptureOutcome, ...]
    attempts: int
    playback_bytes: int
    categories: tuple[str, ...]


def iter_url_outcomes(
    groups: Sequence[Sequence[ParsedCapture]],
    process: Callable[[Sequence[ParsedCapture]], UrlOutcome],
    workers: PlaybackWorkers,
) -> Iterator[UrlOutcome]:
    """Resolve each URL once, with bounded workers and results in CDX order."""

    pending: dict[int, Future] = {}
    next_submit = 0
    total = len(groups)

    def fill() -> None:
        nonlocal next_submit
        while next_submit < total and len(pending) < workers.max_workers:
            pending[next_submit] = workers.submit(process, groups[next_submit])
            next_submit += 1

    for index in range(total):
        fill()
        yield pending.pop(index).result()


def process_url_group(
    captures: Sequence[ParsedCapture],
    *,
    workers: PlaybackWorkers,
    existing_identities: frozenset[CaptureIdentity],
    existing_representatives: dict[tuple[str, str, str], StoredResponse],
) -> UrlOutcome:
    """Walk one URL chronologically, remembering only successful digests.

    The map acts as a set of acquired digests while retaining the reference
    needed to write revisits. Existing responses seed it for resumed runs and
    later years. No candidate selection or failure cache is needed.
    """

    urlkey = captures[0].identity.urlkey
    successful_downloads = {
        key: stored for key, stored in existing_representatives.items()
        if key[0] == urlkey
    }
    outcomes: list[CaptureOutcome] = []
    attempts = 0
    playback_bytes = 0
    categories: list[str] = []

    for capture in captures:
        outcome, downloaded = _resolve_capture(
            capture,
            workers=workers,
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
    capture: ParsedCapture,
    *,
    workers: PlaybackWorkers,
    existing_identities: frozenset[CaptureIdentity],
    successful_downloads: dict[tuple[str, str, str], StoredResponse],
) -> tuple[CaptureOutcome, DownloadOutcome | None]:
    identity = capture.identity
    if identity in existing_identities:
        return CaptureOutcome(identity, CaptureKind.EXISTING), None

    key = revisit_group_key(identity)
    representative = successful_downloads.get(key) if key is not None else None
    if (
        representative is not None
        and representative.identity.timestamp <= identity.timestamp
    ):
        return CaptureOutcome(
            identity, CaptureKind.REVISIT, representative=representative
        ), None

    downloaded = workers.download(identity)
    if downloaded.failure is not None:
        return CaptureOutcome(
            identity,
            CaptureKind.FAILURE,
            failure=downloaded.failure,
            attempts=downloaded.attempts,
            elapsed_s=downloaded.elapsed_s,
        ), downloaded

    result = downloaded.result
    assert result is not None
    if result.digest_matched and key is not None:
        successful_downloads[key] = stored_from_playback(result)
    return CaptureOutcome(
        identity,
        CaptureKind.DOWNLOADED,
        playback=result,
        attempts=downloaded.attempts,
        elapsed_s=downloaded.elapsed_s,
    ), downloaded
