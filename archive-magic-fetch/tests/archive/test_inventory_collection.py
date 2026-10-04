"""CDXJ capture identities and safe reusable response references."""

import json
from dataclasses import replace

import pytest

from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.archive.inventory_collection import (
    inventory_collection, revisit_from_stored, stored_from_capture,
)
from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.pipeline.stage_year import YearStage
from archive_magic_fetch.pipeline.write_captures import _CollectionWarcWriter
from helpers import make_capt, playback


def test_generated_inventory_preserves_identities_status_scope_and_earliest_response(tmp_path):
    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    writer = _CollectionWarcWriter(stage.layout, "2004", target_bytes=1)
    first = make_capt(ts="20040601000000", status="-", digest=payload_digest(b"hello"))
    later = replace(first, timestamp="20040602000000")
    revisit = replace(first, timestamp="20040603000000")
    different_url = replace(first, urlkey="com,example)/other", original_url="http://example.org/other")
    missing = make_capt(url="http://example.org/missing", digest="-")
    mismatch = make_capt(url="http://example.org/mismatch", digest=payload_digest(b"hello"))
    unequal = make_capt(url="http://example.org/unequal", digest=payload_digest(b"hello\n"))
    response = playback(first, status=301)
    writer.write_playback(playback(later, status=302))
    writer.write_playback(response)  # Physical order does not select the representative.
    writer.write_revisit(revisit_from_stored(revisit, stored_from_capture(response)))
    writer.write_playback(playback(different_url))
    writer.write_playback(playback(missing))
    writer.write_playback(replace(playback(mismatch), digest_matched=False))
    writer.write_playback(playback(unequal))
    writer.close()
    inventory = stage.prepare_inventory()
    assert inventory.identities == {first, later, revisit, different_url, missing, mismatch, unequal}
    stored = inventory.lookup_representative(first.urlkey, first.payload_digest, "-",
                                             not_after_timestamp=revisit.timestamp)
    assert stored == stored_from_capture(response)
    assert revisit_from_stored(replace(revisit, timestamp="20040604000000"), stored).http_status_code == 301
    assert inventory.lookup_representative(first.urlkey, first.payload_digest, "200",
                                           not_after_timestamp="20040501000000") is None
    assert inventory.lookup_representative(first.urlkey, first.payload_digest, "200",
                                           not_after_timestamp="20030601000000") is None
    for capture in (missing, mismatch, unequal):
        assert inventory.lookup_representative(capture.urlkey, capture.payload_digest, capture.status_token,
                                               not_after_timestamp="20041201000000") is None
    assert len(inventory.by_url_digest) == 2


@pytest.mark.parametrize("cdx_status,expected", [("301", 301), ("-", 200)])
def test_legacy_row_without_http_status_retains_cdx_fallback(tmp_path, cdx_status, expected):
    layout = ArchiveLayout(tmp_path, "example.org")
    capture = make_capt(status=cdx_status, digest=payload_digest(b"hello"))
    row = {"url": capture.original_url, "cdxStatus": cdx_status,
           "cdxDigest": capture.payload_digest, "digest": capture.payload_digest,
           "cdxDigestMatch": True, "mime": "text/html"}
    layout.collection_index("2004").write_text(f"{capture.urlkey} {capture.timestamp} {json.dumps(row)}\n")
    inventory = inventory_collection(layout, "2004")
    assert inventory.identities == {capture}
    assert next(iter(inventory.by_url_digest.values())).status_code == expected


@pytest.mark.parametrize("digest,flag,reusable", [
    (payload_digest(b"hello").lower().replace("sha1:", ""), True, True),
    (payload_digest(b"different"), True, False),
    (payload_digest(b"hello"), False, False),
    (payload_digest(b"hello"), None, False),
    (None, True, False),
    ("invalid", True, False),
])
def test_representatives_require_equal_valid_digests_and_affirmative_flag(tmp_path, digest, flag, reusable):
    layout = ArchiveLayout(tmp_path, "example.org")
    capture = make_capt(digest=payload_digest(b"hello"))
    row = {"url": capture.original_url, "cdxStatus": "200", "status": "200",
           "cdxDigest": capture.payload_digest, "digest": digest, "cdxDigestMatch": flag}
    layout.collection_index("2004").write_text(f"{capture.urlkey} {capture.timestamp} {json.dumps(row)}\n")
    inventory = inventory_collection(layout, "2004")
    assert inventory.identities == {capture}
    assert bool(inventory.by_url_digest) is reusable


def test_empty_payload_statuses_and_revisits_keep_distinct_identity_and_reuse_rules(tmp_path):
    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    redirect = make_capt(status="301", digest=payload_digest(b""))
    different = replace(redirect, status_token="302", timestamp="20040616000000")
    revisit = replace(redirect, timestamp="20040617000000")
    result = playback(redirect, body=b"", status=301)
    writer.write_playback(result)
    writer.write_revisit(revisit_from_stored(revisit, stored_from_capture(result)))
    writer.close()
    inventory = stage.prepare_inventory()
    assert inventory.identities == {redirect, revisit}
    assert not inventory.contains(different)
    assert inventory.lookup_representative(different.urlkey, different.payload_digest, "302",
                                           not_after_timestamp=different.timestamp) is None
    assert len(inventory.by_url_digest) == 1
