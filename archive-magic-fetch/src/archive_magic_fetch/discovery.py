"""Internet Archive CDX discovery."""

from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping, Optional, Sequence

from wayback import CdxRecord
from wayback.exceptions import RateLimitError


# Matches WaybackClient.search()'s default per-request limit.
_PROGRESS_INTERVAL = 1000

OutputMode = str
OUTPUT_MODES = ("none", "latest", "all")


def _is_redirect_status(status: Optional[int]) -> bool:
    """Return whether a known HTTP status is in the 3xx class."""

    return status is not None and 300 <= status < 400


def select_latest_capture(
    captures: Sequence[CdxRecord],
) -> Optional[CdxRecord]:
    """Choose one capture for ``latest`` mode from a urlkey group.

    Preference order:
    1. newest capture with CDX status ``200``
    2. newest capture whose status is present and not ``3xx``
    3. omit the group (redirect-only / statusless-only)

    “Newest” is determined by ``capture.timestamp``, not input order.
    """

    if not captures:
        raise ValueError("capture group is empty")

    with_200 = [
        capture for capture in captures if capture.statuscode == 200
    ]
    if with_200:
        return max(with_200, key=lambda capture: capture.timestamp)

    non_redirect = [
        capture
        for capture in captures
        if capture.statuscode is not None
        and not _is_redirect_status(capture.statuscode)
    ]
    if non_redirect:
        return max(non_redirect, key=lambda capture: capture.timestamp)

    return None


def apply_output_mode(
    capture_groups: Mapping[str, Sequence[CdxRecord]],
    mode: OutputMode,
) -> dict[str, list[CdxRecord]]:
    """Transform grouped captures into the selection for one output axis."""

    if mode not in OUTPUT_MODES:
        raise ValueError(f"unsupported output mode: {mode}")
    if mode == "none":
        return {}
    if mode == "all":
        return {
            urlkey: list(captures)
            for urlkey, captures in capture_groups.items()
        }

    selected: dict[str, list[CdxRecord]] = {}
    for urlkey, captures in capture_groups.items():
        capture = select_latest_capture(captures)
        if capture is not None:
            selected[urlkey] = [capture]
    return selected


def normalize_cdx_search(url_pattern: str) -> tuple[str, Optional[str]]:
    """Rewrite CDX URL sugar into an explicit match type when needed.

    A trailing ``/*`` means prefix match on the text before ``*``. Internet
    Archive's CDX server accepts that sugar, but the literal ``url=.../*`` form
    can hang under date filters. Prefer ``url=.../`` with ``matchType=prefix``.
    """

    if not isinstance(url_pattern, str) or not url_pattern:
        raise ValueError("URL pattern must be a non-empty string")

    if url_pattern.endswith("/*"):
        return url_pattern.removesuffix("*"), "prefix"
    return url_pattern, None


def discover(
    client,
    url_pattern: str,
    date_start: str,
    date_end: str,
    *,
    progress: Optional[Callable[[int], None]] = None,
) -> list[CdxRecord]:
    """Materialize all Internet Archive captures for the supplied selection.

    When ``progress`` is provided, it is called with the capture count after
    every ``_PROGRESS_INTERVAL`` records during a successful attempt. A
    rate-limited attempt discards partial rows before retrying, so progress
    restarts from zero on the next attempt.
    """

    search_url, match_type = normalize_cdx_search(url_pattern)
    search_kwargs: dict[str, object] = {
        "from_date": date_start,
        "to_date": date_end,
        "resolve_revisits": False,
    }
    if match_type is not None:
        search_kwargs["match_type"] = match_type

    def attempt() -> list[CdxRecord]:
        captures: list[CdxRecord] = []
        for capture in client.search(search_url, **search_kwargs):
            captures.append(capture)
            count = len(captures)
            if progress is not None and count % _PROGRESS_INTERVAL == 0:
                progress(count)
        return captures

    while True:
        try:
            return attempt()
        except RateLimitError as error:
            delay = error.retry_after or 60
            print(
                f"Rate limited during discovery; retrying in {delay}s...",
                flush=True,
            )
            time.sleep(delay)


def group_captures(
    captures: Iterable[CdxRecord],
) -> dict[str, list[CdxRecord]]:
    """Collapse literal duplicates and group captures by Wayback URL key."""

    grouped: dict[str, list[CdxRecord]] = {}
    seen_records: set[CdxRecord] = set()
    for capture in captures:
        if not isinstance(capture.original, str):
            raise ValueError("capture URL must be a string")

        urlkey = capture.urlkey
        if not isinstance(urlkey, str) or not urlkey:
            raise ValueError("capture must include a non-empty CDX urlkey")

        if capture in seen_records:
            continue
        seen_records.add(capture)

        grouped.setdefault(urlkey, []).append(capture)

    for group_list in grouped.values():
        group_list.sort(key=lambda capture: capture.timestamp)

    return grouped
