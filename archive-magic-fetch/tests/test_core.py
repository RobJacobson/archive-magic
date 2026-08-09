"""High-value tests for Archive Magic Fetch clean-sheet rewrite."""

from __future__ import annotations

import json
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
    list_annual_indexes,
    list_year_warcs,
    load_failures,
    next_warc_sequence,
    write_failures,
)
from archive_magic_fetch.fetch import (
    FetchSettings,
    _capture_link,
    _download_with_retries,
    _format_elapsed,
    _style_result,
    build_settings,
    run_fetch,
)
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
    RunMetrics,
    UnresolvedFailure,
    INVALID_URI_PAYLOAD_DIGEST,
    make_identity,
    normalize_original_url,
)
from archive_magic_fetch.warc import (
    YearWarcWriter,
    classify_playback_error,
    get_warc_identity,
    inventory_year,
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


def test_cdx_ingest_skips_are_logged(capsys):
    from archive_magic_fetch.fetch import _report_cdx_ingest_skips

    failures = [
        UnresolvedFailure(
            identity=CaptureIdentity(
                urlkey="malformed:abc",
                original_url="-",
                timestamp="00000000000000",
                status_token="-",
                payload_digest="malformed:abc",
            ),
            category=FailureCategory.MALFORMED_CDX,
            message="invalid timestamp: bad-row",
        ),
    ]
    _report_cdx_ingest_skips(2008, failures)
    out = capsys.readouterr().out
    assert "year 2008: skipping 1 malformed CDX row(s)" in out
    assert "skip: invalid timestamp: bad-row" in out




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
    inv = inventory_year(layout, 2004)
    assert inv.contains(identity)
    warc = list_year_warcs(layout, 2004)[0]
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                rebuilt = get_warc_identity(record)
                assert rebuilt.status_token == "-"
                assert rebuilt == identity
                record.raw_stream.read()


def test_console_link_uses_compact_label_and_full_destination():
    identity = _identity(
        url="http://www.example.org/a",
        ts="20080516181742",
    )

    plain = _capture_link(identity, enabled=False)
    linked = _capture_link(identity, enabled=True)

    assert plain == "20080516181742/http://example.org/a"
    assert plain in linked
    assert "https://web.archive.org/web/20080516181742/http://www.example.org/a" in linked
    assert linked.startswith("\033]8;;")
    assert _style_result("Error", "error", enabled=False) == "Error"
    assert _style_result("Error", "error", enabled=True) == "\033[1;31mError\033[0m"


def test_serial_retry_uses_five_then_ten_seconds(capsys):
    identity = _identity()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return _playback(capture)

    result, failure = _download_with_retries(
        MagicMock(),
        identity,
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
        number=2,
        total=1234,
    )

    assert result is not None
    assert failure is None
    assert attempts == 3
    assert sleeps == [5.0, 10.0]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("   2/1234:") for line in lines)
    assert all("https://web.archive.org/web/" not in line for line in lines)
    assert "20040615000000/http://example.org/" in lines[0]


def test_serial_retry_honors_retry_after():
    class TestRateLimitError(Exception):
        retry_after = 17

    identity = _identity()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TestRateLimitError("429")
        return _playback(capture)

    result, failure = _download_with_retries(
        MagicMock(),
        identity,
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
    )

    assert result is not None
    assert failure is None
    assert sleeps == [17.0]


def test_permanent_failure_does_not_retry():
    download = MagicMock(side_effect=RuntimeError("permanent"))
    sleeps: list[float] = []

    result, failure = _download_with_retries(
        MagicMock(),
        _identity(),
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
    )

    assert result is None
    assert failure is not None
    assert download.call_count == 1
    assert sleeps == []


def test_invalid_uri_digest_skips_playback():
    download = MagicMock(side_effect=AssertionError("should not download"))

    result, failure = _download_with_retries(
        MagicMock(),
        _identity(digest=INVALID_URI_PAYLOAD_DIGEST),
        download_fn=download,
        metrics=RunMetrics(),
        sleep=lambda _seconds: None,
    )

    assert result is None
    assert failure is not None
    assert failure.category == FailureCategory.UNAVAILABLE
    download.assert_not_called()


# Serial playback session and response handling
# ---------------------------------------------------------------------------

