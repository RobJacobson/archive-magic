"""Select an ordered, unique capture set from a complete source listing."""

from __future__ import annotations

from archive_magic_fetch.contracts import DiscoveryRequest, SourceAdapter
from archive_magic_fetch.models import CaptureListing


def discover_captures(
    source: SourceAdapter, request: DiscoveryRequest, *, date_start: str, date_end: str
) -> CaptureListing:
    listing = source.discover(request)
    selected = {}
    for capture in sorted(listing.captures, key=lambda item: item.identity.sort_key()):
        if date_start <= capture.identity.timestamp <= date_end:
            selected.setdefault(capture.identity, capture)
    return CaptureListing(tuple(selected.values()), listing.query)
