"""CDX acquisition, date bounds, and raw pages."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from archive_magic_fetch.cdx import (
    fetch_year_cdx,
    normalize_cdx_search,
    parse_date_bound,
    year_ranges,
)
from archive_magic_fetch.collection import ArchiveLayout, ensure_collection_dirs
from archive_magic_fetch.config import StorageConfig
from archive_magic_fetch.fetch import build_settings
from archive_magic_fetch.models import FailureCategory
from helpers import FakeSession, cdx_json


def test_raw_cdx_saved_before_normalization_and_malformed_in_failures(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
    missing_url = [
        "com,example)/",
        "20040615000000",
        "",
        "text/html",
        "200",
        "-",
        "1",
    ]
    body = cdx_json([good, bad, missing_url])
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
    assert len(result.failures) == 2
    assert all(f.category == FailureCategory.MALFORMED_CDX for f in result.failures)
    assert result.failures[0].identity != result.failures[1].identity
    assert result.failures[1].identity.timestamp == "20040615000000"
    assert result.query_meta["response_encoding"] == "identity"
    assert result.raw_path.name == "page-001.cdx.gz"
    assert not (result.source_dir / "query.json").exists()


def test_non_list_cdx_entries_become_malformed_failures(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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


def test_parse_date_bound_strips_hyphens_and_pads_precision():
    assert parse_date_bound(None, default="1995-01-01", bound="start") == (
        "19950101000000"
    )
    assert parse_date_bound("1995", default="", bound="start") == "19950101000000"
    assert parse_date_bound("1995", default="", bound="end") == "19951231235959"
    assert parse_date_bound("2004-06", default="", bound="start") == "20040601000000"
    assert parse_date_bound("2004-06", default="", bound="end") == "20040630235959"
    assert parse_date_bound("200406", default="", bound="end") == "20040630235959"
    assert parse_date_bound("2004-06-15", default="1995-01-01", bound="start") == (
        "20040615000000"
    )
    assert parse_date_bound("2004-12-31", default="1995-01-01", bound="end") == (
        "20041231235959"
    )
    with pytest.raises(ValueError, match="invalid date bound"):
        parse_date_bound("2004-06-15T00:00:00", default="", bound="start")
    with pytest.raises(ValueError):
        parse_date_bound("200413", default="", bound="start")
    settings = build_settings(
        "http://example.org/",
        date_start="2004-06",
        date_end="2004-12-31",
        storage=StorageConfig("local", Path("/tmp/workspace")),
    )
    assert settings.date_start == "20040601000000"
    assert settings.date_end == "20041231235959"
    assert list(year_ranges(settings.date_start, settings.date_end)) == [
        (2004, "20040601000000", "20041231235959")
    ]
    year_end = build_settings(
        "http://example.org/",
        date_end="2004",
        storage=StorageConfig("local", Path("/tmp/workspace")),
    )
    assert year_end.date_start == "19950101000000"
    assert year_end.date_end == "20041231235959"


def test_year_ranges_clips_first_and_last_years():
    assert list(year_ranges("20030601120000", "20050301120000")) == [
        (2003, "20030601120000", "20031231235959"),
        (2004, "20040101000000", "20041231235959"),
        (2005, "20050101000000", "20050301120000"),
    ]


def test_normalize_cdx_search_rewrites_wildcard_and_prefix():
    assert normalize_cdx_search("*.example.org") == ("example.org", "domain")
    assert normalize_cdx_search("http://*.example.org/") == ("example.org", "domain")
    assert normalize_cdx_search("http://example.org/path/*") == (
        "http://example.org/path/",
        "prefix",
    )
    assert normalize_cdx_search("http://example.org/a") == (
        "http://example.org/a",
        None,
    )


def test_multipage_cdx_metadata_coherent_and_parsed_from_disk(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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


def test_cdx_retries_protocol_incomplete_read(tmp_path):
    from http.client import IncompleteRead
    from unittest.mock import MagicMock

    from urllib3.exceptions import ProtocolError

    from helpers import FakeRaw

    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    row = [
        "com,example)/",
        "20040615000000",
        "http://example.org/",
        "text/html",
        "200",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
        "123",
    ]
    body = cdx_json([row])
    sleeps: list[float] = []

    class TruncateThenOkSession:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = 0

        def get(self, url, stream=True, timeout=120):
            self.calls += 1
            response = MagicMock()
            response.status_code = 200
            response.headers = {"Content-Encoding": "identity"}
            response.raise_for_status = MagicMock()

            def close() -> None:
                self.closed += 1

            response.close = close
            if self.calls == 1:
                def boom(decode_content: bool = False):
                    raise ProtocolError(
                        "Connection broken: "
                        "IncompleteRead(7070 bytes read)",
                        IncompleteRead(b" partial"),
                    )

                response.raw = MagicMock()
                response.raw.read = boom
                response.content = None
            else:
                response.raw = FakeRaw(body)
                response.content = body
            return response

        def close(self):
            return None

    session = TruncateThenOkSession()
    result = fetch_year_cdx(
        layout,
        url_pattern="http://example.org/",
        year=2004,
        date_start="20040101000000",
        date_end="20041231235959",
        run_id="test-incomplete-read",
        session=session,
        sleep=sleeps.append,
    )
    assert session.calls == 2
    assert session.closed >= 1
    assert sleeps == [5.0]
    assert len(result.captures) == 1
    assert result.query_meta["request_count"] == 1
