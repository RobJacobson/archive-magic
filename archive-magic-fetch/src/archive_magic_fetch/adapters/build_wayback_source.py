"""Assemble Wayback discovery, acquisition, policy, and client lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from wayback import WaybackClient

from archive_magic_fetch.adapters.interpret_wayback_failures import (
    failure_advice,
    is_invalid_uri_payload_digest,
    wayback_url,
)
from archive_magic_fetch.adapters.create_wayback_client import make_client
from archive_magic_fetch.config.models import (
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
)
from archive_magic_fetch.contracts import DiscoveryRequest, SourceAdapter
from archive_magic_fetch.models import (
    CaptureListing,
    CaptureRef,
    CaptureResult,
    FailureCategory,
    UnresolvedFailure,
)
from archive_magic_fetch.pipeline.decoding.decode_memento import decode_memento
from archive_magic_fetch.pipeline.discovery import load_or_fetch_year_cdx as discovery
from archive_magic_fetch.pipeline.retrieval.retrieve_memento import retrieve_memento
from archive_magic_fetch.runtime.track_http_requests import RequestStats


def build_source(
    *,
    index_directory: Path,
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS,
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
) -> SourceAdapter[WaybackClient]:
    def discover(request: DiscoveryRequest) -> CaptureListing:
        return discovery.load_or_fetch_year_cdx(
            index_directory=index_directory,
            year=request.year,
            current_year=request.current_year,
            url_pattern=request.url_pattern,
            cdx_window_days=cdx_window_days,
            cdx_page_limit=cdx_page_limit,
            sleep=sleep,
        )

    return SourceAdapter(
        discover=discover,
        open_client=_open_client,
        fetch=_fetch,
        preflight=_preflight,
        failure_advice=failure_advice,
        capture_link=lambda identity: wayback_url(
            identity.timestamp, identity.original_url
        ),
    )


@contextmanager
def _open_client(stats: RequestStats) -> Iterator[WaybackClient]:
    with make_client() as client:
        client.session.track_playback(stats)
        yield client


def _fetch(client: WaybackClient, capture: CaptureRef) -> CaptureResult:
    return decode_memento(retrieve_memento(client, capture), capture)


def _preflight(capture: CaptureRef) -> UnresolvedFailure | None:
    if is_invalid_uri_payload_digest(capture.identity.payload_digest):
        return UnresolvedFailure(
            capture.identity,
            FailureCategory.UNAVAILABLE,
            "CDX digest is IA Invalid URI stub",
            display_reason="invalid URI",
        )
    return None
