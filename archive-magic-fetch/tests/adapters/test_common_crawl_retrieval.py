"""Bounded range transport, worker instrumentation, retries, and cleanup."""

from concurrent.futures import CancelledError
from dataclasses import replace
from threading import Event
from unittest.mock import MagicMock

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse
from io import BytesIO

from archive_magic_fetch.adapters.build_common_crawl_source import build_source
from archive_magic_fetch.adapters.interpret_common_crawl_failures import CorruptRecord, failure_advice
from archive_magic_fetch.pipeline.retrieval.fetch_capture import fetch_capture
from archive_magic_fetch.pipeline.retrieval.retrieve_warc_range import retrieve_warc_range
from archive_magic_fetch.runtime.pace_requests import StartGate
from archive_magic_fetch.runtime.track_http_requests import RequestStats
from common_crawl_helpers import Client, Response, record, range_response


def test_range_header_and_raw_bounded_read():
    capture, data = record()
    response = range_response(data, capture)
    client = Client(lambda *a, **kw: response)
    assert retrieve_warc_range(client, capture) == data
    url, options = client.calls[0]
    assert url == "https://data.commoncrawl.org/" + capture.locator.filename
    assert options == {"headers": {"Range": f"bytes=100-{99+len(data)}", "Accept-Encoding": "identity"},
                       "stream": True, "allow_redirects": False, "timeout": (10, 60)}
    assert sum(response.raw.reads) <= len(data) + 2
    assert response.closed


@pytest.mark.parametrize("status,headers", [(200, {}), (302, {"Location": "https://evil.test/"}),
    (206, {"Content-Range": "bytes 0-10/100"}), (206, {"Content-Range": "invalid"}),
    (206, {"Content-Encoding": "gzip"}), (206, {"Content-Length": "10"})])
def test_rejects_invalid_headers_before_reading(status, headers):
    capture, data = record()
    response = range_response(data, capture)
    response.status_code = status
    response.headers.update(headers)
    with pytest.raises(CorruptRecord):
        retrieve_warc_range(Client(lambda *a, **kw: response), capture)
    assert response.raw.reads == [] and response.closed


@pytest.mark.parametrize("delta", [-1, 1, 100000])
def test_exact_compressed_body_length_and_oversized_read_bound(delta):
    capture, data = record()
    response = range_response(data[:delta] if delta < 0 else data + b"x" * delta, capture)
    with pytest.raises(CorruptRecord, match="range length mismatch"):
        retrieve_warc_range(Client(lambda *a, **kw: response), capture)
    assert response.raw.tell() <= len(data) + 1 and response.closed


@pytest.mark.parametrize("failure", [OSError("read failed"), KeyboardInterrupt(), CancelledError()])
def test_response_closed_on_read_failure_or_interruption(failure):
    capture, data = record()
    response = range_response(data, capture)
    response.raw.read = MagicMock(side_effect=failure)
    expected = CorruptRecord if isinstance(failure, OSError) else type(failure)
    with pytest.raises(expected):
        retrieve_warc_range(Client(lambda *a, **kw: response), capture)
    assert response.closed


def test_decode_retry_uses_instrumented_persistent_client_and_closes(tmp_path, monkeypatch):
    capture, data = record()
    source = build_source(index_directory=tmp_path)
    sent, responses, sleeps, waits = [], [], [], []
    def send(adapter, request, **kwargs):
        sent.append((adapter, request))
        response = requests.Response()
        response.request = request
        response.status_code = 206
        response.headers["Content-Range"] = f"bytes 100-{99+len(data)}/999999"
        # Same-length gzip corruption fails in decoding, then a fresh valid range.
        body = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:] if len(sent) == 1 else data
        response.raw = HTTPResponse(body=BytesIO(body), preload_content=False)
        responses.append(response)
        return response
    monkeypatch.setattr(HTTPAdapter, "send", send)
    gate = StartGate(1000, report=lambda _: None)
    stats = RequestStats(lambda: waits.append(True), report=lambda _: None)
    with source.open_client(stats) as client:
        adapter = client.get_adapter("https://data.commoncrawl.org/")
        adapter.close = MagicMock(wraps=adapter.close)
        outcome = fetch_capture(capture, source=source, client=lambda: client, gate=gate, stats=stats, max_attempts=3, sleep=sleeps.append)
        assert outcome.result.body == b"hello" and outcome.attempts == 2
        assert len({id(item[0]) for item in sent}) == 1
    assert adapter.close.call_count == 1
    assert stats.snapshot()["requests_total"] == 2 and waits == [True, True]
    assert sleeps == [5]
    assert all(response.raw.closed for response in responses)
    stats.close()


@pytest.mark.parametrize("status,retry,cooldown", [(429, True, True), (503, True, True),
    (500, True, False), (504, True, False), (404, False, False), (403, False, False)])
def test_source_http_failure_policy(status, retry, cooldown):
    response = Response(status=status, headers={"Retry-After": "120"})
    advice = failure_advice(requests.HTTPError(response=response), 1)
    assert advice.retryable == retry
    assert bool(advice.cooldown) == cooldown
    if cooldown:
        assert advice.cooldown[1] == 120


def test_cc_late_backpressure_preserved_before_cancellation(tmp_path):
    cancelled = Event()
    source = build_source(index_directory=tmp_path)
    def fetch(client, capture):
        cancelled.set()
        raise requests.HTTPError(response=Response(status=503, headers={"Retry-After": "120"}))
    source = replace(source, fetch=fetch)
    gate = StartGate(1, clock=lambda: 100, report=lambda _: None)
    stats = RequestStats(lambda **kw: None, report=lambda _: None)
    with pytest.raises(CancelledError):
        fetch_capture(record()[0], source=source, client=object, gate=gate, stats=stats, max_attempts=5, cancelled=cancelled)
    assert gate._blocked_until == 220
    stats.close()


def test_requests_does_not_consume_redirect_body_before_range_validation(tmp_path, monkeypatch):
    capture, data = record()
    response = requests.Response()
    response.status_code = 302
    response.headers["Location"] = "https://data.commoncrawl.org/another.warc.gz"
    response.raw = HTTPResponse(body=BytesIO(b"unbounded redirect body"), preload_content=False)
    response.raw.read = MagicMock(side_effect=AssertionError("redirect body must never be read"))
    def send(adapter, request, **kwargs):
        response.request = request
        response.url = request.url
        return response
    monkeypatch.setattr(HTTPAdapter, "send", send)
    source = build_source(index_directory=tmp_path)
    stats = RequestStats(lambda: None, report=lambda _: None)
    with source.open_client(stats) as client:
        with pytest.raises(CorruptRecord, match="expected 206"):
            source.fetch(client, capture)
    response.raw.read.assert_not_called()
    assert response.raw.closed
    assert stats.snapshot()["requests_total"] == 1
    stats.close()
