"""WARC writing, inventory, digestion, and playback classification."""

from __future__ import annotations

from unittest.mock import MagicMock

from archive_magic_fetch.archive.format import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_URLKEY_HEADER,
)
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.archive.inventory_collection import (
    get_warc_identity,
    inventory_collection,
)
from archive_magic_fetch.archive.layout import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    list_collection_warcs,
)
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.models import CaptureResult
from archive_magic_fetch.pipeline.build_collection_index import build_collection_index
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from archive_magic_fetch.pipeline.write_captures import _CollectionWarcWriter, _validate_warc
from helpers import (
    cdx_json,
    fetch_memento,
    make_capt,
    make_source,
    memento_client,
    patch_cdx,
    playback,
)
from warcio.archiveiterator import ArchiveIterator


def test_empty_redirect_playback_is_stored_with_location(tmp_path):
    """Historical 3xx captures often have an empty body; still archive them."""

    empty_digest = payload_digest(b"")
    identity = make_capt(
        url="http://example.org/thecase",
        ts="20080404233814",
        status="302",
        digest=empty_digest,
    )
    location = "http://example.org/site/page/the_case"
    result = fetch_memento(
        memento_client(identity, b"", headers={"Location": location}),
        identity,
    )
    assert result.status_code == 302
    assert result.body == b""
    assert result.digest_matched is True
    assert result.warc_payload_digest == empty_digest
    assert any(
        name.lower() == "location" and value == location
        for name, value in result.headers
    )

    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = _CollectionWarcWriter(layout, "2008")
    writer.write_playback(result)
    artifacts = writer.close()
    with artifacts[0].path.open("rb") as stream:
        records = list(ArchiveIterator(stream))
    responses = [rec for rec in records if rec.rec_type == "response"]
    assert len(responses) == 1
    rec = responses[0]
    assert rec.http_headers.get_statuscode() == "302"
    assert rec.http_headers.get_header("Location") == location
    assert rec.content_stream().read() == b""


def test_inventory_remembers_redirect_representative_by_status(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    empty = payload_digest(b"")
    identity = make_capt(
        url="http://example.org/thecase",
        ts="20040603000000",
        status="301",
        digest=empty,
    )
    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(identity, body=b"", status=301))
    writer.close()
    build_collection_index(layout, "2004")

    inv = inventory_collection(layout, "2004")
    stored = inv.lookup_representative(
        identity.urlkey,
        empty,
        "301",
        not_after_timestamp="20040604000000",
    )
    assert stored is not None
    assert stored.identity.timestamp == "20040603000000"
    assert (
        inv.lookup_representative(
            identity.urlkey,
            empty,
            "302",
            not_after_timestamp="20040604000000",
        )
        is None
    )


def test_trailing_newline_soft_match_seeds_revisit_and_survives_inventory(
    tmp_path,
):
    """IA CDX hashed body+LF; playback body without LF still revisits."""

    body = b"<html>soft</html>"
    dig = payload_digest(body + b"\n").split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        from helpers import fetch_memento

        downloads.append(identity.timestamp)
        return fetch_memento(memento_client(identity, body), identity)

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
        ]
    )
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
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
    assert downloads == ["20040601000000"]
    assert result.metrics.downloads == 1
    assert result.metrics.revisits == 1
    assert result.metrics.digest_mismatch_accepted == 0

    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        responses = []
        revisits = []
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                responses.append(record)
                assert record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) is None
                assert record.rec_headers.get_header("WARC-Payload-Digest") == (
                    payload_digest(body)
                )
                assert record.content_stream().read() == body
            elif record.rec_type == "revisit":
                revisits.append(record)
                record.raw_stream.read()
            else:
                record.raw_stream.read()
    assert len(responses) == 1
    assert len(revisits) == 1

    inv = inventory_collection(layout, "2004")
    assert (
        inv.lookup_representative(
            "com,example)/",
            f"sha1:{dig}",
            "200",
            not_after_timestamp="20040602000000",
        )
        is not None
    )


def test_custom_cdx_urlkey_survives_warc_inventory(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    identity = make_capt(urlkey="custom,key)/special")
    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(identity))
    writer.close()
    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                assert record.rec_headers.get_header(CDX_URLKEY_HEADER) == (
                    "custom,key)/special"
                )
                rebuilt = get_warc_identity(record)
                assert rebuilt.urlkey == "custom,key)/special"
                assert rebuilt == identity
                record.raw_stream.read()
    build_collection_index(layout, "2004")
    inv = inventory_collection(layout, "2004")
    assert inv.contains(identity)


