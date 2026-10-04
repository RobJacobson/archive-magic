"""Exercise the real Wayback/request stack with only HTTP transport mocked."""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime

import archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx as cdx
import archive_magic_fetch.pipeline.run_fetch as fetch
import pytest
import requests
from archive_magic_fetch.adapters.create_wayback_client import make_client
from archive_magic_fetch.adapters.build_wayback_source import build_source
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.models import CaptureRef
from archive_magic_fetch.runtime.track_http_requests import (
    TRACE_COLUMNS,
    RequestStats,
    format_trace_time,
)
from helpers import AcquisitionHarness, fetch_memento, make_capt, make_source, playback
from requests.adapters import HTTPAdapter


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
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def workers_for(clock, path, **kwargs):
    return AcquisitionHarness(
        make_source(
            client_factory=make_client, download=fetch_memento, sleep=clock.sleep
        ),
        pace=True,
        starts_per_second=2,
        sleep=clock.sleep,
        clock=clock,
        trace_path=path,
        **kwargs,
    )


@pytest.mark.parametrize("retry_after", [None, "60", "900"])
def test_429_retry_is_paced_and_traced(tmp_path, monkeypatch, capsys, retry_after):
    clock = Clock()
    path = tmp_path / "requests.csv"
    calls = []
    identity = make_capt(digest=payload_digest(b"hello"))

    def send(_adapter, request, **kwargs):
        calls.append((clock(), request.url))
        if len(calls) == 1:
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return response(request, 429, headers, memento=False)
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        outcome = workers.download(identity)
    finally:
        workers.close()
    assert outcome.failure is None
    assert outcome.attempts == 2
    delay = max(60, int(retry_after or 0))
    assert [t for t, _ in calls] == [100, 100 + delay]
    trace = events(path)
    assert [e["attempt"] for e in trace] == ["1", "2"]
    assert [e["request_in_attempt"] for e in trace] == ["1", "1"]
    assert [e["request_id"] for e in trace] == ["1", "2"]
    assert [e["status"] for e in trace] == ["429", "200"]
    assert trace[0]["retry_after"] == (retry_after or "")
    assert trace[0]["capture_time"] == "2004-06-15T00:00:00"
    assert trace[0]["digest"] == identity.payload_digest[-6:]
    assert (
        datetime.fromisoformat(trace[0]["start_utc"]).utcoffset().total_seconds() == 0
    )
    assert len(trace[0]["start_utc"]) == 24
    assert workers.request_stats.snapshot()["http_429"] == 1
    assert workers.request_stats.snapshot()["peak_starts_1s"] == 1
    output = capsys.readouterr().out
    assert f"cooldown={delay}s, level=1" in output
    assert "Retry-After" not in output


