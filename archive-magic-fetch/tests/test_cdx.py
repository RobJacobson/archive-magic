"""CDX library integration and date-bound tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from wayback import CdxRecord

from archive_magic_fetch.cdx import (
    fetch_cdx,
    normalize_cdx_search,
    parse_date_bound,
    year_ranges,
)
from archive_magic_fetch.config import FetchOutput
from archive_magic_fetch.fetch import build_settings


class FakeClient:
    def __init__(self, records=()):
        self.records = records
        self.calls = []
        self.closed = False

    def search(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return iter(self.records)

    def close(self):
        self.closed = True


def record(**overrides):
    values = {
        "urlkey": "com,example)/",
        "timestamp": datetime(2004, 6, 15, tzinfo=timezone.utc),
        "original": "http://example.org/",
        "mimetype": "text/html",
        "statuscode": 200,
        "digest": "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
        "length": 123,
    }
    values.update(overrides)
    return CdxRecord(**values)


def test_fetch_cdx_delegates_paging_and_parsing_to_wayback(monkeypatch):
    fake = FakeClient([record()])
    sessions = []

    def fake_wayback(*, session):
        sessions.append(session)
        return fake

    monkeypatch.setattr("archive_magic_fetch.cdx.WaybackClient", fake_wayback)
    result = fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        retries=4,
    )

    assert len(result.captures) == 1
    assert result.captures[0].identity.timestamp == "20040615000000"
    assert result.captures[0].identity.status_token == "200"
    assert fake.calls == [
        (
            "example.org",
            {
                "match_type": "domain",
                "from_date": "20040101000000",
                "to_date": "20041231235959",
                "resolve_revisits": False,
                "skip_malformed_results": True,
            },
        )
    ]
    assert sessions[0].retries == 0
    assert fake.closed
    assert result.search_url == "example.org"
    assert result.match_type == "domain"


def test_fetch_cdx_pauses_60s_on_connection_refused(monkeypatch):
    from wayback.exceptions import WaybackRetryError

    attempts = {"n": 0}
    sleeps: list[float] = []
    reports: list[str] = []

    class FlakyClient:
        def __init__(self):
            self.closed = False

        def search(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                cause = ConnectionError(
                    "HTTPSConnectionPool(host='web.archive.org', port=443): "
                    "Max retries exceeded with url: /cdx/search/cdx "
                    "(Caused by NewConnectionError("
                    "\"Failed to establish a new connection: "
                    "[Errno 61] Connection refused\"))"
                )
                raise WaybackRetryError(4, 19.04, cause) from cause
            return iter([record()])

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "archive_magic_fetch.cdx.WaybackClient",
        lambda **_kwargs: FlakyClient(),
    )
    result = fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        retries=4,
        sleep=sleeps.append,
        report=reports.append,
    )

    assert len(result.captures) == 1
    assert attempts["n"] == 3
    assert sleeps == [60.0, 60.0]
    assert reports == [
        "rate limit: TCP connection refused during CDX query; "
        "pausing 60s before attempt 2/5",
        "rate limit: TCP connection refused during CDX query; "
        "pausing 60s before attempt 3/5",
    ]


def test_fetch_cdx_raises_after_exhausted_connection_refused_retries(monkeypatch):
    sleeps: list[float] = []

    class RefusedClient:
        def search(self, *args, **kwargs):
            raise ConnectionError(
                "Max retries exceeded: [Errno 61] Connection refused"
            )

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.cdx.WaybackClient",
        lambda **_kwargs: RefusedClient(),
    )
    with pytest.raises(RuntimeError, match="CDX query failed after 3 attempts"):
        fetch_cdx(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
            retries=2,
            sleep=sleeps.append,
            report=lambda _message: None,
        )
    assert sleeps == [60.0, 60.0]


def test_fetch_cdx_does_not_retry_permanent_errors(monkeypatch):
    sleeps: list[float] = []
    attempts = {"n": 0}

    class PermanentClient:
        def search(self, *args, **kwargs):
            attempts["n"] += 1
            raise ValueError("malformed CDX")

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.cdx.WaybackClient",
        lambda **_kwargs: PermanentClient(),
    )
    with pytest.raises(RuntimeError, match="CDX query failed after 1 attempts"):
        fetch_cdx(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
            retries=4,
            sleep=sleeps.append,
            report=lambda _message: None,
        )
    assert attempts["n"] == 1
    assert sleeps == []


def test_parse_date_bound_strips_hyphens_and_pads_precision():
    assert parse_date_bound(None, default="1995-01-01", bound="start") == (
        "19950101000000"
    )
    assert parse_date_bound("1995", default="", bound="start") == "19950101000000"
    assert parse_date_bound("1995", default="", bound="end") == "19951231235959"
    assert parse_date_bound("2004-06", default="", bound="start") == "20040601000000"
    assert parse_date_bound("2004-06", default="", bound="end") == "20040630235959"
    assert parse_date_bound("200406", default="", bound="end") == "20040630235959"
    assert parse_date_bound("2004-06-15", default="", bound="start") == (
        "20040615000000"
    )
    assert parse_date_bound("2004-12-31", default="", bound="end") == (
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
        output=FetchOutput("local", Path("/tmp/workspace")),
    )
    assert settings.date_start == "20040601000000"
    assert settings.date_end == "20041231235959"


def test_year_ranges_clips_first_and_last_years():
    assert list(year_ranges("20030601120000", "20050301120000")) == [
        (2003, "20030601120000", "20031231235959"),
        (2004, "20040101000000", "20041231235959"),
        (2005, "20050101000000", "20050301120000"),
    ]


def test_normalize_cdx_search_rewrites_wildcard_and_prefix():
    assert normalize_cdx_search("*.example.org") == ("example.org", "domain")
    assert normalize_cdx_search("http://*.example.org/") == (
        "example.org",
        "domain",
    )
    assert normalize_cdx_search("http://example.org/path/*") == (
        "http://example.org/path/",
        "prefix",
    )
    assert normalize_cdx_search("http://example.org/a") == (
        "http://example.org/a",
        None,
    )
