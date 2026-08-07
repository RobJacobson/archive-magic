"""High-value tests for Archive Magic Fetch clean-sheet rewrite."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from warcio.archiveiterator import ArchiveIterator
from wayback.exceptions import MementoPlaybackError, RateLimitError

from archive_magic_fetch.cdx import (
    fetch_year_cdx,
    parse_date_bound,
    year_bounds,
)
from archive_magic_fetch.collection import (
    collection_layout,
    ensure_collection_dirs,
    list_year_warcs,
    load_failures,
    next_warc_sequence,
    write_failures,
)
from archive_magic_fetch.fetch import FetchSettings, build_settings, run_fetch
from archive_magic_fetch.index import (
    publish_annual_index,
    publish_collection_index,
    validate_annual_revisit_closure,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.models import (
    CDX_URLKEY_HEADER,
    DEFAULT_DATE_START,
    MISSING_CDX_STATUS,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    UnresolvedFailure,
    make_identity,
    normalize_original_url,
)
from archive_magic_fetch.scheduler import PlaybackScheduler
from archive_magic_fetch.warc import (
    YearWarcWriter,
    classify_playback_error,
    get_warc_identity,
    inventory_collection,
    payload_digest,
    validate_warc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_normalize_original_url_strips_default_ports():
    assert (
        normalize_original_url("http://www.example.org:80/path")
        == "http://www.example.org/path"
    )
    assert (
        normalize_original_url("https://www.example.org:443/path?q=1#f")
        == "https://www.example.org/path?q=1#f"
    )
    assert (
        normalize_original_url("http://www.example.org:8080/path")
        == "http://www.example.org:8080/path"
    )
    assert (
        normalize_original_url("http://[2001:db8::1]:80/x")
        == "http://[2001:db8::1]/x"
    )
    identity = make_identity(
        original_url="http://example.org:80/",
        timestamp="20040615000000",
        status_token="200",
        payload_digest="sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    assert identity.original_url == "http://example.org/"


def _identity(
    url: str = "http://example.org/",
    ts: str = "20040615000000",
    status: str = "200",
    digest: str = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    urlkey: Optional[str] = None,
) -> CaptureIdentity:
    return make_identity(
        original_url=url,
        timestamp=ts,
        status_token=status,
        payload_digest=digest,
        urlkey=urlkey,
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


class _FakeRaw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, decode_content: bool = False):
        return self._body


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
        response.headers = {"Content-Encoding": "identity"}
        response.raw = _FakeRaw(body)
        response.raise_for_status = MagicMock()
        response.close = MagicMock()
        return response

    def close(self):
        return None


def _patch_cdx(body: bytes):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_year_cdx

    def fake_fetch_year_cdx(layout, **kwargs):
        kwargs = dict(kwargs)
        kwargs["session"] = FakeSession([body])
        kwargs["sleep"] = lambda _s: None
        return original(layout, **kwargs)

    cdx_mod.fetch_year_cdx = fake_fetch_year_cdx
    fetch_mod.fetch_year_cdx = fake_fetch_year_cdx
    return original, cdx_mod, fetch_mod


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
    raw = result.raw_path.read_bytes()
    assert raw == body
    assert b"200406" in raw
    assert len(result.captures) == 1
    assert len(result.failures) == 1
    assert result.failures[0].category == FailureCategory.MALFORMED_CDX
    query = json.loads((result.source_dir / "query.json").read_text())
    assert query["years"]["2004"]["response_encoding"] == "identity"
    assert query["years"]["2004"]["response_encoding"] != "nd-json-pages"


def test_malformed_rows_keep_distinct_failure_identities():
    from archive_magic_fetch.cdx import _malformed

    a = _malformed("bad-a", "x")
    b = _malformed("bad-b", "y")
    assert a.identity != b.identity

    # Distinct malformed rows that share timestamp/url identity fields must
    # still remain distinct after the readable-field rewrite path.
    shared = (
        "com,example)/ 20040615000000 http://example.org/ text/html 200 - "
    )
    c = _malformed(shared + "extra-one", "x")
    d = _malformed(shared + "extra-two", "y")
    assert c.identity != d.identity
    assert c.identity.timestamp == "20040615000000"
    assert d.identity.timestamp == "20040615000000"


def test_non_list_cdx_entries_become_malformed_failures(tmp_path):
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
    body = json.dumps([good, {"unexpected": True}, "also-bad"]).encode("utf-8")
    session = FakeSession([body])
    result = fetch_year_cdx(
        layout,
        url_pattern="http://example.org/",
        year=2004,
        date_start="20040101000000",
        date_end="20041231235959",
        run_id="test-nonlist",
        session=session,
        sleep=lambda _s: None,
    )
    assert len(result.captures) == 1
    assert len(result.failures) == 2
    assert all(f.category == FailureCategory.MALFORMED_CDX for f in result.failures)
    assert result.failures[0].identity != result.failures[1].identity


# ---------------------------------------------------------------------------
# 2 + 4. Statusless identity via three run_fetch passes
# ---------------------------------------------------------------------------


def test_statusless_capture_three_runs_no_extra_network(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    digest = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    identity = make_identity(
        original_url="http://example.org/",
        timestamp="20040615000000",
        status_token=MISSING_CDX_STATUS,
        payload_digest=f"sha1:{digest}",
    )
    body_bytes = b"statusless-body"
    real_digest = payload_digest(body_bytes).split(":")[1]
    identity = make_identity(
        original_url="http://example.org/",
        timestamp="20040615000000",
        status_token=MISSING_CDX_STATUS,
        payload_digest=f"sha1:{real_digest}",
        urlkey="com,example)/",
    )
    calls = {"n": 0}

    def download_fn(_client, capt_identity):
        calls["n"] += 1
        return _playback(capt_identity, body=body_bytes, status=200)

    rows = [
        [
            "com,example)/",
            "20040615000000",
            "http://example.org/",
            "text/html",
            "-",
            real_digest,
            "5",
        ]
    ]
    body = _cdx_json(rows)
    original, cdx_mod, fetch_mod = _patch_cdx(body)
    try:
        for _run in range(3):
            result = run_fetch(
                FetchSettings(
                    url_pattern="http://example.org/",
                    date_start="20040615000000",
                    date_end="20040615000000",
                    archives_root=tmp_path,
                ),
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            )
            assert result.exit_code == 0
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert calls["n"] == 1
    inv = inventory_collection(layout)
    assert inv.contains(identity)
    warc = list_year_warcs(layout, 2004)[0]
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                rebuilt = get_warc_identity(record)
                assert rebuilt.status_token == "-"
                assert rebuilt == identity
                record.raw_stream.read()


# ---------------------------------------------------------------------------
# 3. Scheduler pacing / concurrency / real RateLimitError 429
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
        _identity(
            ts=f"2004061500000{i}",
            digest="sha1:" + ("A" * 31 + "234567"[i % 6]),
        )
        for i in range(3)
    ]

    attempts = {"n": 0}

    def download_fn(_client, identity):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RateLimitError(response=MagicMock(), retry_after=2)
        return _playback(identity)

    scheduler = PlaybackScheduler(
        client_factory=lambda: MagicMock(),
        identities=identities,
        max_in_flight=2,
        requests_per_second=8.0,
        max_attempts=4,
        download_fn=download_fn,
        clock=mono,
        sleep=sleep,
    )
    results = []

    def consumer():
        for item in scheduler.results():
            results.append(item)
            scheduler.acknowledge()

    t = threading.Thread(target=consumer)
    t.start()
    scheduler.run()
    t.join(timeout=5)
    assert sleeps, "scheduler should wait between starts or on 429"
    assert scheduler.metrics.peak_in_flight <= 2
    assert scheduler._blocked_until > 0.0
    assert scheduler.metrics.cooldown_wait_s > 0
    successes = [r for r in results if hasattr(r, "result")]
    assert len(successes) >= 2
    assert attempts["n"] >= 3


def test_rate_limit_uses_error_retry_after_without_exponential_default():
    from archive_magic_fetch.scheduler import PlaybackScheduler

    clock = {"t": 100.0}
    identity = _identity()

    scheduler = PlaybackScheduler(
        client_factory=lambda: MagicMock(),
        identities=[identity],
        clock=lambda: clock["t"],
        sleep=lambda _s: None,
    )
    scheduler._note_429(12.0, identity=identity)
    assert scheduler._blocked_until == pytest.approx(112.0)

    # A second 429 without Retry-After must stay at the fixed default, not 120s.
    clock["t"] = 200.0
    scheduler._note_429(None, identity=identity)
    assert scheduler._blocked_until == pytest.approx(260.0)
    assert scheduler._consecutive_429 == 2


def test_session_raises_rate_limit_for_429_memento_response():
    from archive_magic_fetch.cdx import ArchiveMagicWaybackSession
    from wayback import WaybackSession
    from wayback.exceptions import RateLimitError

    session = ArchiveMagicWaybackSession()
    response = MagicMock()
    response.status_code = 429
    response.headers = {"Memento-Datetime": "Wed, 01 Jun 2004 00:00:00 GMT"}
    # Parent would treat this as a successful memento; our session must not.

    original_send = WaybackSession.send

    def fake_send(self, request, **kwargs):
        return response

    WaybackSession.send = fake_send  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitError) as raised:
            session.send(MagicMock())
        assert raised.value.retry_after == 60
    finally:
        WaybackSession.send = original_send  # type: ignore[method-assign]
        session.close()


def test_retry_after_extracted_from_rate_limit_error_and_message():
    from archive_magic_fetch.scheduler import _retry_after_from_error

    err = RateLimitError(response=MagicMock(headers={}), retry_after=7)
    assert _retry_after_from_error(err) == 7.0

    header_response = MagicMock()
    header_response.headers = {"Retry-After": "9"}
    bare = RuntimeError("429 too many requests")
    bare.response = header_response  # type: ignore[attr-defined]
    assert _retry_after_from_error(bare) == 9.0

    msg = RuntimeError("Wayback rate limit exceeded, retry after 15 s")
    assert _retry_after_from_error(msg) == 15.0


def test_connection_error_with_429_in_timestamp_is_not_rate_limit():
    from archive_magic_fetch.scheduler import _is_rate_limit_error
    from wayback.exceptions import WaybackRetryError

    # Timestamps like 20080429 contain the digits 429 but are not HTTP 429s.
    causal = ConnectionError(
        "HTTPSConnectionPool(host='web.archive.org', port=443): "
        "Max retries exceeded with url: "
        "/web/20080429120000id_/http://example.org/page"
    )
    wrapped = WaybackRetryError(0, 0.08, causal)
    assert not _is_rate_limit_error(wrapped)
    assert not _is_rate_limit_error(causal)

    real = RateLimitError(response=MagicMock(headers={}), retry_after=60)
    assert _is_rate_limit_error(real)
    assert _is_rate_limit_error(WaybackRetryError(0, 0.1, real))
    assert _is_rate_limit_error(RuntimeError("429 error while loading memento"))


def test_retries_do_not_jump_ahead_of_first_attempts():
    clock = {"t": 0.0}
    order: list[str] = []

    def mono():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    first = _identity(ts="20040601000000", digest="sha1:" + ("A" * 32))
    second = _identity(ts="20040602000000", digest="sha1:" + ("B" * 32))
    attempts = {first.timestamp: 0, second.timestamp: 0}

    def download_fn(_client, identity):
        attempts[identity.timestamp] += 1
        order.append(f"{identity.timestamp}:{attempts[identity.timestamp]}")
        if identity.timestamp == first.timestamp and attempts[identity.timestamp] == 1:
            err = RuntimeError("500 error while loading memento")
            raise err
        return _playback(identity)

    scheduler = PlaybackScheduler(
        client_factory=lambda: MagicMock(),
        identities=[first, second],
        max_in_flight=1,
        requests_per_second=float("inf"),
        max_attempts=3,
        download_fn=download_fn,
        clock=mono,
        sleep=sleep,
    )

    def consumer():
        for _item in scheduler.results():
            scheduler.acknowledge()

    t = threading.Thread(target=consumer)
    t.start()
    scheduler.run()
    t.join(timeout=5)
    # Second capture's first attempt must precede the first capture's retry.
    assert order.index("20040602000000:1") < order.index("20040601000000:2")


# ---------------------------------------------------------------------------
# 5 + 6. Same-year revisits via run_fetch; cross-year full responses
# ---------------------------------------------------------------------------


def test_same_year_representative_revisits_and_redirects_individual(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    shared_body = b"shared"
    dig = payload_digest(shared_body).split(":")[1]
    a_ts = "20040601000000"
    b_ts = "20040602000000"
    redir_ts = "20040603000000"
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        if identity.status_token == "302":
            body = b"redir"
            return _playback(identity, body=body, status=302)
        return _playback(identity, body=shared_body, status=200)

    body = _cdx_json(
        [
            [
                "com,example)/",
                a_ts,
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "6",
            ],
            [
                "com,example)/",
                b_ts,
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "6",
            ],
            [
                "com,example)/",
                redir_ts,
                "http://example.org/",
                "text/html",
                "302",
                dig,
                "6",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040603000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code == 0
    assert downloads == [a_ts, redir_ts]
    warc = list_year_warcs(layout, 2004)[0]
    types = []
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 2
    assert types.count("revisit") == 1


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
    stored_2004 = inv.year_index(2004).by_url_digest[(y2004.urlkey, dig)]
    stored_2005 = inv.year_index(2005).by_url_digest[(y2005.urlkey, dig)]
    assert stored_2004.relative_key.startswith("archive/2004/")
    assert stored_2005.relative_key.startswith("archive/2005/")


# ---------------------------------------------------------------------------
# Stable CDX urlkey survives inventory
# ---------------------------------------------------------------------------


def test_custom_cdx_urlkey_survives_warc_inventory(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = _identity(urlkey="custom,key)/special")
    writer = YearWarcWriter(layout, 2004)
    writer.write_playback(_playback(identity))
    writer.close()
    warc = list_year_warcs(layout, 2004)[0]
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                assert record.rec_headers.get_header(CDX_URLKEY_HEADER) == (
                    "custom,key)/special"
                )
                rebuilt = get_warc_identity(record)
                assert rebuilt.urlkey == "custom,key)/special"
                assert rebuilt == identity
                record.raw_stream.read()
    inv = inventory_collection(layout)
    assert inv.contains(identity)


# ---------------------------------------------------------------------------
# Digest validation + 5xx retry classification
# ---------------------------------------------------------------------------


def test_digest_mismatch_is_digest_validation_failure():
    identity = _identity(digest="sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    class Client:
        def get_memento(self, *args, **kwargs):
            memento = MagicMock()
            memento.__enter__ = lambda s: s
            memento.__exit__ = MagicMock(return_value=False)
            memento.content = b"wrong-body"
            memento.status_code = 200
            memento.memento_url = "https://web.archive.org/web/x"
            memento.timestamp = __import__("datetime").datetime(
                2004, 6, 15, tzinfo=__import__("datetime").timezone.utc
            )
            memento.headers = {"Content-Type": "text/html"}
            memento.url = identity.original_url
            return memento

    from archive_magic_fetch.warc import DigestValidationError, download_exact_for_identity

    with pytest.raises(DigestValidationError):
        download_exact_for_identity(Client(), identity)


def test_playback_5xx_is_retryable():
    category, retryable = classify_playback_error(
        MementoPlaybackError("500 error while loading memento at http://x")
    )
    assert retryable is True
    assert category == FailureCategory.RETRY_EXHAUSTED


# ---------------------------------------------------------------------------
# Annual revisit closure rejects orphans
# ---------------------------------------------------------------------------


def test_orphan_revisit_annual_index_is_rejected(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    dig = "sha1:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
    response_id = _identity(ts="20040601000000", digest=dig)
    revisit_id = _identity(ts="20040602000000", digest=dig)

    writer = YearWarcWriter(layout, 2004)
    writer.write_playback(_playback(response_id, body=b"body"))
    from archive_magic_fetch.warc import StoredResponse, revisit_from_stored

    stored = StoredResponse(
        identity=response_id,
        relative_key=layout.warc_relative_key(2004, 1),
        warc_date="2004-06-01T00:00:00Z",
        warc_payload_digest=payload_digest(b"body"),
        target_uri=response_id.original_url,
        status_code=200,
        headers=(("Content-Type", "text/html"),),
    )
    writer.write_revisit(revisit_from_stored(revisit_id, stored))
    warcs = writer.close()
    # Build an index, then delete the response WARC content path trick:
    # create a revisit-only WARC year by indexing only after removing response.
    publish_annual_index(layout, 2004)
    lines = [
        line
        for line in layout.annual_index(2004).read_text().splitlines()
        if "warc/revisit" in line or '"mime": "warc/revisit"' in line or "revisit" in line
    ]
    # Force validator with synthetic revisit-only lines referencing missing digest.
    fake_lines = [
        'com,example)/ 20040602000000 {"url":"http://example.org/",'
        '"digest":"sha1:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",'
        '"mime":"warc/revisit","status":"200",'
        f'"filename":"{warcs[0].relative_key}","offset":0,"length":10}}'
    ]
    with pytest.raises(ValueError, match="no same-year response"):
        validate_annual_revisit_closure(layout, 2004, fake_lines)


def test_orphan_redirect_revisit_annual_index_is_rejected(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    # Redirect-status revisits must still resolve; no redirect exemption.
    fake_lines = [
        'com,example)/ 20040602000000 {"url":"http://example.org/",'
        '"digest":"sha1:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",'
        '"mime":"warc/revisit","status":"302",'
        '"filename":"archive/2004/example.org-2004-001.warc.gz",'
        '"offset":0,"length":10}'
    ]
    with pytest.raises(ValueError, match="no same-year response"):
        validate_annual_revisit_closure(layout, 2004, fake_lines)


# ---------------------------------------------------------------------------
# Date bounds
# ---------------------------------------------------------------------------


def test_year_end_bound_covers_full_utc_year():
    end = parse_date_bound("2004", default="", bound="end")
    assert end == "20041231235959"
    assert year_bounds(2004, "20040101000000", end) is not None
    with pytest.raises(ValueError):
        parse_date_bound("200413", default="", bound="start")
    settings = build_settings("http://example.org/", date_end="2004")
    assert settings.date_end == "20041231235959"
    assert DEFAULT_DATE_START.startswith("1995")


# ---------------------------------------------------------------------------
# 7. WARC rollover naming / size / 1000 reject
# ---------------------------------------------------------------------------


def test_warc_rollover_naming_and_rejects_1000(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
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
    assert not layout.annual_index(2004).exists()
    publish_annual_index(layout, 2004)
    assert layout.annual_index(2004).is_file()
    inv = inventory_collection(layout)
    assert inv.contains(capt)


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
    assert text1 == layout.collection_index.read_text()
    lines = [line for line in text1.splitlines() if line]
    keys = [(line.split()[0], line.split()[1]) for line in lines]
    assert keys == sorted(keys)
    validate_cdxj_against_warcs(layout, lines)


# ---------------------------------------------------------------------------
# 11. Partial run + failure persistence across scoped rerun
# ---------------------------------------------------------------------------


def test_partial_run_truthful_manifest_and_nonzero_exit(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    good_body = b"good"
    good_digest = payload_digest(good_body).split(":")[1]
    bad_digest = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
    good = _identity(ts="20040601000000", digest=f"sha1:{good_digest}")
    bad = _identity(ts="20040602000000", digest=f"sha1:{bad_digest}")

    def download_fn(_client, identity):
        if identity.timestamp == bad.timestamp:
            raise RuntimeError("memento unavailable")
        return _playback(identity, body=good_body)

    body = _cdx_json(
        [
            [
                "com,example)/",
                good.timestamp,
                good.original_url,
                "text/html",
                "200",
                good_digest,
                "5",
            ],
            [
                "com,example)/",
                bad.timestamp,
                bad.original_url,
                "text/html",
                "200",
                bad_digest,
                "5",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(body)
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
    assert layout.failures_path.is_file()
    assert list_year_warcs(layout, 2004)
    assert all(w["record_count"] > 0 for w in manifest["warcs"])


def test_scoped_rerun_keeps_prior_failures_and_annual_indexes(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    prior = UnresolvedFailure(
        identity=_identity(ts="20040601000000"),
        category=FailureCategory.UNAVAILABLE,
        message="still unresolved from earlier run",
    )
    write_failures(layout, [prior])
    # Seed a 2004 annual index so a 2005-only rerun must still publish it.
    capt = _identity(ts="20040615000000")
    writer = YearWarcWriter(layout, 2004)
    writer.write_playback(_playback(capt))
    writer.close()
    publish_annual_index(layout, 2004)

    body_2005 = _cdx_json(
        [
            [
                "com,example)/",
                "20050601000000",
                "http://example.org/",
                "text/html",
                "200",
                payload_digest(b"y2005").split(":")[1],
                "5",
            ]
        ]
    )

    def download_fn(_client, identity):
        return _playback(identity, body=b"y2005")

    original, cdx_mod, fetch_mod = _patch_cdx(body_2005)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20050601000000",
                date_end="20050601000000",
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
    remaining = {item.identity for item in load_failures(layout)}
    assert prior.identity in remaining
    manifest = json.loads(layout.manifest_path.read_text())
    annual_names = {item["filename"] for item in manifest["annual_indexes"]}
    assert "indexes/years/2004.cdxj" in annual_names
    assert "indexes/years/2005.cdxj" in annual_names
    warc_2004 = [
        item for item in manifest["warcs"] if item["filename"].startswith("archive/2004/")
    ]
    assert warc_2004 and warc_2004[0]["record_count"] > 0


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


def test_resolved_prior_failure_exits_zero(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body_bytes = b"resolved-body"
    digest = payload_digest(body_bytes).split(":")[1]
    identity = _identity(
        ts="20040615000000",
        digest=f"sha1:{digest}",
        urlkey="com,example)/",
    )
    write_failures(
        layout,
        [
            UnresolvedFailure(
                identity=identity,
                category=FailureCategory.UNAVAILABLE,
                message="historical unresolved",
            )
        ],
    )

    def download_fn(_client, capt_identity):
        return _playback(capt_identity, body=body_bytes)

    body = _cdx_json(
        [
            [
                "com,example)/",
                identity.timestamp,
                identity.original_url,
                "text/html",
                "200",
                digest,
                "5",
            ]
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040615000000",
                date_end="20040615000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code == 0
    assert result.failures == []
    manifest = json.loads(layout.manifest_path.read_text())
    assert manifest["status"] == "complete"
    assert not layout.failures_path.is_file()


def test_newer_failure_details_replace_stale(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = _identity(ts="20040615000000", urlkey="com,example)/")
    write_failures(
        layout,
        [
            UnresolvedFailure(
                identity=identity,
                category=FailureCategory.UNAVAILABLE,
                message="stale historical message",
            )
        ],
    )

    def download_fn(_client, capt_identity):
        raise MementoPlaybackError("memento playback failed: 404 Not Found")

    body = _cdx_json(
        [
            [
                "com,example)/",
                identity.timestamp,
                identity.original_url,
                "text/html",
                "200",
                identity.payload_digest.split(":")[1],
                "5",
            ]
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040615000000",
                date_end="20040615000000",
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
    remaining = load_failures(layout)
    assert len(remaining) == 1
    assert remaining[0].identity == identity
    assert remaining[0].message != "stale historical message"
    assert "stale historical message" not in remaining[0].message


def test_run_source_ids_are_unique(tmp_path):
    from archive_magic_fetch.cdx import init_run_source

    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    first = init_run_source(layout)
    second = init_run_source(layout)
    assert first != second
    assert first.name != second.name
    # Pre-create a colliding candidate directory and ensure allocation bumps.
    collide = layout.sources_root / first.name
    assert collide.is_dir()
    third = init_run_source(layout)
    assert third.name not in {first.name, second.name}


def test_multipage_cdx_metadata_coherent_and_parsed_from_disk(tmp_path):
    import hashlib

    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    page1_row = [
        "com,example)/",
        "20040601000000",
        "http://example.org/",
        "text/html",
        "200",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "5",
    ]
    page2_row = [
        "com,example)/a",
        "20040602000000",
        "http://example.org/a",
        "text/html",
        "200",
        "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "5",
    ]
    # IA resume-key pagination: page ends with [[], ["resume-token"]]
    page1 = json.dumps([page1_row, [], ["resume-token"]]).encode("utf-8")
    page2 = json.dumps([page2_row]).encode("utf-8")
    session = FakeSession([page1, page2])
    result = fetch_year_cdx(
        layout,
        url_pattern="http://example.org/",
        year=2004,
        date_start="20040101000000",
        date_end="20041231235959",
        run_id="test-multipage",
        session=session,
        sleep=lambda _s: None,
    )
    assert len(result.captures) == 2
    assert result.query_meta["page_count"] == 2
    pages = result.query_meta["pages"]
    assert len(pages) == 2
    # Top-level fields describe page one; not a summed byte_length.
    assert result.query_meta["byte_length"] == pages[0]["byte_length"]
    assert result.query_meta["sha256"] == pages[0]["sha256"]
    assert result.query_meta["raw_file"] == pages[0]["raw_file"]
    assert result.query_meta["byte_length"] != sum(
        int(p["byte_length"]) for p in pages
    )
    # Durable page files exist and match recorded checksums.
    for page in pages:
        path = result.source_dir / str(page["raw_file"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == page["sha256"]
