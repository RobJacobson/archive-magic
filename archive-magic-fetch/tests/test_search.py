from datetime import datetime, timezone

import pytest
from wayback import CdxRecord
from wayback.exceptions import RateLimitError, UnexpectedResponseFormat

from archive_magic_fetch import search


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def record(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    captured="20000101000000",
    statuscode=200,
    digest="A" * 32,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest=digest,
        length=100,
    )


def test_select_latest_prefers_newest_200_over_newer_non_200():
    older_200 = record(captured="20100101000000", statuscode=200)
    newer_404 = record(captured="20200101000000", statuscode=404)
    newer_301 = record(captured="20210101000000", statuscode=301)

    assert search.select_latest_capture(
        [older_200, newer_404, newer_301]
    ) is older_200


def test_select_latest_uses_timestamp_not_input_order():
    older_200 = record(captured="20100101000000", statuscode=200)
    newer_200 = record(
        captured="20200101000000",
        statuscode=200,
        digest="B" * 32,
    )

    assert search.select_latest_capture([newer_200, older_200]) is newer_200


def test_select_latest_prefers_newest_non_redirect_when_no_200():
    older_404 = record(captured="20100101000000", statuscode=404)
    newer_301 = record(captured="20200101000000", statuscode=301)

    assert search.select_latest_capture([newer_301, older_404]) is older_404


def test_select_latest_uses_newest_redirect_for_redirect_only_groups():
    only_301 = record(captured="20200101000000", statuscode=301)
    only_302 = record(captured="20210101000000", statuscode=302)

    assert (
        search.select_latest_capture([only_301, only_302]) is only_302
    )


def test_select_captures_latest_and_none():
    first = record(
        urlkey="com,example)/",
        captured="20100101000000",
        statuscode=404,
    )
    second = record(
        urlkey="com,example)/",
        captured="20200101000000",
        statuscode=200,
    )
    third = record(
        urlkey="com,example)/about",
        original="https://example.com/about",
        captured="20200101000000",
        statuscode=301,
    )
    groups = {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }

    assert search.select_captures(groups, "none") == {}
    assert search.select_captures(groups, "latest") == {
        first.urlkey: [second],
        third.urlkey: [third],
    }
    assert search.select_captures(groups, "all") == {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }
    assert search.select_captures(groups, "unique") == {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }
    assert search.select_captures(groups, "all") is groups
    assert search.select_captures(groups, "unique") is groups


def test_normalize_cdx_search_rewrites_trailing_star_to_explicit_prefix():
    assert search.normalize_cdx_search("example.com/*") == (
        "example.com/",
        "prefix",
    )
    assert search.normalize_cdx_search("https://example.com/path/*") == (
        "https://example.com/path/",
        "prefix",
    )


def test_normalize_cdx_search_leaves_non_prefix_patterns_unchanged():
    assert search.normalize_cdx_search("example.com/") == (
        "example.com/",
        None,
    )
    assert search.normalize_cdx_search("*.example.com") == (
        "*.example.com",
        None,
    )


def test_search_captures_materializes_search_with_explicit_bounds():
    expected = [record()]
    calls = []

    class Client:
        def search(self, url_pattern, **kwargs):
            calls.append((url_pattern, kwargs))
            return iter(expected)

    assert search.search_captures(
        Client(), "example.com/*", "1995", "2020"
    ) == expected
    assert calls == [
        (
            "example.com/",
            {
                "from_date": "1995",
                "to_date": "2020",
                "limit": 10_000,
                "resolve_revisits": False,
                "match_type": "prefix",
            },
        )
    ]


def test_search_captures_passes_exact_patterns_without_match_type():
    expected = [record()]
    calls = []

    class Client:
        def search(self, url_pattern, **kwargs):
            calls.append((url_pattern, kwargs))
            return iter(expected)

    assert search.search_captures(
        Client(), "example.com/", "2002", "2002"
    ) == expected
    assert calls == [
        (
            "example.com/",
            {
                "from_date": "2002",
                "to_date": "2002",
                "limit": 10_000,
                "resolve_revisits": False,
            },
        )
    ]


def test_search_captures_accepts_explicit_host_match_type():
    calls = []

    class Client:
        def search(self, url_pattern, **kwargs):
            calls.append((url_pattern, kwargs))
            return iter(())

    assert search.search_captures(
        Client(),
        "https://example.com/path",
        "2002",
        "2003",
        match_type="host",
    ) == []
    assert calls[0][1]["match_type"] == "host"


