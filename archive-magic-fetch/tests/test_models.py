"""Model and identity helpers."""

from __future__ import annotations

from archive_magic_fetch.models import (
    make_identity,
    normalize_original_url,
    same_original_url,
)

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


def test_same_original_url_accepts_ia_double_encoding():
    cdx = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%20DESC&state=46"
    )
    link = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%2520DESC&state=46"
    )
    assert same_original_url(cdx, link)
    assert same_original_url(cdx, cdx)
    assert not same_original_url(
        cdx,
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=other&page=5&sort=name%20DESC&state=46",
    )
    assert not same_original_url(
        "http://example.org/a",
        "https://example.org/a",
    )
    assert same_original_url(
        "http://example.org:80/q?x=%20",
        "http://example.org/q?x=%2520",
    )