def test_session_raises_rate_limit_for_429_memento_response():
    from archive_magic_fetch.cdx import ArchiveMagicWaybackSession
    from wayback import WaybackSession
    from wayback.exceptions import RateLimitError

    session = ArchiveMagicWaybackSession()
    response = MagicMock()
    response.status_code = 429
    response.headers = {
        "Memento-Datetime": "Wed, 01 Jun 2004 00:00:00 GMT",
        "Retry-After": "17",
    }
    # Parent would treat this as a successful memento; our session must not.

    original_send = WaybackSession.send

    def fake_send(self, request, **kwargs):
        return response

    WaybackSession.send = fake_send  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitError) as raised:
            session.send(MagicMock())
        assert raised.value.retry_after == 17
    finally:
        WaybackSession.send = original_send  # type: ignore[method-assign]
        session.close()


def _urllib3_response(body: bytes, headers: dict[str, str]):
    from io import BytesIO

    from urllib3 import HTTPResponse

    return HTTPResponse(
        body=BytesIO(body),
        headers=headers,
        status=200,
        preload_content=False,
        decode_content=False,
        original_response=None,
    )


def _requests_response(body: bytes, *, content_encoding: str | None, memento: bool):
    from requests import Response

    headers: dict[str, str] = {}
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    if memento:
        headers["Memento-Datetime"] = "Fri, 09 May 2008 08:22:33 GMT"
    response = Response()
    response.status_code = 200
    response.url = "https://web.archive.org/web/20080509082233id_/http://example.org/"
    response.headers.update(headers)
    response.raw = _urllib3_response(body, headers)
    return response


def test_session_repairs_false_gzip_content_encoding_on_mementos():
    """IA may claim gzip while the memento body is already plaintext HTML."""

    import gzip as gzip_mod

    from archive_magic_fetch.cdx import ArchiveMagicWaybackSession
    from wayback import WaybackSession

    plaintext = b"<!DOCTYPE html><html><body>ok</body></html>"
    real_gzip = gzip_mod.compress(b"compressed-payload")
    session = ArchiveMagicWaybackSession()
    original_send = WaybackSession.send

    def run(canned, expected):
        def fake_send(self, request, **kwargs):
            return canned

        WaybackSession.send = fake_send  # type: ignore[method-assign]
        repaired = session.send(MagicMock())
        assert repaired.content == expected
        assert "Content-Encoding" not in repaired.headers

    try:
        # False CE:gzip — body is HTML; must not raise ContentDecodingError.
        run(
            _requests_response(plaintext, content_encoding="gzip", memento=True),
            plaintext,
        )
        # True CE:gzip — still expose the decompressed payload.
        run(
            _requests_response(real_gzip, content_encoding="gzip", memento=True),
            b"compressed-payload",
        )
    finally:
        WaybackSession.send = original_send  # type: ignore[method-assign]
        session.close()


def test_session_leaves_non_memento_gzip_bodies_unconsumed():
    """CDX responses must keep streaming; do not eagerly rewrite them."""

    from archive_magic_fetch.cdx import repair_false_gzip_content_encoding

    body = b'[["urlkey","timestamp"]]'
    response = _requests_response(body, content_encoding="gzip", memento=False)
    repair_false_gzip_content_encoding(response)
    assert response._content is False
    assert response.headers.get("Content-Encoding") == "gzip"
    # Stream still available for decode_content=False CDX readers.
    assert response.raw.read() == body


def _memento_client(
    identity,
    body: bytes,
    *,
    headers: dict | None = None,
):
    from datetime import datetime, timezone

    class Client:
        def get_memento(self, *args, **kwargs):
            memento = MagicMock()
            memento.__enter__ = lambda s: s
            memento.__exit__ = MagicMock(return_value=False)
            memento.content = body
            if identity.status_token.isdigit():
                memento.status_code = int(identity.status_token)
            else:
                memento.status_code = 200
            memento.memento_url = (
                f"https://web.archive.org/web/{identity.timestamp}id_/"
                f"{identity.original_url}"
            )
            ts = identity.timestamp
            memento.timestamp = datetime(
                int(ts[0:4]),
                int(ts[4:6]),
                int(ts[6:8]),
                int(ts[8:10]),
                int(ts[10:12]),
                int(ts[12:14]),
                tzinfo=timezone.utc,
            )
            memento.headers = {"Content-Type": "text/html", **(headers or {})}
            memento.url = identity.original_url
            return memento

    return Client()


