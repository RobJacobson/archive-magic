"""Internet Archive CDX discovery."""

from __future__ import annotations

import time
from typing import Iterable

from wayback import CdxRecord
from wayback.exceptions import RateLimitError


def discover(
    client,
    url_pattern: str,
    date_start: str,
    date_end: str,
) -> list[CdxRecord]:
    """Materialize all Internet Archive captures for the supplied selection."""

    def attempt() -> list[CdxRecord]:
        return list(
            client.search(
                url_pattern,
                from_date=date_start,
                to_date=date_end,
                resolve_revisits=False,
            )
        )

    try:
        return attempt()
    except RateLimitError as error:
        time.sleep(error.retry_after or 60)
        return attempt()


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

    for group_captures in grouped.values():
        group_captures.sort(key=lambda capture: capture.timestamp)

    return grouped
