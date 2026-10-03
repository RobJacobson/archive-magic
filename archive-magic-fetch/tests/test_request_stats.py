"""Exercise the real Wayback/request stack with only HTTP transport mocked."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import pytest
import requests
from requests.adapters import HTTPAdapter

from archive_magic_fetch import cdx, fetch
from archive_magic_fetch.config import FetchOutput
from archive_magic_fetch.models import ParsedCapture
from archive_magic_fetch.playback import download_exact, make_client, payload_digest
from archive_magic_fetch.workers import PlaybackWorkers
from helpers import make_capt, playback


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def response(request, status=200, headers=None, *, memento=True):
    result = requests.Response()
    result.request = request
    result.url = request.url
    result.status_code = status
    result._content = b"hello"
    result._content_consumed = True
    if memento:
        result.headers["Memento-Datetime"] = "Tue, 15 Jun 2004 00:00:00 GMT"
    result.headers.update(headers or {})
    return result


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def workers_for(clock, path, **kwargs):
    return PlaybackWorkers(
        make_client, download_exact, pace=True, starts_per_second=2,
        sleep=clock.sleep, clock=clock, trace_path=path, **kwargs,
    )


def test_429_retry_is_paced_and_traced(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.jsonl"
    calls = []
    identity = make_capt(digest=payload_digest(b"hello"))

    def send(_adapter, request, **kwargs):
        calls.append((clock(), request.url))
        if len(calls) == 1:
            return response(request, 429, {"Retry-After": "60"})
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        outcome = workers.download(identity)
    finally:
        workers.close()
    assert outcome.failure is None
    assert outcome.attempts == 2
    assert [t for t, _ in calls] == [100, 160]
    trace = events(path)
    starts = [e for e in trace if e["event"] == "request_start"]
    ends = [e for e in trace if e["event"] == "request_end"]
    assert [e["attempt"] for e in starts] == [1, 2]
    assert [e["request_in_attempt"] for e in starts] == [1, 1]
    assert [e["request_id"] for e in starts] == [1, 2]
    assert [e["status"] for e in ends] == [429, 200]
    assert ends[0]["retry_after"] == "60"
    assert starts[0]["capture"]["timestamp"] == identity.timestamp
    assert datetime.fromisoformat(starts[0]["time_utc"]).utcoffset().total_seconds() == 0
    assert trace[-1]["requests_total"] == 2
    assert trace[-1]["http_429"] == 1
    assert trace[-1]["peak_starts_1s"] == 1


def test_nearby_redirect_stops_after_one_http_request(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.jsonl"
    calls = []
    identity = make_capt(digest=payload_digest(b"hello"))
    nearby = "https://web.archive.org/web/20040615000001id_/http://example.org/"

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        return response(request, 302, {
            "Location": nearby,
            "X-Archive-Redirect-Reason": "found capture at 20040615000001",
        }, memento=False)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        outcome = workers.download(identity)
    finally:
        workers.close()
    assert outcome.failure is not None
    assert outcome.attempts == 1
    assert len(calls) == 1
    assert nearby not in calls
    assert events(path)[-1]["requests_total"] == 1


def test_requests_redirects_do_not_bypass_gate_or_double_count(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.jsonl"
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(clock())
        if len(calls) == 1:
            return response(request, 302, {"Location": "/web/target"}, memento=False)
        return response(request)

    def download(client, identity):
        with client.session.get("https://web.archive.org/web/start") as result:
            assert len(result.history) == 1
        return playback(identity)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = PlaybackWorkers(
        make_client, download, pace=True, starts_per_second=2,
        sleep=clock.sleep, clock=clock, trace_path=path,
    )
    try:
        assert workers.download(make_capt()).failure is None
    finally:
        workers.close()
    assert calls == [100, 100.5]
    assert events(path)[-1]["requests_total"] == 2


def test_normal_requests_obey_two_per_second_and_120_per_minute(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.jsonl"
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(clock())
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        for i in range(125):
            assert workers.download(make_capt(url=f"http://example.org/{i}")).failure is None
        assert workers.request_stats.snapshot()["starts_last_60s"] == 120
        clock.sleep(60)
        assert workers.request_stats.snapshot()["starts_last_60s"] == 0
    finally:
        workers.close()
    assert calls == [100 + i / 2 for i in range(125)]
    summary = events(path)[-1]
    assert summary["requests_total"] == 125
    assert summary["peak_starts_1s"] == 2
    assert summary["peak_starts_60s"] == 120


@pytest.mark.parametrize("failure", ["http_500", "http_429", "connect_error"])
def test_retry_budget_is_four_retries_with_no_hidden_library_retries(
    tmp_path, monkeypatch, failure,
):
    clock = Clock()
    path = tmp_path / "requests.jsonl"

    def send(adapter, request, **kwargs):
        assert adapter.max_retries.total == 0
        if failure == "connect_error":
            raise requests.ConnectionError("connection lost")
        return response(request, 429 if failure == "http_429" else 500, memento=False)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path, retries=4)
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()
    assert outcome.failure is not None
    assert outcome.attempts == 5
    trace = events(path)
    assert trace[-1]["requests_total"] == 5
    starts = [e for e in trace if e["event"] == "request_start"]
    assert [e["attempt"] for e in starts] == [1, 2, 3, 4, 5]
    assert all(e["request_in_attempt"] == 1 for e in starts)
    assert len([e for e in trace if e["event"] == "request_end"]) == 5
    if failure == "connect_error":
        assert trace[1]["error"] == "ConnectionError"
        assert trace[1]["status"] is None


def test_multiple_worker_sessions_share_one_transport_limit(tmp_path, monkeypatch):
    path = tmp_path / "requests.jsonl"
    barrier = threading.Barrier(3)

    def download(client, identity):
        barrier.wait(timeout=5)
        return download_exact(client, identity)

    monkeypatch.setattr(HTTPAdapter, "send", lambda _adapter, request, **kw: response(request))
    workers = PlaybackWorkers(
        make_client, download, pace=True, starts_per_second=2, max_workers=3,
        sleep=time.sleep, trace_path=path,
    )
    try:
        futures = [workers.submit(lambda group: workers.download(group[0]), [make_capt()]) for _ in range(3)]
        assert all(f.result().failure is None for f in futures)
    finally:
        workers.close()
    starts = [e for e in events(path) if e["event"] == "request_start"]
    assert len({e["thread"] for e in starts}) == 3
    assert all(b["monotonic_s"] - a["monotonic_s"] >= 0.49 for a, b in zip(starts, starts[1:]))
    assert events(path)[-1]["peak_starts_1s"] <= 2


@pytest.mark.parametrize("trace_requests", [False, True])
def test_fetch_deduplicates_and_reuses_without_extra_http_and_logs_summary(
    tmp_path, monkeypatch, trace_requests,
):
    first = make_capt(digest=payload_digest(b"hello"))
    revisit = make_capt(ts="20040615000001", digest=first.payload_digest)
    listing = cdx.CdxResult(
        tuple(ParsedCapture(i, "text/html") for i in (first, first, revisit)),
        "example.org", "domain",
    )
    monkeypatch.setattr(cdx, "fetch_cdx", lambda **kw: listing)
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    result = fetch.run_fetch(fetch.FetchSettings(
        url_pattern="*.example.org", archive_id="example.org",
        date_start="20040101000000", date_end="20041231235959",
        output=FetchOutput("local", tmp_path / "data"),
        playback_workers=1, playback_starts_per_second=2,
        trace_requests=trace_requests,
    ))
    assert result.exit_code == 0
    assert len(calls) == 1
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    run_log = next(result.layout.logs_root.glob("*.log"))
    assert "playback HTTP: requests=1, peak/1s=1, peak/60s=1, 429s=0" in run_log.read_text()
    trace = run_log.with_suffix(".requests.jsonl")
    assert trace.exists() == trace_requests
    if trace_requests:
        assert events(trace)[-1]["requests_total"] == 1
