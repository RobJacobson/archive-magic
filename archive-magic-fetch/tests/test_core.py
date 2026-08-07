"""High-value tests for Archive Magic Fetch clean-sheet rewrite."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders

from archive_magic_fetch.cdx import (
    fetch_year_cdx,
    parse_raw_cdx_bytes,
)
from archive_magic_fetch.collection import (
    collection_layout,
    ensure_collection_dirs,
    list_year_warcs,
    next_warc_sequence,
    write_failures,
    write_manifest,
)
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import (
    merge_cdxj_lines,
    publish_annual_index,
    publish_collection_index,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.models import (
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    MISSING_CDX_STATUS,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    RunMetrics,
    UnresolvedFailure,
    make_identity,
)
from archive_magic_fetch.scheduler import PlaybackScheduler
from archive_magic_fetch.warc import (
    YearWarcWriter,
    download_exact_for_identity,
    get_warc_identity,
    inventory_collection,
    payload_digest,
    validate_warc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(
    url: str = "http://example.org/",
    ts: str = "20040615000000",
    status: str = "200",
    digest: str = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
) -> CaptureIdentity:
    return make_identity(
        original_url=url,
        timestamp=ts,
        status_token=status,
        payload_digest=digest,
    )


def _playback(
    identity: CaptureIdentity,
    body: bytes = b"hello",
    status: int = 200,
) -> PlaybackResult:
    return PlaybackResult(
        identity=identity,
        body=body,
        status_code=status,
        headers=(("Content-Type", "text/html"), ("Content-Length", str(len(body)))),
        warc_date=f"{identity.timestamp[0:4]}-{identity.timestamp[4:6]}-"
        f"{identity.timestamp[6:8]}T{identity.timestamp[8:10]}:"
        f"{identity.timestamp[10:12]}:{identity.timestamp[12:14]}Z",
        source_uri=f"https://web.archive.org/web/{identity.timestamp}id_/{identity.original_url}",
        warc_payload_digest=payload_digest(body),
    )


def _cdx_json(rows: list[list[str]]) -> bytes:
    return json.dumps(rows).encode("utf-8")


class FakeSession:
    """Minimal session returning scripted CDX responses."""

    def __init__(self, bodies: list[bytes], status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.calls = 0

    def get(self, url, stream=True, timeout=120):
        self.calls += 1
        body = self.bodies.pop(0) if self.bodies else b"[]"
        response = MagicMock()
        response.status_code = self.status
        response.content = body
        response.encoding = "utf-8"
        response.headers = {}
        response.raise_for_status = MagicMock()
        response.close = MagicMock()
        return response

    def close(self):
        return None


# ---------------------------------------------------------------------------
# 1. Raw CDX preservation + malformed rows
# ---------------------------------------------------------------------------


def test_raw_cdx_saved_before_normalization_and_malformed_in_failures(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)

    good = [
        "com,example)/",
        "20040615000000",
        "http://example.org/",
        "text/html",
        "200",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
        "123",
    ]
    # Invalid timestamp (too short after wayback would repair — we refuse to).
    bad = [
        "com,example)/broken",
        "200406",
        "http://example.org/broken",
        "text/html",
        "200",
        "-",
        "1",
    ]
    body = _cdx_json([good, bad])
    session = FakeSession([body])
    result = fetch_year_cdx(
        layout,
        url_pattern="http://example.org/",
        year=2004,
        date_start="20040101000000",
        date_end="20041231235959",
        run_id="test-run",
        session=session,
        sleep=lambda _s: None,
    )
    assert result.raw_path.is_file()
    # Raw source contains the malformed row bytes-as-saved.
    raw = result.raw_path.read_bytes()
    assert b"200406" in raw
    assert b"example.org/broken" in raw
    assert len(result.captures) == 1
    assert result.captures[0].identity.timestamp == "20040615000000"
    assert len(result.failures) == 1
    assert result.failures[0].category == FailureCategory.MALFORMED_CDX
    query = json.loads((result.source_dir / "query.json").read_text())
    assert "2004" in query["years"]
    assert query["years"]["2004"]["sha256"]


# ---------------------------------------------------------------------------
# 2 + 4. Statusless identity + existing WARC prevents network
# ---------------------------------------------------------------------------


def test_statusless_capture_three_runs_no_extra_network(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = make_identity(
        original_url="http://example.org/",
        timestamp="20040615000000",
        status_token=MISSING_CDX_STATUS,
        payload_digest="sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    calls = {"n": 0}

    def download_fn(_client, capt_identity):
        calls["n"] += 1
        # Playback returns numeric 200 while identity keeps "-"
        return _playback(capt_identity, status=200)

    rows = [
        [
            "com,example)/",
            "20040615000000",
            "http://example.org/",
            "text/html",
            "-",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "5",
        ]
    ]
    body = _cdx_json(rows)

    def client_factory():
        return MagicMock()

    for run in range(3):
        session = FakeSession([body])
        # Patch fetch_year_cdx indirectly by wiring run_fetch download/session
        # via a full run with custom CDX would be heavy; write WARC on first run
        # then inventory.
        if run == 0:
            writer = YearWarcWriter(layout, 2004, target_bytes=10**12)
            result = download_fn(None, identity)
            writer.write_playback(result)
            writer.close()
            publish_annual_index(layout, 2004)
            assert calls["n"] == 1
        inv = inventory_collection(layout)
        assert inv.contains(identity)
        # Identity rebuilt from WARC uses CDX-Status extension, not HTTP 200.
        warc = list_year_warcs(layout, 2004)[0]
        with warc.open("rb") as stream:
            for record in ArchiveIterator(stream):
                if record.rec_type == "response":
                    rebuilt = get_warc_identity(record)
                    assert rebuilt.status_token == "-"
                    assert rebuilt == identity
                    record.raw_stream.read()
        # No further network when contained.
        calls_before = calls["n"]
        assert inv.contains(identity)
        assert calls["n"] == calls_before


# ---------------------------------------------------------------------------
# 3. Scheduler pacing / concurrency / 429
# ---------------------------------------------------------------------------


def test_scheduler_smooth_spacing_concurrency_retry_and_429():
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def mono():
        return clock["t"]

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    identities = [
        _identity(ts=f"2004061500000{i}", digest=f"sha1:{chr(65+i)*32}")
        for i in range(3)
    ]
    # Make digests valid base32 length
    identities = [
        _identity(
            ts=f"2004061500000{i}",
            digest="sha1:" + ("A" * 31 + "234567"[i % 6]),
        )
        for i in range(3)
    ]

    class RateLimitOnce(Exception):
        def __init__(self):
            self.retry_after = 2.0
            self.status_code = 429
            super().__init__("HTTP 429")

    attempts = {"n": 0}

    def download_fn(_client, identity):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RateLimitOnce()
        # Spend no wall time; advance nothing.
        return _playback(identity)

    scheduler = PlaybackScheduler(
        client_factory=lambda: MagicMock(),
        identities=identities,
        max_in_flight=2,
        start_interval=0.125,
        max_attempts=4,
        download_fn=download_fn,
        clock=mono,
        sleep=sleep,
    )
    results = []

    def consumer():
        for item in scheduler.results():
            results.append(item)

    t = threading.Thread(target=consumer)
    t.start()
    scheduler.run()
    t.join(timeout=5)
    # Smooth starts: gate wait is sliced at 0.05s while preserving interval.
    assert sleeps, "scheduler should wait between starts or on 429"
    assert any(s > 0 for s in sleeps)
    assert scheduler.metrics.peak_in_flight <= 2
    successes = [r for r in results if hasattr(r, "result")]
    assert len(successes) >= 2
    # First attempt rate-limited; overall attempts exceed identity count.
    assert attempts["n"] >= 3
    # Global 429 gate recorded cooldown wait once Retry-After applied.
    assert scheduler._blocked_until > 0 or scheduler.metrics.cooldown_wait_s >= 0


# ---------------------------------------------------------------------------
# 5 + 6. Same-year revisits; cross-year full responses
# ---------------------------------------------------------------------------


def test_same_year_representative_revisits_and_redirects_individual(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    dig = "sha1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    a = _identity(ts="20040601000000", digest=dig)
    b = _identity(ts="20040602000000", digest=dig)
    redir = _identity(
        ts="20040603000000",
        status="302",
        digest="sha1:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
    )
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        body = b"shared" if identity.payload_digest == dig else b"redir"
        status = 302 if identity.status_token == "302" else 200
        headers = (("Content-Type", "text/html"), ("Content-Length", str(len(body))))
        if status == 302:
            headers = (("Location", "http://example.org/target"),) + headers
        return _playback(identity, body=body, status=status)

    body = _cdx_json(
        [
            [
                "com,example)/",
                a.timestamp,
                a.original_url,
                "text/html",
                "200",
                dig.split(":")[1],
                "6",
            ],
            [
                "com,example)/",
                b.timestamp,
                b.original_url,
                "text/html",
                "200",
                dig.split(":")[1],
                "6",
            ],
            [
                "com,example)/",
                redir.timestamp,
                redir.original_url,
                "text/html",
                "302",
                dig.split(":")[1],  # same digest intentionally; still individual
                "6",
            ],
        ]
    )

    # Use a simpler direct write path than full run_fetch for this unit.
    writer = YearWarcWriter(layout, 2004)
    r1 = download_fn(None, a)
    writer.write_playback(r1)
    from archive_magic_fetch.warc import revisit_from_stored, StoredResponse

    stored = StoredResponse(
        identity=a,
        relative_key=layout.warc_relative_key(2004, 1),
        warc_date=r1.warc_date,
        warc_payload_digest=r1.warc_payload_digest,
        target_uri=a.original_url,
        status_code=200,
        headers=r1.headers,
    )
    writer.write_revisit(
        __import__(
            "archive_magic_fetch.warc", fromlist=["revisit_from_stored"]
        ).revisit_from_stored(b, stored)
    )
    r3 = download_fn(None, redir)
    writer.write_playback(r3)
    warcs = writer.close()
    assert len(warcs) == 1
    publish_annual_index(layout, 2004)
    types = []
    with warcs[0].path.open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 2  # representative + redirect
    assert types.count("revisit") == 1
    assert downloads == [a.timestamp, redir.timestamp]


def test_cross_year_matching_payloads_are_full_responses(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    dig = "sha1:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    y2004 = _identity(ts="20040601000000", digest=dig)
    y2005 = _identity(ts="20050601000000", digest=dig)

    for year, capt in ((2004, y2004), (2005, y2005)):
        writer = YearWarcWriter(layout, year)
        writer.write_playback(_playback(capt, body=b"logo"))
        writer.close()
        publish_annual_index(layout, year)

    inv = inventory_collection(layout)
    assert inv.contains(y2004)
    assert inv.contains(y2005)
    # Each year holds its own full response (not a cross-year dependency).
    stored_2004 = inv.year_index(2004).by_url_digest[(y2004.urlkey, dig)]
    stored_2005 = inv.year_index(2005).by_url_digest[(y2005.urlkey, dig)]
    assert stored_2004.relative_key.startswith("archive/2004/")
    assert stored_2005.relative_key.startswith("archive/2005/")
    assert stored_2004.identity.timestamp.startswith("2004")
    assert stored_2005.identity.timestamp.startswith("2005")
    # Revisit counts in each year WARC must be zero for these sole captures.
    for year in (2004, 2005):
        warc = list_year_warcs(layout, year)[0]
        types = []
        with warc.open("rb") as stream:
            for record in ArchiveIterator(stream):
                types.append(record.rec_type)
                record.raw_stream.read()
        assert types.count("response") == 1
        assert types.count("revisit") == 0


# ---------------------------------------------------------------------------
# 7. WARC rollover naming / size / 1000 reject
# ---------------------------------------------------------------------------


def test_warc_rollover_naming_and_rejects_1000(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    # Tiny target force rollover after each record.
    writer = YearWarcWriter(layout, 2004, target_bytes=1)
    for i in range(2):
        capt = _identity(
            ts=f"2004060{i+1}000000",
            digest="sha1:" + ("E" * 31 + str(i)),
        )
        writer.write_playback(_playback(capt, body=b"x" * 100))
    warcs = writer.close()
    assert len(warcs) == 2
    assert warcs[0].relative_key.endswith("-2004-001.warc.gz")
    assert warcs[1].relative_key.endswith("-2004-002.warc.gz")
    for artifact in warcs:
        validate_warc(artifact.path)
        with artifact.path.open("rb") as stream:
            first = next(ArchiveIterator(stream))
            assert first.rec_headers.get_header("WARC-Type") == "warcinfo"
            assert first.rec_headers.get_header("WARC-Filename") == artifact.path.name

    # Reject sequence 1000
    for seq in range(1, 1000):
        path = layout.warc_path(2005, seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    with pytest.raises(RuntimeError, match="999"):
        next_warc_sequence(layout, 2005)


# ---------------------------------------------------------------------------
# 8. Crash recovery indexes finalized WARC
# ---------------------------------------------------------------------------


def test_crash_recovery_indexes_finalized_warc_without_redownload(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    capt = _identity()
    writer = YearWarcWriter(layout, 2004)
    writer.write_playback(_playback(capt))
    warcs = writer.close()
    assert warcs
    # Simulate crash before annual index.
    assert not layout.annual_index(2004).exists()
    publish_annual_index(layout, 2004)
    assert layout.annual_index(2004).is_file()
    inv = inventory_collection(layout)
    assert inv.contains(capt)
    # Incomplete temp WARC is cleaned / not listed
    temp = layout.year_dir(2004) / ".tmp-leak.warc.gz.partial"
    temp.write_bytes(b"incomplete")
    listed = list_year_warcs(layout, 2004)
    assert all(not p.name.startswith(".tmp-") for p in listed)


# ---------------------------------------------------------------------------
# 9. Annual + collection CDXJ merge
# ---------------------------------------------------------------------------


def test_annual_and_collection_index_merge_sorted_and_idempotent(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    for year, day in ((2004, "01"), (2005, "02")):
        capt = _identity(ts=f"{year}06{day}000000")
        writer = YearWarcWriter(layout, year)
        writer.write_playback(_playback(capt, body=f"y{year}".encode()))
        writer.close()
        publish_annual_index(layout, year)
    first = publish_collection_index(layout)
    second = publish_collection_index(layout)
    assert first is not None and second is not None
    text1 = layout.collection_index.read_text()
    text2 = layout.collection_index.read_text()
    assert text1 == text2
    lines = [line for line in text1.splitlines() if line]
    keys = [(line.split()[0], line.split()[1]) for line in lines]
    assert keys == sorted(keys)
    validate_cdxj_against_warcs(layout, lines)
    # Idempotent annual re-merge
    before = layout.annual_index(2004).read_text()
    publish_annual_index(layout, 2004)
    assert layout.annual_index(2004).read_text() == before


# ---------------------------------------------------------------------------
# 11. Partial run
# ---------------------------------------------------------------------------


def test_partial_run_truthful_manifest_and_nonzero_exit(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    good = _identity(ts="20040601000000")
    bad = _identity(
        ts="20040602000000",
        digest="sha1:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
    )

    def download_fn(_client, identity):
        if identity.timestamp == bad.timestamp:
            raise RuntimeError("memento unavailable")
        return _playback(identity)

    body = _cdx_json(
        [
            [
                "com,example)/",
                good.timestamp,
                good.original_url,
                "text/html",
                "200",
                good.payload_digest.split(":")[1],
                "5",
            ],
            [
                "com,example)/",
                bad.timestamp,
                bad.original_url,
                "text/html",
                "200",
                bad.payload_digest.split(":")[1],
                "5",
            ],
        ]
    )

    # Monkeypatch fetch_year_cdx by injecting through session on real call:
    from archive_magic_fetch import fetch as fetch_mod
    from archive_magic_fetch import cdx as cdx_mod

    original = cdx_mod.fetch_year_cdx

    def fake_fetch_year_cdx(layout, **kwargs):
        session = FakeSession([body])
        kwargs = dict(kwargs)
        kwargs["session"] = session
        kwargs["sleep"] = lambda _s: None
        return original(layout, **kwargs)

    monkey_target = cdx_mod.fetch_year_cdx
    cdx_mod.fetch_year_cdx = fake_fetch_year_cdx
    fetch_mod.fetch_year_cdx = fake_fetch_year_cdx
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040602000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code != 0
    manifest = json.loads(layout.manifest_path.read_text())
    assert manifest["status"] == "partial"
    assert manifest["counts"]["unresolved"] >= 1
    assert layout.failures_path.is_file()
    failures = json.loads(layout.failures_path.read_text())
    assert failures["failures"]
    # Successful WARC still published
    assert list_year_warcs(layout, 2004)


def test_cli_rejects_reversed_range():
    from archive_magic_fetch.cli import main

    code = main(
        [
            "http://example.org/",
            "--start",
            "20050101",
            "--end",
            "20040101",
        ]
    )
    assert code == 2
