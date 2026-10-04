"""Exercise the source boundary independently of Wayback protocols and sessions."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.settings import FetchSettings
from archive_magic_fetch.contracts import FailureAdvice, SourceAdapter
from archive_magic_fetch.models import (
    CaptureListing,
    CaptureRef,
    CaptureResult,
    FailureCategory,
)
from archive_magic_fetch.pipeline.runner import run_fetch
from helpers import make_capt
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
        lambda identity: "fixture://capture/" + identity.timestamp,
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
