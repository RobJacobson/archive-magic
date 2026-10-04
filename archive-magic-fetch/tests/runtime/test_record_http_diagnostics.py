"""429 diagnostics preserve rejection handling and bound captured content."""

import gzip
import json
from io import BytesIO

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse
from wayback.exceptions import RateLimitError

from archive_magic_fetch.adapters.create_wayback_client import make_client
from archive_magic_fetch.runtime.record_http_diagnostics import (
    BODY_EXCERPT_BYTES, BODY_WIRE_BYTES, HEADER_VALUE_CHARS,
)
from archive_magic_fetch.runtime.track_http_requests import RequestStats


@pytest.mark.parametrize("encoding", [None, "gzip", "false-gzip"])
def test_429_diagnostics_bound_and_decode_body_without_exposing_cookies(
    tmp_path, monkeypatch, encoding,
):
    body = b'Rate limited\n"quoted"\x00 ' + b"x" * 100000
    wire = gzip.compress(body) if encoding == "gzip" else body
    stream = BytesIO(wire)
    path = tmp_path / "run.429.jsonl"
    stats = RequestStats(lambda: None, report=lambda _: None, diagnostics_path=path)
    captured = []

    def send(adapter, request, **kwargs):
        result = requests.Response()
        result.url = request.url
        result.request = request
        result.status_code = 429
        result.headers.update({
            "Server": "edge" * HEADER_VALUE_CHARS, "Content-Type": "text/html",
            "X-Request-Id": "request-123", "Set-Cookie": "secret-cookie",
            "Authorization": "secret-token", "Retry-After": "120",
        })
        if encoding:
            result.headers["Content-Encoding"] = "gzip"
        result.raw = HTTPResponse(body=stream, preload_content=False, decode_content=False)
        captured.append(result)
        return result

    monkeypatch.setattr(HTTPAdapter, "send", send)
    try:
        with make_client() as client:
            client.session.track_requests(stats)
            with pytest.raises(RateLimitError) as raised:
                client.session.get("https://web.archive.org/web/example")
        assert raised.value.retry_after == 120
        assert raised.value.response.status_code == 429
    finally:
        stats.close()
    lines = path.read_text().splitlines()
    assert len(lines) == 1  # Embedded newlines and control characters are escaped.
    record = json.loads(lines[0])
    assert record["body_excerpt"] == body[:BODY_EXCERPT_BYTES].decode()
    assert record["body_truncated"] is True
    assert record["headers"]["X-Request-Id"] == "request-123"
    assert len(record["headers"]["Server"]) == HEADER_VALUE_CHARS
    assert "secret-cookie" not in lines[0]
    assert "secret-token" not in lines[0]
    assert record["request_id"] == 1
    assert record["phase"] == "playback"
    assert captured[0].raw.closed
    assert len(captured[0].content) == BODY_EXCERPT_BYTES


def test_diagnostic_read_failure_preserves_429_and_header_timing(tmp_path, monkeypatch):
    from unittest.mock import Mock

    path = tmp_path / "run.429.jsonl"
    now = [100.0]
    stats = RequestStats(
        lambda: None, report=lambda _: None, diagnostics_path=path,
        trace_path=tmp_path / "trace.csv", clock=lambda: now[0],
    )
    raw = Mock()
    raw._original_response = None

    def fail_read(size, *, decode_content):
        assert size == BODY_WIRE_BYTES + 1
        assert decode_content is False
        now[0] += 12
        raise requests.ReadTimeout("body stalled")

    raw.read.side_effect = fail_read

    def send(adapter, request, **kwargs):
        result = requests.Response()
        result.url = request.url
        result.status_code = 429
        result.headers["Server"] = "edge"
        result.raw = raw
        return result

    monkeypatch.setattr(HTTPAdapter, "send", send)
    try:
        with make_client() as client:
            client.session.track_requests(stats)
            with pytest.raises(RateLimitError):
                client.session.get("https://web.archive.org/web/example")
    finally:
        stats.close()
    record = json.loads(path.read_text())
    assert record["headers"] == {"Server": "edge"}
    assert record["body_read_error"] == "ReadTimeout: body stalled"
    assert record["body_excerpt"] == ""
    raw.close.assert_called()
    import csv
    row = next(csv.DictReader((tmp_path / "trace.csv").open()))
    assert row["status"] == "429"
    assert row["duration_ms"] == "0"


def test_diagnostics_path_is_lazy_and_write_failure_does_not_hide_429(tmp_path, monkeypatch):
    path = tmp_path / "run.429.jsonl"
    reports = []
    stats = RequestStats(lambda: None, report=reports.append, diagnostics_path=path)
    assert not path.exists()
    path.write_text("existing diagnostics\n")

    def send(adapter, request, **kwargs):
        result = requests.Response()
        result.url = request.url
        result.status_code = 429
        result._content = b"limited"
        result._content_consumed = True
        return result

    monkeypatch.setattr(HTTPAdapter, "send", send)
    try:
        with make_client() as client:
            client.session.track_requests(stats)
            with pytest.raises(RateLimitError):
                client.session.get("https://web.archive.org/web/example")
    finally:
        stats.close()
    assert path.read_text() == "existing diagnostics\n"
    assert any("could not save HTTP 429 diagnostics" in report for report in reports)