def test_429_backoff_escalates_across_success_and_same_capture_retries(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.csv"
    calls = []
    statuses = iter([429, 200, 429, 429, 200])
    identity = make_capt(digest=payload_digest(b"hello"))

    def send(_adapter, request, **kwargs):
        calls.append(clock())
        status = next(statuses)
        return response(request, status, memento=status == 200)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        first = workers.download(identity)
        second = workers.download(identity)
    finally:
        workers.close()

    assert first.failure is None
    assert second.failure is None
    assert first.attempts == 2
    assert second.attempts == 3
    assert calls == [100, 160, 160.5, 280.5, 460.5]
    trace = events(path)
    assert [e["status"] for e in trace] == ["429", "200", "429", "429", "200"]
    assert all(e["retry_after"] == "" for e in trace)


def test_nearby_redirect_stops_after_one_http_request(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.csv"
    calls = []
    identity = make_capt(digest=payload_digest(b"hello"))
    nearby = "https://web.archive.org/web/20040615000001id_/http://example.org/"

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        return response(
            request,
            302,
            {
                "Location": nearby,
                "X-Archive-Redirect-Reason": "found capture at 20040615000001",
            },
            memento=False,
        )

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
    assert len(events(path)) == 1
    assert events(path)[0]["location"] == nearby


def test_requests_redirects_do_not_bypass_gate_or_double_count(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.csv"
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
    workers = AcquisitionHarness(
        make_source(client_factory=make_client, download=download, sleep=clock.sleep),
        pace=True,
        starts_per_second=2,
        sleep=clock.sleep,
        clock=clock,
        trace_path=path,
    )
    try:
        assert workers.download(make_capt()).failure is None
    finally:
        workers.close()
    assert calls == [100, 100.5]
    assert len(events(path)) == 2


def test_normal_requests_obey_two_per_second_and_120_per_minute(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "requests.csv"
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(clock())
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    workers = workers_for(clock, path)
    try:
        for i in range(125):
            assert (
                workers.download(make_capt(url=f"http://example.org/{i}")).failure
                is None
            )
        assert workers.request_stats.snapshot()["starts_last_60s"] == 120
        clock.sleep(60)
        assert workers.request_stats.snapshot()["starts_last_60s"] == 0
    finally:
        workers.close()
    assert calls == [100 + i / 2 for i in range(125)]
    assert len(events(path)) == 125
    summary = workers.request_stats.snapshot()
    assert summary["requests_total"] == 125
    assert summary["peak_starts_1s"] == 2
    assert summary["peak_starts_60s"] == 120


@pytest.mark.parametrize("failure", ["http_500", "http_429", "connect_error"])
def test_retry_budget_is_four_retries_with_no_hidden_library_retries(
    tmp_path,
    monkeypatch,
    failure,
):
    clock = Clock()
    path = tmp_path / "requests.csv"

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
    assert len(trace) == 5
    assert [e["attempt"] for e in trace] == ["1", "2", "3", "4", "5"]
    assert all(e["request_in_attempt"] == "1" for e in trace)
    if failure == "connect_error":
        assert trace[1]["error"] == "ConnectionError"
        assert trace[1]["status"] == ""


def test_multiple_worker_sessions_share_one_transport_limit(tmp_path, monkeypatch):
    path = tmp_path / "requests.csv"
    barrier = threading.Barrier(3)

    def download(client, identity):
        barrier.wait(timeout=5)
        return fetch_memento(client, identity)

    monkeypatch.setattr(
        HTTPAdapter, "send", lambda _adapter, request, **kw: response(request)
    )
    workers = AcquisitionHarness(
        make_source(client_factory=make_client, download=download, sleep=time.sleep),
        pace=True,
        starts_per_second=2,
        max_workers=3,
        sleep=time.sleep,
        trace_path=path,
    )
    try:
        futures = [
            workers.submit(lambda group: workers.download(group[0]), [make_capt()])
            for _ in range(3)
        ]
        assert all(f.result().failure is None for f in futures)
    finally:
        workers.close()
    starts = sorted(events(path), key=lambda row: int(row["request_id"]))
    assert len({e["thread"] for e in starts}) == 3
    times = [datetime.fromisoformat(row["start_utc"]) for row in starts]
    assert all((b - a).total_seconds() >= 0.49 for a, b in zip(times, times[1:]))
    assert workers.request_stats.snapshot()["peak_starts_1s"] <= 2


@pytest.mark.parametrize("trace_requests", [False, True])
def test_fetch_deduplicates_and_reuses_without_extra_http_and_logs_summary(
    tmp_path,
    monkeypatch,
    trace_requests,
):
    first = make_capt(digest=payload_digest(b"hello"))
    revisit = make_capt(ts="20040615000001", digest=first.payload_digest)
    listing = cdx._CdxResult(
        tuple(CaptureRef(i, "text/html") for i in (first, first, revisit)),
        "example.org",
        "domain",
    )
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **kw: listing)
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    run_settings = FetchSettings(
        url_pattern="*.example.org",
        archive_id="example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        output=FetchOutput("local", tmp_path / "data"),
        playback_workers=1,
        playback_starts_per_second=2,
        trace_requests=trace_requests,
    )
    result = fetch.run_fetch(run_settings, source=make_source(run_settings))
    assert result.exit_code == 0
    assert len(calls) == 1
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    run_log = next(result.layout.logs_root.glob("*.log"))
    assert (
        "HTTP: requests=1, peak/1s=1, peak/60s=1, 429s=0"
        in run_log.read_text()
    )
    trace = run_log.with_suffix(".requests.csv")
    assert trace.exists() == trace_requests
    if trace_requests:
        assert len(events(trace)) == 1


def test_csv_pairs_out_of_order_completions_and_flushes_interrupted_requests(tmp_path):
    clock = Clock()
    path = tmp_path / "requests.csv"
    stats = RequestStats(
        lambda: None, report=lambda _: None, trace_path=path, clock=clock
    )
    first = make_capt(digest="sha1:JC26EGPOB6ITUVFV5IZATNF5MQT65D7V")
    second = make_capt(
        ts="20040615000001", digest="sha1:T3MZUXWWZBHELZF5G3ID6N3BZDYBDGAW"
    )
    request = requests.Request(
        "GET", "https://web.archive.org/web/example?a=1,2"
    ).prepare()
    with stats.attempt(first, 1):
        ticket1 = stats.start(request)
    with stats.attempt(second, 2):
        ticket2 = stats.start(request)
    assert events(path) == []  # Starts stay in memory until completion.
    clock.sleep(0.0126)
    location = 'https://example.org/?value="a,b"\ncontinued'
    stats.finish(ticket2, response=response(request, 302, {"Location": location}))
    assert events(path)[0]["duration_ms"] == "13"  # Flushed before close.
    with stats.attempt(first, 3):
        stats.start(request)  # Interrupted before headers.
    clock.sleep(2)
    stats.finish(ticket1, response=response(request))
    stats.close()
    rows = events(path)
    assert [r["request_id"] for r in rows] == ["2", "1", "3"]
    assert [r["digest"] for r in rows] == ["YBDGAW", "T65D7V", "T65D7V"]
    assert rows[0]["location"] == location
    assert rows[0]["url"] == request.url
    assert rows[0]["starts_last_1s"] == "2"
    assert rows[1]["starts_last_1s"] == "1"  # Counts at start, not finish.
    assert rows[1]["duration_ms"] == "2013"
    assert rows[2]["error"] == "Interrupted"
    assert rows[2]["duration_ms"] == rows[2]["status"] == ""
    assert tuple(rows[0]) == TRACE_COLUMNS
    assert TRACE_COLUMNS[-5:] == ("thread", "retry_after", "error", "url", "location")
    assert "urlkey" not in path.read_text()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2026-10-03T23:53:56.980784+00:00", "2026-10-03T23:53:56.981Z"),
        ("2026-10-03T23:59:59.999800+00:00", "2026-10-04T00:00:00.000Z"),
    ],
)
def test_trace_timestamps_round_to_milliseconds(source, expected):
    assert format_trace_time(datetime.fromisoformat(source)) == expected


@pytest.mark.parametrize("rate, starts", [
    (0.25, [100, 160, 164, 168]),
    (8, [100, 160, 162.5, 162.625]),
])
@pytest.mark.parametrize("trace_requests", [False, True])
def test_cdx_retry_pagination_and_playback_share_transport_gate(
    tmp_path, monkeypatch, rate, starts, trace_requests,
):
    clock = Clock()
    calls = []
    digest = payload_digest(b"hello").removeprefix("sha1:")
    cdx_page = (
        f"org,example)/ 20040615000000 http://example.org/ text/html 200 {digest} 5\n"
        "\nresume-token\n"
    ).encode()

    def send(adapter, request, **kwargs):
        assert adapter.max_retries.total == 0
        calls.append((clock(), request.url))
        if "/cdx/" in request.url:
            result = response(request, 429 if len(calls) == 1 else 200, memento=False)
            result._content = (
                b"temporarily rate limited" if len(calls) == 1 else
                b"" if "resumeKey=" in request.url else cdx_page
            )
            return result
        return response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    settings = FetchSettings(
        url_pattern="*.example.org", archive_id="example.org",
        date_start="20040101000000", date_end="20041231235959",
        output=FetchOutput("local", tmp_path / "data"), playback_workers=1,
        playback_starts_per_second=rate, trace_requests=trace_requests,
    )
    source = build_source(index_directory=tmp_path / "index", sleep=clock.sleep)
    result = fetch.run_fetch(settings, source=source, clock=clock, sleep=clock.sleep)
    assert result.exit_code == 0
    assert result.metrics.downloads == 1
    assert [t for t, _ in calls] == starts
    run_log = next(result.layout.logs_root.glob("*.log"))
    assert "HTTP: requests=4" in run_log.read_text()
    assert "cdx HTTP 429 request #1" in run_log.read_text()
    if trace_requests:
        rows = events(run_log.with_suffix(".requests.csv"))
        assert [r["phase"] for r in rows] == ["cdx", "cdx", "cdx", "playback"]
        assert [r["attempt"] for r in rows] == ["1", "2", "2", "1"]
        assert [r["request_in_attempt"] for r in rows] == ["1", "1", "2", "1"]
        assert [r["capture_time"] for r in rows[:3]] == ["", "", ""]
    diagnostics = [json.loads(line) for line in run_log.with_suffix(".429.jsonl").read_text().splitlines()]
    assert len(diagnostics) == 1
    assert diagnostics[0]["request_id"] == 1
    assert diagnostics[0]["phase"] == "cdx"
    assert diagnostics[0]["body_excerpt"] == "temporarily rate limited"


def test_cdx_waits_do_not_consume_query_timeout(tmp_path, monkeypatch):
    from archive_magic_fetch.runtime.pace_requests import StartGate

    gate = StartGate(10)
    stats = RequestStats(gate.wait, report=lambda _: None)
    gate.wait()  # The CDX request must wait 0.1s, beyond its 0.03s I/O budget.
    monkeypatch.setattr(cdx, "DEFAULT_CDX_TIMEOUT_SECONDS", 0.03)

    def send(adapter, request, **kwargs):
        result = response(request, memento=False)
        result._content = b""
        return result

    monkeypatch.setattr(HTTPAdapter, "send", send)
    try:
        result = cdx._fetch_cdx(
            url_pattern="example.org", date_start="20040101000000",
            date_end="20041231235959", stats=stats,
        )
        assert result.captures == ()
        assert stats.snapshot()["requests_total"] == 1
    finally:
        stats.close()


def test_timed_out_cdx_cannot_send_another_page(tmp_path, monkeypatch):
    from archive_magic_fetch.runtime.pace_requests import StartGate

    release = threading.Event()
    settled = threading.Event()
    calls = []
    stats = RequestStats(StartGate(0).wait, report=lambda _: None)
    monkeypatch.setattr(cdx, "DEFAULT_CDX_TIMEOUT_SECONDS", 0.03)
    original = cdx._materialize_cdx_search

    def search(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            settled.set()

    def send(adapter, request, **kwargs):
        calls.append(request.url)
        assert release.wait(2)
        result = response(request, memento=False)
        result._content = b"\nresume-token\n"
        return result

    monkeypatch.setattr(cdx, "_materialize_cdx_search", search)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    try:
        with pytest.raises(RuntimeError, match="wall-clock budget"):
            cdx._fetch_cdx(
                url_pattern="example.org", date_start="20040101000000",
                date_end="20041231235959", stats=stats,
            )
    finally:
        release.set()
        assert settled.wait(2)
        stats.close()
    assert len(calls) == 1


def test_closed_request_stats_cannot_send(tmp_path, monkeypatch):
    from concurrent.futures import CancelledError
    from unittest.mock import Mock

    wait = Mock()
    stats = RequestStats(wait, report=lambda _: None)
    stats.close()
    with pytest.raises(CancelledError):
        stats.start(requests.Request("GET", "https://example.org").prepare())
    wait.assert_not_called()
