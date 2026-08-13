"""End-to-end run_fetch orchestration and run records."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.collection import (
    archive_layout,
    ensure_collection_dirs,
    list_collection_warcs,
)
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import publish_collection_index
from archive_magic_fetch.models import (
    MAX_PLAYBACK_ATTEMPTS,
    MISSING_CDX_STATUS,
    make_identity,
)
from archive_magic_fetch.warc import (
    CollectionWarcWriter,
    get_warc_identity,
    inventory_collection,
    payload_digest,
)
from helpers import (
    cdx_json,
    found_capture_client,
    make_capt,
    patch_cdx,
    patch_cdx_by_year,
    playback,
    substitution_client,
)

def test_statusless_capture_three_runs_no_extra_network(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
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
        return playback(capt_identity, body=body_bytes, status=200)

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
    body = cdx_json(rows)
    original, cdx_mod, fetch_mod = patch_cdx(body)
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
    inv = inventory_collection(layout, "2004")
    assert inv.contains(identity)
    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                rebuilt = get_warc_identity(record)
                assert rebuilt.status_token == "-"
                assert rebuilt == identity
                record.raw_stream.read()


def test_reset_data_redownloads_instead_of_reusing(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = make_capt(urlkey="com,example)/")
    calls = {"n": 0}

    def download_fn(_client, capt_identity):
        calls["n"] += 1
        return playback(capt_identity)

    rows = [
        [
            "com,example)/",
            "20040615000000",
            "http://example.org/",
            "text/html",
            "200",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "5",
        ]
    ]
    body = cdx_json(rows)
    original, cdx_mod, fetch_mod = patch_cdx(body)
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040615000000",
        date_end="20040615000000",
        archives_root=tmp_path,
    )
    try:
        assert (
            run_fetch(
                settings,
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            ).exit_code
            == 0
        )
        assert calls["n"] == 1
        assert inventory_collection(layout, "2004").contains(identity)

        assert (
            run_fetch(
                settings,
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            ).exit_code
            == 0
        )
        assert calls["n"] == 1

        assert (
            run_fetch(
                FetchSettings(
                    url_pattern=settings.url_pattern,
                    date_start=settings.date_start,
                    date_end=settings.date_end,
                    archives_root=settings.archives_root,
                    reset_data=True,
                ),
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            ).exit_code
            == 0
        )
        assert calls["n"] == 2
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original


def test_slash_redirect_substitution_is_stored_and_revisited(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    digest = "TV7A2C32YG3CFKH2CYRHAL2D4UPH7RCE"
    first = make_capt(
        url="http://example.org/conference",
        ts="20040303170500",
        status="301",
        digest=f"sha1:{digest}",
        urlkey="org,example)/conference",
    )
    second = make_capt(
        url="http://example.org/conference",
        ts="20040516142118",
        status="301",
        digest=f"sha1:{digest}",
        urlkey="org,example)/conference",
    )
    clients: list[object] = []

    def client_factory():
        client = substitution_client(
            "http://example.org/conference/", "20040510064339"
        )
        clients.append(client)
        return client

    body = cdx_json(
        [
            [
                first.urlkey,
                first.timestamp,
                first.original_url,
                "text/html",
                "301",
                digest,
                "379",
            ],
            [
                second.urlkey,
                second.timestamp,
                second.original_url,
                "text/html",
                "301",
                digest,
                "377",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040303170500",
                date_end="20040516142118",
                archives_root=tmp_path,
            ),
            client_factory=client_factory,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code == 0
    assert sum(getattr(client, "calls", 0) for client in clients) == 1
    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        records = list(ArchiveIterator(stream))
    responses = [rec for rec in records if rec.rec_type == "response"]
    revisits = [rec for rec in records if rec.rec_type == "revisit"]
    assert len(responses) == 1
    assert len(revisits) == 1
    assert responses[0].http_headers.get_statuscode() == "301"
    assert (
        responses[0].http_headers.get_header("Location")
        == "http://example.org/conference/"
    )
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)


def test_found_capture_substitution_is_stored_under_cdx_identity(tmp_path):
    from archive_magic_fetch.models import CDX_DIGEST_MATCH_HEADER

    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = make_capt(
        url="http://example.org/groups/?PHPSESSID=abc",
        ts="20041009172745",
        status="200",
        digest="sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        urlkey="org,example)/groups",
    )
    body = b"<html>groups</html>"
    clients: list[object] = []

    def client_factory():
        client = found_capture_client(
            "http://example.org/groups/", "20041009202542", body
        )
        clients.append(client)
        return client

    cdx_body = cdx_json(
        [
            [
                identity.urlkey,
                identity.timestamp,
                identity.original_url,
                "text/html",
                "200",
                identity.payload_digest.split(":")[1],
                "100",
            ]
        ]
    )
    original, cdx_mod, fetch_mod = patch_cdx(cdx_body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20041009172745",
                date_end="20041009172745",
                archives_root=tmp_path,
            ),
            client_factory=client_factory,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert result.exit_code == 0
    assert result.metrics.downloads == 1
    assert result.metrics.digest_mismatch_accepted == 1
    assert sum(getattr(client, "calls", 0) for client in clients) == 2
    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        stored = None
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                assert record.rec_headers.get_header("WARC-Target-URI") == (
                    identity.original_url
                )
                assert record.rec_headers.get_header("WARC-Date") == (
                    "2004-10-09T17:27:45Z"
                )
                assert record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) == (
                    "false"
                )
                stored = record.content_stream().read()
            else:
                record.raw_stream.read()
    assert stored == body
    inv = inventory_collection(layout, "2004")
    assert inv.contains(identity)


def test_slash_redirect_from_cdx_skips_playback_and_revisits(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    digest = "TV7A2C32YG3CFKH2CYRHAL2D4UPH7RCE"
    empty_dig = payload_digest(b"").split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity)

    body = cdx_json(
        [
            [
                "org,example)/conference",
                "20040303170500",
                "http://example.org/conference",
                "text/html",
                "301",
                digest,
                "379",
            ],
            [
                "org,example)/conference",
                "20040303180000",
                "http://example.org/conference/",
                "text/html",
                "200",
                empty_dig,
                "0",
            ],
            [
                "org,example)/conference",
                "20040516142118",
                "http://example.org/conference",
                "text/html",
                "301",
                digest,
                "377",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040303170500",
                date_end="20040516142118",
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
    assert downloads == []
    assert result.metrics.downloads == 0
    assert result.metrics.payload_reuses == 2
    assert result.metrics.revisits == 1
    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        records = list(ArchiveIterator(stream))
    responses = [rec for rec in records if rec.rec_type == "response"]
    revisits = [rec for rec in records if rec.rec_type == "revisit"]
    assert len(responses) == 2
    assert len(revisits) == 1
    redirect = next(
        rec for rec in responses if rec.http_headers.get_statuscode() == "301"
    )
    assert redirect.http_headers.get_header("Location") == (
        "http://example.org/conference/"
    )


def test_same_year_representative_revisits_include_redirects(tmp_path):
    """Same urlkey+digest+status revisits, including empty 301s; 302 stays distinct."""

    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    shared_body = b"shared"
    dig = payload_digest(shared_body).split(":")[1]
    a_ts = "20040601000000"
    b_ts = "20040602000000"
    redir_301_a = "20040603000000"
    redir_301_b = "20040604000000"
    redir_302 = "20040605000000"
    downloads: list[tuple[str, str]] = []

    def download_fn(_client, identity):
        downloads.append((identity.timestamp, identity.status_token))
        if identity.status_token in {"301", "302"}:
            return playback(
                identity, body=b"", status=int(identity.status_token)
            )
        return playback(identity, body=shared_body, status=200)

    empty_dig = payload_digest(b"").split(":")[1]
    body = cdx_json(
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
                redir_301_a,
                "http://example.org/thecase",
                "text/html",
                "301",
                empty_dig,
                "0",
            ],
            [
                "com,example)/thecase",
                redir_301_b,
                "http://example.org/thecase",
                "text/html",
                "301",
                empty_dig,
                "0",
            ],
            [
                "com,example)/thecase",
                redir_302,
                "http://example.org/thecase",
                "text/html",
                "302",
                empty_dig,
                "0",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040605000000",
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
    assert set(downloads) == {
        (a_ts, "200"),
        (redir_301_a, "301"),
        (redir_302, "302"),
    }
    assert result.metrics.downloads == 3
    assert result.metrics.revisits == 2
    warc = list_collection_warcs(layout, "2004")[0]
    types = []
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 3
    assert types.count("revisit") == 2


def test_empty_http_200_skips_playback_and_revisits(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    empty_dig = payload_digest(b"").split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity, body=b"", status=int(identity.status_token))

    body = cdx_json(
        [
            [
                "com,example)/",
                "20040601000000",
                "http://example.org/",
                "text/html",
                "200",
                empty_dig,
                "0",
            ],
            [
                "com,example)/",
                "20040602000000",
                "http://example.org/",
                "text/html",
                "200",
                empty_dig,
                "0",
            ],
            [
                "com,example)/gone",
                "20040603000000",
                "http://example.org/gone",
                "text/html",
                "301",
                empty_dig,
                "0",
            ],
        ]
    )
    original, cdx_mod, fetch_mod = patch_cdx(body)
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
    assert downloads == ["20040603000000"]
    assert result.metrics.downloads == 1
    assert result.metrics.payload_reuses == 1
    assert result.metrics.revisits == 1
    warc = list_collection_warcs(layout, "2004")[0]
    types = []
    empty_responses = 0
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            if record.rec_type == "response" and record.content_stream().read() == b"":
                empty_responses += 1
                assert record.http_headers.get_header("Content-Length") == "0"
            else:
                record.raw_stream.read()
    assert types.count("response") == 2
    assert types.count("revisit") == 1
    assert empty_responses == 2


def test_matching_payloads_download_once_per_year(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"logo"
    dig = payload_digest(body).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity, body=body)

    bodies = {
        2004: cdx_json(
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
        2005: cdx_json(
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
    original, cdx_mod, fetch_mod = patch_cdx_by_year(bodies)
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
    assert downloads == ["20040601000000"]
    assert result.metrics.downloads == 1
    assert result.metrics.payload_reuses == 1
    assert result.metrics.revisits == 0

    types_2004 = []
    with list_collection_warcs(layout, "2004")[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types_2004.append(record.rec_type)
            record.raw_stream.read()
    assert types_2004.count("response") == 1
    assert types_2004.count("revisit") == 0

    types_2005 = []
    with list_collection_warcs(layout, "2005")[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types_2005.append(record.rec_type)
            record.raw_stream.read()
    assert types_2005.count("response") == 1
    assert types_2005.count("revisit") == 0

    inv = inventory_collection(layout, "2005")
    stored = inv.lookup_representative(
        "com,example)/",
        payload_digest(body),
        "200",
        not_after_timestamp="20050601000000",
    )
    assert stored is not None
    assert stored.identity.timestamp == "20050601000000"


def test_cross_year_payload_reuse_uses_current_cdx_metadata(
    tmp_path,
):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"shared-across-urls"
    digest = payload_digest(body).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        result = playback(identity, body=body)
        return replace(
            result,
            headers=(
                ("Content-Type", "application/octet-stream"),
                ("X-Archived-Source", "2004"),
                ("Content-Length", str(len(body))),
            ),
        )

    rows = {
        2004: cdx_json(
            [["com,example)/a", "20040601000000", "http://example.org/a",
              "application/octet-stream", "200", digest, str(len(body))]]
        ),
        2005: cdx_json(
            [["com,example)/b", "20050601000000", "http://example.org/b",
              "text/plain", "200", digest, str(len(body))]]
        ),
    }
    original, cdx_mod, fetch_mod = patch_cdx_by_year(rows)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/*",
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

    assert downloads == ["20040601000000"]
    assert result.metrics.payload_reuses == 1
    layout.collection_dir("2004").rename(tmp_path / "detached-2004")
    with list_collection_warcs(layout, "2005")[0].open("rb") as stream:
        response = next(
            record for record in ArchiveIterator(stream)
            if record.rec_type == "response"
        )
        assert response.rec_headers.get_header("WARC-Target-URI") == (
            "http://example.org/b"
        )
        assert response.http_headers.get_statuscode() == "200"
        assert response.http_headers.get_header("Content-Type") == "text/plain"
        assert response.http_headers.get_header("X-Archived-Source") is None
        assert response.content_stream().read() == body


def test_cross_year_payload_cache_uses_ia_digest_for_soft_match(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"early-arc"
    ia_digest = payload_digest(body + b"\n").split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity, body=body)

    rows = {
        year: cdx_json(
            [["com,example)/", f"{year}0601000000", "http://example.org/",
              "text/html", "200", ia_digest, str(len(body))]]
        )
        for year in (2004, 2005)
    }
    original, cdx_mod, fetch_mod = patch_cdx_by_year(rows)
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

    assert downloads == ["20040601000000"]
    assert result.metrics.payload_reuses == 1
    meta = json.loads(layout.collection_index("2005").read_text().split(" ", 2)[2])
    assert meta["cdxDigest"] == f"sha1:{ia_digest}"
    assert meta["digest"] == payload_digest(body)


def test_cross_year_identical_bytes_with_different_ia_digests_download_twice(
    tmp_path,
):
    body = b"same-bytes-different-ia-hashes"
    digests = {
        2004: payload_digest(body).split(":")[1],
        2005: payload_digest(body + b"\n").split(":")[1],
    }
    rows = {
        year: cdx_json(
            [["com,example)/", f"{year}0601000000", "http://example.org/",
              "text/html", "200", digest, str(len(body))]]
        )
        for year, digest in digests.items()
    }
    downloads: list[str] = []
    original, cdx_mod, fetch_mod = patch_cdx_by_year(rows)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20050601000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: (
                downloads.append(identity.timestamp) or playback(identity, body=body)
            ),
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert downloads == ["20040601000000", "20050601000000"]
    assert result.metrics.payload_reuses == 0


def test_digest_mismatch_response_never_seeds_cross_year_cache(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    claimed = "F" * 32
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        result = playback(identity, body=f"body-{identity.timestamp}".encode())
        return replace(result, digest_matched=False)

    rows = {
        year: cdx_json(
            [["com,example)/", f"{year}0601000000", "http://example.org/",
              "text/html", "200", claimed, "4"]]
        )
        for year in (2004, 2005)
    }
    original, cdx_mod, fetch_mod = patch_cdx_by_year(rows)
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

    assert downloads == ["20040601000000", "20050601000000"]
    assert result.metrics.payload_reuses == 0


@pytest.mark.parametrize(
    ("source_status", "status", "digest_token", "strip_cache_fields"),
    (
        (200, "-", "valid", False),
        (200, "201", "valid", False),
        (200, "206", "valid", False),
        (200, "302", "valid", False),
        (200, "200", "-", False),
        (201, "200", "valid", False),
        (206, "200", "valid", False),
        (302, "200", "valid", False),
        (200, "200", "valid", True),
    ),
)
def test_ineligible_current_capture_downloads_individually(
    tmp_path,
    source_status,
    status,
    digest_token,
    strip_cache_fields,
):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"status-sensitive"
    digest = payload_digest(body).split(":")[1]
    older = make_capt(
        ts="20040601000000",
        status=str(source_status),
        digest=f"sha1:{digest}",
    )
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(older, body=body, status=source_status))
    writer.close()
    publish_collection_index(layout, "2004")
    if strip_cache_fields:
        index_path = layout.collection_index("2004")
        parts = index_path.read_text().strip().split(" ", 2)
        meta = json.loads(parts[2])
        meta.pop("cdxDigest")
        meta.pop("cdxDigestMatch")
        index_path.write_text(f"{parts[0]} {parts[1]} {json.dumps(meta)}\n")

    current_digest = digest if digest_token == "valid" else digest_token
    row = [["com,example)/", "20050601000000", "http://example.org/",
            "text/html", status, current_digest, str(len(body))]]
    downloads: list[str] = []
    original, cdx_mod, fetch_mod = patch_cdx(cdx_json(row))
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20050601000000",
                date_end="20050601000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: (
                downloads.append(identity.timestamp) or playback(identity, body=body)
            ),
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert downloads == ["20050601000000"]
    assert result.metrics.payload_reuses == 0


@pytest.mark.parametrize("corruption", ("timestamp", "range"))
def test_future_or_corrupt_payload_cache_entry_falls_back_to_download(
    tmp_path,
    corruption,
):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"ordered-cache"
    digest = payload_digest(body).split(":")[1]
    future = make_capt(ts="20050601000000", digest=f"sha1:{digest}")
    writer = CollectionWarcWriter(layout, "2005")
    writer.write_playback(playback(future, body=body))
    writer.close()
    publish_collection_index(layout, "2005")

    row = [["com,example)/", "20040601000000", "http://example.org/",
            "text/html", "200", digest, str(len(body))]]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity, body=body)

    original, cdx_mod, fetch_mod = patch_cdx(cdx_json(row))
    try:
        first = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040601000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert first.metrics.payload_reuses == 0
    assert downloads == ["20040601000000"]

    # Corrupt the earlier locator and request a later year with the same digest.
    index_path = layout.collection_index("2004")
    parts = index_path.read_text().strip().split(" ", 2)
    meta = json.loads(parts[2])
    if corruption == "timestamp":
        parts[1] = "20030601000000"
    else:
        meta["length"] = "999999999"
    index_path.write_text(f"{parts[0]} {parts[1]} {json.dumps(meta)}\n")
    later_row = [["com,example)/", "20060601000000", "http://example.org/",
                  "text/html", "200", digest, str(len(body))]]
    original, cdx_mod, fetch_mod = patch_cdx(cdx_json(later_row))
    try:
        second = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20060601000000",
                date_end="20060601000000",
                archives_root=tmp_path,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert second.metrics.payload_reuses == 0
    assert downloads[-1] == "20060601000000"


def test_reset_data_allows_prior_year_payload_reuse(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"reset-cache"
    digest = payload_digest(body).split(":")[1]
    older = make_capt(ts="20040601000000", digest=f"sha1:{digest}")
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(older, body=body))
    writer.close()
    publish_collection_index(layout, "2004")

    row = [["com,example)/", "20050601000000", "http://example.org/",
            "text/html", "200", digest, str(len(body))]]
    downloads: list[str] = []
    original, cdx_mod, fetch_mod = patch_cdx(cdx_json(row))
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20050601000000",
                date_end="20050601000000",
                archives_root=tmp_path,
                reset_data=True,
            ),
            client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: (
                downloads.append(identity.timestamp) or playback(identity, body=body)
            ),
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert downloads == []
    assert result.metrics.payload_reuses == 1


def test_different_ia_digest_downloads_twice(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"same-bytes"
    dig_a = payload_digest(body).split(":")[1]
    dig_b = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.payload_digest)
        return playback(identity, body=body)

    cdx_body = cdx_json(
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
    original, cdx_mod, fetch_mod = patch_cdx(cdx_body)
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
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"payload"
    dig = payload_digest(body).split(":")[1]

    def download_fn(_client, identity):
        if identity.timestamp.startswith("2004"):
            raise ConnectionError("memento unavailable")
        return playback(identity, body=body)

    bodies = {
        2004: cdx_json(
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
        2005: cdx_json(
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
    original, cdx_mod, fetch_mod = patch_cdx_by_year(bodies)
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
    assert list_collection_warcs(layout, "2004") == []
    types = []
    with list_collection_warcs(layout, "2005")[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 1


def test_representative_failure_promotes_next_same_key_candidate(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"shared"
    dig = payload_digest(body).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        if identity.timestamp == "20040601000000":
            raise ConnectionError("memento unavailable")
        return playback(identity, body=body)

    cdx_body = cdx_json(
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
    original, cdx_mod, fetch_mod = patch_cdx(cdx_body)
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
    assert downloads == (
        ["20040601000000"] * MAX_PLAYBACK_ATTEMPTS + ["20040602000000"]
    )
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    assert any(f.identity.timestamp == "20040601000000" for f in result.failures)
    types = []
    with list_collection_warcs(layout, "2004")[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 1
    assert types.count("revisit") == 1


def test_completed_run_reports_expected_failures(tmp_path, capsys):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    good_body = b"good"
    good_digest = payload_digest(good_body).split(":")[1]
    bad_digest = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
    good = make_capt(ts="20040601000000", digest=f"sha1:{good_digest}")
    bad = make_capt(ts="20040602000000", digest=f"sha1:{bad_digest}")

    def download_fn(_client, identity):
        if identity.timestamp == bad.timestamp:
            raise RuntimeError("memento unavailable")
        return playback(identity, body=good_body)

    body = cdx_json(
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
    original, cdx_mod, fetch_mod = patch_cdx(body)
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
    run_dirs = list((layout.capture_dir("2004") / "runs").iterdir())
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text())
    assert record["schema_version"] == 2
    assert record["archive_id"] == "example.org"
    assert record["collection_id"] == "2004"
    assert record["counts"]["payload_reused"] == 0
    assert set(record["metrics"]) == {
        "cdx_requests",
        "cdx_duration_s",
        "playback_attempts",
        "playback_bytes",
        "warc_write_s",
        "index_s",
        "attempts_by_category",
    }
    assert len(record["failures"]) == 1
    assert record["failures"][0]["identity"]["timestamp"] == bad.timestamp
    assert record["query"]["raw_file"] == "page-001.cdx.gz"
    assert list_collection_warcs(layout, "2004")
    assert all(w["record_count"] > 0 for w in record["warcs"])
    assert record["index"]["filename"] == (
        "collections/2004/example.org-2004-index.cdxj"
    )
    output = capsys.readouterr().out
    assert (
        "year 2004 done: downloads=1 payload-reuses=0 revisits=0 "
        "already-represented=0 skips/errors=1"
    ) in output
    assert "elapsed " in output
    assert (
        "done: downloads=1 revisits=0 payload-reuses=0 "
        "already-represented=0 skips/errors=1"
        in output
    )


def test_scoped_rerun_keeps_prior_collection_and_records_only_current_failures(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    # Seed a portable 2004 collection; a 2005-only run must leave it unchanged.
    capt = make_capt(ts="20040615000000")
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(capt))
    writer.close()
    publish_collection_index(layout, "2004")
    original_index = layout.collection_index("2004").read_bytes()

    body_2005 = cdx_json(
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
        return playback(identity, body=b"y2005")

    original, cdx_mod, fetch_mod = patch_cdx(body_2005)
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
    assert layout.collection_index("2004").read_bytes() == original_index
    assert layout.collection_index("2005").is_file()
    run_dirs = list((layout.capture_dir("2005") / "runs").iterdir())
    record = json.loads((run_dirs[0] / "run.json").read_text())
    assert record["collection_id"] == "2005"
    assert record["failures"] == []


def test_failed_capture_retries_successfully_on_rerun(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body_bytes = b"eventually-ok"
    dig = payload_digest(body_bytes).split(":")[1]
    capt = make_capt(
        ts="20040615000000",
        digest=f"sha1:{dig}",
        urlkey="com,example)/",
    )
    cdx_body = cdx_json(
        [
            [
                "com,example)/",
                capt.timestamp,
                capt.original_url,
                "text/html",
                "200",
                dig,
                "5",
            ]
        ]
    )

    attempts = {"n": 0}

    def download_fn(_client, identity):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("memento unavailable")
        return playback(identity, body=body_bytes)

    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040615000000",
        date_end="20040615000000",
        archives_root=tmp_path,
    )
    original, cdx_mod, fetch_mod = patch_cdx(cdx_body)
    try:
        first = run_fetch(
            settings,
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
        assert first.exit_code == 0
        assert attempts["n"] == 1
        assert not list_collection_warcs(layout, "2004")
        first_run_dirs = list((layout.capture_dir("2004") / "runs").iterdir())
        assert len(first_run_dirs) == 1
        first_record = json.loads((first_run_dirs[0] / "run.json").read_text())
        assert len(first_record["failures"]) == 1
        assert first_record["failures"][0]["identity"]["timestamp"] == capt.timestamp

        second = run_fetch(
            settings,
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

    assert second.exit_code == 0
    assert attempts["n"] == 2
    assert layout.collection_index("2004").is_file()
    inv = inventory_collection(layout, "2004")
    assert inv.contains(capt)

    run_dirs = sorted((layout.capture_dir("2004") / "runs").iterdir())
    assert len(run_dirs) == 2
    second_record = json.loads((run_dirs[1] / "run.json").read_text())
    assert second_record["failures"] == []
    assert second_record["counts"]["downloaded"] == 1
    assert not (layout.root / "failures.json").exists()


def test_multi_year_empty_run_shares_id_without_playback_collections(
    tmp_path, monkeypatch
):
    from archive_magic_fetch.cdx import YearCdxResult
    import archive_magic_fetch.fetch as fetch_mod

    seen_ids = []

    def empty_year(layout, *, year, run_id, **_kwargs):
        seen_ids.append(run_id)
        run_dir = layout.run_dir(str(year), run_id)
        run_dir.mkdir(parents=True)
        raw = run_dir / "page-001.cdx.gz"
        raw.write_bytes(b"[]")
        return YearCdxResult(
            year=year,
            source_dir=run_dir,
            raw_path=raw,
            captures=(),
            failures=(),
            query_meta={
                "year": year,
                "from": f"{year}0101000000",
                "to": f"{year}1231235959",
                "request_count": 1,
                "raw_file": raw.name,
                "pages": [{"page": 1, "raw_file": raw.name}],
            },
        )

    monkeypatch.setattr(fetch_mod, "fetch_year_cdx", empty_year)
    result = run_fetch(
        FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20051231235959",
            archives_root=tmp_path,
        ),
        client_factory=lambda: MagicMock(),
    )

    assert result.exit_code == 0
    assert len(seen_ids) == 2 and len(set(seen_ids)) == 1
    layout = result.layout
    for year in ("2004", "2005"):
        assert layout.run_record(year, seen_ids[0]).is_file()
        assert not layout.collection_dir(year).exists()




def test_run_record_is_published_after_index(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    dig = payload_digest(b"ordered").split(":")[1]
    body = cdx_json(
        [
            [
                "com,example)/",
                "20040615000000",
                "http://example.org/",
                "text/html",
                "200",
                dig,
                "5",
            ]
        ]
    )

    def download_fn(_client, capt):
        return playback(capt, body=b"ordered")

    original, cdx_mod, fetch_mod = patch_cdx(body)
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
    run_dir = next((layout.capture_dir("2004") / "runs").iterdir())
    run_json = run_dir / "run.json"
    index = layout.collection_index("2004")
    assert run_json.stat().st_mtime_ns >= index.stat().st_mtime_ns


@pytest.mark.parametrize(
    "legacy_name",
    ("archive", "sources", "index.cdxj", "collection.json", "failures.json"),
)
def test_legacy_layout_rejects_all_artifacts(tmp_path, legacy_name):
    layout = archive_layout("http://example.org/", tmp_path)
    layout.root.mkdir(parents=True)
    target = layout.root / legacy_name
    if legacy_name in {"archive", "sources"}:
        target.mkdir()
    else:
        target.write_text("legacy\n", encoding="utf-8")
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


def test_interrupt_finalizes_partial_without_run_json(tmp_path, monkeypatch):
    import archive_magic_fetch.fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "PLAYBACK_WORKERS", 1)
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    first = make_capt(url="http://example.org/a", ts="20040601000000")
    second = make_capt(
        url="http://example.org/b",
        ts="20040602000000",
        digest="sha1:" + "B" * 32,
        urlkey="org,example)/b",
    )
    downloaded: list[str] = []

    def download_fn(_client, identity):
        downloaded.append(identity.original_url)
        return playback(identity)

    interrupt_once = {"armed": True}
    real_log = fetch_mod._log_url_outcome

    def log_then_interrupt(*args, **kwargs):
        real_log(*args, **kwargs)
        if interrupt_once["armed"]:
            interrupt_once["armed"] = False
            raise KeyboardInterrupt()

    monkeypatch.setattr(fetch_mod, "_log_url_outcome", log_then_interrupt)

    body = cdx_json(
        [
            [
                first.urlkey,
                first.timestamp,
                first.original_url,
                "text/html",
                "200",
                first.payload_digest.split(":")[1],
                "5",
            ],
            [
                second.urlkey,
                second.timestamp,
                second.original_url,
                "text/html",
                "200",
                second.payload_digest.split(":")[1],
                "5",
            ],
        ]
    )
    original, cdx_mod, fetch_mod_patched = patch_cdx(body)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_fetch(
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
        fetch_mod_patched.fetch_year_cdx = original

    assert downloaded == [first.original_url]
    warcs = list_collection_warcs(layout, "2004")
    assert [path.name for path in warcs] == ["example.org-2004-001.warc.gz"]
    assert layout.collection_index("2004").is_file()
    assert inventory_collection(layout, "2004").contains(first)
    assert not any(
        (path / "run.json").is_file()
        for path in (layout.capture_dir("2004") / "runs").iterdir()
    )
    assert not layout.collection_warc_partial_path("2004", 1).exists()

    downloaded.clear()
    original, cdx_mod, fetch_mod_patched = patch_cdx(body)
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
        fetch_mod_patched.fetch_year_cdx = original

    assert result.exit_code == 0
    assert downloaded == [second.original_url]
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz"
    ]
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)
