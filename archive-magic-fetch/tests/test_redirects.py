import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from wayback import CdxRecord
from wayback.exceptions import MementoPlaybackError

from archive_magic_fetch import redirects
from archive_magic_fetch.downloads import DownloadedCapture
from archive_magic_fetch.redirects import (
    redirect_scope,
    resolve_redirect_target,
)


@pytest.mark.parametrize("status_code", (200, 300, 302, 303, 307, 404))
def test_non_permanent_responses_do_not_introduce_targets(status_code):
    assert resolve_redirect_target(
        "https://source.test/start",
        status_code,
        (("Location", "https://target.test/"),),
    ) is None


@pytest.mark.parametrize("status_code", (301, 308))
def test_permanent_redirects_resolve_relative_locations(status_code):
    assert resolve_redirect_target(
        "https://source.test/path/start",
        status_code,
        (("location", "../landing?view=all#section"),),
    ) == "https://source.test/landing?view=all"


def test_protocol_relative_location_is_supported():
    assert resolve_redirect_target(
        "http://source.test/",
        301,
        (("Location", "//target.test/page#fragment"),),
    ) == "http://target.test/page"


@pytest.mark.parametrize(
    ("headers", "message"),
    (
        ((), "no Location"),
        ((("Location", "mailto:person@example.com"),), "unsupported"),
        ((("Location", "http://"),), "absolute"),
        (
            (("Location", "https://user:pass@target.test/"),),
            "user information",
        ),
    ),
)
def test_invalid_locations_are_rejected(headers, message):
    with pytest.raises(ValueError, match=message):
        resolve_redirect_target("https://source.test/", 301, headers)


def test_page_scope_uses_an_exact_query_and_deduplicates_authority():
    first = redirect_scope("http://www.target.test/page?q=1", "page")
    second = redirect_scope("https://target.test/page?q=1", "page")

    assert first.url == "http://www.target.test/page?q=1"
    assert first.match_type == "exact"
    assert first.key == second.key


def test_page_scope_keeps_paths_queries_and_significant_ports_distinct():
    scopes = {
        redirect_scope("https://target.test/one", "page").key,
        redirect_scope("https://target.test/two", "page").key,
        redirect_scope("https://target.test/one?q=1", "page").key,
        redirect_scope("https://target.test:8443/one", "page").key,
    }

    assert len(scopes) == 4


def test_website_scope_uses_a_host_query_and_canonicalizes_www():
    first = redirect_scope("http://www.target.test/one", "website")
    second = redirect_scope("https://target.test/two", "website")

    assert first.url == "http://www.target.test/one"
    assert first.match_type == "host"
    assert first.key == second.key


def test_redirect_scope_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported redirect capture mode"):
        redirect_scope("https://target.test/", "all")


def _capture(original, status, *, timestamp="20000101000000"):
    domain = original.split("//", 1)[1].split("/", 1)[0]
    return CdxRecord(
        urlkey=f"{domain})/",
        timestamp=datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        original=original,
        mimetype="text/html",
        statuscode=status,
        digest="A" * 32,
        length=10,
    )


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _downloaded(capture, location):
    return DownloadedCapture(
        body=b"discarded probe body",
        url=capture.original,
        capture_date="2000-01-01T00:00:00Z",
        source_uri=capture.raw_url,
        status_code=capture.statuscode,
        headers=(("Location", location),),
    )


def test_redirect_discovery_probes_only_301_and_308(monkeypatch):
    permanent = _capture("https://source.test/one", 301)
    temporary = _capture("https://source.test/two", 302)
    resumable = _capture("https://source.test/three", 308)
    probes = []
    searches = []

    def download(_client, capture, **_kwargs):
        probes.append(capture)
        return _downloaded(
            capture,
            f"https://target-{capture.statuscode}.test/",
        )

    monkeypatch.setattr(redirects, "download_capture", download)
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda _client, url, *_args, **_kwargs: searches.append(url) or [],
    )

    result = redirects.discover_redirect_captures(
        [permanent, temporary, resumable],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=2,
        retries=0,
    )

    assert set(probes) == {permanent, resumable}
    assert set(searches) == {
        "https://target-301.test/",
        "https://target-308.test/",
    }
    assert result.captures == ()


def test_redirect_discovery_closes_recursive_cycles_once(monkeypatch):
    first = _capture("https://first.test/", 301)
    second = _capture("https://second.test/", 308)
    probes = []
    searches = []

    def download(_client, capture, **_kwargs):
        probes.append(capture)
        location = (
            "https://second.test/"
            if capture is first
            else "https://first.test/"
        )
        return _downloaded(capture, location)

    def search(_client, url, *_args, **_kwargs):
        searches.append(url)
        return [second] if "second.test" in url else [first]

    monkeypatch.setattr(redirects, "download_capture", download)
    monkeypatch.setattr(redirects, "search_captures", search)

    result = redirects.discover_redirect_captures(
        [first],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=2,
        retries=0,
    )

    assert probes == [first, second]
    assert searches == ["https://second.test/", "https://first.test/"]
    assert result.captures == (second,)
    assert len(result.searches) == 2


