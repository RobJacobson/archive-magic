"""Discover capture histories introduced by permanent redirects."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from wayback import CdxRecord

from .search import search_captures
from .collection_paths import normalize_domain
from .downloads import (
    PLAYBACK_ERRORS,
    ThreadClientPool,
    format_playback_failure,
    download_capture,
)
from .retry import DEFAULT_RETRIES


REDIRECT_CAPTURE_MODES = ("none", "page", "website")
PERMANENT_REDIRECT_STATUSES = frozenset((301, 308))


@dataclass(frozen=True)
class RedirectScope:
    """One deduplicated CDX query introduced by a redirect target."""

    key: tuple[object, ...]
    url: str
    match_type: str


@dataclass(frozen=True)
class RedirectSearch:
    """One nonempty CDX result introduced by a redirect target."""

    scope: RedirectScope
    captures: tuple[CdxRecord, ...]


@dataclass(frozen=True)
class RedirectDiscovery:
    """All additional captures and diagnostics found before WARC building."""

    captures: tuple[CdxRecord, ...]
    searches: tuple[RedirectSearch, ...]
    failed_capture_urls: tuple[str, ...]
    messages: tuple[str, ...]
    additional_domains: int


@dataclass(frozen=True)
class _ProbeResult:
    capture: CdxRecord
    target: str | None = None
    failed: bool = False
    message: str | None = None


def resolve_redirect_target(
    base_url: str,
    status_code: int,
    headers,
) -> str | None:
    """Resolve an HTTP(S) Location for a permanent redirect response."""

    if status_code not in PERMANENT_REDIRECT_STATUSES:
        return None
    location = next(
        (
            str(value).strip()
            for name, value in headers
            if name.lower() == "location" and str(value).strip()
        ),
        None,
    )
    if location is None:
        raise ValueError("permanent redirect has no Location header")

    resolved = urljoin(base_url, location)
    parsed = urlsplit(resolved)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"unsupported redirect scheme: {parsed.scheme}")
    normalize_domain(resolved)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def redirect_scope(url: str, mode: str) -> RedirectScope:
    """Translate a target URL into an exact-page or exact-host CDX query."""

    if mode not in {"page", "website"}:
        raise ValueError(f"unsupported redirect capture mode: {mode}")
    parsed = urlsplit(url)
    host, port = normalize_domain(url)
    authority = (host, port)
    if mode == "website":
        return RedirectScope(authority, url, "host")
    return RedirectScope(
        (*authority, parsed.path or "/", parsed.query),
        url,
        "exact",
    )


def _probe_redirect(client, capture: CdxRecord, *, retries: int) -> _ProbeResult:
    """Download one redirect capture and return only its resolved target."""

    try:
        downloaded = download_capture(client, capture, retries=retries)
    except PLAYBACK_ERRORS as error:
        return _ProbeResult(
            capture,
            failed=True,
            message=(
                f"{capture.view_url}: {format_playback_failure(error)}"
            ),
        )

    if (
        capture.statuscode is not None
        and downloaded.status_code != capture.statuscode
    ):
        return _ProbeResult(
            capture,
            failed=True,
            message=(
                f"{capture.view_url}: CDX status {capture.statuscode} but "
                f"playback returned {downloaded.status_code}"
            ),
        )

    try:
        target = resolve_redirect_target(
            capture.original,
            downloaded.status_code,
            downloaded.headers,
        )
    except ValueError as error:
        return _ProbeResult(
            capture,
            message=f"{capture.view_url}: {error}",
        )
    return _ProbeResult(capture, target=target)


def discover_redirect_captures(
    selected_captures: Sequence[CdxRecord],
    client,
    client_factory: Callable,
    *,
    mode: str,
    date_start: str,
    date_end: str,
    worker_count: int,
    retries: int = DEFAULT_RETRIES,
) -> RedirectDiscovery:
    """Find the complete recursive 301/308 capture closure.

    Playback probes overlap through a bounded worker pool. CDX searches stay
    serial on ``client`` and every returned body is deliberately discarded.
    """

    if mode not in {"page", "website"}:
        raise ValueError(f"unsupported redirect capture mode: {mode}")
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")

    primary_domains = {
        normalize_domain(capture.original)
        for capture in selected_captures
    }
    probe_queue = [
        capture
        for capture in selected_captures
        if capture.statuscode in PERMANENT_REDIRECT_STATUSES
    ]
    seen_probes = set(probe_queue)
    seen_searches: set[tuple[object, ...]] = set()
    known_captures = set(selected_captures)
    redirect_captures: list[CdxRecord] = []
    searches: list[RedirectSearch] = []
    failed_capture_urls: list[str] = []
    messages: list[str] = []

    clients = ThreadClientPool(client_factory)
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            while probe_queue:
                def run_probe(capture: CdxRecord) -> _ProbeResult:
                    return _probe_redirect(
                        clients.get(),
                        capture,
                        retries=retries,
                    )

                pending_probes = {
                    pool.submit(run_probe, capture): capture
                    for capture in probe_queue
                }
                probe_queue = []
                targets: list[str] = []
                for finished_probe in as_completed(pending_probes):
                    result = finished_probe.result()
                    if result.message is not None:
                        messages.append(result.message)
                    if result.failed:
                        failed_capture_urls.append(result.capture.view_url)
                    if result.target is not None:
                        targets.append(result.target)

                for target in sorted(set(targets)):
                    scope = redirect_scope(target, mode)
                    if scope.key in seen_searches:
                        continue
                    seen_searches.add(scope.key)
                    captures = search_captures(
                        client,
                        scope.url,
                        date_start,
                        date_end,
                        match_type=scope.match_type,
                        retries=retries,
                    )
                    if not captures:
                        continue
                    searches.append(RedirectSearch(scope, tuple(captures)))
                    for capture in captures:
                        if capture not in known_captures:
                            known_captures.add(capture)
                            redirect_captures.append(capture)
                        if (
                            capture.statuscode in PERMANENT_REDIRECT_STATUSES
                            and capture not in seen_probes
                        ):
                            seen_probes.add(capture)
                            probe_queue.append(capture)
    finally:
        clients.close()

    additional_domains = len(
        {
            normalize_domain(capture.original)
            for capture in redirect_captures
        }
        - primary_domains
    )
    return RedirectDiscovery(
        tuple(redirect_captures),
        tuple(searches),
        tuple(dict.fromkeys(failed_capture_urls)),
        tuple(messages),
        additional_domains,
    )
