"""Portable collection indexing."""

from __future__ import annotations

import json

import pytest

from archive_magic_fetch.collection import (
    ArchiveLayout,
    ensure_collection_dirs,
    list_collection_warcs,
    reset_collection_data,
)
from archive_magic_fetch.index import (
    publish_collection_index,
    reconcile_missing_indexes,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.playback import payload_digest
from archive_magic_fetch.warc import CollectionWarcWriter
from helpers import make_capt, playback


def test_collection_index_beside_warcs_covers_multi_shard_year(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    assert index.relative_key == "example.org-2004-index.cdxj"
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


def test_incremental_index_replaces_tail_lines_without_reading_earlier_warcs(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
    first = make_capt(ts="20040601000000", digest="sha1:" + "A" * 32)
    second = make_capt(ts="20040602000000", digest="sha1:" + "B" * 32)
    writer.write_playback(playback(first, body=b"first"))
    writer.write_playback(playback(second, body=b"second"))
    writer.close()
    initial = publish_collection_index(layout, "2004")
    assert initial is not None

    first_warc, tail = list_collection_warcs(layout, "2004")
    sizes = {first_warc.name: first_warc.stat().st_size, tail.name: tail.stat().st_size}
    first_lines = [
        line
        for line in initial.path.read_text().splitlines()
        if json.loads(line.split(" ", 2)[2])["filename"] == first_warc.name
    ]
    first_warc.unlink()

    writer = CollectionWarcWriter(layout, "2004")
    third = make_capt(ts="20040603000000", digest="sha1:" + "C" * 32)
    writer.write_playback(playback(third, body=b"third"))
    changed = writer.close()
    sizes[tail.name] = tail.stat().st_size
    updated = publish_collection_index(
        layout,
        "2004",
        changed_warcs=[item.path for item in changed],
        warc_sizes=sizes,
    )

    assert updated is not None
    lines = updated.path.read_text().splitlines()
    assert lines == sorted(lines)
    assert all(line in lines for line in first_lines)
    assert {json.loads(line.split(" ", 2)[2])["cdxDigest"] for line in lines} == {
        first.payload_digest,
        second.payload_digest,
        third.payload_digest,
    }


def test_reconcile_missing_indexes_replaces_only_changed_warcs(tmp_path, monkeypatch):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004", target_bytes=1)
    first = make_capt(ts="20040601000000", digest="sha1:" + "A" * 32)
    second = make_capt(ts="20040602000000", digest="sha1:" + "B" * 32)
    writer.write_playback(playback(first, body=b"first"))
    writer.write_playback(playback(second, body=b"second"))
    writer.close()
    publish_collection_index(layout, "2004")
    first_warc = list_collection_warcs(layout, "2004")[0]
    first_lines = [
        line
        for line in layout.collection_index("2004").read_text().splitlines()
        if json.loads(line.split(" ", 2)[2])["filename"] == first_warc.name
    ]

    writer = CollectionWarcWriter(layout, "2004")
    third = make_capt(ts="20040603000000", digest="sha1:" + "C" * 32)
    writer.write_playback(playback(third, body=b"third"))
    changed = writer.close()
    calls: list[list[str] | None] = []
    real = publish_collection_index

    def wrapped(layout, collection_id, **kwargs):
        paths = kwargs.get("changed_warcs")
        calls.append(None if paths is None else [path.name for path in paths])
        return real(layout, collection_id, **kwargs)

    monkeypatch.setattr("archive_magic_fetch.index.publish_collection_index", wrapped)
    assert reconcile_missing_indexes(layout) == ["2004"]
    assert calls == [[item.path.name for item in changed]]
    lines = layout.collection_index("2004").read_text().splitlines()
    assert all(line in lines for line in first_lines)
    assert {json.loads(line.split(" ", 2)[2])["cdxDigest"] for line in lines} == {
        first.payload_digest,
        second.payload_digest,
        third.payload_digest,
    }


@pytest.mark.parametrize("collection_id", ("", "../2004", "nested/2004", "bad id"))
def test_generic_collection_ids_must_be_filesystem_safe(tmp_path, collection_id):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    with pytest.raises(ValueError, match="unsafe collection ID"):
        layout.collection_dir(collection_id)


def test_collection_index_keeps_ia_and_local_soft_match_digests_separate(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    body = b"soft-match"
    ia_digest = payload_digest(body + b"\n")
    capt = make_capt(digest=ia_digest)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(capt, body=body))
    writer.close()

    index = publish_collection_index(layout, "2004")

    meta = json.loads(index.path.read_text().split(" ", 2)[2])
    assert meta["cdxDigest"] == ia_digest
    assert meta["cdxDigestMatch"] is True
    assert meta["digest"] == payload_digest(body)


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
    layout = ArchiveLayout(tmp_path / "data", "example.org")
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
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt(ts="20040601000000")))
    writer.close()
    publish_collection_index(layout, "2004")

    index_path = layout.collection_index("2004")
    assert list_collection_warcs(layout, "2004")
    assert index_path.is_file()
    partial = layout.collection_dir("2004") / "example.org-2004-001.warc.gz.partial"
    partial.write_bytes(b"in-progress")

    reset_collection_data(layout, "2004")

    assert list_collection_warcs(layout, "2004") == []
    assert not index_path.is_file()
    assert not partial.exists()
