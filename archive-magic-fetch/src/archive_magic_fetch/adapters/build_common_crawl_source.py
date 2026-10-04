"""Assemble Common Crawl discovery, worker transport, decoding, and policy."""

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import requests

from archive_magic_fetch.adapters.interpret_common_crawl_failures import failure_advice
from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex
from archive_magic_fetch.contracts import SourceAdapter
from archive_magic_fetch.models import FailureCategory, UnresolvedFailure
from archive_magic_fetch.pipeline.decoding.decode_warc_capture import decode_warc_capture
from archive_magic_fetch.pipeline.discovery.load_or_fetch_common_crawl_year import load_or_fetch_common_crawl_year
from archive_magic_fetch.pipeline.retrieval.retrieve_warc_range import retrieve_warc_range
from archive_magic_fetch.runtime.track_http_requests import InstrumentedHTTPAdapter


def build_source(*, index_directory: Path) -> SourceAdapter:
    index = CommonCrawlIndex()

    def discover(request, stats):
        with requests.Session() as client:
            return load_or_fetch_common_crawl_year(
                request, index_directory=index_directory, index=index, client=client,
            )

    return SourceAdapter(discover, _open_client, _fetch, _preflight, failure_advice, _capture_link)


@contextmanager
def _open_client(stats):
    with _RangeSession() as client:
        client.mount("https://", InstrumentedHTTPAdapter(stats))
        client.headers["User-Agent"] = "archive-magic-fetch/0.1 (Common Crawl range client)"
        yield client


def _fetch(client, capture):
    return decode_warc_capture(retrieve_warc_range(client, capture), capture)


def _preflight(capture):
    if capture.locator is None or not capture.locator.filename.endswith(".warc.gz"):
        return UnresolvedFailure(
            capture.identity, FailureCategory.UNAVAILABLE,
            "Common Crawl acquisition requires a WARC locator; legacy ARC is unsupported",
        )
    return None


def _capture_link(capture):
    if capture.locator is None:
        return "https://index.commoncrawl.org/"
    identity = capture.identity
    query = urlencode({"url": identity.original_url, "matchType": "exact", "from": identity.timestamp,
                       "to": identity.timestamp, "output": "json"})
    return f"https://index.commoncrawl.org/{capture.locator.crawl_id}-index?{query}"


class _RangeSession(requests.Session):
    def resolve_redirects(self, response, request, **kwargs):
        # Requests normally consumes redirect bodies to prepare response.next,
        # even with allow_redirects=False and stream=True. Range responses must
        # reach our header checks before any body is consumed.
        return iter(())
