"""Internet Archive CDX discovery."""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, MutableMapping

import cdx_toolkit


def discover(url_pattern: str, date_start: str, date_end: str) -> list:
    """Materialize all Internet Archive captures for the supplied selection."""

    fetcher = cdx_toolkit.CDXFetcher(source="ia")
    return list(
        fetcher.iter(
            url_pattern,
            from_ts=date_start,
            to=date_end,
        )
    )


def group_captures(captures: Iterable[MutableMapping]) -> OrderedDict:
    """Group captures by exact URL and sort each group chronologically."""

    grouped = OrderedDict()
    for capture in captures:
        grouped.setdefault(capture["url"], []).append(capture)

    for url_captures in grouped.values():
        url_captures.sort(key=lambda capture: capture["timestamp"])

    return grouped