def test_search_captures_reports_progress_after_each_request_limit(monkeypatch):
    monkeypatch.setattr(search, "_CDX_REQUEST_LIMIT", 2)
    expected = [record() for _ in range(5)]
    reported = []
    limits = []

    class Client:
        def search(self, *args, **kwargs):
            limits.append(kwargs["limit"])
            return iter(expected)

    assert (
        search.search_captures(
            Client(),
            "example.com",
            "1995",
            "2020",
            progress=reported.append,
        )
        == expected
    )
    assert limits == [2]
    assert reported == [2, 4]


def test_search_captures_discards_partial_attempt_and_rematerializes_after_rate_limit(
    monkeypatch,
    capsys,
):
    first = record(captured="20000101000000")
    second = record(captured="20010101000000")
    attempts = 0
    sleeps = []
    reported = []

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1

            def results():
                yield first
                if attempts == 1:
                    raise RateLimitError(None, 7)
                yield second

            return results()

    monkeypatch.setattr(search, "sleep_seconds", sleeps.append)

    assert search.search_captures(
        Client(),
        "example.com",
        "1995",
        "2020",
        progress=reported.append,
    ) == [
        first,
        second,
    ]
    assert attempts == 2
    assert sleeps == [10]
    assert reported == []
    output = capsys.readouterr().out
    assert "https://web.archive.org/cdx/search/cdx?" in output
    assert "retry 1/8 in 10s" in output


def test_search_captures_rate_limit_without_retry_after_uses_exponential_delay(
    monkeypatch,
    capsys,
):
    attempts = 0
    sleeps = []

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RateLimitError(None, None)
            return iter([])

    monkeypatch.setattr(search, "sleep_seconds", sleeps.append)

    assert search.search_captures(Client(), "example.com", "1995", "2020") == []
    assert sleeps == [10]
    assert "retry 1/8 in 10s" in capsys.readouterr().out


def test_search_captures_retries_repeated_rate_limits(monkeypatch, capsys):
    sleeps = []
    attempts = 0

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                raise RateLimitError(None, 3)
            return iter([])

    monkeypatch.setattr(search, "sleep_seconds", sleeps.append)

    assert search.search_captures(Client(), "example.com", "1995", "2020") == []
    assert attempts == 4
    assert sleeps == [10, 20, 40]
    assert capsys.readouterr().out.count("\n  retry ") == 3


def test_search_captures_unexpected_response_format_is_fatal():
    class Client:
        def search(self, *args, **kwargs):
            raise UnexpectedResponseFormat("malformed CDX response")

    with pytest.raises(UnexpectedResponseFormat):
        search.search_captures(Client(), "example.com", "1995", "2020")


def test_group_by_url_uses_urlkey_and_sorts_by_datetime():
    captures = [
        record(
            urlkey="com,example)/index.html",
            original="https://example.com/index.html",
            captured="20200102000000",
        ),
        record(
            urlkey="com,example)/index.html",
            original="http://example.com/index.html",
            captured="20190101000000",
        ),
        record(
            urlkey="com,example)/other.html",
            original="https://example.com/other.html",
            captured="20180101000000",
        ),
    ]

    grouped = search.group_by_url(captures)

    assert list(grouped) == [
        ("example.com", "com,example)/index.html"),
        ("example.com", "com,example)/other.html"),
    ]
    assert [
        capture.timestamp
        for capture in grouped[("example.com", "com,example)/index.html")]
    ] == [
        timestamp("20190101000000"),
        timestamp("20200102000000"),
    ]


def test_group_by_url_preserves_value_equal_records():
    capture = record()
    equal_capture = record()

    grouped = search.group_by_url([capture, equal_capture])

    assert grouped[("example.com", capture.urlkey)] == [
        capture,
        equal_capture,
    ]
    assert grouped[("example.com", capture.urlkey)][0] is capture
    assert grouped[("example.com", capture.urlkey)][1] is equal_capture


def test_group_by_url_accepts_upstream_default_port_normalization():
    capture = record(original="http://example.com/")

    grouped = search.group_by_url([capture])

    assert grouped[("example.com", capture.urlkey)][0].original == (
        "http://example.com/"
    )


def test_group_by_url_keeps_wildcard_subdomains_in_separate_histories():
    root = record(
        original="https://www.example.com/",
        urlkey="com,example,www)/",
    )
    blog = record(
        original="https://blog.example.com/",
        urlkey="com,example,blog)/",
    )

    captures_by_url = search.group_by_url([root, blog])

    assert set(captures_by_url) == {
        ("example.com", root.urlkey),
        ("blog.example.com", blog.urlkey),
    }
