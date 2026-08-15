"""Pure capture-resolution policy for chronological URL groups."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Sequence

from .identity import revisit_group_key
from .inventory import StoredResponse, stored_from_playback
from .models import CaptureIdentity, ParsedCapture, PlaybackResult, UnresolvedFailure
from .playback import (
    SLASH_REDIRECT_SOURCE_URI,
    empty_http_200_from_cdx,
    slash_redirect_from_cdx,
)
from .workers import DownloadOutcome, PlaybackWorkers


class CaptureKind(str, Enum):
    EXISTING = "existing"
    REVISIT = "revisit"
    EMPTY = "empty"
    SLASH_REDIRECT = "slash_redirect"
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


def group_needs_playback(
    group: Sequence[ParsedCapture],
    existing_identities: frozenset[CaptureIdentity],
) -> bool:
    """Return whether any capture in a URL group requires Wayback playback."""

    group_urls = tuple(capture.identity.original_url for capture in group)
    return any(
        capture.identity not in existing_identities
        and empty_http_200_from_cdx(capture.identity, mime=capture.mime) is None
        and slash_redirect_from_cdx(
            capture.identity, group_urls=group_urls
        ) is None
        for capture in group
    )


def iter_url_outcomes(
    groups: Sequence[Sequence[ParsedCapture]],
    process: Callable[[Sequence[ParsedCapture]], UrlOutcome],
    workers: PlaybackWorkers,
    skip_workers: Sequence[bool],
) -> Iterator[UrlOutcome]:
    """Yield URL groups in CDX order while resolving local-only groups inline."""

    pending: dict[int, Future] = {}
    next_submit = 0
    total = len(groups)

    def fill() -> None:
        nonlocal next_submit
        while next_submit < total and len(pending) < workers.max_workers:
            if skip_workers[next_submit]:
                next_submit += 1
                continue
            pending[next_submit] = workers.submit(process, groups[next_submit])
            next_submit += 1

    for index, group in enumerate(groups):
        if skip_workers[index]:
            yield process(group)
            continue
        fill()
        yield pending.pop(index).result()


def process_url_group(
    captures: Sequence[ParsedCapture],
    *,
    workers: PlaybackWorkers,
    existing_identities: frozenset[CaptureIdentity],
    existing_representatives: dict[tuple[str, str, str], StoredResponse],
) -> UrlOutcome:
    """Resolve one URL's captures chronologically without shared mutations."""

    local_representatives: dict[tuple[str, str, str], StoredResponse] = {}
    outcomes: list[CaptureOutcome] = []
    attempts = 0
    playback_bytes = 0
    categories: list[str] = []
    group_urls = tuple(capture.identity.original_url for capture in captures)

    for capture in captures:
        outcome, downloaded = _resolve_capture(
            capture,
            group_urls=group_urls,
            workers=workers,
            existing_identities=existing_identities,
            existing_representatives=existing_representatives,
            local_representatives=local_representatives,
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
    group_urls: Sequence[str],
    workers: PlaybackWorkers,
    existing_identities: frozenset[CaptureIdentity],
    existing_representatives: dict[tuple[str, str, str], StoredResponse],
    local_representatives: dict[tuple[str, str, str], StoredResponse],
) -> tuple[CaptureOutcome, DownloadOutcome | None]:
    identity = capture.identity
    if identity in existing_identities:
        return CaptureOutcome(identity, CaptureKind.EXISTING), None

    key = revisit_group_key(identity)
    representative = _find_representative(
        identity,
        key,
        local_representatives,
        existing_representatives,
    )
    if representative is not None:
        return CaptureOutcome(
            identity, CaptureKind.REVISIT, representative=representative
        ), None

    synthesized = empty_http_200_from_cdx(identity, mime=capture.mime)
    if synthesized is not None:
        _remember_representative(key, synthesized, local_representatives)
        return CaptureOutcome(
            identity, CaptureKind.EMPTY, playback=synthesized
        ), None

    synthesized = slash_redirect_from_cdx(identity, group_urls=group_urls)
    if synthesized is not None:
        _remember_representative(key, synthesized, local_representatives)
        return CaptureOutcome(
            identity, CaptureKind.SLASH_REDIRECT, playback=synthesized
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
    kind = (
        CaptureKind.SLASH_REDIRECT
        if result.source_uri == SLASH_REDIRECT_SOURCE_URI
        else CaptureKind.DOWNLOADED
    )
    if result.digest_matched or kind is CaptureKind.SLASH_REDIRECT:
        _remember_representative(key, result, local_representatives)
    return CaptureOutcome(
        identity,
        kind,
        playback=result,
        attempts=downloaded.attempts,
        elapsed_s=downloaded.elapsed_s,
    ), downloaded


def _find_representative(
    identity: CaptureIdentity,
    key: tuple[str, str, str] | None,
    local: dict[tuple[str, str, str], StoredResponse],
    existing: dict[tuple[str, str, str], StoredResponse],
) -> StoredResponse | None:
    if key is None:
        return None
    representative = local.get(key)
    if representative is not None:
        return representative
    candidate = existing.get(key)
    if candidate is not None and candidate.identity.timestamp <= identity.timestamp:
        return candidate
    return None


def _remember_representative(
    key: tuple[str, str, str] | None,
    result: PlaybackResult,
    representatives: dict[tuple[str, str, str], StoredResponse],
) -> None:
    if key is not None:
        representatives[key] = stored_from_playback(result)
