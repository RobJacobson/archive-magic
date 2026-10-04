"""Calendar-year coverage, complete pagination, and catalog-aware caches."""

import json
from dataclasses import replace

import pytest
import requests

from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex, CATALOG_URL
from archive_magic_fetch.contracts import DiscoveryRequest
from archive_magic_fetch.pipeline.discovery.discover_captures import discover_captures
from archive_magic_fetch.pipeline.discovery.load_or_fetch_common_crawl_year import load_or_fetch_common_crawl_year
from common_crawl_helpers import Client, Response, collection, record, row


def index():
    return CommonCrawlIndex(sleep=lambda seconds: None, clock=lambda: 0)


def query(tmp_path, client, *, state=None, year=2017, pattern="*.example.org"):
    return load_or_fetch_common_crawl_year(DiscoveryRequest(pattern, year, 2017), index_directory=tmp_path / "index", index=state or index(), client=client)


def test_pages_overlap_duplicate_locators_and_narrow_then_expanded_dates(tmp_path):
    captures = [record(timestamp=f"2017{month}15000000")[0] for month in ("01", "06", "12")]
    duplicate = replace(captures[1], locator=replace(captures[1].locator, offset=999))
    overlap = collection("CC-MAIN-2016-50", "2016-12-20T00:00:00", "2017-12-31T23:59:59")
    catalog = [collection(), overlap, collection("CC-MAIN-2018-01", "2018-01-01T00:00:00", "2018-02-01T00:00:00")]
    responses = []

    def serve(url, **kwargs):
        if url == CATALOG_URL:
            result = Response(json.dumps(catalog))
        else:
            params = kwargs["params"]
            assert params["from"] == "20170101000000" and params["to"] == "20171231235959"
            assert params["pageSize"] == 5 and params["matchType"] == "domain"
            assert "filter" not in params and "collapse" not in params
            if "showNumPages" in params:
                result = Response('{"pages": 2}')
            else:
                items = captures[:2] if params["page"] == 0 else captures[2:]
                if url == collection()["cdx-api"]:
                    items = [duplicate] if params["page"] == 0 else []
                result = Response("\n".join(json.dumps(row(c)) for c in items))
        responses.append(result)
        return result

    client = Client(serve)
    from types import SimpleNamespace
    source = SimpleNamespace(discover=lambda request, stats: query(tmp_path, client))
    request = DiscoveryRequest("*.example.org", 2017, 2017)
    narrow = discover_captures(source, request, date_start="20170601000000", date_end="20170630235959")
    assert len(narrow.captures) == 1 and narrow.captures[0].locator.offset == 100
    assert narrow.captures[0].locator.crawl_id == overlap["id"]
    initial_queries = len(client.calls)
    expanded = discover_captures(source, request, date_start="20170101000000", date_end="20171231235959")
    assert [c.identity.timestamp for c in expanded.captures] == [c.identity.timestamp for c in captures]
    assert len(client.calls) == initial_queries + 1  # fresh catalog, completed current-year caches
    assert all(r.closed for r in responses)
    assert len(list((tmp_path / "index").rglob("2017.json"))) == 2


def test_catalog_cached_once_per_run_and_new_collections_added_next_run(tmp_path):
    catalog = [collection()]
    capture = record()[0]
    def serve(url, **kwargs):
        if url == CATALOG_URL:
            return Response(json.dumps(catalog))
        if "showNumPages" in kwargs["params"]:
            return Response('{"pages": 1}')
        return Response(json.dumps(row(capture)))
    client = Client(serve)
    state = index()
    query(tmp_path, client, state=state)
    query(tmp_path, client, state=state)
    assert sum(url == CATALOG_URL for url, _ in client.calls) == 1
    catalog.append(collection("CC-MAIN-2017-30"))
    listing = query(tmp_path, client)
    assert len(listing.captures) == 2
    assert len(client.calls) == 6  # first catalog/count/page, next catalog/new count/page
    # An actual catalog metadata change invalidates only the affected entry.
    catalog[0]["to"] = "2017-12-30T23:59:59"
    query(tmp_path, client)
    assert len(client.calls) == 9


@pytest.mark.parametrize("empty", [Response('{"pages": 0}'), Response('{"message":"No Captures found for: example.org"}', status=404)])
def test_valid_empty_results_are_cached(tmp_path, empty):
    client = Client(lambda url, **kw: Response(json.dumps([collection()])) if url == CATALOG_URL else empty)
    assert not query(tmp_path, client).captures
    assert not query(tmp_path, client).captures
    assert len(client.calls) == 3 and empty.closed


@pytest.mark.parametrize("failure", [
    Response("not json"), Response('{"url":"incomplete"}'), Response("missing", status=404),
    Response('{"message":"Index unavailable"}', status=404),
    Response('{"message":"No Captures found for: example.org"}', status=500),
])
def test_failed_page_never_publishes_partial_crawl(tmp_path, failure):
    capture = record()[0]
    def serve(url, **kw):
        if url == CATALOG_URL:
            return Response(json.dumps([collection()]))
        params = kw["params"]
        if "showNumPages" in params:
            return Response('{"pages":2}')
        return Response(json.dumps(row(capture))) if params["page"] == 0 else failure
    with pytest.raises((ValueError, requests.HTTPError)):
        query(tmp_path, Client(serve))
    assert not list(tmp_path.rglob("*.json")) and failure.closed


