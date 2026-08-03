"""Small redirect helpers shared by Fetch orchestration and export."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from .paths import normalize_url_authority


REDIRECT_CAPTURE_MODES = ("none", "page", "website")
PERMANENT_REDIRECT_STATUSES = frozenset((301, 308))


@dataclass(frozen=True)
class RedirectScope:
    """One deduplicated CDX query introduced by a redirect target."""

    key: tuple[object, ...]
    url: str
    match_type: str


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
    normalize_url_authority(resolved)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def redirect_scope(url: str, mode: str) -> RedirectScope:
    """Translate a target URL into an exact-page or exact-host CDX query."""

    if mode not in {"page", "website"}:
        raise ValueError(f"unsupported redirect capture mode: {mode}")
    parsed = urlsplit(url)
    host, port = normalize_url_authority(url)
    authority = (host, port)
    if mode == "website":
        return RedirectScope(authority, url, "host")
    return RedirectScope(
        (*authority, parsed.path or "/", parsed.query),
        url,
        "exact",
    )
