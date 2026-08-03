"""Resolve permanent-redirect targets and expand them via CDX search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from wayback import CdxRecord

from .collection_paths import normalize_domain
from .retry import DEFAULT_RETRIES
from .search import group_by_url, search_captures


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
class RedirectExpansion:
    """New URL histories discovered from one Location target."""

    search: RedirectSearch
    histories: dict[tuple[str, str], list[CdxRecord]]


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
    """Translate a target URL into an exact-page or host-root CDX query.

    In ``website`` mode, only site-root Locations (``/`` with no query) expand
    to a host-wide CDX search. Deeper paths stay exact so an asset CDN 301
    cannot pull an entire third-party host.
    """

    if mode not in {"page", "website"}:
        raise ValueError(f"unsupported redirect capture mode: {mode}")
    parsed = urlsplit(url)
    host, port = normalize_domain(url)
    authority = (host, port)
    path = parsed.path or "/"
    if mode == "website" and path == "/" and not parsed.query:
        return RedirectScope(authority, url, "host")
    return RedirectScope(
        (*authority, path, parsed.query),
        url,
        "exact",
    )


def _redirect_search_label(scope: RedirectScope) -> str:
    """Return a short console label for one redirect CDX search."""

    if scope.match_type == "host":
        host, port = scope.key[0], scope.key[1]
        authority = host if port is None else f"{host}:{port}"
        return f"host {authority}"
    return f"page {scope.url}"


def expand_redirect_target(
    client,
    target_url: str,
    *,
    mode: str,
    date_start: str,
    date_end: str,
    seen_searches: set[tuple[object, ...]],
    known_history_keys: set[tuple[str, str]],
    retries: int = DEFAULT_RETRIES,
    progress: Optional[Callable[[int], None]] = None,
) -> Optional[RedirectExpansion]:
    """CDX-search one Location target and return only unseen URL histories.

    Already-known history keys keep their primary selection and are omitted.
    Returns ``None`` when the search scope was already queried or empty.
    """

    scope = redirect_scope(target_url, mode)
    if scope.key in seen_searches:
        return None
    seen_searches.add(scope.key)
    print(f"Redirect search: {_redirect_search_label(scope)}")
    captures = search_captures(
        client,
        scope.url,
        date_start,
        date_end,
        match_type=scope.match_type,
        progress=progress,
        retries=retries,
    )
    if not captures:
        return None

    grouped = group_by_url(captures)
    histories = {
        key: history
        for key, history in grouped.items()
        if key not in known_history_keys
    }
    return RedirectExpansion(
        RedirectSearch(scope, tuple(captures)),
        histories,
    )


def permanent_redirect_target(
    base_url: str,
    status_code: int,
    headers,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(target, warning)`` for one stored permanent-redirect response."""

    try:
        return resolve_redirect_target(base_url, status_code, headers), None
    except ValueError as error:
        return None, str(error)