def test_digest_mismatch_is_kept_but_never_seeds_revisit(tmp_path):

    from archive_magic_fetch.archive.format import CDX_DIGEST_MATCH_HEADER

    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    claimed = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    body = b"imperfect-but-kept"
    dig = claimed.split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        result = playback(identity, body=body)
        return CaptureResult(
            identity=result.identity,
            body=result.body,
            status_code=result.status_code,
            headers=result.headers,
            warc_date=result.warc_date,
            source_uri=result.source_uri,
            warc_payload_digest=payload_digest(body),
            digest_matched=False,
        )

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
    assert downloads == ["20040601000000", "20040602000000"]
    assert result.metrics.downloads == 2
    assert result.metrics.revisits == 0
    assert result.metrics.digest_mismatch_accepted == 2

    warc = list_collection_warcs(layout, "2004")[0]
    with warc.open("rb") as stream:
        responses = []
        revisits = []
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                responses.append(record)
                assert record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) == "false"
                assert record.rec_headers.get_header("WARC-Payload-Digest") == (
                    payload_digest(body)
                )
            if record.rec_type == "revisit":
                revisits.append(record)
                assert record.rec_headers.get_header("WARC-Payload-Digest") == (
                    payload_digest(body)
                )
            record.raw_stream.read()
    assert len(responses) == 2
    assert len(revisits) == 0

    inv = inventory_collection(layout, "2004")
    assert (
        inv.lookup_representative(
            "com,example)/", claimed, "200", not_after_timestamp="20040602000000"
        )
        is None
    )


def test_warc_rollover_naming_has_no_arbitrary_sequence_limit(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = _CollectionWarcWriter(layout, "2004", target_bytes=1)
    for i in range(2):
        capt = make_capt(
            ts=f"2004060{i + 1}000000",
            digest="sha1:" + ("E" * 31 + str(i)),
        )
        writer.write_playback(playback(capt, body=b"x" * 100))
    warcs = writer.close()
    assert len(warcs) == 2
    assert warcs[0].relative_key.endswith("-2004-001.warc.gz")
    assert warcs[1].relative_key.endswith("-2004-002.warc.gz")
    for artifact in warcs:
        assert artifact.record_count == 2
        assert _validate_warc(artifact.path) == artifact.record_count

    writer = _CollectionWarcWriter(layout, "2005", target_bytes=1, sequence=1000)
    writer.write_playback(playback(make_capt()))
    assert writer.close()[0].path.name.endswith("-1000.warc.gz")


def test_resume_appends_to_same_shard_under_size_cap(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    first = make_capt(ts="20040601000000")
    second = make_capt(
        ts="20040602000000",
        digest="sha1:" + "B" * 32,
    )
    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(first))
    writer.close()
    path = layout.collection_warc_path("2004", 1)
    prefix = path.read_bytes()

    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(second, body=b"later"))
    writer.close()

    warcs = list_collection_warcs(layout, "2004")
    assert [path.name for path in warcs] == ["example.org-2004-001.warc.gz"]
    assert warcs[0].read_bytes().startswith(prefix)
    build_collection_index(layout, "2004")
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)


def test_resume_starts_next_shard_when_last_is_at_cap(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = _CollectionWarcWriter(layout, "2004", target_bytes=1)
    writer.write_playback(playback(make_capt(ts="20040601000000")))
    writer.close()
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz"
    ]

    writer = _CollectionWarcWriter(layout, "2004", target_bytes=1)
    writer.write_playback(
        playback(
            make_capt(ts="20040602000000", digest="sha1:" + "B" * 32),
            body=b"next-shard",
        )
    )
    writer.close()
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz",
        "example.org-2004-002.warc.gz",
    ]


def test_cleanup_temps_removes_legacy_warc_partials(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    collection_dir = layout.collection_dir("2004")
    collection_dir.mkdir(parents=True, exist_ok=True)
    partial = collection_dir / "example.org-2004-001.warc.gz.partial"
    partial.write_bytes(b"keep-me")
    stray = collection_dir / ".tmp-index.cdxj.tmp"
    stray.write_text("x", encoding="utf-8")

    cleanup_temps(layout)

    assert not partial.exists()
    assert not stray.exists()
