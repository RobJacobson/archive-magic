"""End-to-end run_fetch orchestration and run records."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from archive_magic_fetch.archive.format import MISSING_CDX_STATUS
from archive_magic_fetch.archive.identity import make_identity, payload_digest
from archive_magic_fetch.archive.inventory_collection import (
    get_warc_identity,
    inventory_collection,
)
from archive_magic_fetch.archive.layout import (
    ArchiveLayout,
    ensure_collection_dirs,
    list_collection_warcs,
)
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.pipeline.build_collection_index import build_collection_index
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from archive_magic_fetch.pipeline.write_captures import _CollectionWarcWriter
from helpers import (
    cdx_json,
    found_capture_client,
    make_capt,
    make_source,
    patch_cdx,
    patch_cdx_by_year,
    playback,
)
from warcio.archiveiterator import ArchiveIterator
from archive_magic_fetch.pipeline.discovery.cache import wayback_path


def test_statusless_capture_three_runs_no_extra_network(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(body)
    try:
        for _run in range(3):
            run_settings = FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040615000000",
                date_end="20040615000000",
                archive_id="example.org",
                output=FetchOutput("local", tmp_path / "data"),
            )
            result = run_fetch(
                run_settings,
                sleep=lambda _s: None,
                source=make_source(
                    run_settings,
                    client_factory=lambda: MagicMock(),
                    download=download_fn,
                    sleep=lambda _s: None,
                ),
            )
            assert result.exit_code == 0
    finally:
        cdx_mod._fetch_cdx = original

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
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(body)
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040615000000",
        date_end="20040615000000",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    try:
        assert (
            run_fetch(
                settings,
                sleep=lambda _s: None,
                source=make_source(
                    settings,
                    client_factory=lambda: MagicMock(),
                    download=download_fn,
                    sleep=lambda _s: None,
                ),
            ).exit_code
            == 0
        )
        assert calls["n"] == 1
        assert inventory_collection(layout, "2004").contains(identity)

        assert (
            run_fetch(
                settings,
                sleep=lambda _s: None,
                source=make_source(
                    settings,
                    client_factory=lambda: MagicMock(),
                    download=download_fn,
                    sleep=lambda _s: None,
                ),
            ).exit_code
            == 0
        )
        assert calls["n"] == 1

        run_settings = FetchSettings(
            url_pattern=settings.url_pattern,
            date_start=settings.date_start,
            date_end=settings.date_end,
            archive_id=settings.archive_id,
            output=settings.output,
            reset_data=True,
        )
        assert (
            run_fetch(
                run_settings,
                sleep=lambda _s: None,
                source=make_source(
                    run_settings,
                    client_factory=lambda: MagicMock(),
                    download=download_fn,
                    sleep=lambda _s: None,
                ),
            ).exit_code
            == 0
        )
        assert calls["n"] == 2
    finally:
        cdx_mod._fetch_cdx = original


@pytest.mark.parametrize("status", ["200", "301"])
def test_unplayable_capture_leaves_same_digest_available_for_next_capture(
    tmp_path, status
):
    from helpers import fetch_memento, memento_client

    layout = ArchiveLayout(tmp_path / "data", "example.org")
    body = b"shared content"
    captures = [
        make_capt(
            url="http://example.org/groups",
            ts=f"2004100{day}000000",
            status=status,
            digest=payload_digest(body),
        )
        for day in (1, 2, 3)
    ]
    client = found_capture_client(
        "http://example.org/groups/",
        "20041009202542",
        body,
    )
    requested = []

    def download(_client, identity):
        requested.append(identity)
        if identity == captures[0]:
            return fetch_memento(client, identity)
        return fetch_memento(memento_client(identity, body), identity)

    cdx_body = cdx_json(
        [
            [
                identity.urlkey,
                identity.timestamp,
                identity.original_url,
                "text/html",
                status,
                identity.payload_digest.split(":")[1],
                "14",
            ]
            for identity in captures
        ]
    )
    original, cdx_mod = patch_cdx(cdx_body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start=captures[0].timestamp,
            date_end=captures[-1].timestamp,
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert requested == captures[:2]
    assert client.calls == 1
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    assert result.metrics.unresolved == 1
    assert [failure.identity for failure in result.failures] == captures[:1]
    inv = inventory_collection(layout, "2004")
    assert inv.identities == set(captures[1:])
    with list_collection_warcs(layout, "2004")[0].open("rb") as stream:
        records = [
            (record.rec_type, record.rec_headers.get_header("WARC-Date"))
            for record in ArchiveIterator(stream)
            if record.rec_type != "warcinfo"
        ]
    assert records == [
        ("response", "2004-10-02T00:00:00Z"),
        ("revisit", "2004-10-03T00:00:00Z"),
    ]


def test_same_year_representative_revisits_include_redirects(tmp_path):
    """Same urlkey+digest+status revisits, including empty 301s; 302 stays distinct."""

    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
            return playback(identity, body=b"", status=int(identity.status_token))
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
    original, cdx_mod = patch_cdx(body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040605000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

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


def test_identical_digest_with_missing_cdx_status_is_a_revisit(tmp_path):
    """IA warc/revisit rows use status '-'; they share the payload digest."""

    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    body_bytes = b"logo-bytes"
    dig = payload_digest(body_bytes).split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.status_token)
        return playback(identity, body=body_bytes)

    cdx_body = cdx_json(
        [
            [
                "com,example)/logo.png",
                "20080311181249",
                "http://example.org/logo.png",
                "image/png",
                "200",
                dig,
                "80",
            ],
            [
                "com,example)/logo.png",
                "20080408192501",
                "http://example.org/logo.png",
                "warc/revisit",
                "-",
                dig,
                "50",
            ],
        ]
    )
    original, cdx_mod = patch_cdx(cdx_body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20080311181249",
            date_end="20080408192501",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert downloads == ["200"]
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1


def test_empty_http_200_downloads_once_then_revisits(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040603000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert set(downloads) == {"20040601000000", "20040603000000"}
    assert result.metrics.downloads == 2
    assert result.metrics.payload_reuses == 0
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


def test_matching_payloads_in_each_year_get_independent_responses(tmp_path):
    body = b"logo"
    digest = payload_digest(body)
    downloads = []
    bodies = {
        year: cdx_json(
            [
                [
                    "org,example)/",
                    f"{year}060{day}000000",
                    "http://example.org/",
                    "text/html",
                    "200",
                    digest.split(":")[1],
                    "4",
                ]
                for day in (1, 2)
            ]
        )
        for year in (2004, 2005)
    }
    original, cdx_mod = patch_cdx_by_year(bodies)
    try:
        settings = FetchSettings(
            "http://example.org/",
            "20040601000000",
            "20050602000000",
            "example.org",
            FetchOutput("local", tmp_path / "data"),
        )

        def download(_client, identity):
            downloads.append(identity.timestamp)
            return playback(identity, body=body)

        source = make_source(
            settings, client_factory=lambda: MagicMock(), download=download
        )
        result = run_fetch(settings, source=source)
        assert result.exit_code == 0
        assert downloads == ["20040601000000", "20050601000000"]
        assert result.metrics.downloads == result.metrics.revisits == 2
        for year in ("2004", "2005"):
            records = []
            with list_collection_warcs(result.layout, year)[0].open("rb") as stream:
                for record in ArchiveIterator(stream):
                    records.append(record.rec_type)
                    if record.rec_type == "revisit":
                        assert (
                            record.rec_headers.get_header("WARC-Refers-To-Date")
                            == f"{year}-06-01T00:00:00Z"
                        )
                    record.raw_stream.read()
            assert records == ["warcinfo", "response", "revisit"]
        resumed = run_fetch(settings, source=source)
        assert resumed.metrics.local_reuses == 4
        assert resumed.metrics.downloads == 0
        assert len(downloads) == 2
    finally:
        cdx_mod._fetch_cdx = original


def test_different_ia_digest_downloads_twice(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(cdx_body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040602000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert result.metrics.downloads == 2
    assert len(downloads) == 2


def test_failed_older_capture_does_not_use_later_success(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx_by_year(bodies)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20050601000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 0
    assert any(f.identity.timestamp == "20040601000000" for f in result.failures)
    assert list_collection_warcs(layout, "2004") == []
    types = []
    with list_collection_warcs(layout, "2005")[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            types.append(record.rec_type)
            record.raw_stream.read()
    assert types.count("response") == 1


def test_representative_failure_promotes_next_same_key_candidate(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(cdx_body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040603000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    # First fails permanently, second downloads as promoted representative,
    # third becomes a revisit.
    assert downloads == (["20040601000000"] * 5 + ["20040602000000"])
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
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    original, cdx_mod = patch_cdx(body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040602000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    records = list(layout.logs_root.glob("*.json"))
    assert len(records) == 1
    logs = list(layout.logs_root.glob("*.log"))
    assert len(logs) == 1
    assert logs[0].stem == records[0].stem
    assert "done: downloads=1" in logs[0].read_text()
    record = json.loads(records[0].read_text())
    assert "schema_version" not in record
    assert "warc_version" not in record
    assert "warc_target_bytes" not in record
    assert record["archive_id"] == "example.org"
    assert set(record["years"]) == {"2004"}
    record = record["years"]["2004"]
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
    assert record["index"]["filename"] == "example.org-2004-index.cdxj"


def test_scoped_rerun_keeps_prior_collection_and_records_only_current_failures(
    tmp_path,
):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    # Seed a portable 2004 collection; a 2005-only run must leave it unchanged.
    capt = make_capt(ts="20040615000000")
    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(capt))
    writer.close()
    build_collection_index(layout, "2004")
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

    original, cdx_mod = patch_cdx(body_2005)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20050601000000",
            date_end="20050601000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert layout.collection_index("2004").read_bytes() == original_index
    assert layout.collection_index("2005").is_file()
    records = list(layout.logs_root.glob("*.json"))
    record = json.loads(records[0].read_text())["years"]["2005"]
    assert record["collection_id"] == "2005"
    assert record["failures"] == []


def test_failed_capture_retries_successfully_on_rerun(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
        output=FetchOutput("local", tmp_path / "data"),
    )
    original, cdx_mod = patch_cdx(cdx_body)
    try:
        first = run_fetch(
            settings,
            sleep=lambda _s: None,
            source=make_source(
                settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
        assert first.exit_code == 0
        assert attempts["n"] == 1
        assert not list_collection_warcs(layout, "2004")
        records = list(layout.logs_root.glob("*.json"))
        assert len(records) == 1
        first_record = json.loads(records[0].read_text())["years"]["2004"]
        assert len(first_record["failures"]) == 1
        assert first_record["failures"][0]["identity"]["timestamp"] == capt.timestamp

        second = run_fetch(
            settings,
            sleep=lambda _s: None,
            source=make_source(
                settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert second.exit_code == 0
    assert attempts["n"] == 2
    assert layout.collection_index("2004").is_file()
    inv = inventory_collection(layout, "2004")
    assert inv.contains(capt)

    records = sorted(layout.logs_root.glob("*.json"))
    assert len(records) == 2
    second_record = json.loads(records[1].read_text())["years"]["2004"]
    assert second_record["failures"] == []
    assert second_record["counts"]["downloaded"] == 1
    assert not (layout.root / "failures.json").exists()


def test_multi_year_empty_run_shares_id_without_playback_collections(
    tmp_path, monkeypatch
):
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    def empty_year(*, date_start, date_end, **_kwargs):
        return _CdxResult(
            captures=(),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", empty_year
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20051231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        source=make_source(run_settings, client_factory=lambda: MagicMock()),
    )

    assert result.exit_code == 0
    layout = result.layout
    records = list(layout.logs_root.glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert set(record["years"]) == {"2004", "2005"}


def test_cdx_year_failure_continues_with_later_years(tmp_path, monkeypatch, capsys):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    first = make_capt(ts="20040601000000")
    later = make_capt(
        ts="20060601000000",
        digest="sha1:" + "B" * 32,
        urlkey="org,example)/b",
        url="http://example.org/b",
    )
    queried: list[tuple[str, str]] = []
    downloads: list[str] = []

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        queried.append((str(date_start), str(date_end)))
        year = int(str(date_start)[:4])
        if year == 2005:
            raise RuntimeError("CDX query failed after 10 attempts: Connection refused")
        capture = first if year == 2004 else later
        if not (str(date_start) <= capture.timestamp <= str(date_end)):
            return _CdxResult(
                captures=(), search_url="http://example.org/", match_type=None
            )
        return _CdxResult(
            captures=(CaptureRef(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity)

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20061231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=download_fn,
            sleep=lambda _seconds: None,
        ),
    )

    assert queried == [
        ("20040101000000", "20041231235959"),
        ("20050101000000", "20051231235959"),
        ("20060101000000", "20061231235959"),
    ]
    assert result.exit_code == 1
    assert result.failed_years == (2005,)
    assert downloads == [first.timestamp, later.timestamp]
    layout = result.layout
    assert inventory_collection(layout, "2004").contains(first)
    assert inventory_collection(layout, "2006").contains(later)
    assert not list_collection_warcs(layout, "2005")
    assert not (wayback_path(tmp_path / "discovery", "http://example.org/", 2005)).exists()
    output = capsys.readouterr().out
    assert "year 2005: failed" in output
    assert "not querying" not in output
    assert "splitting into" not in output
    assert "fetching 28-day ranges" not in output
    assert "failed years: 2005" in output


def test_cdx_504_skips_year_without_splitting_and_continues(
    tmp_path, monkeypatch, capsys
):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    later = make_capt(
        ts="20050601000000",
        digest="sha1:" + "B" * 32,
        urlkey="org,example)/b",
        url="http://example.org/b",
    )
    queried: list[tuple[str, str]] = []
    gateway = (
        "CDX query failed after 10 attempts: "
        "504 Server Error: Gateway Time-out for url: "
        "https://web.archive.org/cdx/search/cdx?url=example.org"
    )

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        queried.append((str(date_start), str(date_end)))
        year = int(str(date_start)[:4])
        if year == 2004:
            raise RuntimeError(gateway)
        return _CdxResult(
            captures=(CaptureRef(identity=later, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20051231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )

    assert queried == [
        ("20040101000000", "20041231235959"),
        ("20050101000000", "20051231235959"),
    ]
    assert result.exit_code == 1
    assert result.failed_years == (2004,)
    assert not (wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).exists()
    assert (wayback_path(tmp_path / "discovery", "http://example.org/", 2005)).is_file()
    output = capsys.readouterr().out
    assert "year 2004: failed" in output
    assert "splitting into" not in output
    assert "fetching 28-day ranges" not in output
    assert "not querying" not in output
    assert "failed years: 2004" in output


def test_cdx_wall_clock_splits_28_then_7_and_stops_on_failure(
    tmp_path, monkeypatch, capsys
):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult, _date_windows

    january = make_capt(ts="20040105000000")
    calls: list[tuple[str, str]] = []
    downloads: list[str] = []
    year_bounds = ("20040101000000", "20041231235959")
    windows_28 = list(_date_windows(*year_bounds, 28))
    heavy_28 = next(
        window for window in windows_28 if window[0] <= "20040315000000" <= window[1]
    )
    windows_7 = list(_date_windows(*heavy_28, 7))
    hole_7 = next(
        window for window in windows_7 if window[0] <= "20040315000000" <= window[1]
    )
    wall_clock = RuntimeError(
        "CDX query failed after 1 attempts: CDX query exceeded 300s wall-clock budget"
    )

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        start = str(date_start)
        end = str(date_end)
        calls.append((start, end))
        if (start, end) in {year_bounds, heavy_28, hole_7}:
            raise wall_clock
        if start <= "20040105000000" <= end:
            return _CdxResult(
                captures=(CaptureRef(identity=january, mime="text/html"),),
                search_url="http://example.org/",
                match_type=None,
            )
        return _CdxResult(
            captures=(),
            search_url="http://example.org/",
            match_type=None,
        )

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        return playback(identity)

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20040331235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=download_fn,
            sleep=lambda _seconds: None,
        ),
    )

    assert calls[0] == year_bounds
    assert heavy_28 in calls
    assert hole_7 in calls
    assert calls.count(year_bounds) == 1
    assert calls.count(heavy_28) == 1
    assert result.exit_code == 1
    assert result.failed_years == (2004,)
    assert downloads == []
    layout = result.layout
    assert not list_collection_warcs(layout, "2004")
    assert not layout.collection_index("2004").exists()
    assert calls[-1] == hole_7
    assert not (wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).exists()
    assert not (layout.logs_root / "cdx").exists()
    output = capsys.readouterr().out
    assert "CDX index took too long." in output
    assert "fetching 28-day ranges" in output
    assert "fetching 7-day ranges" in output
    assert "failed years: 2004" in output


def test_cdx_year_success_uses_single_query(tmp_path, monkeypatch):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    capture = make_capt(ts="20040601000000")
    calls: list[tuple[str, str]] = []

    def fake_fetch_cdx(*, date_start, date_end, limit, **_kwargs):
        calls.append((str(date_start), str(date_end)))
        assert limit == 5000
        return _CdxResult(
            captures=(CaptureRef(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )

    assert calls == [("20040101000000", "20041231235959")]
    assert result.exit_code == 0
    year_record = json.loads(next(result.layout.logs_root.glob("*.json")).read_text())[
        "years"
    ]["2004"]
    assert year_record["query"]["cdx_page_limit"] == 5000
    assert "cdx_fallback" not in year_record["query"]
    assert "failed_windows" not in year_record["query"]


def test_cdx_failed_year_resumes_windows_and_successful_fallback_is_cached(
    tmp_path, monkeypatch
):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult, _date_windows

    january = make_capt(ts="20040105000000")
    march = make_capt(
        ts="20040315000000",
        digest="sha1:" + "B" * 32,
        urlkey="org,example)/b",
        url="http://example.org/b",
    )
    year_bounds = ("20040101000000", "20041231235959")
    heavy_28 = next(
        window
        for window in _date_windows(*year_bounds, 28)
        if window[0] <= "20040315000000" <= window[1]
    )
    hole_7 = next(
        window
        for window in _date_windows(*heavy_28, 7)
        if window[0] <= "20040315000000" <= window[1]
    )
    wall_clock = RuntimeError(
        "CDX query failed after 1 attempts: CDX query exceeded 300s wall-clock budget"
    )
    fail_hole = True
    calls: list[tuple[str, str]] = []

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        start = str(date_start)
        end = str(date_end)
        calls.append((start, end))
        if (start, end) in {year_bounds, heavy_28} or (
            fail_hole and (start, end) == hole_7
        ):
            raise wall_clock
        if start <= "20040105000000" <= end:
            capture = january
        elif start <= "20040315000000" <= end:
            capture = march
        else:
            return _CdxResult(
                captures=(),
                search_url="http://example.org/",
                match_type=None,
            )
        return _CdxResult(
            captures=(CaptureRef(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20040331235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    first = run_fetch(
        settings,
        sleep=lambda _seconds: None,
        source=make_source(
            settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )
    assert first.exit_code == 1
    assert not (wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).exists()
    failed_calls = list(calls)
    calls.clear()
    fail_hole = False
    second = run_fetch(
        settings,
        sleep=lambda _seconds: None,
        source=make_source(
            settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )
    assert calls[0] == hole_7
    assert year_bounds not in calls and heavy_28 not in calls
    assert all(window not in calls for window in failed_calls if window not in {year_bounds, heavy_28, hole_7})
    assert calls[-1][1] == "20041231235959"
    assert second.exit_code == 0
    cached = json.loads((wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).read_text())["captures"]
    assert [item["timestamp"] for item in cached] == [
        january.timestamp,
        march.timestamp,
    ]
    calls.clear()
    third = run_fetch(
        settings,
        source=make_source(
            settings,
            client_factory=lambda: MagicMock(),
            download=lambda *_args: pytest.fail("cached WARC capture downloaded again"),
        ),
    )
    assert third.exit_code == 0
    assert calls == []
    assert inventory_collection(second.layout, "2004").contains(january)
    assert inventory_collection(second.layout, "2004").contains(march)


def test_legacy_cdx_checkpoint_is_ignored_and_preserved(tmp_path, monkeypatch, capsys):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    capture = make_capt(ts="20040601000000")
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    checkpoint = layout.logs_root / "cdx" / "2004.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "url_pattern": "*.other.org",
                "cdx_page_limit": 5000,
                "date_start": "20040101000000",
                "date_end": "20041231235959",
                "completed": [],
                "holes": [
                    {
                        "date_start": "20040101000000",
                        "date_end": "20041231235959",
                        "kind": "transient",
                        "message": "old hole",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    def fake_fetch_cdx(*, date_start, date_end, **_kwargs):
        calls.append((str(date_start), str(date_end)))
        return _CdxResult(
            captures=(CaptureRef(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", fake_fetch_cdx
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )
    assert calls == [("20040101000000", "20041231235959")]
    assert result.exit_code == 0
    assert json.loads(checkpoint.read_text())["url_pattern"] == "*.other.org"
    assert (wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).is_file()


@pytest.mark.parametrize(
    "legacy_name",
    ("archive", "sources", "index.cdxj", "collection.json", "failures.json"),
)
def test_legacy_layout_rejects_all_artifacts(tmp_path, legacy_name):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    layout.root.mkdir()
    target = layout.root / legacy_name
    if legacy_name in {"archive", "sources"}:
        target.mkdir()
    else:
        target.write_text("legacy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="delete and regenerate"):
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040601000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        run_fetch(
            run_settings,
            source=make_source(run_settings, client_factory=lambda: MagicMock()),
        )


def test_interrupt_retains_staged_year_without_run_json(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.write_captures as writing_module

    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    real_log = writing_module.log_url_outcome

    def log_then_interrupt(*args, **kwargs):
        real_log(*args, **kwargs)
        if interrupt_once["armed"]:
            interrupt_once["armed"] = False
            raise KeyboardInterrupt()

    monkeypatch.setattr(writing_module, "log_url_outcome", log_then_interrupt)

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
    original, cdx_mod = patch_cdx(body)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_settings = FetchSettings(
                url_pattern="http://example.org/",
                date_start="20040601000000",
                date_end="20040602000000",
                archive_id="example.org",
                output=FetchOutput("local", tmp_path / "data"),
                playback_workers=1,
            )
            run_fetch(
                run_settings,
                sleep=lambda _s: None,
                source=make_source(
                    run_settings,
                    client_factory=lambda: MagicMock(),
                    download=download_fn,
                    sleep=lambda _s: None,
                ),
            )
    finally:
        cdx_mod._fetch_cdx = original

    assert downloaded == [first.original_url]
    warcs = list_collection_warcs(layout, "2004")
    assert warcs == []
    assert not layout.collection_index("2004").exists()
    records = list(layout.logs_root.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["years"] == {}
    logs = list(layout.logs_root.glob("*.log"))
    assert len(logs) == 1
    assert "fetching CDX index for 2004\n" in logs[0].read_text()
    assert (wayback_path(tmp_path / "discovery", "http://example.org/", 2004)).is_file()
    assert not list(layout.collection_dir("2004").glob("*.partial"))

    downloaded.clear()
    original, cdx_mod = patch_cdx(body)
    try:
        run_settings = FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040601000000",
            date_end="20040602000000",
            archive_id="example.org",
            output=FetchOutput("local", tmp_path / "data"),
        )
        result = run_fetch(
            run_settings,
            sleep=lambda _s: None,
            source=make_source(
                run_settings,
                client_factory=lambda: MagicMock(),
                download=download_fn,
                sleep=lambda _s: None,
            ),
        )
    finally:
        cdx_mod._fetch_cdx = original

    assert result.exit_code == 0
    assert downloaded == [second.original_url]
    assert result.metrics.local_reuses == 1
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz"
    ]
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)