def test_redirect_discovery_preserves_distinct_same_timestamp_rows(monkeypatch):
    source = _capture("https://source.test/", 301)
    first = _capture("https://target.test/", 200)
    second = first._replace(
        statuscode=404,
        digest="B" * 32,
        length=20,
    )

    monkeypatch.setattr(
        redirects,
        "download_capture",
        lambda _client, capture, **_kwargs: _downloaded(
            capture,
            "https://target.test/",
        ),
    )
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda *_args, **_kwargs: [first, second],
    )

    result = redirects.discover_redirect_captures(
        [source],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=1,
        retries=0,
    )

    assert result.captures == (first, second)


def test_repeated_targets_introduce_one_search(monkeypatch):
    first = _capture("https://source.test/one", 301)
    second = _capture("https://source.test/two", 308)
    searches = []

    monkeypatch.setattr(
        redirects,
        "download_capture",
        lambda _client, capture, **_kwargs: _downloaded(
            capture,
            "https://target.test/page",
        ),
    )
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda _client, url, *_args, **_kwargs: searches.append(url) or [],
    )

    redirects.discover_redirect_captures(
        [first, second],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=2,
        retries=0,
    )

    assert searches == ["https://target.test/page"]


def test_invalid_location_is_a_warning_not_a_playback_failure(monkeypatch):
    capture = _capture("https://source.test/", 301)
    monkeypatch.setattr(
        redirects,
        "download_capture",
        lambda *_args, **_kwargs: DownloadedCapture(
            body=b"discarded",
            url=capture.original,
            capture_date="2000-01-01T00:00:00Z",
            source_uri=capture.raw_url,
            status_code=301,
            headers=(),
        ),
    )
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda *_args, **_kwargs: pytest.fail("warning introduced a search"),
    )

    result = redirects.discover_redirect_captures(
        [capture],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=1,
        retries=0,
    )

    assert result.failed_capture_urls == ()
    assert len(result.messages) == 1
    assert "no Location" in result.messages[0]


def test_probe_failure_keeps_retry_context_and_is_reported_once(monkeypatch):
    capture = _capture("https://source.test/", 308)
    retry_values = []

    def fail(_client, selected, *, retries):
        assert selected is capture
        retry_values.append(retries)
        raise MementoPlaybackError("probe failed")

    monkeypatch.setattr(redirects, "download_capture", fail)
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda *_args, **_kwargs: pytest.fail("failed probe introduced a search"),
    )

    result = redirects.discover_redirect_captures(
        [capture],
        _Client(),
        _Client,
        mode="website",
        date_start="1995",
        date_end="2001",
        worker_count=1,
        retries=4,
    )

    assert retry_values == [4]
    assert result.failed_capture_urls == (capture.view_url,)
    assert len(result.messages) == 1
    assert capture.view_url in result.messages[0]


def test_probe_status_mismatch_is_a_playback_failure(monkeypatch):
    capture = _capture("https://source.test/", 301)
    monkeypatch.setattr(
        redirects,
        "download_capture",
        lambda *_args, **_kwargs: DownloadedCapture(
            body=b"discarded",
            url=capture.original,
            capture_date="2000-01-01T00:00:00Z",
            source_uri=capture.raw_url,
            status_code=308,
            headers=(("Location", "https://target.test/"),),
        ),
    )
    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda *_args, **_kwargs: pytest.fail("mismatch introduced a search"),
    )

    result = redirects.discover_redirect_captures(
        [capture],
        _Client(),
        _Client,
        mode="page",
        date_start="1995",
        date_end="2001",
        worker_count=1,
        retries=0,
    )

    assert result.failed_capture_urls == (capture.view_url,)
    assert "CDX status 301 but playback returned 308" in result.messages[0]


def test_redirect_probes_overlap_and_use_thread_private_clients(monkeypatch):
    first = _capture("https://source.test/one", 301)
    second = _capture("https://source.test/two", 308)
    both_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active_clients = []
    created_clients = []

    class Client(_Client):
        pass

    def factory():
        client = Client()
        created_clients.append(client)
        return client

    def download(client, capture, **_kwargs):
        with lock:
            active_clients.append(client)
            if len(active_clients) == 2:
                both_started.set()
        release.wait(timeout=5)
        return _downloaded(capture, f"https://target-{capture.statuscode}.test/")

    monkeypatch.setattr(redirects, "download_capture", download)
    monkeypatch.setattr(redirects, "search_captures", lambda *_a, **_k: [])

    with ThreadPoolExecutor(max_workers=1) as caller:
        finished = caller.submit(
            redirects.discover_redirect_captures,
            [first, second],
            _Client(),
            factory,
            mode="page",
            date_start="1995",
            date_end="2001",
            worker_count=2,
            retries=0,
        )
        assert both_started.wait(timeout=2)
        assert not finished.done()
        release.set()
        finished.result(timeout=2)

    assert len(created_clients) == 2
    assert len({id(client) for client in active_clients}) == 2
