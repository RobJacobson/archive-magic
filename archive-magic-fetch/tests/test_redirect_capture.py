import pytest

from archive_magic_fetch.redirect_capture import (
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

    assert first.url == "http://www.target.test/one"
    assert first.match_type == "host"
    assert first.key == second.key


def test_redirect_scope_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported redirect capture mode"):
        redirect_scope("https://target.test/", "all")