def test_empty_redirect_playback_is_stored_with_location(tmp_path):
    """Historical 3xx captures often have an empty body; still archive them."""

    from archive_magic_fetch.collection import collection_layout, ensure_collection_dirs
    from archive_magic_fetch.warc import (
        YearWarcWriter,
        download_exact_for_identity,
        payload_digest,
    )
    from warcio.archiveiterator import ArchiveIterator

    empty_digest = payload_digest(b"")
    identity = _identity(
        url="http://example.org/thecase",
        ts="20080404233814",
        status="302",
        digest=empty_digest,
    )
    location = "http://example.org/site/page/the_case"
    result = download_exact_for_identity(
        _memento_client(identity, b"", headers={"Location": location}),
        identity,
    )
    assert result.status_code == 302
    assert result.body == b""
    assert result.digest_matched is True
    assert result.warc_payload_digest == empty_digest
    assert any(name.lower() == "location" and value == location for name, value in result.headers)

    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = YearWarcWriter(layout, 2008)
    writer.write_playback(result)
    artifacts = writer.close()
    with artifacts[0].path.open("rb") as stream:
        records = list(ArchiveIterator(stream))
    responses = [rec for rec in records if rec.rec_type == "response"]
    assert len(responses) == 1
    rec = responses[0]
    assert rec.http_headers.get_statuscode() == "302"
    assert rec.http_headers.get_header("Location") == location
    assert rec.content_stream().read() == b""


def test_empty_non_redirect_playback_is_rejected():
    from archive_magic_fetch.warc import (
        UnusablePlaybackError,
        download_exact_for_identity,
    )

    identity = _identity(status="200")
    with pytest.raises(UnusablePlaybackError, match="empty playback body"):
        download_exact_for_identity(_memento_client(identity, b""), identity)




def test_invalid_uri_playback_is_always_rejected():
    from archive_magic_fetch.warc import (
        UnusablePlaybackError,
        classify_playback_error,
        download_exact_for_identity,
    )

    identity = _identity()
    with pytest.raises(UnusablePlaybackError):
        download_exact_for_identity(
            _memento_client(identity, b"Invalid URI"), identity
        )
    category, retryable = classify_playback_error(
        UnusablePlaybackError("IA playback stub: Invalid URI")
    )
    assert category == FailureCategory.UNAVAILABLE
    assert retryable is False






# ---------------------------------------------------------------------------
# Same-year revisit reuse and annual independence
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
            return _playback(identity, body=b"", status=302)
        return _playback(identity, body=shared_body, status=200)

    empty_dig = payload_digest(b"").split(":")[1]
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
                "com,example)/thecase",
                redir_ts,
                "http://example.org/thecase",
                "text/html",
                "302",
                empty_dig,
                "0",
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


def _patch_cdx_by_year(bodies_by_year: dict[int, bytes]):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_year_cdx

    def fake_fetch_year_cdx(layout, **kwargs):
        kwargs = dict(kwargs)
        year = int(kwargs["year"])
        body = bodies_by_year.get(year, b"[]")
        kwargs["session"] = FakeSession([body])
        kwargs["sleep"] = lambda _s: None
        return original(layout, **kwargs)

    cdx_mod.fetch_year_cdx = fake_fetch_year_cdx
    fetch_mod.fetch_year_cdx = fake_fetch_year_cdx
    return original, cdx_mod, fetch_mod


