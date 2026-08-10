"""WARC writing, inventory, digestion, and playback classification."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from wayback.exceptions import MementoPlaybackError
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.collection import (
    archive_layout,
    ensure_collection_dirs,
    list_collection_warcs,
    next_collection_warc_sequence,
)
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.models import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_URLKEY_HEADER,
    FailureCategory,
    PlaybackResult,
)
from archive_magic_fetch.warc import (
    CollectionWarcWriter,
    classify_playback_error,
    get_warc_identity,
    inventory_collection,
    payload_digest,
    validate_warc,
)
from helpers import cdx_json, make_capt, memento_client, patch_cdx, playback

def test_empty_redirect_playback_is_stored_with_location(tmp_path):
    """Historical 3xx captures often have an empty body; still archive them."""

    from archive_magic_fetch.collection import archive_layout, ensure_collection_dirs
    from archive_magic_fetch.warc import (
        CollectionWarcWriter,
        download_exact_for_identity,
        payload_digest,
    )
    from warcio.archiveiterator import ArchiveIterator

    empty_digest = payload_digest(b"")
    identity = make_capt(
        url="http://example.org/thecase",
        ts="20080404233814",
        status="302",
        digest=empty_digest,
    )
    location = "http://example.org/site/page/the_case"
    result = download_exact_for_identity(
        memento_client(identity, b"", headers={"Location": location}),
        identity,
    )
    assert result.status_code == 302
    assert result.body == b""
    assert result.digest_matched is True
    assert result.warc_payload_digest == empty_digest
    assert any(name.lower() == "location" and value == location for name, value in result.headers)

    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2008")
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


def test_empty_non_redirect_playback_is_rejected():
    from archive_magic_fetch.warc import (
        UnusablePlaybackError,
        download_exact_for_identity,
    )

    identity = make_capt(status="200")
    with pytest.raises(UnusablePlaybackError, match="empty playback body"):
        download_exact_for_identity(memento_client(identity, b""), identity)


def test_invalid_uri_playback_is_always_rejected():
    from archive_magic_fetch.warc import (
        UnusablePlaybackError,
        classify_playback_error,
        download_exact_for_identity,
    )

    identity = make_capt()
    with pytest.raises(UnusablePlaybackError):
        download_exact_for_identity(
            memento_client(identity, b"Invalid URI"), identity
        )
    category, retryable = classify_playback_error(
        UnusablePlaybackError("IA playback stub: Invalid URI")
    )
    assert category == FailureCategory.UNAVAILABLE
    assert retryable is False


def test_custom_cdx_urlkey_survives_warc_inventory(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    identity = make_capt(urlkey="custom,key)/special")
    writer = CollectionWarcWriter(layout, "2004")
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
    inv = inventory_collection(layout, "2004")
    assert inv.contains(identity)


def test_digest_mismatch_is_kept_but_never_seeds_revisit(tmp_path):

    from archive_magic_fetch.models import CDX_DIGEST_MATCH_HEADER

    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    claimed = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    body = b"imperfect-but-kept"
    dig = claimed.split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        downloads.append(identity.timestamp)
        result = playback(identity, body=body)
        return PlaybackResult(
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
    assert inv.lookup_representative(
        "com,example)/", claimed, not_after_timestamp="20040602000000"
    ) is None


def test_playback_5xx_is_retryable():
    category, retryable = classify_playback_error(
        MementoPlaybackError("500 error while loading memento at http://x")
    )
    assert retryable is True
    assert category == FailureCategory.RETRY_EXHAUSTED


def test_wrapped_incomplete_read_is_permanent_truncated_failure():
    from requests.exceptions import ChunkedEncodingError
    from wayback.exceptions import WaybackRetryError

    incomplete = ChunkedEncodingError(
        "Connection broken: IncompleteRead(130810 bytes read, "
        "292753 more expected)"
    )
    wrapped = WaybackRetryError(0, 0.08, incomplete)

    category, retryable = classify_playback_error(wrapped)

    assert category == FailureCategory.TRUNCATED
    assert retryable is False


def test_warc_rollover_naming_and_rejects_1000(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
    for i in range(2):
        capt = make_capt(
            ts=f"2004060{i+1}000000",
            digest="sha1:" + ("E" * 31 + str(i)),
        )
        writer.write_playback(playback(capt, body=b"x" * 100))
    warcs = writer.close()
    assert len(warcs) == 2
    assert warcs[0].relative_key.endswith("-2004-001.warc.gz")
    assert warcs[1].relative_key.endswith("-2004-002.warc.gz")
    for artifact in warcs:
        validate_warc(artifact.path)

    for seq in range(1, 1000):
        path = layout.collection_warc_path("2005", seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    with pytest.raises(RuntimeError, match="999"):
        next_collection_warc_sequence(layout, "2005")


