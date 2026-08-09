"""CDX acquisition, raw pages, and run ID allocation."""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from archive_magic_fetch.cdx import fetch_year_cdx, init_run_id, parse_date_bound, year_bounds
from archive_magic_fetch.collection import collection_layout, ensure_collection_dirs
from archive_magic_fetch.fetch import _report_cdx_ingest_skips, build_settings
from archive_magic_fetch.models import (
    CaptureIdentity,
    DEFAULT_DATE_START,
    FailureCategory,
    UnresolvedFailure,
)
from helpers import FakeSession, cdx_json, make_capt

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
    body = cdx_json([good, bad])
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
    assert gzip.decompress(raw) == body
    assert b"200406" in gzip.decompress(raw)
    assert len(result.captures) == 1
    assert len(result.failures) == 1
    assert result.failures[0].category == FailureCategory.MALFORMED_CDX
    assert result.query_meta["response_encoding"] == "identity"
    assert result.raw_path.name == "page-001.cdx.gz"
    assert not (result.source_dir / "query.json").exists()


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


def test_year_end_bound_covers_full_utc_year():
    end = parse_date_bound("2004", default="", bound="end")
    assert end == "20041231235959"
    assert year_bounds(2004, "20040101000000", end) is not None
    with pytest.raises(ValueError):
        parse_date_bound("200413", default="", bound="start")
    settings = build_settings("http://example.org/", date_end="2004")
    assert settings.date_end == "20041231235959"
    assert DEFAULT_DATE_START.startswith("1995")


def test_init_run_id_allocates_unique_id_after_collision(tmp_path):
    layout = collection_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    first = init_run_id(layout)
    (layout.run_dir("2004", first)).mkdir(parents=True)
    second = init_run_id(layout)
    assert first != second
    assert isinstance(first, str)


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


