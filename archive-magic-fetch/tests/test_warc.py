"""WARC writing, inventory, digestion, and playback classification."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from wayback.exceptions import MementoPlaybackError
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.collection import (
    ArchiveLayout,
    cleanup_temps,
    ensure_collection_dirs,
    list_collection_warcs,
)
from archive_magic_fetch.config import StorageConfig
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import publish_collection_index
from archive_magic_fetch.models import FailureCategory, PlaybackResult
from archive_magic_fetch.protocol import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_URLKEY_HEADER,
)
from archive_magic_fetch.inventory import (
    get_warc_identity,
    inventory_collection,
)
from archive_magic_fetch.playback import classify_playback_error, download_exact, payload_digest
from archive_magic_fetch.warc import (
    CollectionWarcWriter,
    salvage_collection_partials,
    truncate_incomplete_gzip_warc,
    validate_warc,
)
from helpers import cdx_json, found_capture_client, make_capt, memento_client, patch_cdx, playback, substitution_client

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
    result = download_exact(
        memento_client(identity, b"", headers={"Location": location}),
        identity,
    )
    assert result.status_code == 302
    assert result.body == b""
    assert result.digest_matched is True
    assert result.warc_payload_digest == empty_digest
    assert any(name.lower() == "location" and value == location for name, value in result.headers)

    layout = ArchiveLayout(tmp_path, "example.org")
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


def test_slash_redirect_from_cdx_requires_slash_sibling():
    from archive_magic_fetch.playback import (
        SLASH_REDIRECT_SOURCE_URI,
        slash_redirect_from_cdx,
    )

    identity = make_capt(
        url="http://example.org/conference",
        ts="20040303170500",
        status="301",
    )
    result = slash_redirect_from_cdx(
        identity,
        group_urls=(
            "http://example.org/conference",
            "http://example.org/conference/",
        ),
    )
    assert result is not None
    assert result.source_uri == SLASH_REDIRECT_SOURCE_URI
    assert result.status_code == 301
    assert result.body == b""
    assert ("Location", "http://example.org/conference/") in result.headers
    assert (
        slash_redirect_from_cdx(
            identity,
            group_urls=("http://example.org:80/conference/",),
        )
        is not None
    )
    assert (
        slash_redirect_from_cdx(
            identity, group_urls=("http://example.org/conference",)
        )
        is None
    )
    assert (
        slash_redirect_from_cdx(
            make_capt(url="http://example.org/conference/", status="301"),
            group_urls=("http://example.org/conference/",),
        )
        is None
    )
    assert (
        slash_redirect_from_cdx(
            make_capt(url="http://example.org/conference", status="200"),
            group_urls=("http://example.org/conference/",),
        )
        is None
    )


def test_slash_redirect_substitution_is_reconstructed():
    from archive_magic_fetch.playback import (
        SLASH_REDIRECT_SOURCE_URI,
        download_exact,
    )

    identity = make_capt(
        url="http://example.org/conference",
        ts="20040303170500",
        status="301",
        digest="sha1:TV7A2C32YG3CFKH2CYRHAL2D4UPH7RCE",
    )
    client = substitution_client("http://example.org/conference/", "20040510064339")
    result = download_exact(client, identity)
    assert result.status_code == 301
    assert result.body == b""
    assert result.source_uri == SLASH_REDIRECT_SOURCE_URI
    assert result.digest_matched is True
    assert ("Location", "http://example.org/conference/") in result.headers
    assert client.calls == 1

    def client_for(location: str):
        response = MagicMock()
        response.headers = {
            "X-Archive-Redirect-Reason": "found capture at 20040510064339",
            "Location": location,
        }

        class Client:
            def __init__(self):
                self.session = MagicMock()
                self.session.request.return_value = response

            def get_memento(self, *args, **kwargs):
                self.session.request("GET", "https://web.archive.org/web/x")
                raise MementoPlaybackError("could not be played")

        return Client()

    with pytest.raises(MementoPlaybackError):
        download_exact(
            client_for(
                "https://web.archive.org/web/20040510064339id_/http://example.org/elsewhere"
            ),
            identity,
        )
    with pytest.raises(MementoPlaybackError):
        download_exact(
            client_for(
                "https://web.archive.org/web/20040510064339id_/http://example.org/conference/"
            ),
            make_capt(
                url="http://example.org/conference",
                ts="20040303170500",
                status="200",
            ),
        )


def test_slash_redirect_substitution_accepts_default_port_and_relative_location():
    from archive_magic_fetch.playback import download_exact

    identity = make_capt(
        url="http://example.org/policy/edu",
        ts="20040316070310",
        status="301",
    )
    response = MagicMock()
    response.headers = {
        "X-Archive-Redirect-Reason": "found capture at 20040326073528",
        "Location": "/web/20040326073528id_/http://example.org:80/policy/edu/",
    }

    class Client:
        def __init__(self):
            self.session = MagicMock()
            self.session.request.return_value = response

        def get_memento(self, *args, **kwargs):
            self.session.request("GET", "https://web.archive.org/web/x")
            raise MementoPlaybackError("could not be played")

    result = download_exact(Client(), identity)
    assert result.status_code == 301
    assert ("Location", "http://example.org/policy/edu/") in result.headers


def test_found_capture_substitution_is_kept_under_requested_identity():
    from archive_magic_fetch.playback import download_exact

    identity = make_capt(
        url="http://example.org/groups/?PHPSESSID=abc",
        ts="20041009172745",
        status="200",
        digest="sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    body = b"<html>groups</html>"
    client = found_capture_client(
        "http://example.org/groups/",
        "20041009202542",
        body,
    )
    result = download_exact(client, identity)
    assert client.calls == 2
    assert result.substituted is True
    assert result.body == body
    assert result.identity == identity
    assert result.warc_date == "2004-10-09T17:27:45Z"
    assert result.source_uri.endswith("id_/http://example.org/groups/")
    assert result.digest_matched is False
    assert result.status_code == 200


def test_inventory_remembers_redirect_representative_by_status(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    empty = payload_digest(b"")
    identity = make_capt(
        url="http://example.org/thecase",
        ts="20040603000000",
        status="301",
        digest=empty,
    )
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(identity, body=b"", status=301))
    writer.close()
    publish_collection_index(layout, "2004")

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


def test_download_exact_accepts_ia_double_encoded_original_url():
    """IA Link rel=original may %25-escape already-encoded query bytes."""

    from archive_magic_fetch.playback import ExactMismatchError, download_exact

    cdx_url = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%20DESC&state=46"
    )
    link_url = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%2520DESC&state=46"
    )
    identity = make_capt(url=cdx_url, ts="20041116040449")
    body = b"<html>ok</html>"
    result = download_exact(
        memento_client(identity, body, returned_url=link_url),
        identity,
    )
    assert result.identity.original_url == cdx_url
    assert result.body == body
    mismatch = make_capt(url="http://example.org/a")
    with pytest.raises(ExactMismatchError, match="URL mismatch"):
        download_exact(
            memento_client(
                mismatch,
                b"x",
                returned_url="http://example.org/b",
            ),
            mismatch,
        )


def test_cdx_digest_matches_body_accepts_trailing_newline_soft_match():
    from archive_magic_fetch.playback import (
        cdx_digest_matches_body,
        download_exact,
        payload_digest,
    )

    body = b"GIF89a-soft-match"
    exact = payload_digest(body)
    soft = payload_digest(body + b"\n")
    other = payload_digest(b"different")

    assert cdx_digest_matches_body(exact, body) is True
    assert cdx_digest_matches_body(soft, body) is True
    assert cdx_digest_matches_body(other, body) is False
    assert cdx_digest_matches_body(None, body) is True

    identity = make_capt(digest=soft)
    result = download_exact(
        memento_client(identity, body), identity
    )
    assert result.digest_matched is True
    assert result.body == body
    assert result.warc_payload_digest == exact
    assert result.warc_payload_digest != soft


def test_trailing_newline_soft_match_seeds_revisit_and_survives_inventory(
    tmp_path,
):
    """IA CDX hashed body+LF; playback body without LF still revisits."""

    body = b"<html>soft</html>"
    dig = payload_digest(body + b"\n").split(":")[1]
    downloads: list[str] = []

    def download_fn(_client, identity):
        from archive_magic_fetch.playback import download_exact

        downloads.append(identity.timestamp)
        return download_exact(
            memento_client(identity, body), identity
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
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
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
        cdx_mod.fetch_year_cdx = original
        fetch_mod.fetch_year_cdx = original

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
    assert inv.lookup_representative(
        "com,example)/",
        f"sha1:{dig}",
        "200",
        not_after_timestamp="20040602000000",
    ) is not None


def test_empty_non_redirect_playback_is_rejected_when_cdx_digest_is_nonempty():
    from archive_magic_fetch.playback import (
        UnusablePlaybackError,
        download_exact,
    )

    identity = make_capt(status="200")
    with pytest.raises(UnusablePlaybackError, match="empty playback body"):
        download_exact(memento_client(identity, b""), identity)


def test_empty_http_200_matching_cdx_digest_is_stored():
    from archive_magic_fetch.protocol import EMPTY_PAYLOAD_DIGEST
    from archive_magic_fetch.playback import download_exact

    identity = make_capt(status="200", digest=EMPTY_PAYLOAD_DIGEST)
    result = download_exact(memento_client(identity, b""), identity)
    assert result.body == b""
    assert result.status_code == 200
    assert result.digest_matched is True
    assert result.warc_payload_digest == EMPTY_PAYLOAD_DIGEST


def test_empty_http_200_from_cdx_skips_redirects():
    from archive_magic_fetch.protocol import EMPTY_PAYLOAD_DIGEST
    from archive_magic_fetch.playback import empty_http_200_from_cdx

    empty_200 = make_capt(status="200", digest=EMPTY_PAYLOAD_DIGEST)
    result = empty_http_200_from_cdx(empty_200, mime="text/html")
    assert result is not None
    assert result.body == b""
    assert result.headers == (("Content-Type", "text/html"), ("Content-Length", "0"))
    assert empty_http_200_from_cdx(
        make_capt(status="301", digest=EMPTY_PAYLOAD_DIGEST), mime="text/html"
    ) is None
    assert empty_http_200_from_cdx(make_capt(status="200"), mime="text/html") is None


def test_invalid_uri_playback_is_always_rejected():
    from archive_magic_fetch.playback import (
        UnusablePlaybackError,
        classify_playback_error,
        download_exact,
    )

    identity = make_capt()
    with pytest.raises(UnusablePlaybackError):
        download_exact(
            memento_client(identity, b"Invalid URI"), identity
        )
    category, retryable = classify_playback_error(
        UnusablePlaybackError("IA playback stub: Invalid URI")
    )
    assert category == FailureCategory.UNAVAILABLE
    assert retryable is False


def test_custom_cdx_urlkey_survives_warc_inventory(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
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
    publish_collection_index(layout, "2004")
    inv = inventory_collection(layout, "2004")
    assert inv.contains(identity)


def test_digest_mismatch_is_kept_but_never_seeds_revisit(tmp_path):

    from archive_magic_fetch.protocol import CDX_DIGEST_MATCH_HEADER

    layout = ArchiveLayout(tmp_path, "example.org")
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
                archive_id="example.org",
                    storage=StorageConfig("local", tmp_path),
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
        "com,example)/", claimed, "200", not_after_timestamp="20040602000000"
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
    layout = ArchiveLayout(tmp_path, "example.org")
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
        assert artifact.record_count == 2
        assert validate_warc(artifact.path) == artifact.record_count

    for seq in range(1, 1000):
        path = layout.collection_warc_path("2005", seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    with pytest.raises(RuntimeError, match="999"):
        CollectionWarcWriter(layout, "2005", target_bytes=1)


def test_writer_uses_visible_partial_beside_destination(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt()))
    partial = layout.collection_warc_partial_path("2004", 1)
    assert writer.temp_path == partial
    assert partial.name == "example.org-2004-001.warc.gz.partial"
    assert not partial.name.startswith(".")
    assert partial.parent == layout.collection_dir("2004")
    writer.close()
    assert not partial.exists()
    assert list_collection_warcs(layout, "2004")[0].name == (
        "example.org-2004-001.warc.gz"
    )


def test_resume_appends_to_same_shard_under_size_cap(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    first = make_capt(ts="20040601000000")
    second = make_capt(
        ts="20040602000000",
        digest="sha1:" + "B" * 32,
    )
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(first))
    writer.close()

    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(second, body=b"later"))
    writer.close()

    warcs = list_collection_warcs(layout, "2004")
    assert [path.name for path in warcs] == ["example.org-2004-001.warc.gz"]
    publish_collection_index(layout, "2004")
    inv = inventory_collection(layout, "2004")
    assert inv.contains(first)
    assert inv.contains(second)


def test_resume_starts_next_shard_when_last_is_at_cap(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
    writer.write_playback(playback(make_capt(ts="20040601000000")))
    writer.close()
    assert [path.name for path in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz"
    ]

    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
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


def test_salvage_promotes_collection_partial_and_truncates_garbage(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    capt = make_capt()
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(capt))
    partial = writer.temp_path
    assert partial is not None
    writer.stream.close()
    writer.stream = None
    writer.writer = None
    partial.write_bytes(partial.read_bytes() + b"\x00torn-member")

    salvaged = salvage_collection_partials(layout)

    assert len(salvaged) == 1
    assert salvaged[0].path.name == "example.org-2004-001.warc.gz"
    assert not partial.exists()
    publish_collection_index(layout, "2004")
    assert inventory_collection(layout, "2004").contains(capt)
    validate_warc(salvaged[0].path)


def test_cleanup_temps_keeps_visible_warc_partials(tmp_path):
    layout = ArchiveLayout(tmp_path, "example.org")
    ensure_collection_dirs(layout)
    collection_dir = layout.collection_dir("2004")
    collection_dir.mkdir(parents=True)
    partial = layout.collection_warc_partial_path("2004", 1)
    partial.write_bytes(b"keep-me")
    stray = collection_dir / ".tmp-index.cdxj.tmp"
    stray.write_text("x", encoding="utf-8")

    cleanup_temps(layout)

    assert partial.exists()
    assert not stray.exists()


def test_truncate_drops_warcinfo_only_partial(tmp_path):
    empty = tmp_path / "empty.warc.gz.partial"
    empty.write_bytes(b"")
    assert truncate_incomplete_gzip_warc(empty) is None
    assert not empty.exists()