def test_completed_crawl_survives_later_failure_without_returning_partial_year(tmp_path):
    first, second = collection(), collection("CC-MAIN-2017-30")
    def serve(url, **kw):
        if url == CATALOG_URL:
            return Response(json.dumps([first, second]))
        if url == second["cdx-api"]:
            return Response("error", status=403)
        return Response('{"pages":0}')
    with pytest.raises(requests.HTTPError):
        query(tmp_path, Client(serve))
    files = list(tmp_path.rglob("2017.json"))
    assert len(files) == 1 and first["id"] in str(files[0])


def test_source_query_isolation_and_invalid_cache_fails_without_refetch(tmp_path):
    old = tmp_path / "index" / "2017.cdx.json"
    old.parent.mkdir()
    old.write_text('["wayback preserved"]')
    client = Client(lambda url, **kw: Response(json.dumps([collection()])) if url == CATALOG_URL else Response('{"pages":0}'))
    query(tmp_path, client)
    query(tmp_path, client, pattern="example.org/path/*")
    caches = list(tmp_path.rglob("2017.json"))
    assert len(caches) == 2 and old.read_text() == '["wayback preserved"]'
    for cache in caches:
        cache.write_text('{"broken": true}')
    before = len(client.calls)
    with pytest.raises(ValueError, match="invalid Common Crawl cache"):
        query(tmp_path, client)
    assert len(client.calls) == before + 1


def test_index_retry_pacing_and_cleanup():
    sleeps = []
    state = CommonCrawlIndex(sleep=sleeps.append, clock=lambda: 0)
    busy = Response("busy", status=429, headers={"Retry-After": "700"})
    ready = Response("ready")
    responses = iter([busy, ready])
    client = Client(lambda *a, **kw: next(responses))
    assert state.read(client, "https://index.commoncrawl.org/fixture") == "ready"
    assert sleeps == [0, 700.0, 1]
    assert busy.closed and ready.closed


@pytest.mark.parametrize("change", [{"cdx-api": "http://index.commoncrawl.org/CC-MAIN-2017-26-index"},
    {"cdx-api": "https://evil.test/index"}, {"id": "../x"}, {"from": "not-a-date"},
    {"from": "2020-01-01T00:00:00"}])
def test_invalid_catalog_does_not_produce_empty_success(tmp_path, change):
    client = Client(lambda *a, **kw: Response(json.dumps([collection() | change])))
    with pytest.raises(ValueError):
        query(tmp_path, client)
    assert not list(tmp_path.rglob("*.json"))


def test_date_filtered_empty_numbered_page_preserves_complete_annual_cache(tmp_path):
    first, last = [record(timestamp=f"2017{month}15000000")[0] for month in ("01", "12")]
    pages_seen, responses = [], []

    def serve(url, **kwargs):
        if url == CATALOG_URL:
            result = Response(json.dumps([collection(
                start="2016-12-01T00:00:00", end="2018-01-01T00:00:00",
            )]))
        elif "showNumPages" in kwargs["params"]:
            result = Response('{"pages": 3}')
        else:
            page = kwargs["params"]["page"]
            pages_seen.append(page)
            if page == 1:
                result = Response(
                    '{"message":"No Captures found for: example.org"}', status=404,
                )
            else:
                result = Response(json.dumps(row(first if page == 0 else last)))
        responses.append(result)
        return result

    client = Client(serve)
    listing = query(tmp_path, client)
    assert listing.captures == (first, last)
    assert pages_seen == [0, 1, 2] and all(response.closed for response in responses)
    before = len(client.calls)
    assert query(tmp_path, client).captures == listing.captures
    assert len(client.calls) == before + 1  # catalog only; full annual cache is reusable


@pytest.mark.parametrize("pattern", ["https://example.org/news*", "https://example.org/news"])
def test_unspecified_match_type_preserves_inference_and_isolates_old_exact_cache(tmp_path, pattern):
    import hashlib

    # The previous adapter could persist an empty literal-star query. Its key
    # must not poison the corrected query or require deleting other caches.
    old_query = {"url": pattern, "matchType": "exact"}
    old_hash = hashlib.sha256(json.dumps(old_query, sort_keys=True).encode()).hexdigest()
    old_cache = tmp_path / "index" / "common-crawl" / old_hash / collection()["id"] / "2017.json"
    old_cache.parent.mkdir(parents=True)
    old_cache.write_text(json.dumps({
        "version": 1, "query": old_query, "from": "20170101000000", "to": "20171231235959",
        "collection": collection(), "captures": [],
    }))
    old_bytes = old_cache.read_bytes()
    capture = record()[0]
    capture = replace(capture, identity=replace(
        capture.identity, original_url="https://example.org/news", urlkey="org,example)/news",
    ))

    def serve(url, **kwargs):
        if url == CATALOG_URL:
            return Response(json.dumps([collection()]))
        params = kwargs["params"]
        assert params["url"] == pattern
        assert "matchType" not in params
        if "showNumPages" in params:
            return Response('{"pages":1}')
        return Response(json.dumps(row(capture)))

    client = Client(serve)
    assert query(tmp_path, client, pattern=pattern).captures == (capture,)
    assert len(client.calls) == 3
    assert query(tmp_path, client, pattern=pattern).captures == (capture,)
    assert len(client.calls) == 4 and old_cache.read_bytes() == old_bytes
