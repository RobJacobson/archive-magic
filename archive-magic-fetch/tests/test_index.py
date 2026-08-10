"""Portable collection indexing and revisit closure."""

from __future__ import annotations

import json

import pytest

from archive_magic_fetch.collection import (
    archive_layout,
    ensure_collection_dirs,
    list_collection_warcs,
    reset_collection_data,
)
from archive_magic_fetch.index import (
    publish_collection_index,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.warc import (
    CollectionWarcWriter,
    StoredResponse,
    inventory_collection,
    payload_digest,
    revisit_from_stored,
)
from helpers import make_capt, playback


def test_orphan_revisit_in_warc_is_rejected(tmp_path):
    """Revisits must resolve to a full response in the same collection."""

    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"body"
    dig = payload_digest(body)
    response_id = make_capt(ts="20040601000000", digest=dig, status="200")
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(response_id, body=body, status=200))
    # Orphan: refers to a never-written future response
    ghost = StoredResponse(
        identity=make_capt(ts="20040602000000", digest="sha1:" + "F" * 32),
        warc_date="2004-06-02T00:00:00Z",
        warc_payload_digest="sha1:" + "F" * 32,
        target_uri="http://example.org/",
        status_code=200,
    )
    revisit_id = make_capt(
        ts="20040603000000",
        digest="sha1:" + "F" * 32,
        status="200",
    )
    writer.write_revisit(revisit_from_stored(revisit_id, ghost))
    writer.close()
    with pytest.raises(ValueError, match="no earlier response"):
        publish_collection_index(layout, "2004")


def test_cross_year_revisit_closure_is_rejected(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"across-years"
    dig = payload_digest(body)
    older = make_capt(ts="20040601000000", digest=dig)
    newer = make_capt(ts="20050601000000", digest=dig)

    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(older, body=body))
    writer.close()
    publish_collection_index(layout, "2004")

    stored = StoredResponse(
        identity=older,
        warc_date="2004-06-01T00:00:00Z",
        warc_payload_digest=dig,
        target_uri=older.original_url,
        status_code=200,
    )
    writer = CollectionWarcWriter(layout, "2005")
    writer.write_revisit(revisit_from_stored(newer, stored))
    writer.close()
    with pytest.raises(ValueError, match="no earlier response"):
        publish_collection_index(layout, "2005")


def test_forward_revisit_reference_is_rejected(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    body = b"body"
    dig = payload_digest(body)
    future = make_capt(ts="20050601000000", digest=dig)
    writer = CollectionWarcWriter(layout, "2005")
    writer.write_playback(playback(future, body=body))
    writer.close()

    stored_future = StoredResponse(
        identity=future,
        warc_date="2005-06-01T00:00:00Z",
        warc_payload_digest=dig,
        target_uri=future.original_url,
        status_code=200,
    )
    earlier = make_capt(ts="20040601000000", digest=dig)
    # Put both records in 2004: a revisit that points forward in time (same
    # collection) is rejected even if the target response is co-located.
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(future, body=body))
    writer.write_revisit(revisit_from_stored(earlier, stored_future))
    writer.close()
    with pytest.raises(ValueError, match="forward reference"):
        publish_collection_index(layout, "2004")


def test_collection_index_beside_warcs_covers_multi_shard_year(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
    for i in range(2):
        capt = make_capt(
            ts=f"2004060{i+1}000000",
            digest="sha1:" + ("F" * 31 + str(i)),
        )
        writer.write_playback(playback(capt, body=b"x" * 100))
    warcs = writer.close()
    assert len(warcs) == 2

    index = publish_collection_index(layout, "2004")
    assert index is not None
    assert index.relative_key == "collections/2004/example.org-2004-index.cdxj"
    assert layout.collection_index("2004") == (
        layout.collection_dir("2004") / "example.org-2004-index.cdxj"
    )
    assert layout.collection_index("2004").is_file()
    assert not (layout.root / "archive").exists()

    names = set()
    for line in layout.collection_index("2004").read_text().splitlines():
        if not line.strip():
            continue
        meta = json.loads(line.split(" ", 2)[2])
        names.add(meta["filename"])
    assert names == {
        "example.org-2004-001.warc.gz",
        "example.org-2004-002.warc.gz",
    }
    # Only expected index basenames are considered for that collection;
    # foreign companion names are ignored by list_collection_warcs.
    collection_dir = layout.collection_dir("2004")
    (collection_dir / "other.org-2004.cdxj").write_text("x\n", encoding="utf-8")
    assert [p.name for p in list_collection_warcs(layout, "2004")] == [
        "example.org-2004-001.warc.gz",
        "example.org-2004-002.warc.gz",
    ]


def test_crash_recovery_indexes_finalized_warc_without_redownload(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    capt = make_capt()
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(capt))
    warcs = writer.close()
    assert warcs
    assert not layout.collection_index("2004").exists()
    publish_collection_index(layout, "2004")
    assert layout.collection_index("2004").is_file()
    assert layout.collection_index("2004").name == "example.org-2004-index.cdxj"
    inv = inventory_collection(layout, "2004")
    assert inv.contains(capt)


@pytest.mark.parametrize("collection_id", ("", "../2004", "nested/2004", "bad id"))
def test_generic_collection_ids_must_be_filesystem_safe(tmp_path, collection_id):
    layout = archive_layout("http://example.org/", tmp_path)
    with pytest.raises(ValueError, match="unsafe collection ID"):
        layout.collection_dir(collection_id)


def test_generic_collection_writer_and_index_are_portable(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "campaign-launch")
    writer.write_playback(playback(make_capt()))
    warcs = writer.close()

    index = publish_collection_index(layout, "campaign-launch")

    assert warcs[0].path.name == "example.org-campaign-launch-001.warc.gz"
    assert index is not None
    assert index.path.name == "example.org-campaign-launch-index.cdxj"
    payload = json.loads(index.path.read_text().split(" ", 2)[2])
    assert payload["filename"] == warcs[0].path.name


@pytest.mark.parametrize(
    ("filename", "match"),
    (
        ("other.org-2004-001.warc.gz", "foreign WARC"),
        ("example.org-2004-001.warc.gz", "foreign WARC"),
        ("collections/2004/example.org-2004-001.warc.gz", "basename"),
        (None, "out of bounds"),
    ),
)
def test_validate_cdxj_rejects_unsafe_locators(tmp_path, filename, match):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt(ts="20040601000000")))
    writer.close()
    writer = CollectionWarcWriter(layout, "2005")
    writer.write_playback(playback(make_capt(ts="20050601000000")))
    writer.close()

    # Validate against 2005 so a 2004-shaped basename is foreign.
    warcs = list_collection_warcs(layout, "2005")
    size = warcs[0].stat().st_size
    if match == "out of bounds":
        length = size + 1
        fname = warcs[0].name
    else:
        length = 10
        fname = filename
    line = (
        'com,example)/ 20050601000000 {"url":"http://example.org/",'
        f'"filename":"{fname}","offset":0,"length":{length}}}'
    )
    with pytest.raises(ValueError, match=match):
        validate_cdxj_against_warcs(layout, "2005", [line])


def test_reset_collection_data_deletes_warc_and_cdxj(tmp_path):
    layout = archive_layout("http://example.org/", tmp_path)
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt(ts="20040601000000")))
    writer.close()
    publish_collection_index(layout, "2004")

    index_path = layout.collection_index("2004")
    assert list_collection_warcs(layout, "2004")
    assert index_path.is_file()

    reset_collection_data(layout, "2004")

    assert list_collection_warcs(layout, "2004") == []
    assert not index_path.is_file()
