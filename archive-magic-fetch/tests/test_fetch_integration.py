"""End-to-end run_fetch orchestration and run records."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.collection import (
    ArchiveLayout,
    ensure_collection_dirs,
    list_collection_warcs,
)
from archive_magic_fetch.config import StorageConfig
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import publish_collection_index
from archive_magic_fetch.identity import make_identity
from archive_magic_fetch.protocol import (
    MISSING_CDX_STATUS,
)
from archive_magic_fetch.inventory import (
    get_warc_identity,
    inventory_collection,
)
from archive_magic_fetch.playback import payload_digest
from archive_magic_fetch.warc import CollectionWarcWriter
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
    layout = ArchiveLayout(tmp_path, "example.org")
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
                    archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
                ),
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            )
            assert result.exit_code == 0
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    layout = ArchiveLayout(tmp_path, "example.org")
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
        archive_id="example.org",
                    storage=StorageConfig("local", tmp_path),
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
                    archive_id=settings.archive_id,
                    storage=settings.storage,
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
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original


def test_slash_redirect_substitution_is_stored_and_revisited(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=client_factory,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    from archive_magic_fetch.protocol import CDX_DIGEST_MATCH_HEADER

    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=client_factory,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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

    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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


def test_matching_payloads_in_different_years_are_downloaded_from_ia(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

    assert result.exit_code == 0
    assert downloads == ["20040601000000", "20050601000000"]
    assert result.metrics.downloads == 2
    assert result.metrics.payload_reuses == 0
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


def test_different_ia_digest_downloads_twice(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

    assert result.exit_code == 0
    assert result.metrics.downloads == 2
    assert len(downloads) == 2


def test_failed_older_capture_does_not_use_later_success(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

    # First fails permanently, second downloads as promoted representative,
    # third becomes a revisit.
    assert downloads == (
        ["20040601000000"] * 5 + ["20040602000000"]
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


def test_completed_run_reports_expected_failures(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

    assert result.exit_code == 0
    run_dirs = list((layout.capture_dir("2004") / "runs").iterdir())
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run.json").read_text())
    assert "schema_version" not in record
    assert "warc_version" not in record
    assert "warc_target_bytes" not in record
    assert record["archive_id"] == "example.org"
    assert record["collection_id"] == "2004"
    assert record["counts"]["payload_reused"] == 0
    assert set(record["metrics"]) == {
        "cdx_duration_s",
        "playback_attempts",
        "playback_bytes",
        "warc_write_s",
        "index_s",
        "attempts_by_category",
    }
    assert len(record["failures"]) == 1
    assert record["failures"][0]["identity"]["timestamp"] == bad.timestamp
    assert record["query"]["url_pattern"] == "http://example.org/"
    assert record["query"]["match_type"] is None
    assert list_collection_warcs(layout, "2004")
    assert all(w["record_count"] > 0 for w in record["warcs"])
    assert record["index"]["filename"] == (
        "collections/2004/example.org-2004-index.cdxj"
    )


def test_scoped_rerun_keeps_prior_collection_and_records_only_current_failures(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

    assert result.exit_code == 0
    assert layout.collection_index("2004").read_bytes() == original_index
    assert layout.collection_index("2005").is_file()
    run_dirs = list((layout.capture_dir("2005") / "runs").iterdir())
    record = json.loads((run_dirs[0] / "run.json").read_text())
    assert record["collection_id"] == "2005"
    assert record["failures"] == []


def test_failed_capture_retries_successfully_on_rerun(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
        archive_id="example.org",
                    storage=StorageConfig("local", tmp_path),
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
        cdx_mod.fetch_cdx = original
        fetch_mod.fetch_cdx = original

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
    from archive_magic_fetch.cdx import CdxResult
    import archive_magic_fetch.fetch as fetch_mod

    def empty_year(*, date_start, date_end, **_kwargs):
        return CdxResult(
            captures=(),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(fetch_mod, "fetch_cdx", empty_year)
    result = run_fetch(
        FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20051231235959",
            archive_id="example.org",
                    storage=StorageConfig("local", tmp_path),
        ),
        client_factory=lambda: MagicMock(),
    )

    assert result.exit_code == 0
    layout = result.layout
    run_ids = []
    for year in ("2004", "2005"):
        runs = list((layout.capture_dir(year) / "runs").iterdir())
        assert len(runs) == 1
        run_ids.append(runs[0].name)
        assert layout.run_record(year, runs[0].name).is_file()
        assert not layout.collection_dir(year).exists()
    assert run_ids[0] == run_ids[1]


def test_cdx_year_failure_continues_with_later_years(tmp_path, monkeypatch, capsys):
    from archive_magic_fetch.cdx import CdxResult
    from archive_magic_fetch.models import ParsedCapture
    import archive_magic_fetch.fetch as fetch_mod

    first = make_capt(ts="20040601000000")
    later = make_capt(
        ts="20060601000000",
        digest="sha1:" + "B" * 32,
        urlkey="org,example)/b",
        url="http://example.org/b",
    )
    queried: list[int] = []
    downloads: list[str] = []

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        year = int(str(date_start)[:4])
        queried.append(year)
        if year == 2005:
            raise RuntimeError("CDX query failed after 5 attempts: Connection refused")
        capture = first if year == 2004 else later
        return CdxResult(
            captures=(ParsedCapture(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity)

    monkeypatch.setattr(fetch_mod, "fetch_cdx", fake_fetch_cdx)
    result = run_fetch(
        FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20061231235959",
            archive_id="example.org",
            storage=StorageConfig("local", tmp_path),
        ),
        client_factory=lambda: MagicMock(),
        download_fn=download_fn,
        sleep=lambda _seconds: None,
    )

    assert queried == [2004, 2005, 2006]
    assert result.exit_code == 1
    assert result.failed_years == (2005,)
    assert downloads == [first.timestamp, later.timestamp]
    layout = result.layout
    assert inventory_collection(layout, "2004").contains(first)
    assert inventory_collection(layout, "2006").contains(later)
    assert not list_collection_warcs(layout, "2005")
    output = capsys.readouterr().out
    assert "year 2005: failed (" in output
    assert "continuing with remaining years" in output
    assert "failed years: 2005" in output


@pytest.mark.parametrize(
    "legacy_name",
    ("archive", "sources", "index.cdxj", "collection.json", "failures.json"),
)
def test_legacy_layout_rejects_all_artifacts(tmp_path, legacy_name):
    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
        )


def test_interrupt_finalizes_appended_records_without_run_json(tmp_path, monkeypatch):
    import archive_magic_fetch.fetch as fetch_mod
    layout = ArchiveLayout(tmp_path, "example.org")
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
    real_log = fetch_mod.log_url_outcome

    def log_then_interrupt(*args, **kwargs):
        real_log(*args, **kwargs)
        if interrupt_once["armed"]:
            interrupt_once["armed"] = False
            raise KeyboardInterrupt()

    monkeypatch.setattr(fetch_mod, "log_url_outcome", log_then_interrupt)

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
                    archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
                    playback_workers=1,
                ),
                client_factory=lambda: MagicMock(),
                download_fn=download_fn,
                sleep=lambda _s: None,
            )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod_patched.fetch_cdx = original

    assert downloaded == [first.original_url]
    warcs = list_collection_warcs(layout, "2004")
    assert [path.name for path in warcs] == ["example.org-2004-001.warc.gz"]
    assert layout.collection_index("2004").is_file()
    assert inventory_collection(layout, "2004").contains(first)
    runs = layout.capture_dir("2004") / "runs"
    assert not runs.exists() or not any(
        (path / "run.json").is_file() for path in runs.iterdir()
    )
    assert not list(layout.collection_dir("2004").glob("*.partial"))

    downloaded.clear()
    original, cdx_mod, fetch_mod_patched = patch_cdx(body)
    try:
        result = run_fetch(
            FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040602000000",
                archive_id="example.org",
                storage=StorageConfig("local", tmp_path),
            ),
            client_factory=lambda: MagicMock(),
            download_fn=download_fn,
            sleep=lambda _s: None,
        )
    finally:
        cdx_mod.fetch_cdx = original
        fetch_mod_patched.fetch_cdx = original

    assert result.exit_code == 0
    assert downloaded == [second.original_url]
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz"
    ]
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)