def test_matching_payloads_download_once_per_year(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"logo"
    dig = payload_digest(body).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return _playback(identity, body=body)

    bodies = {
        2004: _cdx_json(
            [
                [
                    "com,example)/",
                    "20040601000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    dig,
                    "4",
                ]
            ]
        ),
        2005: _cdx_json(
            [
                [
                    "com,example)/",
                    "20050601000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    dig,
                    "4",
                ]
            ]
        ),
    }
    original, cdx_mod, fetch_mod = _patch_cdx_by_year(bodies)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
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

    assert result.exit_code == 0
    assert downloads == ["20040601000000", "20050601000000"]
    assert result.metrics.downloads == 2
    assert result.metrics.revisits == 0

    types_2004 = []
    with list_year_warcs(layout, 2004)[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types_2004.append(record.rec_type)
            record.raw_stream.read()
    assert types_2004.count("response") == 1
    assert types_2004.count("revisit") == 0

    types_2005 = []
    with list_year_warcs(layout, 2005)[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types_2005.append(record.rec_type)
            record.raw_stream.read()
    assert types_2005.count("response") == 1
    assert types_2005.count("revisit") == 0

    inv = inventory_year(layout, 2005)
    stored = inv.lookup_representative(
        "com,example)/", payload_digest(body), not_after_timestamp="20050601000000"
    )
    assert stored is not None
    assert stored.identity.timestamp == "20050601000000"


def test_different_ia_digest_downloads_twice(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"same-bytes"
    dig_a = payload_digest(body).split(":")[1]
    dig_b = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.payload_digest)
        return _playback(identity, body=body)

    cdx_body = _cdx_json(
        [
            [
                "com,example)/",
                "20040601000000",
                "http://example.org/",
                "text/html",
                "200",
                dig_a,
                "4",
            ],
            [
                "com,example)/",
                "20040602000000",
                "http://example.org/",
                "text/html",
                "200",
                dig_b,
                "4",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(cdx_body)
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

    assert result.exit_code == 0
    assert result.metrics.downloads == 2
    assert len(downloads) == 2


def test_failed_older_capture_does_not_use_later_success(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"payload"
    dig = payload_digest(body).split(":")[1]

    def download_fn(_client, identity):
        if identity.timestamp.startswith("2004"):
            raise ConnectionError("memento unavailable")
        return _playback(identity, body=body)

    bodies = {
        2004: _cdx_json(
            [
                [
                    "com,example)/",
                    "20040601000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    dig,
                    "4",
                ]
            ]
        ),
        2005: _cdx_json(
            [
                [
                    "com,example)/",
                    "20050601000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    dig,
                    "4",
                ]
            ]
        ),
    }
    original, cdx_mod, fetch_mod = _patch_cdx_by_year(bodies)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
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

    assert result.exit_code == 0
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 0
    assert any(
        f.identity.timestamp == "20040601000000" for f in result.failures
    )
    assert list_year_warcs(layout, 2004) == []
    types = []
    with list_year_warcs(layout, 2005)[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 1




def test_representative_failure_promotes_next_same_key_candidate(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"shared"
    dig = payload_digest(body).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        if identity.timestamp == "20040601000000":
            raise ConnectionError("memento unavailable")
        return _playback(identity, body=body)

    cdx_body = _cdx_json(
        [
            [
                "com,example)/",
                "20040601000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "4",
            ],
            [
                "com,example)/",
                "20040602000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "4",
            ],
            [
                "com,example)/",
                "20040603000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "4",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(cdx_body)
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

    # First fails permanently, second downloads as promoted representative,
    # third becomes a revisit.
    assert downloads == [
        "20040601000000",
        "20040601000000",
        "20040601000000",
        "20040602000000",
    ]
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    assert any(f.identity.timestamp == "20040601000000" for f in result.failures)
    types = []
    with list_year_warcs(layout, 2004)[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 1
    assert types.count("revisit") == 1




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
    inv = inventory_year(layout, 2004)
    assert inv.contains(identity)


# ---------------------------------------------------------------------------
# Digest validation + 5xx retry classification
# ---------------------------------------------------------------------------



def test_digest_mismatch_is_kept_but_never_seeds_revisit(tmp_path):

    from archive_magic_fetch.models import CDX_DIGEST_MATCH_HEADER

    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    claimed = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    body = b"imperfect-but-kept"
    dig = claimed.split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        result = _playback(identity, body=body)
        return PlaybackResult(
            identity=result.identity,
            body=result.body,
            status_code=result.status_code,
            headers=result.headers,
            warc_date=result.warc_date,
            source_uri=result.source_uri,
            warc_payload_digest=payload_digest(body),
            digest_matched=False,
        )

    cdx_body = _cdx_json(
        [
            [
                "com,example)/",
                "20040601000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "4",
            ],
            [
                "com,example)/",
                "20040602000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "4",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = _patch_cdx(cdx_body)
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

    assert result.exit_code == 0
    assert downloads == ["20040601000000", "20040602000000"]
    assert result.metrics.downloads == 2
    assert result.metrics.revisits == 0
    assert result.metrics.digest_mismatch_accepted == 2

    warc = list_year_warcs(layout, 2004)[0]
    with warc.open("rb") as stream:
        responses = []
        revisits = []
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                responses.append(record)
                assert record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) == "false"
                assert record.rec_headers.get_header("WARC-Payload-Digest") == (
                    payload_digest(body)
                )
            if record.rec_type == "revisit":
                revisits.append(record)
                assert record.rec_headers.get_header("WARC-Payload-Digest") == (
                    payload_digest(body)
                )
            record.raw_stream.read()
    assert len(responses) == 2
    assert len(revisits) == 0

    inv = inventory_year(layout, 2004)
    assert inv.lookup_representative(
        "com,example)/", claimed, not_after_timestamp="20040602000000"
    ) is None


def test_playback_5xx_is_retryable():
    category, retryable = classify_playback_error(
        MementoPlaybackError("500 error while loading memento at http://x")
    )
    assert retryable is True
    assert category == FailureCategory.RETRY_EXHAUSTED


def test_wrapped_incomplete_read_is_permanent_truncated_failure():
    from requests.exceptions import ChunkedEncodingError
    from wayback.exceptions import WaybackRetryError

    incomplete = ChunkedEncodingError(
        "Connection broken: IncompleteRead(130810 bytes read, "
        "292753 more expected)"
    )
    wrapped = WaybackRetryError(0, 0.08, incomplete)

    category, retryable = classify_playback_error(wrapped)

    assert category == FailureCategory.TRUNCATED
    assert retryable is False


# ---------------------------------------------------------------------------
# Backward revisit closure rejects orphans and forward references
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
        warc_date="2004-06-01T00:00:00Z",
        warc_payload_digest=payload_digest(b"body"),
        target_uri=response_id.original_url,
        status_code=200,
    )
    writer.write_revisit(revisit_from_stored(revisit_id, stored))
    warcs = writer.close()
    publish_annual_index(layout, 2004)
    # Force validator with synthetic revisit-only lines referencing missing digest.
    fake_lines = [
        'com,example)/ 20040602000000 {"url":"http://example.org/",'
        '"digest":"sha1:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",'
        '"mime":"warc/revisit","status":"200",'
        f'"filename":"{warcs[0].relative_key}","offset":0,"length":10}}'
    ]
    with pytest.raises(ValueError, match="no earlier response"):
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
    with pytest.raises(ValueError, match="no earlier response"):
        validate_annual_revisit_closure(layout, 2004, fake_lines)


def test_cross_year_revisit_closure_is_rejected(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"across-years"
    dig = payload_digest(body)
    older = _identity(ts="20040601000000", digest=dig)
    newer = _identity(ts="20050601000000", digest=dig)

    writer = YearWarcWriter(layout, 2004)
    writer.write_playback(_playback(older, body=body))
    writer.close()
    publish_annual_index(layout, 2004)

    from archive_magic_fetch.warc import StoredResponse, revisit_from_stored

    stored = StoredResponse(
        identity=older,
        warc_date="2004-06-01T00:00:00Z",
        warc_payload_digest=dig,
        target_uri=older.original_url,
        status_code=200,
    )
    writer = YearWarcWriter(layout, 2005)
    writer.write_revisit(revisit_from_stored(newer, stored))
    writer.close()
    with pytest.raises(ValueError, match="no earlier response"):
        publish_annual_index(layout, 2005)


def test_forward_revisit_reference_is_rejected(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"body"
    dig = payload_digest(body)
    # Create a later-year response first so a forward Refers-To can exist.
    future = _identity(ts="20050601000000", digest=dig)
    writer = YearWarcWriter(layout, 2005)
    writer.write_playback(_playback(future, body=body))
    writer.close()

    from archive_magic_fetch.warc import StoredResponse, revisit_from_stored

    stored_future = StoredResponse(
        identity=future,
        warc_date="2005-06-01T00:00:00Z",
        warc_payload_digest=dig,
        target_uri=future.original_url,
        status_code=200,
    )
    earlier = _identity(ts="20040601000000", digest=dig)
    writer = YearWarcWriter(layout, 2004)
    writer.write_revisit(revisit_from_stored(earlier, stored_future))
    writer.close()
    lines = [
        'com,example)/ 20040601000000 {"url":"http://example.org/",'
        f'"digest":"{dig}",'
        '"mime":"warc/revisit","status":"200",'
        f'"filename":"{layout.warc_relative_key(2004, 1)}","offset":0,"length":10}}'
    ]
    with pytest.raises(ValueError, match="forward reference"):
        validate_annual_revisit_closure(layout, 2004, lines)


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


def test_annual_index_beside_warcs_covers_multi_shard_year(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = YearWarcWriter(layout, 2004, target_bytes=1)
    for i in range(2):
        capt = _identity(
            ts=f"2004060{i+1}000000",
            digest="sha1:" + ("F" * 31 + str(i)),
        )
        writer.write_playback(_playback(capt, body=b"x" * 100))
    warcs = writer.close()
    assert len(warcs) == 2

    annual = publish_annual_index(layout, 2004)
    assert annual is not None
    assert annual.relative_key == "archive/2004/example.org-2004.cdxj"
    assert layout.annual_index(2004) == layout.year_dir(2004) / "example.org-2004.cdxj"
    assert layout.annual_index(2004).is_file()
    assert not (layout.root / "indexes").exists()

    names = set()
    for line in layout.annual_index(2004).read_text().splitlines():
        if not line.strip():
            continue
        meta = json.loads(line.split(" ", 2)[2])
        names.add(meta["filename"])
    assert names == {
        "archive/2004/example.org-2004-001.warc.gz",
        "archive/2004/example.org-2004-002.warc.gz",
    }


def test_list_annual_indexes_ignores_warcs_and_foreign_names(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    year_dir = layout.year_dir(2004)
    year_dir.mkdir(parents=True)
    layout.warc_path(2004, 1).write_bytes(b"warc")
    # Correct annual index name.
    (year_dir / "example.org-2004.cdxj").write_text(
        "a 20040101000000 {}\n", encoding="utf-8"
    )
    # Foreign names must be ignored.
    (year_dir / "other.org-2004.cdxj").write_text("x\n", encoding="utf-8")
    (year_dir / "2004.cdxj").write_text("y\n", encoding="utf-8")
    (year_dir / "example.org-2004-index.cdxj").write_text("z\n", encoding="utf-8")
    # Another year with matching index.
    y2005 = layout.year_dir(2005)
    y2005.mkdir(parents=True)
    (y2005 / "example.org-2005.cdxj").write_text("b\n", encoding="utf-8")

    found = list_annual_indexes(layout)
    assert [(year, path.name) for year, path in found] == [
        (2004, "example.org-2004.cdxj"),
        (2005, "example.org-2005.cdxj"),
    ]


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
    assert layout.annual_index(2004).name == "example.org-2004.cdxj"
    inv = inventory_year(layout, 2004)
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
# 11. Completed run + failure persistence across scoped rerun
# ---------------------------------------------------------------------------


def test_completed_run_reports_expected_failures(tmp_path, capsys):
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

    assert result.exit_code == 0
    manifest = json.loads(layout.manifest_path.read_text())
    assert manifest["schema_version"] == 3
    assert manifest["status"] == "complete"
    assert set(manifest["metrics"]) == {
        "cdx_requests",
        "cdx_duration_s",
        "playback_attempts",
        "playback_bytes",
        "warc_write_s",
        "index_s",
        "attempts_by_category",
    }
    assert layout.failures_path.is_file()
    assert list_year_warcs(layout, 2004)
    assert all(w["record_count"] > 0 for w in manifest["warcs"])
    output = capsys.readouterr().out
    assert (
        "year 2004 done: downloads=1 revisits=0 "
        "already-represented=0 skips/errors=1"
    ) in output
    assert "elapsed " in output
    assert (
        "done: downloads=1 revisits=0 already-represented=0 skips/errors=1"
        in output
    )


def test_elapsed_format_uses_unbounded_hours():
    assert _format_elapsed(3661.9) == "01:01:01"
    assert _format_elapsed(25 * 60 * 60 + 2) == "25:00:02"


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

    assert result.exit_code == 0
    remaining = {item.identity for item in load_failures(layout)}
    assert prior.identity in remaining
    manifest = json.loads(layout.manifest_path.read_text())
    annual_names = {item["filename"] for item in manifest["annual_indexes"]}
    assert "archive/2004/example.org-2004.cdxj" in annual_names
    assert "archive/2005/example.org-2005.cdxj" in annual_names
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


def test_existing_older_collection_schema_requires_regeneration(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    layout.root.mkdir(parents=True)
    layout.manifest_path.write_text('{"schema_version": 2}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="delete and regenerate"):
        run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040601000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
        )


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

    assert result.exit_code == 0
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
