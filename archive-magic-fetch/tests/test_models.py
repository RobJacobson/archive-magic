"""Model and identity helpers."""

from __future__ import annotations

from archive_magic_fetch.identity import (
    make_identity,
    normalize_original_url,
    revisit_group_key,
    same_original_url,
)
from archive_magic_fetch.protocol import EMPTY_PAYLOAD_DIGEST, MISSING_CDX_PAYLOAD_DIGEST

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


def test_revisit_group_key_splits_empty_payloads_on_status():
    digest = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    png_200 = make_identity(
        original_url="http://example.org/logo.png",
        timestamp="20040615000000",
        status_token="200",
        payload_digest=digest,
    )
    png_revisit = make_identity(
        original_url="http://example.org/logo.png",
        timestamp="20040616000000",
        status_token="-",
        payload_digest=digest,
    )
    redirect_301 = make_identity(
        original_url="http://example.org/",
        timestamp="20040615000000",
        status_token="301",
        payload_digest=EMPTY_PAYLOAD_DIGEST,
    )
    redirect_302 = make_identity(
        original_url="http://example.org/",
        timestamp="20040616000000",
        status_token="302",
        payload_digest=EMPTY_PAYLOAD_DIGEST,
    )
    missing = make_identity(
        original_url="http://example.org/",
        timestamp="20040617000000",
        status_token="301",
        payload_digest=MISSING_CDX_PAYLOAD_DIGEST,
    )

    assert revisit_group_key(png_200) == revisit_group_key(png_revisit)
    assert revisit_group_key(png_200) == (
        png_200.urlkey,
        png_200.payload_digest,
        "",
    )
    key_301 = revisit_group_key(redirect_301)
    key_302 = revisit_group_key(redirect_302)
    assert key_301 == (
        redirect_301.urlkey,
        EMPTY_PAYLOAD_DIGEST,
        "301",
    )
    assert key_302 is not None
    assert key_301 != key_302
    assert revisit_group_key(missing) is None


def test_empty_payload_digest_matches_sha1_of_zero_bytes():
    from archive_magic_fetch.identity import is_empty_payload_digest
    from archive_magic_fetch.protocol import EMPTY_PAYLOAD_DIGEST
    from archive_magic_fetch.playback import payload_digest

    assert payload_digest(b"") == EMPTY_PAYLOAD_DIGEST
    assert is_empty_payload_digest(EMPTY_PAYLOAD_DIGEST)
    assert is_empty_payload_digest(EMPTY_PAYLOAD_DIGEST[5:])
    assert not is_empty_payload_digest("sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
