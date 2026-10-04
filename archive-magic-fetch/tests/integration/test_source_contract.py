"""Exercise the source boundary independently of Wayback protocols and sessions."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.contracts import FailureAdvice, SourceAdapter
from archive_magic_fetch.models import (
    CaptureListing,
    CaptureRef,
    CaptureResult,
    FailureCategory,
)
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from helpers import make_capt, playback
from warcio.archiveiterator import ArchiveIterator


def test_neutral_source_discovers_resolves_writes_indexes_and_resumes(tmp_path):
    # This is legitimate content for this source, even though Wayback treats it as a stub.
    body = b"Invalid URI"
    first = CaptureRef(
        make_capt(digest=payload_digest(body)), "application/octet-stream"
    )
    later = replace(first, identity=replace(first.identity, timestamp="20040616000000"))
    outside = replace(
        first, identity=replace(first.identity, timestamp="20040101000000")
    )
    calls, clients, closed, requests = [], [], [], []

    def discover(request):
        requests.append(request)
        return CaptureListing((later, outside, first, first), {"provider": "fixture"})

    @contextmanager
    def open_client(stats):
        client = object()
        clients.append(client)
        try:
            yield client
        finally:
            closed.append(client)

    def fetch(client, capture):
        assert capture is first
        assert capture.mime == "application/octet-stream"
        calls.append((client, capture))
        return CaptureResult(
            capture.identity,
            body,
            200,
            (("Content-Type", capture.mime),),
            "2004-06-15T00:00:00Z",
            "fixture://capture/first",
            payload_digest(body),
        )

    source = SourceAdapter(
        discover,
        open_client,
        fetch,
        lambda ref: None,
        lambda error, attempt: FailureAdvice(FailureCategory.UNAVAILABLE, False),
        lambda capture: "fixture://capture/" + capture.identity.timestamp,
    )
    settings = FetchSettings(
        "example.org",
        "20040601000000",
        "20041231235959",
        "example.org",
        FetchOutput("local", tmp_path / "data"),
        playback_workers=1,
    )
    result = run_fetch(settings, source=source)
    assert result.exit_code == 0
    assert result.metrics.downloads == result.metrics.revisits == 1
    assert result.metrics.selected == 2
    assert len(calls) == len(clients) == 1
    assert closed == clients
    assert requests[0].year == 2004
    with next(settings.output.data_directory.glob("*.warc.gz")).open("rb") as stream:
        records = [
            (record.rec_type, record.content_stream().read())
            for record in ArchiveIterator(stream)
        ]
    assert [kind for kind, _ in records] == ["warcinfo", "response", "revisit"]
    assert records[1][1] == body
    index = next(settings.output.data_directory.glob("*.cdxj")).read_text().splitlines()
    assert len(index) == 2
    assert [json.loads(line.split(" ", 2)[2])["mime"] for line in index] == [
        "application/octet-stream",
        "warc/revisit",
    ]
    resumed = run_fetch(settings, source=source)
    assert resumed.metrics.local_reuses == 2
    assert len(calls) == len(clients) == 1


@pytest.mark.parametrize("failure", [ValueError("decode failed"), KeyboardInterrupt()])
def test_worker_client_closes_when_acquisition_fails_or_is_interrupted(
    tmp_path, failure
):
    closed = []
    capture = CaptureRef(make_capt(), "text/html")

    @contextmanager
    def open_client(stats):
        try:
            yield object()
        finally:
            closed.append(True)

    def fetch(client, reference):
        assert reference is capture
        raise failure

    source = SourceAdapter(
        lambda request: CaptureListing((capture,), {}),
        open_client,
        fetch,
        lambda ref: None,
        lambda error, attempt: FailureAdvice(FailureCategory.UNAVAILABLE, False),
        lambda identity: "fixture://capture",
    )
    settings = FetchSettings(
        "example.org",
        "20040101000000",
        "20041231235959",
        "example.org",
        FetchOutput("local", tmp_path / "data"),
        playback_workers=1,
    )
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_fetch(settings, source=source)
    else:
        result = run_fetch(settings, source=source)
        assert result.metrics.unresolved == 1
        assert result.failures[0].message == str(failure)
    assert closed == [True]
    assert not list(settings.output.data_directory.glob("*.warc.gz"))
    assert not (settings.output.data_directory / ".staging").exists()


def _annual_fixture(tmp_path):
    captures = tuple(
        CaptureRef(
            make_capt(ts=f"{year}060{day}000000", digest=payload_digest(b"hello")),
            "text/html",
        )
        for year in (2004, 2005)
        for day in (1, 2)
    )
    calls = []

    @contextmanager
    def open_client(stats):
        yield object()

    def fetch(client, capture):
        calls.append(capture.identity)
        return playback(capture.identity)

    def discover(request):
        return CaptureListing(
            tuple(c for c in captures if c.identity.timestamp.startswith(str(request.year))),
            {},
        )

    source = SourceAdapter(
        discover,
        open_client,
        fetch,
        lambda ref: None,
        lambda error, attempt: FailureAdvice(FailureCategory.UNAVAILABLE, False),
        lambda identity: "fixture://capture",
    )
    settings = FetchSettings(
        "example.org",
        "20040101000000",
        "20051231235959",
        "example.org",
        FetchOutput("local", tmp_path / "data"),
        playback_workers=2,
    )
    return settings, source, calls


@pytest.mark.parametrize("empty", [False, True])
def test_reset_earlier_year_leaves_later_year_self_contained(tmp_path, empty):
    from archive_magic_fetch.archive.validate_local_archive import validate_local_archive

    settings, source, calls = _annual_fixture(tmp_path)
    initial = run_fetch(settings, source=source)
    assert initial.metrics.downloads == initial.metrics.revisits == 2
    assert len(calls) == 2
    later_files = {path: path.read_bytes() for path in initial.layout.root.glob("*-2005-*")}
    replacement = CaptureRef(make_capt(digest=payload_digest(b"changed")), "text/html")
    resetting = replace(
        source,
        discover=lambda _: CaptureListing(() if empty else (replacement,), {}),
        fetch=lambda client, capture: playback(capture.identity, body=b"changed"),
    )
    result = run_fetch(
        replace(settings, date_end="20041231235959", reset_data=True),
        source=resetting,
    )
    assert result.exit_code == 0
    assert all(path.read_bytes() == data for path, data in later_files.items())
    if empty:
        assert not list(result.layout.root.glob("*-2004-*"))
    validate_local_archive(result.layout)
    responses = {}
    revisits = []
    with next(result.layout.root.glob("*-2005-*.warc.gz")).open("rb") as stream:
        for record in ArchiveIterator(stream):
            headers = record.rec_headers
            if record.rec_type == "response":
                key = (
                    headers.get_header("WARC-Target-URI"),
                    headers.get_header("WARC-Date"),
                    headers.get_header("WARC-Payload-Digest"),
                )
                responses[key] = record.content_stream().read()
            elif record.rec_type == "revisit":
                revisits.append(
                    (
                        headers.get_header("WARC-Refers-To-Target-URI"),
                        headers.get_header("WARC-Refers-To-Date"),
                        headers.get_header("WARC-Payload-Digest"),
                    )
                )
    assert len(revisits) == 1
    assert responses[revisits[0]] == b"hello"


def test_prior_year_success_does_not_hide_later_year_failures(tmp_path):
    settings, source, calls = _annual_fixture(tmp_path)
    fetch = source.fetch

    def fail_later(client, capture):
        if capture.identity.timestamp.startswith("2005"):
            raise ValueError("unavailable in 2005")
        return fetch(client, capture)

    result = run_fetch(settings, source=replace(source, fetch=fail_later))
    assert result.metrics.downloads == result.metrics.revisits == 1
    assert result.metrics.unresolved == 2
    assert len(calls) == 1
    assert all(f.identity.timestamp.startswith("2005") for f in result.failures)
    assert not list(result.layout.root.glob("*-2005-*"))


@pytest.mark.parametrize("failure", [OSError("writer failed"), KeyboardInterrupt()])
def test_failed_year_drains_workers_before_abort_and_next_year(
    tmp_path, monkeypatch, failure
):
    from threading import Event
    from types import SimpleNamespace

    from archive_magic_fetch.pipeline.write_captures import _CollectionWarcWriter
    from helpers import make_collection

    layout = make_collection(tmp_path / "data")
    original_files = {path: path.read_bytes() for path in layout.root.glob("*-2004-*")}
    started, settled = Event(), Event()
    clients, closed, attempts, discoveries = [], [], [], []
    captures = tuple(
        CaptureRef(
            make_capt(url=f"http://example.org/{url}", ts=ts, digest=payload_digest(body)),
            "text/html",
        )
        for url, ts, body in (
            ("a", "20040601000000", b"a"),
            ("b", "20040601000001", b"b1"),
            ("b", "20040601000002", b"b2"),
            ("a", "20050601000000", b"a"),
        )
    )

    @contextmanager
    def open_client(stats):
        client = SimpleNamespace(stats=stats)
        clients.append(client)
        try:
            yield client
        finally:
            assert settled.is_set()
            closed.append(client)

    def discover(request):
        discoveries.append(request.year)
        if request.year == 2005:
            assert settled.is_set()
        return CaptureListing(
            tuple(c for c in captures if c.identity.timestamp.startswith(str(request.year))),
            {},
        )

    def fetch(client, capture):
        attempts.append(capture)
        if capture is captures[0]:
            assert started.wait(2)
        if capture is captures[1]:
            started.set()
            assert client.stats._local.cancelled.wait(2)
            settled.set()
        return playback(capture.identity)

    write = _CollectionWarcWriter.write_playback

    def fail_first_year(writer, result):
        if result.identity.timestamp.startswith("2004"):
            raise failure
        write(writer, result)

    monkeypatch.setattr(_CollectionWarcWriter, "write_playback", fail_first_year)
    source = SourceAdapter(
        discover,
        open_client,
        fetch,
        lambda _: None,
        lambda error, attempt: FailureAdvice(FailureCategory.UNAVAILABLE, False),
        lambda identity: "fixture://capture",
    )
    settings = FetchSettings(
        "example.org",
        "20040101000000",
        "20051231235959",
        "example.org",
        FetchOutput("local", layout.root),
        playback_workers=2,
        trace_requests=True,
    )
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as raised:
            run_fetch(settings, source=source)
        assert raised.value is failure
        assert discoveries == [2004]
    else:
        result = run_fetch(settings, source=source)
        assert result.failed_years == (2004,)
        assert result.metrics.downloads == 1
        assert not result.failures
        assert discoveries == [2004, 2005]
    assert settled.is_set()
    assert captures[2] not in attempts
    assert len(clients) == len(closed) == 2
    assert all(client.stats._stream is None for client in clients)
    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert not (layout.root / ".staging").exists()
