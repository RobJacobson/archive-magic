from datetime import datetime, timezone

import pytest
from wayback import CdxRecord

from archive_magic_fetch.capture_identity import (
    get_cdx_identity,
    get_warc_identity,
)
from archive_magic_fetch.downloads import DownloadedCapture, payload_digest


def capture(*, digest, status=200):
    return CdxRecord(
        urlkey="com,example)/resource",
        timestamp=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        original="HTTP://EXAMPLE.COM:80/resource",
        mimetype="text/plain",
        statuscode=status,
        digest=digest,
        length=7,
    )


def record_for(selected, body=b"played bytes"):
    return DownloadedCapture(
        body=body,
        url="http://example.com/resource",
        capture_date="2020-01-02T03:04:05Z",
        source_uri=selected.raw_url,
        status_code=selected.statuscode,
        headers=(("Content-Type", "text/plain"),),
    ).to_warc_record(
        cdx_payload_digest=selected.digest,
        target_url="http://example.com/resource",
    )


def test_cdx_and_warc_identity_share_source_digest_not_actual_payload():
    selected = capture(digest="A" * 32)
    record = record_for(selected)

    assert get_warc_identity(record) == get_cdx_identity(selected)
    assert record.rec_headers.get_header(
        "CDX-Payload-Digest"
    ) == "sha1:" + "A" * 32
    assert record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == payload_digest(b"played bytes")
    assert record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) != record.rec_headers.get_header("CDX-Payload-Digest")


def test_missing_cdx_digest_uses_explicit_sentinel():
    selected = capture(digest="-")
    record = record_for(selected)

    assert record.rec_headers.get_header("CDX-Payload-Digest") == "-"
    assert get_warc_identity(record) == get_cdx_identity(selected)
    assert get_warc_identity(record).payload_digest is None


def test_missing_or_invalid_warc_cdx_digest_is_rejected():
    selected = capture(digest="A" * 32)
    missing = record_for(selected)
    missing.rec_headers.remove_header("CDX-Payload-Digest")
    invalid = record_for(selected)
    invalid.rec_headers.replace_header("CDX-Payload-Digest", "invalid")

    with pytest.raises(ValueError, match="missing CDX-Payload-Digest"):
        get_warc_identity(missing)
    with pytest.raises(ValueError, match="invalid CDX-Payload-Digest"):
        get_warc_identity(invalid)


def test_same_time_status_or_digest_variants_remain_distinct():
    first = get_cdx_identity(capture(digest="A" * 32, status=200))
    different_status = get_cdx_identity(
        capture(digest="A" * 32, status=404)
    )
    different_digest = get_cdx_identity(
        capture(digest="B" * 32, status=200)
    )

    assert len({first, different_status, different_digest}) == 3
