"""Internet Archive CDX discovery."""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, MutableMapping
from urllib.parse import urldefrag, urlsplit

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


def normalize_capture_url(url: str) -> str:
    """Remove URL syntax that cannot identify distinct response content."""

    fragmentless = urldefrag(url).url
    parsed = urlsplit(fragmentless)
    if parsed.query == "" and fragmentless.endswith("?"):
        return fragmentless[:-1]
    return fragmentless


def _row_signature(capture: MutableMapping) -> tuple:
    """Return a stable signature for one literal CDX result row."""

    return tuple(
        sorted((str(name), repr(value)) for name, value in capture.items())
    )


def group_captures(captures: Iterable[MutableMapping]) -> OrderedDict:
    """Normalize, collapse literal duplicates, and group captures by URL key."""

    grouped = OrderedDict()
    seen_rows = set()
    for capture in captures:
        url = capture["url"]
        if not isinstance(url, str):
            raise ValueError("capture URL must be a string")

        urlkey = capture["urlkey"]
        if not isinstance(urlkey, str) or not urlkey:
            raise ValueError("capture must include a non-empty CDX urlkey")

        row_signature = _row_signature(capture)
        if row_signature in seen_rows:
            continue
        seen_rows.add(row_signature)

        # Fragments never reach the server. A bare query delimiter contains no
        # parameters and triggers unreliable exact playback for historical IA
        # rows, so neither is retained as resource identity.
        capture["url"] = normalize_capture_url(url)
        grouped.setdefault(urlkey, []).append(capture)

    for group_captures in grouped.values():
        group_captures.sort(key=lambda capture: capture["timestamp"])

    return grouped
