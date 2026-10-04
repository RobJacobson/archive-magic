"""CDX library integration and date-bound tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from archive_magic_fetch.archive.dates import parse_date_bound, year_ranges
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import build_settings
from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import (
    _CdxFailureKind,
    _classify_cdx_failure,
    _date_windows,
    _fetch_cdx,
    _format_cdx_index_scope,
    _format_cdx_window_label,
    _next_split_days,
    _normalize_cdx_search,
    _window_calendar_days,
)
from wayback import CdxRecord


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

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient", fake_wayback
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
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
                "limit": 5000,
                "resolve_revisits": False,
                "skip_malformed_results": True,
            },
        )
    ]
    assert sessions[0].retries == 0
    assert sessions[0].timeout == 300.0
    assert fake.closed
    assert result.search_url == "example.org"
    assert result.match_type == "domain"


def test_fetch_cdx_respects_custom_page_limit(monkeypatch):
    fake = FakeClient([record()])

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: fake,
    )
    _fetch_cdx(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        limit=1000,
    )
    assert fake.calls[0][1]["limit"] == 1000


def test_classify_cdx_failure_kinds():
    assert _classify_cdx_failure(TimeoutError("read timed out")) is (
        _CdxFailureKind.RATE_LIMIT
    )
    assert (
        _classify_cdx_failure(
            RuntimeError(
                "HTTPSConnectionPool(host='web.archive.org', port=443): "
                "Read timed out. (read timeout=60)"
            )
        )
        is _CdxFailureKind.RATE_LIMIT
    )
    assert (
        _classify_cdx_failure(
            ConnectionError(
                "('Connection aborted.', RemoteDisconnected("
                "'Remote end closed connection without response'))"
            )
        )
        is _CdxFailureKind.RATE_LIMIT
    )
    gateway = RuntimeError("504 Gateway Timeout")
    gateway.status_code = 504
    assert _classify_cdx_failure(gateway) is _CdxFailureKind.RATE_LIMIT
    wall = TimeoutError("CDX query exceeded 300s wall-clock budget")
    assert _classify_cdx_failure(wall) is _CdxFailureKind.WALL_CLOCK
    refused = ConnectionError("Connection refused")
    assert _classify_cdx_failure(refused) is _CdxFailureKind.RATE_LIMIT
    rate = RuntimeError("Wayback rate limit exceeded, retry after 60 s")
    rate.status_code = 429
    assert _classify_cdx_failure(rate) is _CdxFailureKind.RATE_LIMIT
    assert _classify_cdx_failure(ValueError("malformed CDX")) is None
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _cdx_should_split

    assert not _cdx_should_split(TimeoutError("read timed out"))
    assert _cdx_should_split(TimeoutError("CDX query exceeded 300s wall-clock budget"))
    assert not _cdx_should_split(gateway)
    assert not _cdx_should_split(refused)
    assert not _cdx_should_split(rate)


def test_fetch_cdx_retries_504_with_unavailable_backoff(monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []
    reports: list[str] = []

    class TimeoutClient:
        def search(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                error = RuntimeError("504 Gateway Timeout")
                error.status_code = 504
                raise error
            return iter([record()])

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: TimeoutClient(),
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        sleep=sleeps.append,
        report=reports.append,
    )

    assert len(result.captures) == 1
    assert attempts["n"] == 3
    assert sleeps == [60.0, 120.0]
    assert reports == [
        "Wayback timed out looking up the CDX index (HTTP 504).",
        "waiting 60s (attempt 2/10).",
        "Wayback timed out looking up the CDX index (HTTP 504).",
        "waiting 120s (attempt 3/10).",
    ]


def test_fetch_cdx_keeps_one_unavailable_budget_for_504_and_429(monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []
    reports: list[str] = []

    class MixedClient:
        def search(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                error = RuntimeError("504 Gateway Timeout")
                error.status_code = 504
                raise error
            if attempts["n"] == 3:
                error = RuntimeError("Wayback rate limit exceeded")
                error.status_code = 429
                raise error
            if attempts["n"] == 4:
                error = RuntimeError("504 Gateway Timeout")
                error.status_code = 504
                raise error
            return iter([record()])

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: MixedClient(),
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        sleep=sleeps.append,
        report=reports.append,
    )

    assert len(result.captures) == 1
    assert attempts["n"] == 5
    assert sleeps == [60.0, 120.0, 240.0, 480.0]
    assert reports == [
        "Wayback timed out looking up the CDX index (HTTP 504).",
        "waiting 60s (attempt 2/10).",
        "Wayback timed out looking up the CDX index (HTTP 504).",
        "waiting 120s (attempt 3/10).",
        "Wayback returned HTTP 429 (too many requests).",
        "waiting 240s (attempt 4/10).",
        "Wayback timed out looking up the CDX index (HTTP 504).",
        "waiting 480s (attempt 5/10).",
    ]


def test_fetch_cdx_enforces_wall_clock_budget(monkeypatch):
    import time as time_module

    sleeps: list[float] = []
    reports: list[str] = []
    closed = {"n": 0}

    class SlowClient:
        def search(self, *args, **kwargs):
            def gen():
                time_module.sleep(2.0)
                yield record()

            return gen()

        def close(self):
            closed["n"] += 1

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.DEFAULT_CDX_TIMEOUT_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: SlowClient(),
    )
    with pytest.raises(RuntimeError, match="wall-clock budget"):
        _fetch_cdx(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
            sleep=sleeps.append,
            report=reports.append,
        )
    assert sleeps == []
    assert reports == []
    assert closed["n"] >= 1


def test_fetch_cdx_uses_linear_backoff_on_connection_refused(monkeypatch):
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
                    '"Failed to establish a new connection: '
                    '[Errno 61] Connection refused"))'
                )
                raise WaybackRetryError(4, 19.04, cause) from cause
            return iter([record()])

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: FlakyClient(),
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        sleep=sleeps.append,
        report=reports.append,
    )

    assert len(result.captures) == 1
    assert attempts["n"] == 3
    assert sleeps == [60.0, 120.0]
    assert reports == [
        "Could not connect to Wayback.",
        "waiting 60s (attempt 2/10).",
        "Could not connect to Wayback.",
        "waiting 120s (attempt 3/10).",
    ]


def test_fetch_cdx_escalates_past_retry_after_on_http_429(monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []
    reports: list[str] = []

    class RateLimitedClient:
        def search(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 4:
                error = RuntimeError("Wayback rate limit exceeded, retry after 60 s")
                error.retry_after = 60
                error.status_code = 429
                raise error
            return iter([record()])

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: RateLimitedClient(),
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        sleep=sleeps.append,
        report=reports.append,
    )

    assert len(result.captures) == 1
    assert attempts["n"] == 4
    assert sleeps == [60.0, 120.0, 240.0]
    assert reports == [
        "Wayback returned HTTP 429 (too many requests).",
        "waiting 60s (attempt 2/10).",
        "Wayback returned HTTP 429 (too many requests).",
        "waiting 120s (attempt 3/10).",
        "Wayback returned HTTP 429 (too many requests).",
        "waiting 240s (attempt 4/10).",
    ]


def test_fetch_cdx_uses_retry_after_when_larger_than_backoff(monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []

    class RateLimitedClient:
        def search(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                error = RuntimeError("Wayback rate limit exceeded")
                error.retry_after = 300
                error.status_code = 429
                raise error
            return iter([record()])

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: RateLimitedClient(),
    )
    result = _fetch_cdx(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        sleep=sleeps.append,
        report=lambda _message: None,
    )
    assert len(result.captures) == 1
    assert sleeps == [300.0, 300.0]


def test_fetch_cdx_raises_after_exhausted_connection_refused_retries(monkeypatch):
    sleeps: list[float] = []

    class RefusedClient:
        def search(self, *args, **kwargs):
            raise ConnectionError("Max retries exceeded: [Errno 61] Connection refused")

        def close(self):
            return None

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: RefusedClient(),
    )
    with pytest.raises(RuntimeError, match="CDX query failed after 10 attempts"):
        _fetch_cdx(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
            sleep=sleeps.append,
            report=lambda _message: None,
        )
    assert sleeps == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0, 600.0, 600.0, 600.0]


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
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx.WaybackClient",
        lambda **_kwargs: PermanentClient(),
    )
    with pytest.raises(RuntimeError, match="CDX query failed after 1 attempts"):
        _fetch_cdx(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
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
    assert parse_date_bound("2004-12-31", default="", bound="end") == ("20041231235959")
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


def test_date_windows_splits_into_inclusive_n_day_chunks():
    assert list(_date_windows("20040101000000", "20040131235959", 10)) == [
        ("20040101000000", "20040110235959"),
        ("20040111000000", "20040120235959"),
        ("20040121000000", "20040130235959"),
        ("20040131000000", "20040131235959"),
    ]


def test_date_windows_never_cross_year_boundary():
    windows = list(_date_windows("20031225000000", "20040105000000", 10))
    assert windows == [
        ("20031225000000", "20031231235959"),
        ("20040101000000", "20040105000000"),
    ]
    assert all(start[:4] == end[:4] for start, end in windows)


def test_date_windows_handles_leap_day_and_short_range():
    assert list(_date_windows("20040220000000", "20040305000000", 10)) == [
        ("20040220000000", "20040229235959"),
        ("20040301000000", "20040305000000"),
    ]
    assert list(_date_windows("20040615000000", "20040617000000", 10)) == [
        ("20040615000000", "20040617000000"),
    ]
    assert list(_date_windows("20040615000000", "20040615000000", 1)) == [
        ("20040615000000", "20040615000000"),
    ]


def test_date_windows_rejects_non_positive_days():
    with pytest.raises(ValueError, match="must be positive"):
        list(_date_windows("20040101000000", "20040110235959", 0))


def test_next_split_days_uses_28_then_7():
    assert _next_split_days("20040101000000", "20041231235959", 28) == 28
    assert _next_split_days("20040101000000", "20040128235959", 28) == 7
    assert _next_split_days("20040101000000", "20040107235959", 28) is None
    assert _window_calendar_days("20040101000000", "20040128235959") == 28
    assert _format_cdx_window_label("20040101000000", "20040110235959") == (
        "2004-01-01..2004-01-10"
    )
    assert _format_cdx_index_scope(2004, "20040101000000", "20041231235959") == "2004"
    assert _format_cdx_index_scope(2004, "20040101000000", "20040128235959") == (
        "2004-01-01 to 2004-01-28"
    )


def test_normalize_cdx_search_rewrites_wildcard_and_prefix():
    assert _normalize_cdx_search("*.example.org") == ("example.org", "domain")
    assert _normalize_cdx_search("http://*.example.org/") == (
        "example.org",
        "domain",
    )
    assert _normalize_cdx_search("http://example.org/path/*") == (
        "http://example.org/path/",
        "prefix",
    )
    assert _normalize_cdx_search("http://example.org/a") == (
        "http://example.org/a",
        None,
    )
