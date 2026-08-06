"""Search Internet Archive CDX capture indexes."""

from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlencode

from wayback import CdxRecord
from wayback.exceptions import RateLimitError, WaybackRetryError

from .collection_paths import domain_folder, normalize_domain
from .console import print_progress
from .retry import (
    DEFAULT_RETRIES,
    RetryExhaustedError,
    RetryableWaybackResponseError,
    format_seconds,
    retry_decision,
    retry_delay_seconds,
    sleep_seconds,
)


_CDX_REQUEST_LIMIT = 10_000

OutputMode = str
FILES_MODES = ("none", "latest", "unique", "all")


def _is_redirect_status(status: Optional[int]) -> bool:
    """Return whether a known HTTP status is in the 3xx class."""

    return status is not None and 300 <= status < 400


def select_latest_capture(
    captures: Sequence[CdxRecord],
) -> Optional[CdxRecord]:
    """Choose one capture for ``latest`` mode from a URL history.

    Preference order:
    1. newest capture with CDX status ``200``
    2. newest capture whose status is present and not ``3xx``
    3. newest capture whose status is ``3xx``
    4. omit the history (statusless-only)

    “Newest” is determined by ``capture.timestamp``, not input order.
    """

    if not captures:
        raise ValueError("URL history is empty")

    newest_200 = max(
        (capture for capture in captures if capture.statuscode == 200),
        key=lambda capture: capture.timestamp,
        default=None,
    )
    if newest_200 is not None:
        return newest_200

    newest_non_redirect = max(
        (
            capture
            for capture in captures
            if capture.statuscode is not None
            and not _is_redirect_status(capture.statuscode)
        ),
        key=lambda capture: capture.timestamp,
        default=None,
    )
    if newest_non_redirect is not None:
        return newest_non_redirect

    return max(
        (
            capture
            for capture in captures
            if _is_redirect_status(capture.statuscode)
        ),
        key=lambda capture: capture.timestamp,
        default=None,
    )


def select_captures(
    captures_by_url: Mapping[tuple[str, str], Sequence[CdxRecord]],
    mode: OutputMode,
) -> Mapping[tuple[str, str], Sequence[CdxRecord]]:
    """Select captures from each URL history for one output axis."""

    if mode not in FILES_MODES:
        raise ValueError(f"unsupported output mode: {mode}")
    if mode == "none":
        return {}
    if mode in {"all", "unique"}:
        return captures_by_url

    selected: dict[tuple[str, str], list[CdxRecord]] = {}
    for history_key, captures in captures_by_url.items():
        capture = select_latest_capture(captures)
        if capture is not None:
            selected[history_key] = [capture]
    return selected


def normalize_cdx_search(url_pattern: str) -> tuple[str, Optional[str]]:
    """Rewrite CDX URL sugar into an explicit match type when needed.

    A leading ``*.`` on the host (e.g. ``*.example.com`` or
    ``*.example.com/*``) means CDX domain match: the apex host plus all
    subdomains. That form is checked before path-prefix sugar.

    A trailing ``/*`` means path-prefix match on the text before ``*``.
    Internet Archive's CDX server accepts that sugar, but the literal
    ``url=.../*`` form can hang under date filters. Prefer ``url=.../``
    with ``matchType=prefix``.
    """

    if not isinstance(url_pattern, str) or not url_pattern:
        raise ValueError("URL pattern must be a non-empty string")

    text = url_pattern.strip()
    domain_target = _domain_wildcard_target(text)
    if domain_target is not None:
        return domain_target, "domain"
    if text.endswith("/*"):
        return text.removesuffix("*"), "prefix"
    return url_pattern, None


def _domain_wildcard_target(url_pattern: str) -> Optional[str]:
    """Return the apex CDX URL for a ``*.host`` pattern, else ``None``."""

    remainder = url_pattern
    if "://" in remainder:
        remainder = remainder.split("://", 1)[1]
    if remainder.startswith("//"):
        remainder = remainder[2:]
    if not remainder.startswith("*."):
        return None
    host_part = remainder[2:].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host_part or "*" in host_part or host_part.startswith("."):
        raise ValueError(
            f"URL pattern must use a single leading *. on the host: "
            f"{url_pattern}"
        )
    host, port = normalize_domain(host_part, allow_bare=True)
    if port is None:
        return host
    return f"{host}:{port}"


def search_captures(
    client,
    url_pattern: str,
    date_start: str,
    date_end: str,
    *,
    match_type: Optional[str] = None,
    progress: Optional[Callable[[int], None]] = None,
    retries: int = DEFAULT_RETRIES,
) -> list[CdxRecord]:
    """Materialize all Internet Archive captures for the supplied selection.

    When ``progress`` is provided, it is called after each request limit's
    worth of records during a successful attempt. A rate-limited attempt
    discards partial rows before retrying, so progress restarts from zero on
    the next attempt.
    """

    search_url, inferred_match_type = normalize_cdx_search(url_pattern)
    search_kwargs: dict[str, object] = {
        "from_date": date_start,
        "to_date": date_end,
        "limit": _CDX_REQUEST_LIMIT,
        "resolve_revisits": False,
    }
    selected_match_type = match_type or inferred_match_type
    if selected_match_type is not None:
        search_kwargs["match_type"] = selected_match_type

    def attempt() -> list[CdxRecord]:
        captures: list[CdxRecord] = []
        for capture in client.search(search_url, **search_kwargs):
            captures.append(capture)
            count = len(captures)
            if progress is not None and count % _CDX_REQUEST_LIMIT == 0:
                progress(count)
        return captures

    if retries < 0:
        raise ValueError("retries cannot be negative")

    attempt_number = 0
    started_at = time.monotonic()
    query_url = (
        "https://web.archive.org/cdx/search/cdx?"
        + urlencode({"url": search_url, **search_kwargs})
    )
    while True:
        attempt_number += 1
        try:
            return attempt()
        except (
            RateLimitError,
            RetryableWaybackResponseError,
            WaybackRetryError,
        ) as error:
            decision = retry_decision(error)
            if decision is None:
                raise
            if attempt_number > retries:
                raise RetryExhaustedError(
                    attempts=attempt_number,
                    elapsed_seconds=time.monotonic() - started_at,
                    cause=decision.cause,
                ) from error
            delay = retry_delay_seconds(
                attempt_number,
                retry_after=decision.retry_after,
            )
            cause = " ".join(str(decision.cause).split())
            after = f" after {cause}" if cause else ""
            print_progress(
                f"{query_url}\n  retry {attempt_number}/{retries} in "
                f"{format_seconds(delay)}s{after}"
            )
            sleep_seconds(delay)


def group_by_url(
    captures: Iterable[CdxRecord],
) -> dict[tuple[str, str], list[CdxRecord]]:
    """Group captures by Wayback URL key and sort them by timestamp."""

    captures_by_url: dict[tuple[str, str], list[CdxRecord]] = {}
    for capture in captures:
        if not isinstance(capture.original, str):
            raise ValueError("capture URL must be a string")

        urlkey = capture.urlkey
        if not isinstance(urlkey, str) or not urlkey:
            raise ValueError("capture must include a non-empty CDX urlkey")

        captures_by_url.setdefault(
            (domain_folder(capture.original), urlkey),
            [],
        ).append(capture)

    for history in captures_by_url.values():
        history.sort(key=lambda capture: capture.timestamp)

    return captures_by_url
