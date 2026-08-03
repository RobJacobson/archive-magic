from datetime import datetime, timezone

import pytest
from wayback import CdxRecord

from archive_magic_fetch import redirects
from archive_magic_fetch.redirects import (
    expand_redirect_target,
    permanent_redirect_target,
    redirect_scope,
    resolve_redirect_target,
)


@pytest.mark.parametrize("status_code", (200, 300, 302, 303, 307, 404))
def test_non_permanent_responses_do_not_introduce_targets(status_code):
    assert resolve_redirect_target(
        "https://source.test/start",
        status_code,
        (("Location", "https://target.test/"),),
    ) is None


@pytest.mark.parametrize("status_code", (301, 308))
def test_permanent_redirects_resolve_relative_locations(status_code):
    assert resolve_redirect_target(
        "https://source.test/path/start",
        status_code,
        (("location", "../landing?view=all#section"),),
    ) == "https://source.test/landing?view=all"


def test_protocol_relative_location_is_supported():
    assert resolve_redirect_target(
        "http://source.test/",
        301,
        (("Location", "//target.test/page#fragment"),),
    ) == "http://target.test/page"


@pytest.mark.parametrize(
    ("headers", "message"),
    (
        ((), "no Location"),
        ((("Location", "mailto:person@example.com"),), "unsupported"),
        ((("Location", "http://"),), "absolute"),
        (
            (("Location", "https://user:pass@target.test/"),),
            "user information",
        ),
    ),
)
def test_invalid_locations_are_rejected(headers, message):
    with pytest.raises(ValueError, match=message):
        resolve_redirect_target("https://source.test/", 301, headers)


def test_permanent_redirect_target_returns_warning_for_invalid_location():
    target, warning = permanent_redirect_target(
        "https://source.test/",
        301,
        (),
    )
    assert target is None
    assert warning is not None
    assert "no Location" in warning


def test_page_scope_uses_an_exact_query_and_deduplicates_authority():
    first = redirect_scope("http://www.target.test/page?q=1", "page")
    second = redirect_scope("https://target.test/page?q=1", "page")

    assert first.url == "http://www.target.test/page?q=1"
    assert first.match_type == "exact"
    assert first.key == second.key


def test_page_scope_keeps_paths_queries_and_significant_ports_distinct():
    scopes = {
        redirect_scope("https://target.test/one", "page").key,
        redirect_scope("https://target.test/two", "page").key,
        redirect_scope("https://target.test/one?q=1", "page").key,
        redirect_scope("https://target.test:8443/one", "page").key,
    }

    assert len(scopes) == 4


def test_website_scope_uses_a_host_query_and_canonicalizes_www():
    first = redirect_scope("http://www.target.test/one", "website")
    second = redirect_scope("https://target.test/two", "website")
    numbered = redirect_scope("https://www1.target.test/three", "website")

    assert first.url == "http://www.target.test/one"
    assert first.match_type == "host"
    assert first.key == second.key
    assert numbered.key == second.key


def test_redirect_scope_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported redirect capture mode"):
        redirect_scope("https://target.test/", "all")


def _capture(original, status, *, timestamp="20000101000000", digest="A" * 32):
    domain = original.split("//", 1)[1].split("/", 1)[0]
    return CdxRecord(
        urlkey=f"{domain})/",
        timestamp=datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        original=original,
        mimetype="text/html",
        statuscode=status,
        digest=digest,
        length=10,
    )


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_expand_redirect_target_skips_known_histories_and_dedupes_scopes(monkeypatch):
    known = _capture("https://source.test/", 301)
    first = _capture("https://target.test/", 200)
    second = first._replace(statuscode=404, digest="B" * 32, length=20)
    searches = []

    monkeypatch.setattr(
        redirects,
        "search_captures",
        lambda _client, url, *_args, **_kwargs: searches.append(url)
        or [known, first, second],
    )

    seen = set()
    known_keys = {("source.test", known.urlkey)}
    expansion = expand_redirect_target(
        _Client(),
        "https://target.test/",
        mode="page",
        date_start="1995",
        date_end="2001",
        seen_searches=seen,
        known_history_keys=known_keys,
        retries=0,
    )

    assert searches == ["https://target.test/"]
    assert expansion is not None
    assert expansion.search.captures == (known, first, second)
    assert set(expansion.histories) == {("target.test", first.urlkey)}
    assert expansion.histories[("target.test", first.urlkey)] == [first, second]

    again = expand_redirect_target(
        _Client(),
        "https://target.test/",
        mode="page",
        date_start="1995",
        date_end="2001",
        seen_searches=seen,
        known_history_keys=known_keys,
        retries=0,
    )
    assert again is None
    assert searches == ["https://target.test/"]


def test_expand_redirect_target_returns_none_for_empty_search(monkeypatch):
    monkeypatch.setattr(redirects, "search_captures", lambda *_a, **_k: [])

    assert (
        expand_redirect_target(
            _Client(),
            "https://missing.test/",
            mode="website",
            date_start="1995",
            date_end="2001",
            seen_searches=set(),
            known_history_keys=set(),
            retries=0,
        )
        is None
    )
